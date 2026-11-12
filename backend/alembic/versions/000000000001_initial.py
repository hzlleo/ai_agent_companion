"""初始基线迁移——P0-3 交付（本 chunk 无域表）。

harness 骨架（config / database / pools）先行落地，域表（persona /
chat / memory / usage 等）由后续 chunk 交付并在各自迁移中创建。本迁移
仅承载 Alembic 版本链锚点（``alembic_version`` 表由框架自建），确保：

    - 全新库 ``pixi run alembic upgrade head`` 可直接到达基线；
    - 后续迁移挂在稳定 revision 之下，链起点可追溯。

Revision ID: 000000000001
Revises:
Create Date: 2026-11-12
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "000000000001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：无操作（版本链锚点）。"""


def downgrade() -> None:
    """降级：无操作（版本链锚点）。"""
