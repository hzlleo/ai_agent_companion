"""realtime voice 域音频字节处理——PCM16 能量 / WAV 封装 / 分片。

纯字节面工具（零第三方依赖、零 numpy）：上行 PCM16 只做能量统计与 WAV
容器封装，下行 TTS 音频只做固定字节分片——全部为 O(n) 线性扫描，单帧
（几十毫秒音频）开销在微秒级，不构成 CPU 密集面，因此不经
``run_blocking_ops`` 下沉线程池。

约定：

    - PCM 载荷为 **16 位小端有符号整数**（int16），按采样点解释；
    - 能量为归一化 RMS（0-1）：``mean(|sample|) / 32768``——用平均绝对值
      代替平方根，避免大帧平方和溢出并保持单调性（VAD 阈值只依赖单调
      比较），文档化决策；
    - WAV 封装只写标准 RIFF 头（``wave`` 模块，16 位 PCM），奇数尾字节
      按帧对齐裁剪（半个采样不构成一帧）；
    - 分片函数保证「按序拼接 = 原始字节」（零拷贝切片视图语义）。
"""

from __future__ import annotations

import io
import wave
from array import array
from collections.abc import Iterator

__all__ = [
    "compute_rms_normalized",
    "pcm16_to_wav",
    "iter_byte_chunks",
    "pcm16_duration_ms",
]

#: int16 满量程（归一化分母）。
_INT16_FULL_SCALE: float = 32768.0

#: 单个采样点字节数（16 位）。
_SAMPLE_BYTES: int = 2


def compute_rms_normalized(pcm16: bytes) -> float:
    """计算 PCM16 帧的归一化能量（平均绝对值 / 满量程）。

    空载荷返回 0.0（静音语义）；载荷长度按 2 字节对齐截断（尾字节丢弃）。

    Args:
        pcm16: 16 位小端 PCM 字节。

    Returns:
        归一化能量（0.0-1.0）。
    """
    usable = len(pcm16) - (len(pcm16) % _SAMPLE_BYTES)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm16[:usable])
    total = 0
    for sample in samples:
        total += sample if sample >= 0 else -sample
    return (total / len(samples)) / _INT16_FULL_SCALE


def pcm16_duration_ms(pcm16: bytes, sample_rate_hz: int, channels: int) -> int:
    """计算 PCM16 载荷对应的音频时长（毫秒）。

    Args:
        pcm16: PCM16 字节。
        sample_rate_hz: 采样率（>0）。
        channels: 声道数（>0）。

    Returns:
        时长毫秒（向下取整；空载荷 0）。

    Raises:
        ValueError: 采样率 / 声道数非正。
    """
    if sample_rate_hz <= 0 or channels <= 0:
        raise ValueError("采样率与声道数必须为正")
    frame_bytes = _SAMPLE_BYTES * channels
    frames = len(pcm16) // frame_bytes
    return (frames * 1000) // sample_rate_hz


def pcm16_to_wav(pcm16: bytes, sample_rate_hz: int, channels: int) -> bytes:
    """把裸 PCM16 字节封装为 WAV 容器（ASR 端点按容器嗅探）。

    Args:
        pcm16: 16 位小端 PCM 字节（尾部不足一帧的字节裁剪丢弃）。
        sample_rate_hz: 采样率（>0）。
        channels: 声道数（1-2）。

    Returns:
        WAV 容器字节。

    Raises:
        ValueError: 采样率 / 声道数非法，或载荷不足一帧。
    """
    if sample_rate_hz <= 0 or channels <= 0:
        raise ValueError("采样率与声道数必须为正")
    frame_bytes = _SAMPLE_BYTES * channels
    usable = len(pcm16) - (len(pcm16) % frame_bytes)
    if usable <= 0:
        raise ValueError("PCM16 载荷不足一帧，无法封装 WAV")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(_SAMPLE_BYTES)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm16[:usable])
    return buffer.getvalue()


def iter_byte_chunks(payload: bytes, chunk_size: int) -> Iterator[bytes]:
    """按固定字节数切分载荷（下行 TTS 分片面）。

    Args:
        payload: 待切分字节。
        chunk_size: 单块字节数（>0）。

    Yields:
        顺序切片；最后一块可能短于 ``chunk_size``；空载荷不产出任何块。

    Raises:
        ValueError: ``chunk_size`` 非正。
    """
    if chunk_size <= 0:
        raise ValueError("分片字节数必须为正")
    for start in range(0, len(payload), chunk_size):
        yield payload[start : start + chunk_size]
