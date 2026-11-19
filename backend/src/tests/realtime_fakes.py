"""实时语音测试共享基建——内存双工传输 / 脚本化语音与聊天面 / 伪时钟。

设计约束（对齐测试纪律）：

    - **零真实网络**：传输面是进程内队列，语音服务是脚本 fake；
    - **零真实等待**：超时判定经伪时钟 + 即时休眠面驱动；测试只等待
      事件（出站消息谓词 / 门闩），不做固定 sleep；
    - **零真实模型**：ASR / TTS 结果由脚本决定（含失败注入与门闩阻塞
      ——用于背压 / 打断在途场景）。

伪造面与生产契约保持同构（协议结构 / 方法签名），从而同一用例可无缝
切换真实实现（服务层集成测试经真实 ChatService + 假网关驱动）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from array import array
from collections.abc import Awaitable, Callable
from typing import Any

from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.voice.realtime_models import RealtimeVoiceSettings
from packages.harness.voice.voice_models import AsrResultModel, TtsResultModel
from packages.harness.voice.voice_service import VoiceService

#: 一帧音频的默认毫秒数（16kHz 单声道 16 位 → 3200 字节）。
FRAME_MS: int = 100
#: 16kHz 单声道 16 位每毫秒字节数。
BYTES_PER_MS: int = 32


def pcm_frame(amplitude: int, *, ms: int = FRAME_MS) -> bytes:
    """构造定长 PCM16 帧（幅度为 0 即静音帧）。

    Args:
        amplitude: 采样幅度（-32768..32767；6000 已显著高于缺省起说阈值）。
        ms: 帧时长毫秒。

    Returns:
        16 位小端 PCM 字节。
    """
    samples = array("h", [amplitude] * (ms * BYTES_PER_MS // 2))
    return samples.tobytes()


def loud_frame(*, amplitude: int = 6000, ms: int = FRAME_MS) -> bytes:
    """高能量语音帧（触发 VAD 起说 / 维持语音态）。"""
    return pcm_frame(amplitude, ms=ms)


def silence_frame(*, ms: int = FRAME_MS) -> bytes:
    """零能量静音帧。"""
    return pcm_frame(0, ms=ms)


def audio_message(seq: int, frame: bytes, *, ts_ms: int = 0) -> dict[str, Any]:
    """构造上行 audio_chunk 消息字典。"""
    return {
        "type": "audio_chunk",
        "ts_ms": ts_ms,
        "seq": seq,
        "data": base64.b64encode(frame).decode("ascii"),
    }


def start_message(**overrides: Any) -> dict[str, Any]:
    """构造上行 start 消息字典（采样率 / 声道与测试配置一致）。"""
    payload: dict[str, Any] = {
        "type": "start",
        "ts_ms": 0,
        "sample_rate_hz": 16000,
        "channels": 1,
    }
    payload.update(overrides)
    return payload


def realtime_settings(**overrides: Any) -> RealtimeVoiceSettings:
    """构造测试用实时语音配置（小窗口——语段在数帧内闭合）。

    Args:
        **overrides: 覆盖字段。

    Returns:
        启用态配置（VAD 窗口压缩到百毫秒量级，保持真实字段语义）。
    """
    base: dict[str, Any] = {
        "enabled": True,
        "sample_rate_hz": 16000,
        "channels": 1,
        "vad_speech_rms_threshold": 0.06,
        "vad_silence_rms_threshold": 0.03,
        "vad_start_consecutive_frames": 2,
        "vad_min_speech_ms": 100,
        "vad_silence_end_ms": 100,
        "vad_max_segment_ms": 1000,
        "partial_transcript_interval_ms": 0,
        "partial_min_speech_ms": 100,
        "sentence_max_chars": 80,
        "tts_chunk_bytes": 1024,
        "audio_queue_max_chunks": 8,
        "audio_batch_max_chunks": 2,
        "max_session_ms": 600_000,
        "idle_timeout_ms": 60_000,
        "heartbeat_interval_ms": 15_000,
        "barge_in_enabled": True,
    }
    base.update(overrides)
    return RealtimeVoiceSettings(**base)


def voice_settings(*, asr: bool = True, tts: bool = True) -> VoiceSettings:
    """构造启用 / 关闭 ASR / TTS 的语音域配置。"""
    return VoiceSettings(
        tts=TtsSettings(enabled=tts),
        asr=AsrSettings(enabled=asr),
    )


def persona_record() -> PersonaRecordDTO:
    """最小人格记录（会话绑定 / TTS 发音绑定面）。"""
    return PersonaRecordDTO(
        id=1,
        name="companion-1",
        display_name="小助手",
        status="active",
        is_primary=True,
        created_at=1_700_000_000_000,
        updated_at=1_700_000_000_000,
        card={
            "name": "小助手",
            "description": "温柔陪伴",
            "personality": "耐心",
            "scenario": "深夜",
            "first_mes": "你好呀。",
            "mes_example": "",
            "system_prompt": "",
            "post_history_instructions": "",
            "character_book": None,
        },
    )


class FakeRealtimeTransport:
    """内存双工传输——入站队列 + 出站记录（零网络）。

    出站消息按 JSON 解析为字典记录；用例经 :meth:`wait_for` 以谓词等待
    指定消息出现（事件驱动，无固定 sleep）。``send_gate`` 可门闩下行发送
    （制造在途发送阻塞——背压用例用它把音频工人停在状态迁移的下行上）。
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed: tuple[int, str] | None = None
        self.send_gate: asyncio.Event | None = None
        self._blocked_sends = 0
        self._blocked_changed = asyncio.Event()
        self._changed = asyncio.Event()

    async def send_text(self, payload: str) -> None:
        """记录下行 JSON 并唤醒等待者（``send_gate`` 存在时先阻塞）。"""
        if self.send_gate is not None:
            self._blocked_sends += 1
            self._blocked_changed.set()
            await self.send_gate.wait()
        self.sent.append(json.loads(payload))
        self._changed.set()

    async def receive_text(self) -> str | None:
        """取下一条上行 JSON（None = 对端关闭）。"""
        return await self._inbound.get()

    async def close(self, code: int, reason: str) -> None:
        """记录关闭动作（幂等——重复关闭保留首个记录）。"""
        if self.closed is None:
            self.closed = (code, reason)

    async def push(self, message: dict[str, Any]) -> None:
        """投递上行消息字典。"""
        await self._inbound.put(json.dumps(message))

    async def push_raw(self, payload: str) -> None:
        """投递原始上行文本（非法 JSON / 形态校验用例）。"""
        await self._inbound.put(payload)

    async def push_disconnect(self) -> None:
        """模拟对端断开（receive_text 返回 None）。"""
        await self._inbound.put(None)

    def messages_of(self, message_type: str) -> list[dict[str, Any]]:
        """按类型筛出已发送消息。"""
        return [message for message in self.sent if message.get("type") == message_type]

    def types(self) -> list[str]:
        """返回已发送消息类型序列。"""
        return [str(message.get("type")) for message in self.sent]

    async def wait_for(
        self,
        predicate: Callable[[list[dict[str, Any]]], bool],
        *,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """等待出站记录满足谓词（返回当时的全量记录）。

        Args:
            predicate: 出站记录谓词。
            timeout: 超时秒数（测试预算——不参与业务时序）。

        Returns:
            满足谓词时的出站记录列表。

        Raises:
            asyncio.TimeoutError: 超时仍未满足。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate(self.sent):
                return list(self.sent)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"等待出站消息超时，已收到: {self.types()}")
            self._changed.clear()
            if predicate(self.sent):
                return list(self.sent)
            await asyncio.wait_for(self._changed.wait(), timeout=remaining)

    async def wait_for_blocked_sends(self, count: int, *, timeout: float = 5.0) -> None:
        """等待指定数量的下行发送进入 ``send_gate`` 阻塞。

        Args:
            count: 需要观察到的阻塞发送条数。
            timeout: 超时秒数（测试预算——不参与业务时序）。

        Raises:
            asyncio.TimeoutError: 超时仍不足指定条数。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._blocked_sends < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"等待下行发送阻塞超时：已阻塞 {self._blocked_sends} 条"
                )
            self._blocked_changed.clear()
            if self._blocked_sends >= count:
                return
            await asyncio.wait_for(self._blocked_changed.wait(), timeout=remaining)


