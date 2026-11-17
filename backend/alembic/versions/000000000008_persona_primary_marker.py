"""主人格标记迁移（persona 域多激活可寻址模型——is_active 单激活位退役）。

harness/persona 域注册表 ``ff_personas`` 的绑定位模型重构：

    - 增 ``is_primary_marker`` 可空 String(16) 列 + 唯一键
      ``uq_ff_personas_primary (is_primary_marker)``：主人格行回填
      ``'primary'``（承接旧 ``is_active=True`` 的单激活不变式——旧模型
      保证全表至多一行激活，回填后不变式即刻完整），其余行 NULL——
      MySQL / SQLite 唯一键均忽略 NULL，**全表至多一个主人格由 DB
      强制**（并发 set_primary 双写由唯一键拒绝 + 服务层重试收敛——
      与 0007 策展记忆 active_marker 同模式）；
    - 删 ``is_active`` 布尔列（单激活位语义由主人格标记整体取代——
      多人格同时可用，主人格唯一）。

升级数据兼容：既有 ``is_active=1`` 的行（旧不变式保证至多一行）回填为
主人格；零激活形态（无 is_active=1 行）回填后保持零主人格（读取面
fail loud——调用方须显式 set-primary）。降级逆向重建 ``is_active``
（主人格行回填 True，其余 False）。

Revision ID: 000000000008
Revises: 000000000007
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000008"
down_revision: Union[str, None] = "000000000007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 轻量回填表形态（无 ORM 注册——仅本迁移内组装 UPDATE 语句用）。
_PERSONAS_BACKFILL_TABLE = sa.table(
    "ff_personas",
    sa.column("is_active", sa.Boolean),
    sa.column("is_primary_marker", sa.String),
)


def upgrade() -> None:
    """升级：is_primary_marker 列 + 旧激活行回填 + 唯一键 + 删 is_active 列。"""
    op.add_column(
        "ff_personas",
        sa.Column(
            "is_primary_marker",
            sa.String(length=16),
            nullable=True,
            comment="主人格标记（主人格行 'primary' / 其余 NULL——唯一键忽略 NULL，DB 强制至多一个主人格）",
        ),
    )
    # 既有激活行回填标记（先于唯一键创建——回填后不变式即刻完整；
    # 旧单激活不变式保证至多一行 is_active=1，回填不撞键）。谓词经
    # ``is_(True)`` 渲染 ``IS true``（两方言均支持；避免 == 布尔字面量
    # 的 E712 面与裸列真值歧义）。
    op.execute(
        _PERSONAS_BACKFILL_TABLE.update()
        .where(_PERSONAS_BACKFILL_TABLE.c.is_active.is_(True))
        .values(is_primary_marker="primary")
    )
    with op.batch_alter_table("ff_personas") as batch:
        batch.create_unique_constraint("uq_ff_personas_primary", ["is_primary_marker"])
        batch.drop_column("is_active")


def downgrade() -> None:
    """降级：重建 is_active 列（主人格行回填 True）+ 删唯一键与标记列。"""
    op.add_column(
        "ff_personas",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="单激活绑定位（全表至多一行 True；事务性切换）",
        ),
    )
    # 主人格行回填激活位（降级方向不变式同样至多一行——唯一键保证）。
    op.execute(
        _PERSONAS_BACKFILL_TABLE.update()
        .where(_PERSONAS_BACKFILL_TABLE.c.is_primary_marker == "primary")
        .values(is_active=True)
    )
    with op.batch_alter_table("ff_personas") as batch:
        batch.drop_constraint("uq_ff_personas_primary", type_="unique")
        batch.drop_column("is_primary_marker")
