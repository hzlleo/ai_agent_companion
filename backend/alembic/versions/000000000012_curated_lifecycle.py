"""策展记忆生命周期列迁移（memory 域——R2c 记忆生命周期）。

``ff_curated_memory`` 增补四列（生命周期math / 管理面开关的持久化面）：

    - ``pinned``：置顶标记（BOOL NOT NULL 默认 0——主人显式置顶；置顶行
      免于时间衰减与归档）；
    - ``frozen``：冻结标记（BOOL NOT NULL 默认 0——冻结主题不参与梦境
      合并提案 / 不接收并入；也不被归档）；
    - ``access_count``：检索命中累计次数（INTEGER NOT NULL 默认 0——检索
      写读强化轴，见 harness.memory.retrieval 写读强化约定）；
    - ``last_accessed_ms``：最近检索命中时刻（BIGINT NULL——未命中为
      NULL）。

状态值域扩展：``status`` 列新增取值 ``archived``（归档态——时间衰减 /
使用强化的生命周期终态）。值域是字符串列，**无数据迁移**：既有
active / superseded 行语义不变；归档行的单 active 守卫标记
（``active_marker``）由服务层同步置 NULL，使同主题可重新起链。

升级顺序：先加列（server_default 覆盖存量行）再放开默认值——两方言
（MySQL 协议 / SQLite）对 ``ALTER TABLE ADD COLUMN`` + 默认值的支持面
一致（本迁移不涉外键 / 索引，故无 5320 方言面）。

降级：删四列（生命周期状态丢弃——降级语义，文档化）；``archived``
值域的存量行须先回退为 ``superseded``（否则降级后状态值越界——本迁移
显式做该数据回退，保证 round-trip 后 ``status`` 值落在旧值域内）。

Revision ID: 000000000012
Revises: 000000000011
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000012"
down_revision: Union[str, None] = "000000000011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：ff_curated_memory 增补生命周期四列（置顶 / 冻结 / 访问统计）。"""
    op.add_column(
        "ff_curated_memory",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="置顶标记（免于时间衰减与归档）",
        ),
    )
    op.add_column(
        "ff_curated_memory",
        sa.Column(
            "frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="冻结标记（不参与梦境合并 / 不被归档）",
        ),
    )
    op.add_column(
        "ff_curated_memory",
        sa.Column(
            "access_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="检索命中累计次数（写读强化轴）",
        ),
    )
    op.add_column(
        "ff_curated_memory",
        sa.Column(
            "last_accessed_ms",
            sa.BigInteger(),
            nullable=True,
            comment="最近检索命中时刻（epoch 毫秒；NULL = 未命中）",
        ),
    )


def downgrade() -> None:
    """降级：删生命周期四列（archived 存量行先回退为 superseded）。"""
    op.execute(
        sa.text(
            "UPDATE ff_curated_memory SET status = 'superseded' "
            "WHERE status = 'archived'"
        )
    )
    op.drop_column("ff_curated_memory", "last_accessed_ms")
    op.drop_column("ff_curated_memory", "access_count")
    op.drop_column("ff_curated_memory", "frozen")
    op.drop_column("ff_curated_memory", "pinned")
