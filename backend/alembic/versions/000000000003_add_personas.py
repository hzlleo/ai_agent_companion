"""人格注册表迁移——P0-5 交付（ff_personas + ff_persona_states）。

harness/persona 域两张表：

    - ``ff_personas``：人格注册表（全局管理面目录——非 PersonaMixin，
      设计决策见 ``packages/harness/persona/orm.py`` 模块 docstring）；
      ``name`` 全局唯一；``is_active`` 单激活位（全表至多一行 True）；
      ``chara_card`` JSON 列承载 chara card v2 文档。
    - ``ff_persona_states``：M/S 心理状态层（每 (persona_id, layer) 一行；
      L 层以卡片字段承载不入表）；``persona_id`` 外键级联删除。

主键形态与 ORM 一致：BIGINT AUTO_INCREMENT（SQLite 方言渲染为
INTEGER——rowid 自增）。

Revision ID: 000000000003
Revises: 000000000002
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000003"
down_revision: Union[str, None] = "000000000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 ff_personas + ff_persona_states 表（索引 / 唯一约束）。"""
    op.create_table(
        "ff_personas",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column("name", sa.String(length=128), nullable=False, comment="注册名（全局唯一）"),
        sa.Column("display_name", sa.String(length=128), nullable=False, comment="展示名"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="可用性状态（active/disabled）",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            comment="单激活绑定位（全表至多一行 True；事务性切换）",
        ),
        sa.Column(
            "chara_card",
            sa.JSON(),
            nullable=False,
            comment="人格卡片 JSON（chara card v2 文档）",
        ),
        sa.Column(
            "created_at",
            sa.BigInteger(),
            nullable=False,
            comment="创建时间（epoch 毫秒）",
        ),
        sa.Column(
            "updated_at",
            sa.BigInteger(),
            nullable=False,
            comment="更新时间（epoch 毫秒）",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_ff_personas_name"),
    )
    op.create_table(
        "ff_persona_states",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column(
            "persona_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            comment="所属人格 ID（FK ff_personas.id，级联删除）",
        ),
        sa.Column("layer", sa.String(length=8), nullable=False, comment="状态层（M=中期；S=短期）"),
        sa.Column("state", sa.JSON(), nullable=False, comment="状态 JSON 文档"),
        sa.Column(
            "created_at",
            sa.BigInteger(),
            nullable=False,
            comment="创建时间（epoch 毫秒）",
        ),
        sa.Column(
            "updated_at",
            sa.BigInteger(),
            nullable=False,
            comment="更新时间（epoch 毫秒）",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["persona_id"],
            ["ff_personas.id"],
            name="fk_ff_persona_states_persona_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("persona_id", "layer", name="uq_ff_persona_states_persona_layer"),
    )
    op.create_index(op.f("ix_ff_persona_states_persona_id"), "ff_persona_states", ["persona_id"])


def downgrade() -> None:
    """降级：删除 ff_persona_states + ff_personas 表（含索引 / 约束）。"""
    op.drop_index(op.f("ix_ff_persona_states_persona_id"), table_name="ff_persona_states")
    op.drop_table("ff_persona_states")
    op.drop_table("ff_personas")
