"""harness/llm embeddings 测试——client 传输 + 网关 embed 角色链路。

覆盖面：请求形态（/embeddings 路径 / model+input body / Bearer 头）/
响应归一（按 index 排序 / usage 映射）/ 错误契约（空输入 / data 数量
不符 / 序号越界 / 非法 JSON / 401）/ 网关 embed_by_role（角色解析 /
entry_gate / 熔断上报 / purpose=embed 用量发射成功与失败路径 /
persona_id 显式与上下文解析 / 角色未绑定 MODEL_NOT_FOUND）。全链
MockTransport——零真实出站。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.config.models import PoolsConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.context import bind_context
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.client import LlmHttpClient
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    EmbeddingsResponseModel,
    LlmUsageEvent,
    TokenUsageModel,
)
from packages.harness.pools.manager import PoolManager

#: MockTransport handler 类型。
Handler = Callable[[httpx.Request], httpx.Response]


def _embeddings_response(texts: list[str] | None = None) -> httpx.Response:
    """合法 embeddings 200 响应（按输入数量回送向量 + usage）。

    Args:
        texts: 输入文本列表（None = 固定两条——请求形态断言面用）。

    Returns:
        与输入同长的 embeddings 响应。
    """
    count = 2 if texts is None else len(texts)
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "test-embed",
            "data": [
                {"index": i, "object": "embedding", "embedding": [0.1 * (i + 1), 0.2]}
                for i in range(count)
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7},
        },
    )


def _wire() -> Any:
    """embeddings 测试 wire 配置（带密钥环境变量声明）。"""
    import os

    os.environ["TEST_EMBED_KEY"] = "secret-key"
    return parse_model_entry(
        {
            "name": "local-embed",
            "base_url": "http://embed.test/v1",
            "api_key_env": "TEST_EMBED_KEY",
            "model": "test-embed",
            "roles": ["embed"],
        }
    )


def _client(handler: Handler) -> LlmHttpClient:
    """构造直连 client（wire 构建同网关链路；MockTransport——零真实出站）。"""
    from packages.harness.config.llm_wire import build_llm_wire_config

    return LlmHttpClient(
        build_llm_wire_config(_wire()),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_attempts=1,
    )


async def test_client_embeddings_request_shape() -> None:
    """请求形态：POST {base}/embeddings + model/input body + Bearer 头。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _embeddings_response()

    client = _client(handler)
    response = await client.embeddings(["早安", "晚安"])

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "http://embed.test/v1/embeddings"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer secret-key"
    import json as jsonlib

    assert jsonlib.loads(request.read()) == {"model": "test-embed", "input": ["早安", "晚安"]}

    assert response.model == "test-embed"
    assert response.vectors == [[0.1, 0.2], [0.2, 0.2]]
    assert response.usage is not None
    assert response.usage == TokenUsageModel(input_tokens=7, output_tokens=0, total_tokens=7)
    assert response.latency_ms >= 0


