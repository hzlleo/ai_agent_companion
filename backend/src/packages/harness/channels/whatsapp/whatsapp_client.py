"""WhatsApp Cloud API 出站客户端——Graph API ``/messages`` 文本发送（共享 httpx）。

锁定设计决策：**不引入 Meta SDK**——按 Cloud API 协议手写；HTTP 出站一律
经注入的共享 ``httpx.AsyncClient``（pools.PoolManager 下发——硬规则：无私建
连接；测试注入 ``httpx.MockTransport`` 客户端，零真实网络）。

调用形态：``POST {api_base}/{api_version}/{phone_number_id}/messages``，
请求体为文本消息形态（``messaging_product=whatsapp`` / ``to=<wa_id>`` /
``type=text`` / ``text.body``），鉴权头 ``Authorization: Bearer
<access_token>``。响应成功形态的 ``messages[0].id`` 即平台消息 ID（wamid）。

错误码映射表（channels 域段——复用既有码表，无新增）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    其余 HTTP 非 2xx               CHANNEL_API_ERROR          由调用方隔离
    HTTP 401 / 403（token 无效）   CHANNEL_TOKEN_MISSING      否
    httpx 传输异常（连接 / 超时）  CHANNEL_API_ERROR          由调用方隔离
    响应 JSON 非法                 CHANNEL_API_ERROR          否
    空文本 / 空目标（发送面）       VALIDATION_ERROR           否（调用方 bug）
    ``phone_number_id`` 未配置     CONFIG_INVALID             否（部署配置）
    ``access_token`` 未配置        CHANNEL_TOKEN_MISSING      否
    ============================  ========================  ==========

超长文本不分段：Cloud API 文本上限（4096 字符）由平台侧强制，超限以
``CHANNEL_API_ERROR`` 逐消息隔离（不阻断入站消费——与 Telegram 分段策略
的差异点，文档化于 channels.md）。
"""

from __future__ import annotations

from typing import Final

import httpx
from loguru import logger
from pydantic import ValidationError

from packages.harness.channels.whatsapp.whatsapp_models import (
    WhatsAppSendRequestModel,
    WhatsAppSendResponseModel,
    WhatsAppTextModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["WHATSAPP_API_TIMEOUT_SECONDS", "WhatsAppCloudClient"]

#: 单次 Graph API 请求超时秒数（代码常量——本通道配置面只保留凭据 / 端点）。
WHATSAPP_API_TIMEOUT_SECONDS: Final[float] = 15.0

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class WhatsAppCloudClient:
    """WhatsApp Cloud API 客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；本类
    零连接资源、零后台任务。凭据由装配方从 Settings（SecretStr）解析后传入
    ——本类不读环境变量、不落日志。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_base: str = "https://graph.facebook.com",
        api_version: str = "v21.0",
        phone_number_id: str = "",
        access_token: str = "",
        request_timeout_seconds: float = WHATSAPP_API_TIMEOUT_SECONDS,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            api_base: Graph API 基础 URL（尾斜杠容错）。
            api_version: Graph API 版本段（如 ``v21.0``）。
            phone_number_id: 企业号码 ID（出站路径段——空值在发送时拒绝）。
            access_token: 永久访问令牌（Bearer 头——空值在发送时拒绝）。
            request_timeout_seconds: 单次请求超时秒数。
        """
        self._http = http_client
        self._api_base = api_base.rstrip("/")
        self._api_version = api_version.strip("/")
        self._phone_number_id = phone_number_id.strip()
        self._access_token = access_token.strip()
        self._timeout = httpx.Timeout(request_timeout_seconds)

    @property
    def has_access_token(self) -> bool:
        """是否持有访问令牌（False = 发送时以 ``CHANNEL_TOKEN_MISSING`` 拒绝）。"""
        return bool(self._access_token)

    @property
    def http_client(self) -> httpx.AsyncClient:
        """底层共享 HTTP 客户端（装配观测面——请求记录器 / 测试注入用）。"""
        return self._http

    async def send_text(self, *, wa_id: str, text: str) -> str | None:
        """发送文本消息（Graph API ``/messages``）。

        Args:
            wa_id: 目标 WhatsApp 号码（归一后的纯数字形态）。
            text: 消息文本（非空——超长由平台侧拒绝）。

        Returns:
            平台消息 ID（响应 ``messages[0].id``）；响应未回传凭据时为 None。

        Raises:
            BusinessException: 空文本 / 空目标（``VALIDATION_ERROR``）；
                未配置号码 ID（``CONFIG_INVALID``）；未配置令牌 / 401 / 403
                （``CHANNEL_TOKEN_MISSING``）；传输 / 状态 / 解析失败
                （``CHANNEL_API_ERROR``）。
        """
        target = wa_id.strip()
        body_text = text.strip()
        if not target:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="WhatsApp 出站目标号码不能为空",
            )
        if not body_text:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="WhatsApp 出站文本不能为空",
                details={"wa_id": target},
            )
        if not self._access_token:
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message="WhatsApp 通道未配置访问令牌（WHATSAPP_ACCESS_TOKEN）",
                details={"wa_id": target},
            )
        if not self._phone_number_id:
            raise BusinessException(
                code=ErrorCode.CONFIG_INVALID,
                message="WhatsApp 通道未配置企业号码 ID（WHATSAPP_PHONE_NUMBER_ID）",
                details={"wa_id": target},
            )
        url = f"{self._api_base}/{self._api_version}/{self._phone_number_id}/messages"
        request = WhatsAppSendRequestModel(
            to=target, text=WhatsAppTextModel(body=body_text)
        )
        headers: dict[str, str] = {"Authorization": f"Bearer {self._access_token}"}
        try:
            response = await self._http.post(
                url,
                json=request.model_dump(),
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="WhatsApp Cloud API 传输失败",
                details={"wa_id": target, "error": str(exc)},
            ) from exc
        self._raise_for_status(response, wa_id=target)
        try:
            wire = WhatsAppSendResponseModel.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="WhatsApp Cloud API 响应不是合法 JSON",
                details={
                    "wa_id": target,
                    "status_code": response.status_code,
                    "parse_error": str(exc)[:200],
                },
            ) from exc
        for message in wire.messages:
            provider_id = message.id.strip()
            if provider_id:
                logger.debug(
                    "WhatsApp 文本消息已发送: wa_id={}, message_id={}",
                    target,
                    provider_id,
                )
                return provider_id
        logger.warning(
            "WhatsApp 发送成功但响应未回传消息 ID: wa_id={}, status_code={}",
            target,
            response.status_code,
        )
        return None

    def _raise_for_status(self, response: httpx.Response, *, wa_id: str) -> None:
        """HTTP 状态检查（401 / 403 → 令牌无效；其余非 2xx → API 错误）。

        Args:
            response: HTTP 响应。
            wa_id: 目标号码（错误 details 携带）。

        Raises:
            BusinessException: 见本方法 docstring 主体。
        """
        if response.status_code in (401, 403):
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message="WhatsApp 访问令牌无效或与 Meta 应用配置不一致",
                details={
                    "wa_id": wa_id,
                    "status_code": response.status_code,
                    "body_snippet": response.text[:_BODY_SNIPPET_LIMIT],
                },
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"WhatsApp Cloud API 调用失败：返回非成功状态 {response.status_code}",
                details={
                    "wa_id": wa_id,
                    "status_code": response.status_code,
                    "body_snippet": response.text[:_BODY_SNIPPET_LIMIT],
                },
            )
