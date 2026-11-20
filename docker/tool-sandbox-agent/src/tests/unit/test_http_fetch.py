"""http_fetch 工具测试——允许清单矩阵 / URL 校验 / 抓取（monkeypatch 零网络）。

覆盖矩阵：parse_allowlist 归一化；域名点边界匹配（含前缀仿冒负例）；
HttpFetchArgsModel 校验（协议 / 主机 / 超时边界 / 默认值）；重定向逐跳
校验（允许 / 拒绝）；default_fetch 经 monkeypatch build_opener（成功形态 /
8KB 截断 / 字符集回退 / HTTP 错误 / 传输失败 / 超时 / 首跳拒绝 / 环境代理
禁用——opener 携带空 ProxyHandler）；执行器经注入 fetch（禁用拒绝 /
参数透传 / 经运行时全链默认值物化）。

hermetic 红线：零真实网络——default_fetch 的 urlopen 面经 fake opener
替换；执行器测试注入 fake fetch 函数。
"""

from __future__ import annotations

import email.message
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import ValidationError

import tool_sandbox.http_fetch as http_fetch_module
from tool_sandbox.http_fetch import (
    HTTP_FETCH_TOOL_NAME,
    HttpFetchArgsModel,
    build_http_fetch_tool,
    default_fetch,
    domain_matches_allowlist,
    parse_allowlist,
    validate_fetch_url,
)
from tool_sandbox.runtime import SandboxToolError, SandboxToolRuntime
from tool_sandbox.wire_models import InvokeRequestModel


class TestAllowlistParsing:
    async def test_empty_raw_yields_empty_list(self) -> None:
        """空串 / 纯分隔符：空清单（禁用语义）。"""
        assert parse_allowlist("") == []
        assert parse_allowlist(" , , ") == []

    async def test_normalization_and_order(self) -> None:
        """归一化：strip + 小写 + 去尾点；保持输入顺序。"""
        assert parse_allowlist(" Example.COM , api.github.com ,.ORG ") == [
            "example.com",
            "api.github.com",
            "org",
        ]

    async def test_domain_match_matrix(self) -> None:
        """点边界匹配矩阵：精确 / 子域命中；前缀仿冒 / 空清单不命中。"""
        allowlist = ["example.com", "api.github.com"]
        assert domain_matches_allowlist("example.com", allowlist) is True
        assert domain_matches_allowlist("www.example.com", allowlist) is True
        assert domain_matches_allowlist("deep.api.github.com", allowlist) is True
        # 点边界负例：前缀仿冒不命中
        assert domain_matches_allowlist("evil-example.com", allowlist) is False
        assert domain_matches_allowlist("notexample.com", allowlist) is False
        assert domain_matches_allowlist("evil.com", ["vil.com"]) is False
        # 大小写 / 尾点归一
        assert domain_matches_allowlist("EXAMPLE.com.", allowlist) is True
        # 空清单恒 False（默认拒绝）
        assert domain_matches_allowlist("example.com", []) is False


