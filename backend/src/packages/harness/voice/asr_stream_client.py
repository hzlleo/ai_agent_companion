"""voice 域流式 ASR 客户端——Qwen3-ASR 实时 WebSocket 协议（传输注入）。

**部署契约（本客户端持有文档——部署侧据此包装）**：

    Qwen3-ASR 实时识别（``qwen3-asr-flash-realtime``）以「实时转写事件族」
    WebSocket 服务形态部署（DashScope 实时协议同形；部署侧 ego 包装只需
    逐事件透传，无需改写事件名）::

        ws {VOICE_ASR_STREAMING_BASE_URL}
        → {"type": "session.update",
           "session": {"model": str, "input_audio_format": "pcm16",
                       "sample_rate_hz": int, "language": str | null}}
        ← {"type": "session.created"} / {"type": "session.updated"}   # 握手确认
        → {"type": "input_audio_buffer.append", "audio": base64(pcm16)}
        → {"type": "input_audio_buffer.commit"}
        → {"type": "session.finish"}
        ← {"type": "conversation.item.input_audio_transcription.text",
           "text": str}                                              # 进行中（partial）
        ← {"type": "conversation.item.input_audio_transcription.completed",
           "transcript": str}                                        # 最终稿
        ← {"type": "session.finished"}                               # 收尾确认
        ← {"type": "error", "error": {"code": str, "message": str}}   # 服务端错误
        → {"type": "session.cancel"}                                 # 客户端取消（best-effort）

    鉴权沿用语部分节（``VOICE_ASR_*`` 凭据），本客户端不重复持有密钥键；
    端点 / 模型 / 超时三者即最小配置面（见 :class:`AsrSettings`）。

**错误码映射表**（复用既有码表，``core/errors.py`` 不动；与批量客户端
同段说明：依赖不可用类对应 ``LLM_CALL_FAILED``）::

    ============================  ========================  ==========
    失败类别                       错误码                    可调用方处置
    ============================  ========================  ==========
    连接失败 / 打开超时             LLM_CALL_FAILED           回退批量主链
    握手超时 / 握手被拒             LLM_CALL_FAILED           回退批量主链
    事件 JSON 非法 / 形态不符       LLM_CALL_FAILED           回退批量主链
    收尾前对端关闭（中途断流）      LLM_CALL_FAILED           回退批量主链
    收尾等待超时                    LLM_CALL_FAILED           回退批量主链
    已关闭后继续喂帧 / 收尾         LLM_CALL_FAILED           调用方 bug 面
    ============================  ========================  ==========

    调用方（实时会话）对任何 ``LLM_CALL_FAILED`` 都执行一次告警 + 指标计数
    并回退既有批量转写——流式通道的任何失败都不打断会话。

并发与资源约束：

    - **单连接单语段**：一次 :meth:`Qwen3AsrStreamClient.open` 对应一个
      VAD 语段；语段切分 / 丢弃 / 打断 / 会话关闭都经 :meth:`cancel` 或
      :meth:`AsrStreamHandle.finish` 收敛连接（零残留）。
    - **喂帧背压**：发送队列有界，满时丢最旧帧并计数（实时识别宁丢旧帧
      不阻塞 VAD 收帧循环）；丢弃数经 :attr:`AsrStreamHandle.frames_dropped`
      暴露，会话指标累计。
    - **保活与超时**：连接期间按 ``heartbeat_interval_seconds`` 发协议层
      保活；握手与收尾事件等待均受 ``timeout_seconds`` 硬约束（测试注入
      伪传输即可零真实等待）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Final, Protocol

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.voice.stream_models import (
    STREAM_CLOSE_GOING_AWAY,
    STREAM_CLOSE_NORMAL,
    STREAM_CLOSE_PROTOCOL_ERROR,
    VoiceStreamConnector,
    VoiceStreamTransportFace,
    build_text_frame,
)
from packages.harness.voice.voice_models import AsrResultModel

__all__ = [
    "AsrStreamHandleFace",
    "AsrStreamClientFace",
    "Qwen3AsrStreamClient",
    "AsrStreamHandle",
    "ASR_STREAM_EVENT_SESSION_UPDATE",
    "ASR_STREAM_EVENT_APPEND",
    "ASR_STREAM_EVENT_COMMIT",
    "ASR_STREAM_EVENT_FINISH",
    "ASR_STREAM_EVENT_CANCEL",
]

# --- 协议事件名（客户端 → 服务端） ---
#: 会话参数下发（握手首帧）。
ASR_STREAM_EVENT_SESSION_UPDATE: Final[str] = "session.update"
#: 音频分片追加。
ASR_STREAM_EVENT_APPEND: Final[str] = "input_audio_buffer.append"
#: 音频结束提交（触发最终转写）。
ASR_STREAM_EVENT_COMMIT: Final[str] = "input_audio_buffer.commit"
#: 会话收尾（服务端回 ``session.finished``）。
ASR_STREAM_EVENT_FINISH: Final[str] = "session.finish"
#: 客户端取消（best-effort——服务端可忽略）。
ASR_STREAM_EVENT_CANCEL: Final[str] = "session.cancel"

# --- 协议事件名（服务端 → 客户端） ---
_SERVER_EVENT_HANDSHAKE: frozenset[str] = frozenset({"session.created", "session.updated"})
_SERVER_EVENT_PARTIAL: Final[str] = "conversation.item.input_audio_transcription.text"
_SERVER_EVENT_FINAL: Final[str] = "conversation.item.input_audio_transcription.completed"
_SERVER_EVENT_FINISHED: Final[str] = "session.finished"
_SERVER_EVENT_ERROR: Final[str] = "error"

#: 协议层保活间隔（秒——控制帧，对端无需回业务消息）。
_DEFAULT_HEARTBEAT_SECONDS: Final[float] = 5.0
#: 喂帧发送队列上限（帧；溢出丢最旧）。
_DEFAULT_SEND_QUEUE_MAX: Final[int] = 32
#: 错误 details 携带的错误文本截断长度。
_ERROR_TEXT_LIMIT: Final[int] = 300


class AsrStreamHandleFace(Protocol):
    """流式识别连接句柄（会话层消费面：喂帧 / 收尾 / 取消）。"""

    async def feed(self, pcm16: bytes) -> None:
        """追加一帧 PCM16 音频（背压队列满时丢最旧帧）。

        Args:
            pcm16: 16 位小端单声道 PCM 字节。
        """
        ...

    async def finish(self) -> AsrResultModel:
        """提交并等待最终转写（成功后关闭连接）。

        Returns:
            最终转写结果（text / language）。

        Raises:
            BusinessException: 收尾失败 / 超时 / 中途断流（``LLM_CALL_FAILED``）。
        """
        ...

    async def cancel(self) -> None:
        """取消并关闭连接（幂等；丢弃未落定的最终稿）。"""
        ...

    @property
    def frames_dropped(self) -> int:
        """发送队列溢出丢弃的帧数。

        Returns:
            丢弃帧计数。
        """
        ...


class AsrStreamClientFace(Protocol):
    """流式识别客户端注入面协议（真实客户端 / 测试 fake 均满足）。"""

    async def open(
        self,
        *,
        sample_rate_hz: int,
        language: str | None,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> AsrStreamHandleFace:
        """建立一条流式识别连接（握手完成才返回）。

        Args:
            sample_rate_hz: 上行 PCM 采样率（Hz）。
            language: 语言提示（None = 服务端自动检测）。
            on_partial: 进行中转写回调。

        Returns:
            已握手的连接句柄。

        Raises:
            BusinessException: 连接 / 握手失败（``LLM_CALL_FAILED``）。
        """
        ...


class AsrStreamServerErrorModel(BaseModel):
    """服务端错误事件的 error 子对象（宽容解析——未知字段忽略）。

    Attributes:
        code: 服务端错误码（可空）。
        message: 服务端错误描述（可空）。
    """

    code: str | None = Field(default=None, description="服务端错误码")
    message: str | None = Field(default=None, description="服务端错误描述")
    model_config = ConfigDict(extra="ignore")


class AsrStreamServerEventModel(BaseModel):
    """服务端实时事件（宽容解析——未知事件类型与字段一律忽略）。

    Attributes:
        type: 事件类型（判别键）。
        text: 进行中转写文本（partial 事件）。
        transcript: 最终转写文本（completed 事件）。
        error: 服务端错误详情（error 事件）。
    """

    type: str = Field(min_length=1, description="事件类型")
    text: str | None = Field(default=None, description="进行中转写文本")
    transcript: str | None = Field(default=None, description="最终转写文本")
    error: AsrStreamServerErrorModel | None = Field(default=None, description="错误详情")
    model_config = ConfigDict(extra="ignore")


class Qwen3AsrStreamClient:
    """Qwen3-ASR 实时识别客户端（传输注入；每次 ``open`` 建一条连接）。

    Args:
        connector: 传输连接工厂（生产 = :func:`make_websocket_connector`；
            测试 = 内存 fake 工厂）。
        endpoint: 端点描述（仅错误 details 观测面——传输工厂已绑定实际 URL）。
        model: 握手 ``session.model`` 字段值。
        timeout_seconds: 握手 / 收尾事件等待超时秒数。
        heartbeat_interval_seconds: 协议层保活间隔（秒）。
        send_queue_max: 喂帧发送队列上限（帧；溢出丢最旧）。
    """

    def __init__(
        self,
        connector: VoiceStreamConnector,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        send_queue_max: int = _DEFAULT_SEND_QUEUE_MAX,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("流式识别超时必须为正")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("流式识别保活间隔必须为正")
        if send_queue_max < 1:
            raise ValueError("流式识别发送队列上限必须为正")
        self._connector = connector
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_seconds
        self._heartbeat = heartbeat_interval_seconds
        self._send_queue_max = send_queue_max

    async def open(
        self,
        *,
        sample_rate_hz: int,
        language: str | None,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> AsrStreamHandle:
        """建立连接并完成握手（失败即上抛——调用方回退批量主链）。

        Args:
            sample_rate_hz: 上行 PCM 采样率（Hz）。
            language: 语言提示（None = 服务端自动检测）。
            on_partial: 进行中转写回调。

        Returns:
            已握手的连接句柄。

        Raises:
            BusinessException: 连接 / 握手失败（``LLM_CALL_FAILED``——映射表
                见模块 docstring）。
        """
        if sample_rate_hz <= 0:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="流式识别的上行采样率必须为正",
                details={"sample_rate_hz": sample_rate_hz},
            )
        transport = await self._connect()
        try:
            await self._handshake(transport, sample_rate_hz, language)
        except BaseException:
            await self._close_quietly(transport, STREAM_CLOSE_PROTOCOL_ERROR, "handshake_failed")
            raise
        handle = AsrStreamHandle(
            transport,
            endpoint=self._endpoint,
            model=self._model,
            timeout_seconds=self._timeout,
            heartbeat_interval_seconds=self._heartbeat,
            send_queue_max=self._send_queue_max,
            on_partial=on_partial,
        )
        await handle.start()
        return handle

    async def _connect(self) -> VoiceStreamTransportFace:
        """建立传输连接（打开超时硬约束）。

        Returns:
            已连接传输面。

        Raises:
            BusinessException: 连接失败 / 打开超时（``LLM_CALL_FAILED``）。
        """
        started_ms = now_ms()
        try:
            return await asyncio.wait_for(self._connector(), timeout=self._timeout)
        except TimeoutError as exc:
            raise self._call_error(
                reason="流式识别连接打开超时",
                latency_ms=now_ms() - started_ms,
            ) from exc
        except OSError as exc:
            raise self._call_error(
                reason="流式识别连接失败",
                error=str(exc)[:_ERROR_TEXT_LIMIT],
                latency_ms=now_ms() - started_ms,
            ) from exc

    async def _handshake(
        self,
        transport: VoiceStreamTransportFace,
        sample_rate_hz: int,
        language: str | None,
    ) -> None:
        """下发会话参数并等待握手确认（超时 / 拒绝即失败）。

        Args:
            transport: 已连接传输面。
            sample_rate_hz: 上行采样率。
            language: 语言提示。

        Raises:
            BusinessException: 握手被拒 / 超时 / 中途关闭（``LLM_CALL_FAILED``）。
        """
        session: dict[str, Any] = {
            "model": self._model,
            "input_audio_format": "pcm16",
            "sample_rate_hz": sample_rate_hz,
            "language": language,
        }
        payload = json.dumps(
            {"type": ASR_STREAM_EVENT_SESSION_UPDATE, "session": session}, ensure_ascii=False
        )
        started_ms = now_ms()
        try:
            await transport.send(build_text_frame(payload))
            while True:
                frame = await asyncio.wait_for(transport.receive(), timeout=self._timeout)
                if frame is None:
                    raise self._call_error(
                        reason="流式识别握手期间连接被关闭",
                        close_code=transport.last_close_code,
                        latency_ms=now_ms() - started_ms,
                    )
                if frame.kind != "text":
                    continue
                event = self._parse_event(frame.text or "")
                if event.type in _SERVER_EVENT_HANDSHAKE:
                    return
                if event.type == _SERVER_EVENT_ERROR:
                    raise self._call_error(
                        reason="流式识别握手被服务端拒绝",
                        server_error=self._server_error_text(event),
                        latency_ms=now_ms() - started_ms,
                    )
        except TimeoutError as exc:
            raise self._call_error(
                reason="流式识别握手确认超时",
                latency_ms=now_ms() - started_ms,
            ) from exc

    def _parse_event(self, payload: str) -> AsrStreamServerEventModel:
        """解析服务端事件 JSON（形态非法即失败）。

        Args:
            payload: 文本帧载荷。

        Returns:
            事件模型。

        Raises:
            BusinessException: JSON 非法 / 缺 type（``LLM_CALL_FAILED``）。
        """
        try:
            return AsrStreamServerEventModel.model_validate_json(payload)
        except ValidationError as exc:
            raise self._call_error(
                reason="流式识别事件 JSON 形态非法",
                parse_error=str(exc)[:_ERROR_TEXT_LIMIT],
                latency_ms=0,
            ) from exc

    @staticmethod
    def _server_error_text(event: AsrStreamServerEventModel) -> str:
        """拼装服务端错误文本（诊断面）。

        Args:
            event: 错误事件。

        Returns:
            ``code: message`` 形态的截断文本。
        """
        detail = event.error
        if detail is None:
            return "unknown"
        parts = [part for part in (detail.code, detail.message) if part]
        return ": ".join(parts)[:_ERROR_TEXT_LIMIT] or "unknown"

    async def _close_quietly(
        self, transport: VoiceStreamTransportFace, code: int, reason: str
    ) -> None:
        """尽力关闭传输（失败仅忽略——不再叠加异常）。

        Args:
            transport: 待关闭传输面。
            code: 关闭码。
            reason: 关闭原因。
        """
        with contextlib.suppress(Exception):
            await transport.close(code, reason)

    def _call_error(
        self,
        *,
        reason: str,
        latency_ms: int,
        error: str | None = None,
        parse_error: str | None = None,
        server_error: str | None = None,
        close_code: int | None = None,
    ) -> BusinessException:
        """构造 ``LLM_CALL_FAILED`` 业务异常（流式识别段统一 details）。

        Args:
            reason: 失败类别描述。
            latency_ms: 失败路径耗时（观测面）。
            error: 传输异常描述（连接失败形态携带）。
            parse_error: JSON / 形态解析错误（契约破坏形态携带）。
            server_error: 服务端错误文本（握手拒绝 / 服务端 error 事件携带）。
            close_code: 对端关闭码（中途断流形态携带）。

        Returns:
            待抛出的业务异常。
        """
        details: dict[str, Any] = {
            "reason": "流式语音识别失败",
            "service": "qwen3-asr-realtime",
            "endpoint": self._endpoint,
            "model": self._model,
            "mode": "streaming",
            "latency_ms": latency_ms,
        }
        if error is not None:
            details["error"] = error
        if parse_error is not None:
            details["parse_error"] = parse_error
        if server_error is not None:
            details["server_error"] = server_error
        if close_code is not None:
            details["close_code"] = close_code
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message=f"流式语音识别失败: {reason}",
            details=details,
        )


class AsrStreamHandle:
    """单条流式识别连接（喂帧 / 收尾 / 取消 + 保活与超时约束）。

    生命周期：``start`` 拉起发送工人 / 保活工人 / 事件泵；``finish`` 提交
    并等待最终稿；``cancel`` 取消并关闭。三条路径都以「传输关闭 + 派生
    任务全部回收」收尾（零残留）。

    Args:
        transport: 已握手的传输面。
        endpoint: 端点描述（错误 details 观测面）。
        model: 模型名（错误 details 观测面）。
        timeout_seconds: 收尾事件等待超时秒数。
        heartbeat_interval_seconds: 保活间隔（秒）。
        send_queue_max: 喂帧发送队列上限。
        on_partial: 进行中转写回调。
    """

    def __init__(
        self,
        transport: VoiceStreamTransportFace,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float,
        heartbeat_interval_seconds: float,
        send_queue_max: int,
        on_partial: Callable[[str], Awaitable[None]],
    ) -> None:
        self._transport = transport
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._on_partial = on_partial
        self._send_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=send_queue_max)
        self._final_future: asyncio.Future[AsrResultModel] = (
            asyncio.get_running_loop().create_future()
        )
        self._failure_future: asyncio.Future[BusinessException] = (
            asyncio.get_running_loop().create_future()
        )
        self._finish_sent = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._frames_dropped = 0
        self._started_ms = now_ms()

    @property
    def frames_dropped(self) -> int:
        """发送队列溢出丢弃的帧数。"""
        return self._frames_dropped

    async def start(self) -> None:
        """拉起发送 / 保活 / 事件泵三工人（内部登记——收尾时统一回收）。"""
        self._spawn(self._sender_loop())
        self._spawn(self._heartbeat_loop())
        self._spawn(self._pump_loop())

    async def feed(self, pcm16: bytes) -> None:
        """追加一帧 PCM16（队列满时丢最旧帧并计数）。

        Args:
            pcm16: 16 位小端单声道 PCM 字节。

        Raises:
            BusinessException: 连接已关闭 / 已失败（调用方回退批量）。
        """
        self._raise_if_failed()
        if self._closed:
            raise self._call_error(reason="流式识别连接已关闭，无法继续喂帧")
        if not pcm16:
            return
        if self._send_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
            self._frames_dropped += 1
        self._send_queue.put_nowait(pcm16)

    async def finish(self) -> AsrResultModel:
        """提交音频并等待最终稿（成功后关闭连接）。

        收尾事件由发送工人按队列顺序发出（先于哨兵入队的音频分片全部落地
        后才发 ``commit`` / ``session.finish``）——本方法只投哨兵再等待。

        三条出口（成功 / 业务失败 / 被外部取消）都经「关闭传输 + 回收派生
        任务」收口，且收口本身不再可被打断：取消落在收尾等待或传输关闭
        途中时，连接与派生任务仍会完整回收后再上抛取消。

        Returns:
            最终转写结果。

        Raises:
            BusinessException: 收尾超时 / 中途断流 / 服务端错误
                （``LLM_CALL_FAILED``）。
            asyncio.CancelledError: 收尾期间被外部取消（收口完成后原样上抛）。
        """
        self._raise_if_failed()
        if self._closed:
            raise self._call_error(reason="流式识别连接已关闭，无法收尾")
        self._enqueue_sentinel()
        try:
            await self._wait_finish_sent()
            result = await self._wait_settled()
        except BusinessException:
            await self._finalize_cancellation_safe(self.cancel())
            raise
        except asyncio.CancelledError:
            await self._finalize_cancellation_safe(self.cancel())
            raise
        await self._finalize_cancellation_safe(
            self._shutdown(STREAM_CLOSE_NORMAL, "session_finished", notify_cancel=False)
        )
        return result

    async def _finalize_cancellation_safe(self, finalize: Awaitable[None]) -> None:
        """驱动收尾协程至完成（对重复取消容忍，取消信号不外泄给收尾）。

        收尾（关闭传输 + 回收派生任务 + 置终态）一经开始就必须走完——被
        外部取消打断会把已打开的连接与派生任务留给事件循环。本方法把收尾
        挂到独立任务上并以 ``asyncio.shield`` 循环等待：等待期间到达的取消
        信号（含重复到达）只记录、继续等待；收尾完成后以 ``CancelledError``
        回应调用方，取消信号优先于收尾异常。

        Args:
            finalize: 收尾协程（``cancel`` 或 ``_shutdown`` 调用）。

        Raises:
            asyncio.CancelledError: 等待期间收到取消（收尾已完成）。
        """
        task = asyncio.ensure_future(finalize)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if not cancelled:
                    raise
        if cancelled:
            if not task.cancelled():
                try:
                    task.result()
                except Exception:
                    logger.opt(exception=True).debug(
                        "流式识别收尾异常让位于取消信号: endpoint={}", self._endpoint
                    )
            raise asyncio.CancelledError
        task.result()

    async def cancel(self) -> None:
        """取消并关闭连接（幂等；不产出最终稿）。"""
        if self._closed:
            return
        await self._shutdown(STREAM_CLOSE_NORMAL, "client_cancel", notify_cancel=True)

    async def _wait_finish_sent(self) -> None:
        """等待发送工人落完 ``commit`` / ``session.finish``（失败即上抛）。

        服务端可能在收尾事件前就下发最终稿（协议宽松）；此时仍须等收尾
        事件真实出站，避免 ``finish`` 提前关闭连接而丢弃收尾语义。

        Raises:
            BusinessException: 发送失败 / 收尾事件发送超时
                （``LLM_CALL_FAILED``）。
        """
        try:
            await asyncio.wait_for(self._finish_sent.wait(), timeout=self._timeout)
        except TimeoutError as exc:
            error = self._call_error(
                reason="流式识别收尾事件发送超时",
                close_code=self._transport.last_close_code,
            )
            self._fail(error)
            raise error from exc
        self._raise_if_failed()

    async def _wait_settled(self) -> AsrResultModel:
        """等待最终稿或失败（收尾等待超时硬约束）。

        Returns:
            最终转写结果。

        Raises:
            BusinessException: 失败 / 超时（``LLM_CALL_FAILED``）。
        """
        waiters: set[asyncio.Future[Any]] = {
            self._final_future,
            self._failure_future,
        }
        done, _ = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=self._timeout,
        )
        if self._failure_future in done:
            raise self._failure_future.result()
        if self._final_future in done:
            return self._final_future.result()
        self._fail(self._call_error(
            reason="流式识别收尾等待超时",
            close_code=self._transport.last_close_code,
        ))
        await self._finalize_cancellation_safe(
            self._shutdown(STREAM_CLOSE_GOING_AWAY, "settle_timeout", notify_cancel=False)
        )
        raise self._failure_future.result()

    def _enqueue_sentinel(self) -> None:
        """投递收尾哨兵（队列满时按既有口径丢最旧帧——哨兵不得被丢）。

        哨兵排在所有已入队音频分片之后，发送工人读到它才会发 ``commit`` /
        ``session.finish``，从而保证线上事件序与喂帧序一致（``finish`` 后
        不再出现迟到的 append 事件）。
        """
        if self._send_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
            self._frames_dropped += 1
        self._send_queue.put_nowait(None)

    async def _sender_loop(self) -> None:
        """逐帧发送喂入的 PCM16；哨兵处按固定序发 commit / session.finish。"""
        try:
            while not self._closed:
                pcm16 = await self._send_queue.get()
                if pcm16 is None:
                    await self._send_event({"type": ASR_STREAM_EVENT_COMMIT})
                    await self._send_event({"type": ASR_STREAM_EVENT_FINISH})
                    self._finish_sent.set()
                    continue
                audio = base64.b64encode(pcm16).decode("ascii")
                await self._transport.send(
                    build_text_frame(
                        json.dumps({"type": ASR_STREAM_EVENT_APPEND, "audio": audio})
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(
                self._call_error(
                    reason="流式识别音频发送失败",
                    error=str(exc)[:_ERROR_TEXT_LIMIT],
                )
            )

    async def _heartbeat_loop(self) -> None:
        """按间隔发送协议层保活（失败不致命——由事件泵判定断流）。"""
        try:
            while not self._closed:
                await asyncio.sleep(self._heartbeat_interval)
                await self._transport.ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _pump_loop(self) -> None:
        """读取服务端事件：partial 回调 / 最终稿落定 / 失败标记。"""
        try:
            while not self._closed:
                frame = await self._transport.receive()
                if frame is None:
                    if self._final_future.done():
                        return
                    self._fail(
                        self._call_error(
                            reason="流式识别在收尾前被服务端关闭",
                            close_code=self._transport.last_close_code,
                        )
                    )
                    return
                if frame.kind != "text":
                    continue
                try:
                    event = AsrStreamServerEventModel.model_validate_json(frame.text or "")
                except ValidationError as exc:
                    self._fail(
                        self._call_error(
                            reason="流式识别事件 JSON 形态非法",
                            parse_error=str(exc)[:_ERROR_TEXT_LIMIT],
                        )
                    )
                    return
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(
                self._call_error(
                    reason="流式识别事件读取失败",
                    error=str(exc)[:_ERROR_TEXT_LIMIT],
                )
            )

    async def _handle_event(self, event: AsrStreamServerEventModel) -> None:
        """按事件类型分派（partial / final / finished / error）。

        Args:
            event: 服务端事件。
        """
        if event.type == _SERVER_EVENT_PARTIAL:
            text = (event.text or "").strip()
            if text:
                try:
                    await self._on_partial(text)
                except Exception:
                    logger.opt(exception=True).debug(
                        "流式识别进行中回调异常（忽略——最终稿仍可用）: endpoint={}",
                        self._endpoint,
                    )
            return
        if event.type == _SERVER_EVENT_FINAL:
            text = (event.transcript or event.text or "").strip()
            if not self._final_future.done():
                self._final_future.set_result(AsrResultModel(text=text))
            return
        if event.type == _SERVER_EVENT_FINISHED:
            if not self._final_future.done():
                self._final_future.set_result(AsrResultModel(text=""))
            return
        if event.type == _SERVER_EVENT_ERROR:
            self._fail(
                self._call_error(
                    reason="流式识别服务端返回错误事件",
                    server_error=Qwen3AsrStreamClient._server_error_text(event),
                )
            )

    async def _send_event(self, payload: dict[str, Any]) -> None:
        """发送一条 JSON 事件（失败即标记连接失败并上抛）。

        Args:
            payload: 事件体。

        Raises:
            BusinessException: 发送失败（``LLM_CALL_FAILED``）。
        """
        try:
            await self._transport.send(
                build_text_frame(json.dumps(payload, ensure_ascii=False))
            )
        except Exception as exc:
            error = self._call_error(
                reason="流式识别事件发送失败", error=str(exc)[:_ERROR_TEXT_LIMIT]
            )
            self._fail(error)
            raise error from exc

    async def _shutdown(self, code: int, reason: str, *, notify_cancel: bool) -> None:
        """收尾连接：可选取消事件 → 关闭传输 → 回收派生任务。

        Args:
            code: 关闭码（语义见 stream_models 模块 docstring）。
            reason: 关闭原因。
            notify_cancel: 是否先发 ``session.cancel``（取消路径）。
        """
        if notify_cancel:
            with contextlib.suppress(Exception):
                await self._transport.send(
                    build_text_frame(json.dumps({"type": ASR_STREAM_EVENT_CANCEL}))
                )
        self._closed = True
        with contextlib.suppress(Exception):
            await self._transport.close(code, reason)
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        """取消并回收全部派生任务（幂等）。"""
        stale = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in stale:
            if not task.done():
                task.cancel()
        if stale:
            await asyncio.gather(*stale, return_exceptions=True)
        self._tasks.clear()

    def _fail(self, error: BusinessException) -> None:
        """标记连接失败（首个失败生效——后续事件忽略）。

        Args:
            error: 失败异常。
        """
        if not self._failure_future.done():
            self._failure_future.set_result(error)
        self._finish_sent.set()

    def _raise_if_failed(self) -> None:
        """已失败时上抛首个失败（调用方据此回退批量）。

        Raises:
            BusinessException: 已记录的失败。
        """
        if self._failure_future.done():
            raise self._failure_future.result()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """登记并启动派生任务（收尾统一取消）。

        Args:
            coro: 协程体。
        """
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)

    def _call_error(
        self,
        *,
        reason: str,
        error: str | None = None,
        parse_error: str | None = None,
        server_error: str | None = None,
        close_code: int | None = None,
    ) -> BusinessException:
        """构造 ``LLM_CALL_FAILED`` 业务异常（连接期统一 details）。

        Args:
            reason: 失败类别描述。
            error: 传输异常描述（发送 / 读取失败形态携带）。
            parse_error: JSON / 形态解析错误（契约破坏形态携带）。
            server_error: 服务端错误文本（error 事件携带）。
            close_code: 对端关闭码（断流形态携带）。

        Returns:
            待抛出的业务异常。
        """
        details: dict[str, Any] = {
            "reason": "流式语音识别失败",
            "service": "qwen3-asr-realtime",
            "endpoint": self._endpoint,
            "model": self._model,
            "mode": "streaming",
            "latency_ms": now_ms() - self._started_ms,
        }
        if error is not None:
            details["error"] = error
        if parse_error is not None:
            details["parse_error"] = parse_error
        if server_error is not None:
            details["server_error"] = server_error
        if close_code is not None:
            details["close_code"] = close_code
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message=f"流式语音识别失败: {reason}",
            details=details,
        )
