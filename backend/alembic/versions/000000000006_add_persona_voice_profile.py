"""人格语音绑定列迁移——P4 chunk 1 交付（voice 域人格扩展点）。

harness/persona 域 ``ff_personas`` 表新增一列：

    - ``voice_profile``：人格语音绑定 JSON（可空；
      :class:`~packages.harness.voice.voice_models.VoiceProfileModel`
      序列化形态——speaker / instruct_text / speed）。

语义：NULL = 未绑定（语音合成回落 ``Settings.voice.tts`` 缺省参数）；
绑定存在时人格级 speaker / speed 覆盖 settings 缺省（优先级：
请求参数 > 人格绑定 > settings 缺省——见 harness/voice 域文档）。

纯增列（additive）：既有行落 NULL，既有读写路径零破坏。

Revision ID: 000000000006
Revises: 000000000005
Create Date: 2026-11-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000006"
down_revision: Union[str, None] = "000000000005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：ff_personas 增 voice_profile JSON 可空列。"""
    op.add_column(
        "ff_personas",
        sa.Column(
            "voice_profile",
            sa.JSON(),
            nullable=True,
            comment="人格语音绑定 JSON（VoiceProfileModel 形态；未绑定为 NULL）",
        ),
    )


def downgrade() -> None:
    """降级：删除 voice_profile 列（绑定数据丢弃——降级即解除绑定）。"""
    op.drop_column("ff_personas", "voice_profile")
