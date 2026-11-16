"""harness/llm 原生工具调用测试——请求面 / 解析面 / 流式拒绝 / 网关透传。

覆盖面：请求体构建（tools 包裹形态 + tool_choice 缺省 auto / 显式）；
assistant 工具调用轮与 tool 结果轮的 wire 序列化；tools+stream 组合
拒绝；MockTransport 端到端（请求形状携带 tools / tool_calls 解析 /
arguments 非法 JSON 与非对象拒绝）；网关角色透传（duty 角色 + tools →
请求面 + 用量 purpose=duty）。零真实网络（MockTransport + 假解析器）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.config.models import PoolsConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.client import LlmHttpClient
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    TOOL_CHOICE_AUTO,
    ChatMessageModel,
    ChatMessageRole,
    ToolCallModel,
    ToolDefinitionModel,
    ToolParametersSchema,
    build_chat_request_body,
)
from packages.harness.pools.manager import PoolManager

#: MockTransport handler 类型。
Handler = Callable[[httpx.Request], httpx.Response]


def _time_tool() -> ToolDefinitionModel:
    """构造测试工具定义（name / description / object schema）。"""
    return ToolDefinitionModel(
        name="get_current_time",
        description="获取当前时间",
        parameters=ToolParametersSchema.model_validate(
            {"type": "object", "properties": {}, "required": []}
        ),
    )


def _search_tool() -> ToolDefinitionModel:
    """构造第二把测试工具（双工具序列化断言用）。"""
    return ToolDefinitionModel(
        name="search_memory",
        description="检索记忆",
        parameters=ToolParametersSchema.model_validate(
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            }
        ),
    )


def _tool_call_response(
    call_id: str = "call-1",
    name: str = "get_current_time",
    arguments: str = "{}",
    content: str | None = None,
) -> httpx.Response:
    """构造携带 tool_calls 的合法 200 响应。"""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }
    return httpx.Response(
        200,
        json={
            "id": "resp-t",
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[..., LlmHttpClient]]:
    """client 工厂（MockTransport 共享 client；用后关闭）。"""
    created: list[httpx.AsyncClient] = []

    def _make(handler: Handler, **kwargs: Any) -> LlmHttpClient:
        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(shared)
        return LlmHttpClient(_wire(), shared, **kwargs)

    yield _make
    for shared in created:
        await shared.aclose()


def _wire() -> Any:
    """测试 wire 配置（免鉴权端点）。"""
    from packages.harness.config.llm_wire import LlmClientConfigModel

    return LlmClientConfigModel(
        provider="openai_compatible",
        base_url="http://llm.test/v1",
        api_key="",
        model="test-model",
    )


def _messages() -> list[ChatMessageModel]:
    """单条用户消息。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="现在几点")]


# ---------------------------------------------------------------------------
# 请求体构建（build_chat_request_body——纯函数面）
# ---------------------------------------------------------------------------


async def test_build_body_tools_wrapped_with_auto_choice() -> None:
    """tools 非空：OpenAI 包裹形态 + tool_choice 缺省 auto。"""
    body = build_chat_request_body(
        "test-model", _messages(), None, stream=False, tools=[_time_tool()]
    )
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": "获取当前时间",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]
    assert body["tool_choice"] == TOOL_CHOICE_AUTO == "auto"


async def test_build_body_tools_explicit_choice_and_multi() -> None:
    """tool_choice 显式透传 + 多工具序列化（additionalProperties 保留）。"""
    body = build_chat_request_body(
        "test-model",
        _messages(),
        None,
        stream=False,
        tools=[_time_tool(), _search_tool()],
        tool_choice="required",
    )
    assert body["tool_choice"] == "required"
    assert [t["function"]["name"] for t in body["tools"]] == [
        "get_current_time",
        "search_memory",
    ]
    assert body["tools"][1]["function"]["parameters"]["additionalProperties"] is False
    assert body["tools"][1]["function"]["parameters"]["required"] == ["query"]


async def test_build_body_without_tools_has_no_tools_keys() -> None:
    """tools 缺省 / 空列表：请求体不出现 tools / tool_choice 键。"""
    body_plain = build_chat_request_body("test-model", _messages(), None, stream=False)
    body_empty = build_chat_request_body("test-model", _messages(), None, stream=False, tools=[])
    for body in (body_plain, body_empty):
        assert "tools" not in body
        assert "tool_choice" not in body


async def test_build_body_rejects_tools_with_stream() -> None:
    """tools + stream 组合：VALIDATION_ERROR（流式恒 content-only）。"""
    with pytest.raises(BusinessException) as exc_info:
        build_chat_request_body("test-model", _messages(), None, stream=True, tools=[_time_tool()])
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "流式" in exc_info.value.message


