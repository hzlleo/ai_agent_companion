"""实时语音会话引擎测试——协议端到端（内存双工 / 脚本 fake / 伪时钟）。

覆盖面：会话建立与拒绝面、VAD 切段 → 转写 → 回复 → 逐句音频的完整链路、
中途转写节流、强制切段、手动 / VAD 打断（任务取消无泄漏）、打断关闭态、
背压丢最旧块、空闲 / 最大时长、客户端结束（尾段冲刷）、ASR / 生成 / TTS
失败的可恢复语义、TTS 关闭降级、传输断开、心跳应答。

全部用例零真实网络、零固定等待：出站消息经事件谓词等待，超时判定经
伪时钟 + 注入休眠面驱动。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.models import VoiceSettings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import ChatResponseModel, ChatStreamChunkModel
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.voice.realtime_models import (
    REALTIME_OUTBOUND_ADAPTER,
    RealtimeVoiceSettings,
)
from packages.harness.voice.realtime_session import (
    ChatSessionStreamAdapter,
    RealtimeVoiceSession,
)
from tests.realtime_fakes import (
    FakeClock,
    FakeRealtimeTransport,
    FakeVoiceService,
    ScriptedChatStream,
    audio_message,
    instant_sleeper,
    loud_frame,
    parked_sleeper,
    persona_record,
    realtime_settings,
    silence_frame,
    start_message,
    tts_failure,
    voice_settings,
)

#: 测试等待预算秒数（只用于兜底，不参与业务时序）。
_WAIT_TIMEOUT: float = 5.0


class SessionHarness:
    """会话测试装配体（传输 / 语音 / 聊天 / 时钟 / 会话任务一体）。"""

    def __init__(self, **settings_overrides: Any) -> None:
        self.clock = FakeClock()
        self.transport = FakeRealtimeTransport()
        self.voice = FakeVoiceService()
        self.chat = ScriptedChatStream()
        self.persona: PersonaRecordDTO | None = persona_record()
        self.settings: RealtimeVoiceSettings = realtime_settings(**settings_overrides)
        self.voice_config: VoiceSettings = voice_settings()
        self.session: RealtimeVoiceSession | None = None
        self.task: asyncio.Task[None] | None = None

    async def boot(
        self,
        *,
        sleeper: Any = parked_sleeper,
        asr_enabled: bool = True,
        tts_enabled: bool = True,
        push_start: bool = True,
    ) -> None:
        """创建会话并推入 start（缺省直到 ready 返回）。"""
        self.voice_config = voice_settings(asr=asr_enabled, tts=tts_enabled)
        self.session = RealtimeVoiceSession(
            transport=self.transport,
            realtime_settings=self.settings,
            voice_settings=self.voice_config,
            chat=self.chat,
            voice_service=self.voice,
            persona=self.persona,
            clock=self.clock,
            sleeper=sleeper,
            watchdog_tick_seconds=0.01,
        )
        self.task = asyncio.create_task(self.session.run())
        if push_start:
            await self.transport.push(start_message())
            await self.wait_for_type("ready")

    async def push(self, message: dict[str, Any]) -> None:
        """投递上行消息。"""
        await self.transport.push(message)

    async def push_frames(self, frames: list[bytes], start_seq: int = 0) -> None:
        """按序投递音频帧。"""
        for offset, frame in enumerate(frames):
            await self.transport.push(audio_message(start_seq + offset, frame))

    async def wait_for_type(self, message_type: str, *, count: int = 1) -> list[dict[str, Any]]:
        """等待指定类型下行消息累计出现 count 条。"""

        def _predicate(sent: list[dict[str, Any]]) -> bool:
            return sum(1 for message in sent if message.get("type") == message_type) >= count

        return await self.transport.wait_for(_predicate, timeout=_WAIT_TIMEOUT)

    def messages(self, message_type: str) -> list[dict[str, Any]]:
        """按类型取已下行消息。"""
        return self.transport.messages_of(message_type)

    def types(self) -> list[str]:
        """已下行消息类型序列。"""
        return self.transport.types()

    async def finish_chat(self, deltas: list[str]) -> None:
        """投喂一轮回复增量并结束生成。"""
        for delta in deltas:
            await self.chat.push(delta)
        await self.chat.finish()

    async def request_end(self) -> None:
        """请求客户端结束并等待会话任务收尾。"""
        if self.task is None:
            return
        if not self.task.done():
            await self.push({"type": "end", "ts_ms": 0})
        await asyncio.wait_for(self.task, timeout=_WAIT_TIMEOUT)

    async def await_close(self) -> None:
        """等待会话自主关闭（超时 / 断开 / 拒绝路径）。"""
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=_WAIT_TIMEOUT)


def _last_state(sent: list[dict[str, Any]]) -> str | None:
    """取最后一条 state 消息的相位值。"""
    states = [message["state"] for message in sent if message.get("type") == "state"]
    return states[-1] if states else None


async def _wait_state(harness: SessionHarness, phase: str) -> None:
    """等待指定相位的 state 消息出现。"""

    def _predicate(sent: list[dict[str, Any]]) -> bool:
        return any(
            message.get("type") == "state" and message.get("state") == phase
            for message in sent
        )

    await harness.transport.wait_for(_predicate, timeout=_WAIT_TIMEOUT)


def _mark(harness: SessionHarness) -> int:
    """记录当前出站记录长度（后续等待只看新消息——避免历史的 listening 误命中）。"""
    return len(harness.transport.sent)


async def _wait_type_after(
    harness: SessionHarness, message_type: str, mark: int, *, count: int = 1
) -> None:
    """等待标记之后出现指定类型的 count 条下行消息。"""

    def _predicate(sent: list[dict[str, Any]]) -> bool:
        return sum(1 for message in sent[mark:] if message.get("type") == message_type) >= count

    await harness.transport.wait_for(_predicate, timeout=_WAIT_TIMEOUT)


async def _wait_state_after(harness: SessionHarness, phase: str, mark: int) -> None:
    """等待标记之后出现指定相位的 state 消息。"""

    def _predicate(sent: list[dict[str, Any]]) -> bool:
        return any(
            message.get("type") == "state" and message.get("state") == phase
            for message in sent[mark:]
        )

    await harness.transport.wait_for(_predicate, timeout=_WAIT_TIMEOUT)


# ---------------------------------------------------------------------------
# 会话建立 / 拒绝面
# ---------------------------------------------------------------------------


async def test_start_ready_declares_capabilities_and_limits() -> None:
    """start → ready：能力 / 协议版本 / VAD 参数 / 资源上限 / listening 相位。"""
    harness = SessionHarness()
    await harness.boot()
    ready = harness.messages("ready")[0]
    assert ready["protocol_version"] == "1"
    assert ready["sample_rate_hz"] == 16000
    assert ready["channels"] == 1
    assert ready["audio_format"] == "wav"
    assert ready["asr_enabled"] is True
    assert ready["tts_enabled"] is True
    assert ready["barge_in_enabled"] is True
    assert ready["vad"]["min_speech_ms"] == 100
    assert ready["limits"]["audio_queue_max_chunks"] == 8
    assert _last_state(harness.transport.sent) == "listening"
    assert harness.session is not None
    assert harness.session.session_id == ready["session_id"]
    await harness.request_end()
    assert harness.session.metrics.closed_reason is not None
    assert harness.session.active_task_count == 0


async def test_start_rejected_when_asr_disabled() -> None:
    """ASR 未启用 → error(CONFLICT) + closed(session_rejected)。"""
    harness = SessionHarness()
    await harness.boot(asr_enabled=False, push_start=False)
    await harness.push(start_message())
    await harness.await_close()
    errors = harness.messages("error")
    assert errors and errors[0]["code"] == "CONFLICT"
    assert errors[0]["recoverable"] is False
    assert harness.messages("closed")[0]["reason"] == "session_rejected"
    assert harness.transport.closed is not None
    # ready 未下发（能力不足直接拒绝）。
    assert harness.messages("ready") == []


async def test_start_rejects_sample_rate_mismatch() -> None:
    """客户端采集采样率与服务端不一致 → 拒绝（避免转写音高错乱）。"""
    harness = SessionHarness()
    await harness.boot(push_start=False)
    await harness.push(start_message(sample_rate_hz=8000))
    await harness.await_close()
    errors = harness.messages("error")
    assert errors and errors[0]["code"] == "VALIDATION_ERROR"
    assert harness.messages("closed")[0]["reason"] == "session_rejected"


async def test_start_rejects_persona_mismatch() -> None:
    """start.persona_id 与连接绑定人格不一致 → 拒绝。"""
    harness = SessionHarness()
    await harness.boot(push_start=False)
    await harness.push(start_message(persona_id=42))
    await harness.await_close()
    errors = harness.messages("error")
    assert errors and errors[0]["code"] == "VALIDATION_ERROR"
    assert harness.messages("closed")[0]["reason"] == "session_rejected"


async def test_second_start_is_recoverable_error() -> None:
    """重复 start（单连接单会话）→ 可恢复 error，会话继续可用。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push(start_message())
    await harness.wait_for_type("error")
    error = harness.messages("error")[-1]
    assert error["recoverable"] is True
    assert "单连接单会话" in error["message"]
    await harness.request_end()
    assert len(harness.messages("ready")) == 1


