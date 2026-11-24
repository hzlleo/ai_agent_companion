"""WhatsApp 通道适配器——文本出站 + 24 小时客户服务窗判定（发送面）。

通道路由器把人格回复交给本适配器；适配器在发起 Graph API 调用前先判定
**24 小时客户服务窗**：Cloud API 只允许在用户最后一条消息后的 24 小时内
发送自由格式消息，超窗必须使用模板消息（本通道暂未实现模板发送——超窗
以受控业务错误拒绝并文档化）。

chat_id grammar（锁定决策）：

    ``wa:<WA_ID>`` —— WA_ID 为归一后的纯数字国际号码（与入站会话键同源）；
    非法 grammar（缺前缀 / 目标为空）按调用方 bug 面拒绝
    （``VALIDATION_ERROR``）。

出站形态边界：

    - **仅文本**：``text.body`` 整条投递（超长由平台侧拒绝——本通道不做
      4096 分段，与 Telegram 分段策略的差异点）；
    - **附件不投递**：``OutboundMessageModel.attachments`` 被优雅忽略（表情
      桥附发的贴纸不会打断文本回复——逐附件结果不回填，默认空列表）；
    - **引用回复不透传**：``reply_to_message_id`` 未映射为 Cloud API
      ``context`` 字段（通道中立出站按纯文本语义消费）。

失败语义：窗口外 / 平台失败（``CHANNEL_API_ERROR`` / ``CHANNEL_TOKEN_MISSING``
/ ``CONFIG_INVALID``）按适配器协议**上抛**——通道路由器统一兜底隔离（单条
出站失败不击穿入站消费；入站 worker 与适配器发送面互不反噬）。
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.phone_numbers import normalize_phone_identifier
from packages.harness.channels.whatsapp.whatsapp_client import WhatsAppCloudClient
from packages.harness.channels.whatsapp.whatsapp_models import WhatsAppServiceWindow
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["WHATSAPP_CHAT_ID_PREFIX", "WhatsAppAdapter"]

#: chat_id grammar 前缀（``wa:<wa_id>``——与入站会话键同源）。
WHATSAPP_CHAT_ID_PREFIX: str = "wa"


class WhatsAppAdapter:
    """WhatsApp 通道适配器（Graph API 文本发送 + 24 小时服务窗闸）。

    实现 :class:`packages.harness.channels.channel_service.ChannelAdapterProtocol`
    的 ``send_message`` 面（结构化类型——无需显式继承）；出站经注入的
    :class:`~packages.harness.channels.whatsapp.whatsapp_client.WhatsAppCloudClient`
    （共享 httpx client 承载——本类零连接资源、零长驻任务）。
    """

    def __init__(
        self,
        *,
        client: WhatsAppCloudClient,
        service_window: WhatsAppServiceWindow,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化适配器。

        Args:
            client: Graph API 客户端（lifespan 装配——共享池注入）。
            service_window: 24 小时服务窗状态（与入站 runner 共享同一实例；
                记录每个会话最近入站毫秒）。
            clock: 时钟（epoch 毫秒——窗口判定；测试注入伪时钟）。
        """
        self._client = client
        self._service_window = service_window
        self._clock = clock

    @property
    def client(self) -> WhatsAppCloudClient:
        """底层客户端（装配观测面——启动校验 / 测试注入用）。"""
        return self._client

    @property
    def service_window(self) -> WhatsAppServiceWindow:
        """服务窗状态（与入站 runner 共享——观测 / 测试面）。"""
        return self._service_window

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站文本消息（grammar 解析 → 服务窗判定 → Graph API）。

        Args:
            message: 出站消息（通道中立形态；``chat_id`` 须为
                ``wa:<wa_id>`` grammar；附件被忽略）。

        Returns:
            投递结果（平台消息 ID 归一字符串形态；未回传凭据时
            ``message_ids`` 为空列表）。

        Raises:
            BusinessException: chat_id grammar 非法（``VALIDATION_ERROR``）；
                超出 24 小时客户服务窗（``CHANNEL_API_ERROR``——受控业务
                错误，模板消息未实现）；平台失败（``CHANNEL_API_ERROR`` /
                ``CHANNEL_TOKEN_MISSING`` / ``CONFIG_INVALID``）。
        """
        wa_id = self._parse_chat_id(message.chat_id)
        now_ms = self._clock()
        if not self._service_window.is_within_window(wa_id, now_ms):
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=(
                    "WhatsApp 出站超出 24 小时客户服务窗"
                    "（自由格式消息不可用；模板消息尚未实现）"
                ),
                details={
                    "wa_id": wa_id,
                    "last_inbound_ms": self._service_window.last_inbound_ms(wa_id),
                    "now_ms": now_ms,
                    "window_ms": self._service_window.window_ms,
                },
            )
        if message.attachments:
            logger.debug(
                "WhatsApp 出站仅文本（附件忽略）: chat_id={}, attachments={}",
                message.chat_id,
                len(message.attachments),
            )
        provider_id = await self._client.send_text(wa_id=wa_id, text=message.text)
        return ChannelDeliveryResultModel(
            channel=ChannelType.WHATSAPP,
            chat_id=message.chat_id,
            message_ids=[provider_id] if provider_id is not None else [],
        )

    @staticmethod
    def _parse_chat_id(chat_id: str) -> str:
        """解析并归一 ``wa:<wa_id>`` grammar。

        Args:
            chat_id: 出站目标会话键。

        Returns:
            归一后的纯数字 wa_id。

        Raises:
            BusinessException: 前缀不符 / 目标为空（``VALIDATION_ERROR``
                ——调用方 bug 面）。
        """
        prefix, separator, raw_target = chat_id.partition(":")
        if prefix != WHATSAPP_CHAT_ID_PREFIX or not separator:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"WhatsApp chat_id 须为 wa:<wa_id> 形态: {chat_id!r}",
                details={"chat_id": chat_id},
            )
        target = normalize_phone_identifier(raw_target)
        if not target:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"WhatsApp chat_id 目标号码为空: {chat_id!r}",
                details={"chat_id": chat_id},
            )
        return target
