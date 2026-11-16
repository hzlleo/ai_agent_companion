"""channels 域 Model Classes 测试——值域 / 校验 / 默认值 / 身份键。

零真实配置 / 零真实网络：纯模型构造断言。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.channels.channel_models import (
    ChannelContextModel,
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter


async def test_channel_type_value_domain() -> None:
    """ChannelType：P3 值域 = telegram（StrEnum 字符串兼容）。"""
    assert ChannelType.TELEGRAM.value == "telegram"
    assert ChannelType.TELEGRAM == "telegram"
    assert [member.value for member in ChannelType] == ["telegram"]


async def test_inbound_message_defaults_and_fields() -> None:
    """InboundMessageModel：全字段构造 + ts_ms 自动填充（epoch 毫秒）。"""
    message = InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id="42",
        sender_id="7",
        text="你好",
    )
    assert message.channel is ChannelType.TELEGRAM
    assert message.chat_id == "10086"
    assert message.message_id == "42"
    assert message.sender_id == "7"
    assert message.text == "你好"
    assert isinstance(message.ts_ms, int)
    assert message.ts_ms > 0


async def test_inbound_message_rejects_empty_fields() -> None:
    """InboundMessageModel：空 chat_id / 空文本拒绝（边界校验）。"""
    with pytest.raises(ValidationError):
        InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="",
            message_id="42",
            sender_id="7",
            text="你好",
        )
    with pytest.raises(ValidationError):
        InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_id="42",
            sender_id="7",
            text="",
        )


async def test_outbound_message_reply_to_nullable() -> None:
    """OutboundMessageModel：reply_to_message_id 可空（兜底消息不引用）。"""
    message = OutboundMessageModel(chat_id="10086", text="收到")
    assert message.reply_to_message_id is None
    referenced = OutboundMessageModel(
        chat_id="10086", text="收到", reply_to_message_id="42"
    )
    assert referenced.reply_to_message_id == "42"
    with pytest.raises(ValidationError):
        OutboundMessageModel(chat_id="10086", text="")


async def test_channel_context_and_session_key() -> None:
    """ChannelContextModel + 会话身份键（channel:chat_id——路由器会话维度）。"""
    context = ChannelContextModel(channel=ChannelType.TELEGRAM, chat_id="10086")
    assert ChannelRouter.session_key(context) == "channel:telegram:10086"


async def test_delivery_result_defaults() -> None:
    """ChannelDeliveryResultModel：message_ids 缺省空列表（未送达形态）。"""
    result = ChannelDeliveryResultModel(channel=ChannelType.TELEGRAM, chat_id="10086")
    assert result.message_ids == []
    delivered = ChannelDeliveryResultModel(
        channel=ChannelType.TELEGRAM, chat_id="10086", message_ids=["11", "12"]
    )
    assert delivered.message_ids == ["11", "12"]