async def test_message_before_start_and_invalid_json_are_recoverable() -> None:
    """未 start 的音频 / 非法 JSON → 可恢复 error；start 之后会话照常可用。"""
    harness = SessionHarness()
    await harness.boot(push_start=False)
    await harness.push(audio_message(0, loud_frame()))
    await harness.transport.push_raw("{not json")
    await harness.wait_for_type("error", count=2)
    assert all(message["recoverable"] for message in harness.messages("error"))
    await harness.push(start_message())
    await harness.wait_for_type("ready")
    await harness.request_end()


async def test_ping_is_answered_with_state() -> None:
    """心跳 ping → state（当前相位）应答。"""
    harness = SessionHarness()
    await harness.boot()
    before = len(harness.messages("state"))
    await harness.push({"type": "ping", "ts_ms": 0})
    await harness.transport.wait_for(
        lambda sent: len([m for m in sent if m.get("type") == "state"]) > before
    )
    assert harness.messages("state")[-1]["state"] == "listening"


# ---------------------------------------------------------------------------
# 完整链路（VAD → ASR → 生成 → 句切 → TTS 下行）
# ---------------------------------------------------------------------------


async def test_full_duplex_turn_produces_transcript_text_and_audio() -> None:
    """完整链路：切段转写 → 生成增量 → 逐句音频分片 → 回合收尾回聆听。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    transcript = harness.messages("final_transcript")[0]
    assert transcript["text"] == "你好呀"
    assert transcript["segment_id"].startswith("seg-")
    assert harness.voice.asr_calls
    await harness.finish_chat(["你好。今天", "怎么样？"])
    await harness.wait_for_type("assistant_text", count=3)
    await _wait_state(harness, "speaking")
    await _wait_state(harness, "listening")
    audio = harness.messages("audio_chunk")
    assert audio, "逐句 TTS 应产生音频分片"
    assert audio[0]["content_type"] == "audio/wav"
    assert audio[0]["total_chunks"] >= 1
    assert all(
        harness.messages("audio_chunk")[0]["turn_id"] == chunk["turn_id"] for chunk in audio
    )
    finals = [m for m in harness.messages("assistant_text") if m["final"]]
    assert finals and finals[-1]["text"] == "你好。今天怎么样？"
    await harness.request_end()
    assert harness.session is not None
    metrics = harness.session.metrics
    assert metrics.final_transcripts == 1
    assert metrics.generations == 1
    assert metrics.tts_sentences == 2
    assert metrics.audio_chunks_sent == len(audio)
    assert metrics.interruptions == 0
    assert harness.messages("closed")[0]["reason"] == "client_end"
    assert harness.session.active_task_count == 0


async def test_tts_sentence_indexes_are_per_turn_monotonic() -> None:
    """同轮多句的 sentence_index 必须单调递增（客户端按该键聚合分片）。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    mark = _mark(harness)
    await harness.finish_chat(["第一句话。", "第二句话。", "第三句话？"])
    await _wait_state_after(harness, "listening", mark)
    chunks = harness.messages("audio_chunk")
    indexes = sorted({int(chunk["sentence_index"]) for chunk in chunks})
    assert indexes == [0, 1, 2]
    # 同一句的分片总数与实际分片数一致（客户端据此判断收齐）。
    for index in indexes:
        parts = [chunk for chunk in chunks if chunk["sentence_index"] == index]
        assert len(parts) == int(parts[0]["total_chunks"])
        assert sorted(int(part["seq"]) for part in parts) == list(range(len(parts)))
    await harness.request_end()


