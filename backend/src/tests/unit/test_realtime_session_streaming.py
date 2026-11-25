"""实时会话双轨流式测试——流式 ASR / TTS 接线与回退（fake 传输 / fake 服务）。

覆盖矩阵：

    - ready 能力字段（aec / streaming_asr / streaming_tts）与开关一致性；
    - 流式 ASR：真 partial 下行 → final 落定 → 进入生成（不调批量）；
    - 握手失败 / 中途喂帧失败 / 收尾失败：一次告警 + 指标计数 + 回退批量；
    - 开关关闭：零流式握手、批量主链不变；
    - 流式 TTS：逐句流式合成 → WAV 分片下行（wire 契约不变）；
    - 流式 TTS 失败：回退整句批量（该句音频仍下发）；
    - barge-in：同时取消流式 TTS 连接与生成（无半句音频）；
    - 会话关闭：双轨连接全部回收（任务集合为空、句柄已取消）。

全部用例零真实网络 / 零固定等待：出站消息经事件谓词等待，句子用脚本
队列投喂。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.config.models import VoiceSettings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.voice.realtime_models import RealtimeVoiceSettings
from packages.harness.voice.realtime_session import RealtimeVoiceSession
from tests.realtime_fakes import (
    FakeClock,
    FakeRealtimeTransport,
    FakeVoiceService,
    ScriptedChatStream,
    audio_message,
    loud_frame,
    parked_sleeper,
    pcm_frame,
    persona_record,
    realtime_settings,
    silence_frame,
    start_message,
    tts_failure,
    voice_settings,
    wait_until,
)

#: 测试等待预算秒数（只用于兜底，不参与业务时序）。
_WAIT_TIMEOUT: float = 5.0


class StreamSessionHarness:
    """双轨会话测试装配体（流式开关经 fake 服务旋钮控制）。"""

    def __init__(
        self,
        *,
        stream_asr: bool = False,
        stream_tts: bool = False,
        aec_enabled: bool = True,
        **settings_overrides: Any,
    ) -> None:
        self.clock = FakeClock()
        self.transport = FakeRealtimeTransport()
        self.voice = FakeVoiceService()
        self.voice.stream_asr_enabled = stream_asr
        self.voice.stream_tts_enabled = stream_tts
        self.chat = ScriptedChatStream()
        self.persona = persona_record()
        self.settings: RealtimeVoiceSettings = realtime_settings(
            aec_enabled=aec_enabled, **settings_overrides
        )
        self.voice_config: VoiceSettings = voice_settings()
        self.session: RealtimeVoiceSession | None = None
        self.task: asyncio.Task[None] | None = None

    async def boot(self) -> None:
        """创建会话并推到 ready。"""
        self.session = RealtimeVoiceSession(
            transport=self.transport,
            realtime_settings=self.settings,
            voice_settings=self.voice_config,
            chat=self.chat,
            voice_service=self.voice,
            persona=self.persona,
            clock=self.clock,
            sleeper=parked_sleeper,
            watchdog_tick_seconds=0.01,
        )
        self.task = asyncio.create_task(self.session.run())
        await self.transport.push(start_message())
        await self.wait_for_type("ready")

    async def push_frames(self, frames: list[bytes], *, start_seq: int) -> None:
        """按序投递音频帧。"""
        for offset, frame in enumerate(frames):
            await self.transport.push(audio_message(start_seq + offset, frame))

    async def wait_for_type(
        self, message_type: str, *, count: int = 1
    ) -> list[dict[str, Any]]:
        """等待指定类型下行消息累计出现 count 条。"""

        def _predicate(sent: list[dict[str, Any]]) -> bool:
            return sum(1 for message in sent if message.get("type") == message_type) >= count

        return await self.transport.wait_for(_predicate, timeout=_WAIT_TIMEOUT)

    def messages(self, message_type: str) -> list[dict[str, Any]]:
        """按类型取已下行消息。"""
        return self.transport.messages_of(message_type)

    async def request_end(self) -> None:
        """请求结束并等待会话收尾。"""
        assert self.task is not None
        if not self.task.done():
            await self.transport.push({"type": "end", "ts_ms": 0})
        await asyncio.wait_for(self.task, timeout=_WAIT_TIMEOUT)


async def _start_segment_with_stream(harness: StreamSessionHarness) -> None:
    """推两帧高声（起说）并等待流式识别握手完成。

    Args:
        harness: 双轨装配体（须已 boot 且启用流式 ASR）。
    """
    await harness.push_frames([loud_frame(), loud_frame()], start_seq=0)
    assert harness.session is not None
    await wait_until(
        lambda: harness.session.metrics.stream_asr_connections >= 1,
        description="流式识别握手",
    )


async def test_ready_declares_stream_and_aec_capabilities() -> None:
    """ready：aec / streaming_asr / streaming_tts 与配置一致。"""
    harness = StreamSessionHarness(stream_asr=True, stream_tts=False)
    await harness.boot()
    ready = harness.messages("ready")[0]
    assert ready["aec"] is True
    assert ready["streaming_asr"] is True
    assert ready["streaming_tts"] is False
    await harness.request_end()


async def test_ready_aec_disabled_flag() -> None:
    """AEC 公告可显式降级（无 AEC 调试环境）。"""
    harness = StreamSessionHarness(aec_enabled=False)
    await harness.boot()
    assert harness.messages("ready")[0]["aec"] is False
    await harness.request_end()


async def test_stream_asr_partials_and_final_drive_generation() -> None:
    """流式 ASR：真 partial 下行 → final → 生成；批量转写零调用。"""
    harness = StreamSessionHarness(stream_asr=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    await harness.voice.push_partial("你好")
    await harness.wait_for_type("partial_transcript")
    assert harness.messages("partial_transcript")[0]["text"] == "你好"
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await harness.wait_for_type("final_transcript")
    transcript = harness.messages("final_transcript")[0]
    assert transcript["text"] == "流式转写"
    assert harness.voice.asr_calls == []
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_connections == 1
    assert harness.session.metrics.stream_asr_fallbacks == 0
    assert harness.voice.asr_stream_handle is not None
    assert harness.voice.asr_stream_handle.finished is True
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session.active_task_count == 0


async def test_stream_asr_handshake_failure_falls_back_to_batch() -> None:
    """握手失败：指标计数 + 回退批量转写（会话不中断）。"""
    harness = StreamSessionHarness(stream_asr=True)
    harness.voice.asr_stream_error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="流式握手失败"
    )
    await harness.boot()
    await harness.push_frames(
        [loud_frame(), loud_frame(), silence_frame()], start_seq=0
    )
    await harness.wait_for_type("final_transcript")
    assert harness.messages("final_transcript")[0]["text"] == "你好呀"
    assert harness.voice.asr_calls
    assert harness.voice.asr_stream_open_calls
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_fallbacks == 1
    assert harness.session.metrics.stream_asr_connections == 0
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session.active_task_count == 0


async def test_stream_asr_budget_rejection_does_not_degrade() -> None:
    """预算拒绝（RATE_LIMITED）：不降级、不计数、不告警；语段走批量并由闸收尾。"""
    harness = StreamSessionHarness(stream_asr=True)
    harness.voice.asr_stream_error = BusinessException(
        code=ErrorCode.RATE_LIMITED, message="ASR 日预算超限"
    )
    await harness.boot()
    for round_index in range(2):
        await harness.push_frames(
            [loud_frame(), loud_frame(), silence_frame()], start_seq=round_index * 3
        )
        await harness.wait_for_type("final_transcript", count=round_index + 1)
    assert len(harness.voice.asr_stream_open_calls) == 2
    assert harness.voice.asr_calls
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_fallbacks == 0
    assert harness.session.metrics.stream_asr_connections == 0
    await harness.chat.finish()
    await harness.request_end()


async def test_stream_asr_feed_failure_falls_back_to_batch() -> None:
    """中途喂帧失败（断流）：降级后该语段走批量转写。"""
    harness = StreamSessionHarness(stream_asr=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    assert harness.voice.asr_stream_handle is not None
    harness.voice.asr_stream_handle.feed_error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="流式断流"
    )
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await harness.wait_for_type("final_transcript")
    assert harness.messages("final_transcript")[0]["text"] == "你好呀"
    assert harness.voice.asr_calls
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_fallbacks == 1
    await harness.chat.finish()
    await harness.request_end()


async def test_stream_asr_finish_failure_falls_back_to_batch() -> None:
    """收尾失败：该语段回退批量转写（一次告警 + 指标）。"""
    harness = StreamSessionHarness(stream_asr=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    assert harness.voice.asr_stream_handle is not None
    harness.voice.asr_stream_handle.finish_error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="流式收尾失败"
    )
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await harness.wait_for_type("final_transcript")
    assert harness.messages("final_transcript")[0]["text"] == "你好呀"
    assert harness.voice.asr_calls
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_fallbacks == 1
    await harness.chat.finish()
    await harness.request_end()


async def test_stream_disabled_uses_batch_only() -> None:
    """开关关闭：零流式握手，批量主链照常。"""
    harness = StreamSessionHarness(stream_asr=False, stream_tts=False)
    await harness.boot()
    await harness.push_frames(
        [loud_frame(), loud_frame(), silence_frame()], start_seq=0
    )
    await harness.wait_for_type("final_transcript")
    assert harness.voice.asr_stream_open_calls == []
    assert harness.voice.tts_stream_calls == []
    assert harness.voice.asr_calls
    assert harness.session is not None
    assert harness.session.metrics.stream_asr_connections == 0
    await harness.chat.finish()
    await harness.request_end()


async def test_stream_tts_synthesizes_sentences_over_stream() -> None:
    """流式 TTS：逐句流式合成 → WAV 分片下行（wire 契约不变）。"""
    harness = StreamSessionHarness(stream_tts=True)
    await harness.boot()
    await harness.push_frames(
        [loud_frame(), loud_frame(), silence_frame()], start_seq=0
    )
    await harness.wait_for_type("final_transcript")
    await harness.chat.push("你好。")
    await harness.chat.finish()
    await harness.wait_for_type("audio_chunk")
    chunk = harness.messages("audio_chunk")[0]
    assert chunk["content_type"] == "audio/wav"
    assert harness.voice.tts_stream_calls == ["你好。"]
    assert harness.voice.tts_calls == []
    assert harness.session is not None
    assert harness.session.metrics.stream_tts_sentences == 1
    await harness.request_end()


async def test_stream_tts_failure_falls_back_to_batch_sentence() -> None:
    """流式合成失败：降级该句批量合成（音频仍下发）。"""
    harness = StreamSessionHarness(stream_tts=True)
    harness.voice.tts_stream_error = tts_failure()
    await harness.boot()
    await harness.push_frames(
        [loud_frame(), loud_frame(), silence_frame()], start_seq=0
    )
    await harness.wait_for_type("final_transcript")
    await harness.chat.push("你好。")
    await harness.chat.finish()
    await harness.wait_for_type("audio_chunk")
    assert harness.voice.tts_stream_calls == ["你好。"]
    assert harness.voice.tts_calls == ["你好。"]
    assert harness.session is not None
    assert harness.session.metrics.stream_tts_fallbacks == 1
    await harness.request_end()


async def test_barge_in_cancels_streaming_tts_and_generation() -> None:
    """barge-in：取消在途生成与流式合成（门闩上的分片都不下行）。"""
    harness = StreamSessionHarness(stream_tts=True)
    await harness.boot()
    await harness.push_frames(
        [loud_frame(), loud_frame(), silence_frame()], start_seq=0
    )
    await harness.wait_for_type("final_transcript")
    harness.voice.tts_stream_gate = asyncio.Event()
    await harness.chat.push("第一句话，很长的一句。")
    await wait_until(
        lambda: harness.voice.tts_stream_calls != [], description="流式合成启动"
    )
    await harness.transport.push({"type": "barge_in", "ts_ms": 0, "reason": "manual"})
    await harness.wait_for_type("interrupted")
    assert harness.messages("audio_chunk") == []
    assert harness.chat.finished is True
    harness.voice.tts_stream_gate.set()
    await harness.chat.push("好的。")
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.interruptions == 1
    assert harness.session.active_task_count == 0


async def test_session_close_settles_streaming_asr_and_clears_tasks() -> None:
    """关闭排空：尾段经流式收尾落定（非泄漏），任务集合清空。"""
    harness = StreamSessionHarness(stream_asr=True)
    harness.voice.stream_tts_enabled = True
    await harness.boot()
    await _start_segment_with_stream(harness)
    await harness.chat.push("好。")
    await harness.chat.finish()
    await harness.request_end()
    assert harness.voice.asr_stream_handle is not None
    assert harness.voice.asr_stream_handle.finished is True
    assert harness.session is not None
    assert harness.session.active_task_count == 0
    assert harness.messages("closed")[0]["reason"] == "client_end"


async def test_external_cancel_during_stream_finish_cancels_handle_and_clears_tasks() -> None:
    """外部取消（服务停机）落在 finish 挂起时：句柄收口、任务集合清零。"""
    harness = StreamSessionHarness(stream_asr=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    handle = harness.voice.asr_stream_handle
    assert handle is not None
    handle.finish_gate = asyncio.Event()
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await wait_until(lambda: handle.finish_started.is_set(), description="流式收尾挂起")
    assert harness.task is not None
    harness.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(harness.task, timeout=_WAIT_TIMEOUT)
    assert handle.cancelled is True
    assert handle.finished is False
    assert harness.session is not None
    assert harness.session.active_task_count == 0
    assert harness.messages("final_transcript") == []


async def test_transport_close_during_stream_finish_cancels_handle_and_clears_tasks() -> None:
    """对端断开落在 finish 挂起时：会话关闭路径同样收口句柄（零残留）。"""
    harness = StreamSessionHarness(stream_asr=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    handle = harness.voice.asr_stream_handle
    assert handle is not None
    handle.finish_gate = asyncio.Event()
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await wait_until(lambda: handle.finish_started.is_set(), description="流式收尾挂起")
    assert harness.task is not None
    await harness.transport.push_disconnect()
    await asyncio.wait_for(harness.task, timeout=_WAIT_TIMEOUT)
    assert handle.cancelled is True
    assert harness.session is not None
    assert harness.session.active_task_count == 0
    assert harness.transport.closed is not None


async def test_stream_asr_handshake_buffer_replays_last_fifty_once() -> None:
    """握手窗口 >50 帧：只回放最后 50 帧且每帧恰一次（有界缓冲）。"""
    harness = StreamSessionHarness(
        stream_asr=True, vad_max_segment_ms=60_000, audio_queue_max_chunks=128
    )
    harness.voice.asr_stream_open_gate = asyncio.Event()
    await harness.boot()
    frames = [pcm_frame(6000 + index) for index in range(60)]
    await harness.push_frames(frames, start_seq=0)
    session = harness.session
    assert session is not None
    await wait_until(
        lambda: (pending := session._stream_asr_pending) is not None
        and len(pending) == 50
        and pending[-1] == frames[-1]
        and session._audio_queue.empty(),
        description="握手窗口缓冲",
    )
    harness.voice.asr_stream_open_gate.set()
    await wait_until(
        lambda: bool(harness.voice.asr_stream_handles), description="流式识别握手完成"
    )
    handle = harness.voice.asr_stream_handles[0]
    await wait_until(lambda: len(handle.fed) == 50, description="缓冲回放完成")
    assert handle.fed == frames[-50:]
    assert len(set(handle.fed)) == 50
    await harness.chat.finish()
    await harness.request_end()


async def test_stream_asr_handshake_replay_feeds_stream_once_only() -> None:
    """缓冲回放只喂流式识别一次：不重复喂 VAD、不重复喂流。"""
    harness = StreamSessionHarness(stream_asr=True, vad_max_segment_ms=60_000)
    harness.voice.asr_stream_open_gate = asyncio.Event()
    await harness.boot()
    frames = [pcm_frame(7000 + index) for index in range(6)]
    await harness.push_frames(frames, start_seq=0)
    session = harness.session
    assert session is not None
    await wait_until(
        lambda: (pending := session._stream_asr_pending) is not None
        and len(pending) == len(frames) - 1
        and pending[-1] == frames[-1]
        and session._audio_queue.empty(),
        description="握手窗口缓冲",
    )
    harness.voice.asr_stream_open_gate.set()
    await wait_until(
        lambda: bool(harness.voice.asr_stream_handles)
        and len(harness.voice.asr_stream_handles[0].fed) == len(frames) - 1,
        description="缓冲回放完成",
    )
    handle = harness.voice.asr_stream_handles[0]
    assert handle.fed == frames[1:]
    assert session.metrics.speech_starts == 1
    assert session.metrics.audio_chunks_received == len(frames)
    await harness.chat.finish()
    await harness.request_end()


async def test_barge_in_cancels_inflight_stream_asr_and_tts() -> None:
    """在途流式 ASR 句柄 + 流式 TTS 连接同时存在：barge-in 两者均取消。"""
    harness = StreamSessionHarness(stream_asr=True, stream_tts=True)
    await harness.boot()
    await _start_segment_with_stream(harness)
    first_handle = harness.voice.asr_stream_handle
    assert first_handle is not None
    first_handle.finish_gate = asyncio.Event()
    await harness.push_frames([loud_frame(), silence_frame()], start_seq=2)
    await harness.push_frames([loud_frame(), loud_frame()], start_seq=4)
    await wait_until(
        lambda: len(harness.voice.asr_stream_handles) == 2,
        description="下一语段流式连接",
    )
    second_handle = harness.voice.asr_stream_handles[1]
    first_handle.finish_gate.set()
    await harness.wait_for_type("final_transcript")
    harness.voice.tts_stream_gate = asyncio.Event()
    await harness.chat.push("第一句话，很长的一句。")
    await wait_until(
        lambda: harness.voice.tts_stream_calls != [], description="流式合成启动"
    )
    await harness.transport.push({"type": "barge_in", "ts_ms": 0, "reason": "manual"})
    await harness.wait_for_type("interrupted", count=2)
    assert second_handle.cancelled is True
    assert harness.voice.tts_stream_closed == 1
    assert harness.messages("audio_chunk") == []
    harness.voice.tts_stream_gate.set()
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.active_task_count == 0


async def test_session_close_cancels_open_stream_on_discarded_segment() -> None:
    """关闭时尾段被丢弃：进行中的流式连接被取消（零残留）。"""
    harness = StreamSessionHarness(
        stream_asr=True, vad_min_speech_ms=500, vad_max_segment_ms=1000
    )
    await harness.boot()
    await _start_segment_with_stream(harness)
    await harness.request_end()
    assert harness.voice.asr_stream_handle is not None
    assert harness.voice.asr_stream_handle.cancelled is True
    assert harness.voice.asr_stream_handle.finished is False
    assert harness.session is not None
    assert harness.session.metrics.segments_discarded == 1
    assert harness.session.active_task_count == 0
