"""harness/llm client 测试——OpenAI 兼容客户端（httpx.MockTransport 零真实出站）。

覆盖面：非流式 / 流式成功链、请求体与鉴权头、重试（5xx / 429
Retry-After / 连接错误——幂等安全集）、不可重试失败（4xx / 超时）、
响应与 SSE 解析失败、Retry-After 解析与等待策略（零 sleep 确定性）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_fixed

from packages.harness.config.llm_wire import LlmClientConfigModel
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.client import (
    LlmHttpClient,
    LlmRetryableError,
    _WaitRetryAfterFallback,
    parse_retry_after_seconds,
)
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatRequestParams,
)

#: MockTransport handler 类型（请求 → 响应；可抛 httpx 传输异常）。
Handler = Callable[[httpx.Request], httpx.Response]


def _wire(*, api_key: str = "sk-test", model: str = "test-model") -> LlmClientConfigModel:
    """构造测试 wire 配置。"""
    return LlmClientConfigModel(
        provider="openai_compatible",
        base_url="http://llm.test/v1",
        api_key=api_key,
        model=model,
    )


def _user_message() -> list[ChatMessageModel]:
    """单条用户消息。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="你好")]


def _ok_response(content: str = "你好呀") -> httpx.Response:
    """构造合法非流式 200 响应。"""
    return httpx.Response(
        200,
        json={
            "id": "resp-1",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    )


def _sse_response() -> httpx.Response:
    """构造合法 SSE 流（含注释行 / event 行 / 末块 usage / [DONE]）。"""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
        ": keep-alive 注释行",
        "event: ping",
        "",
        'data: {"choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"好"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ]
    body = "\n".join(lines)
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def _always_500(request: httpx.Request) -> httpx.Response:
    """恒 500 handler（重试耗尽路径）。"""
    return httpx.Response(500, text="upstream down")


def _always_429_retry_after_3600(request: httpx.Request) -> httpx.Response:
    """恒 429 + 超长 Retry-After handler（封顶路径）。"""
    return httpx.Response(429, headers={"Retry-After": "3600"}, text="slow down")


def _bad_json_200(request: httpx.Request) -> httpx.Response:
    """200 但非 JSON handler。"""
    return httpx.Response(200, text="<html>not json</html>")


def _empty_choices_200(request: httpx.Request) -> httpx.Response:
    """200 但 choices 为空 handler。"""
    return httpx.Response(200, json={"id": "r", "model": "test-model", "choices": []})


def _malformed_sse(request: httpx.Request) -> httpx.Response:
    """SSE data 块非法 JSON handler。"""
    body = "data: {broken json\n\ndata: [DONE]\n\n"
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def _tail_only_sse(request: httpx.Request) -> httpx.Response:
    """usage-only 末块 + 无 usage 空块 handler。"""
    lines = [
        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
        'data: {"choices":[],"usage":null}',
        "data: [DONE]",
    ]
    return httpx.Response(
        200,
        content="\n".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[..., LlmHttpClient]]:
    """client 工厂（每用例注入 MockTransport 共享 client；用后统一关闭）。

    Yields:
        工厂函数——``make_client(handler, **client kwargs)``。
    """
    created: list[httpx.AsyncClient] = []

    def _make(handler: Handler, **kwargs: Any) -> LlmHttpClient:
        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(shared)
        return LlmHttpClient(_wire(), shared, **kwargs)

    yield _make
    for shared in created:
        await shared.aclose()


# ---------------------------------------------------------------------------
# 非流式
# ---------------------------------------------------------------------------


async def test_chat_success_sends_wire_request(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """非流式成功：端点 URL / 鉴权头 / 请求体 / 归一响应全链。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    client = make_client(handler)
    response = await client.chat(_user_message(), ChatRequestParams(temperature=0.7, max_tokens=64))

    assert response.content == "你好呀"
    assert response.finish_reason == "stop"
    assert response.model == "test-model"
    assert response.usage is not None
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 7
    assert response.usage.total_tokens == 12
    assert response.latency_ms >= 0

    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "http://llm.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = request.read().decode("utf-8")
    assert '"model":"test-model"' in body
    assert '"temperature":0.7' in body
    assert '"max_tokens":64' in body
    assert "stream" not in body


async def test_chat_free_key_endpoint_no_auth_header(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """免鉴权端点（api_key 空串）：无 Authorization 头。"""
    captured: list[httpx.Request] = []
    shared = httpx.MockTransport(lambda request: (captured.append(request), _ok_response())[1])
    http_client = httpx.AsyncClient(transport=shared)
    try:
        client = LlmHttpClient(_wire(api_key=""), http_client)
        response = await client.chat(_user_message())
        assert response.content == "你好呀"
        assert "Authorization" not in captured[0].headers
    finally:
        await http_client.aclose()


async def test_chat_retries_500_then_succeeds(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """5xx 可重试：首次 500，重试后 200（传输恰好两次）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="upstream boom")
        return _ok_response()

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    response = await client.chat(_user_message())
    assert response.content == "你好呀"
    assert len(calls) == 2


async def test_chat_retries_429_with_retry_after_zero(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """429 尊重 Retry-After：头值 0 秒 → 立即重试成功（零等待）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        return _ok_response()

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    response = await client.chat(_user_message())
    assert response.content == "你好呀"
    assert len(calls) == 2


async def test_chat_retry_exhausted_reraises(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """重试耗尽：有界次数后上抛 LLM_CALL_FAILED（reraise 语义）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _always_500(request)

    client = make_client(handler, retry_attempts=2, retry_wait_seconds=0.0)

    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert len(calls) == 2


async def test_chat_4xx_not_retried(make_client: Callable[..., LlmHttpClient]) -> None:
    """其余 4xx 不可重试：单次调用即上抛 LLM_CALL_FAILED。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request shape")

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert exc_info.value.details is not None
    assert exc_info.value.details["status_code"] == 400
    assert len(calls) == 1


async def test_chat_401_maps_key_missing(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """401 鉴权失败 → LLM_API_KEY_MISSING（不重试）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="invalid api key")

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_API_KEY_MISSING
    assert len(calls) == 1


async def test_chat_retry_after_capped(make_client: Callable[..., LlmHttpClient]) -> None:
    """Retry-After 封顶：端点声明 3600 秒冷却 → 按 retry_after_max 封顶。"""
    client = make_client(
        _always_429_retry_after_3600, retry_attempts=1, retry_after_max_seconds=0.25
    )

    with pytest.raises(LlmRetryableError) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.retry_after_seconds == 0.25
    assert exc_info.value.details is not None
    assert exc_info.value.details["retry_after_seconds"] == 0.25
    assert exc_info.value.headers == {"Retry-After": "0"}


async def test_chat_connect_error_retried(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """连接错误可重试：首次 ConnectError，重试后成功。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return _ok_response()

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    response = await client.chat(_user_message())
    assert response.content == "你好呀"
    assert len(calls) == 2


async def test_chat_read_timeout_not_retried(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """读超时不可重试：LLM_REQUEST_FAILED，单次调用，details 携带超时配置。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("read timed out")

    client = make_client(
        handler, retry_attempts=3, retry_wait_seconds=0.0, request_timeout_seconds=7.0
    )
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
    assert exc_info.value.details is not None
    assert exc_info.value.details["timeout_seconds"] == 7.0
    assert len(calls) == 1


async def test_chat_malformed_json_response(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """200 但响应体非法 JSON → LLM_CALL_FAILED。"""
    client = make_client(_bad_json_200)
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_chat_empty_choices_rejected(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """200 但 choices 为空 → LLM_CALL_FAILED。"""
    client = make_client(_empty_choices_200)
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_user_message())
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


async def test_stream_success_parses_sse(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """流式成功：请求体 stream 声明 / 增量块序列 / 末块 usage。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response()

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream(_user_message())]

    assert [chunk.content for chunk in chunks] == ["", "你", "好", ""]
    assert chunks[2].finish_reason == "stop"
    assert chunks[3].usage is not None
    assert chunks[3].usage.input_tokens == 3
    assert chunks[3].usage.output_tokens == 2
    assert chunks[3].usage.total_tokens == 5

    body = captured[0].read().decode("utf-8")
    assert '"stream":true' in body
    assert '"include_usage":true' in body


async def test_stream_retries_open_phase(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """流式发起阶段可重试：首次 500，重试后 SSE 正常（首块前重放幂等）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="upstream down")
        return _sse_response()

    client = make_client(handler, retry_attempts=3, retry_wait_seconds=0.0)
    chunks = [chunk async for chunk in client.stream(_user_message())]
    assert len(calls) == 2
    assert chunks[-1].usage is not None


async def test_stream_malformed_data_line(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """流中段 data 块非法 JSON → LLM_CALL_FAILED（不重试）。"""
    client = make_client(_malformed_sse, retry_attempts=3, retry_wait_seconds=0.0)
    with pytest.raises(BusinessException) as exc_info:
        async for _chunk in client.stream(_user_message()):
            pass
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_stream_usage_only_and_empty_tail_chunks(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """末块形态归一：choices 空 + usage → usage-only 块；choices 空无 usage → 空块。"""
    client = make_client(_tail_only_sse)
    chunks = [chunk async for chunk in client.stream(_user_message())]
    assert len(chunks) == 2
    assert chunks[0].usage is not None
    assert chunks[0].usage.total_tokens == 2
    assert chunks[1].usage is None
    assert chunks[1].content == ""


# ---------------------------------------------------------------------------
# Retry-After 解析与等待策略（零 sleep 确定性）
# ---------------------------------------------------------------------------


async def test_parse_retry_after_seconds_variants() -> None:
    """Retry-After 解析：秒数 / 缺失 / 非法 / HTTP-date 未来与过去。"""
    assert parse_retry_after_seconds("120") == 120.0
    assert parse_retry_after_seconds(" 8 ") == 8.0
    assert parse_retry_after_seconds(None) is None
    assert parse_retry_after_seconds("") is None
    assert parse_retry_after_seconds("soon-ish") is None

    future = (datetime.now(tz=UTC) + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    remaining = parse_retry_after_seconds(future)
    assert remaining is not None
    assert 25.0 <= remaining <= 30.0

    past = "Thu, 01 Jan 1970 00:00:00 GMT"
    assert parse_retry_after_seconds(past) == 0.0


def _retry_state_with(exc: Exception) -> RetryCallState:
    """构造携带异常的 tenacity 重试状态（零 sleep 等待策略测试）。"""
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})
    try:
        raise exc
    except Exception as raised:
        state.set_exception((type(raised), raised, raised.__traceback__))
    return state


async def test_wait_strategy_prefers_retry_after() -> None:
    """等待策略：Retry-After 存在 → 优先返回；缺失 → 回落指数退避。"""
    strategy = _WaitRetryAfterFallback(wait_fixed(9.0))
    with_retry_after = _retry_state_with(LlmRetryableError(retry_after_seconds=7.5))
    assert strategy(with_retry_after) == 7.5

    without_retry_after = _retry_state_with(LlmRetryableError())
    assert strategy(without_retry_after) == 9.0

    other_error = _retry_state_with(ValueError("x"))
    assert strategy(other_error) == 9.0
