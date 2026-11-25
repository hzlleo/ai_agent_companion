"""voice 域流式通道客户端测试——ASR / TTS 协议面（fake 传输，零真实网络）。

覆盖矩阵：

    - ASR：握手事件形状 / 握手确认 / 握手拒绝 / 握手超时 / partial 真增量
      回调 / final 落定 / 收尾事件序 / 中途断流（关闭码）/ 收尾超时 /
      取消（cancel 事件 + 关闭码）/ 喂帧队列溢出丢最旧 / 关闭后喂帧拒绝 /
      保活 ping / 非法事件 JSON / 采样率越界 / 连接失败映射；
    - TTS：分片顺序与字节一致性 / 完成事件收尾 / 消费侧取消（aclose 发
      cancel + 关闭）/ 服务端 error 事件 / 中途断流 / 文本越界零建连 /
      非法 base64 分片 / instruct 指令透传。

全部用例经 :class:`tests.realtime_fakes.FakeStreamTransport` 驱动；等待
经事件谓词轮询（零固定 sleep），超时用例用小超时预算。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.voice.asr_stream_client import (
    ASR_STREAM_EVENT_APPEND,
    ASR_STREAM_EVENT_CANCEL,
    ASR_STREAM_EVENT_COMMIT,
    ASR_STREAM_EVENT_FINISH,
    ASR_STREAM_EVENT_SESSION_UPDATE,
    AsrStreamHandle,
    Qwen3AsrStreamClient,
)
from packages.harness.voice.stream_models import (
    STREAM_CLOSE_GOING_AWAY,
    STREAM_CLOSE_NORMAL,
    STREAM_CLOSE_PROTOCOL_ERROR,
    VoiceStreamFrameModel,
)
from packages.harness.voice.stream_transport import make_websocket_connector
from packages.harness.voice.tts_stream_client import (
    TTS_STREAM_EVENT_CANCEL,
    TTS_STREAM_EVENT_START,
    CosyVoiceStreamClient,
)
from tests.realtime_fakes import (
    FakeStreamTransport,
    make_stream_connector,
    wait_until,
)

#: 一帧假 PCM16（16 字节——内容任意，协议层只做 base64 透传）。
_PCM_FRAME: bytes = b"\x01\x00\x02\x00\x03\x00\x04\x00"


def _asr_client(
    transport: FakeStreamTransport,
    *,
    timeout_seconds: float = 1.0,
    heartbeat_interval_seconds: float = 60.0,
    send_queue_max: int = 32,
) -> Qwen3AsrStreamClient:
    """构造绑定 fake 传输的流式识别客户端。

    Args:
        transport: fake 传输。
        timeout_seconds: 握手 / 收尾超时。
        heartbeat_interval_seconds: 保活间隔。
        send_queue_max: 喂帧队列上限。

    Returns:
        流式识别客户端。
    """
    return Qwen3AsrStreamClient(
        make_stream_connector(transport),
        endpoint="ws://asr.test/v1/realtime",
        model="qwen3-asr-flash-realtime",
        timeout_seconds=timeout_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        send_queue_max=send_queue_max,
    )


def _tts_client(
    transport: FakeStreamTransport, *, timeout_seconds: float = 1.0
) -> CosyVoiceStreamClient:
    """构造绑定 fake 传输的流式合成客户端。

    Args:
        transport: fake 传输。
        timeout_seconds: 连接 / 分片超时。

    Returns:
        流式合成客户端。
    """
    return CosyVoiceStreamClient(
        make_stream_connector(transport),
        endpoint="ws://tts.test/realtime",
        model="cosyvoice3",
        timeout_seconds=timeout_seconds,
        sample_rate_hz=24000,
    )


async def _open_asr(
    transport: FakeStreamTransport, *, timeout_seconds: float = 1.0
) -> tuple[Qwen3AsrStreamClient, list[str], AsrStreamHandle]:
    """完成一次握手（预置 ack 事件）并返回客户端与 partial 记录。

    Args:
        transport: fake 传输。
        timeout_seconds: 握手超时。

    Returns:
        ``(客户端, partial 文本记录)``。
    """
    partials: list[str] = []

    async def _on_partial(text: str) -> None:
        partials.append(text)

    await transport.push_event({"type": "session.updated"})
    client = _asr_client(transport, timeout_seconds=timeout_seconds)
    handle = await client.open(
        sample_rate_hz=16000, language=None, on_partial=_on_partial
    )
    return client, partials, handle


# ---------------------------------------------------------------------------
# 流式 ASR
# ---------------------------------------------------------------------------


async def test_asr_handshake_sends_session_update_and_accepts_ack() -> None:
    """握手：下发 session.update（模型 / PCM16 / 采样率）并等待确认。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    updates = transport.events_of(ASR_STREAM_EVENT_SESSION_UPDATE)
    assert len(updates) == 1
    session = updates[0]["session"]
    assert session["model"] == "qwen3-asr-flash-realtime"
    assert session["input_audio_format"] == "pcm16"
    assert session["sample_rate_hz"] == 16000
    assert session["language"] is None
    await handle.cancel()


