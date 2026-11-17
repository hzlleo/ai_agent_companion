"""voice 域 Model Classes——TTS / ASR 请求结果契约 + 人格语音绑定 + 用量事件
（Unit 文件）。

契约分层：

    - :class:`TtsRequestModel`  —— TTS 合成请求（客户端本地校验面：文本
      上限 / speaker 可选 / 语速区间 / instruct2 指令可选）；
    - :class:`VoiceTtsRequestDTO` —— voice API 合成请求（管理面入参：
      text + persona_id 可选 + speaker / speed 显式覆盖）；
    - :class:`TtsResultModel`   —— TTS 合成结果（内存字节形态 + 媒体类型
      + 端到端时延）；
    - :class:`AsrResultModel`   —— ASR 转写结果（文本 / 时长毫秒 / 语言，
      均为 OpenAI 兼容 audio 端点返回载荷的归一形态）；
    - :class:`VoiceProfileModel` —— 人格语音绑定（存储于 ``ff_personas.
      voice_profile`` JSON 列——persona 域经本模型序列化 / 反序列化）；
    - :class:`VoiceUsageEvent`  —— voice 用量事件（service → usage 域的
      注入式记录器载荷；token 恒 0——语音调用无 token 语义）；
    - :class:`TtsClientFace` / :class:`AsrClientFace` —— 客户端注入面协议
      （真实 :class:`~packages.harness.voice.tts_client.CosyVoiceTtsClient` /
      :class:`~packages.harness.voice.asr_client.Qwen3AsrClient` / 测试 fake
      均满足——与 tools 域 SandboxClientFace 同约定）。

大输出边界（文档化决策）：TTS 结果为**内存字节**（``audio: bytes``）——
单次合成上限受请求文本 500 字符约束，音频体量在 MB 量级内；若后续
需要长文本 / 流式音频，在服务层演进为流式传输面（本模型保持归一结果
形态不变）。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

__all__ = [
    "TTS_TEXT_MAX_CHARS",
    "TtsRequestModel",
    "VoiceTtsRequestDTO",
    "TtsResultModel",
    "AsrResultModel",
    "VoiceProfileModel",
    "VoiceUsageEvent",
    "TtsClientFace",
    "AsrClientFace",
]

#: TTS 单次合成文本上限（字符）。
TTS_TEXT_MAX_CHARS: int = 500


class TtsRequestModel(BaseModel):
    """TTS 合成请求（客户端本地校验面）。

    Attributes:
        text: 待合成文本（1-500 字符；上限为单次音频体量与合成时延的
            工程约束——超长文本由调用方分段）。
        speaker: 发音人（None = 服务端缺省发音人——CosyVoice 预置
            speaker 名或部署侧配置的默认值）。
        speed: 语速（0.5-2.0，默认 1.0——1.0 为自然语速）。
        instruct_text: instruct2 模式风格指令（None = 普通合成；提供时
            CosyVoice instruct2 模式按自然语言指令控制语气 / 情绪——
            仅经人格语音绑定携带，请求 API 不直接暴露）。
    """

    text: str = Field(min_length=1, max_length=TTS_TEXT_MAX_CHARS, description="待合成文本")
    speaker: str | None = Field(default=None, description="发音人（None = 服务端缺省）")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="语速（0.5-2.0）")
    instruct_text: str | None = Field(
        default=None, description="instruct2 模式风格指令（None = 普通合成）"
    )


class VoiceTtsRequestDTO(BaseModel):
    """voice API 合成请求（POST /api/v1/voice/tts 入参）。

    Attributes:
        text: 待合成文本（1-500 字符）。
        persona_id: 人格主键（None = 当前激活人格；无激活人格时以未绑定
            形态合成——settings 缺省发音人，用量 persona_id 为空）。
        speaker: 发音人显式覆盖（None = 人格绑定 / settings 缺省）。
        speed: 语速显式覆盖（None = 人格绑定 / settings 缺省）。
    """

    text: str = Field(min_length=1, max_length=TTS_TEXT_MAX_CHARS, description="待合成文本")
    persona_id: int | None = Field(default=None, description="人格主键（None = 激活人格）")
    speaker: str | None = Field(
        default=None, min_length=1, max_length=128, description="发音人显式覆盖"
    )
    speed: float | None = Field(
        default=None, ge=0.5, le=2.0, description="语速显式覆盖（0.5-2.0）"
    )


class TtsResultModel(BaseModel):
    """TTS 合成结果（内存字节形态）。

    Attributes:
        audio: 音频字节（WAV 容器——与部署侧 CosyVoice 包装服务契约一致）。
        content_type: 媒体类型（恒 ``audio/wav``——客户端对响应
            Content-Type 校验通过后归一）。
        latency_ms: 客户端侧合成往返时延（epoch 毫秒差；含失败路径耗时
            由服务层另行计量）。
    """

    audio: bytes = Field(description="音频字节（WAV 容器）")
    content_type: str = Field(default="audio/wav", description="媒体类型")
    latency_ms: int = Field(default=0, ge=0, description="合成往返时延（毫秒）")


class AsrResultModel(BaseModel):
    """ASR 转写结果（OpenAI 兼容 audio 端点返回载荷归一）。

    Attributes:
        text: 转写文本（端点必返字段）。
        duration_ms: 音频时长毫秒（端点可选回传；未回传为 None）。
        language: 识别语言（端点可选回传；未回传为 None）。
    """

    text: str = Field(description="转写文本")
    duration_ms: int | None = Field(default=None, ge=0, description="音频时长（毫秒）")
    language: str | None = Field(default=None, description="识别语言")


class VoiceProfileModel(BaseModel):
    """人格语音绑定（存储于 ``ff_personas.voice_profile`` JSON 列）。

    绑定语义（锁定决策）：**人格级配置覆盖 settings 缺省**——人格绑定
    存在时，其 speaker / speed 覆盖 ``Settings.voice.tts`` 的
    default_speaker / default_speed；请求级显式参数又覆盖人格绑定。
    优先级：请求参数 > 人格绑定 > settings 缺省。

    Attributes:
        speaker: 发音人（CosyVoice speaker id——预置名或部署侧克隆音色名）。
        instruct_text: instruct2 模式风格指令（None = 普通合成；≤2000
            字符；提供时随合成请求透传给 CosyVoice instruct2 模式）。
        speed: 语速（0.5-2.0，默认 1.0）。
    """

    speaker: str = Field(min_length=1, max_length=128, description="发音人（speaker id）")
    instruct_text: str | None = Field(
        default=None,
        max_length=2000,
        description="instruct2 风格指令（None = 普通合成；≤2000 字符）",
    )
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="语速（0.5-2.0）")


class VoiceUsageEvent(BaseModel):
    """voice 用量事件（service → usage 域注入式记录器的载荷）。

    token 语义（锁定决策）：语音调用**无 token 语义**——input / output /
    total 恒 0 落账（调用计数 + 真实时延仍有观测价值；与 llm 网关「端点
    未回传用量时零用量落账」同形态）。

    Attributes:
        ts_ms: 调用发起时间（epoch 毫秒）。
        persona_id: 人格 ID（None = 系统调用——无人格上下文）。
        purpose: 调用用途（恒 ``tts`` / ``asr``——usage 域枚举既有成员）。
        model_entry: 模型条目名（settings.voice.tts/asr.model_entry）。
        provider: provider 类型（``cosyvoice`` / ``qwen3-asr``）。
        latency_ms: 端到端时延毫秒（含失败耗时）。
        status: 调用结果（success / error）。
        request_id: 请求追踪 ID（可空）。
        error_code: 失败错误码（成功为 None）。
    """

    ts_ms: int = Field(description="调用发起时间（epoch 毫秒）")
    persona_id: str | None = Field(default=None, description="人格 ID（系统调用为 None）")
    purpose: str = Field(description="调用用途（tts / asr）")
    model_entry: str = Field(min_length=1, description="模型条目名")
    provider: str = Field(default="cosyvoice", description="provider 类型")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")
    status: str = Field(description="调用结果（success / error）")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
    error_code: str | None = Field(default=None, description="失败错误码（成功为 None）")


class TtsClientFace(Protocol):
    """TTS 客户端注入面协议（真实 CosyVoiceTtsClient / 测试 fake 均满足）。"""

    async def synthesize(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> TtsResultModel:
        """合成语音（文本 → WAV 字节）。

        Args:
            text: 待合成文本（1-500 字符）。
            speaker: 发音人（None = 服务端缺省）。
            speed: 语速（0.5-2.0）。
            instruct_text: instruct2 风格指令（None = 普通合成）。

        Returns:
            合成结果（audio / content_type / latency_ms）。
        """
        ...


class AsrClientFace(Protocol):
    """ASR 客户端注入面协议（真实 Qwen3AsrClient / 测试 fake 均满足）。"""

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        *,
        language: str | None = None,
    ) -> AsrResultModel:
        """转写音频（字节 → 文本）。

        Args:
            audio: 音频字节（WAV / MP3 等常见容器）。
            filename: 原始文件名（multipart file 字段携带——端点按扩展名
                嗅探容器格式）。
            language: 语言提示（None = 端点自动检测）。

        Returns:
            转写结果（text / duration_ms / language）。
        """
        ...
