"""技能表迁移（persona 域——程序性技能 / 触发短语 / 原地版本）。

新建 ``ff_persona_skills`` 一张表：

    - 技能行（PersonaMixin 人格作用域——``persona_id`` 索引）；
    - 内容列：``name`` / ``slug`` / ``description`` / ``triggers``（JSON
      触发短语数组）/ ``content``（markdown 步骤正文，Text 列）；
    - 运行列：``status``（active/disabled）/ ``source``（manual/mined）/
      ``confidence`` / ``use_count`` / ``last_used_ms`` / ``version``；
    - 唯一键 ``uq_ff_persona_skills_slug (persona_id, slug)`` 强制同人格
      同标识至多一行——技能是操作指令，编辑走**原地更新**（``version``
      递增）而非历史链（旧版指令无回溯语义价值，理由见
      ``harness/persona/skills_orm.py`` 模块文档）。

升级顺序：建表 → 建索引 → 建唯一键（单表迁移，无存量行数据迁移）。
降级：删表（技能数据整体丢弃——降级语义，文档化）。

Revision ID: 000000000014
Revises: 000000000013
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000014"
down_revision: Union[str, None] = "000000000013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 ff_persona_skills（技能列 / 触发短语 / slug 唯一键）。"""
    op.create_table(
        "ff_persona_skills",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column(
            "persona_id",
            sa.String(length=64),
            nullable=False,
            comment="人格 ID（请求上下文自动注入 + 查询自动过滤）",
        ),
        sa.Column("name", sa.String(length=120), nullable=False, comment="技能名（1-120 字符）"),
        sa.Column(
            "slug",
            sa.String(length=80),
            nullable=False,
            comment="归一化 kebab 标识（同人格唯一）",
        ),
        sa.Column(
            "description",
            sa.String(length=500),
            nullable=False,
            comment="何时使用的触发描述（1-500 字符）",
        ),
        sa.Column(
            "triggers",
            sa.JSON(),
            nullable=False,
            comment="触发短语 JSON 数组（1-8 条、每条 1-40 字符——字面包含匹配）",
        ),
        sa.Column("content", sa.Text(), nullable=False, comment="markdown 步骤正文（1-8000 字符）"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, comment="状态（active/disabled）"
        ),
        sa.Column("source", sa.String(length=16), nullable=False, comment="来源（manual/mined）"),
        sa.Column("confidence", sa.Float(), nullable=False, comment="置信度（0-1）"),
        sa.Column(
            "use_count", sa.Integer(), nullable=False, comment="compose 命中次数（写读强化轴）"
        ),
        sa.Column(
            "last_used_ms",
            sa.BigInteger(),
            nullable=True,
            comment="最近一次命中时间（epoch 毫秒；未命中为 NULL）",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="原地编辑版本号（首版 1，编辑递增——无历史链）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("persona_id", "slug", name="uq_ff_persona_skills_slug"),
    )
    op.create_index(op.f("ix_ff_persona_skills_persona_id"), "ff_persona_skills", ["persona_id"])
    op.create_index("ix_ff_persona_skills_status", "ff_persona_skills", ["status"])


def downgrade() -> None:
    """降级：删除 ff_persona_skills（技能数据整体丢弃——降级语义）。"""
    op.drop_index("ix_ff_persona_skills_status", table_name="ff_persona_skills")
    op.drop_index(op.f("ix_ff_persona_skills_persona_id"), table_name="ff_persona_skills")
    op.drop_table("ff_persona_skills")
