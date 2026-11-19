"""实时语音协议 Model 测试——消息解析 / 序列化 / 配置不变式 / base64 载荷。

覆盖：上行判别联合的全部类型解析、未知 type / 非法 base64 / 非法字段的
拒绝面、下行消息序列化往返、配置跨字段不变式（双阈值 / 队列 / 时长 /
段上限）、env 前缀生效、音频载荷解码守卫。
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from packages.harness.voice.realtime_models import (
    REALTIME_INBOUND_ADAPTER,
    REALTIME_OUTBOUND_ADAPTER,
    RealtimeAssistantTextMessageModel,
    RealtimeBargeInAckMessageModel,
    RealtimeBargeInMessageModel,
    RealtimeBargeInReason,
    RealtimeClosedMessageModel,
    RealtimeCloseReason,
    RealtimeEndMessageModel,
    RealtimeErrorMessageModel,
    RealtimeFinalTranscriptMessageModel,
    RealtimeInterruptedMessageModel,
    RealtimePartialTranscriptMessageModel,
    RealtimePingMessageModel,
    RealtimeReadyMessageModel,
    RealtimeSessionLimitsModel,
    RealtimeSessionMetricsModel,
    RealtimeSessionPhase,
    RealtimeStartMessageModel,
    RealtimeStateMessageModel,
    RealtimeVadPublicSettingsModel,
    RealtimeVoiceSettings,
    decode_audio_base64,
)
from tests.realtime_fakes import audio_message, loud_frame, realtime_settings


async def test_settings_defaults_disabled_and_documented_values() -> None:
    """缺省配置：关闭态 + 采样率 / 声道 / 缺省窗口与文档一致。"""
    config = RealtimeVoiceSettings()
    assert config.enabled is False
    assert config.sample_rate_hz == 16000
    assert config.channels == 1
    assert config.vad_speech_rms_threshold == pytest.approx(0.06)
    assert config.vad_silence_rms_threshold == pytest.approx(0.03)
    assert config.vad_start_consecutive_frames == 3
    assert config.vad_min_speech_ms == 300
    assert config.vad_silence_end_ms == 700
    assert config.vad_max_segment_ms == 15000
    assert config.barge_in_enabled is True
    assert config.audio_batch_max_chunks <= config.audio_queue_max_chunks


async def test_settings_env_prefix_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """REALTIME_VOICE_ 前缀 env 生效（部署 / 测试统一读取面）。"""
    monkeypatch.setenv("REALTIME_VOICE_ENABLED", "true")
    monkeypatch.setenv("REALTIME_VOICE_SAMPLE_RATE_HZ", "22050")
    monkeypatch.setenv("REALTIME_VOICE_BARGE_IN_ENABLED", "false")
    config = RealtimeVoiceSettings()
    assert config.enabled is True
    assert config.sample_rate_hz == 22050
    assert config.barge_in_enabled is False


async def test_settings_reject_inverted_vad_thresholds() -> None:
    """断句阈值高于起说阈值（滞回反转）→ 加载期拒绝。"""
    with pytest.raises(ValidationError):
        realtime_settings(vad_speech_rms_threshold=0.05, vad_silence_rms_threshold=0.05)


async def test_settings_reject_batch_over_queue_capacity() -> None:
    """批处理块数超过队列上限 → 拒绝。"""
    with pytest.raises(ValidationError):
        realtime_settings(audio_queue_max_chunks=4, audio_batch_max_chunks=8)


async def test_settings_reject_idle_not_shorter_than_max_duration() -> None:
    """空闲超时不短于会话最大时长 → 拒绝。"""
    with pytest.raises(ValidationError):
        realtime_settings(idle_timeout_ms=60_000, max_session_ms=60_000)


async def test_settings_reject_segment_ceiling_below_min_speech() -> None:
    """单段上限不大于最短语音 → 拒绝。"""
    with pytest.raises(ValidationError):
        realtime_settings(vad_min_speech_ms=1000, vad_max_segment_ms=1000)


async def test_settings_reject_partial_start_after_segment_ceiling() -> None:
    """中途转写起点晚于单段上限 → 拒绝。"""
    with pytest.raises(ValidationError):
        realtime_settings(vad_max_segment_ms=1000, partial_min_speech_ms=1500)


async def test_inbound_adapter_parses_every_uplink_type() -> None:
    """上行判别联合覆盖全部六类消息（字段面逐项核对）。"""
    start = REALTIME_INBOUND_ADAPTER.validate_json(
        '{"type": "start", "ts_ms": 1, "persona_id": 2, "sample_rate_hz": 16000, '
        '"channels": 1}'
    )
    assert isinstance(start, RealtimeStartMessageModel)
    assert start.persona_id == 2

    chunk = REALTIME_INBOUND_ADAPTER.validate_python(audio_message(0, loud_frame()))
    assert chunk.type == "audio_chunk"

    end = REALTIME_INBOUND_ADAPTER.validate_json('{"type": "end", "ts_ms": 2}')
    assert isinstance(end, RealtimeEndMessageModel)

    barge = REALTIME_INBOUND_ADAPTER.validate_json('{"type": "barge_in", "ts_ms": 3}')
    assert isinstance(barge, RealtimeBargeInMessageModel)
    assert barge.reason == "manual"

    ack = REALTIME_INBOUND_ADAPTER.validate_json(
        '{"type": "barge_in_ack", "ts_ms": 4, "interruption_id": "intr-1"}'
    )
    assert isinstance(ack, RealtimeBargeInAckMessageModel)

    ping = REALTIME_INBOUND_ADAPTER.validate_json('{"type": "ping", "ts_ms": 5}')
    assert isinstance(ping, RealtimePingMessageModel)


async def test_inbound_adapter_rejects_unknown_type_and_bad_payloads() -> None:
    """未知 type / 非法 base64 / 过短音频 / 负序号全部拒绝。"""
    with pytest.raises(ValidationError):
        REALTIME_INBOUND_ADAPTER.validate_json('{"type": "unknown", "ts_ms": 0}')
    with pytest.raises(ValidationError):
        REALTIME_INBOUND_ADAPTER.validate_json(
            '{"type": "audio_chunk", "ts_ms": 0, "seq": 0, "data": "###"}'
        )
    with pytest.raises(ValidationError):
        REALTIME_INBOUND_ADAPTER.validate_json(
            '{"type": "audio_chunk", "ts_ms": 0, "seq": 0, "data": "AA=="}'
        )
    with pytest.raises(ValidationError):
        REALTIME_INBOUND_ADAPTER.validate_json(
            '{"type": "audio_chunk", "ts_ms": 0, "seq": -1, "data": "AAAA"}'
        )


async def test_outbound_ready_round_trip() -> None:
    """ready 消息含能力 / VAD / 上限字段，序列化后可被联合解析。"""
    ready = RealtimeReadyMessageModel(
        ts_ms=1_700_000_000_000,
        protocol_version="1",
        session_id="s-1",
        sample_rate_hz=16000,
        channels=1,
        asr_enabled=True,
        tts_enabled=False,
        barge_in_enabled=True,
        vad=RealtimeVadPublicSettingsModel(
            speech_rms_threshold=0.06,
            silence_rms_threshold=0.03,
            min_speech_ms=300,
            silence_end_ms=700,
            max_segment_ms=15000,
        ),
        limits=RealtimeSessionLimitsModel(
            max_session_ms=600000,
            idle_timeout_ms=60000,
            heartbeat_interval_ms=15000,
            audio_queue_max_chunks=64,
            sentence_max_chars=80,
        ),
    )
    parsed = REALTIME_OUTBOUND_ADAPTER.validate_json(ready.model_dump_json())
    assert isinstance(parsed, RealtimeReadyMessageModel)
    assert parsed.audio_format == "wav"
    assert parsed.tts_enabled is False
    assert parsed.limits.audio_queue_max_chunks == 64


async def test_outbound_round_trip_for_conversation_messages() -> None:
    """partial / final / assistant / audio / state / interrupted / error / closed 全量可序列化。"""
    payloads = [
        RealtimePartialTranscriptMessageModel(ts_ms=1, segment_id="seg-1", text="你"),
        RealtimeFinalTranscriptMessageModel(
            ts_ms=2, segment_id="seg-1", text="你好", duration_ms=300, language="zh"
        ),
        RealtimeAssistantTextMessageModel(ts_ms=3, turn_id="turn-1", text="在的。", final=True),
        RealtimeStateMessageModel(ts_ms=4, state=RealtimeSessionPhase.LISTENING),
        RealtimeInterruptedMessageModel(
            ts_ms=5,
            interruption_id="intr-1",
            turn_id="turn-1",
            reason=RealtimeBargeInReason.MANUAL,
        ),
        RealtimeErrorMessageModel(
            ts_ms=6, code="LLM_CALL_FAILED", message="生成失败", recoverable=True
        ),
        RealtimeClosedMessageModel(ts_ms=7, reason=RealtimeCloseReason.CLIENT_END),
    ]
    for payload in payloads:
        parsed = REALTIME_OUTBOUND_ADAPTER.validate_json(payload.model_dump_json())
        assert type(parsed) is type(payload)


async def test_decode_audio_base64_round_trip_and_guard() -> None:
    """上行音频解码：合法载荷原字节还原；非法载荷抛 ValueError。"""
    frame = loud_frame(ms=20)
    encoded = base64.b64encode(frame).decode("ascii")
    assert decode_audio_base64(encoded) == frame
    with pytest.raises(ValueError):
        decode_audio_base64("not-base64###")


async def test_session_metrics_defaults_and_closed_reason() -> None:
    """指标模型：零计数缺省 + 关闭原因值域。"""
    metrics = RealtimeSessionMetricsModel(started_ms=1)
    assert metrics.audio_chunks_received == 0
    assert metrics.closed_reason is None
    metrics = metrics.model_copy(update={"closed_reason": RealtimeCloseReason.IDLE_TIMEOUT})
    assert metrics.closed_reason is RealtimeCloseReason.IDLE_TIMEOUT
