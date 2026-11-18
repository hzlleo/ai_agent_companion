"""multimodal 表情包库 ORM 行——``ff_stickers``。

设计决策（**PersonaMixin 人格作用域 + 文件哈希去重**）：

    - 表情包按人格隔离（PersonaMixin 自动注入 / 过滤）——每个人格维护自己
      的素材库，互不可见；
    - 唯一键 ``(persona_id, sha256)`` 让同一文件在同一人格内只存一份；重复
      上传按幂等跳过（批量上传的逐条目结局报告 duplicate）；
    - 文件字节落盘（``storage_path``），DB 只存元数据与标注状态；删除记录
      时服务层同批删除文件（尽力而为——残留文件不参与查询）；
    - VLM 标注状态机为 ``pending`` / ``done`` / ``failed``：上传即 pending，
      标注成功写 done + 描述 / 标签增量，失败写 failed + 错误文本（可重试
      再次标注）；
    - ``source_note`` 是素材法律边界的提示义务承载列（上传必填，读取必现）。

列约定：时间全部 epoch 毫秒（BigInteger）；标签以 JSON 字符串数组落库，
描述与来源备注长度由服务层按配置校验。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import Base, BigIntIdMixin, PersonaMixin, TimestampMixin

__all__ = ["StickerRow"]


class StickerRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """表情包行（每人格一份素材库）。

    Attributes:
        sha256: 文件 SHA-256（同人格去重键）。
        content_type: 图片 MIME 类型。
        storage_path: 文件落盘路径。
        size_bytes: 文件字节数。
        tags: 标签（JSON 字符串数组文本形态）。
        description: 描述（0-500 字符）。
        source_note: 素材来源与授权备注（上传必填）。
        annotation_status: VLM 标注状态（pending / done / failed）。
        annotation_error: 标注失败原因（成功为 NULL）。
        annotation_model_entry: 标注命中的视觉模型条目名（未标注为 NULL）。
        annotated_at_ms: 标注完成时间（epoch 毫秒；未标注为 NULL）。
    """

    __tablename__ = "ff_stickers"
    __table_args__ = (
        UniqueConstraint("persona_id", "sha256", name="uq_ff_stickers_persona_sha"),
        Index("ix_ff_stickers_annotation_status", "annotation_status"),
    )

    sha256: Mapped[str] = mapped_column(String(64), comment="文件 SHA-256（同人格去重键）")
    content_type: Mapped[str] = mapped_column(String(64), comment="图片 MIME 类型")
    storage_path: Mapped[str] = mapped_column(String(512), comment="文件落盘路径")
    size_bytes: Mapped[int] = mapped_column(Integer, comment="文件字节数")
    tags: Mapped[str] = mapped_column(Text, comment="标签（JSON 字符串数组）")
    description: Mapped[str] = mapped_column(Text, comment="描述（0-500 字符）")
    source_note: Mapped[str] = mapped_column(
        String(512), comment="素材来源与授权备注（上传必填——法律边界提示义务）"
    )
    annotation_status: Mapped[str] = mapped_column(
        String(16), comment="VLM 标注状态（pending/done/failed）"
    )
    annotation_error: Mapped[str | None] = mapped_column(
        Text, comment="标注失败原因（成功为 NULL）"
    )
    annotation_model_entry: Mapped[str | None] = mapped_column(
        String(128), comment="标注命中的视觉模型条目名（未标注为 NULL）"
    )
    annotated_at_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="标注完成时间（epoch 毫秒；未标注为 NULL）"
    )
