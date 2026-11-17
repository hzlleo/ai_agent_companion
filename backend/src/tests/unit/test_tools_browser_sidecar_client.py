"""harness/tools 浏览器 sidecar 客户端测试——MockTransport 零真实网络。

覆盖矩阵：token 环境变量间接解析（缺失短路 / 变量指向）；鉴权头形状
（Bearer）；请求体 wire 形态；ok / error 映射（wire 成败终态 →
ToolResultModel——恒不抛）；传输异常 / 非成功状态 / wire 破坏三类失败
归一；健康探测成功 / 失败矩阵；build_browser_sidecar_client 配置面构造
（face 协议满足）。
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.harness.config.models import ToolsBrowserSidecarSettings
from packages.harness.tools.browser import (
    BrowserSidecarClient,
    build_browser_sidecar_client,
)
from packages.harness.tools.browser_models import InvokeResponseModel

#: conftest 硬覆盖的测试专用 token 变量（token_env 缺省指向形态）。
_TEST_TOKEN_ENV = "BROWSER_SIDECAR_TEST_TOKEN"

#: conftest 硬覆盖的测试专用 token 本值。
_TEST_TOKEN = "test-only-browser-sidecar-token"


def _client(
    handler: Any,
    *,
    token_env: str = _TEST_TOKEN_ENV,
    base_url: str = "http://127.0.0.1:8766",
    request_timeout_seconds: float = 30.0,
) -> BrowserSidecarClient:
    """构造测试客户端（MockTransport + 测试 token env）。

    Args:
        handler: MockTransport 处理器。
        token_env: token 环境变量名。
        base_url: sidecar 基础 URL。
        request_timeout_seconds: 请求超时。

    Returns:
        客户端实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BrowserSidecarClient(
        http,
        base_url=base_url,
        token_env=token_env,
        request_timeout_seconds=request_timeout_seconds,
    )


def _wire_response(payload: InvokeResponseModel) -> httpx.Response:
    """构造 200 携带 wire 契约载荷的响应。

    Args:
        payload: wire 响应模型。

    Returns:
        200 JSON 响应。
    """
    return httpx.Response(200, json=payload.model_dump())


class TestConstruction:
    async def test_token_env_indirection(self) -> None:
        """token 环境变量间接：token_env 指向的变量本值生效。"""
        client = _client(lambda request: _wire_response(InvokeResponseModel()))
        assert client.has_token is True
        assert client.base_url == "http://127.0.0.1:8766"

    async def test_token_missing_short_circuits(
        self, monkeypatch: Any
    ) -> None:
        """token 变量缺失：has_token=False + invoke 短路失败（零请求发出）。"""
        monkeypatch.delenv(_TEST_TOKEN_ENV, raising=False)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """记录调用的处理器（短路路径不应触达）。"""
            calls.append(request)
            return _wire_response(InvokeResponseModel())

        client = _client(handler)
        assert client.has_token is False
        result = await client.invoke("navigate", {"url": "https://example.com/x"})
        assert result.ok is False
        assert result.tool == "navigate"
        assert "token 未配置" in (result.error or "")
        assert calls == []

    async def test_token_whitespace_normalized(self, monkeypatch: Any) -> None:
        """token 纯空白：strip 归一后判空（等同缺失）。"""
        monkeypatch.setenv(_TEST_TOKEN_ENV, "   ")
        client = _client(lambda request: _wire_response(InvokeResponseModel()))
        assert client.has_token is False


