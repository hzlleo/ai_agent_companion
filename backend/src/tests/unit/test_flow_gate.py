"""心流门控纯函数测试——判定矩阵 / 连发合并 / 缓冲与排程状态。

覆盖：门控关闭直通；机器事件不回复；睡眠窗口延迟；连发合并与静默窗口
重排原料；回复间隔节流；未读积压阶梯延迟与封顶；关系亲近 / 亲密优先
应答；节律拟人延迟；缓冲上限裁剪确定性；入站记录与积压 / 缓冲边界。
全部为纯函数直调（零 I/O / 零真实配置 / 时间全部注入）。
"""

from __future__ import annotations

from packages.harness.chat.flow_gate import (
    clear_backlog,
    clear_pending,
    evaluate_flow_gate,
    mark_pending,
    merge_buffered_texts,
    record_inbound,
    remaining_quiet_ms,
    rhythm_state_from_gate,
)
from packages.harness.chat.flow_models import (
    FLOW_ARRIVAL_HISTORY_LIMIT,
    FLOW_BUFFERED_TEXT_LIMIT,
    FlowChatStateModel,
    FlowGateInputModel,
    FlowGateSettings,
    FlowReason,
    FlowVerdict,
)
from packages.harness.chat.persona_rotation_models import GroupPersonaSettings
from packages.harness.persona.relationship_models import (
    RelationshipStage,
    RelationshipSummaryModel,
)

_BASE_MS = 1_700_000_000_000


def _settings(**overrides: object) -> FlowGateSettings:
    """构造启用状态的门控配置（测试覆盖值直供）。"""
    payload: dict[str, object] = {"enabled": True}
    payload.update(overrides)
    return FlowGateSettings.model_construct(**payload)


def _relationship(stage: RelationshipStage) -> RelationshipSummaryModel:
    """构造指定阶段的关系摘要（其余字段取空摘要形态）。"""
    return RelationshipSummaryModel(
        stage=stage,
        intimacy_level=3 if stage == RelationshipStage.INTIMATE else 2 if stage == RelationshipStage.CLOSE else 0,
        score=30.0 if stage in (RelationshipStage.CLOSE, RelationshipStage.INTIMATE) else 0.0,
        event_count=2,
        generated_at_ms=_BASE_MS,
    )


def _input(
    *,
    now_ms: int = _BASE_MS,
    arrivals: list[int] | None = None,
    backlog: int = 0,
    allowed: bool = True,
    delay_ms: int = 0,
    synthetic: bool = False,
    relationship: RelationshipSummaryModel | None = None,
) -> FlowGateInputModel:
    """构造门控判定输入（缺省为清醒、单条、无积压）。"""
    return FlowGateInputModel(
        now_ms=now_ms,
        is_synthetic=synthetic,
        rhythm=rhythm_state_from_gate(allowed=allowed, suggested_delay_ms=delay_ms),
        relationship=relationship,
        state=FlowChatStateModel(
            recent_arrival_ms=list(arrivals or [now_ms]),
            backlog_count=backlog,
        ),
    )


async def test_settings_env_surface_is_hard_overridden() -> None:
    """裸构造读到的门控与群聊配置恒为测试硬覆盖值（本地 .env 不得泄入）。"""
    flow = FlowGateSettings()
    assert flow.enabled is False
    assert flow.burst_window_ms == 4000
    assert flow.burst_count == 3
    assert flow.merge_quiet_ms == 2500
    assert flow.min_reply_gap_ms == 1200
    assert flow.pending_grace_ms == 60000
    group = GroupPersonaSettings()
    assert group.enabled is False
    assert group.max_responders == 1
    assert group.cooldown_ms == 60000


async def test_disabled_gate_always_replies() -> None:
    """门控关闭：恒立即回复且零延迟（直通形态）。"""
    decision = evaluate_flow_gate(
        _input(backlog=99, allowed=False, delay_ms=5000),
        FlowGateSettings(enabled=False),
    )
    assert decision.verdict == FlowVerdict.REPLY
    assert decision.delay_ms == 0
    assert decision.reason == FlowReason.DISABLED


async def test_synthetic_event_is_skipped() -> None:
    """机器生成事件：不进入人类回合式回复。"""
    decision = evaluate_flow_gate(_input(synthetic=True), _settings())
    assert decision.verdict == FlowVerdict.SKIP
    assert decision.reason == FlowReason.SYNTHETIC_EVENT


