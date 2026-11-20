"""通道路由器心流门控测试——判定接线 / 延迟排程 / 合并批次 / 降级。

覆盖：门控关闭零排程直通；机器事件不回复；睡眠窗口转延迟批次并经调度器
门面排程；延迟批次执行前遇新入站按静默窗口重排；连发合并按上限承载最近
消息；调度器不可用时延迟裁决降级立即回复；关系亲近优先应答。
全部 fake 依赖（零真实网络 / 零真实配置 / 注入时钟——零固定睡眠）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.flow_models import FlowGateSettings
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.relationship_models import (
    RelationshipStage,
    RelationshipSummaryModel,
)
from packages.harness.rhythm.rhythm_models import RhythmGate

_PERSONA_ID = 7
_BASE_MS = 1_700_000_000_000


class FakeClock:
    """假时钟（epoch 毫秒——测试手动推进）。"""

    def __init__(self, start_ms: int = _BASE_MS) -> None:
        """初始化。"""
        self.now = start_ms

    def __call__(self) -> int:
        """返回当前假时刻。"""
        return self.now


class FakeRhythm:
    """fake 节律门面（可编程放行 / 延迟）。"""

    def __init__(self, *, allowed: bool = True, delay_ms: int = 0) -> None:
        """初始化。"""
        self.allowed = allowed
        self.delay_ms = delay_ms
        self.calls: list[int | None] = []

    async def gate_reply(self, now_ms: int | None = None) -> RhythmGate:
        """返回可编程门结果。"""
        self.calls.append(now_ms)
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
        """记录排程请求。"""
        self.jobs.append((name, factory, delay_ms))
        return name

    async def run_last(self) -> None:
        """执行最近一次排程作业（模拟到期）。"""
        _, factory, _ = self.jobs[-1]
        await factory()


class FakeChatService:
    """fake ChatService（记录会话创建与回复调用；可编程回复失败）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.create_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.reply_error: Exception | None = None

    async def create_session(
        self,
        *,
        persona: object | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建。"""
        self.create_calls.append({"persona": persona, "db_session": db_session, "title": title})
        return ChatSessionStateModel(
            persona_id=_PERSONA_ID,
            persona_name="小鱼",
            card=CharaCardModel(name="小鱼"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance: Any = None
    ) -> str:
        """fake 回复（记录文本与来源分级）。"""
        self.reply_calls.append(
            {"state": state, "text": user_text, "sender_provenance": sender_provenance}
        )
        if self.reply_error is not None:
            raise self.reply_error
        return f"echo:{user_text}"


class FakeAdapter:
    """fake 适配器（记录出站消息）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.sent: list[OutboundMessageModel] = []

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """记录出站并返回成功结果。"""
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=[f"out:{message.chat_id}:{len(self.sent)}"],
        )


@asynccontextmanager
async def _session_factory() -> Any:
    """fake 数据库会话工厂（空会话对象——fake 服务不消费）。"""
    yield object()


def _inbound(
    text: str = "你好",
    *,
    message_id: str = "42",
    synthetic: bool = False,
) -> InboundMessageModel:
    """构造入站消息。"""
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id=message_id,
        sender_id="7",
        text=text,
        synthetic=synthetic,
    )


def _flow_settings(**overrides: Any) -> FlowGateSettings:
    """构造启用的门控配置（测试覆盖值直供）。"""
    payload: dict[str, Any] = {"enabled": True}
    payload.update(overrides)
    return FlowGateSettings(**payload)


def _media_inbound(message_id: str) -> InboundMessageModel:
    """构造仅附件入站消息（空文本 + image 附件——纯图形态）。"""
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id=message_id,
        sender_id="7",
        text="",
        attachments=[
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE,
                url="https://example.test/image.jpg",
            )
        ],
    )


def _router(
    *,
    service: FakeChatService,
    adapter: FakeAdapter,
    clock: FakeClock,
    rhythm: FakeRhythm | None,
    scheduler: FakeScheduler | None,
    settings: FlowGateSettings,
    relationship_lookup: Any = None,
) -> ChannelRouter:
    """装配路由器（注入 fake 依赖）。"""
    router = ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=_session_factory,
        flow_settings=settings,
        rhythm_service=rhythm,
        scheduler_provider=(lambda: scheduler) if scheduler is not None else None,
        relationship_lookup=relationship_lookup,
        clock=clock,
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


