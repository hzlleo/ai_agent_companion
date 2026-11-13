"""梦境运行日志表迁移——P1 chunk 3 交付（memory 域梦境整理）。

harness/memory 域新增一张表：

    - ``ff_dream_runs``：夜间梦境整理运行日志（PersonaMixin 人格作用域；
      每次运行一行——状态 / 统计 / 逐事件决策 / 日记事件链接 / 失败原因）。

主键形态与 ORM 一致：BIGINT AUTO_INCREMENT（SQLite 方言渲染为
INTEGER——rowid 自增）。

Revision ID: 000000000005
Revises: 000000000004
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000005"
down_revision: Union[str, None] = "000000000004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建梦境运行日志表（索引 / 外键）。"""
    op.create_table(
        "ff_dream_runs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column("persona_id", sa.String(length=64), nullable=False, comment="人格 ID（请求上下文自动注入 + 查询自动过滤）"),
        sa.Column("status", sa.String(length=16), nullable=False, comment="运行状态（running/success/failed）"),
        sa.Column("started_ms", sa.BigInteger(), nullable=False, comment="开始时间（epoch 毫秒）"),
        sa.Column("finished_ms", sa.BigInteger(), nullable=True, comment="结束时间（epoch 毫秒；运行中为 None）"),
        sa.Column("stats", sa.JSON(), nullable=False, comment="运行统计 JSON（DreamRunStatsModel 形态）"),
        sa.Column("decisions", sa.JSON(), nullable=False, comment="逐事件决策 JSON 列表（DreamDecisionModel 形态）"),
        sa.Column(
            "diary_episode_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
            comment="日记事件主键（FK ff_memory_episodes.id；未生成为 None）",
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="失败原因（成功为 None）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.ForeignKeyConstraint(
            ["diary_episode_id"],
            ["ff_memory_episodes.id"],
            name="fk_ff_dream_runs_diary_episode_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_dream_runs_persona_id"), "ff_dream_runs", ["persona_id"])
    op.create_index("ix_ff_dream_runs_status", "ff_dream_runs", ["status"])


def downgrade() -> None:
    """降级：删除梦境运行日志表（含索引 / 外键）。"""
    op.drop_index("ix_ff_dream_runs_status", table_name="ff_dream_runs")
    op.drop_index(op.f("ix_ff_dream_runs_persona_id"), table_name="ff_dream_runs")
    op.drop_table("ff_dream_runs")