async def test_sleep_window_defers_until_wake() -> None:
    """睡眠窗口：延迟采用节律门的距醒来毫秒。"""
    decision = evaluate_flow_gate(
        _input(allowed=False, delay_ms=27_000_000), _settings()
    )
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.delay_ms == 27_000_000
    assert decision.reason == FlowReason.SLEEP_WINDOW


async def test_burst_merges_after_quiet_window() -> None:
    """窗口内连发达阈值：转合并并按静默窗口剩余排程。"""
    arrivals = [_BASE_MS - 800, _BASE_MS - 400, _BASE_MS]
    decision = evaluate_flow_gate(
        _input(arrivals=arrivals),
        _settings(burst_window_ms=4000, burst_count=3, merge_quiet_ms=2500),
    )
    assert decision.verdict == FlowVerdict.MERGE
    assert decision.delay_ms == 2500
    assert decision.reason == FlowReason.BURST_MERGE


async def test_burst_outside_window_does_not_merge() -> None:
    """窗口外的历史入站不参与连发计数。"""
    arrivals = [_BASE_MS - 60_000, _BASE_MS - 50_000, _BASE_MS]
    decision = evaluate_flow_gate(
        _input(arrivals=arrivals),
        _settings(burst_window_ms=4000, burst_count=3),
    )
    assert decision.verdict == FlowVerdict.REPLY


async def test_density_pacing_defers_short_gap_follow_up() -> None:
    """生产形态追发（当前消息已入历史）：以上一条入站为锚补齐最小间隔。"""
    arrivals = [_BASE_MS - 300, _BASE_MS]
    decision = evaluate_flow_gate(
        _input(arrivals=arrivals),
        _settings(burst_count=3, min_reply_gap_ms=1200),
    )
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.delay_ms == 900
    assert decision.reason == FlowReason.DENSITY_PACING


async def test_density_pacing_anchor_without_current_entry_is_equivalent() -> None:
    """未含当前条目的历史形态与生产形态等价：末条即上一条入站，补齐同额延迟。"""
    arrivals = [_BASE_MS - 600, _BASE_MS - 300]
    decision = evaluate_flow_gate(
        _input(arrivals=arrivals),
        _settings(burst_count=3, min_reply_gap_ms=1200),
    )
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.delay_ms == 900
    assert decision.reason == FlowReason.DENSITY_PACING


async def test_density_pacing_replies_when_gap_reaches_minimum() -> None:
    """追发间隔达到最小回复间隔：不延迟，落入正常立即回复。"""
    arrivals = [_BASE_MS - 3500, _BASE_MS]
    decision = evaluate_flow_gate(
        _input(arrivals=arrivals),
        _settings(burst_count=3, min_reply_gap_ms=1200),
    )
    assert decision.verdict == FlowVerdict.REPLY
    assert decision.delay_ms == 0
    assert decision.reason == FlowReason.NORMAL


async def test_backlog_defers_with_step_and_cap() -> None:
    """未读积压：阶梯延迟并按上限封顶。"""
    decision = evaluate_flow_gate(
        _input(backlog=9),
        _settings(
            backlog_defer_threshold=6,
            backlog_defer_step_ms=8000,
            backlog_defer_max_ms=90000,
        ),
    )
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.delay_ms == 32000
    assert decision.reason == FlowReason.BACKLOG_DEFER
    capped = evaluate_flow_gate(
        _input(backlog=100),
        _settings(
            backlog_defer_threshold=6,
            backlog_defer_step_ms=8000,
            backlog_defer_max_ms=90000,
        ),
    )
    assert capped.delay_ms == 90000


async def test_close_relationship_bypasses_throttles() -> None:
    """亲近 / 亲密关系：跳过密度与积压节流，优先应答。"""
    decision = evaluate_flow_gate(
        _input(
            backlog=20,
            arrivals=[_BASE_MS - 200, _BASE_MS],
            relationship=_relationship(RelationshipStage.CLOSE),
        ),
        _settings(backlog_defer_threshold=6, min_reply_gap_ms=1200),
    )
    assert decision.verdict == FlowVerdict.REPLY
    assert decision.reason == FlowReason.RELATIONSHIP_PRIORITY
    stranger = evaluate_flow_gate(
        _input(backlog=20, relationship=_relationship(RelationshipStage.STRANGER)),
        _settings(backlog_defer_threshold=6),
    )
    assert stranger.verdict == FlowVerdict.DEFER


