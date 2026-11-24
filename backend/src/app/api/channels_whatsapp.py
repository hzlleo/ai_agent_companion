"""WhatsApp Cloud API webhook API——Meta 回调接收面（hub 校验 + 签名校验）。

    - GET /api/v1/channels/whatsapp —— Meta 应用配置 webhook 时的 hub 校验
      握手：``hub.mode=subscribe`` 且 ``hub.verify_token`` 与
      ``WHATSAPP_VERIFY_TOKEN`` 恒时比较，通过则 200 纯文本回显
      ``hub.challenge``；不匹配 → 403。
    - POST /api/v1/channels/whatsapp —— 消息 / 状态回执推送入口：先校验
      ``X-Hub-Signature-256: sha256=<hex>``（HMAC-SHA256(raw body,
      ``WHATSAPP_APP_SECRET``)），缺失 / 不符 → 403 **先于** JSON 解析与
      去重登记；随后仅接受 ``application/json``、64 KiB 体上限
      （Content-Length 先检、实读复核），最后委托 runner 入队并立即 200。

契约说明：

    - **恒 200 确认语义**：Meta 对非 2xx 响应会重投——状态回执 / 不可路由
      消息 / 队列满丢弃 / 处理失败均返回 ``200 {"status": "ok"}`` 确认；
      错误细节只进日志。仅请求形态错误走 4xx（403 签名 / hub 校验失败，
      400 Content-Type 或体非法 / 超限）。
    - **hub 校验**：``mode`` 必须为 ``subscribe``，``challenge`` 必须非空；
      ``WHATSAPP_VERIFY_TOKEN`` 为空 = 校验关闭（仅开发 / 测试形态——
      ``ENV=prod`` 且通道启用时启动期强制非空，见
      :func:`packages.harness.config.auth_policy.ensure_auth_policy`）。
      恒时比较使用 ``secrets.compare_digest``（防时序侧信道）。
    - **签名校验**：``WHATSAPP_APP_SECRET`` 为空 = 签名校验关闭（同上
      fail-closed 边界）。签名在请求体解析与去重之前完成——被拒请求不
      消耗去重窗口；``X-Hub-Signature-256`` 必须带 ``sha256=`` 前缀。
    - **请求体校验**：仅接受 ``application/json``；体大小上限
      :data:`WHATSAPP_MAX_BODY_BYTES`（webhook 载荷是消息文本 / 状态回执，
      64 KiB 已远超正常推送）——声明或实际超限 → 400；非法 JSON / 非 JSON
      对象 / 深层嵌套（``RecursionError``）→ 400。对象内字段合法性由
      ``whatsapp_models`` 容错解析承担（隔离确认——不进 HTTP 错误面）。
    - **入站消纳（有界队列）**：通道启用时 lifespan 装配
      ``app.state.channels_whatsapp_runner``（``WhatsAppWebhookRunner``）——
      端点只做校验 / 去重登记 / 入队后**立即确认**，LLM 回合耗时由后台
      固定 worker 承担；队列满丢弃并计数（Meta 重投可重新消费）。runner
      未装配的嵌入形态回落单事件直调
      （``process_whatsapp_webhook_payload``）。
    - **路由器未装配**（本通道 / 全通道关闭形态）：确认并丢弃（warning
      日志——通道层是可选装配面，webhook 端点不因配置缺失而 5xx）。
    - **装配面**：``app.state.channels_whatsapp_runner`` /
      ``app.state.channels_router``（lifespan 装配）；Settings 取
      ``app.state.settings``（``create_app`` 上挂；未上挂形态回落全局
      单例——与 main.lifespan 同回退次序）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Final, cast

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from loguru import logger
from pydantic import BaseModel, Field

from packages.harness.channels.phone_numbers import parse_phone_owner_numbers
from packages.harness.channels.whatsapp.whatsapp_models import (
    process_whatsapp_webhook_payload,
)
from packages.harness.channels.whatsapp.whatsapp_runner import WhatsAppWebhookRunner
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WHATSAPP_MAX_BODY_BYTES",
    "WhatsAppAckDTO",
    "router",
    "verify_whatsapp_hub_challenge",
    "verify_whatsapp_signature",
]

router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["channels-whatsapp"])

#: 请求体字节上限（消息文本 / 状态回执的 webhook 载荷——64 KiB 已远超正常推送）。
WHATSAPP_MAX_BODY_BYTES: Final[int] = 64 * 1024

#: Meta 签名头名称（Cloud API 固定形态 ``sha256=<hex>``）。
_SIGNATURE_HEADER: Final[str] = "x-hub-signature-256"

#: hub 校验模式字面量（Meta 应用配置 webhook 时的固定值）。
_HUB_SUBSCRIBE_MODE: Final[str] = "subscribe"


class WhatsAppAckDTO(BaseModel):
    """webhook 确认载荷（恒 200 形态——Meta 重投防线）。

    Attributes:
        status: 确认状态（恒 ``ok``——收到即确认；处理结果不进响应面）。
    """

    status: str = Field(default="ok", description="确认状态（恒 ok）")


def _resolve_settings(request: Request) -> Settings:
    """解析生效 Settings（``app.state`` 注入面优先，回落全局单例）。

    Args:
        request: 原始 ASGI 请求。

    Returns:
        生效 Settings（与 main.lifespan 同回退次序）。
    """
    return cast("Settings | None", getattr(request.app.state, "settings", None)) or (
        global_settings
    )


def verify_whatsapp_hub_challenge(
    *,
    mode: str,
    presented_token: str | None,
    challenge: str,
    expected_token: str,
) -> str:
    """校验 Meta hub 握手参数（恒时比较——失败 403）。

    Args:
        mode: ``hub.mode`` 查询参数（Meta 握手为 ``subscribe``）。
        presented_token: ``hub.verify_token`` 查询参数（None = 未携带）。
        challenge: ``hub.challenge`` 查询参数（通过后原样回显）。
        expected_token: ``WHATSAPP_VERIFY_TOKEN`` 本值（空 = 校验关闭）。

    Returns:
        通过校验的 challenge 文本（调用方以纯文本 200 回显）。

    Raises:
        BusinessException: 模式不符 / challenge 缺失 / token 不匹配
            （``FORBIDDEN`` → 403）。
    """
    if mode.strip().lower() != _HUB_SUBSCRIBE_MODE:
        raise BusinessException(
            code=ErrorCode.FORBIDDEN,
            message="WhatsApp webhook hub 校验失败：hub.mode 必须为 subscribe",
            details={"hub_mode": mode},
        )
    if not challenge:
        raise BusinessException(
            code=ErrorCode.FORBIDDEN,
            message="WhatsApp webhook hub 校验失败：缺少 hub.challenge",
        )
    if expected_token and (
        presented_token is None
        or not secrets.compare_digest(
            presented_token.encode("utf-8"), expected_token.encode("utf-8")
        )
    ):
        raise BusinessException(
            code=ErrorCode.FORBIDDEN,
            message="WhatsApp webhook hub 校验失败：hub.verify_token 不匹配",
        )
    return challenge


def verify_whatsapp_signature(
    raw_body: bytes, signature_header: str, app_secret: str
) -> bool:
    """校验 ``X-Hub-Signature-256``（HMAC-SHA256 + 恒时比较）。

    Args:
        raw_body: 原始请求体字节（未解析——签名对象是原始字节）。
        signature_header: 请求头值（``sha256=<hex>``；缺失为空串）。
        app_secret: ``WHATSAPP_APP_SECRET`` 本值（空 = 校验关闭）。

    Returns:
        True = 通过（或校验关闭）；False = 头缺失 / 形态不符 / HMAC 不匹配。
    """
    if not app_secret:
        return True
    presented = signature_header.strip()
    if not presented:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


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


def _require_json_content_type(request: Request) -> None:
    """校验请求 Content-Type（仅接受 application/json）。

    Args:
        request: 原始 ASGI 请求（Content-Type 头消费面）。

    Raises:
        BusinessException: Content-Type 缺失或不是 ``application/json``
            （``BAD_REQUEST`` → 400）。
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="WhatsApp webhook 仅接受 application/json 请求体",
        )


