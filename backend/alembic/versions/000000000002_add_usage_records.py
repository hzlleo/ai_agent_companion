"""用量记录表迁移——P0-4 交付（ff_usage_records）。

harness/usage 域的 token 用量记录表（llm 网关用量发射 → usage 服务
落库）。persona_id 为普通可空索引列而非 PersonaMixin（跨人格聚合
真源——设计决策见 ``packages/harness/usage/orm.py`` 模块 docstring）。

主键形态与 ORM 一致：BIGINT AUTO_INCREMENT（SQLite 方言渲染为
INTEGER——rowid 自增）。

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000002"
down_revision: Union[str, None] = "000000000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 ff_usage_records 表（用量记录 + 查询 / 聚合索引）。"""
    op.create_table(
        "ff_usage_records",
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
            nullable=True,
            comment="人格 ID（可空——系统调用无人格；普通索引列非 PersonaMixin）",
        ),
        sa.Column(
            "ts_ms",
            sa.BigInteger(),
            nullable=False,
            comment="调用发起时间（epoch 毫秒）",
        ),
        sa.Column(
            "model_entry",
            sa.String(length=128),
            nullable=False,
            comment="模型条目名（聚合维度）",
        ),
        sa.Column("provider", sa.String(length=64), nullable=False, comment="provider 类型"),
        sa.Column(
            "purpose",
            sa.String(length=16),
            nullable=False,
            comment="调用用途（聚合维度）",
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=False, comment="输入 token 数"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, comment="输出 token 数"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, comment="总 token 数"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="端到端时延（毫秒）"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, comment="调用结果（success/error）"
        ),
        sa.Column("request_id", sa.String(length=64), nullable=True, comment="请求追踪 ID（可空）"),
        sa.Column(
            "error_code",
            sa.String(length=64),
            nullable=True,
            comment="失败错误码（成功为 NULL）",
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
    )
    op.create_index(
        op.f("ix_ff_usage_records_persona_id"), "ff_usage_records", ["persona_id"]
    )
    op.create_index(op.f("ix_ff_usage_records_ts_ms"), "ff_usage_records", ["ts_ms"])
    op.create_index(
        op.f("ix_ff_usage_records_model_entry"), "ff_usage_records", ["model_entry"]
    )
    op.create_index(op.f("ix_ff_usage_records_purpose"), "ff_usage_records", ["purpose"])


def downgrade() -> None:
    """降级：删除 ff_usage_records 表（含索引）。"""
    op.drop_index(op.f("ix_ff_usage_records_purpose"), table_name="ff_usage_records")
    op.drop_index(op.f("ix_ff_usage_records_model_entry"), table_name="ff_usage_records")
    op.drop_index(op.f("ix_ff_usage_records_ts_ms"), table_name="ff_usage_records")
    op.drop_index(op.f("ix_ff_usage_records_persona_id"), table_name="ff_usage_records")
    op.drop_table("ff_usage_records")