async def test_short_burst_is_discarded_without_asr_call() -> None:
    """短语音（低于最短门槛）丢弃：不调 ASR、不下发 final transcript。"""
    harness = SessionHarness(vad_min_speech_ms=500, vad_max_segment_ms=1000)
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.push({"type": "ping", "ts_ms": 0})
    await harness.wait_for_type("state", count=2)
    assert harness.messages("final_transcript") == []
    assert harness.voice.asr_calls == []
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.segments_discarded == 1


async def test_empty_transcript_returns_to_listening_without_generation() -> None:
    """空转写（端点无识别结果）：不下发 final、不生成回复、相位回聆听。"""
    harness = SessionHarness()
    await harness.boot()
    harness.voice.asr_text = "   "
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    # state 序：listening（建立）→ thinking（切段）→ listening（空转写收尾）。
    await harness.wait_for_type("state", count=3)
    assert _last_state(harness.transport.sent) == "listening"
    assert harness.messages("final_transcript") == []
    assert harness.voice.asr_calls != []
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.generations == 0


async def test_forced_segment_on_speech_ceiling() -> None:
    """长独白达到单段上限强制切段并送 ASR（无需静音窗口）。"""
    harness = SessionHarness(
        vad_max_segment_ms=500, vad_silence_end_ms=5000, vad_min_speech_ms=100
    )
    await harness.boot()
    await harness.push_frames([loud_frame() for _ in range(5)])
    await harness.wait_for_type("final_transcript")
    assert harness.messages("final_transcript")[0]["text"] == "你好呀"
    await harness.finish_chat(["收到。"])
    await _wait_state(harness, "listening")
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.segments_forced == 1


