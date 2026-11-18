"""声纹档案 / 同意记录 / 表情包库迁移（voice + multimodal 域）。

新建三张表：

    - ``ff_voiceprint_samples``：声纹样本档案（每人格 × 每发送者一行一样本）。
      列面为 ``sender_id`` / ``sample_path``（样本文件落盘路径）/ ``sample_sha256``
      / ``duration_ms`` / ``sample_rate_hz`` / ``embedding``（说话人向量 JSON
      数组）/ ``embedding_dim``；唯一键
      ``uq_ff_voiceprint_samples_persona_sender_sha (persona_id, sender_id,
      sample_sha256)`` 强制「同样本不重复入库」（重复上传幂等跳过）；档案
      是否可用由样本条数与同意记录共同决定，迁移本身不写数据；
    - ``ff_voiceprint_consents``：同意记录（append-only 的授予行 + 撤回时刻）。
      列面为 ``consent_text_version`` / ``granted_at_ms`` / ``revoked_at_ms``
      （可空——NULL 表示同意仍然有效）；撤回只更新 ``revoked_at_ms``，行本身
      保留作为处理合法性凭证。无唯一键（同一发送者可多次授予 / 撤回，历史
      按 ``granted_at_ms`` 倒序可读）；
    - ``ff_stickers``：表情包库（每人格一份）。列面为 ``sha256``（去重键）/
      ``content_type`` / ``storage_path`` / ``size_bytes`` / ``tags``（JSON
      字符串数组）/ ``description`` / ``source_note``（素材来源与授权备注——
      上传必填）/ ``annotation_status``（pending / done / failed）/
      ``annotation_error`` / ``annotation_model_entry`` / ``annotated_at_ms``；
      唯一键 ``uq_ff_stickers_persona_sha (persona_id, sha256)`` 强制「同人格
      同一文件只存一份」。

升级顺序：建声纹样本表与索引 → 建同意记录表与索引 → 建表情包表与索引（单
迁移，无存量行数据迁移）。降级：按建表逆序删表（三表之间无外键，删表顺序不
触发 MySQL 系引擎的索引占用错误）。数据整体丢弃（降级语义，文档化）。

Revision ID: 000000000016
Revises: 000000000015
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000016"
down_revision: Union[str, None] = "000000000015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建声纹样本 / 同意记录 / 表情包三表（唯一键 / 索引）。"""
    op.create_table(
        "ff_voiceprint_samples",
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
            "sender_id",
            sa.String(length=128),
            nullable=False,
            comment="发送者 ID（平台侧用户标识；与人格共同构成声纹档案轴）",
        ),
        sa.Column(
            "sample_path",
            sa.String(length=512),
            nullable=False,
            comment="样本音频落盘路径（撤回 / 删除时随行清理）",
        ),
        sa.Column(
            "sample_sha256",
            sa.String(length=64),
            nullable=False,
            comment="样本音频 SHA-256（重复上传去重键）",
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, comment="样本时长（毫秒）"),
        sa.Column(
            "sample_rate_hz", sa.Integer(), nullable=False, comment="样本采样率（赫兹）"
        ),
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=False,
            comment="说话人向量（JSON 浮点数组——跨方言以文本承载）",
        ),
        sa.Column(
            "embedding_dim", sa.Integer(), nullable=False, comment="说话人向量维度"
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "persona_id",
            "sender_id",
            "sample_sha256",
            name="uq_ff_voiceprint_samples_persona_sender_sha",
        ),
    )
    op.create_index(
        op.f("ix_ff_voiceprint_samples_persona_id"), "ff_voiceprint_samples", ["persona_id"]
    )
    op.create_index(
        "ix_ff_voiceprint_samples_persona_sender",
        "ff_voiceprint_samples",
        ["persona_id", "sender_id"],
    )
    op.create_table(
        "ff_voiceprint_consents",
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
            "sender_id",
            sa.String(length=128),
            nullable=False,
            comment="发送者 ID（同意主体）",
        ),
        sa.Column(
            "consent_text_version",
            sa.String(length=64),
            nullable=False,
            comment="同意文本版本（与隐私告知版本一致——处理合法性凭证）",
        ),
        sa.Column(
            "granted_at_ms", sa.BigInteger(), nullable=False, comment="同意授予时间（epoch 毫秒）"
        ),
        sa.Column(
            "revoked_at_ms",
            sa.BigInteger(),
            nullable=True,
            comment="撤回时间（epoch 毫秒；NULL = 同意有效）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ff_voiceprint_consents_persona_id"), "ff_voiceprint_consents", ["persona_id"]
    )
    op.create_index(
        "ix_ff_voiceprint_consents_persona_sender",
        "ff_voiceprint_consents",
        ["persona_id", "sender_id"],
    )
    op.create_table(
        "ff_stickers",
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
            "sha256",
            sa.String(length=64),
            nullable=False,
            comment="文件 SHA-256（同人格去重键）",
        ),
        sa.Column(
            "content_type", sa.String(length=64), nullable=False, comment="图片 MIME 类型"
        ),
        sa.Column(
            "storage_path", sa.String(length=512), nullable=False, comment="文件落盘路径"
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, comment="文件字节数"),
        sa.Column(
            "tags", sa.Text(), nullable=False, comment="标签（JSON 字符串数组）"
        ),
        sa.Column("description", sa.Text(), nullable=False, comment="描述（0-500 字符）"),
        sa.Column(
            "source_note",
            sa.String(length=512),
            nullable=False,
            comment="素材来源与授权备注（上传必填——法律边界提示义务）",
        ),
        sa.Column(
            "annotation_status",
            sa.String(length=16),
            nullable=False,
            comment="VLM 标注状态（pending/done/failed）",
        ),
        sa.Column(
            "annotation_error",
            sa.Text(),
            nullable=True,
            comment="标注失败原因（成功为 NULL）",
        ),
        sa.Column(
            "annotation_model_entry",
            sa.String(length=128),
            nullable=True,
            comment="标注命中的视觉模型条目名（未标注为 NULL）",
        ),
        sa.Column(
            "annotated_at_ms",
            sa.BigInteger(),
            nullable=True,
            comment="标注完成时间（epoch 毫秒；未标注为 NULL）",
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=False, comment="创建时间（epoch 毫秒）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, comment="更新时间（epoch 毫秒）"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("persona_id", "sha256", name="uq_ff_stickers_persona_sha"),
    )
    op.create_index(op.f("ix_ff_stickers_persona_id"), "ff_stickers", ["persona_id"])
    op.create_index("ix_ff_stickers_annotation_status", "ff_stickers", ["annotation_status"])


def downgrade() -> None:
    """降级：按建表逆序删表情包 / 同意记录 / 声纹样本表（数据整体丢弃）。"""
    op.drop_index("ix_ff_stickers_annotation_status", table_name="ff_stickers")
    op.drop_index(op.f("ix_ff_stickers_persona_id"), table_name="ff_stickers")
    op.drop_table("ff_stickers")
    op.drop_index(
        "ix_ff_voiceprint_consents_persona_sender", table_name="ff_voiceprint_consents"
    )
    op.drop_index(
        op.f("ix_ff_voiceprint_consents_persona_id"), table_name="ff_voiceprint_consents"
    )
    op.drop_table("ff_voiceprint_consents")
    op.drop_index(
        "ix_ff_voiceprint_samples_persona_sender", table_name="ff_voiceprint_samples"
    )
    op.drop_index(
        op.f("ix_ff_voiceprint_samples_persona_id"), table_name="ff_voiceprint_samples"
    )
    op.drop_table("ff_voiceprint_samples")
