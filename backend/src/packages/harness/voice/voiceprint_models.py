"""voice 域声纹 Model Classes——配置面 / 请求结果契约 / PIPL 告知文本（Unit 文件）。

契约分层：

    - :class:`VoiceprintSettings` —— ``VOICEPRINT_`` env 面（默认关闭；
      未配置说话人向量端点时整功能关闭）；
    - :class:`VoiceprintSampleUploadModel` / :class:`VoiceprintEnrollRequestDTO`
      / :class:`VoiceprintVerifyRequestDTO` —— 注册（批量样本）与校验请求；
    - :class:`VoiceprintProfileDTO` / :class:`VoiceprintEnrollResultModel` /
      :class:`VoiceprintVerifyResultModel` / :class:`VoiceprintDeleteResultModel`
      —— 档案读取、注册逐样本结局、校验裁决与删除结果；
    - :class:`VoiceprintConsentGrantDTO` / :class:`VoiceprintConsentRecordDTO`
      —— 同意记录的授予入参与读取形态（版本 / 授予时间 / 撤回时间）；
    - :class:`VoiceprintPrivacyNoticeDTO` + :func:`render_privacy_notice`
      —— PIPL 告知义务的机器可读面（目的 / 范围 / 保存期 / 撤回权）；
    - :class:`VoiceprintEmbeddingFace` —— 说话人向量提供方注入面协议。

隐私边界（锁定设计）：声纹数据按**人格 × 发送者**隔离；样本音频落盘、
向量落库，撤回同意或主动删除时样本文件与向量立即清除，同意记录只保留
版本与授予 / 撤回时间作为处理合法性凭证。``embedding_base_url`` 为空视为
未配置提供方——整功能关闭而非部分可用（避免产生「同意已授予但无法比对」
的半配置态）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.usage.budget_limits import DAILY_BUDGET_MAX

__all__ = [
    "VOICEPRINT_AUDIO_BASE64_MAX_LENGTH",
    "VOICEPRINT_AUDIO_MIME_ALLOWLIST",
    "VOICEPRINT_CONSENT_TEXT_VERSION",
    "VOICEPRINT_MAX_UPLOAD_BYTES",
    "VOICEPRINT_PRIVACY_NOTICE_VERSION",
    "VoiceprintSettings",
    "VoiceprintSampleUploadModel",
    "VoiceprintEnrollRequestDTO",
    "VoiceprintVerifyRequestDTO",
    "VoiceprintConsentGrantDTO",
    "VoiceprintConsentRecordDTO",
    "VoiceprintSampleRecordDTO",
    "VoiceprintProfileDTO",
    "VoiceprintAudioProbeModel",
    "VoiceprintAudioProbeSource",
    "VoiceprintRejectedSampleModel",
    "VoiceprintEnrollResultModel",
    "VoiceprintVerifyResultModel",
    "VoiceprintDeleteResultModel",
    "VoiceprintWithdrawResultModel",
    "VoiceprintPrivacyNoticeDTO",
    "VoiceprintRejectReason",
    "VoiceprintVerifyReason",
    "VoiceprintEmbeddingFace",
    "render_privacy_notice",
]

#: 同意文本版本（隐私告知与同意记录共用——撤回后历史行保留该版本号）。
VOICEPRINT_CONSENT_TEXT_VERSION: str = "v1"

#: 隐私告知版本（与同意文本版本同步演进；管理 API 回传供前端展示）。
VOICEPRINT_PRIVACY_NOTICE_VERSION: str = "v1"

#: 单样本音频字节上限的代码默认（服务层字节帽与 DTO 长度帽的单一来源）。
VOICEPRINT_MAX_UPLOAD_BYTES: int = 10485760

#: base64 载荷字符串上限（按字节帽的 4/3 展开精确推导——pydantic 层
#: 提前拒绝超大请求体，服务层仍保留解码后的精确字节校验）。
VOICEPRINT_AUDIO_BASE64_MAX_LENGTH: int = 4 * ((VOICEPRINT_MAX_UPLOAD_BYTES + 2) // 3)

#: 可接受的音频 MIME 白名单（WAV / MP3 / OGG / WebM / MP4 容器）。
VOICEPRINT_AUDIO_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/webm",
        "audio/mp4",
        "audio/x-m4a",
    }
)

#: 隐私告知模板（保存期与撤回权按配置动态渲染；``str.format`` 占位）。
_PRIVACY_NOTICE_TEMPLATE: str = (
    "声纹识别用于在多人对话与语音消息中区分说话人身份，只在以下边界内处理你的语音数据："
    "目的——把语音片段归属到发送者，用于称呼、关系记忆与说话人确认；"
    "范围——仅在你主动授予同意后采集你提交的语音样本，提取说话人向量，并保存样本时长、"
    "采样率与文件哈希等必要元数据；通话全文不因声纹功能额外留存，向量不用于第三方模型训练；"
    "保存期——样本与向量最多保留 {retention_days} 天，到期自动清除；"
    "撤回权——你可随时撤回同意或删除声纹档案，撤回 / 删除后样本音频与向量立即清除，"
    "仅保留同意文本版本、授予时间与撤回时间作为合规凭证；"
    "隔离——数据按人格 × 发送者隔离，不同人格之间互不可见。"
)


def render_privacy_notice(retention_days: int) -> str:
    """渲染 PIPL 告知文本（目的 / 范围 / 保存期 / 撤回权 / 隔离边界）。

    Args:
        retention_days: 样本最长保存天数（仅用于告知文案；到期清理由
            服务层按同一配置执行）。

    Returns:
        完整中文告知文本。
    """
    return _PRIVACY_NOTICE_TEMPLATE.format(retention_days=retention_days)


class VoiceprintSettings(BaseSettings):
    """声纹配置（env_prefix=``VOICEPRINT_``——voice 域）。

    锁定决策：``enabled=False``（默认）且 ``embedding_base_url`` 为空——
    声纹是可选能力面，缺省零配置开发可用、测试 hermetic；两个开关同时
    成立时功能才对外可用，其余形态一律按「未启用」拒绝（CONFLICT）。

    Attributes:
        enabled: 功能总闸（False = 声纹 API 拒绝）。
        embedding_base_url: 说话人向量服务基础 URL（空 = 未配置提供方，
            整功能关闭；HTTP 契约见 ``voiceprint_embedding`` 模块文档）。
        embedding_path: 向量端点路径（拼接到 base_url 之后）。
        embedding_model: 请求中携带的模型名（包装服务按名选择权重）。
        request_timeout_seconds: 单次向量提取请求超时秒数。
        similarity_threshold: 校验通过所需的余弦相似度阈值（0-1）。
        min_samples: 一次注册至少提交的样本数。
        max_samples: 单发送者样本档案上限（再次注册累计不得超过）。
        min_sample_duration_ms: 注册样本最短时长（短于此时长拒收）。
        min_verify_duration_ms: 校验音频最短时长（短音频拒识）。
        min_sample_rate_hz: 注册样本最低采样率（过低采样率拒收）。
        max_upload_bytes: 单样本音频字节上限。
        max_batch_samples: 单次注册请求的样本条数上限（资源面守卫）。
        retention_days: 样本 / 向量最长保存天数（到期由服务层清理）。
        storage_dir: 样本文件落盘根目录（相对路径按进程工作目录解析）。
        daily_budget_global: 全局每日声纹向量提取调用数上限（0 = 不限；
            有效上限 10,000——计数页帽 50 × 每页 200 行；按 usage 行
            计数，跨 worker 近似一致——软竞态）。
        daily_budget_persona: 单人格每日声纹向量提取调用数上限（0 =
            不限；有效上限 10,000；仅人格上下文调用受此档约束）。
    """

    enabled: bool = Field(default=False, description="是否启用声纹识别")
    embedding_base_url: str = Field(
        default="", description="说话人向量服务基础 URL（空 = 未配置提供方，整功能关闭）"
    )
    embedding_path: str = Field(
        default="/embedding", min_length=1, description="说话人向量端点路径"
    )
    embedding_model: str = Field(
        default="voiceprint-embedding", min_length=1, description="说话人向量模型名"
    )
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, description="说话人向量提取请求超时（秒）"
    )
    similarity_threshold: float = Field(
        default=0.75, ge=0.0, le=1.0, description="校验通过所需的余弦相似度阈值（0-1）"
    )
    min_samples: int = Field(default=3, ge=1, description="一次注册至少提交的样本数")
    max_samples: int = Field(default=20, ge=1, description="单发送者样本档案上限")
    min_sample_duration_ms: int = Field(
        default=1500, ge=1, description="注册样本最短时长（毫秒；短于此时长拒收）"
    )
    min_verify_duration_ms: int = Field(
        default=1000, ge=1, description="校验音频最短时长（毫秒；短音频拒识）"
    )
    min_sample_rate_hz: int = Field(
        default=8000, ge=1, description="注册样本最低采样率（赫兹）"
    )
    max_upload_bytes: int = Field(
        default=VOICEPRINT_MAX_UPLOAD_BYTES,
        ge=1024,
        description="单样本音频字节上限（10MB 量级）",
    )
    max_batch_samples: int = Field(
        default=20, ge=1, description="单次注册请求的样本条数上限（资源面守卫）"
    )
    retention_days: int = Field(
        default=365, ge=1, description="样本与向量最长保存天数（到期自动清除）"
    )
    storage_dir: str = Field(
        default="data/voiceprints", min_length=1, description="样本文件落盘根目录"
    )
    daily_budget_global: int = Field(
        default=0,
        ge=0,
        le=DAILY_BUDGET_MAX,
        description=(
            "全局每日声纹向量提取调用数上限（0 = 不限；有效上限 10,000——"
            "计数页帽 50×200；按 usage 行计数，跨 worker 近似一致）"
        ),
    )
    daily_budget_persona: int = Field(
        default=0,
        ge=0,
        le=DAILY_BUDGET_MAX,
        description=(
            "单人格每日声纹向量提取调用数上限（0 = 不限；有效上限 10,000——"
            "计数页帽 50×200；仅人格上下文调用受约束）"
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="VOICEPRINT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _validate_sample_bounds(self) -> VoiceprintSettings:
        """校验样本条数上限与注册下限、单批上限的一致性。

        Returns:
            校验通过的自身。

        Raises:
            ValueError: ``max_samples`` 小于 ``min_samples``、
                ``max_batch_samples`` 大于 ``max_samples``，或
                ``max_batch_samples`` 小于 ``min_samples``（配置自相矛盾
                ——注册下限必须能在单批内满足）。
        """
        if self.max_samples < self.min_samples:
            raise ValueError("VOICEPRINT_MAX_SAMPLES 不能小于 VOICEPRINT_MIN_SAMPLES")
        if self.max_batch_samples > self.max_samples:
            raise ValueError("VOICEPRINT_MAX_BATCH_SAMPLES 不能大于 VOICEPRINT_MAX_SAMPLES")
        if self.max_batch_samples < self.min_samples:
            raise ValueError("VOICEPRINT_MAX_BATCH_SAMPLES 不能小于 VOICEPRINT_MIN_SAMPLES")
        return self


class VoiceprintSampleUploadModel(BaseModel):
    """单条声纹样本上传形态（JSON base64 载荷——与 look_at 同约定）。

    Attributes:
        audio_base64: 音频字节的 base64 编码（DTO 层按代码默认字节帽的
            4/3 展开上限拒绝超大载荷；解码后服务层仍按生效
            ``max_upload_bytes`` 与 MIME 白名单精确校验）。
        content_type: 音频 MIME 类型（缺省 ``audio/wav``）。
        duration_ms: 音频时长毫秒（上传方声明；服务以实测为准——WAV 头
            或裸 PCM 字节长度，不一致时采用实测值）。
        sample_rate_hz: 采样率赫兹（上传方声明；WAV 以头内值为准，裸
            PCM 以声明值为换算依据）。
    """

    audio_base64: str = Field(
        min_length=1,
        max_length=VOICEPRINT_AUDIO_BASE64_MAX_LENGTH,
        description="音频字节 base64 载荷",
    )
    content_type: str = Field(
        default="audio/wav", min_length=1, max_length=64, description="音频 MIME 类型"
    )
    duration_ms: int = Field(ge=0, description="音频时长（毫秒）")
    sample_rate_hz: int = Field(ge=0, description="采样率（赫兹）")


class VoiceprintEnrollRequestDTO(BaseModel):
    """声纹注册请求（批量样本——一次注册至少 ``min_samples`` 条）。

    Attributes:
        samples: 样本列表（条数由服务按配置的注册下限 / 单批上限校验）。
    """

    samples: list[VoiceprintSampleUploadModel] = Field(
        min_length=1, max_length=50, description="声纹样本列表（批量注册）"
    )


class VoiceprintVerifyRequestDTO(BaseModel):
    """声纹校验请求（单条待识别音频）。

    Attributes:
        sample: 待识别音频（短音频由服务拒识）。
    """

    sample: VoiceprintSampleUploadModel = Field(description="待识别音频样本")


class VoiceprintConsentGrantDTO(BaseModel):
    """同意授予请求。

    Attributes:
        consent_text_version: 用户同意的告知文本版本（须与当前版本一致；
            缺省 = 当前版本）。
    """

    consent_text_version: str = Field(
        default=VOICEPRINT_CONSENT_TEXT_VERSION,
        min_length=1,
        max_length=64,
        description="同意的告知文本版本（须与当前版本一致）",
    )


class VoiceprintConsentRecordDTO(BaseModel):
    """同意记录读取形态（管理面 / 档案聚合）。

    Attributes:
        id: 记录主键。
        sender_id: 同意主体（发送者 ID）。
        consent_text_version: 同意的告知文本版本。
        granted_at_ms: 授予时间（epoch 毫秒）。
        revoked_at_ms: 撤回时间（epoch 毫秒；None = 同意有效）。
    """

    id: int
    sender_id: str
    consent_text_version: str
    granted_at_ms: int
    revoked_at_ms: int | None = None

    @property
    def active(self) -> bool:
        """同意是否仍然有效（撤回时间为空）。

        Returns:
            True 表示当前可处理声纹数据。
        """
        return self.revoked_at_ms is None


class VoiceprintAudioProbeSource(StrEnum):
    """音频元数据实测来源值域（拒绝上传方声明作为唯一依据）。"""

    WAV_HEADER = "wav_header"
    RAW_PCM = "raw_pcm"
    DECLARED = "declared"


class VoiceprintAudioProbeModel(BaseModel):
    """音频实测元数据（时长 / 采样率 / 声道——注册与校验的门槛依据）。

    来源语义：``wav_header`` 从 RIFF/fmt/data 头实测；``raw_pcm`` 按声明
    采样率与字节长度换算（单声道 16 位假设）；``declared`` 仅用于无法
    在零依赖下解析的容器（MP3 / OGG / WebM / MP4——沿用上传方声明）。

    Attributes:
        duration_ms: 实测时长毫秒。
        sample_rate_hz: 实测采样率赫兹。
        channels: 实测声道数（raw_pcm / declared 固定 1）。
        source: 元数据来源（见值域说明）。
    """

    duration_ms: int = Field(ge=0, description="实测时长（毫秒）")
    sample_rate_hz: int = Field(ge=0, description="实测采样率（赫兹）")
    channels: int = Field(ge=1, description="实测声道数")
    source: VoiceprintAudioProbeSource = Field(description="元数据来源")


class VoiceprintSampleRecordDTO(BaseModel):
    """声纹样本读取形态（不含向量本身——向量是内部比对材料）。

    Attributes:
        id: 样本主键。
        sender_id: 发送者 ID。
        sample_sha256: 样本哈希（去重键）。
        duration_ms: 样本时长毫秒。
        sample_rate_hz: 采样率赫兹。
        embedding_dim: 说话人向量维度。
        created_at: 入库时间（epoch 毫秒）。
    """

    id: int
    sender_id: str
    sample_sha256: str
    duration_ms: int
    sample_rate_hz: int
    embedding_dim: int
    created_at: int


class VoiceprintProfileDTO(BaseModel):
    """声纹档案聚合（样本摘要 + 有效同意 + 告知版本）。

    Attributes:
        sender_id: 发送者 ID。
        sample_count: 样本条数。
        total_duration_ms: 样本总时长毫秒。
        samples: 样本摘要列表（按入库时间升序）。
        consent: 当前有效同意（None = 尚未授予或已撤回）。
        privacy_notice_version: 当前告知文本版本。
        privacy_notice: 完整告知文本（目的 / 范围 / 保存期 / 撤回权）。
    """

    sender_id: str
    sample_count: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    samples: list[VoiceprintSampleRecordDTO]
    consent: VoiceprintConsentRecordDTO | None = None
    privacy_notice_version: str
    privacy_notice: str


class VoiceprintRejectReason(StrEnum):
    """注册样本拒收原因值域（部分成功语义——逐样本结局）。"""

    DUPLICATE = "duplicate"
    PAYLOAD_INVALID = "payload_invalid"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    AUDIO_TOO_SHORT = "audio_too_short"
    SAMPLE_RATE_TOO_LOW = "sample_rate_too_low"
    EMBEDDING_DIM_MISMATCH = "embedding_dim_mismatch"


class VoiceprintRejectedSampleModel(BaseModel):
    """注册中被拒收的单条样本。

    Attributes:
        index: 请求内样本序（0 起）。
        reason: 拒收原因（枚举值域）。
        message: 中文可读说明（面向管理面 / 调用方）。
    """

    index: int = Field(ge=0, description="请求内样本序（0 起）")
    reason: VoiceprintRejectReason = Field(description="拒收原因")
    message: str = Field(description="中文可读说明")


class VoiceprintEnrollResultModel(BaseModel):
    """注册结果（批量部分成功语义）。

    Attributes:
        profile: 注册后的档案聚合（含本次新增样本）。
        accepted: 本次接收的样本条数。
        rejected: 被拒收的逐样本结局。
    """

    profile: VoiceprintProfileDTO
    accepted: int = Field(ge=0, description="本次接收的样本条数")
    rejected: list[VoiceprintRejectedSampleModel]


class VoiceprintVerifyReason(StrEnum):
    """校验裁决原因值域。"""

    MATCHED = "matched"
    BELOW_THRESHOLD = "below_threshold"
    NO_SAMPLES = "no_samples"
    AUDIO_TOO_SHORT = "audio_too_short"


class VoiceprintVerifyResultModel(BaseModel):
    """校验裁决（短音频拒识 / 无样本 / 低于阈值均为 ``matched=False``）。

    Attributes:
        sender_id: 被比对的目标发送者 ID。
        matched: 是否通过阈值。
        similarity: 与档案聚合向量的余弦相似度（-1 到 1；无样本时为 0）。
        threshold: 本次生效的阈值（配置快照）。
        reason: 裁决原因（枚举值域）。
    """

    sender_id: str
    matched: bool
    similarity: float = Field(ge=-1.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    reason: VoiceprintVerifyReason


class VoiceprintDeleteResultModel(BaseModel):
    """档案删除 / 同意撤回后的清理结果。

    Attributes:
        sender_id: 发送者 ID。
        deleted_samples: 被清除的样本条数（文件同批删除）。
    """

    sender_id: str
    deleted_samples: int = Field(ge=0)


class VoiceprintWithdrawResultModel(BaseModel):
    """同意撤回结果（撤回行保留 + 样本与向量同批清除）。

    Attributes:
        consent: 被撤回的同意记录（``revoked_at_ms`` 已写入）。
        deleted_samples: 被清除的样本条数（文件同批删除）。
    """

    consent: VoiceprintConsentRecordDTO
    deleted_samples: int = Field(ge=0)


class VoiceprintPrivacyNoticeDTO(BaseModel):
    """PIPL 告知面（管理 API 回传——目的 / 范围 / 保存期 / 撤回权）。

    Attributes:
        notice_version: 告知文本版本。
        consent_text_version: 同意文本版本（授予时须回传同值）。
        retention_days: 样本与向量最长保存天数。
        text: 完整告知文本。
    """

    notice_version: str
    consent_text_version: str
    retention_days: int = Field(ge=1)
    text: str


class VoiceprintEmbeddingFace(Protocol):
    """说话人向量提供方注入面协议（真实 HTTP 客户端 / 测试 fake 均满足）。"""

    async def embed(
        self, audio: bytes, filename: str, content_type: str
    ) -> list[float]:
        """提取说话人向量。

        Args:
            audio: 音频字节。
            filename: 原始文件名（multipart file 字段携带）。
            content_type: 音频 MIME 类型。

        Returns:
            非空浮点向量（维度由提供方决定；服务层校验同档案维度一致）。

        Raises:
            BusinessException: 提供方不可用 / 响应契约破坏（``LLM_CALL_FAILED``）。
        """
        ...