async def test_partial_transcript_is_throttled_and_shares_segment_id() -> None:
    """中途转写：节流后下发 partial，语段标识与 final 一致。"""
    harness = SessionHarness(
        partial_transcript_interval_ms=50,
        partial_min_speech_ms=100,
        vad_silence_end_ms=300,
        vad_max_segment_ms=5000,
    )
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame()])
    await harness.push_frames([loud_frame()], start_seq=2)
    await harness.wait_for_type("partial_transcript")
    partial = harness.messages("partial_transcript")[0]
    assert partial["text"] == "你好呀"
    await harness.push_frames([silence_frame() for _ in range(3)], start_seq=3)
    await harness.wait_for_type("final_transcript")
    assert harness.messages("final_transcript")[0]["segment_id"] == partial["segment_id"]
    mark = _mark(harness)
    await harness.finish_chat(["嗯。"])
    await _wait_state_after(harness, "listening", mark)
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.partial_transcripts >= 1


async def test_end_flushes_pending_segment_before_close() -> None:
    """客户端 end：冲刷在途语段（转写 + 回复）后优雅关闭。"""
    harness = SessionHarness(vad_silence_end_ms=5000)
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), loud_frame()])
    await harness.push({"type": "end", "ts_ms": 0})
    await harness.wait_for_type("final_transcript")
    await harness.finish_chat(["我在。"])
    assert harness.task is not None
    await asyncio.wait_for(harness.task, timeout=_WAIT_TIMEOUT)
    assert harness.messages("final_transcript")[0]["text"] == "你好呀"
    assert harness.messages("closed")[0]["reason"] == "client_end"
    assert harness.transport.closed == (1000, "client_end")


