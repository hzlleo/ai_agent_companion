"""voice 域流式 TTS 客户端——CosyVoice 流式合成（文本 → PCM 分片流，传输注入）。

**部署契约（本客户端持有文档——部署侧据此包装）**：

    CosyVoice 流式合成以「逐分片下行」WebSocket 服务形态部署（薄包装层
    透传事件名即可）::

        ws {VOICE_TTS_STREAMING_BASE_URL}
        → {"type": "tts.start",
           "text": str, "speaker": str | null, "speed": float,
           "instruct_text": str,                 # 仅人格绑定携带时出现
           "sample_rate_hz": int}                # 期望输出 PCM16 采样率
        ← {"type": "tts.started"}                # 可选确认（实现可略）
        ← {"type": "tts.delta", "audio": base64(pcm16)}    # 音频分片（顺序即播放序）
        ← {"type": "tts.completed"}              # 本句合成完成
        ← {"type": "error", "error": {"code": str, "message": str}}
        → {"type": "tts.cancel"}                 # 中途取消（barge-in；best-effort）

    鉴权沿用语部分节（``VOICE_TTS_*`` 凭据），本客户端不重复持有密钥键；
    端点 / 模型 / 超时 / 输出采样率即最小配置面（见 :class:`TtsSettings`）。

**输出口径（锁定决策）**：分片为**裸 PCM16 小端单声道**字节流（顺序拼接
即完整句子音频），采样率取 ``VOICE_TTS_STREAMING_SAMPLE_RATE_HZ``；会话层
用既有 :func:`pcm16_to_wav` 封装为 WAV 后按既有 ``audio_chunk`` 分片下行
——wire 契约与前端解码路径零变更（客户端仍整句解码播放，半句音频永不下发）。

**错误码映射表**（复用既有码表，``core/errors.py`` 不动）::

    ============================  ========================  ==========
    失败类别                       错误码                    可调用方处置
    ============================  ========================  ==========
    文本 / 语速越界                 VALIDATION_ERROR          调用方 bug
    连接失败 / 打开超时             LLM_CALL_FAILED           回退批量主链
    分片等待超时 / 中途断流         LLM_CALL_FAILED           回退批量主链
    事件 JSON 非法 / 服务端 error   LLM_CALL_FAILED           回退批量主链
    ============================  ========================  ==========

    调用方（实时会话逐句合成）对 ``LLM_CALL_FAILED`` 执行一次告警 + 指标
    计数并回退既有整句批量合成——该句音频由批量路径补上，会话不断。

取消语义（barge-in 安全边界）：消费方关闭异步生成器（``aclose``）或取消
消费任务时，生成器 ``finally`` 发送 ``tts.cancel`` 并关闭连接——在途连接
零残留；已 yield 的分片不会被撤回（会话层逐句缓冲，未完成句不上行）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncGenerator
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.voice.stream_models import (
    STREAM_CLOSE_NORMAL,
    VoiceStreamConnector,
    VoiceStreamTransportFace,
    build_text_frame,
)
from packages.harness.voice.voice_models import TtsRequestModel

__all__ = [
    "TtsStreamClientFace",
    "CosyVoiceStreamClient",
    "TTS_STREAM_EVENT_START",
    "TTS_STREAM_EVENT_DELTA",
    "TTS_STREAM_EVENT_COMPLETED",
    "TTS_STREAM_EVENT_CANCEL",
]

#: 合成启动事件（客户端 → 服务端）。
TTS_STREAM_EVENT_START: Final[str] = "tts.start"
#: 音频分片事件（服务端 → 客户端）。
TTS_STREAM_EVENT_DELTA: Final[str] = "tts.delta"
#: 合成完成事件（服务端 → 客户端）。
TTS_STREAM_EVENT_COMPLETED: Final[str] = "tts.completed"
#: 取消事件（客户端 → 服务端，best-effort）。
TTS_STREAM_EVENT_CANCEL: Final[str] = "tts.cancel"

#: 服务端错误事件名。
_SERVER_EVENT_ERROR: Final[str] = "error"
#: 协议层保活间隔（秒——控制帧）。
_DEFAULT_HEARTBEAT_SECONDS: Final[float] = 5.0
#: 错误 details 携带的错误文本截断长度。
_ERROR_TEXT_LIMIT: Final[int] = 300


class TtsStreamClientFace(Protocol):
    """流式合成客户端注入面协议（真实客户端 / 测试 fake 均满足）。"""

    @property
    def sample_rate_hz(self) -> int:
        """输出 PCM16 采样率（Hz——会话层 WAV 封装依据）。

        Returns:
            采样率。
        """
        ...

    def synthesize_stream(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """流式合成一句文本（逐分片产出 PCM16 字节）。

        Args:
            text: 待合成文本（1-500 字符）。
            speaker: 发音人（None = 服务端缺省）。
            speed: 语速（0.5-2.0）。
            instruct_text: instruct2 风格指令（None = 不携带）。

        Returns:
            PCM16 分片异步迭代器。

        Raises:
            BusinessException: 参数越界 / 连接或分片失败。
        """
        ...


class TtsStreamServerErrorModel(BaseModel):
    """服务端错误事件的 error 子对象（宽容解析）。

    Attributes:
        code: 服务端错误码（可空）。
        message: 服务端错误描述（可空）。
    """

    code: str | None = Field(default=None, description="服务端错误码")
    message: str | None = Field(default=None, description="服务端错误描述")
    model_config = ConfigDict(extra="ignore")


class TtsStreamServerEventModel(BaseModel):
    """服务端流式合成事件（宽容解析——未知字段忽略）。

    Attributes:
        type: 事件类型（判别键）。
        audio: 音频分片（base64 PCM16——``tts.delta`` 事件）。
        error: 服务端错误详情（``error`` 事件）。
    """

    type: str = Field(min_length=1, description="事件类型")
    audio: str | None = Field(default=None, description="音频分片（base64 PCM16）")
    error: TtsStreamServerErrorModel | None = Field(default=None, description="错误详情")
    model_config = ConfigDict(extra="ignore")


class CosyVoiceStreamClient:
    """CosyVoice 流式合成客户端（传输注入；每句一条连接）。

    Args:
        connector: 传输连接工厂（生产 = :func:`make_websocket_connector`；
            测试 = 内存 fake 工厂）。
        endpoint: 端点描述（仅错误 details 观测面）。
        model: 握手 ``tts.start`` 的 model 提示（部署侧兼容字段）。
        timeout_seconds: 连接 / 分片等待超时秒数。
        sample_rate_hz: 期望输出 PCM16 采样率（Hz——下行 WAV 封装依据）。
        heartbeat_interval_seconds: 协议层保活间隔（秒）。
    """

    def __init__(
        self,
        connector: VoiceStreamConnector,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float,
        sample_rate_hz: int,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("流式合成超时必须为正")
        if sample_rate_hz <= 0:
            raise ValueError("流式合成采样率必须为正")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("流式合成保活间隔必须为正")
        self._connector = connector
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_seconds
        self._sample_rate_hz = sample_rate_hz
        self._heartbeat_interval = heartbeat_interval_seconds

    @property
    def sample_rate_hz(self) -> int:
        """输出 PCM16 采样率（Hz——会话层 WAV 封装依据）。"""
        return self._sample_rate_hz

    async def synthesize_stream(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """流式合成一句文本（逐分片产出 PCM16 字节）。

        Args:
            text: 待合成文本（1-500 字符——越界 ``VALIDATION_ERROR``）。
            speaker: 发音人（None = 服务端缺省）。
            speed: 语速（0.5-2.0）。
            instruct_text: instruct2 风格指令（None = 不携带）。

        Yields:
            PCM16 分片（顺序即播放序；单声道、``sample_rate_hz`` 采样率）。

        Raises:
            BusinessException: 参数越界（``VALIDATION_ERROR``）；连接 / 分片
                等待超时 / 中途断流 / 服务端错误（``LLM_CALL_FAILED``）。
        """
        request = self._validate_request(
            text, speaker=speaker, speed=speed, instruct_text=instruct_text
        )
        transport = await self._connect()
        heartbeat: asyncio.Task[None] | None = None
        completed = False
        try:
            heartbeat = asyncio.create_task(self._heartbeat_loop(transport))
            await self._send_start(transport, request)
            while True:
                frame = await asyncio.wait_for(transport.receive(), timeout=self._timeout)
                if frame is None:
                    raise self._call_error(
                        reason="流式合成在收尾事件前被服务端关闭",
                        close_code=transport.last_close_code,
                    )
                if frame.kind != "text":
                    continue
                event = self._parse_event(frame.text or "")
                if event.type == TTS_STREAM_EVENT_DELTA:
                    chunk = self._decode_audio(event)
                    if chunk:
                        yield chunk
                    continue
                if event.type == TTS_STREAM_EVENT_COMPLETED:
                    completed = True
                    return
                if event.type == _SERVER_EVENT_ERROR:
                    raise self._call_error(
                        reason="流式合成服务端返回错误事件",
                        server_error=self._server_error_text(event),
                    )
        except TimeoutError as exc:
            raise self._call_error(
                reason="流式合成分片等待超时",
                close_code=transport.last_close_code,
            ) from exc
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if not completed:
                with contextlib.suppress(Exception):
                    await transport.send(
                        build_text_frame(json.dumps({"type": TTS_STREAM_EVENT_CANCEL}))
                    )
            with contextlib.suppress(Exception):
                await transport.close(STREAM_CLOSE_NORMAL, "tts_stream_end")

    def _validate_request(
        self,
        text: str,
        *,
        speaker: str | None,
        speed: float,
        instruct_text: str | None,
    ) -> TtsRequestModel:
        """校验合成请求参数（复用批量请求模型边界）。

        Args:
            text: 待合成文本。
            speaker: 发音人。
            speed: 语速。
            instruct_text: 风格指令。

        Returns:
            校验通过的请求模型。

        Raises:
            BusinessException: 参数越界（``VALIDATION_ERROR``）。
        """
        try:
            return TtsRequestModel(
                text=text, speaker=speaker, speed=speed, instruct_text=instruct_text
            )
        except ValidationError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="流式语音合成请求参数非法（文本 1-500 字符 / 语速 0.5-2.0）",
                details={"reason": str(exc)[:200]},
            ) from exc

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
                reason="流式合成连接打开超时", latency_ms=now_ms() - started_ms
            ) from exc
        except OSError as exc:
            raise self._call_error(
                reason="流式合成连接失败",
                error=str(exc)[:_ERROR_TEXT_LIMIT],
                latency_ms=now_ms() - started_ms,
            ) from exc

    async def _send_start(
        self, transport: VoiceStreamTransportFace, request: TtsRequestModel
    ) -> None:
        """下发 ``tts.start``（请求体与批量端点字段同源，追加采样率）。

        Args:
            transport: 已连接传输面。
            request: 已校验的合成请求。

        Raises:
            BusinessException: 发送失败（``LLM_CALL_FAILED``）。
        """
        body: dict[str, Any] = {
            "type": TTS_STREAM_EVENT_START,
            "text": request.text,
            "speaker": request.speaker,
            "speed": request.speed,
            "sample_rate_hz": self._sample_rate_hz,
            "model": self._model,
        }
        if request.instruct_text is not None:
            body["instruct_text"] = request.instruct_text
        try:
            await transport.send(build_text_frame(json.dumps(body, ensure_ascii=False)))
        except Exception as exc:
            raise self._call_error(
                reason="流式合成启动事件发送失败", error=str(exc)[:_ERROR_TEXT_LIMIT]
            ) from exc

    def _parse_event(self, payload: str) -> TtsStreamServerEventModel:
        """解析服务端事件（形态非法即失败）。

        Args:
            payload: 文本帧载荷。

        Returns:
            事件模型。

        Raises:
            BusinessException: JSON 非法 / 缺 type（``LLM_CALL_FAILED``）。
        """
        try:
            return TtsStreamServerEventModel.model_validate_json(payload)
        except ValidationError as exc:
            raise self._call_error(
                reason="流式合成事件 JSON 形态非法",
                parse_error=str(exc)[:_ERROR_TEXT_LIMIT],
            ) from exc

    def _decode_audio(self, event: TtsStreamServerEventModel) -> bytes:
        """解码分片音频（base64 → PCM16 字节；非法分片即失败）。

        Args:
            event: ``tts.delta`` 事件。

        Returns:
            解码后的 PCM16 字节（空分片返回空字节——调用方跳过）。

        Raises:
            BusinessException: 分片缺 audio / base64 非法（``LLM_CALL_FAILED``）。
        """
        if not event.audio:
            return b""
        try:
            return base64.b64decode(event.audio, validate=True)
        except (ValueError, TypeError) as exc:
            raise self._call_error(
                reason="流式合成音频分片不是合法 base64",
                parse_error=str(exc)[:_ERROR_TEXT_LIMIT],
            ) from exc

    @staticmethod
    def _server_error_text(event: TtsStreamServerEventModel) -> str:
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

    async def _heartbeat_loop(self, transport: VoiceStreamTransportFace) -> None:
        """按间隔发送协议层保活（失败不致命——由分片等待判定断流）。

        Args:
            transport: 当前连接。
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                await transport.ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _call_error(
        self,
        *,
        reason: str,
        latency_ms: int = 0,
        error: str | None = None,
        parse_error: str | None = None,
        server_error: str | None = None,
        close_code: int | None = None,
    ) -> BusinessException:
        """构造 ``LLM_CALL_FAILED`` 业务异常（流式合成段统一 details）。

        Args:
            reason: 失败类别描述。
            latency_ms: 失败路径耗时（观测面）。
            error: 传输异常描述（连接 / 发送失败形态携带）。
            parse_error: JSON / base64 解析错误（契约破坏形态携带）。
            server_error: 服务端错误文本（error 事件携带）。
            close_code: 对端关闭码（断流形态携带）。

        Returns:
            待抛出的业务异常。
        """
        details: dict[str, Any] = {
            "reason": "流式语音合成失败",
            "service": "cosyvoice3-realtime",
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
            message=f"流式语音合成失败: {reason}",
            details=details,
        )
