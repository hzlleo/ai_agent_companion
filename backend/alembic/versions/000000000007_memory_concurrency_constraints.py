"""策展单 active 守卫 + 会话消息 seq 唯一迁移（memory 域并发不变式）。

harness/memory 域两处 DB 级不变式（并发写竞态的强制面）：

    - ``ff_curated_memory`` 增 ``active_marker`` 可空列 + 唯一键
      ``uq_ff_curated_active (persona_id, kind, topic, active_marker)``：
      既有 active 行回填 ``'active'``，superseded 行保持 NULL——MySQL /
      SQLite 唯一键均忽略 NULL，同 (persona, kind, topic) 至多一条
      active 行由 DB 强制（并发 supersede 双写由唯一键拒绝 + 服务层
      重试收敛）；
    - ``ff_chat_messages`` 增唯一键 ``uq_ff_chat_messages_seq
      (session_id, seq)``：会话内 seq 至多一条（并发 append_message
      的 DB 级兜底；服务层另经会话行锁串行化计数）。

纯增列 + 增约束（additive）：既有行回填后零破坏，读写路径由服务层
维护标记（见 ``memory.service`` 的 upsert_curated / append_message）。

Revision ID: 000000000007
Revises: 000000000006
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000007"
down_revision: Union[str, None] = "000000000006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 轻量回填表形态（无 ORM 注册——仅本迁移内组装 UPDATE 语句用）。
_CURATED_BACKFILL_TABLE = sa.table(
    "ff_curated_memory",
    sa.column("status", sa.String),
    sa.column("active_marker", sa.String),
)


def upgrade() -> None:
    """升级：active_marker 列 + active 行回填 + 两个唯一键。"""
    op.add_column(
        "ff_curated_memory",
        sa.Column(
            "active_marker",
            sa.String(length=16),
            nullable=True,
            comment="单 active 守卫标记（active 行 'active' / superseded 行 NULL——唯一键忽略 NULL，多代共存）",
        ),
    )
    # 既有 active 行回填标记（先于唯一键创建——回填后不变式即刻完整）。
    op.execute(
        _CURATED_BACKFILL_TABLE.update()
        .where(_CURATED_BACKFILL_TABLE.c.status == "active")
        .values(active_marker="active")
    )
    with op.batch_alter_table("ff_curated_memory") as batch:
        batch.create_unique_constraint(
            "uq_ff_curated_active",
            ["persona_id", "kind", "topic", "active_marker"],
        )
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.create_unique_constraint("uq_ff_chat_messages_seq", ["session_id", "seq"])


def downgrade() -> None:
    """降级：移除两个唯一键 + 删除 active_marker 列（守卫标记数据丢弃）。"""
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_constraint("uq_ff_chat_messages_seq", type_="unique")
    with op.batch_alter_table("ff_curated_memory") as batch:
        batch.drop_constraint("uq_ff_curated_active", type_="unique")
    op.drop_column("ff_curated_memory", "active_marker")