async def test_tts_disabled_yields_text_only_turn() -> None:
    """TTS 关闭：ready 声明 tts_enabled=false，回合只下行文本（无音频分片）。"""
    harness = SessionHarness()
    await harness.boot(tts_enabled=False)
    assert harness.messages("ready")[0]["tts_enabled"] is False
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    mark = _mark(harness)
    await harness.finish_chat(["晚安。"])
    await _wait_type_after(harness, "assistant_text", mark, count=2)
    await _wait_state_after(harness, "listening", mark)
    assert harness.messages("audio_chunk") == []
    assert harness.messages("assistant_text")
    assert harness.voice.tts_calls == []
    await harness.request_end()


# ---------------------------------------------------------------------------
# 打断
# ---------------------------------------------------------------------------


async def test_manual_barge_in_cancels_inflight_generation() -> None:
    """手动打断：取消在途生成 + 下行 interrupted + state(listening)，任务回收。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    mark = _mark(harness)
    # 生成已启动但尚未产出增量（脚本队列为空 → 生成器阻塞）。
    await harness.push({"type": "barge_in", "ts_ms": 0, "reason": "manual"})
    await harness.wait_for_type("interrupted")
    interrupted = harness.messages("interrupted")[0]
    assert interrupted["reason"] == "manual"
    assert interrupted["turn_id"].startswith("turn-")
    await _wait_state_after(harness, "listening", mark)
    assert _last_state(harness.transport.sent) == "listening"
    assert harness.chat.finished is True
    assert harness.session is not None
    assert harness.session.metrics.interruptions == 1
    await harness.request_end()
    assert harness.session.active_task_count == 0


async def test_vad_barge_in_during_playback_cancels_tts() -> None:
    """VAD 检测到新语音打断播报：取消 TTS 在途任务、清空未合成句、回到聆听。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.chat.push("第一句话。")
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("audio_chunk")
    await _wait_state(harness, "speaking")
    # 阻塞后续合成（第二句在途中），再制造新语音。
    harness.voice.tts_gate = asyncio.Event()
    harness.voice.tts_started.clear()
    await harness.chat.push("第二句话。")
    await asyncio.wait_for(harness.voice.tts_started.wait(), timeout=_WAIT_TIMEOUT)
    mark = _mark(harness)
    await harness.push_frames([loud_frame(), loud_frame()], start_seq=10)
    await harness.wait_for_type("interrupted")
    interrupted = harness.messages("interrupted")[0]
    assert interrupted["reason"] == "vad"
    assert harness.session is not None
    assert harness.session.metrics.interruptions == 1
    await _wait_state_after(harness, "listening", mark)
    assert _last_state(harness.transport.sent) == "listening"
    # 在途 TTS 已被取消：第二句在合成门闩上被打断（不会产出音频分片）。
    assert harness.voice.tts_calls == ["第一句话。", "第二句话。"]
    # 打断后新语段的生成轮次在收尾时消费预置脚本（end 会等待轮次播完）；
    # 释放合成门闩——第二句已在打断时被取消，新轮合成照常完成。
    harness.voice.tts_gate = None
    await harness.chat.push("好的，你说。")
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session.active_task_count == 0


async def test_barge_in_disabled_keeps_playback_and_turn_completes() -> None:
    """打断开关关闭：播放中检测到新语音不打断，回合照常收尾。"""
    harness = SessionHarness(barge_in_enabled=False)
    await harness.boot()
    assert harness.messages("ready")[0]["barge_in_enabled"] is False
    await harness.chat.push("第一句话。")
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("audio_chunk")
    mark = _mark(harness)
    await harness.push_frames([loud_frame(), loud_frame()], start_seq=10)
    await harness.chat.finish()
    await _wait_state_after(harness, "listening", mark)
    assert harness.messages("interrupted") == []
    await harness.chat.push("第二句回复。")
    await harness.chat.finish()
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.interruptions == 0


async def test_session_leaves_no_orphan_tasks_after_barge_in_and_close() -> None:
    """打断 + 关闭后除测试自身外无遗留任务（事件循环级泄漏断言）。"""
    baseline = asyncio.all_tasks()
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    await harness.push({"type": "barge_in", "ts_ms": 0})
    await harness.wait_for_type("interrupted")
    await harness.chat.push("收到。")
    await harness.chat.finish()
    await harness.request_end()
    current = asyncio.current_task()
    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not current and task not in baseline and not task.done()
    ]
    assert leaked == [], f"会话遗留任务: {[task.get_coro() for task in leaked]}"
    assert harness.session is not None
    assert harness.session.active_task_count == 0


