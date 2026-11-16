"""server 入口测试——app 工厂 / 鉴权矩阵 / 端点面（手写 ASGI 调用，零 httpx）。

覆盖矩阵：/health 无鉴权存活面；/invoke 鉴权矩阵（头缺失 / 非 Bearer /
错误 token / 正确 token——统一 401 detail 无 oracle）；stub 运行时的
invoke 成功 / 未知工具终态；请求形态非法 422；默认运行时 http_fetch
全链（monkeypatch 抓取 + env 允许清单——hermetic）；create_app 的
token fail-fast；恒时比较真值表；绑定地址解析；main() 的 uvicorn 接线。

hermetic 红线：零真实网络、零 httpx 依赖——ASGI 层经 scope/receive/send
直驱（本树锁定决策：server 测试不引入 httpx TestClient）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import server as server_module
from server import (
    constant_time_token_matches,
    create_app,
    main,
    resolve_bind_address,
)
from tool_sandbox.http_fetch import HTTP_FETCH_TOOL_NAME
from tool_sandbox.runtime import SandboxToolRuntime, SandboxToolSpec
from tool_sandbox.wire_models import InvokeRequestModel, InvokeResponseModel

#: conftest 硬覆盖的测试专用 token。
_TEST_TOKEN = "test-only-sandbox-token"

#: 正确鉴权头（Bearer 形态）。
_AUTH_HEADERS = {"authorization": f"Bearer {_TEST_TOKEN}"}


class _AsgiResponse:
    """ASGI 响应收集器（status / headers / body）。"""

    def __init__(self) -> None:
        """初始化（空收集态）。"""
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""

    def header(self, name: str) -> str | None:
        """按名取首响应头（小写归一比较）。

        Args:
            name: 响应头名（大小写不敏感）。

        Returns:
            首个命中值；未命中为 None。
        """
        for key, value in self.headers:
            if key.decode("latin-1").lower() == name:
                return value.decode("latin-1")
        return None

    def json(self) -> Any:
        """解析 JSON 响应体。

        Returns:
            反序列化载荷。
        """
        return json.loads(self.body)


async def _asgi_request(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
) -> _AsgiResponse:
    """手写 ASGI 调用（直驱 app(scope, receive, send)——零网络零依赖）。

    Args:
        app: ASGI 应用（FastAPI 实例）。
        method: HTTP 方法。
        path: 请求路径。
        headers: 请求头（小写键形态——ASGI headers 规范）。
        json_body: JSON 请求体（None = 空体；自动携带 application/json）。

    Returns:
        收集到的响应（status / headers / body）。
    """
    merged_headers = dict(headers or {})
    body = b""
    if json_body is not None:
        merged_headers.setdefault("content-type", "application/json")
        body = json.dumps(json_body).encode("utf-8")
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in merged_headers.items()
        ],
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    result = _AsgiResponse()

    async def receive() -> dict[str, Any]:
        """单消息请求体（整体一次性送达）。"""
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        """收集响应消息（start 定状态 / body 累积）。"""
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            result.body += message.get("body", b"")

    await app(scope, receive, send)
    return result


def _stub_runtime() -> SandboxToolRuntime:
    """构造 stub 工具运行时（单工具 echo——鉴权 / 端点测试隔离工具语义）。"""
    runtime = SandboxToolRuntime()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """回显参数。"""
        return {"echo": args.get("value")}

    runtime.register(SandboxToolSpec(name="echo", description="回显"), _execute)
    return runtime


class TestHealth:
    async def test_health_no_auth(self) -> None:
        """/health 无鉴权：200 + status ok（存活面）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(app, "GET", "/health")
        assert response.status == 200
        assert response.json() == {"status": "ok"}


