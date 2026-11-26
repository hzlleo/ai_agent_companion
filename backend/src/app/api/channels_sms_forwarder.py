"""SmsForwarder 通道 webhook API——手机端短信 / 通知转发的接收面。

    - POST /api/v1/channels/sms-forwarder —— 手机端 SmsForwarder 应用配置的
      HTTP POST 目标；接收自定义模板 JSON，委托 ``sms_forwarder`` 处理器
      路由到通道路由器。

契约说明：

    - **恒 200 确认语义**：手机端转发失败会重投——忽略类事件（通知关闭 /
      空正文 / 未知类型）与处理失败（处理器逐事件隔离，恒不抛）均返回
      ``200 {"status": "ok"}`` 确认防重投风暴；错误细节只进日志。仅三类
      请求形态错误走 4xx：鉴权失败（401 / 403）、非 ``application/json``
      （400）、请求体非法 / 超限（400）。
    - **入站鉴权**：``SMS_FORWARDER_TOKEN`` 非空时开启——要求
      ``Authorization: Bearer <token>``、``X-SmsForwarder-Token`` /
      ``X-Sms-Forwarder-Token`` 请求头或 ``?token=`` 查询参数携带同一值，
      **恒时比较**（``secrets.compare_digest``——防时序侧信道）。缺失 →
      401（``UNAUTHORIZED``）；值不符 → 403（``FORBIDDEN``）。鉴权在请求
      体解析与去重登记之前完成——被拒请求不消耗去重窗口。变量为空 =
      鉴权关闭（仅限同机 / 可信内网部署形态；``ENV=prod`` 且通道启用时
      启动期 fail-closed，见
      :func:`packages.harness.config.auth_policy.ensure_auth_policy`）。
    - **请求体校验**：仅接受 ``application/json``；非该 Content-Type → 400。
      体读取经共享有界读取器（:mod:`app.api.inbound_body`）——上限
      :data:`app.api.inbound_body.SMS_FORWARDER_MAX_BODY_BYTES`（64 KiB；
      转发载荷是短文本）；声明 Content-Length 超限即 400（零读取），无
      Content-Length 的 chunked 请求逐块累计、超限即停。空体 / 非法 JSON /
      非 JSON 对象 / 极深嵌套（``RecursionError``）→ 400；对象内字段的
      合法性由处理器容错解析承担（隔离确认——不进 HTTP 错误面）。
    - **入站消纳（有界队列）**：通道启用时 lifespan 装配
      ``app.state.channels_sms_forwarder_runner``
      （``SmsForwarderWebhookRunner``）——端点只做鉴权 / 体校验 / 去重
      登记 / 入队后**立即确认**，LLM 回合耗时由后台固定 worker 承担；
      队列满丢弃并计数（端点恒 200，满队丢弃即最终丢失；不登记去重仅吸收
      偶发重复）。runner 未装配的嵌入形态
      回落单事件直调（``process_sms_forwarder_payload``）。
    - **路由器未装配**（本通道 / 全通道关闭形态）：确认并丢弃（warning
      日志——通道层是可选装配面，webhook 端点不因配置缺失而 5xx）。
    - **装配面**：``app.state.channels_sms_forwarder_runner`` /
      ``app.state.channels_router``（lifespan 装配）；Settings 取
      ``app.state.settings``（``create_app`` 上挂；未上挂形态回落全局
      单例——与 main.lifespan 同回退次序）。
"""

from __future__ import annotations

import secrets
from typing import Final, cast

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from app.api.inbound_body import (
    SMS_FORWARDER_MAX_BODY_BYTES,
    read_bounded_json_object,
)
from packages.harness.channels.sms_forwarder.sms_models import (
    parse_owner_numbers,
    process_sms_forwarder_payload,
)
from packages.harness.channels.sms_forwarder.sms_runner import SmsForwarderWebhookRunner
from packages.harness.config.auth_policy import resolve_sms_forwarder_token
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["SMS_FORWARDER_MAX_BODY_BYTES", "SmsForwarderAckDTO", "router"]

router = APIRouter(prefix="/api/v1/channels/sms-forwarder", tags=["channels-sms-forwarder"])

#: 手机端可配置的鉴权头候选（自定义请求头名称不统一——两种常见拼写均接受）。
_TOKEN_HEADERS: Final[tuple[str, ...]] = ("x-smsforwarder-token", "x-sms-forwarder-token")


class SmsForwarderAckDTO(BaseModel):
    """webhook 确认载荷（恒 200 形态——手机端重投防线）。

    Attributes:
        status: 确认状态（恒 ``ok``——收到即确认；处理结果不进响应面）。
    """

    status: str = Field(default="ok", description="确认状态（恒 ok）")


