"""通道路由出站附件守卫测试——URL 协议 / 主机归一 / 解析后私网判定 / 上限。

零网络：主机解析经注入 fake resolver（映射主机名 → 地址集合），默认
resolver 的 ``socket.getaddrinfo`` 不参与单元测试；校验失败统一
``BusinessException``（受控文案），成功形态返回归一主机名。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from packages.harness.channels.outbound_guard import (
    OUTBOUND_ATTACHMENT_MAX_BYTES,
    HostAddressResolver,
    is_private_ip_literal,
    normalize_host,
    validate_attachment_size,
    validate_http_url,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _resolver(mapping: Mapping[str, Sequence[str]]) -> HostAddressResolver:
    """构造主机名 → 地址集合的 fake resolver。

    Args:
        mapping: 主机名到地址元组的映射（未命中返回空元组）。

    Returns:
        异步解析函数。
    """

    async def _resolve(host: str) -> tuple[str, ...]:
        return tuple(mapping.get(host, ()))

    return _resolve


def _rejecting_resolver(error: Exception) -> HostAddressResolver:
    """构造恒抛指定异常的 resolver（解析失败 fail-closed 测试）。

    Args:
        error: 解析时抛出的异常。

    Returns:
        异步解析函数。
    """

    async def _resolve(host: str) -> tuple[str, ...]:
        raise error

    return _resolve


async def test_http_and_https_urls_pass() -> None:
    """http/https 公网域名通过（含端口与路径）。"""
    resolver = _resolver({"cdn.example.com": ("93.184.216.34",)})
    assert (
        await validate_http_url("http://cdn.example.com/a.png", resolver=resolver)
        == "cdn.example.com"
    )
    assert (
        await validate_http_url(
            "https://cdn.example.com:8443/nested/a.png?token=1", resolver=resolver
        )
        == "cdn.example.com"
    )


async def test_public_ip_literal_passes() -> None:
    """公网 IP 字面量通过（解析结果为公网地址）。"""
    resolver = _resolver({"93.184.216.34": ("93.184.216.34",)})
    await validate_http_url("https://93.184.216.34/a.png", resolver=resolver)


async def test_ipv6_public_address_passes() -> None:
    """公网 IPv6 地址通过（非 v4-mapped / 非 ULA）。"""
    resolver = _resolver({"2606:4700::1111": ("2606:4700::1111",)})
    await validate_http_url("https://[2606:4700::1111]/a.png", resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "ftp://cdn.example.com/a.png",
    ],
)
async def test_non_http_schemes_rejected(url: str) -> None:
    """非 http/https 协议：拒绝（VALIDATION_ERROR + 协议文案）。"""
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url(url, resolver=_resolver({}))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "http/https" in exc_info.value.message


async def test_missing_host_rejected() -> None:
    """缺少主机名：拒绝（受控文案）。"""
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url("https:///path", resolver=_resolver({}))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "主机名" in exc_info.value.message


@pytest.mark.parametrize(
    "host",
    ["127.1", "2130706433", "0x7f.1", "0177.0.0.1", "0", "[::ffff:127.0.0.1]", "127.0.0.1."],
)
async def test_non_canonical_private_forms_rejected_by_resolution(host: str) -> None:
    """七类非规范 / 特殊形态：按解析结果判定为私网并拒绝。

    这些主机名无法由 :mod:`ipaddress` 直接解析（``127.1`` / 整数 /
    十六进制 / 八进制 / ``0`` / v4-mapped / 尾点），若只看字面量会放行；
    resolver 返回系统解析后的地址后必须拒绝。
    """
    normalized = normalize_host(host)
    if "ffff" in normalized:
        resolved = "::ffff:127.0.0.1"
    elif normalized == "0":
        resolved = "0.0.0.0"
    else:
        resolved = "127.0.0.1"
    resolver = _resolver({normalized: (resolved,)})
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url(f"http://{host}/a.png", resolver=resolver)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "私网" in exc_info.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://10.0.0.5/a.png",
        "http://172.16.3.4/a.png",
        "http://192.168.1.9/a.png",
        "http://169.254.10.1/a.png",
        "http://[::1]/a.png",
        "http://[fc00::1]/a.png",
        "http://[::ffff:192.168.0.1]/a.png",
    ],
)
async def test_private_addresses_rejected_after_resolution(url: str) -> None:
    """私网 / loopback / link-local / ULA / v4-mapped：解析后拒绝。"""
    host = normalize_host(url.split("//", 1)[1].split("/", 1)[0])
    resolver = _resolver({host: (host,)})
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url(url, resolver=resolver)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "私网" in exc_info.value.message


async def test_any_private_address_in_resolution_rejects() -> None:
    """解析出多个地址时任一私网即拒绝（全部地址判公网）。"""
    resolver = _resolver({"mixed.example.com": ("93.184.216.34", "10.0.0.7")})
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url("https://mixed.example.com/a.png", resolver=resolver)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.details["address"] == "10.0.0.7"


async def test_resolver_failure_fails_closed() -> None:
    """resolver 抛错：fail-closed 拒绝（不因解析失败放行）。"""
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url(
            "https://cdn.example.com/a.png",
            resolver=_rejecting_resolver(OSError("dns down")),
        )
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "fail-closed" in exc_info.value.message


async def test_empty_resolution_fails_closed() -> None:
    """resolver 返回空集合：fail-closed 拒绝。"""
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url("https://cdn.example.com/a.png", resolver=_resolver({}))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "为空" in exc_info.value.message


async def test_malformed_resolved_address_fails_closed() -> None:
    """解析结果不是合法地址：按非公网拒绝（fail-closed）。"""
    resolver = _resolver({"cdn.example.com": ("not-an-address",)})
    with pytest.raises(BusinessException) as exc_info:
        await validate_http_url("https://cdn.example.com/a.png", resolver=resolver)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_normalize_host_strips_whitespace_brackets_trailing_dot_and_case() -> None:
    """主机归一：去空白 / 尾点 / 方括号 / 大小写。"""
    assert normalize_host("  [::1].  ") == "::1"
    assert normalize_host("EXAMPLE.COM.") == "example.com"
    assert normalize_host(" 127.0.0.1. ") == "127.0.0.1"
    assert normalize_host("") == ""


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.0.1",
        "::1",
        "[::1]",
        "fc00::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ],
)
async def test_is_private_ip_literal_true(host: str) -> None:
    """私网 / 保留 / 组播 / v4-mapped 字面量判定为 True。"""
    assert is_private_ip_literal(host) is True


@pytest.mark.parametrize(
    "host",
    ["cdn.example.com", "93.184.216.34", "2606:4700::1111", "127.1"],
)
async def test_is_private_ip_literal_false_for_public_or_non_literal(host: str) -> None:
    """公网字面量与域名（含无法解析的非规范形态）判定为 False。"""
    assert is_private_ip_literal(host) is False


async def test_size_cap_boundary() -> None:
    """字节上限边界：恰好上限通过，超出 1 字节拒绝。"""
    validate_attachment_size(OUTBOUND_ATTACHMENT_MAX_BYTES, source="url")
    with pytest.raises(BusinessException) as exc_info:
        validate_attachment_size(OUTBOUND_ATTACHMENT_MAX_BYTES + 1, source="path")
    assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
    assert "超过大小上限" in exc_info.value.message