class TestInvokeAuth:
    async def test_missing_authorization_header_rejected(self) -> None:
        """鉴权：头缺失 → 401（统一 detail——无 oracle）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app, "POST", "/invoke", json_body={"tool": "echo", "arguments": {}}
        )
        assert response.status == 401
        assert response.json()["detail"] == "鉴权失败"

    async def test_non_bearer_scheme_rejected(self) -> None:
        """鉴权：非 Bearer 形态 → 401。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers={"authorization": "Basic dXNlcjpwYXNz"},
            json_body={"tool": "echo", "arguments": {}},
        )
        assert response.status == 401

    async def test_wrong_token_rejected(self) -> None:
        """鉴权：错误 token → 401（统一 detail）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers={"authorization": "Bearer wrong-token"},
            json_body={"tool": "echo", "arguments": {}},
        )
        assert response.status == 401
        assert response.json()["detail"] == "鉴权失败"

    async def test_bearer_scheme_case_insensitive(self) -> None:
        """鉴权：scheme 大小写不敏感（HTTP 语义）——bearer 小写通过。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers={"authorization": f"bearer {_TEST_TOKEN}"},
            json_body={"tool": "echo", "arguments": {"value": 1}},
        )
        assert response.status == 200

    async def test_correct_token_passes(self) -> None:
        """鉴权：正确 token → 200 + 工具执行结果（成功终态形态）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers=_AUTH_HEADERS,
            json_body={"tool": "echo", "arguments": {"value": 7}},
        )
        assert response.status == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["result"] == {"echo": 7}
        assert payload["error"] is None
        assert payload["latency_ms"] >= 0


class TestInvokeSemantics:
    async def test_unknown_tool_terminal(self) -> None:
        """未知工具：恒 200 + ok=False 终态（载荷内表达失败）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers=_AUTH_HEADERS,
            json_body={"tool": "nope", "arguments": {}},
        )
        assert response.status == 200
        payload = response.json()
        assert payload["ok"] is False
        assert "未知工具: nope" in (payload["error"] or "")

    async def test_malformed_body_rejected_422(self) -> None:
        """请求形态非法（未知键）：FastAPI 422（wire 契约严格面）。"""
        app = create_app(runtime=_stub_runtime())
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers=_AUTH_HEADERS,
            json_body={"unexpected": 1},
        )
        assert response.status == 422

    async def test_invalid_json_body_rejected_422(self) -> None:
        """非法 JSON 体：FastAPI 422（调用方 bug 面）。"""
        app = create_app(runtime=_stub_runtime())
        merged = dict(_AUTH_HEADERS)
        merged["content-type"] = "application/json"
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/invoke",
            "raw_path": b"/invoke",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", f"Bearer {_TEST_TOKEN}".encode("latin-1")),
                (b"content-type", b"application/json"),
            ],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        }
        result = _AsgiResponse()

        async def receive() -> dict[str, Any]:
            """非 JSON 请求体。"""
            return {"type": "http.request", "body": b"not-json{", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            """收集响应。"""
            if message["type"] == "http.response.start":
                result.status = message["status"]
            elif message["type"] == "http.response.body":
                result.body += message.get("body", b"")

        await app(scope, receive, send)
        assert result.status == 422

    async def test_default_runtime_http_fetch_full_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认运行时全链：http_fetch 经 monkeypatch 抓取 + env 允许清单。"""
        monkeypatch.setenv("SANDBOX_HTTP_ALLOWLIST", "example.com")
        captured: dict[str, Any] = {}

        def _fake_fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
            """记录调用的 fake 抓取（hermetic——零真实网络）。"""
            captured["url"] = url
            captured["timeout"] = timeout_seconds
            captured["allowlist"] = allowlist
            return {
                "url": url,
                "status_code": 200,
                "content_type": "text/plain",
                "body": "ok",
                "truncated": False,
            }

        monkeypatch.setattr(
            "tool_sandbox.http_fetch.default_fetch", _fake_fetch
        )
        app = create_app()
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers=_AUTH_HEADERS,
            json_body={
                "tool": HTTP_FETCH_TOOL_NAME,
                "arguments": {"url": "https://example.com/page"},
            },
        )
        assert response.status == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["result"]["body"] == "ok"
        # 默认值物化经 wire：timeout 默认 10 进入抓取面
        assert captured == {
            "url": "https://example.com/page",
            "timeout": 10,
            "allowlist": ["example.com"],
        }


class TestAppFactory:
    async def test_token_missing_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 缺失：app 工厂 fail fast（RuntimeError + 中文指引）。"""
        monkeypatch.delenv("SANDBOX_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="SANDBOX_TOKEN"):
            create_app(runtime=_stub_runtime())

    async def test_token_whitespace_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 纯空白：等同缺失（strip 归一后判空）。"""
        monkeypatch.setenv("SANDBOX_TOKEN", "   ")
        with pytest.raises(RuntimeError, match="SANDBOX_TOKEN"):
            create_app(runtime=_stub_runtime())


class TestHelpers:
    async def test_constant_time_token_matches_truth_table(self) -> None:
        """恒时比较真值表：一致 / 不一致 / 长度不等 / 非 ASCII。"""
        assert constant_time_token_matches("abc", "abc") is True
        assert constant_time_token_matches("abc", "abd") is False
        assert constant_time_token_matches("abc", "abcd") is False
        assert constant_time_token_matches("密钥", "密钥") is True
        assert constant_time_token_matches("密钥", "其他") is False

    async def test_resolve_bind_address_defaults(self) -> None:
        """绑定地址缺省：容器形态 0.0.0.0:8765。"""
        host, port = resolve_bind_address()
        assert host == "0.0.0.0"
        assert port == 8765

    async def test_resolve_bind_address_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """绑定地址 env 覆写：SANDBOX_HOST / SANDBOX_PORT。"""
        monkeypatch.setenv("SANDBOX_HOST", "127.0.0.1")
        monkeypatch.setenv("SANDBOX_PORT", "9001")
        assert resolve_bind_address() == ("127.0.0.1", 9001)

    async def test_resolve_bind_address_invalid_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非法端口：RuntimeError（配置错误面）。"""
        monkeypatch.setenv("SANDBOX_PORT", "not-a-port")
        with pytest.raises(RuntimeError, match="SANDBOX_PORT"):
            resolve_bind_address()

    async def test_main_wires_resolved_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main()：uvicorn.run 接收解析后的 host/port + app 工厂产物。"""
        monkeypatch.setenv("SANDBOX_HOST", "127.0.0.1")
        monkeypatch.setenv("SANDBOX_PORT", "9002")
        captured: dict[str, Any] = {}

        def _fake_uvicorn_run(app: Any, *, host: str, port: int) -> None:
            """捕获 uvicorn 启动参数。"""
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port

        monkeypatch.setattr(server_module.uvicorn, "run", _fake_uvicorn_run)
        main()
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9002
        assert captured["app"] is not None


class TestWireRoundTrip:
    async def test_invoke_request_model_round_trip(self) -> None:
        """wire 模型经 ASGI 反序列化等价（请求模型 ↔ JSON 形态）。"""
        request = InvokeRequestModel(tool="echo", arguments={"value": 3})
        assert InvokeRequestModel.model_validate(request.model_dump()) == request
        response = InvokeResponseModel(ok=True, result={"a": 1}, latency_ms=5)
        assert InvokeResponseModel.model_validate(response.model_dump()) == response
