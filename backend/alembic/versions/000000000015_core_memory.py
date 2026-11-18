"""核心记忆表迁移（persona 域——自编辑核心记忆块 / append-only 修订链）。

新建两张表：

    - ``ff_core_memory_blocks``：人格作用域的四块自编辑自我模型（块键为
      封闭四元组 ``self_identity`` / ``owner_profile`` /
      ``relationship_story`` / ``open_notes``）；列面为 ``block_key`` /
      ``content``（Text）/ ``locked``（主人专属写闸）/ ``version`` /
      ``updated_by``（owner/agent）/ ``last_edited_session_id``；唯一键
      ``uq_ff_core_memory_blocks_key (persona_id, block_key)`` 强制
      「每人格每块至多一行」——块行由服务层首次读取时懒建（四个空行），
      迁移本身不写数据；
    - ``ff_core_memory_revisions``：append-only 修订日志（服务层只插入，
      从不更新 / 删除）；``block_id`` 外键指向块行并 ``ON DELETE
      CASCADE``（块删除时修订随块清理）；列面为 ``version`` / ``content``
      / ``provenance``（owner/agent）/ ``source``（admin/chat）/
      ``session_id`` / ``ts_ms``（索引）；``block_id`` 与 ``ts_ms`` 建
      索引（按块取历史 / 时间倒序）。修订表不挂人格列——归属经
      ``block_id`` 指向已作用域化的块行（理由见
      ``harness/persona/core_memory_orm.py`` 模块文档）。

升级顺序：建块表与索引 → 建修订表与索引（单迁移，无存量行数据迁移）。
降级：先删修订表、再删块表（外键子表在前——MySQL 系引擎要求外键列
保有索引，删表顺序即引擎安全序）；数据整体丢弃（降级语义，文档化）。

Revision ID: 000000000015
Revises: 000000000014
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000015"
down_revision: Union[str, None] = "000000000014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建核心记忆块表与 append-only 修订表（唯一键 / 外键 / 索引）。"""
    op.create_table(
        "ff_core_memory_blocks",
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
            "block_key",
            sa.String(length=32),
            nullable=False,
            comment="块键（self_identity/owner_profile/relationship_story/open_notes）",
        ),
        sa.Column("content", sa.Text(), nullable=False, comment="块正文（0-2000 字符；空串不注入）"),
        sa.Column(
            "locked",
            sa.Boolean(),
            nullable=False,
            comment="是否锁定（True = 仅主人可改写）",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            comment="编辑版本号（首版 1，内容/锁定变更递增）",
        ),
        sa.Column(
            "updated_by", sa.String(length=16), nullable=False, comment="最近一次写入者（owner/agent）"
        ),
        sa.Column(
            "last_edited_session_id",
            sa.BigInteger(),
            nullable=True,
            comment="最近一次对话内编辑的会话主键（管理面编辑为 NULL）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("persona_id", "block_key", name="uq_ff_core_memory_blocks_key"),
    )
    op.create_index(
        op.f("ix_ff_core_memory_blocks_persona_id"), "ff_core_memory_blocks", ["persona_id"]
    )
    op.create_table(
        "ff_core_memory_revisions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
            comment="主键（BIGINT 自增）",
        ),
        sa.Column(
            "block_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            comment="归属块主键（外键；块删除级联清理）",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="修订产生时的块版本号"),
        sa.Column("content", sa.Text(), nullable=False, comment="修订后的块全文（历史不可变）"),
        sa.Column("provenance", sa.String(length=16), nullable=False, comment="写入者（owner/agent）"),
        sa.Column("source", sa.String(length=16), nullable=False, comment="写入来源（admin/chat）"),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            nullable=True,
            comment="对话内编辑的会话主键（管理面修订为 NULL）",
        ),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False, comment="修订时刻（epoch 毫秒）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.ForeignKeyConstraint(
            ["block_id"],
            ["ff_core_memory_blocks.id"],
            name="fk_ff_core_memory_revisions_block_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ff_core_memory_revisions_block_id", "ff_core_memory_revisions", ["block_id"]
    )
    op.create_index("ix_ff_core_memory_revisions_ts_ms", "ff_core_memory_revisions", ["ts_ms"])


def downgrade() -> None:
    """降级：删修订表与块表（先删外键子表——核心记忆数据整体丢弃）。

    ``ix_ff_core_memory_revisions_block_id`` 不单独 drop：该索引被外键约束
    占用，MySQL 系引擎（含 SeekDB）先删索引会报错误 5320——索引随
    ``drop_table`` 与 FK 一并清理（ts_ms 索引与 FK 无关，照常先删）。
    """
    op.drop_index("ix_ff_core_memory_revisions_ts_ms", table_name="ff_core_memory_revisions")
    op.drop_table("ff_core_memory_revisions")
    op.drop_index(
        op.f("ix_ff_core_memory_blocks_persona_id"), table_name="ff_core_memory_blocks"
    )
    op.drop_table("ff_core_memory_blocks")
