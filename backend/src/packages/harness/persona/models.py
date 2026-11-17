"""persona 域值域 Model Classes——人格状态 / L-M-S 状态层 / 导入冲突枚举。

值域（本工程人格注册表契约——多激活可寻址模型）：

    - :class:`PersonaStatus`     —— 人格管理面状态（active 可用 / disabled
      停用）；与主人格标记 ``PersonaRow.is_primary_marker`` 正交——
      status 是可用性开关（enabled 人格构成可寻址集，可同时被多个
      上下文寻址绑定），主人格标记是「缺省解析目标」绑定位（DB 唯一
      键强制全表至多一个）；
    - :class:`PersonaStateLayer` —— L/M/S 心理状态模型的可变层：

        - ``L``（long-term，长期身份）：**不存在于状态表**——不可变的
          人格正典直接以 chara card 字段承载（name / personality /
          scenario / first_mes / ...），会话期不更新；
        - ``M``（mid-term，中期心情 / 关系）：跨会话演化的心情与关系
          评价文档（JSON state）；
        - ``S``（short-term，短期会话状态）：单会话内的即时状态（话题
          焦点 / 情绪强度 / 最近意图等，JSON state）。

    - :class:`PersonaImportConflictStrategy` —— 导入冲突策略（skip 跳过 /
      rename 改名 / overwrite 覆盖；仅名称冲突时生效——内容同卡恒幂等）；
    - :class:`PersonaImportReason` —— 导入未落库原因（identical_content
      内容同卡 / name_conflict 名称冲突）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PersonaStatus",
    "PersonaStateLayer",
    "PersonaImportConflictStrategy",
    "PersonaImportReason",
]


class PersonaStatus(StrEnum):
    """人格管理面状态值域（可用性开关，与主人格标记正交）。

    - ``active``：可用——进入可寻址集（允许被通道 / 心流 / 多会话按
      上下文绑定），且允许被设为主人格（set-primary）；
    - ``disabled``：停用——管理面下线（退出可寻址集），设为主人格
      守卫拒绝（CONFLICT）；停用当前主人格被守卫拒绝（先另设主人格）。
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


class PersonaImportConflictStrategy(StrEnum):
    """导入冲突策略值域（仅名称冲突时生效——内容同卡恒幂等跳过）。

    - ``skip``：跳过（缺省）——不落库，返回 ``name_conflict`` 未导入
      结果（温和策略，调用方决定下一步）；
    - ``rename``：改名——用 ``desired_name``（提供且可用时）或自动
      后缀 ``名字-2`` 起寻找空闲名落库；
    - ``overwrite``：覆盖——整体替换同名人格的卡片（保留 M/S 状态 /
      status / 主人格标记 / 语音绑定——仅卡片文档替换）。
    """

    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"


class PersonaImportReason(StrEnum):
    """导入未落库原因值域（``imported=False`` 时的机器可读原因）。

    - ``identical_content``：内容同卡——导入卡片与既有人格卡片的内容
      哈希一致（幂等响应，与冲突策略无关）；
    - ``name_conflict``：名称冲突——目标注册名被占用且策略为 skip。
    """

    IDENTICAL_CONTENT = "identical_content"
    NAME_CONFLICT = "name_conflict"