async def test_sleep_window_outranks_relationship_priority() -> None:
    """睡眠窗口优先于关系优先（人睡了，关系再近也不叫醒）。"""
    decision = evaluate_flow_gate(
        _input(
            allowed=False,
            delay_ms=3600_000,
            backlog=20,
            relationship=_relationship(RelationshipStage.INTIMATE),
        ),
        _settings(),
    )
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.reason == FlowReason.SLEEP_WINDOW


async def test_rhythm_delay_defers_awake_reply() -> None:
    """清醒时段的拟人延迟：转延迟批次，不做即时回复。"""
    decision = evaluate_flow_gate(_input(delay_ms=2500), _settings())
    assert decision.verdict == FlowVerdict.DEFER
    assert decision.delay_ms == 2500
    assert decision.reason == FlowReason.RHYTHM_DELAY


async def test_plain_awake_message_replies_immediately() -> None:
    """无特殊信号：立即回复。"""
    decision = evaluate_flow_gate(_input(), _settings())
    assert decision.verdict == FlowVerdict.REPLY
    assert decision.reason == FlowReason.NORMAL


async def test_merge_keeps_newest_messages_and_reports_drops() -> None:
    """合并超出上限：保留最近条目、按入站顺序连接并报告丢弃条数。"""
    merged = merge_buffered_texts(
        ["m1", "m2", "m3", "m4"], _settings(merge_max_messages=2)
    )
    assert merged.text == "m3\nm4"
    assert merged.used_count == 2
    assert merged.dropped_count == 2
    empty = merge_buffered_texts([], _settings())
    assert empty.text == ""
    assert empty.used_count == 0
    assert empty.dropped_count == 0


async def test_record_inbound_caps_history_and_buffer() -> None:
    """入站记录：到达历史与缓冲均按内部上限保留最近条目。"""
    state = FlowChatStateModel()
    for index in range(FLOW_BUFFERED_TEXT_LIMIT + 5):
        state = record_inbound(state, f"m{index}", _BASE_MS + index)
    assert len(state.recent_arrival_ms) == FLOW_ARRIVAL_HISTORY_LIMIT
    assert len(state.buffered_texts) == FLOW_BUFFERED_TEXT_LIMIT
    assert state.buffered_texts[-1] == f"m{FLOW_BUFFERED_TEXT_LIMIT + 4}"
    assert state.backlog_count == FLOW_BUFFERED_TEXT_LIMIT + 5


async def test_record_inbound_without_text_append_keeps_arrival_and_backlog() -> None:
    """仅附件入站（append_text=False）：到达时刻与积压递增，缓冲不追加空条目。"""
    state = record_inbound(FlowChatStateModel(), "旧文本", _BASE_MS)
    state = record_inbound(state, "", _BASE_MS + 10, append_text=False)
    assert state.buffered_texts == ["旧文本"]
    assert state.backlog_count == 2
    assert state.recent_arrival_ms == [_BASE_MS, _BASE_MS + 10]


async def test_pending_and_backlog_state_transitions() -> None:
    """排程标记 / 清除与积压清空的状态迁移保持显式。"""
    state = record_inbound(FlowChatStateModel(), "你好", _BASE_MS)
    pending = mark_pending(state, due_ms=_BASE_MS + 2000)
    assert pending.pending is True
    assert pending.pending_due_ms == _BASE_MS + 2000
    cleared = clear_pending(pending)
    assert cleared.pending is False
    assert cleared.pending_due_ms is None
    drained = clear_backlog(cleared)
    assert drained.backlog_count == 0
    assert drained.buffered_texts == []
    assert drained.recent_arrival_ms == [_BASE_MS]


async def test_remaining_quiet_window_is_deterministic() -> None:
    """静默窗口剩余毫秒：按最后入站与注入时刻确定性计算。"""
    state = FlowChatStateModel(recent_arrival_ms=[_BASE_MS - 500])
    settings = _settings(merge_quiet_ms=2500)
    assert remaining_quiet_ms(state, _BASE_MS, settings) == 2000
    assert remaining_quiet_ms(state, _BASE_MS + 5000, settings) == 0
    assert remaining_quiet_ms(FlowChatStateModel(), _BASE_MS, settings) == 0