async def _read_raw_body(request: Request) -> bytes:
    """读取请求体并执行实读上限校验（Content-Length 缺失时同样兜底）。

    Args:
        request: 原始 ASGI 请求（请求体消费面）。

    Returns:
        原始请求体字节。

    Raises:
        BusinessException: 读取失败 / 空体 / 超过
            :data:`WHATSAPP_MAX_BODY_BYTES`（``BAD_REQUEST`` → 400）。
    """
    try:
        raw_body = await request.body()
    except (ValueError, UnicodeDecodeError) as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="WhatsApp webhook 请求体读取失败",
        ) from exc
    if not raw_body.strip():
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="WhatsApp webhook 请求体为空",
        )
    if len(raw_body) > WHATSAPP_MAX_BODY_BYTES:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=(
                "WhatsApp webhook 请求体超过上限："
                f"{WHATSAPP_MAX_BODY_BYTES} 字节"
            ),
        )
    return raw_body


def _parse_json_object(raw_body: bytes) -> dict[str, object]:
    """解析请求体为 JSON 对象（非法 / 非对象 / 深嵌套受控 400）。

    Args:
        raw_body: 已通过上限校验的原始请求体。

    Returns:
        解析后的 JSON 对象（字段合法性交给 ``whatsapp_models`` 容错解析）。

    Raises:
        BusinessException: 非法 JSON / 非 JSON 对象 / 超深嵌套
            （``BAD_REQUEST`` → 400）。
    """
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="WhatsApp webhook 请求体不是合法 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="WhatsApp webhook 请求体须为 JSON 对象（Cloud API 推送形态）",
        )
    return payload


