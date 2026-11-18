"""chat 域群聊多人格 Model Classes——应答者选择 / 冷却记忆 / 配置（Unit 文件）。

同一群会话里可以有多个启用人格并存（多激活可寻址模型）。本模块的模型
只承载「谁应答」的输入与结论，选择算法与轮转规则在
:mod:`packages.harness.chat.persona_rotation`。默认 ``enabled=False``：
群聊仍由会话绑定的主人格单人格应答，既有语义零变化。

归属语义（锁定）：群聊多人格模式下每个应答人格拥有**自己的持久化会话**
（会话标题带人格主键后缀），因此 ``ff_chat_sessions.persona_id`` 就是该
会话全部消息的应答归属；助手回合的 ``replied_by`` 即会话行人格主键，
不依赖消息级冗余列，也不需要迁移。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "GROUP_PERSONA_MAX_RESPONDER_CAP",
    "GroupPersonaSettings",
    "GroupSelectionReason",
    "GroupResponderCandidateModel",
    "GroupResponderStateModel",
    "GroupPersonaSelectionModel",
]

#: 单条群消息的应答人格数量硬上限（配置值夹逼上界——防应答风暴）。
GROUP_PERSONA_MAX_RESPONDER_CAP: int = 4


class GroupPersonaSettings(BaseSettings):
    """群聊多人格应答配置（env_prefix=``GROUP_PERSONA_``）。

    Attributes:
        enabled: 是否启用群聊多人格（False = 群聊沿用单主人格应答，
            既有语义零变化）。
        max_responders: 单条群消息的应答人格数量上限（1 = 轮流单人格
            应答；>1 = 多人格依次接续同一轮对话）。
        cooldown_ms: 人格应答冷却毫秒（同一人格两次应答之间的最小间隔；
            0 = 不设冷却）。
    """

    enabled: bool = Field(default=False, description="是否启用群聊多人格应答")
    max_responders: int = Field(
        default=1,
        ge=1,
        le=GROUP_PERSONA_MAX_RESPONDER_CAP,
        description="单条群消息的应答人格数量上限（1-4）",
    )
    cooldown_ms: int = Field(
        default=60000, ge=0, description="同一人格两次应答之间的冷却毫秒"
    )

    model_config = SettingsConfigDict(
        env_prefix="GROUP_PERSONA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class GroupSelectionReason(StrEnum):
    """应答者选择理由值域（观测 / 日志面）。

    - ``DISABLED``：群聊多人格未启用；
    - ``NOT_GROUP``：单聊消息（不参与多人格轮转）；
    - ``NO_CANDIDATES``：无可用人格（空集——调用方回退既有单人格路径）；
    - ``ROTATION``：按轮转规则选出（优先上一轮未发言者）；
    - ``REPEAT_FALLBACK``：可用者都刚发过言（允许重复，保持连续应答）；
    - ``ALL_COOLING_FALLBACK``：全部处于冷却（选最久未发言者兜底）。
    """

    DISABLED = "disabled"
    NOT_GROUP = "not_group"
    NO_CANDIDATES = "no_candidates"
    ROTATION = "rotation"
    REPEAT_FALLBACK = "repeat_fallback"
    ALL_COOLING_FALLBACK = "all_cooling_fallback"


class GroupResponderCandidateModel(BaseModel):
    """应答人格候选（启用人格的应答历史摘要）。

    Attributes:
        persona_id: 人格主键。
        is_primary: 是否主人格（选择序的稳定参考值）。
        last_reply_ms: 该人格在本群聊的最近应答时刻（epoch 毫秒；None =
            尚未发言）。
    """

    persona_id: int = Field(gt=0, description="人格主键")
    is_primary: bool = Field(default=False, description="是否主人格")
    last_reply_ms: int | None = Field(
        default=None, description="最近应答时刻（epoch 毫秒；None = 未发言）"
    )


class GroupResponderStateModel(BaseModel):
    """群聊应答记忆（路由器按会话身份键持有——内存态）。

    Attributes:
        last_reply_ms_by_persona: 人格主键 → 最近应答时刻（epoch 毫秒）；
            冷却判定原料。
        last_responder_ids: 上一轮应答人格主键列表（重复抑制原料——
            优先让上一轮未发言的人格接话）。
    """

    last_reply_ms_by_persona: dict[int, int] = Field(
        default_factory=dict, description="人格主键 → 最近应答时刻（epoch 毫秒）"
    )
    last_responder_ids: list[int] = Field(
        default_factory=list, description="上一轮应答人格主键列表"
    )


class GroupPersonaSelectionModel(BaseModel):
    """应答者选择结论（路由器消费——按序逐人格应答与投递）。

    Attributes:
        responder_persona_ids: 本轮应答人格主键列表（去重、按选择序；
            空列表 = 回退既有单人格路径）。
        reason: 选择理由枚举。
        cooling_persona_ids: 本轮因冷却被排除的人格主键（观测面）。
    """

    responder_persona_ids: list[int] = Field(
        default_factory=list, description="本轮应答人格主键列表（选择序）"
    )
    reason: GroupSelectionReason = Field(description="选择理由枚举")
    cooling_persona_ids: list[int] = Field(
        default_factory=list, description="本轮因冷却被排除的人格主键"
    )
