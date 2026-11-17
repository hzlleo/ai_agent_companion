"""导航 / 子资源策略纯函数测试——允许清单 / 私网拒绝 / 协议面（穷尽矩阵）。

覆盖矩阵：parse_allowlist（空 / 分隔符 / 大小写 / 领先点 / 顺序保持）；
domain_matches_allowlist（精确 / 子域 / 点边界反例 / 空清单）；协议面
（http/https 通过；about / javascript / file / data / 无协议拒绝）；
extract_url_host（归一 / 缺失拒绝）；parse_ip_literal（v4 / v6 / 括号 /
域名 / 垃圾）；私网判定（loopback / RFC1918 / link-local / ULA / 保留 /
公网 / fail-closed 垃圾文本）；require_host_allowed（空清单拒绝 / 命中 /
不命中）；request_host_allowed（路由拦截判定矩阵）。
"""

from __future__ import annotations

import pytest

from browser_sidecar import policy


class TestParseAllowlist:
    async def test_empty_raw_returns_empty_list(self) -> None:
        """空串 / 纯分隔符：空清单（navigate 拒绝一切语义）。"""
        assert policy.parse_allowlist("") == []
        assert policy.parse_allowlist(",,,") == []
        assert policy.parse_allowlist(" , , ") == []

    async def test_normalization(self) -> None:
        """归一：strip + 小写 + 去首尾点；顺序保持。"""
        assert policy.parse_allowlist(" Example.COM. , cdn.example.org ") == [
            "example.com",
            "cdn.example.org",
        ]

    async def test_leading_dot_entry_normalized(self) -> None:
        """领先点条目归一（``.ORG`` → ``org``——用户 *.org 意图）。"""
        assert policy.parse_allowlist(".ORG") == ["org"]

    async def test_env_unset_semantics_via_empty_default(self) -> None:
        """环境变量未设置 = 空（默认拒绝——与 http_fetch 同哲学的锚点）。"""
        assert policy.parse_allowlist("") == []


class TestDomainMatchesAllowlist:
    async def test_exact_match(self) -> None:
        """精确命中。"""
        assert policy.domain_matches_allowlist("example.com", ["example.com"]) is True

    async def test_subdomain_match_dot_boundary(self) -> None:
        """子域命中（点边界）。"""
        assert policy.domain_matches_allowlist("api.example.com", ["example.com"]) is True
        assert policy.domain_matches_allowlist("a.b.example.com", ["example.com"]) is True

    async def test_dot_boundary_negatives(self) -> None:
        """点边界反例：evil-example.com 不命中 example.com。"""
        assert policy.domain_matches_allowlist("evil-example.com", ["example.com"]) is False
        assert policy.domain_matches_allowlist("notexample.com", ["example.com"]) is False
        assert policy.domain_matches_allowlist("example.com.evil.org", ["example.com"]) is False

    async def test_empty_allowlist_always_false(self) -> None:
        """空清单恒 False（默认拒绝）。"""
        assert policy.domain_matches_allowlist("example.com", []) is False

    async def test_case_and_trailing_dot_normalized(self) -> None:
        """大小写 / 尾点归一（DNS 语义）。"""
        assert policy.domain_matches_allowlist("Example.COM.", ["example.com"]) is True


class TestUrlScheme:
    async def test_http_https_allowed(self) -> None:
        """http / https 通过。"""
        assert policy.url_scheme_is_allowed("http://example.com/x") is True
        assert policy.url_scheme_is_allowed("https://example.com/x") is True

    async def test_dangerous_schemes_refused(self) -> None:
        """危险协议拒绝：about / javascript / file / data / ftp / 无协议。"""
        assert policy.url_scheme_is_allowed("about:blank") is False
        assert policy.url_scheme_is_allowed("javascript:alert(1)") is False
        assert policy.url_scheme_is_allowed("file:///etc/passwd") is False
        assert policy.url_scheme_is_allowed("data:text/html,x") is False
        assert policy.url_scheme_is_allowed("ftp://example.com/x") is False
        assert policy.url_scheme_is_allowed("not-a-url") is False


class TestExtractUrlHost:
    async def test_host_normalized(self) -> None:
        """主机归一：小写 + 去尾点。"""
        assert policy.extract_url_host("https://Example.COM./x") == "example.com"

    async def test_port_tolerated(self) -> None:
        """带端口 URL：主机提取不含端口。"""
        assert policy.extract_url_host("https://example.com:8443/x") == "example.com"

    async def test_missing_host_rejected(self) -> None:
        """主机缺失：PolicyViolationError（受控拒绝）。"""
        with pytest.raises(policy.PolicyViolationError, match="主机名"):
            policy.extract_url_host("https:///path-only")

    async def test_ipv6_bracket_host(self) -> None:
        """IPv6 括号形态：提取括号内字面量。"""
        assert policy.extract_url_host("http://[::1]/x") == "::1"