async def test_client_embeddings_reorders_by_index() -> None:
    """响应归一：data 乱序时按 index 归位（与输入同序）。"""
    shuffled = httpx.Response(
        200,
        json={
            "model": "test-embed",
            "data": [
                {"index": 1, "embedding": [0.9]},
                {"index": 0, "embedding": [0.1]},
            ],
            "usage": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return shuffled

    response = await _client(handler).embeddings(["a", "b"])
    assert response.vectors == [[0.1], [0.9]]
    assert response.usage is None


async def test_client_embeddings_empty_input_rejected() -> None:
    """空输入：VALIDATION_ERROR（编程错误面——不发请求）。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _embeddings_response()

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embeddings([])
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert calls == []


async def test_client_embeddings_count_mismatch_rejected() -> None:
    """data 数量与输入不符：LLM_CALL_FAILED。"""
    mismatch = httpx.Response(
        200,
        json={
            "model": "test-embed",
            "data": [{"index": 0, "embedding": [0.1]}],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return mismatch

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embeddings(["a", "b"])
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_client_embeddings_index_out_of_range_rejected() -> None:
    """序号越界：LLM_CALL_FAILED。"""
    bad_index = httpx.Response(
        200,
        json={
            "model": "test-embed",
            "data": [
                {"index": 0, "embedding": [0.1]},
                {"index": 5, "embedding": [0.2]},
            ],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return bad_index

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embeddings(["a", "b"])
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_client_embeddings_invalid_json_rejected() -> None:
    """响应体非法 JSON：LLM_CALL_FAILED。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embeddings(["a"])
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_client_embeddings_auth_error_mapped() -> None:
    """401 鉴权失败：LLM_API_KEY_MISSING（不可重试——单次请求）。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embeddings(["a"])
    assert exc_info.value.code == ErrorCode.LLM_API_KEY_MISSING
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 网关 embed_by_role
# ---------------------------------------------------------------------------


class RecordingUsageRecorder:
    """假用量记录器（记录事件——purpose=embed 断言面）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.events: list[LlmUsageEvent] = []

    async def __call__(self, event: LlmUsageEvent) -> None:
        """记录事件。"""
        self.events.append(event)


async def _make_pool_manager(handler: Handler) -> PoolManager:
    """构造已启动的 PoolManager（共享 client 替换为 MockTransport）。"""
    settings = build_isolated_settings(pools=PoolsConfig(cb_fail_max=2))
    manager = PoolManager(settings)
    await manager.start()
    original = manager.http_client
    manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if original is not None:
        await original.aclose()
    return manager


def _embed_entry() -> Any:
    """embed 角色测试条目。"""
    return parse_model_entry(
        {
            "name": "local-embed",
            "base_url": "http://embed.test/v1",
            "model": "test-embed",
            "roles": ["embed"],
        }
    )


@pytest.fixture
async def embed_env() -> AsyncIterator[
    tuple[LlmGateway, PoolManager, RecordingUsageRecorder, list[httpx.Request]]
]:
    """embed 网关测试环境（按输入数量回送的成功端点 + embed 角色条目 + 假记录器）。

    Yields:
        ``(gateway, pool_manager, recorder, captured_requests)`` 四元组。
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        import json as jsonlib

        texts: list[str] = jsonlib.loads(request.read()).get("input", [])
        return _embeddings_response(texts)

    manager = await _make_pool_manager(handler)
    recorder = RecordingUsageRecorder()
    gateway = LlmGateway(
        resolver=ModelResolver([_embed_entry()]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        usage_recorder=recorder,
        retry_attempts=1,
        retry_wait_seconds=0.0,
    )
    try:
        yield gateway, manager, recorder, captured
    finally:
        await manager.stop()


async def test_gateway_embed_by_role_success(embed_env: tuple) -> None:
    """成功链：角色解析 + 传输 + purpose=embed 用量发射（token 计数来自响应）。"""
    gateway, _, recorder, captured = embed_env
    response = await gateway.embed_by_role(["早安", "晚安"], persona_id="p-1")

    assert isinstance(response, EmbeddingsResponseModel)
    assert len(response.vectors) == 2
    assert str(captured[0].url).endswith("/embeddings")

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.purpose == "embed"
    assert event.model_entry == "local-embed"
    assert event.persona_id == "p-1"
    assert event.status == "success"
    assert event.input_tokens == 7
    assert event.total_tokens == 7
    assert event.error_code is None


async def test_gateway_embed_persona_from_context(embed_env: tuple) -> None:
    """persona_id 解析序：显式参数优先 → 上下文变量 → None（系统调用）。"""
    gateway, _, recorder, _ = embed_env
    async with bind_context(persona_id="ctx-persona"):
        await gateway.embed_by_role(["a"])
        assert recorder.events[-1].persona_id == "ctx-persona"
        await gateway.embed_by_role(["a"], persona_id="explicit-persona")
        assert recorder.events[-1].persona_id == "explicit-persona"
    await gateway.embed_by_role(["a"])
    assert recorder.events[-1].persona_id is None


async def test_gateway_embed_role_unbound() -> None:
    """embed 角色未绑定条目：MODEL_NOT_FOUND（角色解析在传输前——与
    chat_by_role 同语义：无条目可归属，不落用量行）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return _embeddings_response()

    manager = await _make_pool_manager(handler)
    recorder = RecordingUsageRecorder()
    gateway = LlmGateway(
        resolver=ModelResolver([]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        usage_recorder=recorder,
        retry_attempts=1,
        retry_wait_seconds=0.0,
    )
    try:
        with pytest.raises(BusinessException) as exc_info:
            await gateway.embed_by_role(["a"])
        assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
        assert recorder.events == []
    finally:
        await manager.stop()


async def test_gateway_embed_transport_error_breaker_and_usage() -> None:
    """传输失败链：熔断失败计数 + error 用量落账（tokens=0）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    manager = await _make_pool_manager(handler)
    recorder = RecordingUsageRecorder()
    gateway = LlmGateway(
        resolver=ModelResolver([_embed_entry()]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        usage_recorder=recorder,
        retry_attempts=1,
        retry_wait_seconds=0.0,
    )
    try:
        with pytest.raises(BusinessException) as exc_info:
            await gateway.embed_by_role(["a"], persona_id="p-9")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

        entry_pool = manager.get_llm_entry_pool()
        breaker = entry_pool.get_entry_breaker(_embed_entry())
        assert breaker.consecutive_failures == 1

        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.purpose == "embed"
        assert event.status == "error"
        assert event.persona_id == "p-9"
        assert event.total_tokens == 0
        assert event.error_code == ErrorCode.LLM_CALL_FAILED
    finally:
        await manager.stop()
