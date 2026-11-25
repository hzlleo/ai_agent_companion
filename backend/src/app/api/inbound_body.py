"""公开入站 webhook 的共享有界请求体读取器（QQ / SmsForwarder / WhatsApp 单源）。

三条公开入站端点（豁免管理面鉴权、各自校验平台凭据）都必须在解析 JSON 前
对请求体施加**通道级字节上限**。本模块把上限常量与读取 / 解析流程收敛为
唯一实现：

    POST 请求
        ↓ ① 声明的 Content-Length 预检（超限即受控 400，零读取）
        ↓ ② ``request.stream()`` 逐块累计（无 Content-Length 的 chunked
           传输也受同一上限约束；累计超限的瞬间抛受控 400——异常传播
           终止迭代，后续块不再向 ASGI 流索取）
        ↓ ③ 空体 / 非法 JSON / 非 JSON 对象 → 受控 400
        原始字节（签名校验面）或 JSON 对象（端点业务面）

上限常量：

    - :data:`QQ_MAX_BODY_BYTES` = 8 MiB——OneBot 段数组可内联
      ``base64://`` 媒体载荷；按出站附件帽 8 MiB 与表情单文件 5 MiB 的
      base64 膨胀（约 6.7 MiB）留出余量，避免合法图片段被 64 KiB 短文本
      帽误拒；
    - :data:`SMS_FORWARDER_MAX_BODY_BYTES` = 64 KiB——手机端转发载荷是
      短文本；
    - :data:`WHATSAPP_MAX_BODY_BYTES` = 64 KiB——Meta 推送是消息文本 /
      状态回执。

调用方只做平台专属的鉴权 / Content-Type / 签名顺序编排，体读取与解析
不再各自持有副本（单一真源；新增豁免 POST 端点必须接入本模块，结构守卫
见 ``tests/unit/test_inbound_body_structure.py``）。
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import Request

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "QQ_MAX_BODY_BYTES",
    "SMS_FORWARDER_MAX_BODY_BYTES",
    "WHATSAPP_MAX_BODY_BYTES",
    "parse_json_object",
    "read_bounded_body",
    "read_bounded_json_object",
]

#: QQ webhook 请求体字节上限（8 MiB——容纳合法 base64 段，见模块 docstring）。
QQ_MAX_BODY_BYTES: Final[int] = 8 * 1024 * 1024

#: SmsForwarder webhook 请求体字节上限（短文本转发载荷——64 KiB）。
SMS_FORWARDER_MAX_BODY_BYTES: Final[int] = 64 * 1024

#: WhatsApp webhook 请求体字节上限（消息文本 / 状态回执——64 KiB）。
WHATSAPP_MAX_BODY_BYTES: Final[int] = 64 * 1024


def _declared_length_exceeds(declared_length: str, limit: int) -> bool:
    """判定声明的 Content-Length 是否超过上限（缺失 / 非数值视为不超限）。

    Args:
        declared_length: 原始 ``Content-Length`` 头值。
        limit: 字节上限。

    Returns:
        True = 声明值可解析且超过上限（先于实读拒绝——避免读大包）。
    """
    value = declared_length.strip()
    return value.isdigit() and int(value) > limit


def _oversize_error(channel_label: str, limit: int) -> BusinessException:
    """构造统一的超限受控错误（``BAD_REQUEST`` → 400）。

    Args:
        channel_label: 通道中文 / 英文标识（错误文案定位面）。
        limit: 字节上限。

    Returns:
        超限业务异常（调用方直接抛出）。
    """
    return BusinessException(
        code=ErrorCode.BAD_REQUEST,
        message=f"{channel_label} webhook 请求体超过上限：{limit} 字节",
    )


async def read_bounded_body(request: Request, *, limit: int, channel_label: str) -> bytes:
    """按通道上限读取原始请求体（chunked 逐块累计，超限即停）。

    Args:
        request: 原始 ASGI 请求（请求体消费面）。
        limit: 允许的最大字节数（调用方传通道常量）。
        channel_label: 通道标识（错误文案定位面）。

    Returns:
        原始请求体字节（长度 ≤ ``limit``；去除首尾空白后非空）。

    Raises:
        BusinessException: 声明 / 实际超过上限、读取失败或空体
            （``BAD_REQUEST`` → 400）。
    """
    if _declared_length_exceeds(request.headers.get("content-length", ""), limit):
        raise _oversize_error(channel_label, limit)
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise _oversize_error(channel_label, limit)
            if chunk:
                chunks.append(chunk)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=f"{channel_label} webhook 请求体读取失败",
        ) from exc
    raw_body = b"".join(chunks)
    if not raw_body.strip():
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=f"{channel_label} webhook 请求体为空",
        )
    return raw_body


def parse_json_object(
    raw_body: bytes,
    *,
    channel_label: str,
    object_label: str,
) -> dict[str, object]:
    """解析原始请求体为 JSON 对象（非法 / 非对象 / 深嵌套受控 400）。

    Args:
        raw_body: 已通过上限校验的原始请求体。
        channel_label: 通道标识（错误文案定位面）。
        object_label: 该通道期望的对象形态说明（如「OneBot 事件形态」）。

    Returns:
        解析后的 JSON 对象（字段合法性交给各通道容错解析）。

    Raises:
        BusinessException: 非法 JSON / 非 JSON 对象 / 超深嵌套
            （``BAD_REQUEST`` → 400）。
    """
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=f"{channel_label} webhook 请求体不是合法 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=f"{channel_label} webhook 请求体须为 JSON 对象（{object_label}）",
        )
    return payload


async def read_bounded_json_object(
    request: Request,
    *,
    limit: int,
    channel_label: str,
    object_label: str,
) -> dict[str, object]:
    """读取有界请求体并解析为 JSON 对象（读取 + 解析一步完成）。

    Args:
        request: 原始 ASGI 请求（请求体消费面）。
        limit: 允许的最大字节数（调用方传通道常量）。
        channel_label: 通道标识（错误文案定位面）。
        object_label: 该通道期望的对象形态说明。

    Returns:
        解析后的 JSON 对象。

    Raises:
        BusinessException: 超限 / 读取失败 / 空体 / 非法 JSON / 非对象
            （``BAD_REQUEST`` → 400）。
    """
    raw_body = await read_bounded_body(
        request, limit=limit, channel_label=channel_label
    )
    return parse_json_object(
        raw_body, channel_label=channel_label, object_label=object_label
    )
