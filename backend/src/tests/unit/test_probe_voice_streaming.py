"""部署探测脚本测试——PASS / SKIP / FAIL 三态（回环服务端，零外网）。

被测对象为根 ``scripts/probe_voice_streaming.py`` 的探测函数（单次握手 +
一帧往返 + 取消）。回环服务端按流式客户端契约最小应答；失败面用未监听
端口构造。全部异步、无固定等待。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from probe_voice_streaming import _probe_asr, _probe_tts
from websockets.asyncio.server import ServerConnection, serve

from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings


async def _streaming_handler(connection: ServerConnection) -> None:
    """最小流式服务端：ASR 握手 / 收尾与 TTS 分片 / 完成事件应答。"""
    async for message in connection:
        if not isinstance(message, str):
            continue
        payload: dict[str, Any] = json.loads(message)
        event = payload.get("type")
        if event == "session.update":
            await connection.send(json.dumps({"type": "session.updated"}))
        elif event == "input_audio_buffer.append":
            await connection.send(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.text",
                        "text": "你",
                    }
                )
            )
        elif event == "input_audio_buffer.commit":
            await connection.send(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "你好",
                    }
                )
            )
        elif event == "session.finish":
            await connection.send(json.dumps({"type": "session.finished"}))
        elif event == "tts.start":
            await connection.send(
                json.dumps({"type": "tts.delta", "audio": "AQACAA=="})
            )
        elif event == "tts.cancel":
            continue


@pytest.fixture
async def loopback_url() -> AsyncIterator[str]:
    """回环流式服务端点 fixture。

    Yields:
        ``ws://127.0.0.1:<port>/stream`` 端点。
    """
    async with serve(_streaming_handler, "127.0.0.1", 0) as server:
        sockets = server.sockets
        assert sockets
        port = sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/stream"


def _settings(url: str, *, enabled: bool = True, asr_enabled: bool = True) -> Any:
    """构造指向回环端点的隔离 Settings（流式开关形态）。"""
    return build_isolated_settings(
        voice=VoiceSettings(
            tts=TtsSettings(
                enabled=enabled,
                streaming_enabled=enabled,
                streaming_base_url=url,
                streaming_model="cosyvoice3",
            ),
            asr=AsrSettings(
                enabled=asr_enabled,
                streaming_enabled=asr_enabled,
                streaming_base_url=url,
                streaming_model="qwen3-asr-flash-realtime",
            ),
        )
    )


async def test_probe_asr_passes_against_contract_server(loopback_url: str) -> None:
    """ASR 探测：握手 + 一帧 + 收尾成功 → PASS（含端点）。"""
    status, detail = await _probe_asr(_settings(loopback_url), timeout_seconds=2.0)
    assert status == "PASS"
    assert loopback_url in detail
    assert "你好" in detail


async def test_probe_tts_passes_and_cancels(loopback_url: str) -> None:
    """TTS 探测：首分片到达且取消成功 → PASS。"""
    status, detail = await _probe_tts(_settings(loopback_url), timeout_seconds=2.0)
    assert status == "PASS"
    assert "first_chunk_bytes=4" in detail
    assert "cancel_ok=True" in detail


async def test_probe_skips_when_disabled() -> None:
    """未启用：SKIP（无凭据 / 开关关闭），不发起连接。"""
    settings = _settings("ws://127.0.0.1:1/stream", enabled=False, asr_enabled=False)
    asr_status, asr_detail = await _probe_asr(settings, timeout_seconds=0.5)
    tts_status, tts_detail = await _probe_tts(settings, timeout_seconds=0.5)
    assert asr_status == "SKIP"
    assert tts_status == "SKIP"
    assert "STREAMING_ENABLED=false" in asr_detail
    assert "STREAMING_ENABLED=false" in tts_detail


async def test_probe_fails_on_unreachable_endpoint() -> None:
    """端点不可达：FAIL（携带端点与原因），不抛异常。"""
    settings = _settings("ws://127.0.0.1:1/stream")
    asr_status, asr_detail = await _probe_asr(settings, timeout_seconds=0.5)
    tts_status, tts_detail = await _probe_tts(settings, timeout_seconds=0.5)
    assert asr_status == "FAIL"
    assert tts_status == "FAIL"
    assert "ws://127.0.0.1:1/stream" in asr_detail
    assert "ws://127.0.0.1:1/stream" in tts_detail


async def test_probe_timeout_is_bounded() -> None:
    """超时受控：极短超时下探测返回 FAIL 而不是挂起。"""
    settings = _settings("ws://127.0.0.1:1/stream")
    status, detail = await asyncio.wait_for(
        _probe_tts(settings, timeout_seconds=0.05), timeout=5.0
    )
    assert status == "FAIL"
    assert "reason=" in detail