class TestParseIpLiteral:
    async def test_ipv4_literal(self) -> None:
        """IPv4 字面量解析。"""
        assert policy.parse_ip_literal("192.168.1.1") is not None

    async def test_ipv6_literal_and_brackets(self) -> None:
        """IPv6 字面量（裸 / 括号形态）。"""
        assert policy.parse_ip_literal("::1") is not None
        assert policy.parse_ip_literal("[::1]") is not None
        assert policy.parse_ip_literal("fe80::1") is not None

    async def test_domain_returns_none(self) -> None:
        """域名 / 垃圾形态：None（非字面量）。"""
        assert policy.parse_ip_literal("example.com") is None
        assert policy.parse_ip_literal("not-an-ip") is None
        assert policy.parse_ip_literal("") is None


class TestPrivateRefusal:
    async def test_ip_literal_private_matrix(self) -> None:
        """私网字面量矩阵：loopback / RFC1918 / link-local / ULA / 保留。"""
        for host in (
            "127.0.0.1",
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.9",
            "172.31.255.255",
            "169.254.169.254",
            "::1",
            "fe80::1",
            "fc00::1",
            "0.0.0.0",
        ):
            assert policy.ip_literal_host_is_private(host) is True, host

    async def test_ip_literal_public_not_private(self) -> None:
        """公网字面量：非私网（进入允许清单判定面）。"""
        assert policy.ip_literal_host_is_private("8.8.8.8") is False
        assert policy.ip_literal_host_is_private("1.1.1.1") is False

    async def test_domain_not_private_literal(self) -> None:
        """域名：非 IP 字面量（私网判定 False——DNS 解析面另行处理）。"""
        assert policy.ip_literal_host_is_private("example.com") is False

    async def test_ip_text_private_matrix(self) -> None:
        """解析产物 IP 文本私网矩阵（DNS 解析消费面）。"""
        assert policy.ip_text_is_private("10.0.0.5") is True
        assert policy.ip_text_is_private("127.0.0.1") is True
        assert policy.ip_text_is_private("172.20.1.2") is True
        assert policy.ip_text_is_private("192.168.0.1") is True
        assert policy.ip_text_is_private("169.254.1.1") is True
        assert policy.ip_text_is_private("::ffff:10.0.0.1") is True
        assert policy.ip_text_is_private("8.8.8.8") is False

    async def test_ip_text_garbage_fail_closed(self) -> None:
        """垃圾文本 fail-closed：按私网处理（解析器产物不得穿透拒绝面）。"""
        assert policy.ip_text_is_private("not-an-ip") is True
        assert policy.ip_text_is_private("") is True


class TestRequireHostAllowed:
    async def test_empty_allowlist_refuses_all(self) -> None:
        """空清单：拒绝一切（默认拒绝——零配置零出站）。"""
        with pytest.raises(policy.PolicyViolationError, match="默认拒绝"):
            policy.require_host_allowed("example.com", [])

    async def test_allowed_host_passes(self) -> None:
        """命中清单：通过。"""
        policy.require_host_allowed("api.example.com", ["example.com"])

    async def test_disallowed_host_rejected(self) -> None:
        """不命中清单：受控拒绝。"""
        with pytest.raises(policy.PolicyViolationError, match="不在允许清单"):
            policy.require_host_allowed("evil.example.org", ["example.com"])


class TestRequestHostAllowed:
    async def test_allowed_request_passes(self) -> None:
        """路由拦截：清单内请求放行（主文档 / 子资源 / 重定向同判定）。"""
        assert (
            policy.request_host_allowed("https://cdn.example.com/app.js", ["example.com"])
            is True
        )

    async def test_non_allowlisted_subresource_blocked(self) -> None:
        """路由拦截：清单外子资源阻断（跨域 CDN 默认全断——文档化取舍）。"""
        assert (
            policy.request_host_allowed("https://evil-cdn.net/app.js", ["example.com"])
            is False
        )

    async def test_private_literal_blocked_regardless_of_allowlist(self) -> None:
        """路由拦截：私网字面量阻断——即使清单显式包含。"""
        assert policy.request_host_allowed("http://127.0.0.1/admin", ["127.0.0.1"]) is False
        assert policy.request_host_allowed("http://10.0.0.5/x", ["10.0.0.5"]) is False

    async def test_bad_scheme_blocked(self) -> None:
        """路由拦截：非 http/https 协议阻断。"""
        assert policy.request_host_allowed("javascript:alert(1)", ["example.com"]) is False
        assert policy.request_host_allowed("data:text/html,x", []) is False

    async def test_empty_allowlist_blocks_everything(self) -> None:
        """路由拦截：空清单全断（与 navigate 默认拒绝一致）。"""
        assert policy.request_host_allowed("https://example.com/x", []) is False

    async def test_missing_host_blocked(self) -> None:
        """路由拦截：无主机 URL 阻断（形态异常面）。"""
        assert policy.request_host_allowed("https:///path", ["example.com"]) is False
