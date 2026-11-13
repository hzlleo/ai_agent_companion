"""memory 域 ORM——三层记忆四表（P1 chunk 1 交付）。

表清单：

    - ``ff_memory_episodes``  —— 情景层事件日志（append-only）；
    - ``ff_curated_memory``   —— 策展层蒸馏记忆（supersede 语义）；
    - ``ff_chat_sessions``    —— 工作层会话头（人格作用域承载者）；
    - ``ff_chat_messages``    —— 工作层会话消息（按 session 归属）。

设计决策（**人格作用域切分**）：

    - 三张表挂 ``PersonaMixin``（episodes / curated / sessions）——记忆是
      典型的 per-persona 作用域数据：写路径 ``before_flush`` 自动注入
      persona_id，读路径 ``do_orm_execute`` 自动过滤（见 database.base
      的 Mixin 契约）；调用方（服务 / API / CLI）须先经
      ``bind_persona_context`` 绑定上下文，否则 fail loud；
    - ``ff_chat_messages`` **不挂 PersonaMixin**（messages keyed by
      session only）：消息的归属轴是会话，人格隔离经其所属会话行间接
      承担——消息的读写路径一律先经人格作用域的会话行定位 session_id
      （get → NOT_FOUND 即隔离生效），再按键取消息。避免消息行冗余
      persona 列带来的双轴一致性负担。

append-only 语义（ff_memory_episodes）：

    - 只增不改：服务层无 update 路径（ORM 不设约束，纪律由服务层 +
      API 面承担——仅 append / query / admin delete）；
    - 删除是管理面显式动作（admin API 逐条 / 批量部分成功）。

supersede 语义（ff_curated_memory）：

    - 同 (persona_id, kind, topic) 至多一条 ``status=active`` 行——由
      服务层在单事务内保证（先查 active 行 → 标记 superseded + 链接
      新行 → 插入新 active 行）；
    - ``superseded_by_id`` 自引用 FK 指向取代者（旧行 → 新行方向；
      active 行为 NULL）；``version`` 随链递增（首版 1）；
    - 删除 active 行后该 topic 无 active 行，下次 upsert 重新起链
      （version 从 1 重新计数——文档化决策）。

列约定：时间全部 epoch 毫秒（BigInteger）；枚举值以字符串落库（值域
由 memory.models 枚举在 DTO 边界校验）；importance 1-10。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)

__all__ = [
    "MemoryEpisodeRow",
    "CuratedMemoryRow",
    "ChatSessionRow",
    "ChatMessageRow",
]


class MemoryEpisodeRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """情景层事件行（append-only 事件日志——发生过的每件事一行）。

    Attributes:
        kind: 事件类型（chat/duty/dream/platform/system）。
        source: 事件来源标识（如 ``chat`` / ``duty:daily`` / ``platform:qq``）。
        content: 事件载荷 JSON（按 kind 判别的 Model Class 序列化形态，
            见 memory.models 载荷契约）。
        importance: 重要度（1-10；检索排序 / 梦境筛选权重轴）。
        provenance: 来源可信度（owner/agent/untrusted）。
        content_hash: 内容指纹（kind+source+载荷规范 JSON 的 sha256
            短指纹；append 去重键）。
        ts_ms: 事件发生时间（epoch 毫秒；索引——日记 / 时间范围查询轴）。
    """

    __tablename__ = "ff_memory_episodes"
    __table_args__ = (
        Index("ix_ff_memory_episodes_kind", "kind"),
        Index("ix_ff_memory_episodes_content_hash", "content_hash"),
    )

    kind: Mapped[str] = mapped_column(String(16), comment="事件类型（chat/duty/dream/platform/system）")
    source: Mapped[str] = mapped_column(String(128), comment="事件来源标识")
    content: Mapped[dict[str, Any]] = mapped_column(JSON, comment="事件载荷 JSON（按 kind 判别）")
    importance: Mapped[int] = mapped_column(Integer, comment="重要度（1-10）")
    provenance: Mapped[str] = mapped_column(String(16), comment="来源可信度（owner/agent/untrusted）")
    content_hash: Mapped[str] = mapped_column(String(16), comment="内容指纹（append 去重键）")
    ts_ms: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="事件发生时间（epoch 毫秒）"
    )


class CuratedMemoryRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """策展层蒸馏记忆行（supersede 语义——同 (persona, kind, topic) 单 active）。

    Attributes:
        kind: 策展类型（memory/user_note/relationship）。
        topic: 主题键（同 (persona, kind, topic) 至多一条 active 行）。
        content: 记忆正文（文本）。
        importance: 重要度（1-10）。
        provenance: 来源可信度（owner/agent——untrusted 由服务层拒绝，
            永不落本表）。
        status: 状态（active/superseded）。
        superseded_by_id: 取代者行主键（自引用 FK；active 行为 NULL）。
        version: 链内版本号（首版 1，supersede 递增）。
    """

    __tablename__ = "ff_curated_memory"
    __table_args__ = (
        Index("ix_ff_curated_memory_kind", "kind"),
        Index("ix_ff_curated_memory_topic", "topic"),
        Index("ix_ff_curated_memory_status", "status"),
    )

    kind: Mapped[str] = mapped_column(String(16), comment="策展类型（memory/user_note/relationship）")
    topic: Mapped[str] = mapped_column(String(255), comment="主题键（同 persona+kind+topic 单 active）")
    content: Mapped[str] = mapped_column(Text, comment="记忆正文")
    importance: Mapped[int] = mapped_column(Integer, comment="重要度（1-10）")
    provenance: Mapped[str] = mapped_column(
        String(16), comment="来源可信度（owner/agent；untrusted 服务层拒绝）"
    )
    status: Mapped[str] = mapped_column(String(16), comment="状态（active/superseded）")
    superseded_by_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_curated_memory.id", ondelete="SET NULL"),
        comment="取代者行主键（自引用；active 行为 NULL）",
    )
    version: Mapped[int] = mapped_column(Integer, comment="链内版本号（首版 1）")


class ChatSessionRow(Base, BigIntIdMixin, TimestampMixin, PersonaMixin):
    """工作层会话头行（人格作用域承载者——消息行经本表间接隔离）。

    Attributes:
        title: 会话标题（展示面）。
        message_count: 消息计数（append_message 递增——亦即下一消息 seq）。
        last_active_at: 最后活跃时间（epoch 毫秒；索引——最近会话排序轴）。
    """

    __tablename__ = "ff_chat_sessions"
    __table_args__ = (Index("ix_ff_chat_sessions_last_active_at", "last_active_at"),)

    title: Mapped[str] = mapped_column(String(255), comment="会话标题（展示面）")
    message_count: Mapped[int] = mapped_column(Integer, comment="消息计数（下一消息 seq）")
    last_active_at: Mapped[int] = mapped_column(
        BigInteger, comment="最后活跃时间（epoch 毫秒）"
    )


class ChatMessageRow(Base, BigIntIdMixin, TimestampMixin):
    """工作层会话消息行（**非 PersonaMixin**——按 session 归属）。

    人格隔离经所属会话行间接承担：消息读写路径先经人格作用域的
    ``ChatSessionRow`` 定位 session_id（他人格会话 NOT_FOUND 即隔离
    生效），再按键取消息——见模块 docstring 设计决策。

    Attributes:
        session_id: 所属会话主键（FK ff_chat_sessions.id，级联删除）。
        seq: 会话内序号（从 1 递增——append_message 维护）。
        role: 消息角色（user/assistant/system）。
        content: 消息文本。
        ts_ms: 消息时间（epoch 毫秒——回合产生时刻）。
    """

    __tablename__ = "ff_chat_messages"

    session_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_chat_sessions.id", ondelete="CASCADE"),
        index=True,
        comment="所属会话 ID（FK ff_chat_sessions.id，级联删除）",
    )
    seq: Mapped[int] = mapped_column(Integer, comment="会话内序号（从 1 递增）")
    role: Mapped[str] = mapped_column(String(16), comment="消息角色（user/assistant/system）")
    content: Mapped[str] = mapped_column(Text, comment="消息文本")
    ts_ms: Mapped[int] = mapped_column(BigInteger, comment="消息时间（epoch 毫秒）")