class TestInvokeWireShape:
    async def test_auth_header_and_body_shape(self) -> None:
        """请求形状：Authorization Bearer 头 + JSON 请求体 wire 形态。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            """捕获请求头与请求体。"""
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            captured["json"] = request.read()
            return _wire_response(InvokeResponseModel(ok=True, result={"title": "ok"}))

        client = _client(handler)
        result = await client.invoke("navigate", {"url": "https://example.com/x"})
        assert result.ok is True
        assert captured["url"] == "http://127.0.0.1:8766/invoke"
        assert captured["authorization"] == f"Bearer {_TEST_TOKEN}"
        assert b'"tool":"navigate"' in captured["json"]

    async def test_base_url_trailing_slash_tolerated(self) -> None:
        """base_url 尾斜杠容错：拼接不产生双斜杠路径。"""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            """捕获请求 URL。"""
            captured["url"] = str(request.url)
            return _wire_response(InvokeResponseModel())

        client = _client(handler, base_url="http://127.0.0.1:8766/")
        await client.invoke("list_tabs", {})
        assert captured["url"] == "http://127.0.0.1:8766/invoke"


class TestInvokeMapping:
    async def test_wire_success_maps_to_tool_result(self) -> None:
        """wire ok=True → ToolResultModel ok=True + result 透传 + sidecar 时延。"""
        client = _client(
            lambda request: _wire_response(
                InvokeResponseModel(ok=True, result={"title": "Example"}, latency_ms=42)
            )
        )
        result = await client.invoke("navigate", {"url": "https://example.com/x"})
        assert result.ok is True
        assert result.tool == "navigate"
        assert result.result == {"title": "Example"}
        assert result.error is None
        assert result.latency_ms == 42

    async def test_wire_failure_maps_to_tool_result(self) -> None:
        """wire ok=False → ok=False + error 透传（result 恒空）。"""
        client = _client(
            lambda request: _wire_response(
                InvokeResponseModel(ok=False, error="目标域名不在允许清单: evil.example.org")
            )
        )
        result = await client.invoke("navigate", {"url": "https://evil.example.org/x"})
        assert result.ok is False
        assert "允许清单" in (result.error or "")
        assert result.result == {}

    async def test_wire_failure_without_error_normalized(self) -> None:
        """wire ok=False 且 error 缺省：归一兜底文案。"""
        client = _client(lambda request: _wire_response(InvokeResponseModel(ok=False)))
        result = await client.invoke("navigate", {})
        assert result.ok is False
        assert result.error == "sidecar 工具执行失败"

    async def test_non_200_status_mapped(self) -> None:
        """非成功状态（401 鉴权失败）：ok=False + 状态码入文案。"""
        client = _client(lambda request: httpx.Response(401, json={"detail": "鉴权失败"}))
        result = await client.invoke("navigate", {})
        assert result.ok is False
        assert "401" in (result.error or "")

    async def test_transport_error_mapped(self) -> None:
        """传输异常（连接拒绝）：ok=False sidecar 不可达（恒不抛）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """抛连接异常的处理器。"""
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        result = await client.invoke("navigate", {})
        assert result.ok is False
        assert "sidecar 不可达" in (result.error or "")

    async def test_malformed_wire_body_mapped(self) -> None:
        """wire 契约破坏（非法 JSON）：ok=False（恒不抛）。"""
        client = _client(lambda request: httpx.Response(200, content=b"not-json{"))
        result = await client.invoke("navigate", {})
        assert result.ok is False
        assert "wire 契约破坏" in (result.error or "")


class TestHealthProbe:
    async def test_probe_success(self) -> None:
        """探测成功：200 + status=ok → True。"""
        client = _client(lambda request: httpx.Response(200, json={"status": "ok"}))
        assert await client.probe_health() is True

    async def test_probe_uses_health_endpoint(self) -> None:
        """探测端点：GET /health（无鉴权头——存活面）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            """捕获请求方法 / 路径 / 头。"""
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"status": "ok"})

        client = _client(handler)
        assert await client.probe_health() is True
        assert captured["method"] == "GET"
        assert captured["url"] == "http://127.0.0.1:8766/health"
        assert captured["authorization"] is None

    async def test_probe_non_200(self) -> None:
        """探测非 200（503）：False。"""
        client = _client(lambda request: httpx.Response(503))
        assert await client.probe_health() is False

    async def test_probe_transport_error(self) -> None:
        """探测传输异常：False（恒不抛）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """抛连接异常的处理器。"""
            raise httpx.ConnectError("refused")

        client = _client(handler)
        assert await client.probe_health() is False

    async def test_probe_wrong_body(self) -> None:
        """探测载荷 status 非 ok：False。"""
        client = _client(lambda request: httpx.Response(200, json={"status": "bad"}))
        assert await client.probe_health() is False

    async def test_probe_malformed_body(self) -> None:
        """探测载荷非法 JSON：False。"""
        client = _client(lambda request: httpx.Response(200, content=b"garbage"))
        assert await client.probe_health() is False


class TestFactory:
    async def test_build_client_from_settings_face(self) -> None:
        """build_browser_sidecar_client：真实 ToolsBrowserSidecarSettings（face
        协议满足）+ 配置透传。"""
        settings = ToolsBrowserSidecarSettings(
            enabled=True,
            base_url="http://127.0.0.1:9999",
            token_env=_TEST_TOKEN_ENV,
            request_timeout_seconds=7.5,
        )
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "ok"})
            )
        )
        client = build_browser_sidecar_client(http, settings)
        assert client.base_url == "http://127.0.0.1:9999"
        assert client.has_token is True
        assert await client.probe_health() is True
