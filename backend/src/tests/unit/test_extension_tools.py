"""外部 HTTP 扩展工具执行器测试——请求构造 / 占位 / 安全帽 / 错误语义。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.models import ExtensionTool
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import ChatResponseModel, ToolCallModel
from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.tools.extension_tools import build_extension_tool
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolExecutor, ToolSpecModel


def _extension(
    *,
    name: str = "calendar_query",
    url: str = "http://service.local/query",
    method: str = "POST",
    headers: dict[str, str] | None = None,
    result_path: str = "",
    timeout_ms: int = 5000,
    response_max_bytes: int = 65536,
    parameters: dict[str, Any] | None = None,
) -> ExtensionTool:
    """构造测试用扩展工具声明。"""
    params = parameters or {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "note": {"type": "string"},
            "item": {"type": "string"},
            "verbose": {"type": "string"},
        },
        "required": ["date"],
        "additionalProperties": False,
    }
    return ExtensionTool.model_validate(
        {
            "name": name,
            "description": "测试扩展工具",
            "parameters": params,
            "kind": "http",
            "http": {
                "url": url,
                "method": method,
                "headers": headers or {},
                "timeout_ms": timeout_ms,
                "response_max_bytes": response_max_bytes,
                "result_path": result_path,
            },
        }
    )


def _register(
    tool: ExtensionTool, factory: Any, *, clock: Any = None
) -> tuple[ToolRegistry, ToolSpecModel, ToolExecutor]:
    """构建并注册扩展工具（返回注册表与规格执行器）。"""
    assert tool.http is not None
    spec, executor = build_extension_tool(
        tool, tool.http, http_client_factory=factory, clock=clock
    )
    registry = ToolRegistry()
    registry.register(spec, executor)
    return registry, spec, executor


async def test_post_success_extracts_result_path_and_wires_timeout() -> None:
    """POST：JSON body=参数、result_path 提取、超时注入与时间戳。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录请求并返回固定 JSON。"""
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"events": [{"title": "站会"}]})

    tool = _extension(result_path="events", timeout_ms=250)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client, clock=lambda: 1234)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is True
    assert result.result["text"] == '[{"title":"站会"}]'
    assert result.result["status_code"] == 200
    assert result.result["fetched_at_ms"] == 1234
    assert seen["method"] == "POST"
    assert seen["body"] == {"date": "2026-01-01"}
    assert seen["timeout"]["read"] == 0.25


async def test_get_encodes_url_placeholder_and_sends_query() -> None:
    """GET：URL 占位值 URL 编码；未占位的参数进入查询串。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 URL 并返回固定 JSON。"""
        seen["method"] = request.method
        seen["raw_path"] = request.url.raw_path.split(b"?")[0]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [1, 2]})

    tool = _extension(
        url="http://service.local/items/{item}/detail",
        method="GET",
        result_path="items",
        parameters={
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "verbose": {"type": "string"},
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"item": "a/b", "verbose": "1"})
    assert result.ok is True
    assert seen["method"] == "GET"
    assert seen["raw_path"] == b"/items/a%2Fb/detail"
    assert seen["params"] == {"verbose": "1"}


async def test_env_placeholder_resolves_header_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """{env:VAR} 构造期解析：真实 env 值进入请求头。"""
    monkeypatch.setenv("EXT_TEST_TOKEN", "env-secret-value")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录 Authorization 头。"""
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={})

    tool = _extension(headers={"Authorization": "{env:EXT_TEST_TOKEN}"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is True
    assert seen["authorization"] == "env-secret-value"


async def test_env_placeholder_missing_fails_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """{env:VAR} 缺失：构造期受控失败（装配层据此跳过）。"""
    monkeypatch.delenv("EXT_MISSING_TOKEN", raising=False)
    tool = _extension(headers={"Authorization": "{env:EXT_MISSING_TOKEN}"})
    assert tool.http is not None
    with pytest.raises(BusinessException) as exc_info:
        build_extension_tool(
            tool, tool.http, http_client_factory=lambda: httpx.AsyncClient()
        )
    assert "EXT_MISSING_TOKEN" in exc_info.value.message


async def test_header_argument_substitution_success() -> None:
    """header 中的参数占位：执行期替换为调用值。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """记录自定义头。"""
        seen["item"] = request.headers.get("x-item", "")
        return httpx.Response(200, json={})

    tool = _extension(headers={"X-Item": "{item}"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01", "item": "abc"})
    assert result.ok is True
    assert seen["item"] == "abc"


async def test_header_argument_newline_rejected() -> None:
    """header 参数值含换行：受控失败（禁止头注入）。"""
    tool = _extension(headers={"X-Note": "{note}"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(
            tool.name, {"date": "2026-01-01", "note": "line1\nline2"}
        )
    assert result.ok is False
    assert "含换行" in (result.error or "")


async def test_undeclared_placeholder_fails_build() -> None:
    """URL 引用未声明参数占位：构造期受控失败。"""
    tool = _extension(url="http://service.local/{unknown}/x")
    assert tool.http is not None
    with pytest.raises(BusinessException) as exc_info:
        build_extension_tool(
            tool, tool.http, http_client_factory=lambda: httpx.AsyncClient()
        )
    assert "未声明" in exc_info.value.message


async def test_non_2xx_error_includes_status_and_body_not_headers() -> None:
    """非 2xx：错误含状态码与响应片段，不含响应头。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回 401 与一个敏感响应头。"""
        return httpx.Response(
            401,
            content=b'denied by calendar service',
            headers={"X-Secret": "super-secret-header-value"},
        )

    tool = _extension()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    error = result.error or ""
    assert "401" in error
    assert "denied by calendar service" in error
    assert "super-secret-header-value" not in error


async def test_non_2xx_body_snippet_truncated() -> None:
    """非 2xx 响应片段：按字符上限截断。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回 500 与超长正文。"""
        return httpx.Response(500, content=("A" * 400 + "ENDMARK").encode())

    tool = _extension()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "ENDMARK" not in (result.error or "")


async def test_response_size_cap() -> None:
    """响应体超过 response_max_bytes：受控失败。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回超出上限的正文。"""
        return httpx.Response(200, content=b"x" * 100)

    tool = _extension(response_max_bytes=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "字节上限" in (result.error or "")


class _CountingBodyStream(httpx.AsyncByteStream):
    """可计数异步响应体（记录传输层实际被消费的字节数）。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.bytes_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.bytes_read += len(chunk)
            yield chunk


async def test_response_size_cap_stops_stream_mid_read() -> None:
    """超过字节帽：读到上限即停止消费（未读完整 body）并受控失败。"""
    stream = _CountingBodyStream([b"x" * 10 for _ in range(10)])

    def handler(request: httpx.Request) -> httpx.Response:
        """返回计数流响应。"""
        return httpx.Response(200, stream=stream)

    tool = _extension(response_max_bytes=25)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "字节上限" in (result.error or "")
    assert stream.bytes_read < 100


async def test_header_unicode_encoding_failure_is_controlled() -> None:
    """header 值含非 ASCII：编码异常转为受控中文错误（不上抛）。"""
    tool = _extension(headers={"X-Note": "中文"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith(f"{ErrorCode.INTERNAL_ERROR}: ")
    assert "UnicodeEncodeError" in result.error


@pytest.mark.parametrize(
    ("property_schema", "value"),
    [
        pytest.param({"type": "string", "enum": ["a", "b"]}, "c", id="enum"),
        pytest.param(
            {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "2026/01/01",
            id="pattern",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            [1, "2"],
            id="items-strict",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}, "minItems": 2},
            [1],
            id="minItems",
        ),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}, "maxItems": 1},
            [1, 2],
            id="maxItems",
        ),
    ],
)
async def test_json_schema_constraints_reject_violations(
    property_schema: dict[str, Any], value: object
) -> None:
    """enum / pattern / items / minItems / maxItems 越界：参数校验失败。"""
    parameters = {
        "type": "object",
        "properties": {"date": property_schema},
        "required": ["date"],
    }
    tool = _extension(parameters=parameters)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": value})
    assert result.ok is False
    assert "参数校验失败" in (result.error or "")


