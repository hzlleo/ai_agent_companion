"""memory 域值域 Model Classes——三层记忆的枚举 / 事件载荷 / 内容指纹。

三层记忆模型（本工程锁定设计）：

    - **episodic（情景层）**：append-only 事件日志——发生过的每件事一行
      （chat 回合 / duty 报告 / dream 产物 / platform 动作 / system 注记）；
      只增不改不覆盖，删除是管理面显式动作；
    - **curated（策展层）**：蒸馏后的结构化记忆——同 (persona, kind,
      topic) 至多一条 active 行，新版本经 **supersede 语义**取代旧行
      （旧行标记 superseded + 链接到新行，历史链可回溯）；
    - **working（工作层）**：chat 会话回合——经 ``ff_chat_sessions`` /
      ``ff_chat_messages`` 持久化（会话行承载人格作用域，消息行按键
      session 归属，见 orm 模块文档）。

provenance（来源可信度）taxonomy——全记忆层统一：

    - ``owner``：主人说的（可信度最高——策展层准入）；
    - ``agent``：智能体自己推断的（可信——策展层准入）；
    - ``untrusted``：平台内容 / 外部不可信输入（**永不进入策展层**——
      服务层强制拒绝；检索与梦境晋升链路依赖此不变式）。

事件载荷（content JSON 的 Model Class 形态——按 kind 判别）：

    - :class:`ChatTurnPayloadModel`        —— chat 回合载荷；
    - :class:`DutyReportPayloadModel`      —— duty 报告载荷；
    - :class:`PlatformActionPayloadModel`  —— platform 动作载荷；
    - :class:`EpisodeNotePayloadModel`     —— dream / system 注记通用载荷。

content_hash：kind + source + 载荷规范 JSON 的 sha256 短指纹（16 hex），
供 append 去重（幂等摄入）。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "EpisodeKind",
    "MemoryProvenance",
    "CuratedKind",
    "CuratedStatus",
    "ChatTurnPayloadModel",
    "DutyReportPayloadModel",
    "PlatformActionPayloadModel",
    "EpisodeNotePayloadModel",
    "EpisodePayload",
    "parse_episode_payload",
    "payload_for_kind",
    "compute_content_hash",
    "CONTENT_HASH_LENGTH",
]


class EpisodeKind(StrEnum):
    """情景事件类型值域（ff_memory_episodes.kind）。

    - ``chat``：对话回合（陪伴对话中值得记住的交互）；
    - ``duty``：值班任务事件（日程提醒 / 例行巡检报告）；
    - ``dream``：梦境产物（记忆整理叙事——P1 chunk 3 写入）；
    - ``platform``：平台动作（外部平台消息 / 指令记录）；
    - ``system``：系统注记（运维 / 管理面注入的备忘）。
    """

    CHAT = "chat"
    DUTY = "duty"
    DREAM = "dream"
    PLATFORM = "platform"
    SYSTEM = "system"


class MemoryProvenance(StrEnum):
    """记忆来源可信度值域（全记忆层统一 taxonomy）。

    - ``owner``：主人说的（最高可信）；
    - ``agent``：智能体推断的（可信）；
    - ``untrusted``：平台内容 / 外部不可信输入（永不入策展层——服务层
      强制拒绝，检索 / 晋升链路依赖此不变式）。
    """

    OWNER = "owner"
    AGENT = "agent"
    UNTRUSTED = "untrusted"


class CuratedKind(StrEnum):
    """策展记忆类型值域（ff_curated_memory.kind）。

    - ``memory``：蒸馏记忆（从情景层提炼的事实 / 偏好 / 经历）；
    - ``user_note``：主人备忘（显式注入的笔记）；
    - ``relationship``：关系状态（智能体与主人的关系演化记录）。
    """

    MEMORY = "memory"
    USER_NOTE = "user_note"
    RELATIONSHIP = "relationship"


class CuratedStatus(StrEnum):
    """策展记忆状态值域（ff_curated_memory.status）。

    - ``active``：现行版本（同 (persona, kind, topic) 至多一条）；
    - ``superseded``：已被新版本取代（superseded_by_id 指向取代者）。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"


# =========================================================================== []
# 事件载荷 Model Classes（content JSON 的类型化形态——按 kind 判别）
# =========================================================================== []


class ChatTurnPayloadModel(BaseModel):
    """chat 回合事件载荷（kind=chat 的 content 形态）。

    Attributes:
        user_content: 用户侧文本（单向回合可为空串）。
        assistant_content: 人格回复文本。
        session_id: 关联会话主键（ff_chat_sessions.id；无关联为 None）。
    """

    user_content: str = Field(default="", description="用户侧文本")
    assistant_content: str = Field(default="", description="人格回复文本")
    session_id: int | None = Field(default=None, description="关联会话主键")


