"""server 入口测试——app 工厂 / 鉴权矩阵 / 端点面（手写 ASGI 调用，零 httpx）。

覆盖矩阵：/health 无鉴权存活面；/invoke 鉴权矩阵（头缺失 / 非 Bearer /
错误 token / 正确 token——统一 401 detail 无 oracle）；stub 运行时的
invoke 成功 / 未知工具终态；请求形态非法 422；默认运行时 navigate 全链
（monkeypatch 默认构建 + fake 注入面——hermetic）；create_app 的 token
fail-fast；lifespan 的管理器起停接线；恒时比较真值表；绑定地址解析；
main() 的 uvicorn 接线。

hermetic 红线：零真实浏览器、零网络——ASGI 层经 scope/receive/send
直驱（本树锁定决策：server 测试不引入 httpx TestClient）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import server as server_module
from browser_sidecar.browser import BrowserManager
from browser_sidecar.runtime import BrowserSidecarRuntime, BrowserSidecarToolSpec
from browser_sidecar.wire_models import InvokeRequestModel, InvokeResponseModel
from server import (
    constant_time_token_matches,
    create_app,
    main,
    resolve_bind_address,
)

#: conftest 硬覆盖的测试专用 token。
_TEST_TOKEN = "test-only-browser-token"

#: 正确鉴权头（Bearer 形态）。
_AUTH_HEADERS = {"authorization": f"Bearer {_TEST_TOKEN}"}


class _AsgiResponse:
    """ASGI 响应收集器（status / headers / body）。"""

    def __init__(self) -> None:
        """初始化（空收集态）。"""
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""

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


def _stub_runtime() -> BrowserSidecarRuntime:
    """构造 stub 工具运行时（单工具 echo——鉴权 / 端点测试隔离工具语义）。"""
    runtime = BrowserSidecarRuntime()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """回显参数。"""
        return {"echo": args.get("value")}

    runtime.register(BrowserSidecarToolSpec(name="echo", description="回显"), _execute)
    return runtime


def _fake_manager(started: list[bool], stopped: list[bool]) -> BrowserManager:
    """构造记录起停的 fake 管理器（lifespan 接线断言面）。"""

    async def _factory(profile: str) -> Any:
        """fake context 工厂（永不触达真实浏览器）。"""
        raise AssertionError("fake manager 的 context 工厂不应被触达")

    manager = BrowserManager(
        context_factory=_factory,
        profile="default",
        clock=lambda: 0.0,
        sleep=_never_sleep,
    )
    original_start = manager.start
    original_stop = manager.stop

    async def _start() -> None:
        """记录启动。"""
        started.append(True)
        await original_start()

    async def _stop() -> None:
        """记录停机。"""
        stopped.append(True)
        await original_stop()

    manager.start = _start
    manager.stop = _stop
    return manager


async def _never_sleep(seconds: float) -> None:
    """永不返回的 fake sleep（收割任务挂起——stop 取消收尾）。"""
    await asyncio.Event().wait()


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

    async def test_default_runtime_navigate_full_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认运行时全链：navigate 经注入 fake face + 允许清单（hermetic）。"""

        class _FakePage:
            """最小 fake 页面（goto / title / url）。"""

            def __init__(self) -> None:
                """初始化。"""
                self.url = "https://example.com/page"
                self.goto_calls: list[tuple[str, int]] = []

            async def goto(self, url: str, *, timeout_ms: int) -> None:
                """记录导航。"""
                self.goto_calls.append((url, timeout_ms))
                self.url = url

            async def title(self) -> str:
                """返回标题。"""
                return "默认链标题"

            async def inner_text(self) -> str:
                """返回正文。"""
                return ""

            async def click(self, selector: str) -> None:
                """不应触达。"""

            async def fill(self, selector: str, value: str) -> None:
                """不应触达。"""

            async def content_size(self) -> tuple[int, int]:
                """不应触达。"""
                return (0, 0)

            async def viewport_size(self) -> tuple[int, int]:
                """不应触达。"""
                return (0, 0)

            async def screenshot(self, *, clip_width: int, clip_height: int) -> bytes:
                """不应触达。"""
                return b""

            async def close(self) -> None:
                """关闭。"""

        class _FakeContext:
            """最小 fake context。"""

            def __init__(self) -> None:
                """初始化。"""
                self._page = _FakePage()

            def pages(self) -> list[_FakePage]:
                """单页面列表。"""
                return [self._page]

            async def new_page(self) -> _FakePage:
                """复用既有页面。"""
                return self._page

            async def close(self) -> None:
                """关闭。"""

        context = _FakeContext()

        async def _factory(profile: str) -> _FakeContext:
            """fake context 工厂。"""
            return context

        async def _resolve(host: str) -> list[str]:
            """fake 解析器（公网 IP）。"""
            return ["93.184.216.34"]

        from browser_sidecar.browser import register_browser_tools

        runtime = BrowserSidecarRuntime()
        manager = BrowserManager(
            context_factory=_factory, profile="default", clock=lambda: 0.0
        )
        register_browser_tools(
            runtime,
            manager=manager,
            allowlist_provider=lambda: ["example.com"],
            resolver=_resolve,
            nav_timeout_ms=5000,
        )

        def _fake_build() -> tuple[BrowserSidecarRuntime, BrowserManager | None]:
            """替换默认构建（hermetic——零真实浏览器）。"""
            return runtime, None

        monkeypatch.setattr(server_module, "build_default_browser_sidecar", _fake_build)
        monkeypatch.setenv("BROWSER_SIDECAR_ALLOWLIST", "example.com")
        app = create_app()
        response = await _asgi_request(
            app,
            "POST",
            "/invoke",
            headers=_AUTH_HEADERS,
            json_body={
                "tool": "navigate",
                "arguments": {"url": "https://example.com/page"},
            },
        )
        assert response.status == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["result"]["title"] == "默认链标题"
        assert context._page.goto_calls == [("https://example.com/page", 5000)]


