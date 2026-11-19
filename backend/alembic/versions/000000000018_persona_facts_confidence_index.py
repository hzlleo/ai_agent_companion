"""人物事实复合索引迁移（persona 域——画像读取面的作用域 + 状态 + 置信度查询）。

为 ``ff_persona_facts`` 增加复合索引
``ix_ff_persona_facts_persona_status_confidence (persona_id, status,
confidence)``：服务层按人格作用域 + 状态过滤、再按置信度排序读取现行
事实面（管理面板列表 / compose 注入候选 / 关系摘要派生），复合索引
使该读路径避免全表扫描。

列约定：本表时间列 ``created_at`` / ``updated_at`` 均为 epoch 毫秒
（BigInteger）；``confidence`` 为 0-1 浮点，索引按升序存储（降序读取
由优化器反向扫描）。

升级顺序：仅建索引（单表，无数据迁移）。降级：删索引（数据保留）。

Revision ID: 000000000018
Revises: 000000000017
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000018"
down_revision: str | None = "000000000017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级：创建人格作用域 + 状态 + 置信度复合索引。"""
    op.create_index(
        "ix_ff_persona_facts_persona_status_confidence",
        "ff_persona_facts",
        ["persona_id", "status", "confidence"],
    )


def downgrade() -> None:
    """降级：删除复合索引（事实行保留）。"""
    op.drop_index(
        "ix_ff_persona_facts_persona_status_confidence", table_name="ff_persona_facts"
    )
