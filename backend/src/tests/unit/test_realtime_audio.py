"""实时语音音频字节处理测试——能量 / WAV 封装 / 分片 / 时长。

全部纯字节断言（无第三方音频库）：WAV 头经 stdlib ``wave`` 反向解析验证
采样率 / 声道 / 采样宽度，分片拼接必须与原始字节逐字节一致。
"""

from __future__ import annotations

import io
import wave

import pytest

from packages.harness.voice.realtime_audio import (
    compute_rms_normalized,
    iter_byte_chunks,
    pcm16_duration_ms,
    pcm16_to_wav,
)
from tests.realtime_fakes import loud_frame, silence_frame


async def test_rms_zero_for_empty_and_silence() -> None:
    """空载荷 / 静音帧能量为 0。"""
    assert compute_rms_normalized(b"") == 0.0
    assert compute_rms_normalized(silence_frame()) == 0.0


async def test_rms_ignores_trailing_odd_byte() -> None:
    """尾部不足一个采样的字节不参与能量统计（帧对齐裁剪）。"""
    frame = loud_frame(amplitude=1000, ms=10) + b"\x7f"
    assert compute_rms_normalized(frame) == pytest.approx(1000 / 32768)


async def test_rms_negative_amplitude_symmetric() -> None:
    """负幅度按绝对值计入（能量无方向性）。"""
    assert compute_rms_normalized(loud_frame(amplitude=-2000, ms=10)) == pytest.approx(
        2000 / 32768
    )


async def test_pcm16_to_wav_produces_valid_container() -> None:
    """WAV 封装：头字段与载荷长度经 stdlib 解析核对。"""
    pcm = loud_frame(ms=200)
    payload = pcm16_to_wav(pcm, 16000, 1)
    with wave.open(io.BytesIO(payload), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        assert wav_file.getnframes() == 3200
        assert wav_file.readframes(3200) == pcm


async def test_pcm16_to_wav_rejects_empty_and_bad_params() -> None:
    """不足一帧 / 非法采样率 / 非法声道 → ValueError。"""
    with pytest.raises(ValueError):
        pcm16_to_wav(b"\x01", 16000, 1)
    with pytest.raises(ValueError):
        pcm16_to_wav(b"\x00\x00", 0, 1)
    with pytest.raises(ValueError):
        pcm16_to_wav(b"\x00\x00", 16000, 0)


async def test_iter_byte_chunks_reassembles_exactly() -> None:
    """分片按序拼接等于原始载荷（末片短于分片大小）。"""
    payload = bytes(range(256)) * 10
    chunks = list(iter_byte_chunks(payload, 1000))
    assert len(chunks) == 3
    assert [len(chunk) for chunk in chunks] == [1000, 1000, 560]
    assert b"".join(chunks) == payload
    assert list(iter_byte_chunks(b"", 100)) == []


async def test_iter_byte_chunks_rejects_bad_size() -> None:
    """分片大小非正 → ValueError。"""
    with pytest.raises(ValueError):
        list(iter_byte_chunks(b"abc", 0))


async def test_pcm16_duration_ms() -> None:
    """时长换算：16kHz 单声道 100ms 帧 = 100ms；空载荷 0。"""
    assert pcm16_duration_ms(loud_frame(ms=100), 16000, 1) == 100
    assert pcm16_duration_ms(b"", 16000, 1) == 0
    assert pcm16_duration_ms(loud_frame(ms=100), 8000, 1) == 200


async def test_pcm16_duration_rejects_bad_params() -> None:
    """采样率 / 声道非正 → ValueError。"""
    with pytest.raises(ValueError):
        pcm16_duration_ms(b"\x00\x00", 0, 1)
    with pytest.raises(ValueError):
        pcm16_duration_ms(b"\x00\x00", 16000, 0)
