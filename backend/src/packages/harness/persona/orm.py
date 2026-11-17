"""persona 域 ORM——人格注册表 ``ff_personas`` + 状态表 ``ff_persona_states``。

设计决策（**两张表均非 PersonaMixin——注册表是全局管理面数据**）：

    人格注册表本身是「人格的目录」，而非某个人格的作用域数据：
    admin 需要在**无人格上下文**的管理面跨人格列出 / 切换 / 复制 / 删除
    人格。PersonaMixin 的请求上下文自动过滤（无人格上下文即 fail loud）
    与该语义直接冲突（自引用：人格行自身的 persona_id 该是什么？）。
    故两张表均为普通全局表（与 usage 域同一决策模式——跨人格管理面
    真源不入 PersonaMixin）。memory 域记忆表才是 PersonaMixin 的消费面
    （per-persona 逻辑隔离）。

列与不变式约定（多激活可寻址模型——多人格同时可用，主人格唯一）：

    - ``ff_personas``：
        - ``name`` 全局唯一（注册键——重命名同样受唯一约束）；
        - ``status``（active/disabled）是**可用性开关**（生命周期管理面；
          enabled 人格构成可寻址集——见 service 层 ``list_addressable``）；
        - ``is_primary_marker`` 可空 String(16) 是**主人格标记**：主人格
          行恒 ``'primary'``、其余行恒 NULL——唯一键
          ``uq_ff_personas_primary (is_primary_marker)`` 对 NULL 不生效
          （MySQL / SQLite 同语义），**全表至多一个主人格由 DB 强制**
          （并发 set_primary 双写由唯一键拒绝 + 服务层重试收敛——与
          策展记忆 active_marker 同模式）；设为主人格要求 status=active；
          停用 / 删除当前主人格被服务层守卫拒绝（先另设主人格）；
        - ``chara_card`` JSON 列整体存储 :class:`CharaCardModel` 文档
          （L 层正典——会话期不可变）；
        - ``voice_profile`` JSON 可空列存储人格语音绑定
          （:class:`~packages.harness.voice.voice_models.VoiceProfileModel`
          序列化形态；未绑定为 NULL → 合成回落 settings 缺省发音人；
          语音绑定与卡片正交——管理面独立增删，不参与卡片补丁合并语义）；
        - 乐观锁版本列：**本工程 ORM 无版本计数约定**（database/base.py
          仅有 BigIntIdMixin / TimestampMixin / PersonaMixin，usage 域表
          亦无版本列）——遵循既有约定不引入 version 列；注册表写路径
          低频（管理面操作），冲突面由唯一约束 + 单事务切换兜底。
    - ``ff_persona_states``：每 (persona_id, layer) 至多一行（唯一约束）；
      ``persona_id`` 外键指向 ``ff_personas.id``（ON DELETE CASCADE）；
      服务层删除人格时显式同事务清理状态行（SQLite 测试方言默认不
      启用 FK 强制，DB 级 CASCADE 仅作直删 SQL 的兜底）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.database.base import (
    Base,
    BigIntIdMixin,
    TimestampMixin,
)

__all__ = ["PERSONA_PRIMARY_MARKER", "PersonaRow", "PersonaStateRow"]

#: 主人格标记常量（主人格行 is_primary_marker 列的恒定取值；其余行 NULL）。
PERSONA_PRIMARY_MARKER: str = "primary"


class PersonaRow(Base, BigIntIdMixin, TimestampMixin):
    """人格注册行（全局管理面目录——见模块 docstring 非 PersonaMixin 决策）。

    Attributes:
        name: 注册名（全局唯一——标识与引用键）。
        display_name: 展示名（管理面显示用；缺省回落 card.name）。
        status: 可用性状态（active / disabled；enabled 人格构成可寻址集）。
        is_primary_marker: 主人格标记（主人格行恒 ``'primary'``、其余行
            NULL；唯一键忽略 NULL——DB 强制全表至多一个主人格）。
        chara_card: 人格卡片 JSON 文档（CharaCardModel 序列化形态）。
        voice_profile: 人格语音绑定 JSON（VoiceProfileModel 序列化形态；
            NULL = 未绑定——合成回落 settings 缺省）。
    """

    __tablename__ = "ff_personas"
    __table_args__ = (
        UniqueConstraint("name", name="uq_ff_personas_name"),
        UniqueConstraint("is_primary_marker", name="uq_ff_personas_primary"),
    )

    name: Mapped[str] = mapped_column(String(128), comment="注册名（全局唯一）")
    display_name: Mapped[str] = mapped_column(String(128), comment="展示名")
    status: Mapped[str] = mapped_column(String(16), comment="可用性状态（active/disabled）")
    is_primary_marker: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="主人格标记（主人格行 'primary' / 其余 NULL——唯一键忽略 NULL，DB 强制至多一个主人格）",
    )
    chara_card: Mapped[dict[str, Any]] = mapped_column(
        JSON, comment="人格卡片 JSON（chara card v2 文档）"
    )
    voice_profile: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="人格语音绑定 JSON（VoiceProfileModel 形态；未绑定为 NULL）",
    )

    @property
    def is_primary(self) -> bool:
        """是否为主人格（``is_primary_marker=='primary'`` 的归一布尔面）。

        读取面 DTO（``PersonaRecordDTO.is_primary``）经 from_attributes
        消费本属性；标记字符串形态是存储实现细节，不外泄。
        """
        return self.is_primary_marker == PERSONA_PRIMARY_MARKER


class PersonaStateRow(Base, BigIntIdMixin, TimestampMixin):
    """人格心理状态行（L/M/S 模型的可变层——M / S）。

    每 (persona_id, layer) 一行；``state`` 为自由 JSON 文档（M 层：心情 /
    关系评价；S 层：会话内即时状态）。L 层（长期身份）**不在本表**——
    以 chara card 字段承载（会话期不可变，见 persona.models 层值域）。

    Attributes:
        persona_id: 所属人格 ID（外键 ff_personas.id，ON DELETE CASCADE）。
        layer: 状态层（M=中期心情/关系；S=短期会话状态）。
        state: 状态 JSON 文档（整体替换式更新）。
    """

    __tablename__ = "ff_persona_states"
    __table_args__ = (
        UniqueConstraint("persona_id", "layer", name="uq_ff_persona_states_persona_layer"),
    )

    persona_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("ff_personas.id", ondelete="CASCADE"),
        index=True,
        comment="所属人格 ID（FK ff_personas.id，级联删除）",
    )
    layer: Mapped[str] = mapped_column(String(8), comment="状态层（M=中期；S=短期）")
    state: Mapped[dict[str, Any]] = mapped_column(JSON, comment="状态 JSON 文档")
