"""realtime voice 域 Model Classes（Unit 文件）——WS 会话协议 / 配置 / 指标。

协议分层（上下行消息各为判别联合，wire 字段 ``type`` 为判别键）：

    上行（客户端 → 服务端）：
        start        建立会话（采样率 / 声道 / 可选人格）
        audio_chunk  麦克风 PCM16 base64 分块（序号 + 时间戳）
        end          会话优雅结束
        barge_in     客户端手动打断（播放中「打断」按钮）
        barge_in_ack 客户端已停止本地播放的确认
        ping         心跳（服务端以 state 消息应答）

    下行（服务端 → 客户端）：
        ready / partial_transcript / final_transcript / assistant_text /
        audio_chunk / state / interrupted / error / closed

设计约束：

    - **音频只做 PCM16 字节处理**：上行载荷为 base64 编码的 16 位小端
      单声道 PCM；下行 TTS 音频为 WAV 容器字节（CosyVoice 合成端点契约），
      按固定字节分片走 ``audio_chunk`` 消息——不引入音频编解码依赖；
    - **时间统一 epoch 毫秒**（``ts_ms``），心跳 / 空闲 / 最大时长判定
      全部以毫秒计；
    - **单连接单会话**：``start`` 只接受一次，重复即 ``VALIDATION_ERROR``；
    - 全部下行消息携带 ``type`` 与 ``ts_ms``，客户端按 ``type`` 分派。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "REALTIME_PROTOCOL_VERSION",
    "REALTIME_AUDIO_CHUNK_MAX_BYTES",
    "REALTIME_AUDIO_CHUNK_MAX_BASE64_CHARS",
    "AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE",
    "SentenceBufferModel",
    "RealtimeSessionPhase",
    "RealtimeCloseReason",
    "RealtimeBargeInReason",
    "RealtimeStartMessageModel",
    "RealtimeAudioInputChunkMessageModel",
    "RealtimeAudioOutputChunkMessageModel",
    "RealtimeEndMessageModel",
    "RealtimeBargeInMessageModel",
    "RealtimeBargeInAckMessageModel",
    "RealtimePingMessageModel",
    "RealtimeInboundMessageModel",
    "REALTIME_INBOUND_ADAPTER",
    "RealtimeVadPublicSettingsModel",
    "RealtimeSessionLimitsModel",
    "RealtimeReadyMessageModel",
    "RealtimePartialTranscriptMessageModel",
    "RealtimeFinalTranscriptMessageModel",
    "RealtimeAssistantTextMessageModel",
    "RealtimeAudioInputChunkMessageModel",
    "RealtimeAudioOutputChunkMessageModel",
    "RealtimeStateMessageModel",
    "RealtimeInterruptedMessageModel",
    "RealtimeErrorMessageModel",
    "RealtimeClosedMessageModel",
    "RealtimeOutboundMessageModel",
    "REALTIME_OUTBOUND_ADAPTER",
    "RealtimeVoiceSettings",
    "RealtimeSessionMetricsModel",
    "RealtimeTransportFace",
    "RealtimeChatFace",
    "decode_audio_base64",
]


#: 协议版本（客户端与服务端在同一代码基线内演进；本字段进入 ready 消息的
#: 上下文字段表，供部署侧代理 / 抓包工具识别）。
REALTIME_PROTOCOL_VERSION: str = "1"

#: 上行音频块 base64 解码后的最小字节数（16 位单声道一帧 = 2 字节；
#: 小于该值说明客户端分块异常）。
_MIN_AUDIO_BYTES: int = 2

#: 上行音频单块解码后的字节上限（64 KiB PCM16——16kHz 单声道约 2 秒音频）。
#: 单块超过该上限视为客户端分块参数异常：解析期拒绝该块（可恢复错误，
#: 会话保留），并在会话指标 ``audio_chunks_rejected`` 上计数。选 64 KiB 是
#: 在「单块上传延迟」与「协议层内存上界」之间取整：默认 100ms 帧仅 3.2 KiB，
#: 即使客户端按秒批量上传（32 KiB）也留有一倍余量。
REALTIME_AUDIO_CHUNK_MAX_BYTES: int = 64 * 1024
#: 上述字节上限对应的 base64 字符长度上限（``4 * ceil(n / 3)``）——字段级
#: 前置拒绝，避免对明显超大的字符串做完整解码。该长度只是必要条件：长度
#: 达标但解码后越界（例如 87384 字符无填充时解码为 65538 字节）仍由字段
#: 校验器精确拒绝。
REALTIME_AUDIO_CHUNK_MAX_BASE64_CHARS: int = 4 * ((REALTIME_AUDIO_CHUNK_MAX_BYTES + 2) // 3)
#: 音频块超限的校验错误类型标识（字段校验器抛出；上行分派据此把超限与
#: 其它协议错误区分开——独立指标计数与针对性提示文案）。
AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE: str = "audio_chunk_too_large"


class RealtimeSessionPhase(StrEnum):
    """会话状态机相位（下行 state 消息与内部迁移表同源）。"""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CLOSED = "closed"


class RealtimeCloseReason(StrEnum):
    """会话关闭原因（closed 消息 / 指标日志共用值域）。"""

    CLIENT_END = "client_end"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"
    SESSION_REJECTED = "session_rejected"
    INTERNAL_ERROR = "internal_error"
    TRANSPORT_CLOSED = "transport_closed"


class RealtimeBargeInReason(StrEnum):
    """打断来源（VAD 检测到新语音 / 客户端手动按钮）。"""

    VAD = "vad"
    MANUAL = "manual"


class RealtimeStartMessageModel(BaseModel):
    """上行 start——建立实时语音会话。

    Attributes:
        type: 判别键（恒 ``start``）。
        ts_ms: 客户端发起时间（epoch 毫秒）。
        persona_id: 绑定的会话人格主键（None = 服务端当前激活人格）。
        sample_rate_hz: 客户端采集采样率（None = 服务端缺省配置；提供时
            必须与服务端配置一致——不一致即拒绝，避免转写音高错乱）。
        channels: 客户端采集声道数（None = 服务端缺省配置；提供时同上）。
    """

    type: Literal["start"] = "start"
    ts_ms: int = Field(ge=0, description="客户端发起时间（epoch 毫秒）")
    persona_id: int | None = Field(default=None, gt=0, description="绑定人格主键")
    sample_rate_hz: int | None = Field(default=None, ge=8000, le=48000, description="采集采样率")
    channels: int | None = Field(default=None, ge=1, le=2, description="采集声道数")


class RealtimeAudioInputChunkMessageModel(BaseModel):
    """上行 audio_chunk——麦克风 PCM16 分块（base64）。

    Attributes:
        type: 判别键（恒 ``audio_chunk``）。
        ts_ms: 客户端采集时间（epoch 毫秒）。
        seq: 客户端分块序号（单调递增；服务端只用于诊断日志与丢弃计数，
            不承担重排职责——WS 单连接内有序）。
        data: base64 编码的 16 位小端 PCM 字节（解码后 2 字节至 64 KiB；
            超过单块上限的块在解析期拒绝，会话保留并计入
            ``audio_chunks_rejected``）。
    """

    type: Literal["audio_chunk"] = "audio_chunk"
    ts_ms: int = Field(ge=0, description="客户端采集时间（epoch 毫秒）")
    seq: int = Field(ge=0, description="客户端分块序号")
    data: str = Field(
        min_length=1,
        max_length=REALTIME_AUDIO_CHUNK_MAX_BASE64_CHARS,
        description="base64 编码的 PCM16 字节（解码后不超过 64 KiB）",
    )

    @field_validator("data")
    @classmethod
    def _validate_audio_payload(cls, value: str) -> str:
        """校验 base64 形态与解码后字节长度（解析期拒绝非法 / 超限载荷）。

        Args:
            value: 客户端载荷字符串。

        Returns:
            原样返回的 base64 字符串（解码结果不在此处缓存——服务端入队时
            解码一次，避免模型内隐藏状态）。

        Raises:
            ValueError: 非 base64 / 解码后不足一帧（2 字节）。
            PydanticCustomError: 解码后超过单块上限（64 KiB）。
        """
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"audio_chunk.data 不是合法 base64: {exc}") from exc
        if len(decoded) < _MIN_AUDIO_BYTES:
            raise ValueError("audio_chunk.data 解码后不足一帧（至少 2 字节 PCM16）")
        if len(decoded) > REALTIME_AUDIO_CHUNK_MAX_BYTES:
            raise PydanticCustomError(
                AUDIO_CHUNK_TOO_LARGE_ERROR_TYPE,
                "audio_chunk.data 解码后 {actual} 字节，超过单块上限 {limit} 字节",
                {"actual": len(decoded), "limit": REALTIME_AUDIO_CHUNK_MAX_BYTES},
            )
        return value


class RealtimeEndMessageModel(BaseModel):
    """上行 end——客户端结束会话（服务端完成收尾后回 closed）。

    Attributes:
        type: 判别键（恒 ``end``）。
        ts_ms: 客户端结束时间（epoch 毫秒）。
    """

    type: Literal["end"] = "end"
    ts_ms: int = Field(ge=0, description="客户端结束时间（epoch 毫秒）")


class RealtimeBargeInMessageModel(BaseModel):
    """上行 barge_in——客户端手动打断（播放面板「打断」按钮）。

    Attributes:
        type: 判别键（恒 ``barge_in``）。
        ts_ms: 客户端打断时间（epoch 毫秒）。
        reason: 打断来源（客户端按钮恒 ``manual``；保留 ``vad`` 值域供
            客户端本地 VAD 预判时使用）。
    """

    type: Literal["barge_in"] = "barge_in"
    ts_ms: int = Field(ge=0, description="客户端打断时间（epoch 毫秒）")
    reason: Literal["manual", "vad"] = Field(default="manual", description="打断来源")


class RealtimeBargeInAckMessageModel(BaseModel):
    """上行 barge_in_ack——客户端已清空本地播放队列的确认。

    Attributes:
        type: 判别键（恒 ``barge_in_ack``）。
        ts_ms: 客户端确认时间（epoch 毫秒）。
        interruption_id: 对应下行 interrupted 消息的打断标识（原样回传，
            供服务端对齐「已停止播放」的时序）。
    """

    type: Literal["barge_in_ack"] = "barge_in_ack"
    ts_ms: int = Field(ge=0, description="客户端确认时间（epoch 毫秒）")
    interruption_id: str = Field(min_length=1, max_length=64, description="打断标识")


class RealtimePingMessageModel(BaseModel):
    """上行 ping——心跳（服务端以 state 消息应答并刷新空闲计时）。

    Attributes:
        type: 判别键（恒 ``ping``）。
        ts_ms: 客户端心跳时间（epoch 毫秒）。
    """

    type: Literal["ping"] = "ping"
    ts_ms: int = Field(ge=0, description="客户端心跳时间（epoch 毫秒）")


#: 上行消息判别联合（``type`` 为判别键；解析入口见
#: :data:`REALTIME_INBOUND_ADAPTER`）。
type RealtimeInboundMessageModel = Annotated[
    RealtimeStartMessageModel
    | RealtimeAudioInputChunkMessageModel
    | RealtimeEndMessageModel
    | RealtimeBargeInMessageModel
    | RealtimeBargeInAckMessageModel
    | RealtimePingMessageModel,
    Field(discriminator="type"),
]

#: 上行 wire 解析器（未知 type / 字段越界 / 非法 base64 均抛
#: :class:`pydantic.ValidationError`——调用方归一为 error 消息）。
REALTIME_INBOUND_ADAPTER: TypeAdapter[RealtimeInboundMessageModel] = TypeAdapter(
    RealtimeInboundMessageModel
)


class RealtimeVadPublicSettingsModel(BaseModel):
    """ready 消息内公开的 VAD 参数（客户端可据此本地预判，不强制）。

    Attributes:
        speech_rms_threshold: 起说能量阈值（归一化 0-1）。
        silence_rms_threshold: 断句能量阈值（滞回下沿，0-1）。
        min_speech_ms: 最短有效语音（低于即丢弃）。
        silence_end_ms: 静音结束窗口（持续静音达到即切段）。
        max_segment_ms: 单段语音上限（达到即强制切段）。
    """

    speech_rms_threshold: float = Field(ge=0.0, le=1.0, description="起说能量阈值")
    silence_rms_threshold: float = Field(ge=0.0, le=1.0, description="断句能量阈值")
    min_speech_ms: int = Field(ge=0, description="最短有效语音毫秒")
    silence_end_ms: int = Field(ge=0, description="静音结束窗口毫秒")
    max_segment_ms: int = Field(ge=0, description="单段语音上限毫秒")


class RealtimeSessionLimitsModel(BaseModel):
    """ready 消息内公开的会话资源上限（客户端据此显示与自我节流）。

    Attributes:
        max_session_ms: 单会话最长时长。
        idle_timeout_ms: 空闲超时（无任何上行消息即关闭）。
        heartbeat_interval_ms: 客户端心跳建议间隔。
        audio_queue_max_chunks: 服务端音频队列上限（溢出丢最旧块）。
        sentence_max_chars: 助手文本单句上限（句切参数）。
    """

    max_session_ms: int = Field(ge=0, description="单会话最长毫秒")
    idle_timeout_ms: int = Field(ge=0, description="空闲超时毫秒")
    heartbeat_interval_ms: int = Field(ge=0, description="心跳建议间隔毫秒")
    audio_queue_max_chunks: int = Field(ge=1, description="音频队列上限（块）")
    sentence_max_chars: int = Field(ge=1, description="助手单句字符上限")


class RealtimeReadyMessageModel(BaseModel):
    """下行 ready——会话建立成功（服务端能力与上限公告）。

    Attributes:
        type: 判别键（恒 ``ready``）。
        ts_ms: 服务端就绪时间（epoch 毫秒）。
        protocol_version: 协议版本。
        session_id: 服务端会话标识（日志 / 指标关联）。
        sample_rate_hz: 上行 PCM 采样率（客户端须按此采集）。
        channels: 上行声道数。
        audio_format: 下行音频格式（恒 ``wav``——TTS 端点契约）。
        asr_enabled: ASR 能力是否可用（不可用时整会话拒绝）。
        tts_enabled: TTS 能力是否可用（False = 仅文本字幕回复）。
        barge_in_enabled: 服务端是否启用打断。
        vad: 公开的 VAD 参数。
        limits: 公开的会话资源上限。
    """

    type: Literal["ready"] = "ready"
    ts_ms: int = Field(ge=0, description="服务端就绪时间（epoch 毫秒）")
    protocol_version: str = Field(min_length=1, description="协议版本")
    session_id: str = Field(min_length=1, max_length=64, description="会话标识")
    sample_rate_hz: int = Field(ge=8000, le=48000, description="上行采样率")
    channels: int = Field(ge=1, le=2, description="上行声道数")
    audio_format: Literal["wav"] = Field(default="wav", description="下行音频格式")
    asr_enabled: bool = Field(description="ASR 可用性")
    tts_enabled: bool = Field(description="TTS 可用性")
    barge_in_enabled: bool = Field(description="打断功能开关")
    vad: RealtimeVadPublicSettingsModel = Field(description="VAD 参数")
    limits: RealtimeSessionLimitsModel = Field(description="会话资源上限")


class RealtimePartialTranscriptMessageModel(BaseModel):
    """下行 partial_transcript——进行中语段的节流转写（非最终）。

    Attributes:
        type: 判别键（恒 ``partial_transcript``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        segment_id: 语段标识（同一语段的 partial 与 final 共用——客户端
            据此替换字幕行）。
        text: 转写文本（非空；空结果不下行）。
    """

    type: Literal["partial_transcript"] = "partial_transcript"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    segment_id: str = Field(min_length=1, max_length=64, description="语段标识")
    text: str = Field(min_length=1, description="转写文本")


class RealtimeFinalTranscriptMessageModel(BaseModel):
    """下行 final_transcript——切段完成后的最终转写。

    Attributes:
        type: 判别键（恒 ``final_transcript``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        segment_id: 语段标识。
        text: 转写文本（非空；空结果不下行——服务端直接回到 listening）。
        duration_ms: 语段有效语音时长（毫秒）。
        language: 识别语言（端点未回传为 None）。
    """

    type: Literal["final_transcript"] = "final_transcript"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    segment_id: str = Field(min_length=1, max_length=64, description="语段标识")
    text: str = Field(min_length=1, description="转写文本")
    duration_ms: int = Field(ge=0, description="语段有效语音时长（毫秒）")
    language: str | None = Field(default=None, description="识别语言")


class RealtimeAssistantTextMessageModel(BaseModel):
    """下行 assistant_text——人格回复文本（增量或整轮收尾）。

    Attributes:
        type: 判别键（恒 ``assistant_text``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        turn_id: 轮次标识（同轮音频分片与文本共用）。
        text: 文本增量（``final=false``）或整轮全文（``final=true``）。
        final: 是否整轮收尾（True = 本轮生成结束，后续只有音频分片）。
    """

    type: Literal["assistant_text"] = "assistant_text"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    turn_id: str = Field(min_length=1, max_length=64, description="轮次标识")
    text: str = Field(min_length=1, description="文本增量或整轮全文")
    final: bool = Field(default=False, description="是否整轮收尾")


class RealtimeAudioOutputChunkMessageModel(BaseModel):
    """下行 audio_chunk——句级 TTS 音频分片（base64 WAV 字节流）。

    Attributes:
        type: 判别键（恒 ``audio_chunk``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        turn_id: 轮次标识。
        sentence_index: 句序（同轮内从 0 递增——客户端按句拼接后解码播放）。
        seq: 分片序号（同句内从 0 递增）。
        total_chunks: 该句分片总数（客户端据此判断句音频收齐）。
        content_type: 音频内容类型（恒 ``audio/wav``）。
        data: base64 编码的 WAV 字节分片。
    """

    type: Literal["audio_chunk"] = "audio_chunk"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    turn_id: str = Field(min_length=1, max_length=64, description="轮次标识")
    sentence_index: int = Field(ge=0, description="句序")
    seq: int = Field(ge=0, description="分片序号")
    total_chunks: int = Field(ge=1, description="该句分片总数")
    content_type: Literal["audio/wav"] = Field(default="audio/wav", description="音频格式")
    data: str = Field(min_length=1, description="base64 编码的 WAV 分片")


class RealtimeStateMessageModel(BaseModel):
    """下行 state——会话状态迁移（同时作为心跳应答）。

    Attributes:
        type: 判别键（恒 ``state``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        state: 当前相位。
        detail: 迁移说明（可为空——仅诊断用，客户端不据此分支）。
    """

    type: Literal["state"] = "state"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    state: RealtimeSessionPhase = Field(description="当前相位")
    detail: str | None = Field(default=None, description="迁移说明（诊断用）")


class RealtimeInterruptedMessageModel(BaseModel):
    """下行 interrupted——打断已生效（在途生成与 TTS 已取消）。

    Attributes:
        type: 判别键（恒 ``interrupted``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        interruption_id: 打断标识（客户端在 barge_in_ack 原样回传）。
        turn_id: 被打断的轮次（无在途轮次时为 None）。
        reason: 打断来源。
    """

    type: Literal["interrupted"] = "interrupted"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    interruption_id: str = Field(min_length=1, max_length=64, description="打断标识")
    turn_id: str | None = Field(default=None, max_length=64, description="被打断轮次")
    reason: RealtimeBargeInReason = Field(description="打断来源")


class RealtimeErrorMessageModel(BaseModel):
    """下行 error——可恢复 / 不可恢复错误（不可恢复随后跟 closed）。

    Attributes:
        type: 判别键（恒 ``error``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        code: 错误码（复用 core 错误码值域字符串：VALIDATION_ERROR /
            CONFLICT / LLM_CALL_FAILED / INTERNAL_ERROR）。
        message: 面向用户的中文提示。
        recoverable: 会话是否可继续（True = 当前轮次失败但会话保留）。
    """

    type: Literal["error"] = "error"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    code: str = Field(min_length=1, description="错误码")
    message: str = Field(min_length=1, description="面向用户的中文提示")
    recoverable: bool = Field(description="会话是否可继续")


class RealtimeClosedMessageModel(BaseModel):
    """下行 closed——会话关闭（服务端随后关闭传输）。

    Attributes:
        type: 判别键（恒 ``closed``）。
        ts_ms: 服务端产生时间（epoch 毫秒）。
        reason: 关闭原因。
        detail: 附加说明（可为空——仅诊断用）。
    """

    type: Literal["closed"] = "closed"
    ts_ms: int = Field(ge=0, description="服务端产生时间（epoch 毫秒）")
    reason: RealtimeCloseReason = Field(description="关闭原因")
    detail: str | None = Field(default=None, description="附加说明（诊断用）")


#: 下行消息判别联合（``type`` 为判别键）。
type RealtimeOutboundMessageModel = Annotated[
    RealtimeReadyMessageModel
    | RealtimePartialTranscriptMessageModel
    | RealtimeFinalTranscriptMessageModel
    | RealtimeAssistantTextMessageModel
    | RealtimeAudioOutputChunkMessageModel
    | RealtimeStateMessageModel
    | RealtimeInterruptedMessageModel
    | RealtimeErrorMessageModel
    | RealtimeClosedMessageModel,
    Field(discriminator="type"),
]

#: 下行 wire 序列化器（服务端统一经 ``model_dump_json`` 输出；本适配器
#: 供测试与抓包工具反向校验下行载荷形态）。
REALTIME_OUTBOUND_ADAPTER: TypeAdapter[RealtimeOutboundMessageModel] = TypeAdapter(
    RealtimeOutboundMessageModel
)


class RealtimeVoiceSettings(BaseSettings):
    """实时语音会话配置（env_prefix=``REALTIME_VOICE_``——默认关闭）。

    锁定决策：``enabled=False``（默认）——实时语音是可选能力面：WS 端点
    在关闭态以 error + closed 拒绝连接（不占用音频资源），缺省零配置
    开发可用、测试 hermetic。TTS 音色 / 模型沿用既有 ``VOICE_TTS_*``
    配置（人格语音绑定生效），本分节不重复发音人键。

    不变式（加载期拒绝半生效配置）：
        - 断句阈值必须低于起说阈值（双阈值滞回下沿）；
        - 批处理块数不得超过队列上限（批量出队不得越过背压边界）；
        - 空闲超时必须短于会话最大时长（否则空闲规则永不先触发）；
        - 单段上限必须大于最短语音（否则每个语段都被丢弃）；
        - 中途转写起点不得晚于单段上限。

    Attributes:
        enabled: 实时语音总闸（False = WS 端点拒绝连接）。
        sample_rate_hz: 上行 PCM 采样率（客户端须按 ready 公告采集）。
        channels: 上行声道数（1 = 单声道；VAD 按单声道能量计算）。
        vad_speech_rms_threshold: 起说能量阈值（归一化 0-1；帧 RMS 达到
            即计为语音帧）。
        vad_silence_rms_threshold: 断句能量阈值（滞回下沿；持续低于该值
            满静音窗即切段）。
        vad_start_consecutive_frames: 起说防抖帧数（连续达到起说阈值
            才进入语音态——单帧尖峰不触发）。
        vad_min_speech_ms: 最短有效语音时长（低于即丢弃，不送 ASR）。
        vad_silence_end_ms: 静音结束窗口（持续静音达到即切段）。
        vad_max_segment_ms: 单段语音上限（达到即强制切段送 ASR）。
        partial_transcript_interval_ms: 中途转写节流间隔（0 = 关闭中途
            转写；真实 ASR 端点无流式通道，中途结果由节流的中途批量
            转写产生）。
        partial_min_speech_ms: 中途转写的最短已积累语音（低于不送）。
        sentence_max_chars: 助手文本单句上限（超过在标点 / 空格处硬切）。
        tts_chunk_bytes: 下行 TTS 音频分片字节数（WAV 字节流分片）。
        tts_queue_max: 逐句 TTS 队列上限（溢出策略：丢最旧句并计数）。
        audio_queue_max_chunks: 音频入队上限（溢出策略：丢最旧块并计数）。
        audio_batch_max_chunks: 单次批量出队块数上限。
        max_session_ms: 单会话最长时长（到时关闭）。
        idle_timeout_ms: 空闲超时（无任何上行消息即关闭）。
        heartbeat_interval_ms: 心跳建议间隔（ready 公告；服务端按空闲超时
            判定，不强制客户端按该间隔发送）。
        barge_in_enabled: 打断开关（False = 用户新语音不打断播放）。
        max_sessions: 进程内并发会话上限（超限以 1013 拒绝——进程级，
            多 worker 部署时每进程各自计数）。
    """

    enabled: bool = Field(default=False, description="实时语音总闸（False = WS 端点拒绝连接）")
    sample_rate_hz: int = Field(
        default=16000, ge=8000, le=48000, description="上行 PCM 采样率（Hz）"
    )
    channels: int = Field(default=1, ge=1, le=2, description="上行声道数")
    vad_speech_rms_threshold: float = Field(
        default=0.06, gt=0.0, le=1.0, description="起说能量阈值（归一化 0-1）"
    )
    vad_silence_rms_threshold: float = Field(
        default=0.03, ge=0.0, le=1.0, description="断句能量阈值（滞回下沿 0-1）"
    )
    vad_start_consecutive_frames: int = Field(
        default=3, ge=1, le=100, description="起说防抖帧数（连续达标帧）"
    )
    vad_min_speech_ms: int = Field(
        default=300, ge=50, le=60000, description="最短有效语音（毫秒）"
    )
    vad_silence_end_ms: int = Field(
        default=700, ge=100, le=60000, description="静音结束窗口（毫秒）"
    )
    vad_max_segment_ms: int = Field(
        default=15000, ge=500, le=600000, description="单段语音上限（毫秒）"
    )
    partial_transcript_interval_ms: int = Field(
        default=800, ge=0, le=60000, description="中途转写节流间隔（毫秒；0 = 关闭）"
    )
    partial_min_speech_ms: int = Field(
        default=600, ge=100, le=600000, description="中途转写最短已积累语音（毫秒）"
    )
    sentence_max_chars: int = Field(
        default=80, ge=10, le=500, description="助手单句字符上限"
    )
    tts_chunk_bytes: int = Field(
        default=16384, ge=1024, le=1048576, description="下行 TTS 音频分片字节数"
    )
    tts_queue_max: int = Field(
        default=32, ge=1, le=4096, description="逐句 TTS 队列上限（句；溢出丢最旧）"
    )
    audio_queue_max_chunks: int = Field(
        default=64, ge=1, le=4096, description="音频入队上限（块；溢出丢最旧）"
    )
    audio_batch_max_chunks: int = Field(
        default=8, ge=1, le=256, description="单次批量出队块数上限"
    )
    max_session_ms: int = Field(
        default=600000, ge=1000, le=86400000, description="单会话最长时长（毫秒）"
    )
    idle_timeout_ms: int = Field(
        default=60000, ge=1000, le=86400000, description="空闲超时（毫秒）"
    )
    heartbeat_interval_ms: int = Field(
        default=15000, ge=1000, le=600000, description="心跳建议间隔（毫秒）"
    )
    barge_in_enabled: bool = Field(default=True, description="打断开关")
    max_sessions: int = Field(
        default=2, ge=1, description="进程内并发会话上限（超限 1013 拒绝）"
    )

    model_config = SettingsConfigDict(
        env_prefix="REALTIME_VOICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _validate_invariants(self) -> RealtimeVoiceSettings:
        """校验跨字段不变式（半生效配置在加载期拒绝）。

        Returns:
            校验通过的自身实例。

        Raises:
            ValueError: 断句阈值不低于起说阈值 / 批处理越过队列上限 /
                空闲超时不短于最大时长 / 单段上限不大于最短语音 /
                中途转写起点晚于单段上限。
        """
        if self.vad_silence_rms_threshold >= self.vad_speech_rms_threshold:
            raise ValueError(
                "REALTIME_VOICE_VAD_SILENCE_RMS_THRESHOLD 必须低于 "
                "REALTIME_VOICE_VAD_SPEECH_RMS_THRESHOLD（双阈值滞回）"
            )
        if self.audio_batch_max_chunks > self.audio_queue_max_chunks:
            raise ValueError(
                "REALTIME_VOICE_AUDIO_BATCH_MAX_CHUNKS 不得超过 "
                "REALTIME_VOICE_AUDIO_QUEUE_MAX_CHUNKS"
            )
        if self.idle_timeout_ms >= self.max_session_ms:
            raise ValueError(
                "REALTIME_VOICE_IDLE_TIMEOUT_MS 必须短于 REALTIME_VOICE_MAX_SESSION_MS"
            )
        if self.vad_max_segment_ms <= self.vad_min_speech_ms:
            raise ValueError(
                "REALTIME_VOICE_VAD_MAX_SEGMENT_MS 必须大于 REALTIME_VOICE_VAD_MIN_SPEECH_MS"
            )
        if self.partial_min_speech_ms > self.vad_max_segment_ms:
            raise ValueError(
                "REALTIME_VOICE_PARTIAL_MIN_SPEECH_MS 不得晚于单段上限"
            )
        return self


class SentenceBufferModel(BaseModel):
    """句切累积面的可观测状态（诊断 / 测试断言用）。

    Attributes:
        pending_chars: 尚未成句的缓冲字符数。
        emitted_sentences: 已产出句数。
    """

    pending_chars: int = Field(ge=0, description="缓冲字符数")
    emitted_sentences: int = Field(ge=0, description="已产出句数")


class RealtimeSessionMetricsModel(BaseModel):
    """会话指标快照（关闭时落日志；测试断言面）。

    Attributes:
        started_ms: 会话开始时间（epoch 毫秒）。
        audio_chunks_received: 收到的音频块数。
        audio_chunks_dropped: 背压丢弃的音频块数。
        audio_chunks_rejected: 超过单块上限被解析期拒绝的音频块数。
        speech_starts: VAD 检测到的起说次数。
        segments_ready: 切段送 ASR 的语段数。
        segments_discarded: 短语音丢弃的语段数。
        segments_forced: 达到单段上限强制切段的语段数。
        partial_transcripts: 下行中途转写条数。
        final_transcripts: 完成最终转写的语段数。
        asr_failures: ASR 调用失败次数。
        generations: 发起的回复生成轮次。
        generation_failures: 回复生成失败次数。
        tts_sentences: 送 TTS 的句子数。
        tts_chunks_dropped: TTS 队列溢出丢弃的最旧句数。
        tts_failures: TTS 调用失败次数。
        audio_chunks_sent: 下行音频分片数。
        interruptions: 打断次数。
        barge_in_acks: 客户端播放清空确认次数。
        closed_reason: 关闭原因（未关闭为 None）。
    """

    started_ms: int = Field(ge=0, description="会话开始时间（epoch 毫秒）")
    audio_chunks_received: int = Field(default=0, ge=0, description="收到音频块数")
    audio_chunks_dropped: int = Field(default=0, ge=0, description="背压丢弃音频块数")
    audio_chunks_rejected: int = Field(default=0, ge=0, description="超限拒绝音频块数")
    speech_starts: int = Field(default=0, ge=0, description="起说次数")
    segments_ready: int = Field(default=0, ge=0, description="送 ASR 语段数")
    segments_discarded: int = Field(default=0, ge=0, description="丢弃短语音数")
    segments_forced: int = Field(default=0, ge=0, description="强制切段数")
    partial_transcripts: int = Field(default=0, ge=0, description="中途转写条数")
    final_transcripts: int = Field(default=0, ge=0, description="最终转写条数")
    asr_failures: int = Field(default=0, ge=0, description="ASR 失败次数")
    generations: int = Field(default=0, ge=0, description="回复生成轮次")
    generation_failures: int = Field(default=0, ge=0, description="生成失败次数")
    tts_sentences: int = Field(default=0, ge=0, description="送 TTS 句子数")
    tts_chunks_dropped: int = Field(default=0, ge=0, description="TTS 队列溢出丢弃句数")
    tts_failures: int = Field(default=0, ge=0, description="TTS 失败次数")
    audio_chunks_sent: int = Field(default=0, ge=0, description="下行音频分片数")
    interruptions: int = Field(default=0, ge=0, description="打断次数")
    barge_in_acks: int = Field(default=0, ge=0, description="播放清空确认数")
    closed_reason: RealtimeCloseReason | None = Field(default=None, description="关闭原因")


class RealtimeTransportFace(Protocol):
    """会话传输面协议（生产 = Starlette WebSocket 适配；测试 = 内存双工）。

    传输实现只承担「文本帧收发 + 关闭」，不含任何协议语义——JSON 编解码
    与状态推进全部在会话引擎内（可脱离网络确定性测试）。
    """

    async def send_text(self, payload: str) -> None:
        """发送一条文本帧（下行 JSON）。

        Args:
            payload: 已序列化的下行 JSON 字符串。
        """
        ...

    async def receive_text(self) -> str | None:
        """接收下一条文本帧（None = 对端已关闭）。

        Returns:
            上行 JSON 字符串；连接终止返回 None。
        """
        ...

    async def close(self, code: int, reason: str) -> None:
        """关闭连接（幂等——已关闭实现应安全返回）。

        Args:
            code: WS 关闭码（正常收尾 1000）。
            reason: 关闭原因文本。
        """
        ...


class RealtimeChatFace(Protocol):
    """回复生成面协议（生产 = ChatService 流式适配；测试 = 脚本 fake）。"""

    def stream_turn(self, user_text: str) -> AsyncIterator[str]:
        """发起一轮回复生成（文本增量迭代）。

        Args:
            user_text: 用户转写文本。

        Yields:
            回复文本增量。
        """
        ...


def decode_audio_base64(payload: str) -> bytes:
    """解码上行音频载荷（模型校验已通过，此处只做一次实际解码）。

    Args:
        payload: base64 字符串。

    Returns:
        解码后的 PCM16 字节。

    Raises:
        ValueError: 载荷非合法 base64（模型校验与解码之间的守卫）。
    """
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"音频载荷不是合法 base64: {exc}") from exc