async def test_build_body_serializes_assistant_tool_calls_turn() -> None:
    """assistant 工具调用轮：content 空 → null；arguments 紧凑 JSON 字符串。"""
    messages = [
        _messages()[0],
        ChatMessageModel(
            role=ChatMessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCallModel(
                    id="call-9",
                    name="search_memory",
                    arguments={"query": "主人 近况", "limit": 3},
                )
            ],
        ),
    ]
    body = build_chat_request_body("test-model", messages, None, stream=False)
    turn = body["messages"][1]
    assert turn["role"] == "assistant"
    assert turn["content"] is None
    assert turn["tool_calls"] == [
        {
            "id": "call-9",
            "type": "function",
            "function": {
                "name": "search_memory",
                "arguments": json.dumps(
                    {"query": "主人 近况", "limit": 3},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        }
    ]


async def test_build_body_serializes_tool_result_turn() -> None:
    """tool 结果轮：role=tool + tool_call_id + content。"""
    messages = [
        ChatMessageModel(
            role=ChatMessageRole.TOOL,
            content="【工具结果 · get_current_time】{}",
            tool_call_id="call-9",
        )
    ]
    body = build_chat_request_body("test-model", messages, None, stream=False)
    assert body["messages"] == [
        {"role": "tool", "tool_call_id": "call-9", "content": "【工具结果 · get_current_time】{}"}
    ]


# ---------------------------------------------------------------------------
# client 端到端（MockTransport）
# ---------------------------------------------------------------------------


async def test_client_chat_sends_tools_and_parses_tool_calls(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """端到端：请求体携带 tools + tool_choice；响应 tool_calls 解析为 dict。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _tool_call_response(arguments=json.dumps({"query": "近况"}))

    client = make_client(handler, retry_attempts=1)
    response = await client.chat(_messages(), tools=[_search_tool()], tool_choice="required")
    request_body = json.loads(captured[0].content)
    assert request_body["tool_choice"] == "required"
    assert request_body["tools"][0]["function"]["name"] == "search_memory"
    assert response.content == ""
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call-1"
    assert call.name == "get_current_time"
    assert call.arguments == {"query": "近况"}


async def test_client_chat_tool_calls_with_content_both_carried(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """content 与 tool_calls 并存：两者均归一（content 原样保留）。"""
    client = make_client(lambda _r: _tool_call_response(content="我先看看时间"))
    response = await client.chat(_messages(), tools=[_time_tool()])
    assert response.content == "我先看看时间"
    assert response.tool_calls[0].arguments == {}


async def test_client_chat_malformed_arguments_rejected(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """arguments 非法 JSON：LLM_CALL_FAILED（details 携带调用 ID）。"""
    client = make_client(lambda _r: _tool_call_response(arguments="{broken"))
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_messages(), tools=[_time_tool()])
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert exc_info.value.details is not None
    assert exc_info.value.details["tool_call_id"] == "call-1"


async def test_client_chat_non_object_arguments_rejected(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """arguments 合法 JSON 但非对象（数组）：LLM_CALL_FAILED。"""
    client = make_client(lambda _r: _tool_call_response(arguments="[1, 2]"))
    with pytest.raises(BusinessException) as exc_info:
        await client.chat(_messages(), tools=[_time_tool()])
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert exc_info.value.details is not None
    assert exc_info.value.details["reason"] == "LLM 工具调用 arguments 必须是 JSON 对象"


async def test_client_chat_plain_response_has_empty_tool_calls(
    make_client: Callable[..., LlmHttpClient],
) -> None:
    """纯文本响应：tool_calls 为空列表（既有行为回归锚点）。"""
    client = make_client(
        lambda _r: httpx.Response(
            200,
            json={
                "id": "r",
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "早安"}}],
            },
        )
    )
    response = await client.chat(_messages())
    assert response.content == "早安"
    assert response.tool_calls == []


# ---------------------------------------------------------------------------
# 网关透传（duty 角色 + tools → 请求面 + 用量 purpose）
# ---------------------------------------------------------------------------


async def _make_pool_manager(handler: Handler) -> PoolManager:
    """构造已启动的 PoolManager（MockTransport 替换共享 client）。"""
    manager = PoolManager(build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)))
    await manager.start()
    original = manager.http_client
    manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if original is not None:
        await original.aclose()
    return manager


async def test_gateway_chat_by_role_passes_tools_through() -> None:
    """网关透传：chat_by_role(tools=...) → 请求体携带 tools + 用量 purpose=duty。"""
    captured: list[httpx.Request] = []
    manager = await _make_pool_manager(lambda r: (captured.append(r), _tool_call_response())[1])
    entry = parse_model_entry(
        {
            "name": "duty-worker",
            "base_url": "http://llm.test/v1",
            "api_key_env": "",
            "model": "test-model",
            "roles": ["duty"],
        }
    )
    events: list[Any] = []

    async def recorder(event: Any) -> None:
        """假用量记录器。"""
        events.append(event)

    gateway = LlmGateway(
        resolver=ModelResolver([entry]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        usage_recorder=recorder,
        retry_attempts=1,
        retry_wait_seconds=0.0,
    )
    try:
        response = await gateway.chat_by_role("duty", _messages(), tools=[_time_tool()])
        assert response.tool_calls[0].name == "get_current_time"
        request_body = json.loads(captured[0].content)
        assert request_body["tools"][0]["function"]["name"] == "get_current_time"
        assert request_body["tool_choice"] == "auto"
        assert len(events) == 1
        assert events[0].purpose == "duty"
        assert events[0].status == "success"
    finally:
        await manager.stop()
