"""persona 域值域 Model Classes——人格状态 / L-M-S 状态层枚举。

值域（本工程人格注册表契约）：

    - :class:`PersonaStatus`     —— 人格管理面状态（active 可用 / disabled
      停用）；与单激活位 ``PersonaRow.is_active`` 正交——status 是可用性
      开关，is_active 是「当前唯一激活人格」绑定位（至多一个为 True）；
    - :class:`PersonaStateLayer` —— L/M/S 心理状态模型的可变层：

        - ``L``（long-term，长期身份）：**不存在于状态表**——不可变的
          人格正典直接以 chara card 字段承载（name / personality /
          scenario / first_mes / ...），会话期不更新；
        - ``M``（mid-term，中期心情 / 关系）：跨会话演化的心情与关系
          评价文档（JSON state）；
        - ``S``（short-term，短期会话状态）：单会话内的即时状态（话题
          焦点 / 情绪强度 / 最近意图等，JSON state）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["PersonaStatus", "PersonaStateLayer"]


class PersonaStatus(StrEnum):
    """人格管理面状态值域（可用性开关，与单激活位正交）。

    - ``active``：可用——允许被激活（activate）为当前人格；
    - ``disabled``：停用——管理面下线，激活守卫拒绝（CONFLICT）；
      停用切换时若该人格正持有激活位则自动清位（见 service 层约定）。
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class PersonaStateLayer(StrEnum):
    """L/M/S 心理状态模型的可变层值域（ff_persona_states.layer）。

    - ``M``：中期层——心情 / 关系评价（跨会话演化）；
    - ``S``：短期层——会话内即时状态（话题焦点 / 情绪强度 / 最近意图）。

    注：长期层 ``L`` 不入状态表——人格身份正典（personaity / 关系设定 /
    开场白等）以 chara card 字段承载，会话期不可变（见模块 docstring）。
    """

    M = "M"
    S = "S"