class TestArgsModel:
    async def test_valid_url_and_default_timeout(self) -> None:
        """合法参数：url 直读 + timeout 默认 10。"""
        args = HttpFetchArgsModel.model_validate({"url": "https://example.com/page"})
        assert args.url == "https://example.com/page"
        assert args.timeout_seconds == 10

    async def test_url_scheme_rejected(self) -> None:
        """协议面：ftp / 无协议拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "ftp://example.com/file"})
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "example.com/page"})

    async def test_url_missing_host_rejected(self) -> None:
        """主机面：http:// 无主机拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "http://"})

    async def test_timeout_bounds(self) -> None:
        """超时边界：1 / 30 合法；0 / 31 拒绝。"""
        assert HttpFetchArgsModel.model_validate(
            {"url": "https://example.com", "timeout_seconds": 1}
        ).timeout_seconds == 1
        assert HttpFetchArgsModel.model_validate(
            {"url": "https://example.com", "timeout_seconds": 30}
        ).timeout_seconds == 30
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "https://example.com", "timeout_seconds": 0})
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "https://example.com", "timeout_seconds": 31})

    async def test_extra_fields_rejected(self) -> None:
        """extra=forbid：未知参数拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate(
                {"url": "https://example.com", "method": "POST"}
            )


class TestValidateFetchUrl:
    async def test_allowed_host_returned_normalized(self) -> None:
        """合法 URL：返回归一化主机（小写 / 去尾点）。"""
        host = validate_fetch_url("https://WWW.Example.com./page", ["example.com"])
        assert host == "www.example.com"

    async def test_scheme_violation_controlled(self) -> None:
        """协议违规：受控失败（http/https 之外拒绝）。"""
        with pytest.raises(SandboxToolError, match="协议"):
            validate_fetch_url("ftp://example.com/file", ["example.com"])

    async def test_missing_host_controlled(self) -> None:
        """主机缺失：受控失败。"""
        with pytest.raises(SandboxToolError, match="主机名"):
            validate_fetch_url("https://", ["example.com"])

    async def test_allowlist_miss_controlled(self) -> None:
        """域名不在清单：受控失败（host 入文案——可观测）。"""
        with pytest.raises(SandboxToolError, match="evil.example.org"):
            validate_fetch_url("https://evil.example.org/x", ["example.com"])

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "10.0.0.5", "172.16.1.1", "169.254.1.1", "::1", "fc00::1"],
    )
    async def test_private_ip_literal_rejected_even_in_allowlist(self, host: str) -> None:
        """私网 IP 字面量：即使命中允许清单也拒绝。"""
        url = f"http://[{host}]/x" if ":" in host else f"http://{host}/x"
        with pytest.raises(SandboxToolError, match="私网"):
            validate_fetch_url(url, [host])

    async def test_public_ip_literal_allowed(self) -> None:
        """公网 IP 字面量：命中允许清单即通过。"""
        assert validate_fetch_url("http://93.184.216.34/x", ["93.184.216.34"]) == (
            "93.184.216.34"
        )


class _FakeResponse:
    """fake urllib 响应（上下文管理器 + read / geturl / headers）。"""

    def __init__(
        self,
        *,
        status: int = 200,
        url: str = "http://allowed.example/page",
        content_type: str = "text/plain; charset=utf-8",
        body: bytes = b"hello",
    ) -> None:
        """初始化（headers 经 email.message 承载 charset 解析）。"""
        self.status = status
        self.url = url
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        self._body = body

    def geturl(self) -> str:
        """终态 URL。"""
        return self.url

    def read(self, amount: int = -1) -> bytes:
        """按上限读正文（urllib 语义）。"""
        if amount < 0:
            return self._body
        return self._body[:amount]

    def __enter__(self) -> _FakeResponse:
        """进入上下文（with opener.open(...) as response:）。"""
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        """退出上下文（不吞异常）。"""
        return False


class _FakeOpener:
    """fake urllib opener（记录请求 + 可注入响应 / 异常）。"""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        """初始化（response 与 error 二选一——error 优先）。"""
        self._response = response
        self._error = error
        self.calls: list[tuple[urllib.request.Request, int | None]] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        """记录请求并返回注入的响应 / 抛注入的异常。"""
        self.calls.append((request, timeout))
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _patch_opener(monkeypatch: pytest.MonkeyPatch, opener: _FakeOpener) -> None:
    """将 urllib.request.build_opener 替换为 fake opener 工厂，并注入公网解析面。"""
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(
        http_fetch_module, "_resolve_host_addresses", lambda host: ["93.184.216.34"]
    )


class TestDefaultFetch:
    async def test_success_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功形态：url / status / content_type / body / truncated 字段齐备。"""
        opener = _FakeOpener(
            response=_FakeResponse(content_type="text/html; charset=utf-8", body=b"<html>x</html>")
        )
        _patch_opener(monkeypatch, opener)
        result = default_fetch("http://allowed.example/page", 10, ["allowed.example"])
        assert result["url"] == "http://allowed.example/page"
        assert result["status_code"] == 200
        assert result["content_type"] == "text/html; charset=utf-8"
        assert result["body"] == "<html>x</html>"
        assert result["truncated"] is False
        # 请求面：目标 URL + 超时透传
        request, timeout = opener.calls[0]
        assert request.full_url == "http://allowed.example/page"
        assert timeout == 10

    async def test_body_truncated_at_8kb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """8KB 截断：超限正文按字节截断 + truncated 标记。"""
        big_body = b"a" * 8193
        opener = _FakeOpener(response=_FakeResponse(body=big_body))
        _patch_opener(monkeypatch, opener)
        result = default_fetch("http://allowed.example/big", 10, ["allowed.example"])
        assert result["truncated"] is True
        assert len(result["body"]) == 8192
        assert result["body"] == "a" * 8192

    async def test_exact_boundary_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恰好 8192 字节：不截断（边界值）。"""
        opener = _FakeOpener(response=_FakeResponse(body=b"b" * 8192))
        _patch_opener(monkeypatch, opener)
        result = default_fetch("http://allowed.example/edge", 10, ["allowed.example"])
        assert result["truncated"] is False
        assert len(result["body"]) == 8192

    async def test_unknown_charset_falls_back_utf8(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知字符集：回退 utf-8 容错解码（不抛）。"""
        opener = _FakeOpener(
            response=_FakeResponse(content_type="text/plain; charset=no-such-codec", body="你好".encode())
        )
        _patch_opener(monkeypatch, opener)
        result = default_fetch("http://allowed.example/cn", 10, ["allowed.example"])
        assert "你好" in result["body"]

    async def test_http_error_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 错误状态（4xx/5xx）：受控失败（状态码入文案）。"""
        error = urllib.error.HTTPError(
            "http://allowed.example/missing", 404, "Not Found", None, None
        )
        opener = _FakeOpener(error=error)
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="404"):
            default_fetch("http://allowed.example/missing", 10, ["allowed.example"])

    async def test_url_error_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """传输失败（URLError）：受控失败。"""
        opener = _FakeOpener(error=urllib.error.URLError(OSError("connection refused")))
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="请求失败"):
            default_fetch("http://allowed.example/x", 10, ["allowed.example"])

    async def test_timeout_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超时（TimeoutError 直抛形态）：受控失败（超时秒数入文案）。"""
        opener = _FakeOpener(error=TimeoutError("timed out"))
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="超时"):
            default_fetch("http://allowed.example/slow", 5, ["allowed.example"])

    async def test_value_error_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """URL 形态非法（urllib 拒绝 / ValueError）：受控失败。"""
        opener = _FakeOpener(error=ValueError("invalid url"))
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="URL 形态非法"):
            default_fetch("http://allowed.example/weird", 10, ["allowed.example"])

    async def test_unexpected_exception_controlled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未预期异常兜底：归一受控失败（类型名入文案——恒不抛未受控）。"""
        opener = _FakeOpener(error=RuntimeError("boom"))
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="RuntimeError"):
            default_fetch("http://allowed.example/x", 10, ["allowed.example"])

    async def test_first_hop_rejected_before_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """首跳拒绝：URL 校验先于请求（opener 零调用）。"""
        opener = _FakeOpener(response=_FakeResponse())
        _patch_opener(monkeypatch, opener)
        with pytest.raises(SandboxToolError, match="不在允许清单"):
            default_fetch("http://evil.example.org/x", 10, ["allowed.example"])
        assert opener.calls == []

    async def test_env_proxy_ignored_opener_carries_empty_proxy_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境代理禁用：env 设 http_proxy 时 opener 仍携带空 ProxyHandler。

        前置自证：urllib 缺省 ``build_opener`` 会拾取 env 代理（getproxies
        非空）——本测试证明 default_fetch 显式传入的 ``ProxyHandler({})``
        覆盖该缺省（点对点直连，与 bili_tools 同锁定决策）。
        """
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        assert urllib.request.getproxies().get("http") == "http://127.0.0.1:9"
        captured: list[tuple[Any, ...]] = []

        def _capture_build(*handlers: Any) -> _FakeOpener:
            captured.append(handlers)
            return _FakeOpener(response=_FakeResponse())

        monkeypatch.setattr(urllib.request, "build_opener", _capture_build)
        monkeypatch.setattr(
            http_fetch_module, "_resolve_host_addresses", lambda host: ["93.184.216.34"]
        )
        result = default_fetch("http://allowed.example/direct", 10, ["allowed.example"])

        assert result["status_code"] == 200
        proxy_handlers = [
            handler
            for handler in captured[0]
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        assert proxy_handlers, "default_fetch 须显式传入 ProxyHandler"
        assert all(handler.proxies == {} for handler in proxy_handlers)

    async def test_host_resolving_to_private_rejected_before_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """域名解析到私网：请求前受控失败（opener 零调用）。"""
        opener = _FakeOpener(response=_FakeResponse())
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
        with pytest.raises(SandboxToolError, match="私网"):
            default_fetch(
                "http://allowed.example/x",
                10,
                ["allowed.example"],
                resolver=lambda host: ["10.1.2.3"],
            )
        assert opener.calls == []

    async def test_mixed_public_private_resolution_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解析结果含任一私网地址：整体拒绝（fail-closed）。"""
        opener = _FakeOpener(response=_FakeResponse())
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
        with pytest.raises(SandboxToolError, match="私网"):
            default_fetch(
                "http://allowed.example/x",
                10,
                ["allowed.example"],
                resolver=lambda host: ["93.184.216.34", "192.168.1.10"],
            )
        assert opener.calls == []

    async def test_public_resolution_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析结果全公网：抓取照常成功。"""
        opener = _FakeOpener(response=_FakeResponse(body=b"ok"))
        _patch_opener(monkeypatch, opener)
        result = default_fetch(
            "http://allowed.example/x",
            10,
            ["allowed.example"],
            resolver=lambda host: ["93.184.216.34"],
        )
        assert result["status_code"] == 200
        assert result["body"] == "ok"


class TestRedirectHandler:
    async def test_redirect_to_disallowed_host_rejected(self) -> None:
        """重定向跳出清单：抛受控失败（不发起跳转请求）。"""
        handler = http_fetch_module._AllowlistRedirectHandler(["example.com"])
        request = urllib.request.Request("http://example.com/a")
        with pytest.raises(SandboxToolError, match="允许清单"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                email.message.Message(),
                "https://evil.example.org/next",
            )

    async def test_redirect_within_allowlist_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重定向留在清单内：透传基类（构造新请求，解析面注入零网络）。"""
        monkeypatch.setattr(
            http_fetch_module, "_resolve_host_addresses", lambda host: ["93.184.216.34"]
        )
        handler = http_fetch_module._AllowlistRedirectHandler(["example.com"])
        request = urllib.request.Request("http://example.com/a")
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            email.message.Message(),
            "http://api.example.com/b",
        )
        assert isinstance(redirected, urllib.request.Request)
        assert redirected.full_url == "http://api.example.com/b"


class TestExecutor:
    async def test_disabled_by_empty_allowlist(self) -> None:
        """空清单 = 工具禁用：受控失败（fetch 零调用）。"""
        calls: list[tuple[str, int, list[str]]] = []

        def _fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
            """记录调用的 fake 抓取。"""
            calls.append((url, timeout_seconds, allowlist))
            return {}

        spec, executor = build_http_fetch_tool(
            allowlist_provider=lambda: [],
            fetch_fn=_fetch,
        )
        assert spec.name == HTTP_FETCH_TOOL_NAME
        assert spec.args_model is HttpFetchArgsModel
        with pytest.raises(SandboxToolError, match="未启用"):
            await executor({"url": "https://example.com/x", "timeout_seconds": 10})
        assert calls == []

    async def test_executor_passes_materialized_args(self) -> None:
        """执行器透传：url / timeout / allowlist 三参齐备。"""
        calls: list[tuple[str, int, list[str]]] = []

        def _fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
            """记录调用的 fake 抓取。"""
            calls.append((url, timeout_seconds, allowlist))
            return {"url": url, "status_code": 200, "body": "ok", "truncated": False}

        spec, executor = build_http_fetch_tool(
            allowlist_provider=lambda: ["example.com"],
            fetch_fn=_fetch,
        )
        result = await executor({"url": "https://example.com/x", "timeout_seconds": 3})
        assert result["status_code"] == 200
        assert calls == [("https://example.com/x", 3, ["example.com"])]

    async def test_full_chain_via_runtime_materializes_defaults(self) -> None:
        """经运行时全链：参数默认值物化（timeout 默认 10 进入 fetch）。"""
        calls: list[tuple[str, int, list[str]]] = []

        def _fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
            """记录调用的 fake 抓取。"""
            calls.append((url, timeout_seconds, allowlist))
            return {"url": url, "status_code": 200, "body": "ok", "truncated": False}

        runtime = SandboxToolRuntime()
        spec, executor = build_http_fetch_tool(
            allowlist_provider=lambda: ["example.com"],
            fetch_fn=_fetch,
        )
        runtime.register(spec, executor)
        response = await runtime.invoke(
            InvokeRequestModel(tool=HTTP_FETCH_TOOL_NAME, arguments={"url": "https://example.com/x"})
        )
        assert response.ok is True
        assert response.result["status_code"] == 200
        assert calls == [("https://example.com/x", 10, ["example.com"])]

    async def test_fetch_failure_maps_to_ok_false(self) -> None:
        """fetch 受控失败 → 运行时归一 ok=False（错误文案透传）。"""

        def _fetch(url: str, timeout_seconds: int, allowlist: list[str]) -> dict[str, Any]:
            """恒拒的 fake 抓取。"""
            raise SandboxToolError("目标域名不在允许清单: evil.example.org")

        runtime = SandboxToolRuntime()
        spec, executor = build_http_fetch_tool(
            allowlist_provider=lambda: ["example.com"],
            fetch_fn=_fetch,
        )
        runtime.register(spec, executor)
        response = await runtime.invoke(
            InvokeRequestModel(
                tool=HTTP_FETCH_TOOL_NAME,
                arguments={"url": "https://evil.example.org/x"},
            )
        )
        assert response.ok is False
        assert "允许清单" in (response.error or "")
