"""通道路由器来源分级补充测试——私聊第三方转发（is_owner=False）按 peer 落档。

既有私聊 / 群聊矩阵保持不变；本文件锁定新增的 ``InboundMessageModel.is_owner``
语义（SmsForwarder 等第三方转发通道不因「私聊」而自动晋升 owner）。心流门控
延迟批次（排程到期后执行）复用同一来源分级——批次执行不得把入站标记洗成
owner。
"""

from __future__ import annotations

from typing import Any

from packages.harness.channels.channel_models import ChannelType, InboundMessageModel
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.chat.flow_models import FlowGateSettings
from packages.harness.memory.models import MemoryProvenance
from packages.harness.rhythm.rhythm_models import RhythmGate
from tests.router_fakes import (
    FakeAdapter,
    FakeChatService,
    FakeClock,
    empty_session_factory,
)


class FakeRhythm:
    """fake 节律门面（可编程放行 / 延迟）。"""

    def __init__(self, *, allowed: bool = True, delay_ms: int = 0) -> None:
        """初始化。

        Args:
            allowed: 是否允许即时回复（False = 进入睡眠窗口延迟）。
            delay_ms: 建议延迟毫秒（``allowed=False`` 时即睡眠窗口长度）。
        """
        self.allowed = allowed
        self.delay_ms = delay_ms

    async def gate_reply(self, now_ms: int | None = None) -> RhythmGate:
        """返回可编程门结果。

        Args:
            now_ms: 判定时刻（fake 不消费）。

        Returns:
            节律门结果（放行标记 + 延迟建议）。
        """
        return RhythmGate(
            allowed=self.allowed,
            suggested_delay_ms=self.delay_ms,
            reason="fake",
        )


class FakeScheduler:
    """fake 调度器门面（记录一次性作业——测试手动执行）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.jobs: list[tuple[str, Any, int]] = []

    def schedule_once(self, name: str, factory: Any, *, delay_ms: int) -> str:
        """记录排程请求。

        Args:
            name: 作业名。
            factory: 作业协程工厂。
            delay_ms: 延迟毫秒。

        Returns:
            作业名。
        """
        self.jobs.append((name, factory, delay_ms))
        return name


def _inbound(*, is_owner: bool, is_group_chat: bool = False) -> InboundMessageModel:
    """构造私聊入站消息（owner / 群聊标记可控）。

    Args:
        is_owner: 是否属主来源（False = 第三方转发）。
        is_group_chat: 是否群聊（SmsForwarder 恒 False）。

    Returns:
        通道中立入站消息。
    """
    return InboundMessageModel(
        channel=ChannelType.SMS_FORWARDER,
        chat_id="sms:13800138000",
        message_id="42",
        sender_id="13800138000",
        text="你好",
        is_owner=is_owner,
        is_group_chat=is_group_chat,
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


async def test_delayed_batch_keeps_peer_provenance_for_non_owner() -> None:
    """心流延迟批次：is_owner=False 私聊入站到期回复仍按 peer 落档。"""
    chat = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    rhythm = FakeRhythm(allowed=False, delay_ms=10_000)
    router = ChannelRouter(
        chat_service_factory=lambda: chat,
        session_factory=empty_session_factory,
        flow_settings=FlowGateSettings(enabled=True),
        rhythm_service=rhythm,
        scheduler_provider=lambda: scheduler,
        clock=clock,
    )
    router.register_adapter(ChannelType.SMS_FORWARDER, adapter)
    result = await router.handle_inbound(_inbound(is_owner=False, is_group_chat=False))
    assert result.message_ids == []
    assert chat.reply_calls == []
    assert len(scheduler.jobs) == 1
    rhythm.allowed = True
    rhythm.delay_ms = 0
    clock.now += 10_000
    await scheduler.jobs[-1][1]()
    assert chat.reply_calls[0]["sender_provenance"] == MemoryProvenance.PEER