@pytest.mark.parametrize(
    ("property_schema", "value"),
    [
        pytest.param({"type": "integer"}, "2", id="integer-string"),
        pytest.param({"type": "integer"}, True, id="integer-bool"),
        pytest.param({"type": "number"}, "1.5", id="number-string"),
        pytest.param({"type": "boolean"}, 1, id="boolean-int"),
        pytest.param({"type": "string"}, 1, id="string-int"),
    ],
)
async def test_json_schema_strict_types_reject_coercion(
    property_schema: dict[str, Any], value: object
) -> None:
    """JSON Schema 语义不接受字符串/数字强转：严格类型拒绝。"""
    parameters = {
        "type": "object",
        "properties": {"date": property_schema},
        "required": ["date"],
    }
    tool = _extension(parameters=parameters)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": value})
    assert result.ok is False
    assert "参数校验失败" in (result.error or "")


async def test_json_schema_constraints_accept_valid_values() -> None:
    """合法 enum / pattern / 数组长度与元素类型：通过校验并执行。"""
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "enum": ["2026-01-01", "2026-01-02"],
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 2,
            },
        },
        "required": ["date"],
    }
    tool = _extension(parameters=parameters)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(
            tool.name, {"date": "2026-01-01", "tags": ["a", "b"]}
        )
    assert result.ok is True