async def test_flow_disabled_replies_immediately_without_scheduling() -> None:
    """门控关闭：立即回复且零排程（直通语义）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    router = ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=_session_factory,
        flow_settings=FlowGateSettings(enabled=False),
        scheduler_provider=lambda: scheduler,
        clock=clock,
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    result = await router.handle_inbound(_inbound())
    assert result.message_ids == ["out:10086:1"]
    assert scheduler.jobs == []
    assert service.reply_calls[0]["text"] == "你好"


async def test_synthetic_event_is_not_replied() -> None:
    """机器生成事件：路由器恒跳过（门控开启时不建会话 / 不回复 / 不排程 /
    不写节拍状态）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(),
    )
    result = await router.handle_inbound(_inbound("心跳", synthetic=True))
    assert result.message_ids == []
    assert service.create_calls == []
    assert service.reply_calls == []
    assert scheduler.jobs == []
    assert router.session_count == 0
    assert router.flow_state_count == 0
    assert router.synthetic_skip_count == 1


async def test_synthetic_event_skipped_when_flow_disabled() -> None:
    """门控关闭（FLOW_ENABLED=false）时合成事件仍恒跳过——文档语义与部署
    开关无关。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=FlowGateSettings(enabled=False),
    )
    result = await router.handle_inbound(_inbound("心跳", synthetic=True))
    assert result.message_ids == []
    assert service.create_calls == []
    assert service.reply_calls == []
    assert scheduler.jobs == []
    assert router.session_count == 0
    assert router.synthetic_skip_count == 1


async def test_sleep_window_schedules_delayed_batch() -> None:
    """睡眠窗口：转延迟排程；到期执行后回复缓冲文本。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    rhythm = FakeRhythm(allowed=False, delay_ms=10_000)
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=rhythm,
        scheduler=scheduler,
        settings=_flow_settings(),
    )
    result = await router.handle_inbound(_inbound("晚安"))
    assert result.message_ids == []
    assert service.reply_calls == []
    assert len(scheduler.jobs) == 1
    assert scheduler.jobs[0][2] == 10_000
    rhythm.allowed = True
    rhythm.delay_ms = 0
    clock.now += 10_000
    await scheduler.run_last()
    assert service.reply_calls[0]["text"] == "晚安"
    assert adapter.sent[0].text == "echo:晚安"


async def test_burst_batch_reschedules_until_quiet_then_merges() -> None:
    """连发合并：窗口内新入站使批次重排，静默满足后按上限合并回复。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(
            burst_count=2,
            burst_window_ms=4000,
            merge_quiet_ms=2500,
            merge_max_messages=5,
        ),
    )
    await router.handle_inbound(_inbound("第一条", message_id="1"))
    assert service.reply_calls[0]["text"] == "第一条"
    clock.now += 100
    await router.handle_inbound(_inbound("第二条", message_id="2"))
    assert len(scheduler.jobs) == 1
    clock.now += 100
    await router.handle_inbound(_inbound("第三条", message_id="3"))
    assert len(scheduler.jobs) == 1
    await scheduler.run_last()
    assert len(scheduler.jobs) == 2
    clock.now = scheduler.jobs[-1][2] + _BASE_MS + 200 + 10_000
    await scheduler.run_last()
    assert service.reply_calls[-1]["text"] == "第二条\n第三条"
    assert adapter.sent[-1].text == "echo:第二条\n第三条"


async def test_density_gap_reached_replies_without_scheduling() -> None:
    """追发间隔超过最小回复间隔：立即回复且不排程延迟批次。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(burst_count=3, min_reply_gap_ms=1200),
    )
    await router.handle_inbound(_inbound("第一条", message_id="1"))
    assert service.reply_calls[-1]["text"] == "第一条"
    clock.now += 3500
    result = await router.handle_inbound(_inbound("第二条", message_id="2"))
    assert result.message_ids == ["out:10086:2"]
    assert scheduler.jobs == []
    assert service.reply_calls[-1]["text"] == "第二条"


