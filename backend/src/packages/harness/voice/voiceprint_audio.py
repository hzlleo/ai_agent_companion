"""voice 域音频探测 Unit——WAV 头解析与裸 PCM 时长换算（零外部依赖）。

设计边界（锁定）：上传方声明的时长 / 采样率不再是门槛判定依据。

    - **WAV（RIFF/WAVE）**：遍历 ``fmt `` / ``data`` 块，从头部实测
      采样率、声道、位深与数据长度，换算时长；头非法 / 缺块 / 数据段
      截断抛 ``ValueError``（服务层映射为样本拒收）；
    - **裸 PCM**：``audio/wav`` 系 MIME 但载荷无 RIFF 头时，按声明采样
      率与字节长度换算时长（单声道 16 位假设——工程内语音通道的上行
      形态）；
    - **不可解析容器**（MP3 / OGG / WebM / MP4）：零依赖下不解析，沿用
      上传方声明值（``source=declared``）——服务层文档化该边界。

实测优先于声明：同一载荷的声明与实测不一致时，服务层的时长 / 采样率
门槛与落库元数据均采用实测值。
"""

from __future__ import annotations

import struct

from packages.harness.voice.voiceprint_models import (
    VoiceprintAudioProbeModel,
    VoiceprintAudioProbeSource,
)

__all__ = [
    "RAW_PCM_BYTES_PER_SAMPLE",
    "RAW_PCM_CHANNELS",
    "probe_audio",
]

#: 裸 PCM 换算假设（字节/采样——16 位）与声道数（单声道）。
RAW_PCM_BYTES_PER_SAMPLE: int = 2
RAW_PCM_CHANNELS: int = 1

#: WAV 系 MIME（无 RIFF 头时按裸 PCM 解释）。
_WAV_MIME_TYPES: frozenset[str] = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})

_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_WAV_HEADER_MIN_BYTES = 12
#: PCM（1）与 IEEE float（3）——语音样本的合法 WAV 编码。
_WAV_AUDIO_FORMATS: frozenset[int] = frozenset({1, 3})


def _probe_wav_header(audio: bytes) -> VoiceprintAudioProbeModel:
    """解析 RIFF/WAVE 头并换算实测元数据。

    Args:
        audio: 完整音频字节（RIFF 魔数已确认）。

    Returns:
        实测元数据（``source=wav_header``）。

    Raises:
        ValueError: 头长度不足 / 非 WAVE / 缺 ``fmt `` 或 ``data`` 块 /
            ``fmt `` 块过短 / 编码或位深非法 / 数据段长度超出实际字节
            （截断头）。
    """
    if len(audio) < _WAV_HEADER_MIN_BYTES or audio[8:12] != _WAVE_MAGIC:
        raise ValueError("WAV 头非法：缺少 RIFF/WAVE 魔数")
    offset = 12
    fmt: tuple[int, int, int, int, int, int] | None = None
    data_start: int | None = None
    data_size: int | None = None
    while offset + 8 <= len(audio):
        chunk_id = audio[offset : offset + 4]
        size = int.from_bytes(audio[offset + 4 : offset + 8], "little")
        body_start = offset + 8
        if chunk_id == b"fmt ":
            if size < 16 or body_start + 16 > len(audio):
                raise ValueError("WAV fmt 块过短或截断")
            fmt = struct.unpack("<HHIIHH", audio[body_start : body_start + 16])
        elif chunk_id == b"data":
            data_start = body_start
            data_size = size
        if fmt is not None and data_start is not None:
            break
        offset = body_start + size + (size & 1)
    if fmt is None:
        raise ValueError("WAV 缺少 fmt 块")
    if data_start is None or data_size is None:
        raise ValueError("WAV 缺少 data 块")
    audio_format, channels, sample_rate_hz, _byte_rate, _block_align, bits = fmt
    if audio_format not in _WAV_AUDIO_FORMATS:
        raise ValueError(f"WAV 编码不支持：audio_format={audio_format}")
    if channels < 1 or sample_rate_hz <= 0 or bits <= 0:
        raise ValueError("WAV fmt 字段非法：声道 / 采样率 / 位深须为正")
    if data_start + data_size > len(audio):
        raise ValueError("WAV data 段截断：声明长度超出实际字节")
    bytes_per_frame = channels * (bits // 8)
    if bytes_per_frame <= 0:
        raise ValueError("WAV 位深非法：不足一字节")
    frames = data_size // bytes_per_frame
    return VoiceprintAudioProbeModel(
        duration_ms=frames * 1000 // sample_rate_hz,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        source=VoiceprintAudioProbeSource.WAV_HEADER,
    )


def _probe_raw_pcm(audio: bytes, sample_rate_hz: int) -> VoiceprintAudioProbeModel:
    """按声明采样率与字节长度换算裸 PCM 时长（单声道 16 位假设）。

    Args:
        audio: 裸 PCM 字节。
        sample_rate_hz: 声明采样率（换算依据）。

    Returns:
        实测元数据（``source=raw_pcm``；采样率非法时时长记 0，交由服务
        层的采样率门槛拒收）。
    """
    if sample_rate_hz <= 0:
        return VoiceprintAudioProbeModel(
            duration_ms=0,
            sample_rate_hz=sample_rate_hz,
            channels=RAW_PCM_CHANNELS,
            source=VoiceprintAudioProbeSource.RAW_PCM,
        )
    bytes_per_second = sample_rate_hz * RAW_PCM_CHANNELS * RAW_PCM_BYTES_PER_SAMPLE
    return VoiceprintAudioProbeModel(
        duration_ms=len(audio) * 1000 // bytes_per_second,
        sample_rate_hz=sample_rate_hz,
        channels=RAW_PCM_CHANNELS,
        source=VoiceprintAudioProbeSource.RAW_PCM,
    )


def probe_audio(
    audio: bytes,
    *,
    content_type: str,
    declared_duration_ms: int,
    declared_sample_rate_hz: int,
) -> VoiceprintAudioProbeModel:
    """探测音频实测元数据（WAV 头 / 裸 PCM / 不可解析容器的声明回落）。

    Args:
        audio: 解码后的音频字节。
        content_type: 上传方声明的 MIME 类型。
        declared_duration_ms: 上传方声明的时长（仅不可解析容器回落用）。
        declared_sample_rate_hz: 上传方声明的采样率（裸 PCM 换算 /
            不可解析容器回落用）。

    Returns:
        实测元数据（来源见 :class:`VoiceprintAudioProbeModel`）。

    Raises:
        ValueError: 载荷带 RIFF 魔数但 WAV 结构非法 / 截断（服务层映射
            为 PAYLOAD_INVALID 拒收）。
    """
    normalized_type = content_type.strip().lower()
    if audio[:4] == _RIFF_MAGIC:
        return _probe_wav_header(audio)
    if normalized_type in _WAV_MIME_TYPES:
        return _probe_raw_pcm(audio, declared_sample_rate_hz)
    return VoiceprintAudioProbeModel(
        duration_ms=max(0, declared_duration_ms),
        sample_rate_hz=max(0, declared_sample_rate_hz),
        channels=1,
        source=VoiceprintAudioProbeSource.DECLARED,
    )
