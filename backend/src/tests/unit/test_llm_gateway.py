"""harness/llm gateway 测试——角色路由 / 池门控 / 熔断上报 / 用量发射。

装配形态：注入解析器（fake 条目）+ 真实 PoolManager（隔离 Settings，
启动后将共享 HTTP client 替换为 MockTransport——零真实出站）+ 假用量
记录器；另含默认接线（wire_default_usage_recorder → usage 服务 →
共享 SQLite 引擎）的网关 → 用量全链测试。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.config.models import LlmEndpointConfig, PoolsConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.context import bind_context
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.gateway import (
    LlmGateway,
    UsageCounter,
    wire_default_usage_counter,
    wire_default_usage_recorder,
)
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    LlmUsageEvent,
)
from packages.harness.pools.manager import PoolManager
from packages.harness.usage.budget_limits import DAILY_BUDGET_MAX
from packages.harness.usage.dtos import UsageQueryDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus
from packages.harness.usage.orm import UsageRecordRow  # 模型注册副作用导入（共享建表）
from packages.harness.usage.service import UsageService

#: MockTransport handler 类型。
Handler = Callable[[httpx.Request], httpx.Response]


def _ok_response(content: str = "回复内容") -> httpx.Response:
    """合法非流式 200 响应。"""
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


def _sse_response() -> httpx.Response:
    """合法 SSE 流（两块内容 + 末块 usage + [DONE]）。"""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"早"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"安"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":6,"completion_tokens":4,"total_tokens":10}}',
        "data: [DONE]",
    ]
    return httpx.Response(
        200,
        content="\n".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


def _entry(
    name: str = "main-chat",
    *,
    roles: tuple[str, ...] = ("chat",),
    api_key_env: str = "",
    base_url: str = "http://llm.test/v1",
) -> Any:
    """构造测试模型条目（parse_model_entry 归一）。"""
    return parse_model_entry(
        {
            "name": name,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "model": "test-model",
            "roles": list(roles),
        }
    )


class RecordingUsageRecorder:
    """假用量记录器（记录事件；可切换为故障注入）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化记录器。

        Args:
            fail: True 时调用即抛异常（网关容错路径验证）。
        """
        self.events: list[LlmUsageEvent] = []
        self.fail = fail

    async def __call__(self, event: LlmUsageEvent) -> None:
        """记录事件（fail 模式抛 RuntimeError）。"""
        if self.fail:
            raise RuntimeError("recorder down")
        self.events.append(event)


async def _make_pool_manager(handler: Handler, **pools_kwargs: Any) -> PoolManager:
    """构造已启动的 PoolManager（共享 client 替换为 MockTransport）。

    Args:
        handler: MockTransport handler。
        **pools_kwargs: PoolsConfig 覆盖项（cb_fail_max 等测试旋钮）。

    Returns:
        已 start 且共享 client 为 MockTransport 的 :class:`PoolManager`
        （调用方负责 ``await manager.stop()``）。
    """
    pools = PoolsConfig(**{"cb_fail_max": 2, **pools_kwargs})
    settings = build_isolated_settings(pools=pools)
    manager = PoolManager(settings)
    await manager.start()
    original = manager.http_client
    manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if original is not None:
        await original.aclose()
    return manager


def _gateway(
    manager: PoolManager,
    *,
    resolver: ModelResolver,
    usage_recorder: Any = None,
    settings_pools: PoolsConfig | None = None,
    settings_llm: LlmEndpointConfig | None = None,
    retry_attempts: int = 1,
) -> LlmGateway:
    """构造注入网关（默认 retry_attempts=1——网关层不重试，client 层已覆盖）。"""
    return LlmGateway(
        resolver=resolver,
        pool_manager=manager,
        settings=build_isolated_settings(
            pools=settings_pools if settings_pools is not None else PoolsConfig(cb_fail_max=2),
            llm=settings_llm,
        ),
        usage_recorder=usage_recorder,
        retry_attempts=retry_attempts,
        retry_wait_seconds=0.0,
    )


def _messages() -> list[ChatMessageModel]:
    """单条用户消息。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="在吗")]


