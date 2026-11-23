"""SmsForwarder 适配器测试——仅入站出站丢弃边界。"""

from __future__ import annotations

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.sms_forwarder.sms_adapter import SmsForwarderAdapter


async def test_outbound_is_dropped_with_empty_delivery_result() -> None:
    """出站恒丢弃：空 message_ids、不抛异常、累计丢弃计数。"""
    adapter = SmsForwarderAdapter()
    result = await adapter.send_message(
        OutboundMessageModel(chat_id="sms:13800138000", text="人格回复")
    )
    assert result.channel is ChannelType.SMS_FORWARDER
    assert result.chat_id == "sms:13800138000"
    assert result.message_ids == []
    assert adapter.dropped_outbound == 1


async def test_outbound_with_attachments_still_dropped() -> None:
    """附件同样丢弃（仅入站边界——不对平台发起任何请求）。"""
    adapter = SmsForwarderAdapter()
    result = await adapter.send_message(
        OutboundMessageModel(
            chat_id="sms:unknown",
            text="带附件回复",
            attachments=[
                ChannelMessageAttachment(kind=ChannelAttachmentKind.IMAGE, url="https://x/y.png")
            ],
        )
    )
    assert result.message_ids == []
    assert adapter.dropped_outbound == 1
