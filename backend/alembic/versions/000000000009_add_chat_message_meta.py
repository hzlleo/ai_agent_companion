"""会话消息元数据列迁移（R2b 输出协议 / 轻情绪引擎持久面）。

``ff_chat_messages`` 增 ``meta`` 可空 JSON 列：

    - 承载 assistant 回合的内部观测元数据（``thought`` 心声 /
      ``emotion_deltas`` 情绪增量 / ``emotion_snapshot`` 情绪快照）——
      永不进入对方可见文本，仅会话详情 API 读侧暴露；
    - 可空（既有行 / 无元数据回合为 NULL——零回填、零默认值）；
    - 写入路径（``ChatSessionService.append_message``）以可选参数形式
      接受元数据，既有调用方零变更。

降级：删除该列（SQLite 走 batch 重建，MySQL 系引擎原生 DROP COLUMN
——两方言同一 Alembic 句子渲染，文档化于 docs/backend/emotion.md）。

Revision ID: 000000000009
Revises: 000000000008
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000009"
down_revision: Union[str, None] = "000000000008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：ff_chat_messages 增 meta 可空 JSON 列（零回填）。"""
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.add_column(
            sa.Column(
                "meta",
                sa.JSON(),
                nullable=True,
                comment="回合元数据 JSON（心声 / 情绪增量 / 情绪快照；NULL = 无元数据）",
            )
        )


def downgrade() -> None:
    """降级：删除 ff_chat_messages.meta 列（batch 重建——两方言同句渲染）。"""
    with op.batch_alter_table("ff_chat_messages") as batch:
        batch.drop_column("meta")
