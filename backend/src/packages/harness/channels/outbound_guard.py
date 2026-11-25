"""通道路由出站附件守卫——URL 形态 / 主机解析 / 私网判定 / 大小上限。

出站附件的三种媒体源（base64 / url / path）在通道适配器内解析；其中
``url`` 来自外部消息载荷、``path`` 来自内部生产者。本模块把可复用的
安全判定集中在通道域：

    - **主机归一**：去空白 / 尾点 / 方括号包裹并统一小写——非规范 IPv4
      形态（``127.1`` / ``2130706433`` / ``0x7f.1`` / ``0177.0.0.1`` /
      ``0``）与带尾点 / 方括号的形态都归一后再进入解析；
    - **解析后判定（防 SSRF 绕过）**：主机（含 IP 字面量）经可注入
      resolver 解析为地址集合，**全部地址**都必须是公网地址；任何
      loopback / link-local / private / reserved / multicast /
      unspecified（含 IPv6 与 v4-mapped IPv6）即拒绝。解析失败 / 空结果
      fail-closed（拒绝而不是放行）；
    - **附件字节上限**：8 MiB（与企微素材上传的常见限制留出余量）。

适配器只做 I/O 与受控失败映射，判定本身无隐式网络；默认 resolver 经
``asyncio`` 事件循环的 ``socket.getaddrinfo`` 执行（测试注入 fake）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Final
from urllib.parse import urlparse

from loguru import logger

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "OUTBOUND_ATTACHMENT_MAX_BYTES",
    "HostAddressResolver",
    "is_private_ip_literal",
    "is_public_address",
    "normalize_host",
    "resolve_host_addresses",
    "validate_attachment_size",
    "validate_http_url",
]

#: 出站附件字节上限（8MB——与企微素材上传的常见限制留出余量）。
OUTBOUND_ATTACHMENT_MAX_BYTES: Final[int] = 8 * 1024 * 1024

#: 允许的出站 URL 协议面。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: 主机名 → 地址列表的异步解析面（生产 = :func:`resolve_host_addresses`）。
HostAddressResolver = Callable[[str], Awaitable[Sequence[str]]]


def normalize_host(host: str) -> str:
    """归一主机名（去空白 / 尾点 / 方括号包裹并统一小写）。

    Args:
        host: 原始主机名（可能带空白 / 尾点 / IPv6 方括号 / 大写）。

    Returns:
        归一后的主机名（空输入返回空串）。
    """
    value = "".join(host.split()).rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.lower()


def _is_non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判定已解析地址是否属于非公网段（v4-mapped IPv6 先解映射）。

    Args:
        address: 已解析的 IP 地址对象。

    Returns:
        True = loopback / link-local / private / reserved / multicast /
        unspecified（任一段即拒绝）。
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def is_public_address(address: str) -> bool:
    """判定解析结果地址是否为公网地址（无法解析按非公网——fail-closed）。

    Args:
        address: resolver 返回的地址字面量（IPv4 / IPv6；容忍方括号）。

    Returns:
        True = 可解析且所有非公网段判定均为 False。
    """
    try:
        parsed = ipaddress.ip_address(normalize_host(address))
    except ValueError:
        return False
    return not _is_non_public_address(parsed)


def is_private_ip_literal(host: str) -> bool:
    """判定主机名是否为私网 / 保留段的 IP 字面量（纯函数）。

    语义与浏览器 sidecar 的导航策略对齐：loopback、RFC1918、link-local、
    ULA、保留 / 组播 / 未指定段一律视为私网。域名（非字面量）返回
    False；非规范 IPv4 形态（``127.1`` 等）无法由 :mod:`ipaddress` 解析，
    也返回 False——其真实地址由 :func:`validate_http_url` 的解析面判定。

    Args:
        host: 原始主机名（IPv4 点分 / IPv6 冒分；容忍方括号包裹）。

    Returns:
        True 表示私网 IP 字面量。
    """
    try:
        parsed = ipaddress.ip_address(normalize_host(host))
    except ValueError:
        return False
    return _is_non_public_address(parsed)


async def resolve_host_addresses(host: str) -> tuple[str, ...]:
    """经事件循环的 ``socket.getaddrinfo`` 解析主机名为地址集合。

    Args:
        host: 归一后的主机名（IP 字面量同样解析——非规范形态按系统
            解析结果归一）。

    Returns:
        去重后的地址字面量元组（解析失败由调用方 fail-closed）。

    Raises:
        OSError: 系统解析失败（``socket.gaierror`` 等）。
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


async def validate_http_url(
    url: str, *, resolver: HostAddressResolver | None = None
) -> str:
    """校验出站附件 URL（协议白名单 + 主机归一 + 解析后全地址私网拒绝）。

    Args:
        url: 媒体 URL（外部载荷来源）。
        resolver: 主机解析面（None = :func:`resolve_host_addresses`；测试
            注入 fake）。

    Returns:
        归一后的主机名（调用方重定向循环可用于诊断）。

    Raises:
        BusinessException: 协议非 http/https / 缺少主机名 / 解析失败或
            空结果 / 任一地址为非公网（``VALIDATION_ERROR``——受控中文
            文案）。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 协议必须是 http/https",
            details={"url": url, "scheme": parsed.scheme},
        )
    host = normalize_host(parsed.hostname or "")
    if not host:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 缺少主机名",
            details={"url": url},
        )
    resolve = resolver if resolver is not None else resolve_host_addresses
    try:
        addresses = tuple(await resolve(host))
    except (OSError, UnicodeError, ValueError) as exc:
        logger.debug("出站附件 URL 主机解析失败（fail-closed）: host={}, error={}", host, exc)
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 主机解析失败（fail-closed，拒绝出站）",
            details={"url": url, "host": host, "error": str(exc)},
        ) from exc
    if not addresses:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="出站附件 URL 主机解析结果为空（fail-closed，拒绝出站）",
            details={"url": url, "host": host},
        )
    for address in addresses:
        if not is_public_address(address):
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="出站附件 URL 不得指向私网 / 保留地址",
                details={"url": url, "host": host, "address": address},
            )
    return host


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
