"""SmsForwarder 适配器——仅入站通道的出站丢弃面。

SmsForwarder 的主用途是把手机短信 / 通知转发到本后端；它的主动控制 API
（若部署形态具备）只覆盖部分手机与版本，本通道锁定**仅入站**：适配器
``send_message`` 恒返回空投递结果（不发起任何平台调用、不抛异常）。人格回复
仍由路由器正常生成并写入会话 / 记忆，供后续回合引用；手机端由用户自行阅读
或回复。后续若接入主动控制 API，可在本类内扩展发送面并保持默认关闭。

适配器存在的必要性：通道中立路由器在入站处理前要求该通道已注册适配器
（未注册会按 ``CHANNEL_DISABLED`` 静默丢弃），因此仅入站通道也必须提供
一个发送面实现。
"""

from __future__ import annotations

from typing import Final

from loguru import logger

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)

__all__ = ["SMS_FORWARDER_OUTBOUND_DROPPED_LOG_LIMIT", "SmsForwarderAdapter"]

#: 出站丢弃日志采样上限（前 N 条逐条 debug；其后静默——避免长跑日志膨胀）。
SMS_FORWARDER_OUTBOUND_DROPPED_LOG_LIMIT: Final[int] = 20


class SmsForwarderAdapter:
    """SmsForwarder 适配器（仅入站——出站恒丢弃并返回空投递结果）。"""

    def __init__(self) -> None:
        """初始化适配器（无外部客户端——仅持有丢弃日志采样计数）。"""
        self._dropped_outbound = 0

    @property
    def dropped_outbound(self) -> int:
        """累计丢弃的出站条数（观测面——人格回复不入手机端）。"""
        return self._dropped_outbound

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """接收出站消息并丢弃（仅入站边界——不发起平台调用）。

        Args:
            message: 路由器产出的出站消息（文本 / 附件均丢弃）。

        Returns:
            空投递结果（``message_ids=[]``——平台未回传任何消息 ID）。
        """
        self._dropped_outbound += 1
        if self._dropped_outbound <= SMS_FORWARDER_OUTBOUND_DROPPED_LOG_LIMIT:
            logger.debug(
                "SmsForwarder 仅入站通道（出站丢弃）: chat_id={}, dropped_total={}",
                message.chat_id,
                self._dropped_outbound,
            )
        return ChannelDeliveryResultModel(
            channel=ChannelType.SMS_FORWARDER,
            chat_id=message.chat_id,
            message_ids=[],
        )