@pytest.fixture
async def gateway_env() -> AsyncIterator[
    tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]]
]:
    """网关测试环境（成功端点 + chat/duty 双角色条目 + 假记录器）。

    Yields:
        ``(gateway, pool_manager, recorder, captured_requests)`` 四元组。
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    manager = await _make_pool_manager(handler)
    recorder = RecordingUsageRecorder()
    gateway = _gateway(
        manager,
        resolver=ModelResolver(
            [
                _entry("main-chat", roles=("chat",)),
                _entry("duty-worker", roles=("duty",)),
            ]
        ),
        usage_recorder=recorder,
    )
    try:
        yield gateway, manager, recorder, captured
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 角色路由 + 成功链
# ---------------------------------------------------------------------------


async def test_chat_by_role_success_emits_usage(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """按角色成功：响应归一 + 用量事件（success / 角色名用途 / token 透传）。"""
    gateway, _manager, recorder, captured = gateway_env
    response = await gateway.chat_by_role("chat", _messages())

    assert response.content == "回复内容"
    assert response.usage is not None
    assert response.usage.total_tokens == 30
    assert len(captured) == 1
    assert str(captured[0].url) == "http://llm.test/v1/chat/completions"

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.status == "success"
    assert event.purpose == "chat"
    assert event.model_entry == "main-chat"
    assert event.provider == "openai_compatible"
    assert event.input_tokens == 10
    assert event.output_tokens == 20
    assert event.total_tokens == 30
    assert event.error_code is None
    assert event.ts_ms > 0
    assert event.latency_ms >= 0


async def test_gateway_passes_logprobs_knobs_to_client() -> None:
    """网关构造旋钮透传传输层：双 None 时请求体整体不含 logprobs 面。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    manager = await _make_pool_manager(handler)
    gateway = LlmGateway(
        resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        retry_attempts=1,
        retry_wait_seconds=0.0,
        top_logprobs=None,
        prompt_logprobs=None,
    )
    try:
        await gateway.chat_by_role("chat", _messages())
    finally:
        await manager.stop()

    sent = json.loads(captured[0].content)
    assert "logprobs" not in sent
    assert "top_logprobs" not in sent
    assert "prompt_logprobs" not in sent


