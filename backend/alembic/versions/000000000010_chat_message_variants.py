"""会话消息变体迁移（R2b swipes——variant_of_id / variant_index + 变体唯一键）。

``ff_chat_messages`` 增两列 + 重写会话内唯一键（消息变体面——单回合多
候选重生成 / 选定提升，见 harness/chat/swipe）：

    - ``variant_of_id``：可空自引用外键（``ff_chat_messages.id``，ON DELETE
      CASCADE——变体随父回合删除）；索引承载变体反查；NULL = 正典对话链
      行（非变体）；
    - ``variant_index``：可空整数——父回合内的变体序号（1..N，只增不
      复用）；NULL = 正典对话链行。两列由服务层强制语义：仅 assistant
      正典回合可作父回合、变体不再生成变体（见 ``memory.service``）；
    - 原 ``uq_ff_chat_messages_seq (session_id, seq)`` 唯一键被复合键
      ``uq_ff_chat_messages_seq_variant (session_id, seq, variant_of_id,
      variant_index)`` 取代：变体行共享父回合 seq（不进入 seq 链），同一
      (session, seq, 父回合, 序号) 至多一行——变体身份由 DB 兜底。正典行
      两列均为 NULL，两方言唯一键均忽略 NULL（多正典行以各自 seq 天然
      区分）；正典 seq 的唯一性由服务层会话行锁计数维持（DB 级兜底面
      让位于变体身份守卫，文档化决策）。

降级：先删除全部变体行（正典链恢复 seq 唯一前置），再删两个唯一键 /
外键 / 索引 / 列，并重建 ``uq_ff_chat_messages_seq``——变体数据丢弃
（降级语义，文档化）。

Revision ID: 000000000010
Revises: 000000000009
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000010"
down_revision: Union[str, None] = "000000000009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 变体父回合外键约束名（降级 / ORM 镜像面锚点）。
_VARIANT_FK_NAME: str = "fk_ff_chat_messages_variant_of_id"
#: 变体反查索引名（与 ORM ``index=True`` 默认命名一致）。
_VARIANT_INDEX_NAME: str = "ix_ff_chat_messages_variant_of_id"
#: 复合唯一键名（旧键 ``uq_ff_chat_messages_seq`` 已删除）。
_VARIANT_UNIQUE_NAME: str = "uq_ff_chat_messages_seq_variant"
#: 旧会话内 seq 唯一键名（降级重建）。
_LEGACY_UNIQUE_NAME: str = "uq_ff_chat_messages_seq"


def upgrade() -> None:
    """升级：变体两列 + 自引用外键 + 复合唯一键（旧 seq 唯一键腾位）。"""
    # 旧键腾位（变体行共享父回合 seq——旧 (session_id, seq) 键会拒绝第二
    # 条同 seq 的正典后继之外的变体行）。
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_constraint(_LEGACY_UNIQUE_NAME, type_="unique")
        batch.add_column(
            sa.Column(
                "variant_of_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
                comment="父回合主键（自引用 FK，级联删除；NULL = 正典对话链行）",
            )
        )
        batch.add_column(
            sa.Column(
                "variant_index",
                sa.Integer(),
                nullable=True,
                comment="父回合内变体序号（1..N；NULL = 正典对话链行）",
            )
        )
    # 索引先于外键（MySQL 系引擎要求外键列保有索引；SQLite batch 重建
    # 会保留已建索引）。
    op.create_index(_VARIANT_INDEX_NAME, "ff_chat_messages", ["variant_of_id"])
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.create_foreign_key(
            _VARIANT_FK_NAME,
            "ff_chat_messages",
            ["variant_of_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            _VARIANT_UNIQUE_NAME,
            ["session_id", "seq", "variant_of_id", "variant_index"],
        )


def downgrade() -> None:
    """降级：删除变体行 + 变体键/列，重建旧 (session_id, seq) 唯一键。"""
    # 变体数据丢弃：旧唯一键重建前必须清除共享父 seq 的变体行。
    op.execute("DELETE FROM ff_chat_messages WHERE variant_of_id IS NOT NULL")
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_constraint(_VARIANT_UNIQUE_NAME, type_="unique")
        batch.drop_constraint(_VARIANT_FK_NAME, type_="foreignkey")
    # MySQL 系引擎：外键删除后其索引才可删除（错误 5320 的逆序回归面）。
    op.drop_index(_VARIANT_INDEX_NAME, table_name="ff_chat_messages")
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_column("variant_index")
        batch.drop_column("variant_of_id")
        batch.create_unique_constraint(_LEGACY_UNIQUE_NAME, ["session_id", "seq"])