async def test_non_json_response_fails_controlled() -> None:
    """非 JSON 响应：受控失败。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回纯文本正文。"""
        return httpx.Response(200, content=b"not-json")

    tool = _extension()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "合法 JSON" in (result.error or "")


async def test_result_path_miss_fails_controlled() -> None:
    """result_path 未命中：受控失败。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回不含目标键的 JSON。"""
        return httpx.Response(200, json={"other": 1})

    tool = _extension(result_path="events")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert result.result == {}
    assert result.error is not None
    assert result.error.startswith(f"{ErrorCode.INTERNAL_ERROR}: ")


async def test_redirect_not_followed() -> None:
    """3xx 不跟随：客户端默认跟随也会因请求级禁用而停在 302。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """首次返回 302，后续返回 200。"""
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "http://service.local/final"})
        return httpx.Response(200, json={"events": []})

    tool = _extension()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "302" in (result.error or "")
    assert len(calls) == 1


async def test_timeout_and_connect_errors_isolated() -> None:
    """超时 / 连接失败：受控失败且不上抛。"""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        """模拟读超时。"""
        raise httpx.ReadTimeout("slow", request=request)

    def connect_handler(request: httpx.Request) -> httpx.Response:
        """模拟连接失败。"""
        raise httpx.ConnectError("down", request=request)

    tool = _extension()
    for handler, marker in ((timeout_handler, "超时"), (connect_handler, "ConnectError")):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registry, _, _ = _register(tool, lambda: client)
            result = await registry.execute(tool.name, {"date": "2026-01-01"})
        assert result.ok is False
        assert marker in (result.error or "")


async def test_whole_json_returned_when_result_path_empty() -> None:
    """result_path 为空：整个 JSON 文档作为文本结果。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回固定 JSON。"""
        return httpx.Response(200, json={"a": 1})

    tool = _extension()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is True
    assert result.result["text"] == '{"a":1}'


class _ScriptedGateway:
    """脚本化网关（工具调用一次后返回最终文本）。"""

    def __init__(self, responses: list[ChatResponseModel]) -> None:
        """初始化响应队列。"""
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """记录请求并弹出下一响应。"""
        self.requests.append({"role": role, "messages": list(messages), "kwargs": kwargs})
        return self._responses.pop(0)


async def test_chat_tool_loop_end_to_end_with_extension_tool() -> None:
    """ChatService 工具循环：LLM tool_call → 扩展执行器 → 结果回灌。"""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录调用参数并返回事件列表。"""
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"events": [{"title": "站会"}]})

    tool = _extension(result_path="events")
    gateway = _ScriptedGateway(
        [
            ChatResponseModel(
                content="",
                tool_calls=[
                    ToolCallModel(
                        id="call-1",
                        name=tool.name,
                        arguments={"date": "2026-01-01"},
                    )
                ],
            ),
            ChatResponseModel(content="明天有站会。"),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry, spec, _ = _register(tool, lambda: client)
        service = ChatService(gateway=gateway, tools=[spec], tool_executor=registry)
        state = ChatSessionStateModel(
            persona_id=7,
            persona_name="test-persona",
            display_name="测试人格",
            card=CharaCardModel(name="test-persona"),
        )
        content = await service.reply(state, "查一下明天的日程")
    assert content == "明天有站会。"
    assert seen == [{"date": "2026-01-01"}]
    feedback = gateway.requests[1]["messages"][-1]
    assert "站会" in feedback.content
    assert feedback.tool_call_id == "call-1"


async def test_extension_tool_owner_only_hidden_from_peer() -> None:
    """owner-only 扩展工具：owner 回合可见，peer 回合被结构性剔除。"""
    tool = _extension()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, spec, _ = _register(tool, lambda: client)
        owner_gateway = _ScriptedGateway([ChatResponseModel(content="主人回复")])
        owner_service = ChatService(
            gateway=owner_gateway,
            tools=[spec],
            tool_executor=registry,
            owner_only_tools=frozenset({tool.name}),
        )
        state = ChatSessionStateModel(
            persona_id=7,
            persona_name="test-persona",
            display_name="测试人格",
            card=CharaCardModel(name="test-persona"),
        )
        await owner_service.reply(state, "查日程")
        peer_gateway = _ScriptedGateway([ChatResponseModel(content="群友回复")])
        peer_service = ChatService(
            gateway=peer_gateway,
            tools=[spec],
            tool_executor=registry,
            owner_only_tools=frozenset({tool.name}),
        )
        await peer_service.reply(
            state, "查日程", sender_provenance=MemoryProvenance.PEER
        )

    owner_tools = owner_gateway.requests[0]["kwargs"]["tools"]
    assert [definition.name for definition in owner_tools] == [tool.name]
    assert peer_gateway.requests[0]["kwargs"]["tools"] is None


async def test_extension_tool_peer_visible_when_not_owner_only() -> None:
    """expose_to_peer=true 的映射结果（不在 owner-only 集合）对 peer 可见。"""
    tool = _extension()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, spec, _ = _register(tool, lambda: client)
        gateway = _ScriptedGateway([ChatResponseModel(content="群友回复")])
        service = ChatService(
            gateway=gateway,
            tools=[spec],
            tool_executor=registry,
            owner_only_tools=frozenset(),
        )
        state = ChatSessionStateModel(
            persona_id=7,
            persona_name="test-persona",
            display_name="测试人格",
            card=CharaCardModel(name="test-persona"),
        )
        await service.reply(state, "查日程", sender_provenance=MemoryProvenance.PEER)

    peer_tools = gateway.requests[0]["kwargs"]["tools"]
    assert [definition.name for definition in peer_tools] == [tool.name]


async def test_invalid_url_runtime_failure_isolated() -> None:
    """运行期 URL 非法（端口非数字）：受控失败且不上抛。"""
    tool = _extension(url="http://service.local:bad/query")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        registry, _, _ = _register(tool, lambda: client)
        result = await registry.execute(tool.name, {"date": "2026-01-01"})
    assert result.ok is False
    assert "InvalidURL" in (result.error or "")
