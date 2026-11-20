"""通道路由出站附件守卫测试——URL 协议 / 私网字面量 / 字节上限纯函数矩阵。

零网络：全部判定为纯函数直调；校验失败统一 ``BusinessException``
（受控文案），成功形态返回 None。
"""

from __future__ import annotations

import pytest

from packages.harness.channels.outbound_guard import (
    OUTBOUND_ATTACHMENT_MAX_BYTES,
    is_private_ip_literal,
    validate_attachment_size,
    validate_http_url,
)
from packages.harness.core.errors import BusinessException, ErrorCode


async def test_http_and_https_urls_pass() -> None:
    """http/https 公网域名通过（含端口与路径）。"""
    validate_http_url("http://cdn.example.com/a.png")
    validate_http_url("https://cdn.example.com:8443/nested/a.png?token=1")


async def test_public_ip_literal_passes() -> None:
    """公网 IP 字面量通过（非私网段）。"""
    validate_http_url("https://93.184.216.34/a.png")


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
        validate_http_url(url)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "http/https" in exc_info.value.message


async def test_missing_host_rejected() -> None:
    """缺少主机名：拒绝（受控文案）。"""
    with pytest.raises(BusinessException) as exc_info:
        validate_http_url("https:///path")
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "主机名" in exc_info.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://127.1.2.3/a.png",
        "http://10.0.0.5/a.png",
        "http://172.16.3.4/a.png",
        "http://192.168.1.9/a.png",
        "http://169.254.10.1/a.png",
        "http://[::1]/a.png",
        "http://[fc00::1]/a.png",
    ],
)
async def test_private_ip_literals_rejected(url: str) -> None:
    """私网 / loopback / link-local / ULA 字面量：拒绝（文案含「私网」）。"""
    with pytest.raises(BusinessException) as exc_info:
        validate_http_url(url)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "私网" in exc_info.value.message


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.1.2.3", "169.254.0.1", "::1", "[::1]", "fc00::1", "0.0.0.0"],
)
async def test_is_private_ip_literal_true(host: str) -> None:
    """私网 / 保留字面量判定为 True。"""
    assert is_private_ip_literal(host) is True


@pytest.mark.parametrize("host", ["cdn.example.com", "93.184.216.34", "2606:4700::1111"])
async def test_is_private_ip_literal_false_for_public_or_domain(host: str) -> None:
    """公网字面量与域名判定为 False（域名解析复检由消费面承担）。"""
    assert is_private_ip_literal(host) is False


async def test_size_cap_boundary() -> None:
    """字节上限边界：恰好上限通过，超出 1 字节拒绝。"""
    validate_attachment_size(OUTBOUND_ATTACHMENT_MAX_BYTES, source="url")
    with pytest.raises(BusinessException) as exc_info:
        validate_attachment_size(OUTBOUND_ATTACHMENT_MAX_BYTES + 1, source="path")
    assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
    assert "超过大小上限" in exc_info.value.message