async def test_scheduler_missing_degrades_to_immediate_reply() -> None:
    """调度器不可用：延迟裁决降级立即回复（fail-safe）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(allowed=False, delay_ms=30_000),
        scheduler=None,
        settings=_flow_settings(),
    )
    result = await router.handle_inbound(_inbound("在吗"))
    assert result.message_ids == ["out:10086:1"]
    assert service.reply_calls[0]["text"] == "在吗"


async def test_close_relationship_prioritizes_reply() -> None:
    """关系亲近：积压阈值触发时优先即时回复。"""

    async def _lookup(persona_id: int, now_ms: int) -> RelationshipSummaryModel:
        return RelationshipSummaryModel(
            stage=RelationshipStage.CLOSE,
            intimacy_level=2,
            score=12.0,
            event_count=1,
            generated_at_ms=now_ms,
        )

    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(backlog_defer_threshold=1),
        relationship_lookup=_lookup,
    )
    result = await router.handle_inbound(_inbound("想你了"))
    assert result.message_ids == ["out:10086:1"]
    assert scheduler.jobs == []
    assert service.reply_calls[0]["text"] == "想你了"


async def test_relationship_and_rhythm_failures_do_not_block_reply() -> None:
    """关系查询 / 节律门失败：按无关系数据与默认放行处理（回复照常）。"""
    async def _failing_lookup(persona_id: int, now_ms: int) -> RelationshipSummaryModel:
        raise RuntimeError("事实库不可用")

    class _FailingRhythm:
        async def gate_reply(self, now_ms: int | None = None) -> RhythmGate:
            raise RuntimeError("节律门不可用")

    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=_FailingRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(backlog_defer_threshold=6),
        relationship_lookup=_failing_lookup,
    )
    result = await router.handle_inbound(_inbound("在吗"))
    assert result.message_ids == ["out:10086:1"]
    assert service.reply_calls[0]["text"] == "在吗"


async def test_scheduler_raise_degrades_to_immediate_reply() -> None:
    """排程调用抛错：降级立即回复（排程面失败不阻断消息）。"""

    class _RaisingScheduler:
        def schedule_once(self, name: str, factory: Any, *, delay_ms: int) -> str:
            raise RuntimeError("调度器拒绝")

    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(allowed=False, delay_ms=5000),
        scheduler=_RaisingScheduler(),
        settings=_flow_settings(),
    )
    result = await router.handle_inbound(_inbound("在吗"))
    assert result.message_ids == ["out:10086:1"]


async def test_delayed_batch_reply_failure_sends_fallback() -> None:
    """延迟批次回复失败：回发兜底文案（错误不击穿调度作业）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    rhythm = FakeRhythm(allowed=False, delay_ms=1000)
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=rhythm,
        scheduler=scheduler,
        settings=_flow_settings(merge_quiet_ms=0),
    )
    await router.handle_inbound(_inbound("在吗"))
    service.reply_error = RuntimeError("网关不可用")
    rhythm.allowed = True
    rhythm.delay_ms = 0
    clock.now += 1000
    await scheduler.run_last()
    assert adapter.sent[-1].text == "我这边出了点问题，稍后再试"


async def test_pending_batch_recovered_after_grace() -> None:
    """已排程任务超过宽限未执行：新入站视为任务丢失并重新排程。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=FakeRhythm(allowed=False, delay_ms=1000),
        scheduler=scheduler,
        settings=_flow_settings(pending_grace_ms=5000),
    )
    await router.handle_inbound(_inbound("第一条"))
    assert len(scheduler.jobs) == 1
    clock.now += 20_000
    await router.handle_inbound(_inbound("第二条"))
    assert len(scheduler.jobs) == 2


async def test_single_empty_text_media_skips_generation_without_fallback() -> None:
    """单条纯媒体空文本（立即裁决路径）：跳过生成、不排程、不发兜底。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    router = _router(
        service=service,
        adapter=adapter,
        clock=FakeClock(),
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(min_reply_gap_ms=0),
    )
    result = await router.handle_inbound(_media_inbound("m1"))
    assert result.message_ids == []
    assert service.reply_calls == []
    assert adapter.sent == []
    assert scheduler.jobs == []
    assert router.empty_text_skip_count == 1


async def test_empty_text_media_burst_keeps_buffer_clean_and_merges_real_text() -> None:
    """连发纯媒体空文本：批次触发后无生成 / 无兜底、缓冲无空条目，到达与
    积压照常推进；随后真实文本合并为净文本（无前导换行）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    scheduler = FakeScheduler()
    clock = FakeClock()
    router = _router(
        service=service,
        adapter=adapter,
        clock=clock,
        rhythm=FakeRhythm(),
        scheduler=scheduler,
        settings=_flow_settings(
            burst_count=2,
            burst_window_ms=4000,
            merge_quiet_ms=0,
            merge_max_messages=5,
            min_reply_gap_ms=0,
            backlog_defer_threshold=8,
        ),
    )
    for message_id in ("m1", "m2", "m3"):
        clock.now += 100
        await router.handle_inbound(_media_inbound(message_id))
    key = "channel:telegram:10086"
    state = router.flow_state_for(key)
    assert state is not None
    assert state.buffered_texts == []
    assert state.backlog_count == 3
    assert len(state.recent_arrival_ms) == 3
    assert service.reply_calls == []
    assert adapter.sent == []
    assert len(scheduler.jobs) == 1
    await scheduler.run_last()
    assert service.reply_calls == []
    assert adapter.sent == []
    state = router.flow_state_for(key)
    assert state is not None
    assert state.pending is False
    clock.now += 100
    await router.handle_inbound(_inbound("真实文本", message_id="m4"))
    assert len(scheduler.jobs) == 2
    await scheduler.run_last()
    assert service.reply_calls[-1]["text"] == "真实文本"
    assert adapter.sent[-1].text == "echo:真实文本"