async def test_asr_handshake_rejection_raises_and_closes_protocol_error() -> None:
    """握手被拒：LLM_CALL_FAILED + 协议错误关闭码（1002）。"""
    transport = FakeStreamTransport()
    await transport.push_event(
        {"type": "error", "error": {"code": "InvalidParameter", "message": "bad model"}}
    )
    client = _asr_client(transport)

    async def _on_partial(text: str) -> None:
        return None

    with pytest.raises(BusinessException) as excinfo:
        await client.open(sample_rate_hz=16000, language="zh", on_partial=_on_partial)
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert excinfo.value.details["server_error"] == "InvalidParameter: bad model"
    assert transport.closed == (STREAM_CLOSE_PROTOCOL_ERROR, "handshake_failed")


async def test_asr_handshake_timeout_raises_and_closes() -> None:
    """握手确认超时：LLM_CALL_FAILED（超时类别）+ 关闭连接。"""
    transport = FakeStreamTransport()
    client = _asr_client(transport, timeout_seconds=0.02)

    async def _on_partial(text: str) -> None:
        return None

    with pytest.raises(BusinessException) as excinfo:
        await client.open(sample_rate_hz=16000, language=None, on_partial=_on_partial)
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert "握手确认超时" in excinfo.value.message
    assert transport.closed is not None


async def test_asr_partial_deltas_and_final_settle() -> None:
    """喂帧 → 真 partial 增量 → final 落定；收尾事件序与正常关闭码。"""
    transport = FakeStreamTransport()
    _, partials, handle = await _open_asr(transport)
    await handle.feed(_PCM_FRAME)
    await wait_until(
        lambda: len(transport.events_of(ASR_STREAM_EVENT_APPEND)) == 1,
        description="音频分片出站",
    )
    await transport.push_event(
        {
            "type": "conversation.item.input_audio_transcription.text",
            "text": "你",
        }
    )
    await transport.push_event(
        {
            "type": "conversation.item.input_audio_transcription.text",
            "text": "你好",
        }
    )
    await transport.push_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "你好呀",
        }
    )
    result = await handle.finish()
    assert result.text == "你好呀"
    assert partials == ["你", "你好"]
    assert transport.events_of(ASR_STREAM_EVENT_COMMIT)
    assert transport.events_of(ASR_STREAM_EVENT_FINISH)
    assert transport.events_of(ASR_STREAM_EVENT_CANCEL) == []
    assert transport.closed == (STREAM_CLOSE_NORMAL, "session_finished")