class TestAppFactory:
    async def test_token_missing_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 缺失：app 工厂 fail fast（RuntimeError + 中文指引）。"""
        monkeypatch.delenv("BROWSER_SIDECAR_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="BROWSER_SIDECAR_TOKEN"):
            create_app(runtime=_stub_runtime())

    async def test_token_whitespace_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 纯空白：等同缺失（strip 归一后判空）。"""
        monkeypatch.setenv("BROWSER_SIDECAR_TOKEN", "   ")
        with pytest.raises(RuntimeError, match="BROWSER_SIDECAR_TOKEN"):
            create_app(runtime=_stub_runtime())


class TestLifespan:
    async def test_default_runtime_lifespan_starts_and_stops_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lifespan 接线：默认运行时的管理器随 lifespan 起 / 停。"""
        started: list[bool] = []
        stopped: list[bool] = []
        manager = _fake_manager(started, stopped)

        def _fake_build() -> tuple[BrowserSidecarRuntime, BrowserManager | None]:
            """替换默认构建（注入 fake 管理器）。"""
            return _stub_runtime(), manager

        monkeypatch.setattr(server_module, "build_default_browser_sidecar", _fake_build)
        app = create_app()
        async with app.router.lifespan_context(app):
            assert started == [True]
            assert stopped == []
        assert stopped == [True]

    async def test_injected_runtime_lifespan_noop(self) -> None:
        """注入运行时：无管理器——lifespan no-op（不触达任何浏览器面）。"""
        app = create_app(runtime=_stub_runtime())
        async with app.router.lifespan_context(app):
            pass


class TestHelpers:
    async def test_constant_time_token_matches_truth_table(self) -> None:
        """恒时比较真值表：一致 / 不一致 / 长度不等 / 非 ASCII。"""
        assert constant_time_token_matches("abc", "abc") is True
        assert constant_time_token_matches("abc", "abd") is False
        assert constant_time_token_matches("abc", "abcd") is False
        assert constant_time_token_matches("密钥", "密钥") is True
        assert constant_time_token_matches("密钥", "其他") is False

    async def test_resolve_bind_address_defaults(self) -> None:
        """绑定地址缺省：容器形态 0.0.0.0:8766。"""
        host, port = resolve_bind_address()
        assert host == "0.0.0.0"
        assert port == 8766

    async def test_resolve_bind_address_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """绑定地址 env 覆写：BROWSER_SIDECAR_HOST / BROWSER_SIDECAR_PORT。"""
        monkeypatch.setenv("BROWSER_SIDECAR_HOST", "127.0.0.1")
        monkeypatch.setenv("BROWSER_SIDECAR_PORT", "9001")
        assert resolve_bind_address() == ("127.0.0.1", 9001)

    async def test_resolve_bind_address_invalid_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非法端口：RuntimeError（配置错误面）。"""
        monkeypatch.setenv("BROWSER_SIDECAR_PORT", "not-a-port")
        with pytest.raises(RuntimeError, match="BROWSER_SIDECAR_PORT"):
            resolve_bind_address()

    async def test_main_wires_resolved_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main()：uvicorn.run 接收解析后的 host/port + app 工厂产物。"""
        monkeypatch.setenv("BROWSER_SIDECAR_HOST", "127.0.0.1")
        monkeypatch.setenv("BROWSER_SIDECAR_PORT", "9002")
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