async def test_barge_in_ack_is_counted() -> None:
    """客户端播放清空确认计入指标（不改变会话相位）。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push({"type": "barge_in_ack", "ts_ms": 0, "interruption_id": "intr-9"})
    await harness.push({"type": "ping", "ts_ms": 0})
    await harness.wait_for_type("state", count=2)
    assert harness.session is not None
    assert harness.session.metrics.barge_in_acks == 1
    await harness.request_end()


# ---------------------------------------------------------------------------
# 背压 / 超时 / 断开
# ---------------------------------------------------------------------------


async def test_audio_backpressure_drops_oldest_chunks() -> None:
    """背压：队列满时丢最旧块并计数（收帧循环不被慢 ASR 阻塞）。"""
    harness = SessionHarness(audio_queue_max_chunks=2, audio_batch_max_chunks=1)
    await harness.boot()
    release = asyncio.Event()
    harness.voice.asr_gate = release
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    # 转写被门闩阻塞 → 音频工人停在段处理，后续帧堆满队列后开始丢弃。
    await asyncio.wait_for(harness.voice.asr_started.wait(), timeout=_WAIT_TIMEOUT)
    await harness.push_frames([silence_frame() for _ in range(6)], start_seq=10)
    # 心跳屏障：上行按序处理——心跳应答 state 到达即说明前述音频块均已入队。
    await harness.push({"type": "ping", "ts_ms": 0})
    await harness.transport.wait_for(
        lambda sent: any(
            message.get("type") == "state" and message.get("detail") == "心跳应答"
            for message in sent
        )
    )
    assert harness.session is not None
    assert harness.session.metrics.audio_chunks_dropped >= 3
    release.set()
    await harness.wait_for_type("final_transcript")
    await harness.finish_chat(["好。"])
    await harness.request_end()
    assert harness.session.active_task_count == 0


async def test_idle_timeout_closes_session() -> None:
    """空闲超时：无上行消息达到阈值 → closed(idle_timeout) + 关闭传输。"""
    harness = SessionHarness(idle_timeout_ms=20_000, max_session_ms=100_000)
    await harness.boot(sleeper=instant_sleeper(harness.clock, advance_ms=10_000))
    await harness.await_close()
    assert harness.messages("closed")[0]["reason"] == "idle_timeout"
    assert harness.transport.closed == (1000, "idle_timeout")
    assert harness.session is not None
    assert harness.session.metrics.closed_reason is not None


async def test_max_duration_closes_session_even_with_heartbeats() -> None:
    """最大时长：心跳维持空闲不触发，但达到会话上限即关闭。"""
    harness = SessionHarness(idle_timeout_ms=30_000, max_session_ms=32_000)
    await harness.boot(sleeper=instant_sleeper(harness.clock, advance_ms=1_000))
    baseline = len(harness.messages("state"))

    async def _keepalive() -> None:
        expected = baseline
        while harness.task is not None and not harness.task.done():
            await harness.push({"type": "ping", "ts_ms": 0})
            expected += 1
            try:
                await harness.transport.wait_for(
                    lambda sent, need=expected: len(
                        [m for m in sent if m.get("type") == "state"]
                    )
                    >= need
                )
            except TimeoutError:
                return

    keepalive = asyncio.create_task(_keepalive())
    try:
        await harness.await_close()
    finally:
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)
    assert harness.messages("closed")[0]["reason"] == "max_duration"
    assert harness.transport.closed == (1000, "max_duration")


async def test_transport_disconnect_finishes_cleanly() -> None:
    """对端断开：会话收尾（closed(transport_closed) + 关闭传输）且无泄漏。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.transport.push_disconnect()
    await harness.await_close()
    assert harness.messages("closed")[0]["reason"] == "transport_closed"
    assert harness.transport.closed == (1000, "transport_closed")
    assert harness.session is not None
    assert harness.session.active_task_count == 0


