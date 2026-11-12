"""persona 域 ORM——人格注册表 ``ff_personas`` + 状态表 ``ff_persona_states``。

设计决策（**两张表均非 PersonaMixin——注册表是全局管理面数据**）：

    人格注册表本身是「人格的目录」，而非某个人格的作用域数据：
    admin 需要在**无人格上下文**的管理面跨人格列出 / 切换 / 复制 / 删除
    人格。PersonaMixin 的请求上下文自动过滤（无人格上下文即 fail loud）
    与该语义直接冲突（自引用：人格行自身的 persona_id 该是什么？）。
    故两张表均为普通全局表（与 usage 域同一决策模式——跨人格管理面
    真源不入 PersonaMixin）。P1 记忆表才是 PersonaMixin 的消费面
    （per-persona 逻辑隔离）。

列与不变式约定：

    - ``ff_personas``：
        - ``name`` 全局唯一（注册键——重命名同样受唯一约束）；
        - ``status``（active/disabled）是**可用性开关**；``is_active`` 是
          **单激活绑定位**——全表至多一行 ``is_active=True``（激活即
          事务性单激活切换，见 service 层）；激活要求 status=active；
        - ``chara_card`` JSON 列整体存储 :class:`CharaCardModel` 文档
          （L 层正典——会话期不可变）；
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
    Boolean,
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

__all__ = ["PersonaRow", "PersonaStateRow"]


class PersonaRow(Base, BigIntIdMixin, TimestampMixin):
    """人格注册行（全局管理面目录——见模块 docstring 非 PersonaMixin 决策）。

    Attributes:
        name: 注册名（全局唯一——标识与引用键）。
        display_name: 展示名（管理面显示用；缺省回落 card.name）。
        status: 可用性状态（active / disabled；与 is_active 正交）。
        is_active: 单激活绑定位（全表至多一行为 True；激活事务性切换）。
        chara_card: 人格卡片 JSON 文档（CharaCardModel 序列化形态）。
    """

    __tablename__ = "ff_personas"
    __table_args__ = (UniqueConstraint("name", name="uq_ff_personas_name"),)

    name: Mapped[str] = mapped_column(String(128), comment="注册名（全局唯一）")
    display_name: Mapped[str] = mapped_column(String(128), comment="展示名")
    status: Mapped[str] = mapped_column(String(16), comment="可用性状态（active/disabled）")
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="单激活绑定位（全表至多一行 True；事务性切换）",
    )
    chara_card: Mapped[dict[str, Any]] = mapped_column(
        JSON, comment="人格卡片 JSON（chara card v2 文档）"
    )


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