async def test_asr_finish_keeps_queued_appends_before_commit_and_finish() -> None:
    """喂帧后立即 finish：append 全部先出站，commit / session.finish 严格在后。

    发送工人在每次发送前让出事件循环（复现真实网络并发窗口），``finish``
    不得抢先发收尾事件（否则服务端会在空缓冲上提交），最终稿仍须落定。
    """
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    for _ in range(4):
        await handle.feed(_PCM_FRAME)
    finish_task = asyncio.create_task(handle.finish())
    await wait_until(
        lambda: transport.events_of(ASR_STREAM_EVENT_FINISH) != [],
        description="收尾事件出站",
    )
    await transport.push_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "你好呀",
        }
    )
    result = await finish_task
    types = [event["type"] for event in transport.events()]
    append_positions = [
        index for index, event_type in enumerate(types) if event_type == ASR_STREAM_EVENT_APPEND
    ]
    commit_index = types.index(ASR_STREAM_EVENT_COMMIT)
    finish_index = types.index(ASR_STREAM_EVENT_FINISH)
    assert len(append_positions) == 4
    assert commit_index > max(append_positions)
    assert finish_index > commit_index
    assert result.text == "你好呀"


async def test_asr_finish_cancelled_closes_connection_and_clears_tasks() -> None:
    """finish 挂起时外部取消：取消事件出站、传输关闭、派生任务清零。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    finish_task = asyncio.create_task(handle.finish())
    await wait_until(
        lambda: transport.events_of(ASR_STREAM_EVENT_FINISH) != [],
        description="收尾事件出站",
    )
    finish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish_task
    assert transport.events_of(ASR_STREAM_EVENT_CANCEL)
    assert transport.closed == (STREAM_CLOSE_NORMAL, "client_cancel")
    assert transport.close_calls == 1
    assert handle._tasks == set()
    assert handle._closed is True
    await handle.cancel()
    assert transport.close_calls == 1


async def test_asr_midstream_close_fails_with_close_code() -> None:
    """收尾前对端关闭：失败上抛并携带关闭码（回退批量依据）。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    await transport.push_close(1011)
    with pytest.raises(BusinessException) as excinfo:
        await handle.finish()
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert excinfo.value.details["close_code"] == 1011


async def test_asr_settle_timeout_raises_and_closes_going_away() -> None:
    """收尾等待超时：失败 + 1001 关闭（连接不再占用）。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport, timeout_seconds=0.02)
    with pytest.raises(BusinessException) as excinfo:
        await handle.finish()
    assert "收尾等待超时" in excinfo.value.message
    assert transport.closed == (STREAM_CLOSE_GOING_AWAY, "settle_timeout")


async def test_asr_cancel_sends_cancel_event_and_is_idempotent() -> None:
    """取消：发 session.cancel + 正常关闭；重复取消不重复关闭。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    await handle.cancel()
    await handle.cancel()
    assert transport.events_of(ASR_STREAM_EVENT_CANCEL)
    assert transport.closed == (STREAM_CLOSE_NORMAL, "client_cancel")
    assert transport.close_calls == 1


async def test_asr_send_queue_overflow_drops_oldest_frame() -> None:
    """喂帧队列满：丢最旧帧并计数（实时宁丢旧帧不阻塞收帧）。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    # 1 帧上限的独立客户端：两帧连投必丢最旧（feed 内无 await，发送工人无机会出队）。
    transport2 = FakeStreamTransport()
    limited = _asr_client(transport2, send_queue_max=1)
    await transport2.push_event({"type": "session.created"})

    async def _on_partial(text: str) -> None:
        return None

    handle2 = await limited.open(
        sample_rate_hz=16000, language=None, on_partial=_on_partial
    )
    await handle2.feed(_PCM_FRAME)
    await handle2.feed(b"\x05\x00\x06\x00")
    assert handle2.frames_dropped == 1
    await handle2.cancel()
    await handle.cancel()


async def test_asr_feed_after_close_raises() -> None:
    """关闭后继续喂帧：LLM_CALL_FAILED（调用方 bug 面，会话层触发回退）。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    await handle.cancel()
    with pytest.raises(BusinessException) as excinfo:
        await handle.feed(_PCM_FRAME)
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED


async def test_asr_heartbeat_emits_pings() -> None:
    """保活：连接期间按间隔发送协议层 ping。"""
    transport = FakeStreamTransport()
    await transport.push_event({"type": "session.updated"})
    client = _asr_client(transport, heartbeat_interval_seconds=0.01)

    async def _on_partial(text: str) -> None:
        return None

    handle = await client.open(
        sample_rate_hz=16000, language=None, on_partial=_on_partial
    )
    await wait_until(lambda: transport.pings >= 1, description="保活 ping")
    await handle.cancel()


async def test_asr_invalid_event_json_fails_settle() -> None:
    """非法事件 JSON：收尾失败（契约破坏面）。"""
    transport = FakeStreamTransport()
    _, _, handle = await _open_asr(transport)
    await transport.inbound.put(VoiceStreamFrameModel(kind="text", text="{oops"))
    with pytest.raises(BusinessException) as excinfo:
        await handle.finish()
    assert "事件 JSON 形态非法" in excinfo.value.message


async def test_asr_invalid_sample_rate_rejected_before_connect() -> None:
    """采样率非正：VALIDATION_ERROR 且不建连。"""
    transport = FakeStreamTransport()
    client = _asr_client(transport)

    async def _on_partial(text: str) -> None:
        return None

    with pytest.raises(BusinessException) as excinfo:
        await client.open(sample_rate_hz=0, language=None, on_partial=_on_partial)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert transport.sent == []


async def test_asr_connect_failure_maps_to_llm_call_failed() -> None:
    """连接失败：LLM_CALL_FAILED（含传输错误描述）。"""

    async def _connect() -> FakeStreamTransport:
        raise OSError("connection refused")

    client = Qwen3AsrStreamClient(
        _connect,
        endpoint="ws://asr.test/v1/realtime",
        model="m",
        timeout_seconds=0.5,
    )

    async def _on_partial(text: str) -> None:
        return None

    with pytest.raises(BusinessException) as excinfo:
        await client.open(sample_rate_hz=16000, language=None, on_partial=_on_partial)
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert "connection refused" in excinfo.value.details["error"]


