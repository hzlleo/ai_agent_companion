"""realtime voice 域会话引擎——全双工语音会话编排（协议 / 状态 / 打断）。

链路顺序（单一真相，文档化于 docs/backend/realtime-voice.md）::

    上行音频帧 → 背压队列（批量出队 / 溢出丢最旧）→ VAD 切段
        ↓ 语段就绪（或强制切段）→ 派生转写任务（音频工人只投递）
    VoiceService.transcribe（真实取材：Qwen3-ASR 批量端点——无流式通道）
        ↓ final_transcript（中途 partial 由节流的中途批量转写产生）
    ChatService.stream_reply（人格组装 / 注入防御 / 关系摘要——流式增量）
        ↓ 句切（标点 / 长度上限）
    VoiceService.synthesize（CosyVoice 逐句合成——无流式音频通道）
        ↓ WAV 字节按块 base64 下行（audio_chunk）

转写与 VAD 并发：切段后转写以独立任务执行（登记进统一任务集合），音频
工人只做切段投递——转写窗口内 VAD 照常收帧、起说与打断判定不受影响。
多段转写可并行；生成请求仍进入既有待生成通道串行消费（单会话同一时刻
仅一个活跃轮次），多段几乎同时完成时按**转写完成序**进入该通道，客户端
可据 ``segment_id`` 对齐 final_transcript 与对应轮次。

打断（barge-in）：VAD 检测到新语音或客户端手动 barge_in 时，取消在途
生成任务与逐句 TTS 任务、清空未合成句队列、丢弃未闭合句缓冲，下行
interrupted + state(listening)。取消语义安全边界（文档化决策）：

    - 生成任务被取消 → ``ChatService.stream_reply`` 的异步生成器关闭，
      半截助手回合**不进入会话历史**（与既有流式语义一致）；
    - 逐句 TTS 只对**已闭合句**入队——被打断的未闭合句从不合成，因此
      不会出现「半句音频」；
    - 在途转写任务不随打断取消（已说完的语段仍应产出 final_transcript
      与回复）；所有派生任务经统一任务集合登记并在关闭时 cancel + gather，
      会话关闭后任务集合为空（无泄漏）。

资源与并发：

    - **单连接单会话**：``start`` 只接受一次，重复即 error（可恢复）；
    - **音频块上限**：单块 base64 解码后不得超过 64 KiB，超限块在解析期
      拒绝（可恢复 error + ``audio_chunks_rejected`` 计数），会话继续；
    - **背压**：音频入队上限取 ``REALTIME_VOICE_AUDIO_QUEUE_MAX_CHUNKS``，
      溢出策略为**丢最旧块并计数**（实时音频宁丢旧帧不阻塞收帧循环；
      丢弃数落关闭指标日志）；
    - **超时**：空闲（无任何上行消息）与最大时长由看门狗按注入时钟判定
      ——时钟与休眠面均可注入（伪时钟测试零真实等待）；
    - **优雅关闭**：``end`` 先冲刷 VAD 尾段，排空在途转写与轮次播报后
      下行 closed；排空期间服务端超时 / 内部关闭先到时立即取消在途生成
      与 TTS 并收尾（关闭原因取先到者）；服务端主动关闭（超时 / 错误 /
      拒绝）与客户端 end 均在 closed 前下行 state(closed)。

测试面：传输 / 聊天 / 语音服务全部注入——内存双工 + 脚本 fake + 伪时钟
即可覆盖全部状态迁移，零真实网络、零真实等待。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger
from pydantic import ValidationError

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.models import VoiceSettings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.voice.realtime_audio import iter_byte_chunks, pcm16_to_wav
from packages.harness.voice.realtime_models import (
    AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE,
    REALTIME_AUDIO_CHUNK_MAX_BYTES,
    REALTIME_INBOUND_ADAPTER,
    REALTIME_PROTOCOL_VERSION,
    RealtimeAssistantTextMessageModel,
    RealtimeAudioInputChunkMessageModel,
    RealtimeAudioOutputChunkMessageModel,
    RealtimeBargeInAckMessageModel,
    RealtimeBargeInMessageModel,
    RealtimeBargeInReason,
    RealtimeChatFace,
    RealtimeClosedMessageModel,
    RealtimeCloseReason,
    RealtimeEndMessageModel,
    RealtimeErrorMessageModel,
    RealtimeFinalTranscriptMessageModel,
    RealtimeInboundMessageModel,
    RealtimeInterruptedMessageModel,
    RealtimeOutboundMessageModel,
    RealtimePartialTranscriptMessageModel,
    RealtimePingMessageModel,
    RealtimeReadyMessageModel,
    RealtimeSessionLimitsModel,
    RealtimeSessionMetricsModel,
    RealtimeSessionPhase,
    RealtimeStartMessageModel,
    RealtimeStateMessageModel,
    RealtimeTransportFace,
    RealtimeVadPublicSettingsModel,
    RealtimeVoiceSettings,
    decode_audio_base64,
)
from packages.harness.voice.realtime_sentence import SentenceAccumulator
from packages.harness.voice.realtime_state import (
    RealtimeStateEvent,
    RealtimeVoiceStateMachine,
)
from packages.harness.voice.realtime_vad import (
    EnergyVad,
    VadEventKind,
    VadEventModel,
    build_vad_config,
)
from packages.harness.voice.voice_service import VoiceService

__all__ = [
    "ChatSessionStreamAdapter",
    "TtsSentenceJobModel",
    "RealtimeVoiceSession",
]

#: 看门狗轮询间隔秒数（伪时钟测试可注入自定义休眠面——本值只影响生产
#: 环境超时判定的粒度，不进入 env 面）。
_DEFAULT_WATCHDOG_TICK_SECONDS: float = 0.25

#: 视为「音频块超过单块上限」的校验错误类型集合：解码字节上限由字段校验器
#: 以自定义类型抛出，base64 字符长度上限由 pydantic 以 ``string_too_long``
#: 抛出——两者都只作用于 audio_chunk.data 字段，据此区分超限与其它协议错误。
_AUDIO_CHUNK_OVERSIZE_ERROR_TYPES: frozenset[str] = frozenset(
    {AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE, "string_too_long"}
)

#: 会话内单帧时间戳推进函数类型（伪时钟测试注入）。
type ClockFace = Callable[[], int]
#: 会话休眠面类型（伪时钟测试注入即时返回实现）。
type SleeperFace = Callable[[float], Awaitable[None]]


class TtsSentenceJobModel:
    """逐句 TTS 作业（内部队列载荷——非 wire 模型，故不入 Unit 文件）。

    Attributes:
        turn_id: 所属轮次标识。
        sentence_index: 轮内句序。
        text: 待合成文本。
    """

    __slots__ = ("turn_id", "sentence_index", "text")

    def __init__(self, *, turn_id: str, sentence_index: int, text: str) -> None:
        self.turn_id = turn_id
        self.sentence_index = sentence_index
        self.text = text


class ChatSessionStreamAdapter:
    """回复生成面适配——把 :class:`ChatService` 会话绑为流式单轮接口。

    Args:
        chat_service: 聊天服务（网关 / 人格组装 / 注入防御全量复用）。
        state: 会话状态（连接级内存态；持久化绑定由装配层决定）。
    """

    def __init__(self, chat_service: ChatService, state: ChatSessionStateModel) -> None:
        self._chat_service = chat_service
        self._state = state

    def stream_turn(self, user_text: str) -> AsyncIterator[str]:
        """发起一轮流式回复（委托 ``ChatService.stream_reply``）。

        Args:
            user_text: 用户转写文本。

        Yields:
            回复文本增量。
        """
        return self._chat_service.stream_reply(self._state, user_text)


class RealtimeVoiceSession:
    """实时语音会话引擎（收发循环 / VAD / 转写 / 生成 / TTS / 打断）。

    Args:
        transport: 文本帧传输面（生产 WS 适配；测试内存双工）。
        realtime_settings: 实时语音配置（VAD / 句切 / 队列 / 超时）。
        voice_settings: 既有语音域配置（判定 ASR / TTS 能否消费）。
        chat: 回复生成面（生产 :class:`ChatSessionStreamAdapter`）。
        voice_service: 语音服务门面（ASR 转写 / TTS 合成 + 用量埋点）。
        persona: 绑定的会话人格（None = 未绑定形态——settings 缺省发音）。
        session_id: 会话标识（None = 现场生成）。
        clock: 时钟面（缺省 epoch 毫秒）。
        sleeper: 休眠面（缺省 ``asyncio.sleep``）。
        watchdog_tick_seconds: 看门狗轮询间隔（秒）。
    """

    def __init__(
        self,
        *,
        transport: RealtimeTransportFace,
        realtime_settings: RealtimeVoiceSettings,
        voice_settings: VoiceSettings,
        chat: RealtimeChatFace,
        voice_service: VoiceService,
        persona: PersonaRecordDTO | None = None,
        session_id: str | None = None,
        clock: ClockFace = now_ms,
        sleeper: SleeperFace | None = None,
        watchdog_tick_seconds: float = _DEFAULT_WATCHDOG_TICK_SECONDS,
    ) -> None:
        self._transport = transport
        self._settings = realtime_settings
        self._voice_settings = voice_settings
        self._chat = chat
        self._voice = voice_service
        self._persona = persona
        self._session_id = session_id or uuid.uuid4().hex
        self._clock = clock
        self._sleeper: SleeperFace = sleeper if sleeper is not None else asyncio.sleep
        self._watchdog_tick_seconds = watchdog_tick_seconds
        self._machine = RealtimeVoiceStateMachine()
        self._vad = EnergyVad(build_vad_config(realtime_settings))
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=realtime_settings.audio_queue_max_chunks
        )
        self._tts_queue: asyncio.Queue[TtsSentenceJobModel] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._close_event = asyncio.Event()
        self._turn_idle_event = asyncio.Event()
        self._turn_idle_event.set()
        self._metrics = RealtimeSessionMetricsModel(started_ms=self._clock())
        self._closed = False
        self._closing = False
        self._finalized = False
        self._closed_sent = False
        self._close_reason: RealtimeCloseReason | None = None
        self._close_detail: str | None = None
        self._started = False
        self._last_inbound_ms = self._metrics.started_ms
        self._segment_seq = 0
        self._turn_seq = 0
        self._interruption_seq = 0
        self._current_segment_id: str | None = None
        self._current_turn_id: str | None = None
        self._pending_user_texts: list[str] = []
        self._accumulator: SentenceAccumulator | None = None
        self._generation_done = True
        self._tts_pending = 0
        self._sentence_seq = 0
        self._generation_task: asyncio.Task[None] | None = None
        self._tts_task: asyncio.Task[None] | None = None
        self._partial_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._transcribe_tasks: set[asyncio.Task[None]] = set()
        self._task_failure_count = 0
        self._last_partial_voiced_ms = 0

    # ------------------------------------------------------------------
    # 观测面（装配层 / 测试断言）
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """会话标识（ready 消息与指标日志共用）。"""
        return self._session_id

    @property
    def phase(self) -> RealtimeSessionPhase:
        """当前相位。"""
        return self._machine.phase

    @property
    def metrics(self) -> RealtimeSessionMetricsModel:
        """指标快照（副本——外部不可改写内部计数）。"""
        return self._metrics.model_copy()

    @property
    def active_task_count(self) -> int:
        """当前登记的派生任务数（关闭后应为 0——泄漏断言面）。"""
        return len(self._tasks)

    @property
    def task_failure_count(self) -> int:
        """派生任务异常计数（done 回调检索面——异常不再悬空未取）。"""
        return self._task_failure_count

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """驱动会话直到关闭（客户端 end / 超时 / 传输断开 / 错误）。

        本方法不抛业务异常：全部失败归一为下行 error + closed 并关闭传输；
        外部取消（服务停机）会取消派生任务后原样上抛。超时看门狗在 start
        处理时启动（会话时长从 start 起算——未 start 的连接不受时长规则约束）。
        """
        try:
            while not self._closed:
                raw = await self._receive_next()
                if raw is None:
                    if self._close_reason is None:
                        self._close_reason = RealtimeCloseReason.TRANSPORT_CLOSED
                    break
                self._last_inbound_ms = self._clock()
                await self._handle_raw(raw)
        except asyncio.CancelledError:
            raise
        except Exception:  # 传输异常 / 引擎内部错误：归一为关闭
            logger.opt(exception=True).error(
                "实时语音会话异常终止: session_id={}", self._session_id
            )
            await self._send_error(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="实时语音会话内部错误，会话已关闭",
                recoverable=False,
            )
            if not self._closing:
                await self._request_close(RealtimeCloseReason.INTERNAL_ERROR)
        finally:
            await self._finalize(
                self._close_reason or RealtimeCloseReason.TRANSPORT_CLOSED
            )

    # ------------------------------------------------------------------
    # 上行分派
    # ------------------------------------------------------------------

    async def _receive_next(self) -> str | None:
        """等待下一条上行文本帧或关闭事件（二者竞争，先到先返回）。

        Returns:
            上行 JSON 字符串；关闭事件先到（或对端关闭）返回 None。

        Raises:
            异常: 传输实现自身抛出的异常（由主循环归一处理）。
        """
        receive_task: asyncio.Task[str | None] = asyncio.create_task(
            self._transport.receive_text()
        )
        close_task: asyncio.Task[bool] = asyncio.create_task(self._close_event.wait())
        done, pending = await asyncio.wait(
            {receive_task, close_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if close_task in done and close_task.result():
            if not receive_task.done():
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            return None
        return receive_task.result()

    async def _handle_raw(self, raw: str) -> None:
        """解析并分派一条上行消息（非法载荷归一为可恢复 error）。

        Args:
            raw: 上行 JSON 文本。
        """
        try:
            message = REALTIME_INBOUND_ADAPTER.validate_json(raw)
        except ValidationError as exc:
            logger.debug(
                "实时语音上行消息非法: session_id={}, errors={}",
                self._session_id,
                exc.error_count(),
            )
            if self._is_oversized_audio_chunk(exc):
                self._metrics.audio_chunks_rejected += 1
                await self._send_error(
                    code=str(ErrorCode.VALIDATION_ERROR),
                    message=(
                        "音频块超过单块上限（"
                        f"{REALTIME_AUDIO_CHUNK_MAX_BYTES // 1024} KiB），"
                        "该块已丢弃（会话保留）"
                    ),
                    recoverable=True,
                )
                return
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message="上行消息格式非法（type / 字段 / base64 载荷校验失败）",
                recoverable=True,
            )
            return
        await self._dispatch(message)

    def _is_oversized_audio_chunk(self, exc: ValidationError) -> bool:
        """判定校验失败是否来自上行音频块超过单块上限。

        Args:
            exc: 上行载荷校验失败。

        Returns:
            True 表示 audio_chunk.data 因字节长度或 base64 长度上限被拒绝。
        """
        return any(
            error["type"] in _AUDIO_CHUNK_OVERSIZE_ERROR_TYPES
            and "audio_chunk" in error["loc"]
            for error in exc.errors()
        )

    async def _dispatch(self, message: RealtimeInboundMessageModel) -> None:
        """按消息类型分派（未 start 前的音频 / 控制消息一律拒绝）。

        Args:
            message: 已解析的上行消息。
        """
        if isinstance(message, RealtimeStartMessageModel):
            await self._handle_start(message)
            return
        if not self._started:
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message="会话尚未 start（首条上行消息必须是 start）",
                recoverable=True,
            )
            return
        if isinstance(message, RealtimeAudioInputChunkMessageModel):
            await self._handle_audio_chunk(message)
        elif isinstance(message, RealtimeEndMessageModel):
            await self._handle_end()
        elif isinstance(message, RealtimeBargeInMessageModel):
            await self._trigger_barge_in(RealtimeBargeInReason(message.reason))
        elif isinstance(message, RealtimeBargeInAckMessageModel):
            self._metrics.barge_in_acks += 1
            logger.debug(
                "客户端播放已清空: session_id={}, interruption_id={}",
                self._session_id,
                message.interruption_id,
            )
        elif isinstance(message, RealtimePingMessageModel):
            await self._send_state_message(force=True, detail="心跳应答")

    # ------------------------------------------------------------------
    # 会话建立
    # ------------------------------------------------------------------

    async def _handle_start(self, message: RealtimeStartMessageModel) -> None:
        """处理 start：能力校验 → ready → 进入聆听 → 启动音频工人。

        Args:
            message: start 消息。
        """
        if self._started:
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message="会话已建立（单连接单会话，start 只接受一次）",
                recoverable=True,
            )
            return
        if (
            message.sample_rate_hz is not None
            and message.sample_rate_hz != self._settings.sample_rate_hz
        ) or (message.channels is not None and message.channels != self._settings.channels):
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message=(
                    "客户端采集参数与服务端配置不一致（采样率 "
                    f"{self._settings.sample_rate_hz} / 声道 {self._settings.channels}）"
                ),
                recoverable=False,
            )
            await self._request_close(RealtimeCloseReason.SESSION_REJECTED)
            return
        if (
            message.persona_id is not None
            and self._persona is not None
            and message.persona_id != self._persona.id
        ):
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message="start.persona_id 与连接绑定的会话人格不一致",
                recoverable=False,
            )
            await self._request_close(RealtimeCloseReason.SESSION_REJECTED)
            return
        if not self._voice_settings.asr.enabled:
            await self._send_error(
                code=str(ErrorCode.CONFLICT),
                message="语音识别未启用（VOICE_ASR_ENABLED=true 时实时语音可用）",
                recoverable=False,
            )
            await self._request_close(RealtimeCloseReason.SESSION_REJECTED)
            return
        self._started = True
        self._metrics.started_ms = self._clock()
        self._last_inbound_ms = self._metrics.started_ms
        self._spawn(self._watchdog())
        tts_enabled = self._voice_settings.tts.enabled
        await self._send(
            RealtimeReadyMessageModel(
                ts_ms=self._clock(),
                protocol_version=REALTIME_PROTOCOL_VERSION,
                session_id=self._session_id,
                sample_rate_hz=self._settings.sample_rate_hz,
                channels=self._settings.channels,
                asr_enabled=True,
                tts_enabled=tts_enabled,
                barge_in_enabled=self._settings.barge_in_enabled,
                vad=RealtimeVadPublicSettingsModel(
                    speech_rms_threshold=self._settings.vad_speech_rms_threshold,
                    silence_rms_threshold=self._settings.vad_silence_rms_threshold,
                    min_speech_ms=self._settings.vad_min_speech_ms,
                    silence_end_ms=self._settings.vad_silence_end_ms,
                    max_segment_ms=self._settings.vad_max_segment_ms,
                ),
                limits=RealtimeSessionLimitsModel(
                    max_session_ms=self._settings.max_session_ms,
                    idle_timeout_ms=self._settings.idle_timeout_ms,
                    heartbeat_interval_ms=self._settings.heartbeat_interval_ms,
                    audio_queue_max_chunks=self._settings.audio_queue_max_chunks,
                    sentence_max_chars=self._settings.sentence_max_chars,
                ),
            )
        )
        await self._advance(RealtimeStateEvent.START)
        self._audio_task = self._spawn(self._audio_worker())
        logger.info(
            "实时语音会话已建立: session_id={}, persona_id={}, tts_enabled={}, barge_in={}",
            self._session_id,
            self._persona.id if self._persona is not None else None,
            tts_enabled,
            self._settings.barge_in_enabled,
        )

    # ------------------------------------------------------------------
    # 音频入队 / VAD 切段
    # ------------------------------------------------------------------

    async def _handle_audio_chunk(
        self, message: RealtimeAudioInputChunkMessageModel
    ) -> None:
        """上行音频块入队（溢出策略：丢最旧块并计数）。

        Args:
            message: 音频块消息。
        """
        try:
            pcm = decode_audio_base64(message.data)
        except ValueError:
            await self._send_error(
                code=str(ErrorCode.VALIDATION_ERROR),
                message="音频块 base64 解码失败（块已丢弃）",
                recoverable=True,
            )
            return
        if self._audio_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_queue.get_nowait()
            self._metrics.audio_chunks_dropped += 1
            if self._metrics.audio_chunks_dropped % 32 == 1:
                logger.warning(
                    "实时语音音频队列溢出（丢最旧块）: session_id={}, dropped={}, capacity={}",
                    self._session_id,
                    self._metrics.audio_chunks_dropped,
                    self._settings.audio_queue_max_chunks,
                )
        self._audio_queue.put_nowait(pcm)
        self._metrics.audio_chunks_received += 1

    async def _audio_worker(self) -> None:
        """音频工人：批量出队喂 VAD，按事件推进切段 / 转写 / 部分转写。

        Raises:
            asyncio.CancelledError: 关闭收尾取消（原样上抛）。
        """
        try:
            while not self._closed:
                first = await self._audio_queue.get()
                batch = [first]
                while len(batch) < self._settings.audio_batch_max_chunks:
                    try:
                        batch.append(self._audio_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                now = self._clock()
                for frame in batch:
                    result = self._vad.feed(frame, now)
                    for event in result.events:
                        await self._handle_vad_event(event, now)
                    self._schedule_partial(now)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 音频工人退出会让会话永久失聪（收帧仍在入队、无人消费）——
            # 下行可恢复 error 后主动关闭，不让会话静默挂到最大时长。
            self._task_failure_count += 1
            logger.opt(exception=True).error(
                "实时语音音频工人异常（关闭会话——不再静默失聪）: session_id={}",
                self._session_id,
            )
            await self._send_error(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="实时语音音频处理内部错误，会话已关闭",
                recoverable=True,
            )
            await self._request_close(
                RealtimeCloseReason.INTERNAL_ERROR, detail="audio_worker_exception"
            )

    async def _handle_vad_event(self, event: VadEventModel, now_ms_value: int) -> None:
        """处理 VAD 事件（起说 / 切段 / 丢弃）。

        Args:
            event: VAD 事件。
            now_ms_value: 事件时间（epoch 毫秒）。
        """
        if event.kind is VadEventKind.SPEECH_START:
            self._metrics.speech_starts += 1
            self._last_partial_voiced_ms = 0
            if self._current_segment_id is None:
                self._segment_seq += 1
                self._current_segment_id = f"seg-{self._segment_seq}"
            if self._settings.barge_in_enabled and self._machine.phase in (
                RealtimeSessionPhase.THINKING,
                RealtimeSessionPhase.SPEAKING,
            ):
                await self._trigger_barge_in(RealtimeBargeInReason.VAD)
            await self._advance(RealtimeStateEvent.SPEECH_START)
            return
        if event.kind is VadEventKind.SEGMENT_DISCARDED:
            self._metrics.segments_discarded += 1
            self._current_segment_id = None
            logger.debug(
                "实时语音短语音丢弃: session_id={}, voiced_ms={}",
                self._session_id,
                event.duration_ms,
            )
            return
        await self._handle_segment(event, forced=event.kind is VadEventKind.SEGMENT_FORCED)

    async def _handle_segment(self, event: VadEventModel, *, forced: bool) -> None:
        """切段投递：推进相位与指标，派生转写任务（音频工人不做内联等待）。

        Args:
            event: 切段事件（携带 PCM16 与时长）。
            forced: 是否由单段上限强制切段。
        """
        segment_id = self._current_segment_id
        if segment_id is None:
            self._segment_seq += 1
            segment_id = f"seg-{self._segment_seq}"
        self._current_segment_id = None
        await self._cancel_partial()
        await self._advance(RealtimeStateEvent.SEGMENT_READY)
        self._metrics.segments_ready += 1
        if forced:
            self._metrics.segments_forced += 1
        pcm = event.pcm16 or b""
        if not pcm:
            await self._advance(RealtimeStateEvent.TURN_DONE)
            return
        task = self._spawn(
            self._transcribe_segment(segment_id, pcm, event.duration_ms or 0)
        )
        self._transcribe_tasks.add(task)
        task.add_done_callback(self._transcribe_tasks.discard)

    async def _transcribe_segment(
        self, segment_id: str, pcm: bytes, duration_ms: int
    ) -> None:
        """转写单个语段并推进后续链路（独立任务——不阻塞音频工人与 VAD）。

        多段转写可并行；转写完成的文本按完成序进入生成请求通道（单会话
        同一时刻只有一个活跃轮次，其余排队并依次消费）。

        Args:
            segment_id: 语段标识（下行 final_transcript 使用）。
            pcm: 语段 PCM16 字节。
            duration_ms: 语段有效语音时长（毫秒）。
        """
        try:
            wav = pcm16_to_wav(pcm, self._settings.sample_rate_hz, self._settings.channels)
            result = await self._voice.transcribe(
                wav, f"{segment_id}.wav", persona=self._persona
            )
        except BusinessException as exc:
            self._metrics.asr_failures += 1
            await self._advance(RealtimeStateEvent.ASR_ERROR)
            logger.warning(
                "实时语音转写失败（会话保留）: session_id={}, segment_id={}, code={}",
                self._session_id,
                segment_id,
                exc.code,
            )
            await self._send_error(
                code=str(exc.code),
                message=f"语音转写失败：{exc.message}",
                recoverable=True,
            )
            return
        except Exception:  # 防御性：服务层之外的意外异常不打死会话
            self._metrics.asr_failures += 1
            await self._advance(RealtimeStateEvent.ASR_ERROR)
            logger.opt(exception=True).warning(
                "实时语音转写异常（会话保留）: session_id={}, segment_id={}",
                self._session_id,
                segment_id,
            )
            await self._send_error(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="语音转写内部错误（会话保留）",
                recoverable=True,
            )
            return
        text = result.text.strip()
        if not text:
            # 空转写（端点未识别出文字）不是「生成中」——直接收尾回聆听。
            await self._advance(RealtimeStateEvent.TURN_DONE)
            return
        self._metrics.final_transcripts += 1
        await self._send(
            RealtimeFinalTranscriptMessageModel(
                ts_ms=self._clock(),
                segment_id=segment_id,
                text=text,
                duration_ms=duration_ms,
                language=result.language,
            )
        )
        await self._advance(RealtimeStateEvent.ASR_DONE)
        self._request_generation(text)

    # ------------------------------------------------------------------
    # 中途转写（节流——真实取材为批量 ASR 的中途调用）
    # ------------------------------------------------------------------

    def _schedule_partial(self, now_ms_value: int) -> None:
        """按语音进度节流调度中途转写（无流式 ASR 端点时的 partial 来源）。

        节流按**已积累语音时长**计量（而非墙钟）：批量收帧节奏不影响
        节流口径，伪时钟测试与生产同规则。``now_ms_value`` 仅用于事件
        时间戳传递（中途转写自身不读时钟）。

        Args:
            now_ms_value: 当前时间（epoch 毫秒——仅用于诊断上下文）。
        """
        del now_ms_value
        if self._settings.partial_transcript_interval_ms <= 0:
            return
        if self._partial_task is not None or not self._vad.speaking:
            return
        segment_id = self._current_segment_id
        if segment_id is None:
            return
        voiced_ms = self._vad.voiced_ms
        if voiced_ms < self._settings.partial_min_speech_ms:
            return
        if voiced_ms - self._last_partial_voiced_ms < self._settings.partial_transcript_interval_ms:
            return
        self._last_partial_voiced_ms = voiced_ms
        audio = self._vad.current_audio()
        if not audio:
            return
        self._partial_task = self._spawn(self._partial_loop(segment_id, audio))

    async def _partial_loop(self, segment_id: str, pcm: bytes) -> None:
        """执行一次中途转写（语段已切 / 已变则丢弃结果）。

        Args:
            segment_id: 语段标识（结果只有在语段仍进行时下行）。
            pcm: 当前已缓冲的 PCM16 字节。
        """
        try:
            wav = pcm16_to_wav(pcm, self._settings.sample_rate_hz, self._settings.channels)
            result = await self._voice.transcribe(
                wav, f"{segment_id}-partial.wav", persona=self._persona
            )
            if self._current_segment_id != segment_id:
                return
            text = result.text.strip()
            if not text:
                return
            self._metrics.partial_transcripts += 1
            await self._send(
                RealtimePartialTranscriptMessageModel(
                    ts_ms=self._clock(), segment_id=segment_id, text=text
                )
            )
        except BusinessException as exc:
            logger.debug(
                "中途转写失败（忽略——最终转写仍然可用）: session_id={}, code={}",
                self._session_id,
                exc.code,
            )
        finally:
            self._partial_task = None

    async def _cancel_partial(self) -> None:
        """取消在途中途转写（语段切换 / 关闭前调用）。"""
        task = self._partial_task
        if task is None:
            return
        self._partial_task = None
        await self._cancel_task(task)

    # ------------------------------------------------------------------
    # 回复生成 / 句切 / 逐句 TTS
    # ------------------------------------------------------------------

    def _request_generation(self, user_text: str) -> None:
        """请求一轮回复生成（有在途轮次时排队——串行化，无音频混流）。

        Args:
            user_text: 用户转写文本。
        """
        if self._current_turn_id is not None:
            self._pending_user_texts.append(user_text)
            return
        self._begin_turn(user_text)

    def _begin_turn(self, user_text: str) -> None:
        """启动一轮生成（同步占用轮次槽位——异步体见 ``_run_turn``）。

        Args:
            user_text: 用户转写文本。
        """
        self._turn_seq += 1
        turn_id = f"turn-{self._turn_seq}"
        self._current_turn_id = turn_id
        self._accumulator = SentenceAccumulator(self._settings.sentence_max_chars)
        self._sentence_seq = 0
        self._generation_done = False
        self._turn_idle_event.clear()
        self._metrics.generations += 1
        self._generation_task = self._spawn(self._run_turn(user_text, turn_id))

    async def _run_turn(self, user_text: str, turn_id: str) -> None:
        """执行一轮生成：流式增量 → 句切成句 → 入 TTS 队列。

        Args:
            user_text: 用户转写文本。
            turn_id: 轮次标识。
        """
        cancelled = False
        parts: list[str] = []
        try:
            async for delta in self._chat.stream_turn(user_text):
                if self._current_turn_id != turn_id:
                    return
                if not delta:
                    continue
                parts.append(delta)
                await self._send(
                    RealtimeAssistantTextMessageModel(
                        ts_ms=self._clock(), turn_id=turn_id, text=delta, final=False
                    )
                )
                accumulator = self._accumulator
                if accumulator is not None:
                    for sentence in accumulator.feed(delta):
                        self._enqueue_sentence(turn_id, sentence)
            accumulator = self._accumulator
            if accumulator is not None:
                for sentence in accumulator.flush():
                    self._enqueue_sentence(turn_id, sentence)
            full_text = "".join(parts).strip()
            if not full_text:
                raise BusinessException(
                    code=ErrorCode.LLM_CALL_FAILED,
                    message="模型流式回复为空",
                )
            await self._send(
                RealtimeAssistantTextMessageModel(
                    ts_ms=self._clock(), turn_id=turn_id, text=full_text, final=True
                )
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BusinessException as exc:
            self._metrics.generation_failures += 1
            await self._advance(RealtimeStateEvent.GENERATION_ERROR)
            logger.warning(
                "实时语音回复生成失败（会话保留）: session_id={}, turn_id={}, code={}",
                self._session_id,
                turn_id,
                exc.code,
            )
            await self._send_error(
                code=str(exc.code),
                message=f"回复生成失败：{exc.message}",
                recoverable=True,
            )
        except Exception:  # 防御性：生成面意外异常不打死会话
            self._metrics.generation_failures += 1
            await self._advance(RealtimeStateEvent.GENERATION_ERROR)
            logger.opt(exception=True).warning(
                "实时语音回复生成异常（会话保留）: session_id={}, turn_id={}",
                self._session_id,
                turn_id,
            )
            await self._send_error(
                code=str(ErrorCode.INTERNAL_ERROR),
                message="回复生成内部错误（会话保留）",
                recoverable=True,
            )
        finally:
            if not cancelled:
                self._generation_done = True
                self._generation_task = None
                await self._maybe_finish_turn()

    def _enqueue_sentence(self, turn_id: str, sentence: str) -> None:
        """把成句入 TTS 队列（TTS 关闭时只计数，不产生音频作业）。

        Args:
            turn_id: 轮次标识。
            sentence: 成句文本（非空）。
        """
        if not sentence:
            return
        if not self._voice_settings.tts.enabled:
            return
        sentence_index = self._sentence_seq
        self._sentence_seq += 1
        self._tts_queue.put_nowait(
            TtsSentenceJobModel(turn_id=turn_id, sentence_index=sentence_index, text=sentence)
        )
        self._tts_pending += 1
        if self._tts_task is None:
            self._tts_task = self._spawn(self._tts_worker(turn_id))

    async def _tts_worker(self, turn_id: str) -> None:
        """逐句合成并分片下行（队列空即退出；新句入队时重新拉起）。

        Args:
            turn_id: 启动本工人时的轮次标识（跨轮陈旧作业直接丢弃）。
        """
        cancelled = False
        try:
            while not self._closed:
                try:
                    job = self._tts_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if job.turn_id != self._current_turn_id:
                    self._tts_pending = max(0, self._tts_pending - 1)
                    await self._maybe_finish_turn()
                    continue
                await self._synthesize_job(job)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                self._tts_task = None
                await self._maybe_finish_turn()

    async def _synthesize_job(self, job: TtsSentenceJobModel) -> None:
        """合成单句并下行全部分片（失败计指标 + 可恢复 error）。

        Args:
            job: 逐句作业。
        """
        try:
            result = await self._voice.synthesize(job.text, persona=self._persona)
        except asyncio.CancelledError:
            self._tts_pending = max(0, self._tts_pending - 1)
            raise
        except BusinessException as exc:
            self._metrics.tts_failures += 1
            await self._advance(RealtimeStateEvent.TTS_FAILURE)
            logger.warning(
                "实时语音合成失败（跳过该句）: session_id={}, turn_id={}, code={}",
                self._session_id,
                job.turn_id,
                exc.code,
            )
            await self._send_error(
                code=str(exc.code),
                message=f"语音合成失败（该句已跳过）：{exc.message}",
                recoverable=True,
            )
            self._tts_pending = max(0, self._tts_pending - 1)
            await self._maybe_finish_turn()
            return
        chunks = list(iter_byte_chunks(result.audio, self._settings.tts_chunk_bytes))
        for seq, chunk in enumerate(chunks):
            if self._current_turn_id != job.turn_id:
                break
            if seq == 0:
                await self._advance(RealtimeStateEvent.AUDIO_PLAYING)
            self._metrics.audio_chunks_sent += 1
            await self._send(
                RealtimeAudioOutputChunkMessageModel(
                    ts_ms=self._clock(),
                    turn_id=job.turn_id,
                    sentence_index=job.sentence_index,
                    seq=seq,
                    total_chunks=len(chunks),
                    content_type="audio/wav",
                    data=base64.b64encode(chunk).decode("ascii"),
                )
            )
        self._metrics.tts_sentences += 1
        self._tts_pending = max(0, self._tts_pending - 1)
        await self._maybe_finish_turn()

    async def _maybe_finish_turn(self) -> None:
        """轮次收尾判定（生成结束且逐句队列清空 → 回到聆听 / 启动排队轮次）。"""
        if self._current_turn_id is None:
            return
        if not self._generation_done or self._tts_pending > 0:
            return
        self._current_turn_id = None
        self._accumulator = None
        await self._advance(RealtimeStateEvent.TURN_DONE)
        self._turn_idle_event.set()
        if self._pending_user_texts and not self._closing:
            self._begin_turn(self._pending_user_texts.pop(0))

    # ------------------------------------------------------------------
    # 打断
    # ------------------------------------------------------------------

    async def _trigger_barge_in(self, reason: RealtimeBargeInReason) -> None:
        """打断：取消在途生成与逐句 TTS、清空队列、下行 interrupted + state。

        Args:
            reason: 打断来源（VAD 新语音 / 客户端按钮）。
        """
        if not self._settings.barge_in_enabled:
            return
        has_turn = self._current_turn_id is not None
        busy_phase = self._machine.phase in (
            RealtimeSessionPhase.THINKING,
            RealtimeSessionPhase.SPEAKING,
        )
        if not has_turn and not busy_phase:
            return
        self._interruption_seq += 1
        interruption_id = f"intr-{self._interruption_seq}"
        interrupted_turn = self._current_turn_id
        generation_task = self._generation_task
        tts_task = self._tts_task
        await self._cancel_partial()
        dropped_sentences = await self._cancel_inflight_turn(drop_pending=False)
        self._metrics.interruptions += 1
        await self._send(
            RealtimeInterruptedMessageModel(
                ts_ms=self._clock(),
                interruption_id=interruption_id,
                turn_id=interrupted_turn,
                reason=reason,
            )
        )
        await self._advance(RealtimeStateEvent.BARGE_IN, detail="打断后回到聆听")
        logger.warning(
            "实时语音打断: session_id={}, reason={}, turn_id={}, dropped_sentences={}, "
            "generation_cancelled={}, tts_cancelled={}",
            self._session_id,
            reason.value,
            interrupted_turn,
            dropped_sentences,
            generation_task is not None,
            tts_task is not None,
        )

    # ------------------------------------------------------------------
    # 结束 / 看门狗 / 关闭
    # ------------------------------------------------------------------

    async def _cancel_inflight_turn(self, *, drop_pending: bool) -> int:
        """取消在途生成与逐句 TTS、清空未合成句队列（打断与关闭抢占共用）。

        Args:
            drop_pending: 是否丢弃排队中的用户文本（关闭路径 True——不会再有
                后续轮次消费；打断路径 False——后续语段仍按序生成）。

        Returns:
            被丢弃的未合成句数（诊断日志用）。
        """
        dropped_sentences = 0
        while True:
            try:
                self._tts_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped_sentences += 1
        self._current_turn_id = None
        self._accumulator = None
        self._generation_done = True
        self._tts_pending = 0
        self._sentence_seq = 0
        generation_task = self._generation_task
        self._generation_task = None
        tts_task = self._tts_task
        self._tts_task = None
        if drop_pending:
            self._pending_user_texts.clear()
        await self._cancel_task(generation_task)
        await self._cancel_task(tts_task)
        self._turn_idle_event.set()
        return dropped_sentences

    async def _handle_end(self) -> None:
        """客户端 end：停止收帧 → 冲刷尾段 → 排空在途转写与轮次 → 关闭。

        排空阶段与关闭事件竞争：服务端超时 / 内部关闭先到时立即取消在途
        生成与 TTS 并退出排空，最终收尾由主循环统一执行。
        """
        if self._audio_task is not None:
            await self._cancel_task(self._audio_task)
            self._audio_task = None
        now = self._clock()
        for event in self._vad.flush(now).events:
            await self._handle_vad_event(event, now)
        await self._drain_before_close()
        if not self._closing:
            await self._request_close(RealtimeCloseReason.CLIENT_END)

    async def _drain_before_close(self) -> None:
        """排空在途转写与轮次播报；关闭事件先到则取消在途工作并返回。"""
        while True:
            if self._close_event.is_set():
                await self._cancel_inflight_turn(drop_pending=True)
                return
            pending_transcriptions = [
                task for task in self._transcribe_tasks if not task.done()
            ]
            if pending_transcriptions:
                await self._wait_drain_progress(pending_transcriptions)
                continue
            if self._current_turn_id is None and not self._pending_user_texts:
                return
            turn_idle_wait: asyncio.Task[bool] = asyncio.create_task(
                self._turn_idle_event.wait()
            )
            close_wait: asyncio.Task[bool] = asyncio.create_task(
                self._close_event.wait()
            )
            done, pending = await asyncio.wait(
                {turn_idle_wait, close_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            for waiter in pending:
                waiter.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if close_wait in done and close_wait.result():
                await self._cancel_inflight_turn(drop_pending=True)
                return

    async def _wait_drain_progress(self, tasks: list[asyncio.Task[None]]) -> None:
        """等待任一在途转写结束或关闭事件先到（排空循环的唤醒点）。

        Args:
            tasks: 在途转写任务快照。
        """
        close_wait: asyncio.Task[bool] = asyncio.create_task(self._close_event.wait())
        done, _ = await asyncio.wait(
            {*tasks, close_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if close_wait not in done:
            close_wait.cancel()
            await asyncio.gather(close_wait, return_exceptions=True)

    async def _watchdog(self) -> None:
        """看门狗：空闲超时 / 最大时长判定（注入时钟与休眠面）。"""
        while not self._closed:
            await self._sleeper(self._watchdog_tick_seconds)
            now = self._clock()
            if now - self._last_inbound_ms >= self._settings.idle_timeout_ms:
                await self._request_close(RealtimeCloseReason.IDLE_TIMEOUT)
                return
            if now - self._metrics.started_ms >= self._settings.max_session_ms:
                await self._request_close(RealtimeCloseReason.MAX_DURATION)
                return

    async def _request_close(
        self, reason: RealtimeCloseReason, *, detail: str | None = None
    ) -> None:
        """请求关闭（幂等；首个原因生效，随后唤醒主循环收尾）。

        相位迁移先于 ``_closing`` 置位：``_send`` 在 ``_closing`` 置位后默认
        抑制下行，先迁移才能把 state(closed) 真实下发（关闭路径的 wire 顺序
        为（可选 error）→ state(closed) → closed）。幂等性由 ``_close_reason``
        先行占位保证。

        Args:
            reason: 关闭原因。
            detail: 附加说明（诊断用）。
        """
        if self._close_reason is not None:
            return
        self._close_reason = reason
        self._close_detail = detail
        if reason in (
            RealtimeCloseReason.CLIENT_END,
            RealtimeCloseReason.IDLE_TIMEOUT,
            RealtimeCloseReason.MAX_DURATION,
        ):
            await self._advance(
                RealtimeStateEvent.CLIENT_END
                if reason is RealtimeCloseReason.CLIENT_END
                else (
                    RealtimeStateEvent.IDLE_TIMEOUT
                    if reason is RealtimeCloseReason.IDLE_TIMEOUT
                    else RealtimeStateEvent.MAX_DURATION
                )
            )
        else:
            await self._advance(RealtimeStateEvent.INTERNAL_ERROR)
        if self._closing:
            return
        self._closing = True
        self._close_event.set()

    async def _send_closed(self, reason: RealtimeCloseReason, detail: str | None) -> None:
        """下行 closed（发送失败仅告警——对端可能已断开）。

        Args:
            reason: 关闭原因。
            detail: 附加说明。
        """
        if self._closed_sent:
            return
        self._closed_sent = True
        await self._send(
            RealtimeClosedMessageModel(ts_ms=self._clock(), reason=reason, detail=detail),
            force=True,
        )

    async def _finalize(self, reason: RealtimeCloseReason) -> None:
        """收尾：取消全部派生任务、补发 closed、关闭传输、落指标日志。

        Args:
            reason: 关闭原因（主循环归一的最终值）。
        """
        if self._finalized:
            return
        self._finalized = True
        self._closed = True
        self._closing = True
        self._close_reason = reason
        self._close_event.set()
        await self._send_closed(reason, self._close_detail)
        current = asyncio.current_task()
        stale = [task for task in self._tasks if task is not current]
        for task in stale:
            task.cancel()
        if stale:
            await asyncio.gather(*stale, return_exceptions=True)
        self._tasks.clear()
        self._transcribe_tasks.clear()
        with contextlib.suppress(Exception):
            await self._transport.close(1000, reason.value)
        self._metrics.closed_reason = reason
        snapshot = self._metrics.model_copy()
        logger.info(
            "实时语音会话已关闭: session_id={}, reason={}, audio_chunks={}, dropped={}, "
            "rejected={}, segments={}, partials={}, finals={}, generations={}, "
            "tts_sentences={}, audio_chunks_sent={}, interruptions={}, asr_failures={}, "
            "tts_failures={}, generation_failures={}, duration_ms={}",
            self._session_id,
            reason.value,
            snapshot.audio_chunks_received,
            snapshot.audio_chunks_dropped,
            snapshot.audio_chunks_rejected,
            snapshot.segments_ready,
            snapshot.partial_transcripts,
            snapshot.final_transcripts,
            snapshot.generations,
            snapshot.tts_sentences,
            snapshot.audio_chunks_sent,
            snapshot.interruptions,
            snapshot.asr_failures,
            snapshot.tts_failures,
            snapshot.generation_failures,
            self._clock() - snapshot.started_ms,
        )

    # ------------------------------------------------------------------
    # 下行发送 / 状态迁移 / 任务登记
    # ------------------------------------------------------------------

    async def _send(
        self, message: RealtimeOutboundMessageModel, *, force: bool = False
    ) -> None:
        """发送下行消息（关闭后默认抑制；``force`` 供 closed 使用）。

        Args:
            message: 下行消息。
            force: 关闭后仍发送（仅 closed 使用）。
        """
        if self._closing and not force:
            return
        try:
            await self._transport.send_text(message.model_dump_json())
        except Exception:  # 传输已断：告警并唤醒主循环收尾
            if not self._closing:
                logger.opt(exception=True).warning(
                    "实时语音下行发送失败（会话收尾）: session_id={}, type={}",
                    self._session_id,
                    message.type,
                )
            self._close_event.set()

    async def _send_state_message(
        self, *, force: bool = False, detail: str | None = None
    ) -> None:
        """发送当前相位（state 消息——同时作为心跳应答）。

        Args:
            force: 关闭后仍发送。
            detail: 迁移说明（None = 当前相位摘要）。
        """
        await self._send(
            RealtimeStateMessageModel(
                ts_ms=self._clock(), state=self._machine.phase, detail=detail
            ),
            force=force,
        )

    async def _send_error(self, *, code: str, message: str, recoverable: bool) -> None:
        """发送 error 消息。

        Args:
            code: 错误码字符串。
            message: 面向用户的中文提示。
            recoverable: 会话是否可继续。
        """
        await self._send(
            RealtimeErrorMessageModel(
                ts_ms=self._clock(), code=code, message=message, recoverable=recoverable
            )
        )

    async def _advance(self, event: RealtimeStateEvent, *, detail: str | None = None) -> None:
        """执行状态迁移并在相位变更时下行 state（非法事件记 warning 继续）。

        Args:
            event: 状态事件。
            detail: state 消息的迁移说明（None = 使用规则表语义）。
        """
        transition = self._machine.transition(event, ts_ms=self._clock())
        if not transition.allowed:
            logger.warning(
                "实时语音非法状态迁移（已忽略）: session_id={}, detail={}",
                self._session_id,
                transition.detail,
            )
            return
        if transition.changed:
            await self._send_state_message(detail=detail or transition.detail)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """登记并启动派生任务（关闭时统一取消；异常经 done 回调检索）。

        Args:
            coro: 协程体。

        Returns:
            已登记的 :class:`asyncio.Task`。
        """
        task_name = getattr(coro, "__qualname__", type(coro).__name__)
        task = asyncio.create_task(coro)

        def _on_done(done: asyncio.Task[None]) -> None:
            """回收登记并消费未处理异常（避免 never retrieved 静默丢失）。"""
            self._tasks.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is None:
                return
            self._task_failure_count += 1
            logger.opt(exception=exc).error(
                "实时语音派生任务异常（已检索）: session_id={}, task={}",
                self._session_id,
                task_name,
            )

        self._tasks.add(task)
        task.add_done_callback(_on_done)
        return task

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        """取消并回收派生任务（幂等；完成后从任务集合移除）。

        Args:
            task: 待取消任务（None = 跳过）。
        """
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._tasks.discard(task)

