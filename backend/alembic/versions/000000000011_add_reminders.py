"""持久化提醒表迁移（reminders 域——P10=B）。

``ff_reminders``：主人预约的定时提醒（PersonaMixin 人格作用域——与记忆
表同轴隔离）；扫描作业到期后经属主通道投递（见 harness/reminders/job）。

列与索引：

    - ``persona_id``：人格作用域列（PersonaMixin 契约——索引承载人格
      过滤）；
    - ``title``：提醒标题（1-200 字符，投递文案正文）；
    - ``due_at_ms``：到期时刻（epoch 毫秒；索引 + 复合索引首列组承载
      扫描到期判定）；
    - ``status``：状态（pending/delivered/cancelled/failed——服务层单向
      推进）；
    - ``delivery_attempts`` / ``last_error``：投递失败观测与上限计数；
    - ``delivered_at_ms``：投递成功时刻（NULL = 未投递）；
    - ``source``：来源（manual / chat——观测面）；
    - ``meta``：来源附加信息 JSON（可空）；
    - 复合索引 ``(persona_id, status, due_at_ms)``：扫描主查询形态（当前
      人格 pending 到期行——顺序与 WHERE / ORDER BY 一致）。

降级：删表（提醒数据丢弃——降级语义，文档化）。

Revision ID: 000000000011
Revises: 000000000010
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000011"
down_revision: Union[str, None] = "000000000010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 ff_reminders 表（人格作用域 + 到期 / 复合扫描索引）。"""
    op.create_table(
        "ff_reminders",
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
        sa.Column("title", sa.String(length=200), nullable=False, comment="提醒标题（1-200 字符）"),
        sa.Column(
            "due_at_ms",
            sa.BigInteger(),
            nullable=False,
            comment="到期时刻（epoch 毫秒——扫描判定轴）",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="状态（pending/delivered/cancelled/failed）",
        ),
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="投递失败累计次数（达上限转 failed）",
        ),
        sa.Column(
            "last_error",
            sa.String(length=512),
            nullable=True,
            comment="最近一次投递失败原因（NULL = 无失败）",
        ),
        sa.Column(
            "delivered_at_ms",
            sa.BigInteger(),
            nullable=True,
            comment="投递成功时刻（epoch 毫秒；NULL = 未投递）",
        ),
        sa.Column(
            "source", sa.String(length=16), nullable=False, comment="来源（manual=管理面；chat=对话捕获）"
        ),
        sa.Column(
            "meta",
            sa.JSON(),
            nullable=True,
            comment="来源附加信息 JSON（可空——不参与业务判定）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ff_reminders_persona_id"), "ff_reminders", ["persona_id"])
    op.create_index("ix_ff_reminders_due_at_ms", "ff_reminders", ["due_at_ms"])
    op.create_index(
        "ix_ff_reminders_persona_status_due",
        "ff_reminders",
        ["persona_id", "status", "due_at_ms"],
    )


def downgrade() -> None:
    """降级：删除 ff_reminders 表（提醒数据丢弃——降级语义）。"""
    op.drop_index("ix_ff_reminders_persona_status_due", table_name="ff_reminders")
    op.drop_index("ix_ff_reminders_due_at_ms", table_name="ff_reminders")
    op.drop_index(op.f("ix_ff_reminders_persona_id"), table_name="ff_reminders")
    op.drop_table("ff_reminders")
