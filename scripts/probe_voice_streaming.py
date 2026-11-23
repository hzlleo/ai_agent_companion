#!/usr/bin/env python3
"""语音流式通道部署探测——ASR / TTS 实时端点最小握手与一帧往返（一次性）。

用途：部署机验证「真流式」双轨通道是否可用（本机 CI 无供应商连通性，
全部单测经 fake 传输面）。本脚本**不入门禁链**——由运维在部署机手工执行：

    cd backend && pixi run python ../scripts/probe_voice_streaming.py

判定口径（逐子项独立）：

    - ``PASS``：连接建立 + 握手确认 + 一帧上行/下行往返成功（ASR 追加一
      帧 PCM16 并收尾、TTS 取到至少一个音频分片并成功取消）；
    - ``SKIP``：未启用或端点未配置（无凭据 / ``*_STREAMING_ENABLED=false``
      ——该子项降级不影响批量主链）；
    - ``FAIL``：连接 / 握手 / 往返失败（输出端点与失败原因；该子项降级，
      不影响批量主链——服务端运行期也会自动回退）。

运行方式（锁定）：必须经后端 pixi 环境执行——其 activation 已注入
``PYTHONPATH=<backend>/src``，本脚本因此保持模块级导入（无路径修补、无
``# noqa``）：

    cd backend && pixi run python ../scripts/probe_voice_streaming.py

退出码：``0`` = 无 FAIL（PASS / SKIP 均可）；``1`` = 至少一个 FAIL；
``2`` = 参数错误。

配置来源与后端一致（``backend/.env`` + 进程 env；``VOICE_ASR_*`` /
``VOICE_TTS_*`` 分节）。超时可用 ``--timeout`` 覆盖（秒——部署机网络抖动
时放宽）。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from array import array
from typing import Final

from packages.harness.config.models import Settings
from packages.harness.core.errors import BusinessException
from packages.harness.voice.asr_stream_client import Qwen3AsrStreamClient
from packages.harness.voice.stream_transport import make_websocket_connector
from packages.harness.voice.tts_stream_client import CosyVoiceStreamClient

#: 探测音频：16kHz 单声道 100ms 440Hz 正弦波（PCM16——非静音便于端点判定语音）。
_PROBE_SAMPLE_RATE_HZ: Final[int] = 16000
_PROBE_FRAME_MS: Final[int] = 100
_PROBE_TONE_HZ: Final[float] = 440.0
#: TTS 探测文本（短句——最小合成负载）。
_PROBE_TTS_TEXT: Final[str] = "你好"


def _tone_frame() -> bytes:
    """构造 100ms 440Hz 正弦波 PCM16 帧（16kHz 单声道）。

    Returns:
        16 位小端 PCM 字节。
    """
    samples = _PROBE_SAMPLE_RATE_HZ * _PROBE_FRAME_MS // 1000
    pcm = array(
        "h",
        (
            int(6000 * math.sin(2 * math.pi * _PROBE_TONE_HZ * index / _PROBE_SAMPLE_RATE_HZ))
            for index in range(samples)
        ),
    )
    return pcm.tobytes()


async def _probe_asr(settings: Settings, *, timeout_seconds: float) -> tuple[str, str]:
    """探测流式识别通道（握手 + 一帧 + 收尾）。

    Args:
        settings: 生效配置。
        timeout_seconds: 超时覆盖（秒）。

    Returns:
        ``(状态, 说明)``——状态为 PASS / SKIP / FAIL。
    """
    asr = settings.voice.asr
    if not asr.enabled or not asr.streaming_enabled:
        return (
            "SKIP",
            "无凭据 / 未启用（VOICE_ASR_ENABLED=false 或 "
            "VOICE_ASR_STREAMING_ENABLED=false）",
        )
    client = Qwen3AsrStreamClient(
        make_websocket_connector(
            asr.streaming_base_url, open_timeout_seconds=timeout_seconds
        ),
        endpoint=asr.streaming_base_url,
        model=asr.streaming_model,
        timeout_seconds=timeout_seconds,
    )

    async def _on_partial(text: str) -> None:
        """进行中转写（探测只验证不回显）。"""
        return None

    try:
        handle = await client.open(
            sample_rate_hz=_PROBE_SAMPLE_RATE_HZ,
            language=asr.language,
            on_partial=_on_partial,
        )
        await handle.feed(_tone_frame())
        result = await handle.finish()
    except BusinessException as exc:
        return (
            "FAIL",
            f"endpoint={asr.streaming_base_url} reason={exc.message} "
            f"details={exc.details}",
        )
    except Exception as exc:  # 探测面兜底：任何意外都归 FAIL（不中断另一子项）
        return (
            "FAIL",
            f"endpoint={asr.streaming_base_url} reason={type(exc).__name__}: {exc}",
        )
    preview = result.text.strip()[:40]
    return (
        "PASS",
        f"endpoint={asr.streaming_base_url} model={asr.streaming_model} "
        f"final_text={preview!r}",
    )


async def _probe_tts(settings: Settings, *, timeout_seconds: float) -> tuple[str, str]:
    """探测流式合成通道（握手 + 首分片 + 取消）。

    Args:
        settings: 生效配置。
        timeout_seconds: 超时覆盖（秒）。

    Returns:
        ``(状态, 说明)``——状态为 PASS / SKIP / FAIL。
    """
    tts = settings.voice.tts
    if not tts.enabled or not tts.streaming_enabled:
        return (
            "SKIP",
            "无凭据 / 未启用（VOICE_TTS_ENABLED=false 或 "
            "VOICE_TTS_STREAMING_ENABLED=false）",
        )
    client = CosyVoiceStreamClient(
        make_websocket_connector(
            tts.streaming_base_url, open_timeout_seconds=timeout_seconds
        ),
        endpoint=tts.streaming_base_url,
        model=tts.streaming_model,
        timeout_seconds=timeout_seconds,
        sample_rate_hz=tts.streaming_sample_rate_hz,
    )
    stream = client.synthesize_stream(
        _PROBE_TTS_TEXT, speaker=tts.default_speaker, speed=tts.default_speed
    )
    first_bytes = 0
    try:
        async for fragment in stream:
            first_bytes = len(fragment)
            break
    except BusinessException as exc:
        await stream.aclose()
        return (
            "FAIL",
            f"endpoint={tts.streaming_base_url} reason={exc.message} "
            f"details={exc.details}",
        )
    except Exception as exc:  # 探测面兜底
        await stream.aclose()
        return (
            "FAIL",
            f"endpoint={tts.streaming_base_url} reason={type(exc).__name__}: {exc}",
        )
    await stream.aclose()
    if first_bytes <= 0:
        return (
            "FAIL",
            f"endpoint={tts.streaming_base_url} reason=首个音频分片为空",
        )
    return (
        "PASS",
        f"endpoint={tts.streaming_base_url} model={tts.streaming_model} "
        f"first_chunk_bytes={first_bytes} cancel_ok=True",
    )


async def _run(*, only: str, timeout_seconds: float) -> int:
    """执行探测并打印结果。

    Args:
        only: ``all`` / ``asr`` / ``tts``（子项选择）。
        timeout_seconds: 超时覆盖（秒）。

    Returns:
        进程退出码（0 = 无 FAIL；1 = 存在 FAIL）。
    """
    settings = Settings()
    results: list[tuple[str, str, str]] = []
    if only in ("all", "asr"):
        status, detail = await _probe_asr(settings, timeout_seconds=timeout_seconds)
        results.append(("ASR", status, detail))
    if only in ("all", "tts"):
        status, detail = await _probe_tts(settings, timeout_seconds=timeout_seconds)
        results.append(("TTS", status, detail))
    for label, status, detail in results:
        print(f"[voice-streaming-probe] {label:<3} {status:<4} {detail}")
    failed = any(status == "FAIL" for _label, status, _detail in results)
    print(
        "[voice-streaming-probe] 结论: "
        + ("存在 FAIL（该子项降级——批量主链不受影响）" if failed else "全部可用或跳过")
    )
    return 1 if failed else 0


def main() -> int:
    """命令行入口。

    Returns:
        进程退出码（0 / 1 / 2）。
    """
    parser = argparse.ArgumentParser(
        description="语音流式通道部署探测（ASR / TTS 最小握手 + 一帧往返）"
    )
    parser.add_argument(
        "--only",
        choices=("all", "asr", "tts"),
        default="all",
        help="只探测指定子项（缺省 all）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="连接 / 握手 / 往返超时秒数（缺省 10）",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须为正数")
    return asyncio.run(_run(only=args.only, timeout_seconds=args.timeout))


if __name__ == "__main__":
    sys.exit(main())
