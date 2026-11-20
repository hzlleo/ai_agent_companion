"""QQ 通道 webhook API——NapCat OneBot v11 事件上报接收面。

    - POST /api/v1/channels/qq/webhook —— NapCat（部署机）配置的 HTTP POST
      事件上报目标；接收 OneBot 事件 JSON，委托 ``qq_webhook`` 处理器
      路由到通道路由器。

契约说明：

    - **恒 200 确认语义**：OneBot 上报方对非 200 响应会**重试**——忽略类
      事件（notice / request / meta_event）与处理失败（处理器逐事件隔离，
      恒不抛）均返回 ``200 {"status": "ok"}`` 确认防重投风暴；错误细节
      只进日志（与 tg runner「offset 照常推进」的确认语义同哲学）。
    - **入站鉴权（可选）**：``settings.channels.qq.webhook_token_env``
      指向的环境变量非空时开启——要求 ``Authorization: Bearer <token>``
      头，**恒时比较**（``secrets.compare_digest``——防时序侧信道）；
      不匹配 / 缺失 → 401（``UNAUTHORIZED``——ResultDTO error 形态，
      经共享异常处理器）。变量为空 = 鉴权关闭（仅限 NapCat 与后端同机 /
      可信内网部署形态——公网暴露必须配置，见 QqChannelSettings 类
      docstring 取舍）。token 经 env 间接（永不落配置树）。
    - **请求体校验**：仅要求 JSON **对象**形态（OneBot 事件面）；非对象
      （数组 / 字面量 / 非法 JSON）→ 400（``BAD_REQUEST``）；极深嵌套
      JSON 的 ``RecursionError`` 同归 400（解析器递归上限面——恒 200/4xx
      契约不破）。对象内字段的合法性由 webhook 处理器容错解析承担
      （隔离确认——不进 HTTP 错误面）。
    - **入站消纳（有界队列）**：QQ 启用时 lifespan 装配
      ``app.state.channels_qq_runner``（``QqWebhookRunner``）——端点只做
      鉴权 / 体校验 / 去重登记 / 入队后**立即确认**，LLM 回合耗时由后台
      固定 worker 承担；队列满丢弃并计数（平台重投可重新消费）。runner
      未装配的嵌入形态回落单事件直调（``process_webhook_event``）。
    - **路由器未装配**（QQ / 全通道关闭形态）：确认并丢弃（warning 日志
      ——通道层是可选装配面，webhook 端点不因配置缺失而 5xx）。
    - **装配面**：``app.state.channels_qq_runner`` /
      ``app.state.channels_router``（lifespan 装配——与 duty 出站投递面
      同槽位复用）；Settings 取 ``app.state.settings``（``create_app``
      上挂；未上挂形态回落全局单例——与 main.lifespan 同回退次序）。
"""

from __future__ import annotations

import os
import secrets
from typing import cast

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from packages.harness.channels.qq.qq_runner import QqWebhookRunner
from packages.harness.channels.qq.qq_webhook import process_webhook_event
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["QqWebhookAckDTO", "router"]

router = APIRouter(prefix="/api/v1/channels/qq", tags=["channels-qq"])


class QqWebhookAckDTO(BaseModel):
    """webhook 确认载荷（恒 200 形态——上报方重投防线）。

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


def _require_webhook_auth(request: Request, qq_webhook_token_env: str) -> None:
    """入站鉴权（token 配置时恒时比较 Bearer；不匹配 → 401）。

    Args:
        request: 原始 ASGI 请求（Authorization 头消费面）。
        qq_webhook_token_env: 入站校验 token 环境变量名（实际 token 从
            该变量读取；空 = 鉴权关闭）。

    Raises:
        BusinessException: token 已配置但请求头缺失 / scheme 不符 /
            值不匹配（``UNAUTHORIZED`` → 401）。
    """
    expected = os.environ.get(qq_webhook_token_env, "").strip()
    if not expected:
        return
    presented = _bearer_token(request.headers.get("authorization", ""))
    if presented is None or not secrets.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise BusinessException(
            code=ErrorCode.UNAUTHORIZED,
            message="QQ webhook 鉴权失败：Authorization Bearer token 缺失或不匹配",
            details={"webhook_token_env": qq_webhook_token_env},
        )


@router.post("/webhook", response_model=QqWebhookAckDTO)
async def receive_qq_webhook(request: Request) -> QqWebhookAckDTO:
    """接收 OneBot v11 事件上报（鉴权 → 体校验 → 入队 → 立即确认）。

    Args:
        request: 原始 ASGI 请求（JSON 对象体——OneBot 事件载荷）。

    Returns:
        恒 ``{"status": "ok"}`` 确认载荷（忽略 / 隔离失败亦确认——上报方
        对非 200 会重试）。

    Raises:
        BusinessException: 鉴权失败（``UNAUTHORIZED`` → 401）；请求体非
            JSON 对象（``BAD_REQUEST`` → 400）。
    """
    settings = cast("Settings | None", getattr(request.app.state, "settings", None)) or (
        global_settings
    )
    qq = settings.channels.qq
    _require_webhook_auth(request, qq.webhook_token_env)
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="QQ webhook 请求体不是合法 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="QQ webhook 请求体须为 JSON 对象（OneBot 事件形态）",
        )
    runner: QqWebhookRunner | None = getattr(request.app.state, "channels_qq_runner", None)
    if runner is not None:
        runner.submit(payload)
        return QqWebhookAckDTO()
    channel_router = getattr(request.app.state, "channels_router", None)
    if channel_router is None:
        logger.warning("QQ webhook 收到事件但通道路由器未装配（QQ_ENABLED=false?）——确认丢弃")
    else:
        await process_webhook_event(payload, channel_router)
    return QqWebhookAckDTO()