class FakeVoiceService(VoiceService):
    """脚本化语音服务（继承真实门面以保持注入面类型契约）。

    行为旋钮：ASR 文本 / ASR 失败 / ASR 门闩（阻塞在途转写——背压与
    打断用例）、TTS 音频 / TTS 失败 / TTS 门闩。
    """

    def __init__(self) -> None:
        super().__init__()
        self.asr_text: str = "你好呀"
        self.asr_language: str | None = "zh"
        self.asr_error: BusinessException | None = None
        self.asr_gate: asyncio.Event | None = None
        self.asr_started = asyncio.Event()
        self.asr_calls: list[str] = []
        self.tts_audio: bytes = b"RIFF" + b"\x00" * 4096
        self.tts_error: BusinessException | None = None
        self.tts_gate: asyncio.Event | None = None
        self.tts_started = asyncio.Event()
        self.tts_calls: list[str] = []

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        *,
        persona: PersonaRecordDTO | None = None,
        language: str | None = None,
        request_id: str | None = None,
    ) -> AsrResultModel:
        """记录转写调用并按脚本返回（门闩可用于制造在途阻塞）。"""
        self.asr_calls.append(filename)
        self.asr_started.set()
        if self.asr_gate is not None:
            await self.asr_gate.wait()
        if self.asr_error is not None:
            raise self.asr_error
        return AsrResultModel(
            text=self.asr_text,
            duration_ms=300,
            language=self.asr_language,
        )

    async def synthesize(
        self,
        text: str,
        *,
        persona: PersonaRecordDTO | None = None,
        speaker: str | None = None,
        speed: float | None = None,
        request_id: str | None = None,
    ) -> TtsResultModel:
        """记录合成调用并按脚本返回音频 / 抛出失败。"""
        self.tts_calls.append(text)
        self.tts_started.set()
        if self.tts_gate is not None:
            await self.tts_gate.wait()
        if self.tts_error is not None:
            raise self.tts_error
        return TtsResultModel(audio=self.tts_audio, content_type="audio/wav", latency_ms=5)


