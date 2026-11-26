"""梦境运行单飞守卫迁移（memory 域——运行中标记 + 人格作用域唯一键）。

为 ``ff_dream_runs`` 增加可空 ``running_marker`` 列与唯一键
``uq_ff_dream_runs_persona_running (persona_id, running_marker)``：

    - 运行中行写入固定标记 ``'1'``，终态 / 失败 / 崩溃收敛清 NULL；
    - MySQL / SQLite 唯一键均忽略 NULL，历史运行行多代共存；
    - 同人格并发的第二次运行插入被 1062 拒绝（服务层回滚后返回既有
      在途运行）——这是单飞的 DB 级权威闸门，不依赖时间窗宽度。

回填：迁移瞬间可能存在的 ``status=running`` 行按人格保留最新一行打
标记（更早的行保持 NULL 且不阻塞新运行，交由服务入口的孤儿收敛按
``stale_running_threshold_ms`` 补齐终态）。

升级顺序：增列 → 回填最新运行中行 → 建唯一键（先回填后约束，避免
存量行撞键）。降级：删唯一键 → 删列（标记数据丢弃，运行行保留）。

Revision ID: 000000000019
Revises: 000000000018
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000019"
down_revision: Union[str, None] = "000000000018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 轻量回填表形态（无 ORM 注册——仅本迁移内组装 SELECT / UPDATE 语句用）。
_DREAM_RUNS_TABLE = sa.table(
    "ff_dream_runs",
    sa.column("id", sa.BigInteger),
    sa.column("persona_id", sa.String),
    sa.column("status", sa.String),
    sa.column("running_marker", sa.String),
)


def upgrade() -> None:
    """升级：running_marker 列 + 最新运行中行回填 + 单飞唯一键。"""
    op.add_column(
        "ff_dream_runs",
        sa.Column(
            "running_marker",
            sa.String(length=16),
            nullable=True,
            comment="单飞守卫标记（运行中 '1'；终态 / 失败 / 崩溃收敛清 NULL——唯一键忽略 NULL）",
        ),
    )
    connection = op.get_bind()
    running_rows = connection.execute(
        sa.select(_DREAM_RUNS_TABLE.c.id, _DREAM_RUNS_TABLE.c.persona_id)
        .where(_DREAM_RUNS_TABLE.c.status == "running")
        .order_by(_DREAM_RUNS_TABLE.c.id)
    ).fetchall()
    latest_by_persona: dict[str, int] = {}
    for row_id, persona_id in running_rows:
        latest_by_persona[str(persona_id)] = int(row_id)
    if latest_by_persona:
        connection.execute(
            _DREAM_RUNS_TABLE.update()
            .where(_DREAM_RUNS_TABLE.c.id.in_(list(latest_by_persona.values())))
            .values(running_marker="1")
        )
    with op.batch_alter_table("ff_dream_runs") as batch:
        batch.create_unique_constraint(
            "uq_ff_dream_runs_persona_running",
            ["persona_id", "running_marker"],
        )


def downgrade() -> None:
    """降级：移除单飞唯一键 + 删除 running_marker 列（运行行保留）。"""
    with op.batch_alter_table("ff_dream_runs") as batch:
        batch.drop_constraint("uq_ff_dream_runs_persona_running", type_="unique")
    op.drop_column("ff_dream_runs", "running_marker")
