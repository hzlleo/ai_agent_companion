"""通道路由器来源分级补充测试——私聊第三方转发（is_owner=False）按 peer 落档。

既有私聊 / 群聊矩阵保持不变；本文件锁定新增的 ``InboundMessageModel.is_owner``
语义（SmsForwarder 等第三方转发通道不因「私聊」而自动晋升 owner）。
"""

from __future__ import annotations

from packages.harness.channels.channel_models import ChannelType, InboundMessageModel
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.memory.models import MemoryProvenance
from tests.router_fakes import FakeAdapter, FakeChatService


def _inbound(*, is_owner: bool) -> InboundMessageModel:
    """构造私聊入站消息（owner 标记可控）。"""
    return InboundMessageModel(
        channel=ChannelType.SMS_FORWARDER,
        chat_id="sms:13800138000",
        message_id="42",
        sender_id="13800138000",
        text="你好",
        is_owner=is_owner,
    )


async def test_private_peer_flagged_message_uses_peer_provenance() -> None:
    """私聊但 is_owner=False：ChatService.reply 收到 peer（第三方转发不晋升）。"""
    chat = FakeChatService()
    router = ChannelRouter(chat_service_factory=lambda: chat)
    router.register_adapter(ChannelType.SMS_FORWARDER, FakeAdapter())

    await router.handle_inbound(_inbound(is_owner=False))

    assert chat.reply_calls[0]["sender_provenance"] == MemoryProvenance.PEER


async def test_private_owner_flagged_message_uses_owner_provenance() -> None:
    """私聊且 is_owner=True：维持 owner 落档（既有私聊通道语义）。"""
    chat = FakeChatService()
    router = ChannelRouter(chat_service_factory=lambda: chat)
    router.register_adapter(ChannelType.SMS_FORWARDER, FakeAdapter())

    await router.handle_inbound(_inbound(is_owner=True))

    assert chat.reply_calls[0]["sender_provenance"] == MemoryProvenance.OWNER