# ---------------------------------------------------------------------------
# 失败路径（可恢复 / 不可恢复）
# ---------------------------------------------------------------------------


async def test_asr_failure_is_recoverable_and_session_continues() -> None:
    """ASR 失败：可恢复 error + 回到聆听；后续语段照常可用。"""
    harness = SessionHarness()
    await harness.boot()
    harness.voice.asr_error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="ASR 服务不可用"
    )
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("error")
    error = harness.messages("error")[0]
    assert error["recoverable"] is True
    assert error["code"] == "LLM_CALL_FAILED"
    harness.voice.asr_error = None
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()], start_seq=10)
    await harness.wait_for_type("final_transcript")
    await harness.finish_chat(["嗯。"])
    await _wait_state(harness, "listening")
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.asr_failures == 1


async def test_generation_failure_is_recoverable() -> None:
    """生成失败：可恢复 error + 回聆听；指标计失败数。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    await harness.chat.fail(
        BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="模型不可用")
    )
    await harness.wait_for_type("error")
    await _wait_state(harness, "listening")
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.generation_failures == 1


async def test_tts_failure_skips_sentence_and_completes_turn() -> None:
    """TTS 单句失败：可恢复 error、跳过该句、回合照常收尾。"""
    harness = SessionHarness()
    await harness.boot()
    harness.voice.tts_error = tts_failure()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    await harness.finish_chat(["这一句合成失败。"])
    await harness.wait_for_type("error")
    await _wait_state(harness, "listening")
    assert harness.messages("audio_chunk") == []
    await harness.request_end()
    assert harness.session is not None
    assert harness.session.metrics.tts_failures == 1


# ---------------------------------------------------------------------------
# 回复生成面适配（真实 ChatService 委托）
# ---------------------------------------------------------------------------


class _ScriptedGateway:
    """最小流式网关替身（只实现 stream_by_role——适配器委托面）。"""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas
        self.requests: list[list[Any]] = []

    async def stream_by_role(
        self, _role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> Any:
        self.requests.append(list(messages))
        for delta in self._deltas:
            yield ChatStreamChunkModel(content=delta)

    async def chat_by_role(
        self, _role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        self.requests.append(list(messages))
        return ChatResponseModel(content="".join(self._deltas))


async def test_chat_session_adapter_delegates_to_chat_service() -> None:
    """适配器把 stream_turn 委托给真实 ChatService（组装 + 流式增量）。"""
    gateway = _ScriptedGateway(["你好，", "我是小助手。"])
    chat_service = ChatService(gateway=gateway)
    state: ChatSessionStateModel = await chat_service.create_session(persona=persona_record())
    adapter = ChatSessionStreamAdapter(chat_service, state)
    deltas: list[str] = []
    async for delta in adapter.stream_turn("在吗"):
        deltas.append(delta)
    assert "".join(deltas) == "你好，我是小助手。"
    assert gateway.requests, "网关应收到组装后的请求"
    assert state.turns[-1].role == "assistant"


async def test_outbound_payloads_match_protocol_adapter() -> None:
    """端到端下行消息全部可被协议联合解析（wire 契约对称）。"""
    harness = SessionHarness()
    await harness.boot()
    await harness.push_frames([loud_frame(), loud_frame(), silence_frame()])
    await harness.wait_for_type("final_transcript")
    await harness.finish_chat(["在的。"])
    await _wait_state(harness, "listening")
    for message in harness.transport.sent:
        parsed = REALTIME_OUTBOUND_ADAPTER.validate_python(message)
        assert parsed.type == message["type"]
    await harness.request_end()


async def test_settings_invariants_surface_via_session_construction() -> None:
    """非法配置在会话构造前即被拒绝（fail loud，不产生半生效会话）。"""
    with pytest.raises(ValidationError):
        RealtimeVoiceSession(
            transport=FakeRealtimeTransport(),
            realtime_settings=RealtimeVoiceSettings(
                enabled=True,
                vad_speech_rms_threshold=0.02,
                vad_silence_rms_threshold=0.05,
            ),
            voice_settings=voice_settings(),
            chat=ScriptedChatStream(),
            voice_service=FakeVoiceService(),
        )