@router.get("", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    """Meta webhook hub 校验握手（通过回显 challenge；不匹配 403）。

    Args:
        request: 原始 ASGI 请求（查询参数消费面）。

    Returns:
        200 纯文本 challenge（Meta 侧据此完成 webhook 配置）。

    Raises:
        BusinessException: 模式不符 / challenge 缺失 / token 不匹配
            （``FORBIDDEN`` → 403）。
    """
    settings = _resolve_settings(request)
    whatsapp = settings.channels.whatsapp
    challenge = verify_whatsapp_hub_challenge(
        mode=request.query_params.get("hub.mode", ""),
        presented_token=request.query_params.get("hub.verify_token"),
        challenge=request.query_params.get("hub.challenge", ""),
        expected_token=whatsapp.verify_token.get_secret_value().strip(),
    )
    return PlainTextResponse(challenge)


@router.post("", response_model=WhatsAppAckDTO)
async def receive_whatsapp_webhook(request: Request) -> WhatsAppAckDTO:
    """接收 Meta 推送（签名 → 体校验 → 入队 → 立即确认）。

    Args:
        request: 原始 ASGI 请求（JSON 对象体——Cloud API 推送载荷）。

    Returns:
        恒 ``{"status": "ok"}`` 确认载荷（状态回执 / 隔离失败亦确认——Meta
        对非 200 会重投）。

    Raises:
        BusinessException: 签名缺失 / 不符（``FORBIDDEN`` 403）；Content-Type
            非 JSON 或请求体非法 / 超限（``BAD_REQUEST`` 400）。
    """
    settings = _resolve_settings(request)
    whatsapp = settings.channels.whatsapp
    _require_json_content_type(request)
    if _declared_length_exceeds(
        request.headers.get("content-length", ""), WHATSAPP_MAX_BODY_BYTES
    ):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message=(
                "WhatsApp webhook 请求体超过上限："
                f"{WHATSAPP_MAX_BODY_BYTES} 字节"
            ),
        )
    raw_body = await _read_raw_body(request)
    if not verify_whatsapp_signature(
        raw_body,
        request.headers.get(_SIGNATURE_HEADER, ""),
        whatsapp.app_secret.get_secret_value().strip(),
    ):
        raise BusinessException(
            code=ErrorCode.FORBIDDEN,
            message="WhatsApp webhook 签名校验失败：X-Hub-Signature-256 缺失或不匹配",
        )
    payload = _parse_json_object(raw_body)
    runner: WhatsAppWebhookRunner | None = getattr(
        request.app.state, "channels_whatsapp_runner", None
    )
    if runner is not None:
        runner.submit(payload)
        return WhatsAppAckDTO()
    channel_router = getattr(request.app.state, "channels_router", None)
    if channel_router is None:
        logger.warning(
            "WhatsApp webhook 收到推送但通道路由器未装配（WHATSAPP_ENABLED=false?）"
            "——确认丢弃"
        )
        return WhatsAppAckDTO()
    await process_whatsapp_webhook_payload(
        payload,
        channel_router,
        owner_numbers=parse_phone_owner_numbers(whatsapp.owner_numbers),
    )
    return WhatsAppAckDTO()