async def test_chat_by_role_reads_context_axes(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """上下文轴：persona_id / request_id 缺省读请求上下文变量。"""
    gateway, _manager, recorder, _captured = gateway_env
    async with bind_context(persona_id="p-ctx", request_id="req-ctx"):
        await gateway.chat_by_role("chat", _messages())
    event = recorder.events[0]
    assert event.persona_id == "p-ctx"
    assert event.request_id == "req-ctx"


async def test_chat_by_role_explicit_overrides(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """显式参数优先：persona_id / purpose / request_id 覆盖上下文与默认。"""
    gateway, _manager, recorder, _captured = gateway_env
    async with bind_context(persona_id="p-ctx", request_id="req-ctx"):
        await gateway.chat_by_role(
            "duty",
            _messages(),
            persona_id="p-explicit",
            purpose="tool",
            request_id="req-explicit",
        )
    event = recorder.events[0]
    assert event.persona_id == "p-explicit"
    assert event.request_id == "req-explicit"
    assert event.purpose == "tool"
    assert event.model_entry == "duty-worker"


async def test_chat_by_role_unresolved_role(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """角色未绑定：MODEL_NOT_FOUND（解析先于传输与用量发射）。"""
    gateway, _manager, recorder, captured = gateway_env
    with pytest.raises(BusinessException) as exc_info:
        await gateway.chat_by_role("embed", _messages())
    assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
    assert recorder.events == []
    assert captured == []


# ---------------------------------------------------------------------------
# 熔断门控 + 失败路径
# ---------------------------------------------------------------------------


async def test_chat_breaker_open_rejects_and_emits_error(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """条目熔断中：LLM_REQUEST_FAILED + error 用量事件 + 零传输。"""
    gateway, manager, recorder, captured = gateway_env
    entry = _entry("main-chat", roles=("chat",))
    breaker = manager.get_llm_entry_pool().get_entry_breaker(entry)
    breaker.record_failure()
    breaker.record_failure()  # cb_fail_max=2 → open

    with pytest.raises(BusinessException) as exc_info:
        await gateway.chat_by_role("chat", _messages())
    assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
    assert exc_info.value.details is not None
    assert exc_info.value.details["reason"] == "模型条目熔断中"
    assert captured == []

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.status == "error"
    assert event.error_code == "LLM_REQUEST_FAILED"
    assert event.total_tokens == 0


async def test_chat_failure_records_breaker_failure_then_success_resets() -> None:
    """失败 → 熔断计失败 + error 事件；随后成功 → 计数清零（结果上报契约）。"""
    state = {"fail": True}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500, text="down")
        return _ok_response()

    manager = await _make_pool_manager(flaky_handler)
    try:
        recorder = RecordingUsageRecorder()
        entry = _entry("main-chat", roles=("chat",))
        gateway = _gateway(manager, resolver=ModelResolver([entry]), usage_recorder=recorder)
        breaker = manager.get_llm_entry_pool().get_entry_breaker(entry)

        with pytest.raises(BusinessException):
            await gateway.chat_by_role("chat", _messages())
        assert breaker.consecutive_failures == 1
        error_events = [e for e in recorder.events if e.status == "error"]
        assert len(error_events) == 1
        assert error_events[0].error_code == "LLM_CALL_FAILED"
        assert error_events[0].model_entry == "main-chat"

        state["fail"] = False
        response = await gateway.chat_by_role("chat", _messages())
        assert response.content == "回复内容"
        assert breaker.consecutive_failures == 0
        assert len([e for e in recorder.events if e.status == "success"]) == 1
    finally:
        await manager.stop()


async def test_usage_recorder_failure_does_not_break_call(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """记录器故障不影响主调用：响应正常返回，事件零记录（告警路径）。"""
    _gateway_ref, manager, _recorder, _captured = gateway_env
    failing = RecordingUsageRecorder(fail=True)
    broken_gateway = _gateway(
        manager,
        resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
        usage_recorder=failing,
    )
    response = await broken_gateway.chat_by_role("chat", _messages())
    assert response.content == "回复内容"
    assert failing.events == []


async def test_local_validation_errors_do_not_pollute_breaker() -> None:
    """本地校验失败不计熔断：空向量化输入与流式内容分块循环 10 次——计数恒 0。

    端点健康信号只反映真实传输结果：本地编程错误（空输入 / 流式携带
    多模态分块）在 entry_gate 之前拒绝，健康端点不因调用方缺陷被打开。
    """
    manager = await _make_pool_manager(lambda request: _ok_response())
    try:
        recorder = RecordingUsageRecorder()
        embed_entry = _entry("embed-worker", roles=("embed",), base_url="http://embed.test/v1")
        chat_entry = _entry("main-chat", roles=("chat",))
        gateway = _gateway(
            manager,
            resolver=ModelResolver([embed_entry, chat_entry]),
            usage_recorder=recorder,
        )
        embed_breaker = manager.get_llm_entry_pool().get_entry_breaker(embed_entry)
        chat_breaker = manager.get_llm_entry_pool().get_entry_breaker(chat_entry)

        for _ in range(10):
            with pytest.raises(BusinessException) as exc_info:
                await gateway.embed_by_role([])
            assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert embed_breaker.consecutive_failures == 0

        multimodal_messages = [
            ChatMessageModel(
                role=ChatMessageRole.USER,
                content=[{"type": "text", "text": "看图"}],
            )
        ]
        for _ in range(10):
            with pytest.raises(BusinessException) as exc_info:
                async for _chunk in gateway.stream_by_role("chat", multimodal_messages):
                    pass
            assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert chat_breaker.consecutive_failures == 0

        # 本地校验失败仍落 error 用量行（观测面保留）
        validation_events = [e for e in recorder.events if e.status == "error"]
        assert len(validation_events) == 20
        assert all(e.error_code == "VALIDATION_ERROR" for e in validation_events)
    finally:
        await manager.stop()


async def test_embed_transport_failure_still_records_breaker() -> None:
    """真实传输失败仍计熔断：本地校验豁免不弱化端点健康信号。"""
    manager = await _make_pool_manager(lambda request: httpx.Response(500, text="down"))
    try:
        entry = _entry("embed-worker", roles=("embed",), base_url="http://embed.test/v1")
        gateway = _gateway(manager, resolver=ModelResolver([entry]))
        breaker = manager.get_llm_entry_pool().get_entry_breaker(entry)
        with pytest.raises(BusinessException) as exc_info:
            await gateway.embed_by_role(["文本"])
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert breaker.consecutive_failures == 1
    finally:
        await manager.stop()


async def test_gateway_requires_started_pools() -> None:
    """池未装配：INTERNAL_ERROR（fail loud——装配顺序错误尽早暴露）。"""
    settings = build_isolated_settings()
    manager = PoolManager(settings)  # 构造但未 start
    gateway = LlmGateway(
        resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
        pool_manager=manager,
        settings=settings,
        retry_attempts=1,
    )
    with pytest.raises(BusinessException) as exc_info:
        await gateway.chat_by_role("chat", _messages())
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "pool_manager.start()" in exc_info.value.message


async def test_gateway_timeout_from_settings() -> None:
    """超时配置下发：settings.pools.llm_request_timeout_seconds → client。"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    manager = await _make_pool_manager(timeout_handler)
    try:
        gateway = _gateway(
            manager,
            resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
            settings_pools=PoolsConfig(llm_request_timeout_seconds=9, cb_fail_max=2),
            retry_attempts=3,  # 超时不可重试——三次配置验证单次上抛
        )
        with pytest.raises(BusinessException) as exc_info:
            await gateway.chat_by_role("chat", _messages())
        assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["timeout_seconds"] == 9
    finally:
        await manager.stop()


async def test_gateway_wire_key_env_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥解析序：条目 api_key_env 命中的环境变量优先于 Settings 兜底。"""
    monkeypatch.setenv("GW_TEST_ENTRY_KEY", "sk-from-env")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(
            manager,
            resolver=ModelResolver(
                [
                    _entry(
                        "main-chat",
                        roles=("chat",),
                        api_key_env="GW_TEST_ENTRY_KEY",
                    )
                ]
            ),
            settings_llm=LlmEndpointConfig(api_key="sk-fallback"),
        )
        await gateway.chat_by_role("chat", _messages())
        assert captured[0].headers["Authorization"] == "Bearer sk-from-env"
    finally:
        await manager.stop()


async def test_gateway_wire_fallback_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥兜底：条目声明变量未设置 → Settings.llm.api_key 生效。"""
    monkeypatch.delenv("GW_TEST_MISSING_KEY", raising=False)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(
            manager,
            resolver=ModelResolver(
                [
                    _entry(
                        "main-chat",
                        roles=("chat",),
                        api_key_env="GW_TEST_MISSING_KEY",
                    )
                ]
            ),
            settings_llm=LlmEndpointConfig(api_key="sk-fallback"),
        )
        await gateway.chat_by_role("chat", _messages())
        assert captured[0].headers["Authorization"] == "Bearer sk-fallback"
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 条目直连（chat_for_entry）
# ---------------------------------------------------------------------------


async def test_chat_for_entry_direct(
    gateway_env: tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]],
) -> None:
    """条目直连：跳过角色解析，purpose 显式（look_at 回退链消费形态）。"""
    gateway, _manager, recorder, captured = gateway_env
    entry = _entry("vision-fallback", roles=(), base_url="http://vision.test/v1")
    response = await gateway.chat_for_entry(
        entry, _messages(), persona_id="p-look", purpose="look_at"
    )
    assert response.content == "回复内容"
    assert str(captured[0].url) == "http://vision.test/v1/chat/completions"
    event = recorder.events[0]
    assert event.purpose == "look_at"
    assert event.model_entry == "vision-fallback"
    assert event.persona_id == "p-look"


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


async def test_stream_by_role_success_aggregates_usage() -> None:
    """流式成功：增量块透传（含 usage-only 末块）+ 聚合进单一成功事件。"""
    manager = await _make_pool_manager(lambda request: _sse_response())
    try:
        recorder = RecordingUsageRecorder()
        gateway = _gateway(
            manager,
            resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
            usage_recorder=recorder,
        )
        chunks = [chunk async for chunk in gateway.stream_by_role("chat", _messages())]
        assert [chunk.content for chunk in chunks] == ["早", "安", ""]
        assert chunks[-1].usage is not None
        assert chunks[-1].usage.total_tokens == 10

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.status == "success"
        assert event.purpose == "chat"
        assert event.input_tokens == 6
        assert event.output_tokens == 4
        assert event.total_tokens == 10
        assert event.error_code is None
    finally:
        await manager.stop()


async def test_stream_failure_emits_error_and_records_breaker() -> None:
    """流式中段失败：error 事件 + 熔断计失败（record_failure 上报）。"""
    body = "data: {broken\n"
    manager = await _make_pool_manager(
        lambda request: httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    try:
        recorder = RecordingUsageRecorder()
        entry = _entry("main-chat", roles=("chat",))
        gateway = _gateway(manager, resolver=ModelResolver([entry]), usage_recorder=recorder)
        with pytest.raises(BusinessException) as exc_info:
            async for _chunk in gateway.stream_by_role("chat", _messages()):
                pass
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

        breaker = manager.get_llm_entry_pool().get_entry_breaker(entry)
        assert breaker.consecutive_failures == 1

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.status == "error"
        assert event.error_code == "LLM_CALL_FAILED"
    finally:
        await manager.stop()


class _ExplodingStream(httpx.AsyncByteStream):
    """产出若干字节块后抛传输异常的假流（流中段失败注入）。"""

    def __init__(self, chunks: list[bytes], error: httpx.HTTPError) -> None:
        """初始化假流。

        Args:
            chunks: 中断前依次产出的字节块。
            error: 字节块耗尽后抛出的 httpx 传输异常。
        """
        self._chunks = list(chunks)
        self._error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """逐块产出后抛注入的传输异常。"""
        for chunk in self._chunks:
            yield chunk
        raise self._error


async def test_stream_mid_iteration_transport_error_full_chain() -> None:
    """流中段传输失败全链：映射 BusinessException + 熔断计失败 + error 用量行。

    修复回归面：中段 ReadError 此前以原始 httpx 异常逃逸——网关
    ``except BusinessException`` 接不住（熔断不计失败、用量 error 行
    丢失、CLI REPL 被击穿）。映射收口后三面全恢复。
    """
    sse_text = 'data: {"choices":[{"index":0,"delta":{"content":"早"},"finish_reason":null}]}\n\n'
    sse_line = sse_text.encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_ExplodingStream([sse_line], httpx.ReadError("connection dropped")),
        )

    manager = await _make_pool_manager(handler)
    try:
        recorder = RecordingUsageRecorder()
        entry = _entry("main-chat", roles=("chat",))
        gateway = _gateway(manager, resolver=ModelResolver([entry]), usage_recorder=recorder)
        received: list[str] = []
        with pytest.raises(BusinessException) as exc_info:
            async for chunk in gateway.stream_by_role("chat", _messages()):
                received.append(chunk.content)
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert received == ["早"]

        breaker = manager.get_llm_entry_pool().get_entry_breaker(entry)
        assert breaker.consecutive_failures == 1

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.status == "error"
        assert event.error_code == "LLM_CALL_FAILED"
        assert event.model_entry == "main-chat"
    finally:
        await manager.stop()


async def test_stream_early_close_does_not_emit_usage() -> None:
    """消费方提前终止（GeneratorExit）：用量不落账（无终态结果）。"""
    manager = await _make_pool_manager(lambda request: _sse_response())
    try:
        recorder = RecordingUsageRecorder()
        gateway = _gateway(
            manager,
            resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
            usage_recorder=recorder,
        )
        stream = gateway.stream_by_role("chat", _messages())
        first = await stream.__anext__()
        assert first.content == "早"
        await stream.aclose()
        assert recorder.events == []
    finally:
        await manager.stop()


async def test_stream_unresolved_role_raises_on_first_pull() -> None:
    """流式角色未绑定：MODEL_NOT_FOUND（异步生成器首迭代步语义）。"""
    manager = await _make_pool_manager(lambda request: _ok_response())
    try:
        gateway = _gateway(manager, resolver=ModelResolver([]))
        with pytest.raises(BusinessException) as exc_info:
            async for _chunk in gateway.stream_by_role("chat", _messages()):
                pass
        assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# 默认接线全链：gateway → wire_default_usage_recorder → usage 服务 → SQLite
# ---------------------------------------------------------------------------


async def test_default_usage_recorder_full_chain(shared_db_engine: AsyncEngine) -> None:
    """默认接线：chat 成功 → LlmUsageEvent → ff_usage_records 落库可查。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None  # 外部安装——生命周期归共享 fixture
    try:
        manager = await _make_pool_manager(lambda request: _ok_response())
        try:
            gateway = _gateway(
                manager,
                resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
                usage_recorder=wire_default_usage_recorder(),
            )
            async with bind_context(persona_id="p-chain", request_id="req-chain"):
                response = await gateway.chat_by_role("chat", _messages())
            assert response.content == "回复内容"
        finally:
            await manager.stop()

        page = await UsageService().query(UsageQueryDTO(persona_id="p-chain", limit=10))
        assert page.total == -1  # 游标模式不计数
        assert len(page.items) == 1
        record = page.items[0]
        assert record.purpose == UsagePurpose.CHAT
        assert record.model_entry == "main-chat"
        assert record.input_tokens == 10
        assert record.total_tokens == 30
        assert record.request_id == "req-chain"
        assert record.error_code is None
    finally:
        db_engine_holder.reset()


# ---------------------------------------------------------------------------
# 默认接线全链：wire_default_usage_counter → usage 服务 → SQLite（分页计数）
# ---------------------------------------------------------------------------

#: 测试日桶起点（UTC 零点——与网关预算日桶同口径）。
_DAY_START_MS: int = 1_731_000_000_000 // 86_400_000 * 86_400_000


@pytest.fixture
async def default_usage_counter(
    shared_db_engine: AsyncEngine,
) -> AsyncIterator[UsageCounter]:
    """默认预算计数面（engine holder 外部安装到共享引擎；用后复位槽位）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        绑定共享引擎的 :func:`wire_default_usage_counter` 计数器。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None  # 外部安装——生命周期归共享 fixture
    try:
        yield wire_default_usage_counter()
    finally:
        db_engine_holder.reset()


async def _seed_usage_rows(
    session: AsyncSession, count: int, *, persona_id: str, ts_ms: int
) -> None:
    """批量灌入当日 usage 行（单事务——分页计数输入面）。

    Args:
        session: 绑定共享引擎的测试会话。
        count: 行数。
        persona_id: 归属人格 ID。
        ts_ms: 行时间戳（epoch 毫秒）。
    """
    session.add_all(
        [
            UsageRecordRow(
                persona_id=persona_id,
                ts_ms=ts_ms,
                model_entry="main-chat",
                provider="openai_compatible",
                purpose=UsagePurpose.CHAT.value,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=0,
                status=UsageStatus.SUCCESS.value,
                request_id=None,
                error_code=None,
            )
            for _ in range(count)
        ]
    )
    await session.commit()


async def test_default_usage_counter_spans_pages_and_terminates(
    db_session: AsyncSession, default_usage_counter: UsageCounter
) -> None:
    """201 行：首页 200 + 次页 1，累计 201 且尾页正常终止。"""
    await _seed_usage_rows(db_session, 201, persona_id="p-cap", ts_ms=_DAY_START_MS + 1)
    assert await default_usage_counter("p-cap", _DAY_START_MS) == 201


async def test_default_usage_counter_exact_page_boundary(
    db_session: AsyncSession, default_usage_counter: UsageCounter
) -> None:
    """恰好 200 行：首页满且无 next_cursor，计数 = 200。"""
    await _seed_usage_rows(db_session, 200, persona_id="p-cap", ts_ms=_DAY_START_MS + 1)
    assert await default_usage_counter("p-cap", _DAY_START_MS) == 200


async def test_default_usage_counter_empty_day_is_zero(
    default_usage_counter: UsageCounter,
) -> None:
    """空库（当日无行）：计数 = 0。"""
    assert await default_usage_counter("p-cap", _DAY_START_MS) == 0


async def test_default_usage_counter_stops_at_page_cap(
    db_session: AsyncSession, default_usage_counter: UsageCounter
) -> None:
    """超出单日可计数上限的行：页帽截断在单源常量（不再请求下一页）。"""
    await _seed_usage_rows(
        db_session, DAILY_BUDGET_MAX + 200, persona_id="p-cap", ts_ms=_DAY_START_MS + 1
    )
    assert await default_usage_counter("p-cap", _DAY_START_MS) == DAILY_BUDGET_MAX


# ---------------------------------------------------------------------------
# 日预算门禁 / 慢调用观测
# ---------------------------------------------------------------------------


class _RecordingCounter:
    """假预算计数面（记录调用并返回脚本化行数）。"""

    def __init__(self, counts: dict[str | None, int], *, forbidden: bool = False) -> None:
        self.counts = counts
        self.calls: list[tuple[str | None, int]] = []
        self.forbidden = forbidden

    async def __call__(self, persona_id: str | None, day_start_ms: int) -> int:
        if self.forbidden:
            raise AssertionError("0 = 不限定，不应发生计数查询")
        self.calls.append((persona_id, day_start_ms))
        return self.counts.get(persona_id, 0)


class _StepClock:
    """按调用序返回预置时刻的时钟（慢调用阈值判定）。"""

    def __init__(self, steps: list[int]) -> None:
        self.steps = steps
        self.calls = 0

    def __call__(self) -> int:
        index = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        return self.steps[index]


async def _budget_gateway(
    *,
    llm: LlmEndpointConfig,
    counter: Any,
    clock: Any = None,
) -> tuple[LlmGateway, PoolManager, list[httpx.Request]]:
    """构造带预算计数面 / 可注入时钟的网关（MockTransport 零出站）。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ok_response()

    manager = await _make_pool_manager(handler)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    gateway = LlmGateway(
        resolver=ModelResolver([_entry("main-chat", roles=("chat",))]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2), llm=llm),
        usage_counter=counter,
        retry_attempts=1,
        retry_wait_seconds=0.0,
        **kwargs,
    )
    return gateway, manager, captured


async def test_persona_budget_exceeded_rejected_before_call() -> None:
    """persona 档超限：调用前拒绝（RATE_LIMITED + details），零出站。"""
    counter = _RecordingCounter({"7": 2})
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_persona=2), counter=counter
    )
    try:
        with pytest.raises(BusinessException) as excinfo:
            await gateway.chat_by_role("chat", _messages(), persona_id="7")
    finally:
        await manager.stop()
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "persona", "used": 2, "limit": 2, "persona_id": "7"}
    assert captured == []
    assert counter.calls[0][0] == "7"
    assert counter.calls[0][1] % 86_400_000 == 0


