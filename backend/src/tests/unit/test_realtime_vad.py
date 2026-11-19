"""实时语音 VAD 测试——起说防抖 / 静音切段 / 短语音丢弃 / 段上限 / 冲刷。

全部用例喂固定 PCM16 帧（高能量 / 静音），断言事件序列与语段载荷——
不依赖真实时钟（VAD 计时由帧时长累加）。
"""

from __future__ import annotations

from packages.harness.voice.realtime_audio import compute_rms_normalized
from packages.harness.voice.realtime_vad import (
    EnergyVad,
    VadEventKind,
    build_vad_config,
)
from tests.realtime_fakes import loud_frame, realtime_settings, silence_frame


def _vad(**overrides: object) -> EnergyVad:
    """构造测试用 VAD（缺省窗口：2 帧起说 / 100ms 静音切段 / 最短 100ms）。"""
    return EnergyVad(build_vad_config(realtime_settings(**overrides)))


async def test_single_loud_frame_does_not_start_speech() -> None:
    """单帧尖峰不触发起说（防抖要求连续 2 帧）。"""
    vad = _vad()
    result = vad.feed(loud_frame(), 0)
    assert result.events == []
    assert result.speaking is False
    assert vad.speaking is False
    assert vad.current_audio() == b""


async def test_consecutive_frames_start_speech_and_keep_candidate_audio() -> None:
    """连续达标帧进入语音态，起说前导帧并入语段缓冲。"""
    vad = _vad()
    first = vad.feed(loud_frame(), 100)
    assert first.speaking is False
    second = vad.feed(loud_frame(), 200)
    assert [event.kind for event in second.events] == [VadEventKind.SPEECH_START]
    assert second.speaking is True
    assert len(vad.current_audio()) == 6400
    assert vad.voiced_ms == 200


async def test_silence_window_closes_segment_with_audio_payload() -> None:
    """持续静音达到窗口 → segment_ready 携带 PCM16 与有效语音时长。"""
    vad = _vad()
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    vad.feed(loud_frame(), 200)
    result = vad.feed(silence_frame(), 300)
    assert [event.kind for event in result.events] == [VadEventKind.SEGMENT_READY]
    event = result.events[0]
    assert event.pcm16 is not None
    assert event.duration_ms == 300
    assert event.total_ms == 400
    assert vad.speaking is False
    assert result.buffered_bytes == 0


async def test_short_burst_is_discarded() -> None:
    """有效语音低于最短门槛 → segment_discarded（不携带音频）。"""
    vad = _vad(vad_min_speech_ms=500)
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    result = vad.feed(silence_frame(), 200)
    assert [event.kind for event in result.events] == [VadEventKind.SEGMENT_DISCARDED]
    assert result.events[0].pcm16 is None
    assert result.events[0].duration_ms == 200


async def test_brief_pause_does_not_split_segment() -> None:
    """句中短暂停顿（未满静音窗）不清零语音态——滞回防抖。"""
    vad = _vad(vad_silence_end_ms=300)
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    pause = vad.feed(silence_frame(), 200)
    assert pause.speaking is True
    assert pause.events == []
    resumed = vad.feed(loud_frame(), 300)
    assert resumed.speaking is True
    assert resumed.events == []
    closed = vad.feed(silence_frame(), 400)
    assert closed.events == []
    closed_again = vad.feed(silence_frame(), 500)
    assert closed_again.events == []
    final = vad.feed(silence_frame(), 600)
    assert [event.kind for event in final.events] == [VadEventKind.SEGMENT_READY]


async def test_segment_ceiling_forces_ready() -> None:
    """达到单段上限 → segment_forced（长独白不无限阻塞转写）。"""
    vad = _vad(vad_max_segment_ms=500, vad_silence_end_ms=1000)
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    for index in range(2, 4):
        assert vad.feed(loud_frame(), 100 * (index + 1)).events == []
    forced = vad.feed(loud_frame(), 500)
    assert [event.kind for event in forced.events] == [VadEventKind.SEGMENT_FORCED]
    assert forced.speaking is False
    assert forced.events[0].duration_ms == 500


async def test_flush_returns_pending_segment_and_is_idempotent() -> None:
    """flush 冲刷在途语段；空闲态 flush 为空。"""
    vad = _vad()
    assert vad.flush(0).events == []
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    flushed = vad.flush(200)
    assert [event.kind for event in flushed.events] == [VadEventKind.SEGMENT_READY]
    assert vad.speaking is False
    assert vad.flush(300).events == []


async def test_multiple_segments_in_sequence() -> None:
    """两段独立语音依次切分（切段后重新走起说防抖）。"""
    vad = _vad()
    vad.feed(loud_frame(), 0)
    vad.feed(loud_frame(), 100)
    first = vad.feed(silence_frame(), 200)
    assert len(first.events) == 1
    # 单帧静音后单帧高声不足以重新起说。
    restart = vad.feed(loud_frame(), 300)
    assert restart.speaking is False
    started = vad.feed(loud_frame(), 400)
    assert [event.kind for event in started.events] == [VadEventKind.SPEECH_START]
    second = vad.feed(silence_frame(), 500)
    assert [event.kind for event in second.events] == [VadEventKind.SEGMENT_READY]


async def test_rms_energy_distinguishes_speech_and_silence() -> None:
    """能量统计：静音 0；恒定幅度帧 = 幅度 / 满量程。"""
    assert compute_rms_normalized(silence_frame()) == 0.0
    assert compute_rms_normalized(loud_frame(amplitude=1638)) == 1638 / 32768