class ScriptedChatStream:
    """脚本化流式回复面——按队列投喂增量（可阻塞，用于打断 / 串行用例）。"""

    def __init__(self) -> None:
        self._stream: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        self.calls: list[str] = []
        self.finished: bool = False

    def stream_turn(self, user_text: str) -> Any:
        """返回异步生成器（逐条消费脚本队列）。"""
        self.calls.append(user_text)

        async def _generate() -> Any:
            try:
                while True:
                    item = await self._stream.get()
                    if item is None:
                        self.finished = True
                        return
                    if isinstance(item, Exception):
                        raise item
                    yield item
            finally:
                self.finished = True

        return _generate()

    async def push(self, delta: str) -> None:
        """投喂一段增量。"""
        await self._stream.put(delta)

    async def finish(self) -> None:
        """结束本轮流式生成。"""
        await self._stream.put(None)

    async def fail(self, error: Exception) -> None:
        """注入失败（生成面异常）。"""
        await self._stream.put(error)


class FakeClock:
    """伪时钟（epoch 毫秒）——看门狗 / 空闲 / 最大时长时间源。"""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        """返回当前伪时间。"""
        return self.ms

    def advance(self, delta_ms: int) -> None:
        """推进伪时间。"""
        self.ms += delta_ms


def instant_sleeper(
    clock: FakeClock, *, advance_ms: int
) -> Callable[[float], Awaitable[None]]:
    """构造即时休眠面：每次休眠推进伪时钟并让出事件循环（零真实等待）。

    Args:
        clock: 伪时钟。
        advance_ms: 每次休眠推进的毫秒数。

    Returns:
        休眠面协程函数。
    """

    async def _sleep(_seconds: float) -> None:
        clock.advance(advance_ms)
        await asyncio.sleep(0)

    return _sleep


def asr_failure() -> BusinessException:
    """构造 ASR 失败异常（映射为可恢复 error）。"""
    return BusinessException(
        code=ErrorCode.LLM_CALL_FAILED,
        message="ASR 服务不可用",
        details={"reason": "test"},
    )


def tts_failure() -> BusinessException:
    """构造 TTS 失败异常（映射为可恢复 error）。"""
    return BusinessException(
        code=ErrorCode.LLM_CALL_FAILED,
        message="TTS 服务不可用",
        details={"reason": "test"},
    )


async def parked_sleeper(_seconds: float) -> None:
    """挂起型休眠面：只在会话收尾取消时返回（非超时用例零 CPU / 零真实时序）。"""
    await asyncio.sleep(3600)
