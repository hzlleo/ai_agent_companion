"""multimodal 表情包库 Model Classes——配置 / 上传 / 标注 / 通道桥契约（Unit 文件）。

契约分层：

    - :class:`StickerSettings` —— ``STICKER_`` env 面（默认关闭）；
    - :class:`StickerUploadItemModel` / :class:`StickerBatchUploadDTO` /
      :class:`StickerUpdateModel` / :class:`StickerQueryDTO` /
      :class:`StickerBatchDeleteRequest` —— 管理面入参；
    - :class:`StickerRecordDTO` / :class:`StickerBatchUploadResultModel` /
      :class:`StickerBatchDeleteResultModel` / :class:`StickerAnnotationResultModel`
      —— 读取与操作结果（含 VLM 标注状态 / 错误 / 命中条目）；
    - :class:`StickerBinaryModel` —— 服务内部二进制形态（发送 / 标注取字节）；
    - :class:`StickerAnnotateGatewayFace` —— 视觉标注网关注入面协议。

素材法律边界（锁定设计）：**上传必填 ``source_note``**——记录素材来源与授权
状态（如「自绘」「朋友授权使用」「来自公开表情站，CC0」）。库只存储与展示，
不推断版权；来源备注随记录读取，管理面必须可见。VLM 标注只生成描述与标签，
不改变来源备注。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.llm.llm_models import ChatMessageModel, ChatResponseModel

__all__ = [
    "STICKER_IMAGE_MIME_ALLOWLIST",
    "StickerSettings",
    "StickerAnnotationStatus",
    "StickerRejectReason",
    "StickerUploadItemModel",
    "StickerBatchUploadDTO",
    "StickerUpdateModel",
    "StickerQueryDTO",
    "StickerBatchDeleteRequest",
    "StickerRecordDTO",
    "StickerRejectedItemModel",
    "StickerBatchUploadResultModel",
    "StickerBatchDeleteResultModel",
    "StickerAnnotationResultModel",
    "StickerBinaryModel",
    "StickerAnnotateGatewayFace",
]

#: 允许入库的图片 MIME 白名单（与 look_at 图像能力面一致）。
STICKER_IMAGE_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: 标注状态机（pending → done / failed；failed 可重试回 pending）。
class StickerAnnotationStatus(StrEnum):
    """VLM 标注状态值域。"""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class StickerRejectReason(StrEnum):
    """上传拒收原因值域（批量部分成功——逐条目结局）。"""

    DUPLICATE = "duplicate"
    PAYLOAD_INVALID = "payload_invalid"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    SOURCE_NOTE_INVALID = "source_note_invalid"
    TAG_INVALID = "tag_invalid"
    DESCRIPTION_TOO_LONG = "description_too_long"


class StickerSettings(BaseSettings):
    """表情包库配置（env_prefix=``STICKER_``——multimodal 域）。

    锁定决策：``enabled=False``（默认）——表情包是可选能力面（文件落盘 +
    管理面 + 可选的通道收发桥），缺省零配置开发可用、测试 hermetic。

    Attributes:
        enabled: 功能总闸（False = 表情包 API 拒绝；通道桥不装配）。
        storage_dir: 表情包文件落盘根目录。
        max_upload_bytes: 单文件字节上限（超过拒收）。
        max_tags: 单条表情包标签数上限。
        tag_max_length: 单个标签最长字符数。
        description_max_length: 描述最长字符数。
        source_note_max_length: 来源 / 授权备注最长字符数（上传必填）。
        max_batch_items: 单次批量上传条数上限（资源面守卫）。
        channel_reply_enabled: 通道表情收发桥的回表情开关（仅当
            ``enabled`` 为真时生效；False = 只收录不发）。
    """

    enabled: bool = Field(default=False, description="是否启用表情包库")
    storage_dir: str = Field(
        default="data/stickers", min_length=1, description="表情包文件落盘根目录"
    )
    max_upload_bytes: int = Field(
        default=5242880, ge=1024, description="单文件字节上限（5MB 量级）"
    )
    max_tags: int = Field(default=8, ge=1, description="单条表情包标签数上限")
    tag_max_length: int = Field(default=24, ge=1, description="单个标签最长字符数")
    description_max_length: int = Field(default=500, ge=0, description="描述最长字符数")
    source_note_max_length: int = Field(
        default=512, ge=1, description="来源 / 授权备注最长字符数（上传必填）"
    )
    max_batch_items: int = Field(default=20, ge=1, description="单次批量上传条数上限")
    channel_reply_enabled: bool = Field(
        default=True, description="通道桥回表情开关（仅 STICKER_ENABLED=true 时生效）"
    )

    model_config = SettingsConfigDict(
        env_prefix="STICKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class StickerUploadItemModel(BaseModel):
    """单条表情包上传形态（JSON base64 载荷）。

    Attributes:
        image_base64: 图片字节 base64 编码（解码后按 MIME 白名单与大小上限
            校验）。
        content_type: 图片 MIME 类型（png / jpeg / webp / gif）。
        source_note: 素材来源与授权备注（必填——法律边界提示义务）。
        tags: 初始标签（可选；服务归一去重并套用上限）。
        description: 描述（可选；VLM 标注成功后可补充）。
    """

    image_base64: str = Field(min_length=1, description="图片字节 base64 载荷")
    content_type: str = Field(
        default="image/png", min_length=1, max_length=64, description="图片 MIME 类型"
    )
    source_note: str = Field(
        min_length=1, max_length=512, description="素材来源与授权备注（必填）"
    )
    tags: list[str] = Field(default_factory=list, description="初始标签")
    description: str = Field(default="", max_length=500, description="描述")


class StickerBatchUploadDTO(BaseModel):
    """批量上传请求。

    Attributes:
        items: 上传条目列表（条数由服务按 ``max_batch_items`` 校验）。
    """

    items: list[StickerUploadItemModel] = Field(
        min_length=1, max_length=20, description="批量上传条目"
    )


class StickerUpdateModel(BaseModel):
    """表情包修改请求（标签 / 描述 / 来源备注——至少一项）。

    Attributes:
        tags: 新标签集（None = 不修改；空列表 = 清空标签）。
        description: 新描述（None = 不修改）。
        source_note: 新来源备注（None = 不修改）。
    """

    tags: list[str] | None = Field(default=None, description="新标签集（None = 不修改）")
    description: str | None = Field(
        default=None, max_length=500, description="新描述（None = 不修改）"
    )
    source_note: str | None = Field(
        default=None, min_length=1, max_length=512, description="新来源备注（None = 不修改）"
    )

    @model_validator(mode="after")
    def _require_any_change(self) -> StickerUpdateModel:
        """要求至少提供一个修改字段。

        Returns:
            校验通过的自身。

        Raises:
            ValueError: 三个字段全为 None（空修改无意义）。
        """
        if self.tags is None and self.description is None and self.source_note is None:
            raise ValueError("至少提供 tags / description / source_note 之一")
        return self


class StickerQueryDTO(BaseModel):
    """表情包列表查询（标签过滤 / 标注状态过滤 / 数据内联）。

    Attributes:
        tag: 标签过滤（精确匹配归一后的标签）。
        annotation_status: 标注状态过滤。
        limit: 返回条数上限（1-200）。
        include_data: 是否内联文件 base64（管理面缩略图用）。
    """

    tag: str | None = Field(default=None, max_length=24, description="标签过滤")
    annotation_status: StickerAnnotationStatus | None = Field(
        default=None, description="标注状态过滤"
    )
    limit: int = Field(default=100, ge=1, le=200, description="返回条数上限")
    include_data: bool = Field(default=False, description="是否内联文件 base64")


class StickerBatchDeleteRequest(BaseModel):
    """批量删除请求。

    Attributes:
        ids: 目标主键列表（1-100 条）。
    """

    ids: list[int] = Field(min_length=1, max_length=100, description="表情包主键列表")


class StickerRecordDTO(BaseModel):
    """表情包读取形态（含 VLM 标注状态与来源备注）。

    Attributes:
        id: 主键。
        sha256: 文件哈希（去重键）。
        content_type: 图片 MIME 类型。
        size_bytes: 文件字节数。
        tags: 标签列表。
        description: 描述（VLM 标注可补充）。
        source_note: 素材来源与授权备注。
        annotation_status: VLM 标注状态。
        annotation_error: 标注失败原因（成功为 None）。
        annotation_model_entry: 标注命中的视觉模型条目名（未标注为 None）。
        annotated_at_ms: 标注完成时间（epoch 毫秒；未标注为 None）。
        created_at: 入库时间（epoch 毫秒）。
        updated_at: 更新时间（epoch 毫秒）。
        data_base64: 文件 base64（仅 ``include_data`` 查询填充）。
    """

    id: int
    sha256: str
    content_type: str
    size_bytes: int = Field(ge=0)
    tags: list[str]
    description: str
    source_note: str
    annotation_status: StickerAnnotationStatus
    annotation_error: str | None = None
    annotation_model_entry: str | None = None
    annotated_at_ms: int | None = None
    created_at: int
    updated_at: int
    data_base64: str | None = None


class StickerRejectedItemModel(BaseModel):
    """批量上传中被拒收的条目。

    Attributes:
        index: 请求内条目序（0 起）。
        reason: 拒收原因。
        message: 中文可读说明。
    """

    index: int = Field(ge=0, description="请求内条目序（0 起）")
    reason: StickerRejectReason = Field(description="拒收原因")
    message: str = Field(description="中文可读说明")


class StickerBatchUploadResultModel(BaseModel):
    """批量上传结果（部分成功语义）。

    Attributes:
        created: 成功入库的表情包记录。
        rejected: 被拒收条目。
    """

    created: list[StickerRecordDTO]
    rejected: list[StickerRejectedItemModel]


class StickerBatchDeleteResultModel(BaseModel):
    """批量删除结果（部分成功语义）。

    Attributes:
        deleted_ids: 实际删除的主键。
        missing_ids: 不存在的主键（幂等跳过）。
    """

    deleted_ids: list[int]
    missing_ids: list[int]


class StickerAnnotationResultModel(BaseModel):
    """VLM 标注结果（失败落状态且可重试——不抛异常给管理面）。

    Attributes:
        ok: 本次标注是否成功。
        record: 标注后的记录（失败时状态为 failed 且携带错误）。
        message: 中文可读说明。
    """

    ok: bool
    record: StickerRecordDTO
    message: str


class StickerBinaryModel(BaseModel):
    """表情包二进制形态（标注 / 通道发送取字节——不进入 wire JSON）。

    Attributes:
        record: 表情包记录。
        content: 文件字节。
    """

    record: StickerRecordDTO
    content: bytes


class StickerAnnotateGatewayFace(Protocol):
    """视觉标注网关注入面协议（真实 LlmGateway / 测试 fake 均满足）。"""

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """按显式条目调用非流式聊天（视觉标注消费）。"""
        ...