class DutyReportPayloadModel(BaseModel):
    """duty 报告事件载荷（kind=duty 的 content 形态）。

    Attributes:
        title: 报告标题。
        summary: 报告摘要。
        details: 结构化明细（自由 JSON——报告体按任务形态自带结构）。
    """

    title: str = Field(default="", description="报告标题")
    summary: str = Field(default="", description="报告摘要")
    details: dict[str, Any] = Field(default_factory=dict, description="结构化明细")


class PlatformActionPayloadModel(BaseModel):
    """platform 动作事件载荷（kind=platform 的 content 形态）。

    Attributes:
        action: 动作标识（收到消息 / 发出回复 / 执行指令等）。
        summary: 动作摘要。
        metadata: 平台侧附加信息（自由 JSON）。
    """

    action: str = Field(default="", description="动作标识")
    summary: str = Field(default="", description="动作摘要")
    metadata: dict[str, Any] = Field(default_factory=dict, description="平台附加信息")


class EpisodeNotePayloadModel(BaseModel):
    """通用注记事件载荷（kind=dream / system 的 content 形态）。

    Attributes:
        text: 注记正文。
        metadata: 附加信息（自由 JSON）。
    """

    text: str = Field(default="", description="注记正文")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加信息")


#: 事件载荷联合类型（content 字段的类型面——按 kind 判别解析）。
EpisodePayload = (
    ChatTurnPayloadModel | DutyReportPayloadModel | PlatformActionPayloadModel | EpisodeNotePayloadModel
)

#: kind → 载荷 Model Class 判别表（解析真源；值域即 EpisodePayload 各成员类）。
_PAYLOAD_BY_KIND: dict[EpisodeKind, type[EpisodePayload]] = {
    EpisodeKind.CHAT: ChatTurnPayloadModel,
    EpisodeKind.DUTY: DutyReportPayloadModel,
    EpisodeKind.PLATFORM: PlatformActionPayloadModel,
    EpisodeKind.DREAM: EpisodeNotePayloadModel,
    EpisodeKind.SYSTEM: EpisodeNotePayloadModel,
}


def payload_for_kind(kind: EpisodeKind) -> type[EpisodePayload]:
    """按事件类型取载荷 Model Class（判别表查询）。

    Args:
        kind: 事件类型。

    Returns:
        该 kind 对应的载荷 Model Class。
    """
    return _PAYLOAD_BY_KIND[kind]


def parse_episode_payload(kind: EpisodeKind | str, raw: Any) -> EpisodePayload:
    """把原始 content JSON 解析为按 kind 判别的载荷 Model Class。

    Args:
        kind: 事件类型（枚举或字符串值）。
        raw: 原始载荷（应为 dict——ORM JSON 列 / API 请求体形态）。

    Returns:
        解析后的载荷 Model Class 实例。

    Raises:
        BusinessException: kind 非法或载荷不是 mapping / 校验失败
            （``VALIDATION_ERROR``）。
    """
    try:
        resolved_kind = EpisodeKind(kind)
    except ValueError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="事件类型非法",
            details={"kind": str(kind), "valid": [member.value for member in EpisodeKind]},
        ) from exc
    if not isinstance(raw, dict):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="事件载荷必须是 JSON 对象",
            details={"kind": resolved_kind.value, "got": type(raw).__name__},
        )
    try:
        payload = _PAYLOAD_BY_KIND[resolved_kind].model_validate(raw)
    except Exception as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="事件载荷校验失败",
            details={"kind": resolved_kind.value, "reason": str(exc)},
        ) from exc
    return payload


#: content_hash 短指纹长度（hex 字符数）。
CONTENT_HASH_LENGTH: int = 16


def compute_content_hash(
    kind: EpisodeKind | str, source: str, content: BaseModel | dict[str, Any]
) -> str:
    """计算情景事件内容指纹（kind + source + 载荷规范 JSON 的 sha256 短指纹）。

    去重键语义：同一 (kind, source, content) 恒同指纹——ts / importance
    不参与（时间与权重变化不改变「同一事件」的判定）。

    Args:
        kind: 事件类型。
        source: 事件来源标识。
        content: 载荷（Model Class 或已序列化 dict——统一走规范 JSON）。

    Returns:
        16 hex 字符短指纹。
    """
    payload_dict = content.model_dump() if isinstance(content, BaseModel) else content
    canonical = json.dumps(
        payload_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    material = f"{EpisodeKind(kind).value}\x1f{source}\x1f{canonical}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:CONTENT_HASH_LENGTH]
