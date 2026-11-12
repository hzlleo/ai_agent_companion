"""记忆四表迁移——P1 chunk 1 交付（memory 域）。

harness/memory 域四张表：

    - ``ff_memory_episodes``：情景层事件日志（append-only；PersonaMixin
      人格作用域——ts_ms / kind / content_hash 索引）；
    - ``ff_curated_memory``：策展层蒸馏记忆（supersede 语义——
      superseded_by_id 自引用 FK；kind / topic / status 索引）；
    - ``ff_chat_sessions``：工作层会话头（PersonaMixin 人格作用域；
      last_active_at 索引）；
    - ``ff_chat_messages``：工作层会话消息（非 PersonaMixin——按
      session 归属，人格隔离经所属会话行间接承担；session_id FK
      级联删除 + 索引）。

主键形态与 ORM 一致：BIGINT AUTO_INCREMENT（SQLite 方言渲染为
INTEGER——rowid 自增）。

Revision ID: 000000000004
Revises: 000000000003
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000004"
down_revision: Union[str, None] = "000000000003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建记忆四表（索引 / 外键）。"""
    op.create_table(
        "ff_memory_episodes",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column("persona_id", sa.String(length=64), nullable=False, comment="人格 ID（请求上下文自动注入 + 查询自动过滤）"),
        sa.Column("kind", sa.String(length=16), nullable=False, comment="事件类型（chat/duty/dream/platform/system）"),
        sa.Column("source", sa.String(length=128), nullable=False, comment="事件来源标识"),
        sa.Column("content", sa.JSON(), nullable=False, comment="事件载荷 JSON（按 kind 判别）"),
        sa.Column("importance", sa.Integer(), nullable=False, comment="重要度（1-10）"),
        sa.Column("provenance", sa.String(length=16), nullable=False, comment="来源可信度（owner/agent/untrusted）"),
        sa.Column("content_hash", sa.String(length=16), nullable=False, comment="内容指纹（append 去重键）"),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False, comment="事件发生时间（epoch 毫秒）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_memory_episodes_persona_id"), "ff_memory_episodes", ["persona_id"])
    op.create_index(op.f("ix_ff_memory_episodes_ts_ms"), "ff_memory_episodes", ["ts_ms"])
    op.create_index("ix_ff_memory_episodes_kind", "ff_memory_episodes", ["kind"])
    op.create_index("ix_ff_memory_episodes_content_hash", "ff_memory_episodes", ["content_hash"])

    op.create_table(
        "ff_curated_memory",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column("persona_id", sa.String(length=64), nullable=False, comment="人格 ID（请求上下文自动注入 + 查询自动过滤）"),
        sa.Column("kind", sa.String(length=16), nullable=False, comment="策展类型（memory/user_note/relationship）"),
        sa.Column("topic", sa.String(length=255), nullable=False, comment="主题键（同 persona+kind+topic 单 active）"),
        sa.Column("content", sa.Text(), nullable=False, comment="记忆正文"),
        sa.Column("importance", sa.Integer(), nullable=False, comment="重要度（1-10）"),
        sa.Column("provenance", sa.String(length=16), nullable=False, comment="来源可信度（owner/agent；untrusted 服务层拒绝）"),
        sa.Column("status", sa.String(length=16), nullable=False, comment="状态（active/superseded）"),
        sa.Column(
            "superseded_by_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
            comment="取代者行主键（自引用；active 行为 NULL）",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="链内版本号（首版 1）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["ff_curated_memory.id"],
            name="fk_ff_curated_memory_superseded_by_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_curated_memory_persona_id"), "ff_curated_memory", ["persona_id"])
    op.create_index("ix_ff_curated_memory_kind", "ff_curated_memory", ["kind"])
    op.create_index("ix_ff_curated_memory_topic", "ff_curated_memory", ["topic"])
    op.create_index("ix_ff_curated_memory_status", "ff_curated_memory", ["status"])

    op.create_table(
        "ff_chat_sessions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column("persona_id", sa.String(length=64), nullable=False, comment="人格 ID（请求上下文自动注入 + 查询自动过滤）"),
        sa.Column("title", sa.String(length=255), nullable=False, comment="会话标题（展示面）"),
        sa.Column("message_count", sa.Integer(), nullable=False, comment="消息计数（下一消息 seq）"),
        sa.Column("last_active_at", sa.BigInteger(), nullable=False, comment="最后活跃时间（epoch 毫秒）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_chat_sessions_persona_id"), "ff_chat_sessions", ["persona_id"])
    op.create_index("ix_ff_chat_sessions_last_active_at", "ff_chat_sessions", ["last_active_at"])

    op.create_table(
        "ff_chat_messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column(
            "session_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            comment="所属会话 ID（FK ff_chat_sessions.id，级联删除）",
        ),
        sa.Column("seq", sa.Integer(), nullable=False, comment="会话内序号（从 1 递增）"),
        sa.Column("role", sa.String(length=16), nullable=False, comment="消息角色（user/assistant/system）"),
        sa.Column("content", sa.Text(), nullable=False, comment="消息文本"),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False, comment="消息时间（epoch 毫秒）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ff_chat_sessions.id"],
            name="fk_ff_chat_messages_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_chat_messages_session_id"), "ff_chat_messages", ["session_id"])


def downgrade() -> None:
    """降级：删除记忆四表（含索引 / 外键——逆依赖序）。"""
    op.drop_index(op.f("ix_ff_chat_messages_session_id"), table_name="ff_chat_messages")
    op.drop_table("ff_chat_messages")
    op.drop_index("ix_ff_chat_sessions_last_active_at", table_name="ff_chat_sessions")
    op.drop_index(op.f("ix_ff_chat_sessions_persona_id"), table_name="ff_chat_sessions")
    op.drop_table("ff_chat_sessions")
    op.drop_index("ix_ff_curated_memory_status", table_name="ff_curated_memory")
    op.drop_index("ix_ff_curated_memory_topic", table_name="ff_curated_memory")
    op.drop_index("ix_ff_curated_memory_kind", table_name="ff_curated_memory")
    op.drop_index(op.f("ix_ff_curated_memory_persona_id"), table_name="ff_curated_memory")
    op.drop_table("ff_curated_memory")
    op.drop_index("ix_ff_memory_episodes_content_hash", table_name="ff_memory_episodes")
    op.drop_index("ix_ff_memory_episodes_kind", table_name="ff_memory_episodes")
    op.drop_index(op.f("ix_ff_memory_episodes_ts_ms"), table_name="ff_memory_episodes")
    op.drop_index(op.f("ix_ff_memory_episodes_persona_id"), table_name="ff_memory_episodes")
    op.drop_table("ff_memory_episodes")