async def test_global_budget_exceeded_rejected_without_persona_context() -> None:
    """无人格上下文：persona 档跳过，global 档超限即拒。"""
    counter = _RecordingCounter({None: 3})
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_global=3), counter=counter
    )
    try:
        with pytest.raises(BusinessException) as excinfo:
            await gateway.chat_by_role("chat", _messages())
    finally:
        await manager.stop()
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.details == {"scope": "global", "used": 3, "limit": 3}
    assert captured == []
    assert [call[0] for call in counter.calls] == [None]


async def test_budget_below_limit_allows_and_equal_limit_rejects() -> None:
    """used = limit-1 放行（调用后恰好等于上限）；used = limit 拒绝。"""
    allowed = _RecordingCounter({"7": 1})
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_persona=2), counter=allowed
    )
    try:
        response = await gateway.chat_by_role("chat", _messages(), persona_id="7")
    finally:
        await manager.stop()
    assert response.content == "回复内容"
    assert len(captured) == 1

    rejected = _RecordingCounter({"7": 2})
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_persona=2), counter=rejected
    )
    try:
        with pytest.raises(BusinessException):
            await gateway.chat_by_role("chat", _messages(), persona_id="7")
    finally:
        await manager.stop()
    assert captured == []


async def test_zero_budgets_mean_unlimited_without_counting() -> None:
    """两档 0 = 不限：不起计数查询，调用照常。"""
    counter = _RecordingCounter({}, forbidden=True)
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_global=0, daily_budget_persona=0),
        counter=counter,
    )
    try:
        response = await gateway.chat_by_role("chat", _messages(), persona_id="7")
    finally:
        await manager.stop()
    assert response.content == "回复内容"
    assert len(captured) == 1
    assert counter.calls == []


