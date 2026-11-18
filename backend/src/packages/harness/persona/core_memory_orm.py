"""persona 域核心记忆 ORM 行——``ff_core_memory_blocks`` / ``ff_core_memory_revisions``。

设计决策（**PersonaMixin 人格作用域 + append-only 修订链**）：

    - 块行（``ff_core_memory_blocks``）：固定四块（自我认知 / 关于对方 /
      我们的故事 / 备忘）×人格作用域，写路径 ``before_flush`` 自动注入
      ``persona_id``、读路径 ``do_orm_execute`` 自动过滤（与记忆域同一
      契约）；``(persona_id, block_key)`` 唯一键强制「每人格每块至多一
      行」。块集合固定意味着读取面可懒建种子行（首次读取补齐四个空块），
      调用方无需关心「某块尚未创建」这一中间态；
    - 修订行（``ff_core_memory_revisions``）：**append-only 事实日志**
      ——只插入、从不更新 / 删除（服务层无 update / delete 语句，块行
      删除时由 ``ON DELETE CASCADE`` 随块清理）。每行留存写入后的块全文
      与块版本号，供面板回溯「这句话是谁在什么时候改的」；
    - **为什么不给修订行挂 PersonaMixin**：修订的归属经 ``block_id``
      外键指向已作用域化的块行——读取先按块键取块（人格过滤在此生效），
      再按 ``block_id`` 取修订；给修订再加一层 ``persona_id`` 只会造成
      两处作用域轴需要保持同步，而没有额外的隔离收益；
    - ``locked`` 是主人专属写闸：人格工具路径在服务层被结构性拒绝，
      锁定 / 解锁只能经管理面主人编辑。

列约定：时间全部 epoch 毫秒（BigInteger，``ts_ms`` 为修订时刻并索引）；
枚举值以字符串落库（值域由 ``persona.core_memory_models`` 在 DTO 边界
校验）；正文为 Text 列（2-2000 字符超出 varchar 行宽规划）。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = ["CoreMemoryBlockRow", "CoreMemoryRevisionRow"]


class CoreMemoryBlockRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """核心记忆块行（固定四块 × 人格作用域——自编辑自我模型）。

    唯一键 ``uq_ff_core_memory_blocks_key (persona_id, block_key)`` 在 DB
    层强制「每人格每块至多一行」；块集合由
    :class:`~packages.harness.persona.core_memory_models.CoreBlockKey`
    锁定，服务层首次读取时懒建四个空行。

    Attributes:
        block_key: 块键（self_identity/owner_profile/relationship_story/
            open_notes——封闭四元组）。
        content: 块正文（0-2000 字符；空串表示该块尚未写入——不进入
            compose 注入）。
        locked: 是否锁定（True = 仅主人可改写；人格工具路径被拒绝）。
        version: 编辑版本号（首版 1，任何内容 / 锁定状态变更递增）。
        updated_by: 最近一次写入者（owner/agent）。
        last_edited_session_id: 最近一次对话内编辑的会话主键（管理面编辑
            为 NULL——无对话上下文）。
    """

    __tablename__ = "ff_core_memory_blocks"
    __table_args__ = (
        UniqueConstraint("persona_id", "block_key", name="uq_ff_core_memory_blocks_key"),
    )

    block_key: Mapped[str] = mapped_column(
        String(32),
        comment="块键（self_identity/owner_profile/relationship_story/open_notes）",
    )
    content: Mapped[str] = mapped_column(Text, comment="块正文（0-2000 字符；空串不注入）")
    locked: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否锁定（True = 仅主人可改写）"
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, comment="编辑版本号（首版 1，内容/锁定变更递增）"
    )
    updated_by: Mapped[str] = mapped_column(
        String(16), comment="最近一次写入者（owner/agent）"
    )
    last_edited_session_id: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="最近一次对话内编辑的会话主键（管理面编辑为 NULL）",
    )


class CoreMemoryRevisionRow(Base, BigIntIdMixin, TimestampMixin):
    """核心记忆修订行（append-only 历史——只插入、从不更新 / 删除）。

    归属经 ``block_id`` 外键指向块行（``ON DELETE CASCADE``——块删除时
    修订随块清理）；读取先经人格作用域化块查询再按 ``block_id`` 展开，
    故本表不挂 PersonaMixin（理由见模块文档）。

    Attributes:
        block_id: 归属块主键（外键，级联删除；索引——按块取修订）。
        version: 修订产生时的块版本号（与块行当时版本一致）。
        content: 修订后的块全文（逐字留存——历史不可变）。
        provenance: 写入者（owner/agent）。
        source: 写入来源（admin/chat）。
        session_id: 对话内编辑的会话主键（管理面修订为 NULL）。
        ts_ms: 修订时刻（epoch 毫秒——索引，最新在前排序键）。
    """

    __tablename__ = "ff_core_memory_revisions"
    __table_args__ = (
        Index("ix_ff_core_memory_revisions_block_id", "block_id"),
        Index("ix_ff_core_memory_revisions_ts_ms", "ts_ms"),
    )

    block_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_core_memory_blocks.id", ondelete="CASCADE"),
        comment="归属块主键（外键；块删除级联清理）",
    )
    version: Mapped[int] = mapped_column(Integer, comment="修订产生时的块版本号")
    content: Mapped[str] = mapped_column(Text, comment="修订后的块全文（历史不可变）")
    provenance: Mapped[str] = mapped_column(String(16), comment="写入者（owner/agent）")
    source: Mapped[str] = mapped_column(String(16), comment="写入来源（admin/chat）")
    session_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="对话内编辑的会话主键（管理面修订为 NULL）"
    )
    ts_ms: Mapped[int] = mapped_column(BigInteger, comment="修订时刻（epoch 毫秒）")
