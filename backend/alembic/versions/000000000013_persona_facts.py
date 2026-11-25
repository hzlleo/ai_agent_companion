"""人物事实表迁移（persona 域——事实 / 证据链 / 更正循环）。

新建 ``ff_persona_facts`` 一张表：

    - 事实行（PersonaMixin 人格作用域——``persona_id`` 索引）；
    - 结构化列：``category`` / ``content`` / ``normalized_hash``（事实
      身份轴，索引）/ ``confidence`` / ``status`` / ``provenance`` /
      ``source``；
    - 证据链 ``evidence`` JSON 列（FactEvidenceRefModel 数组——情景事件
      回链 + 逐字摘录）；
    - 更正链：``superseded_by_id`` 自引用 FK（ON DELETE SET NULL）+
      ``version`` + 可空守卫列 ``active_marker``；
    - 单 active 不变式由唯一键 ``uq_ff_persona_facts_active
      (persona_id, category, normalized_hash, active_marker)`` 强制——
      现行行标记 ``'active'``、superseded / rejected 行 NULL，两方言
      唯一键均忽略 NULL（与 ``ff_curated_memory`` 同一已验证模式；迁移
      0012 的策展生命周期列不受影响）。

升级顺序：建表 → 建索引 → 建唯一键 / 自引用外键（单表迁移，无存量行
数据迁移）。降级：删表（事实与证据链整体丢弃——降级语义，文档化）。

Revision ID: 000000000013
Revises: 000000000012
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000013"
down_revision: Union[str, None] = "000000000012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 ff_persona_facts（事实列 / 证据链 / 单 active 唯一键）。"""
    op.create_table(
        "ff_persona_facts",
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
        sa.Column(
            "category",
            sa.String(length=16),
            nullable=False,
            comment="事实类别（preference/biography/relationship/context）",
        ),
        sa.Column(
            "content", sa.String(length=1000), nullable=False, comment="事实陈述（≤1000 字符）"
        ),
        sa.Column(
            "normalized_hash",
            sa.String(length=64),
            nullable=False,
            comment="正文归一化指纹（sha256——事实身份轴）",
        ),
        sa.Column("confidence", sa.Float(), nullable=False, comment="置信度（0-1）"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, comment="状态（active/superseded/rejected）"
        ),
        sa.Column(
            "provenance",
            sa.String(length=16),
            nullable=False,
            comment="来源可信度（owner/agent；untrusted 服务层拒绝）",
        ),
        sa.Column(
            "source", sa.String(length=16), nullable=False, comment="事实来源（dream/manual/correction）"
        ),
        sa.Column(
            "evidence",
            sa.JSON(),
            nullable=False,
            comment="证据引用 JSON 数组（FactEvidenceRefModel 序列化形态）",
        ),
        sa.Column(
            "superseded_by_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
            comment="取代者行主键（自引用；active 行为 NULL）",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="更正链版本号（首版 1）"),
        sa.Column(
            "active_marker",
            sa.String(length=16),
            nullable=True,
            comment=(
                "单 active 守卫标记（active 行 'active' / superseded、rejected 行 NULL"
                "——唯一键忽略 NULL，多代共存）"
            ),
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["ff_persona_facts.id"],
            name="fk_ff_persona_facts_superseded_by_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "persona_id",
            "category",
            "normalized_hash",
            "active_marker",
            name="uq_ff_persona_facts_active",
        ),
    )
    op.create_index(
        op.f("ix_ff_persona_facts_persona_id"), "ff_persona_facts", ["persona_id"]
    )
    op.create_index(
        "ix_ff_persona_facts_normalized_hash", "ff_persona_facts", ["normalized_hash"]
    )
    op.create_index("ix_ff_persona_facts_category", "ff_persona_facts", ["category"])
    op.create_index("ix_ff_persona_facts_status", "ff_persona_facts", ["status"])


def downgrade() -> None:
    """降级：删 ff_persona_facts（事实与证据链整体丢弃——降级语义）。"""
    op.drop_table("ff_persona_facts")