async def test_stream_budget_rejected_on_first_iteration() -> None:
    """流式路径：首个迭代步前即拒绝（不触达端点）。"""
    counter = _RecordingCounter({"7": 2})
    gateway, manager, captured = await _budget_gateway(
        llm=LlmEndpointConfig(daily_budget_persona=2), counter=counter
    )
    stream = gateway.stream_by_role("chat", _messages(), persona_id="7")
    try:
        with pytest.raises(BusinessException) as excinfo:
            await stream.__anext__()
    finally:
        await manager.stop()
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert captured == []


async def test_slow_call_warn_threshold_triggers_and_stays_silent_below() -> None:
    """慢调用 WARN：≥10s 触发（含 purpose / entry / 耗时），<10s 不触发。"""
    from loguru import logger

    base = 1_731_000_000_000
    slow_clock = _StepClock([base, base + 11_000, base + 11_000])
    gateway, manager, _captured = await _budget_gateway(
        llm=LlmEndpointConfig(), counter=_RecordingCounter({}), clock=slow_clock
    )
    captured_logs: list[str] = []
    handler_id = logger.add(lambda message: captured_logs.append(str(message)), level="WARNING")
    try:
        await gateway.chat_by_role("chat", _messages())
    finally:
        logger.remove(handler_id)
        await manager.stop()
    assert any(
        "LLM 慢调用" in message
        and "entry=main-chat" in message
        and "purpose=chat" in message
        and "latency_ms=11000" in message
        for message in captured_logs
    )

    fast_clock = _StepClock([base, base + 9_999, base + 9_999])
    gateway, manager, _captured = await _budget_gateway(
        llm=LlmEndpointConfig(), counter=_RecordingCounter({}), clock=fast_clock
    )
    captured_logs.clear()
    handler_id = logger.add(lambda message: captured_logs.append(str(message)), level="WARNING")
    try:
        await gateway.chat_by_role("chat", _messages())
    finally:
        logger.remove(handler_id)
        await manager.stop()
    assert not any("LLM 慢调用" in message for message in captured_logs)
