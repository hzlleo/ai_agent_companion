"""声纹音频探测 Unit 测试——WAV 头实测 / 裸 PCM 换算 / 非法与截断头。

真实 WAV 载荷由 ``test_voiceprint_service._wav_bytes`` 构造（与注册 / 校验
用例同源，避免两套头格式漂移）。本测试锁定「声明不构成门槛依据」的
探测层事实：可解析形态一律以头部 / 字节长度实测值为准。
"""

from __future__ import annotations

import pytest

from packages.harness.voice.voiceprint_audio import probe_audio
from packages.harness.voice.voiceprint_models import VoiceprintAudioProbeSource
from tests.unit.test_voiceprint_service import _wav_bytes


async def test_probe_wav_reads_header_duration_and_rate() -> None:
    """WAV 头实测：时长 / 采样率 / 声道来自 fmt 与 data 块。"""
    audio = _wav_bytes(b"header", duration_ms=1_500, sample_rate_hz=16_000, channels=1)
    probe = probe_audio(
        audio,
        content_type="audio/wav",
        declared_duration_ms=9_999,
        declared_sample_rate_hz=44_100,
    )
    assert probe.source is VoiceprintAudioProbeSource.WAV_HEADER
    assert probe.duration_ms == 1_500
    assert probe.sample_rate_hz == 16_000
    assert probe.channels == 1


async def test_probe_wav_different_rate_and_channels() -> None:
    """不同采样率 / 双声道同样按头内声明换算（不被上传方声明覆盖）。"""
    audio = _wav_bytes(b"stereo", duration_ms=1_000, sample_rate_hz=8_000, channels=2)
    probe = probe_audio(
        audio,
        content_type="audio/x-wav",
        declared_duration_ms=1,
        declared_sample_rate_hz=8_000,
    )
    assert probe.source is VoiceprintAudioProbeSource.WAV_HEADER
    assert probe.duration_ms == 1_000
    assert probe.sample_rate_hz == 8_000
    assert probe.channels == 2


async def test_probe_raw_pcm_uses_declared_rate_and_byte_length() -> None:
    """裸 PCM（无 RIFF）：按声明采样率与字节长度换算 1 秒。"""
    audio = b"\x00" * 32_000  # 16000 采样 × 单声道 × 16 位 = 1s
    probe = probe_audio(
        audio,
        content_type="audio/wav",
        declared_duration_ms=99,
        declared_sample_rate_hz=16_000,
    )
    assert probe.source is VoiceprintAudioProbeSource.RAW_PCM
    assert probe.duration_ms == 1_000
    assert probe.sample_rate_hz == 16_000
    assert probe.channels == 1


async def test_probe_non_wav_container_falls_back_to_declared() -> None:
    """不可解析容器（MP3 等）：零依赖下回落上传方声明值。"""
    probe = probe_audio(
        b"ID3-not-really-parsed",
        content_type="audio/mpeg",
        declared_duration_ms=2_400,
        declared_sample_rate_hz=44_100,
    )
    assert probe.source is VoiceprintAudioProbeSource.DECLARED
    assert probe.duration_ms == 2_400
    assert probe.sample_rate_hz == 44_100


async def test_probe_truncated_wav_header_rejected() -> None:
    """截断头（data 段声明长度超出实际字节）→ ValueError。"""
    audio = _wav_bytes(b"truncated", duration_ms=1_000)
    with pytest.raises(ValueError, match="截断"):
        probe_audio(
            audio[:44],
            content_type="audio/wav",
            declared_duration_ms=1_000,
            declared_sample_rate_hz=16_000,
        )


async def test_probe_invalid_wav_magic_rejected() -> None:
    """RIFF 魔数在但非 WAVE 容器 → ValueError（不回落裸 PCM）。"""
    with pytest.raises(ValueError, match="WAV"):
        probe_audio(
            b"RIFF\x10\x00\x00\x00AVI ",
            content_type="audio/wav",
            declared_duration_ms=1_000,
            declared_sample_rate_hz=16_000,
        )


async def test_probe_missing_data_chunk_rejected() -> None:
    """只有 fmt 块、缺 data 块 → ValueError。"""
    fmt_block = b"fmt " + (16).to_bytes(4, "little") + (
        1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (
        16_000).to_bytes(4, "little") + (32_000).to_bytes(4, "little") + (
        2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    body = b"RIFF" + (4 + len(fmt_block)).to_bytes(4, "little") + b"WAVE" + fmt_block
    with pytest.raises(ValueError, match="data"):
        probe_audio(
            body,
            content_type="audio/wav",
            declared_duration_ms=1_000,
            declared_sample_rate_hz=16_000,
        )
