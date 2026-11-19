"""实时语音状态机测试——全迁移矩阵 / 非法事件 / 关闭吸收态 / 规则唯一性。

状态机是协议语义的单一真相：本文件按「相位 × 事件」穷举断言目标相位，
并对未定义边断言 allowed=False 且相位不变。
"""

from __future__ import annotations

import pytest

from packages.harness.voice.realtime_models import RealtimeSessionPhase
from packages.harness.voice.realtime_state import (
    RealtimeStateEvent,
    RealtimeVoiceStateMachine,
)

_TS = 1_700_000_000_000


async def _machine_at(phase: RealtimeSessionPhase) -> RealtimeVoiceStateMachine:
    """构造处于指定相位的状态机（经合法路径推进）。"""
    machine = RealtimeVoiceStateMachine()
    if phase is RealtimeSessionPhase.IDLE:
        return machine
    machine.transition(RealtimeStateEvent.START, ts_ms=_TS)
    if phase is RealtimeSessionPhase.LISTENING:
        return machine
    machine.transition(RealtimeStateEvent.SEGMENT_READY, ts_ms=_TS)
    if phase is RealtimeSessionPhase.THINKING:
        return machine
    machine.transition(RealtimeStateEvent.AUDIO_PLAYING, ts_ms=_TS)
    assert machine.phase is RealtimeSessionPhase.SPEAKING
    return machine


async def test_start_moves_idle_to_listening() -> None:
    """idle + START → listening（会话建立）。"""
    machine = RealtimeVoiceStateMachine()
    assert machine.phase is RealtimeSessionPhase.IDLE
    transition = machine.transition(RealtimeStateEvent.START, ts_ms=_TS)
    assert transition.allowed is True
    assert transition.changed is True
    assert transition.from_phase is RealtimeSessionPhase.IDLE
    assert transition.to_phase is RealtimeSessionPhase.LISTENING
    assert "会话建立" in transition.detail


@pytest.mark.parametrize(
    ("phase", "event", "expected"),
    [
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.SPEECH_START, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.SEGMENT_READY, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.ASR_DONE, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.AUDIO_PLAYING, RealtimeSessionPhase.SPEAKING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.TURN_DONE, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.ASR_ERROR, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.LISTENING, RealtimeStateEvent.GENERATION_ERROR, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.SPEECH_START, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.SEGMENT_READY, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.ASR_DONE, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.AUDIO_PLAYING, RealtimeSessionPhase.SPEAKING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.TURN_DONE, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.BARGE_IN, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.ASR_ERROR, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.GENERATION_ERROR, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.THINKING, RealtimeStateEvent.TTS_FAILURE, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.SPEECH_START, RealtimeSessionPhase.SPEAKING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.SEGMENT_READY, RealtimeSessionPhase.THINKING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.AUDIO_PLAYING, RealtimeSessionPhase.SPEAKING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.TURN_DONE, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.BARGE_IN, RealtimeSessionPhase.LISTENING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.GENERATION_ERROR, RealtimeSessionPhase.SPEAKING),
        (RealtimeSessionPhase.SPEAKING, RealtimeStateEvent.TTS_FAILURE, RealtimeSessionPhase.SPEAKING),
    ],
)
async def test_transition_matrix(
    phase: RealtimeSessionPhase,
    event: RealtimeStateEvent,
    expected: RealtimeSessionPhase,
) -> None:
    """合法边按规则表推进（目标相位逐条锁定）。"""
    machine = await _machine_at(phase)
    transition = machine.transition(event, ts_ms=_TS)
    assert transition.allowed is True
    assert transition.from_phase is phase
    assert transition.to_phase is expected
    assert machine.phase is expected


@pytest.mark.parametrize("phase", list(RealtimeSessionPhase))
@pytest.mark.parametrize(
    "event",
    [
        RealtimeStateEvent.CLIENT_END,
        RealtimeStateEvent.IDLE_TIMEOUT,
        RealtimeStateEvent.MAX_DURATION,
        RealtimeStateEvent.INTERNAL_ERROR,
    ],
)
async def test_close_events_reach_closed_from_every_phase(
    phase: RealtimeSessionPhase, event: RealtimeStateEvent
) -> None:
    """四类关闭事件从任意相位都进入 closed（含 closed 自身吸收态）。"""
    machine = await _machine_at(phase) if phase is not RealtimeSessionPhase.CLOSED else (
        RealtimeVoiceStateMachine()
    )
    if phase is RealtimeSessionPhase.CLOSED:
        machine.transition(RealtimeStateEvent.CLIENT_END, ts_ms=_TS)
    transition = machine.transition(event, ts_ms=_TS)
    assert transition.allowed is True
    assert machine.phase is RealtimeSessionPhase.CLOSED


async def test_illegal_events_keep_phase_and_report_reason() -> None:
    """未定义边：allowed=False、相位不变、detail 写明相位与事件。"""
    machine = RealtimeVoiceStateMachine()
    transition = machine.transition(RealtimeStateEvent.AUDIO_PLAYING, ts_ms=_TS)
    assert transition.allowed is False
    assert transition.changed is False
    assert transition.from_phase is RealtimeSessionPhase.IDLE
    assert transition.to_phase is RealtimeSessionPhase.IDLE
    assert "idle" in transition.detail
    assert "audio_playing" in transition.detail
    assert machine.phase is RealtimeSessionPhase.IDLE


async def test_closed_phase_absorbs_non_close_events() -> None:
    """closed 吸收态只接受关闭事件——其它事件记为非法但不改变相位。"""
    machine = await _machine_at(RealtimeSessionPhase.LISTENING)
    machine.transition(RealtimeStateEvent.CLIENT_END, ts_ms=_TS)
    transition = machine.transition(RealtimeStateEvent.SPEECH_START, ts_ms=_TS)
    assert transition.allowed is False
    assert machine.phase is RealtimeSessionPhase.CLOSED


async def test_transition_record_carries_injected_timestamp() -> None:
    """时间戳由调用方注入（状态机零系统时钟依赖）。"""
    machine = RealtimeVoiceStateMachine()
    transition = machine.transition(RealtimeStateEvent.START, ts_ms=999)
    assert transition.ts_ms == 999


async def test_rule_table_has_no_duplicate_edges() -> None:
    """规则表无重复 (from_phase, event) 边（重复会让迁移依赖表顺序）。"""
    from packages.harness.voice.realtime_state import _RULES

    edges = [(rule.from_phase, rule.event) for rule in _RULES]
    assert len(edges) == len(set(edges))
    assert len(edges) >= 40