async def test_asr_upgrade_rejected_maps_to_llm_call_failed() -> None:
    """升级被拒（HTTP 404）：传输归一 OSError → LLM_CALL_FAILED（details 含端点）。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    sockets = server.sockets
    assert sockets
    url = f"ws://127.0.0.1:{sockets[0].getsockname()[1]}/stream"
    client = Qwen3AsrStreamClient(
        make_websocket_connector(url, open_timeout_seconds=1.0),
        endpoint=url,
        model="m",
        timeout_seconds=1.0,
    )

    async def _on_partial(text: str) -> None:
        return None

    try:
        with pytest.raises(BusinessException) as excinfo:
            await client.open(sample_rate_hz=16000, language=None, on_partial=_on_partial)
    finally:
        server.close()
        await server.wait_closed()
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert excinfo.value.message == "流式语音识别失败: 流式识别连接失败"
    assert excinfo.value.details["endpoint"] == url
    assert "404" in excinfo.value.details["error"]


# ---------------------------------------------------------------------------
# 流式 TTS
# ---------------------------------------------------------------------------


async def test_tts_stream_yields_fragments_in_order_and_completes() -> None:
    """分片按序产出（字节一致）、完成事件收尾、正常关闭且不发 cancel。"""
    transport = FakeStreamTransport()
    await transport.push_event(
        {"type": "tts.delta", "audio": "AQACAA=="}
    )
    await transport.push_event(
        {"type": "tts.delta", "audio": "AwAEAA=="}
    )
    await transport.push_event({"type": "tts.completed"})
    client = _tts_client(transport)
    fragments = [
        fragment
        async for fragment in client.synthesize_stream(
            "你好", speaker="中文女", speed=1.1, instruct_text="温柔"
        )
    ]
    assert b"".join(fragments) == b"\x01\x00\x02\x00\x03\x00\x04\x00"
    starts = transport.events_of(TTS_STREAM_EVENT_START)
    assert starts[0]["text"] == "你好"
    assert starts[0]["speaker"] == "中文女"
    assert starts[0]["speed"] == 1.1
    assert starts[0]["instruct_text"] == "温柔"
    assert starts[0]["sample_rate_hz"] == 24000
    assert transport.events_of(TTS_STREAM_EVENT_CANCEL) == []
    assert transport.closed == (STREAM_CLOSE_NORMAL, "tts_stream_end")


async def test_tts_consumer_cancel_sends_cancel_and_closes() -> None:
    """消费侧取消（barge-in）：生成器关闭即发 tts.cancel 并关闭连接。"""
    transport = FakeStreamTransport()
    await transport.push_event({"type": "tts.delta", "audio": "AQACAA=="})
    client = _tts_client(transport)
    stream = client.synthesize_stream("你好")
    await stream.__anext__()
    await stream.aclose()
    assert transport.events_of(TTS_STREAM_EVENT_CANCEL)
    assert transport.closed is not None


async def test_tts_server_error_event_raises() -> None:
    """服务端 error 事件：LLM_CALL_FAILED（含服务端错误文本）。"""
    transport = FakeStreamTransport()
    await transport.push_event(
        {"type": "error", "error": {"code": "SynthesisError", "message": "boom"}}
    )
    client = _tts_client(transport)
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in client.synthesize_stream("你好"):
            pass
    assert excinfo.value.code == ErrorCode.LLM_CALL_FAILED
    assert excinfo.value.details["server_error"] == "SynthesisError: boom"
    assert transport.events_of(TTS_STREAM_EVENT_CANCEL)


async def test_tts_midstream_disconnect_raises_with_close_code() -> None:
    """中途断流：失败携带关闭码（调用方回退批量）。"""
    transport = FakeStreamTransport()
    await transport.push_event({"type": "tts.delta", "audio": "AQACAA=="})
    await transport.push_close(1001)
    client = _tts_client(transport)
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in client.synthesize_stream("你好"):
            pass
    assert excinfo.value.details["close_code"] == 1001


async def test_tts_invalid_text_rejected_without_connect() -> None:
    """文本越界：VALIDATION_ERROR 且零建连。"""
    transport = FakeStreamTransport()
    client = _tts_client(transport)
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in client.synthesize_stream(""):
            pass
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert transport.sent == []


async def test_tts_invalid_delta_base64_fails() -> None:
    """非法 base64 分片：LLM_CALL_FAILED（契约破坏面）。"""
    transport = FakeStreamTransport()
    await transport.push_event({"type": "tts.delta", "audio": "!!not-base64!!"})
    client = _tts_client(transport)
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in client.synthesize_stream("你好"):
            pass
    assert "base64" in excinfo.value.message


async def test_tts_sample_rate_property_exposes_config() -> None:
    """采样率访问器：会话层 WAV 封装取值来源。"""
    transport = FakeStreamTransport()
    client = _tts_client(transport)
    assert client.sample_rate_hz == 24000


async def test_tts_stream_chunks_with_await_between() -> None:
    """分片间事件循环让出（长连接不阻塞其他协程）。"""
    transport = FakeStreamTransport()
    for _ in range(3):
        await transport.push_event({"type": "tts.delta", "audio": "AQACAA=="})
    await transport.push_event({"type": "tts.completed"})
    client = _tts_client(transport)
    collected: list[bytes] = []

    async def _consume() -> None:
        async for fragment in client.synthesize_stream("你好"):
            collected.append(fragment)

    consumer = asyncio.create_task(_consume())
    await wait_until(lambda: len(collected) == 3, description="三片分片")
    await asyncio.wait_for(consumer, timeout=2.0)