def _bearer_token(authorization: str) -> str | None:
    """解析 Authorization 头的 Bearer token。

    Args:
        authorization: 原始 Authorization 头值（可能缺失——空串入参）。

    Returns:
        token 值（已 strip）；头形态非 ``Bearer <token>``（scheme 不符 /
        token 为空）返回 None。
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    stripped = token.strip()
    return stripped if stripped else None


def _presented_token(request: Request) -> str | None:
    """读取请求携带的 SmsForwarder token（头 / 查询参数三形态）。

    Args:
        request: 原始 ASGI 请求（鉴权头 / 查询参数消费面）。

    Returns:
        首个非空 token；全部缺失返回 None。
    """
    bearer = _bearer_token(request.headers.get("authorization", ""))
    if bearer is not None:
        return bearer
    for header_name in _TOKEN_HEADERS:
        value = request.headers.get(header_name, "").strip()
        if value:
            return value
    query_token = request.query_params.get("token", "").strip()
    return query_token or None


def _require_sms_forwarder_auth(request: Request, expected: str) -> None:
    """入站鉴权（token 配置时恒时比较；缺失 401 / 不符 403）。

    Args:
        request: 原始 ASGI 请求（鉴权面消费）。
        expected: 期望 token（空 = 鉴权关闭）。

    Raises:
        BusinessException: token 已配置但请求未携带（``UNAUTHORIZED`` →
            401），或携带值不匹配（``FORBIDDEN`` → 403）。
    """
    if not expected:
        return
    presented = _presented_token(request)
    if presented is None:
        raise BusinessException(
            code=ErrorCode.UNAUTHORIZED,
            message="SmsForwarder webhook 鉴权失败：请求未携带 token",
        )
    if not secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise BusinessException(
            code=ErrorCode.FORBIDDEN,
            message="SmsForwarder webhook 鉴权失败：token 不匹配",
        )


def _require_json_content_type(request: Request) -> None:
    """校验请求 Content-Type（仅接受 application/json）。

    Args:
        request: 原始 ASGI 请求（Content-Type 头消费面）。

    Raises:
        BusinessException: Content-Type 缺失或不是 ``application/json``
            （``BAD_REQUEST`` → 400——手机端转发通道固定 JSON 模板）。
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="SmsForwarder webhook 仅接受 application/json 请求体",
        )



@router.post("", response_model=SmsForwarderAckDTO)
async def receive_sms_forwarder_webhook(request: Request) -> SmsForwarderAckDTO:
    """接收 SmsForwarder 转发载荷（鉴权 → 体校验 → 入队 → 立即确认）。

    Args:
        request: 原始 ASGI 请求（JSON 对象体——SmsForwarder 模板载荷）。

    Returns:
        恒 ``{"status": "ok"}`` 确认载荷（忽略 / 隔离失败亦确认——手机端
        对非 200 会重投）。

    Raises:
        BusinessException: 鉴权失败（``UNAUTHORIZED`` 401 /
            ``FORBIDDEN`` 403）；Content-Type 非 JSON 或请求体非法 /
            超限（``BAD_REQUEST`` 400）。
    """
    settings = cast("Settings | None", getattr(request.app.state, "settings", None)) or (
        global_settings
    )
    sms_forwarder = settings.channels.sms_forwarder
    _require_sms_forwarder_auth(request, resolve_sms_forwarder_token(settings))
    _require_json_content_type(request)
    payload = await read_bounded_json_object(
        request,
        limit=SMS_FORWARDER_MAX_BODY_BYTES,
        channel_label="SmsForwarder",
        object_label="转发模板形态",
    )
    runner: SmsForwarderWebhookRunner | None = getattr(
        request.app.state, "channels_sms_forwarder_runner", None
    )
    if runner is not None:
        runner.submit(payload)
        return SmsForwarderAckDTO()
    channel_router = getattr(request.app.state, "channels_router", None)
    if channel_router is None:
        logger.warning(
            "SmsForwarder webhook 收到转发但通道路由器未装配（SMS_FORWARDER_ENABLED=false?）"
            "——确认丢弃"
        )
        return SmsForwarderAckDTO()
    await process_sms_forwarder_payload(
        payload,
        channel_router,
        notifications_enabled=sms_forwarder.notifications_enabled,
        owner_numbers=parse_owner_numbers(sms_forwarder.owner_numbers),
    )
    return SmsForwarderAckDTO()
