"""通道路由出站附件守卫——URL 形态 / 私网字面量 / 大小上限（纯函数面）。

出站附件的三种媒体源（base64 / url / path）在通道适配器内解析；其中
``url`` 来自外部消息载荷、``path`` 来自内部生产者。本模块把可复用的
安全判定集中在通道域：协议白名单、私网 IP 字面量拒绝、附件字节上限。
适配器只做 I/O 与受控失败映射，判定本身无 I/O。
"""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import urlparse

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "OUTBOUND_ATTACHMENT_MAX_BYTES",
    "is_private_ip_literal",
    "validate_attachment_size",
    "validate_http_url",
]

#: 出站附件字节上限（8MB——与企微素材上传的常见限制留出余量）。
OUTBOUND_ATTACHMENT_MAX_BYTES: Final[int] = 8 * 1024 * 1024

#: 允许的出站 URL 协议面。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


def is_private_ip_literal(host: str) -> bool:
    """判定主机名是否为私网 / 保留段的 IP 字面量（纯函数）。

    语义与浏览器 sidecar 的导航策略对齐：loopback、RFC1918、link-local、
    ULA、保留 / 组播 / 未指定段一律视为私网。域名（非字面量）返回
    False——DNS 解析复检由各消费面（浏览器 sidecar / 沙盒）承担。

    Args:
        host: 归一化主机名（IPv4 点分 / IPv6 冒分；容忍方括号包裹）。

    Returns:
        True 表示私网 IP 字面量。
    """
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def validate_http_url(url: str) -> None:
    """校验出站附件 URL（协议白名单 + 主机名 + 私网字面量拒绝）。

    Args:
        url: 媒体 URL（外部载荷来源）。

    Raises:
        BusinessException: 协议非 http/https / 缺少主机名 / 主机为私网
            IP 字面量（``VALIDATION_ERROR``——受控中文文案）。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 协议必须是 http/https",
            details={"url": url, "scheme": parsed.scheme},
        )
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 缺少主机名",
            details={"url": url},
        )
    if is_private_ip_literal(host):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 不得指向私网 IP 字面量",
            details={"url": url, "host": host},
        )


def validate_attachment_size(size: int, *, source: str) -> None:
    """校验附件字节数上限（受控失败——错误面携带来源标签）。

    Args:
        size: 实际读取到的字节数。
        source: 来源标签（``url`` / ``path``——错误文案的定位面）。

    Raises:
        BusinessException: 字节数超过 :data:`OUTBOUND_ATTACHMENT_MAX_BYTES`
            （``CHANNEL_API_ERROR``——逐附件失败承载）。
    """
    if size > OUTBOUND_ATTACHMENT_MAX_BYTES:
        raise BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            message=(
                f"企微出站附件（{source}）超过大小上限 "
                f"{OUTBOUND_ATTACHMENT_MAX_BYTES} 字节"
            ),
            details={"source": source, "size_bytes": size},
        )
