"""画布产物迁移（canvas 域）。

新建一张表：

    - ``ff_canvas_documents``：渲染产物（HTML / SVG / Markdown 文本）按人格
      作用域持久化。列面为 ``title``（展示名与导出文件名原料）/ ``kind``
      （html / svg / markdown）/ ``content``（自包含内容——LONGTEXT 承载
      256K 字符上限的余量）/ ``revision``（修订号，创建为 1、每次修改递增）/
      ``created_at`` / ``updated_at``（epoch 毫秒）。索引
      ``ix_ff_canvas_documents_persona_updated (persona_id, updated_at)``
      服务「当前人格最近修改」列表查询。无唯一键：同名产物合法（标题是
      展示面，主键与修订号才是标识）。

升级顺序：建表 → 建人格索引 → 建人格 + 更新时间复合索引。降级：按建索引
逆序删索引后删表（单表，无外键，无存量行数据迁移）。数据整体丢弃
（降级语义，文档化）。

Revision ID: 000000000017
Revises: 000000000016
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000017"
down_revision: Union[str, None] = "000000000016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建画布产物表与人格作用域索引。"""
    op.create_table(
        "ff_canvas_documents",
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
        sa.Column("title", sa.String(length=120), nullable=False, comment="标题"),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            comment="产物类型（html/svg/markdown）",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="自包含内容（HTML / SVG / Markdown）",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="修订号（创建为 1，修改递增）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ff_canvas_documents_persona_id"), "ff_canvas_documents", ["persona_id"]
    )
    op.create_index(
        "ix_ff_canvas_documents_persona_updated",
        "ff_canvas_documents",
        ["persona_id", "updated_at"],
    )


def downgrade() -> None:
    """降级：删复合索引 → 人格索引 → 画布产物表（数据整体丢弃）。"""
    op.drop_index(
        "ix_ff_canvas_documents_persona_updated", table_name="ff_canvas_documents"
    )
    op.drop_index(
        op.f("ix_ff_canvas_documents_persona_id"), table_name="ff_canvas_documents"
    )
    op.drop_table("ff_canvas_documents")
