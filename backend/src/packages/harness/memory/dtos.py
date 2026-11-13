"""memory 域 DTO——情景事件 / 策展记忆 / 会话持久化的传输契约（Unit 文件）。

契约分层：

    - 情景层：:class:`EpisodeCreateDTO`（写入面，载荷按 kind 判别解析）/
      :class:`EpisodeRecordDTO`（读取面）/ :class:`EpisodeQueryDTO`
      （kind / provenance / 时间范围过滤 + 游标分页）/ 批量删除三件套
      （部分成功形态）；
    - 策展层：:class:`CuratedUpsertDTO`（upsert 写入面）/
      :class:`CuratedRecordDTO`（读取面）/ :class:`CuratedQueryDTO`
      （kind / topic / status 过滤 + 游标分页）；
    - 会话层：:class:`ChatSessionQueryDTO` / :class:`ChatSessionSummaryDTO`
      / :class:`ChatSessionDetailDTO` / :class:`ChatMessageDTO`。

时间语义：全部 epoch 毫秒；范围过滤**左闭右开**
（``start_ms <= ts_ms < end_ms``——与 usage 域一致，桶对齐无重叠）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    DutyReportPayloadModel,
    EpisodeKind,
    EpisodeNotePayloadModel,
    EpisodePayload,
    MemoryProvenance,
    PlatformActionPayloadModel,
)

__all__ = [
    "EpisodeCreateDTO",
    "EpisodeRecordDTO",
    "EpisodeQueryDTO",
    "EpisodeBatchDeleteRequest",
    "EpisodeBatchDeleteItemDTO",
    "EpisodeBatchDeleteResponseDTO",
    "CuratedUpsertDTO",
    "CuratedRecordDTO",
    "CuratedQueryDTO",
    "ChatSessionQueryDTO",
    "ChatSessionSummaryDTO",
    "ChatSessionDetailDTO",
    "ChatMessageDTO",
]

#: kind → 载荷 Model Class 判别表（DTO 边界 content 解析真源；与
#: memory.models._PAYLOAD_BY_KIND 同构——此处独立声明避免私有表导出）。
_DTO_PAYLOAD_BY_KIND: dict[str, type[EpisodePayload]] = {
    EpisodeKind.CHAT.value: ChatTurnPayloadModel,
    EpisodeKind.DUTY.value: DutyReportPayloadModel,
    EpisodeKind.PLATFORM.value: PlatformActionPayloadModel,
    EpisodeKind.DREAM.value: EpisodeNotePayloadModel,
    EpisodeKind.SYSTEM.value: EpisodeNotePayloadModel,
}


def _dispatch_content(data: Any) -> Any:
    """DTO 边界 content 判别（mode=before 前置归一——dict → 载荷 Model Class）。

    Args:
        data: 原始输入（EpisodeCreateDTO 构造参数——dict / 已解析模型均可）。

    Returns:
        归一后的输入（content 为 dict 且 kind 合法时替换为判别载荷实例；
        其余原样返回——kind 非法交由字段校验报告）。
    """
    if not isinstance(data, dict):
        return data
    kind_raw = data.get("kind")
    payload_cls = _DTO_PAYLOAD_BY_KIND.get(str(kind_raw))
    content = data.get("content")
    if payload_cls is not None and isinstance(content, dict):
        return {**data, "content": payload_cls.model_validate(content)}
    return data


class EpisodeCreateDTO(BaseModel):
    """情景事件创建 DTO（写入面——载荷按 kind 判别解析）。

    Attributes:
        kind: 事件类型。
        source: 事件来源标识（1-128 字符）。
        content: 事件载荷（按 kind 判别的 Model Class——dict 输入在
            DTO 边界归一解析，类型不匹配 kind 校验失败）。
        importance: 重要度（1-10，默认 5）。
        provenance: 来源可信度（默认 owner——管理面手工注入面）。
        ts_ms: 事件发生时间（epoch 毫秒；None = 落库时取当前时间）。
    """

    model_config = ConfigDict(protected_namespaces=())

    kind: EpisodeKind = Field(description="事件类型")
    source: str = Field(min_length=1, max_length=128, description="事件来源标识")
    content: EpisodePayload = Field(description="事件载荷（按 kind 判别）")
    importance: int = Field(default=5, ge=1, le=10, description="重要度（1-10）")
    provenance: MemoryProvenance = Field(
        default=MemoryProvenance.OWNER, description="来源可信度（默认 owner）"
    )
    ts_ms: int | None = Field(
        default=None, ge=0, description="事件发生时间（epoch 毫秒；None=当前时间）"
    )

    @model_validator(mode="before")
    @classmethod
    def _dispatch_payload(cls, data: Any) -> Any:
        """content dict 按 kind 判别解析为载荷 Model Class。

        Args:
            data: 原始输入。

        Returns:
            归一后的输入（见 :func:`_dispatch_content`）。
        """
        return _dispatch_content(data)

    @model_validator(mode="after")
    def _verify_payload_kind(self) -> EpisodeCreateDTO:
        """载荷类型与 kind 一致性校验（直供模型实例时防错配）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: 载荷类型与 kind 判别表不符（如 kind=chat 却给
                duty 报告载荷）。
        """
        expected = _DTO_PAYLOAD_BY_KIND[self.kind.value]
        if type(self.content) is not expected:
            raise ValueError(
                f"事件载荷类型与 kind 不匹配: kind={self.kind.value}, "
                f"expected={expected.__name__}, got={type(self.content).__name__}"
            )
        return self


class EpisodeRecordDTO(BaseModel):
    """情景事件读取 DTO（ORM 行归一——服务层经判别解析构造）。

    Attributes:
        id: 事件主键。
        persona_id: 所属人格 ID。
        kind: 事件类型。
        source: 事件来源标识。
        content: 事件载荷（按 kind 判别的 Model Class）。
        importance: 重要度（1-10）。
        provenance: 来源可信度。
        content_hash: 内容指纹（去重键）。
        ts_ms: 事件发生时间（epoch 毫秒）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒；append-only 下与创建同值）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="事件主键")
    persona_id: str = Field(description="所属人格 ID")
    kind: EpisodeKind = Field(description="事件类型")
    source: str = Field(description="事件来源标识")
    content: EpisodePayload = Field(description="事件载荷（按 kind 判别）")
    importance: int = Field(description="重要度（1-10）")
    provenance: MemoryProvenance = Field(description="来源可信度")
    content_hash: str = Field(description="内容指纹（去重键）")
    ts_ms: int = Field(description="事件发生时间（epoch 毫秒）")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class EpisodeQueryDTO(BaseModel):
    """情景事件查询 DTO（kind / provenance / 时间范围过滤 + 游标分页——
    id 降序，新事件在前）。

    Attributes:
        kind: 事件类型过滤（None = 全部）。
        provenance: 来源可信度过滤（None = 全部）。
        start_ms: 时间范围起点（epoch 毫秒，含；None = 不限）。
        end_ms: 时间范围终点（epoch 毫秒，不含；None = 不限）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    kind: EpisodeKind | None = Field(default=None, description="事件类型过滤")
    provenance: MemoryProvenance | None = Field(default=None, description="来源可信度过滤")
    start_ms: int | None = Field(default=None, ge=0, description="范围起点（epoch 毫秒，含）")
    end_ms: int | None = Field(default=None, ge=0, description="范围终点（epoch 毫秒，不含）")
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class EpisodeBatchDeleteRequest(BaseModel):
    """情景事件批量删除请求（部分成功形态——逐 id 独立事务）。

    Attributes:
        ids: 待删除事件主键列表（1-100 个；append-only 语义下删除是
            管理面显式动作）。
    """

    ids: list[int] = Field(
        min_length=1,
        max_length=100,
        description="待删除事件主键列表（1-100 个）",
    )


class EpisodeBatchDeleteItemDTO(BaseModel):
    """情景事件批量删除结果单条目。

    Attributes:
        id: 事件主键。
        success: 是否删除成功。
        code: 失败错误码（成功为 None）。
        message: 面向用户的提示信息。
    """

    id: int = Field(description="事件主键")
    success: bool = Field(description="是否删除成功")
    code: str | None = Field(default=None, description="失败错误码（成功为 None）")
    message: str = Field(default="", description="提示信息")


class EpisodeBatchDeleteResponseDTO(BaseModel):
    """情景事件批量删除响应（部分成功形态）。

    Attributes:
        total: 提交总数。
        succeeded: 成功数量。
        failed: 失败数量。
        items: 逐 id 结果列表（与提交顺序一致）。
    """

    total: int = Field(description="提交总数")
    succeeded: int = Field(description="成功数量")
    failed: int = Field(description="失败数量")
    items: list[EpisodeBatchDeleteItemDTO] = Field(
        default_factory=list, description="逐 id 结果"
    )


class CuratedUpsertDTO(BaseModel):
    """策展记忆 upsert DTO（写入面——supersede 语义）。

    Attributes:
        kind: 策展类型。
        topic: 主题键（1-255 字符；同 (persona, kind, topic) 单 active）。
        content: 记忆正文（非空文本）。
        importance: 重要度（1-10，默认 5）。
        provenance: 来源可信度（默认 owner；**untrusted 由服务层拒绝**——
            平台内容永不入策展层）。
    """

    kind: CuratedKind = Field(description="策展类型")
    topic: str = Field(min_length=1, max_length=255, description="主题键")
    content: str = Field(min_length=1, description="记忆正文")
    importance: int = Field(default=5, ge=1, le=10, description="重要度（1-10）")
    provenance: MemoryProvenance = Field(
        default=MemoryProvenance.OWNER, description="来源可信度（untrusted 服务层拒绝）"
    )


class CuratedRecordDTO(BaseModel):
    """策展记忆读取 DTO（ORM 行归一）。

    Attributes:
        id: 记忆主键。
        persona_id: 所属人格 ID。
        kind: 策展类型。
        topic: 主题键。
        content: 记忆正文。
        importance: 重要度（1-10）。
        provenance: 来源可信度（owner/agent）。
        status: 状态（active/superseded）。
        superseded_by_id: 取代者行主键（active 行为 None）。
        version: 链内版本号（首版 1）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="记忆主键")
    persona_id: str = Field(description="所属人格 ID")
    kind: CuratedKind = Field(description="策展类型")
    topic: str = Field(description="主题键")
    content: str = Field(description="记忆正文")
    importance: int = Field(description="重要度（1-10）")
    provenance: MemoryProvenance = Field(description="来源可信度")
    status: CuratedStatus = Field(description="状态（active/superseded）")
    superseded_by_id: int | None = Field(description="取代者行主键（active 为 None）")
    version: int = Field(description="链内版本号")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class CuratedQueryDTO(BaseModel):
    """策展记忆查询 DTO（kind / topic / status 过滤 + 游标分页——id 降序）。

    Attributes:
        kind: 策展类型过滤（None = 全部）。
        topic: 主题键精确过滤（None = 全部）。
        status: 状态过滤（None = 全部；active = 现行版本面）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    kind: CuratedKind | None = Field(default=None, description="策展类型过滤")
    topic: str | None = Field(default=None, description="主题键精确过滤")
    status: CuratedStatus | None = Field(default=None, description="状态过滤")
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class ChatSessionQueryDTO(BaseModel):
    """会话列表查询 DTO（游标分页——id 降序，新会话在前）。

    Attributes:
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class ChatSessionSummaryDTO(BaseModel):
    """会话摘要 DTO（列表面——ORM 行归一）。

    Attributes:
        id: 会话主键。
        persona_id: 所属人格 ID。
        title: 会话标题。
        message_count: 消息计数。
        created_at: 创建时间（epoch 毫秒）。
        last_active_at: 最后活跃时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="会话主键")
    persona_id: str = Field(description="所属人格 ID")
    title: str = Field(description="会话标题")
    message_count: int = Field(description="消息计数")
    created_at: int = Field(description="创建时间（epoch 毫秒）")
    last_active_at: int = Field(description="最后活跃时间（epoch 毫秒）")


class ChatMessageDTO(BaseModel):
    """会话消息 DTO（ORM 行归一——按 session 归属，非人格作用域行）。

    Attributes:
        id: 消息主键。
        session_id: 所属会话主键。
        seq: 会话内序号（从 1 递增）。
        role: 消息角色（user/assistant/system）。
        content: 消息文本。
        ts_ms: 消息时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="消息主键")
    session_id: int = Field(description="所属会话主键")
    seq: int = Field(description="会话内序号（从 1 递增）")
    role: ChatMessageRole = Field(description="消息角色")
    content: str = Field(description="消息文本")
    ts_ms: int = Field(description="消息时间（epoch 毫秒）")


class ChatSessionDetailDTO(BaseModel):
    """会话详情 DTO（摘要 + 全量消息——seq 升序）。

    Attributes:
        id: 会话主键。
        persona_id: 所属人格 ID。
        title: 会话标题。
        message_count: 消息计数。
        created_at: 创建时间（epoch 毫秒）。
        last_active_at: 最后活跃时间（epoch 毫秒）。
        messages: 消息列表（seq 升序——时间序回放形态）。
    """

    id: int = Field(description="会话主键")
    persona_id: str = Field(description="所属人格 ID")
    title: str = Field(description="会话标题")
    message_count: int = Field(description="消息计数")
    created_at: int = Field(description="创建时间（epoch 毫秒）")
    last_active_at: int = Field(description="最后活跃时间（epoch 毫秒）")
    messages: list[ChatMessageDTO] = Field(default_factory=list, description="消息列表（seq 升序）")
