"""persona 域人物事实 Model Classes——结构化事实 / 证据链 / 更正面的传输契约。

人物事实（persona facts）是人格对**主人（对方）**的结构化认知：偏好
（口味 / 作息 / 说话习惯）、背景（职业 / 经历）、关系（称呼 / 相处模式）、
近况（近期目标 / 状态变化）。与长期指令（对人格的行为约定）的分界：
指令约束人格「怎么回应」，事实描述对方「是什么样的人」。

存储决策（锁定）：**独立表** ``ff_persona_facts``（PersonaMixin 人格
作用域 + supersede 语义 + 证据 JSON 列）——事实是结构化数据（类别 /
置信度 / 证据链 / 更正版本），策展记忆的 ``topic`` 自由文本面无法承载
类别轴与证据链，故不复用策展表；supersede / 单 active 不变式沿用策展层
已验证的「可空守卫标记 + 唯一键忽略 NULL」模式（见 ``facts_orm``）。

值域与边界：

    - 类别 / 状态 / 来源为封闭 StrEnum（服务层强制取值，越界在 DTO 边界
      即拒绝）；
    - ``provenance`` 复用记忆域统一 taxonomy（``owner`` / ``agent``）——
      ``untrusted`` 永不入本表（服务层强制拒绝，平台内容不可信不进画像）；
    - 正文上限 :data:`FACT_CONTENT_MAX_LENGTH`（手工 / 更正面）；
      梦境提取面的提案上限更窄（见 ``memory.dreaming_models`` 的
      ``DreamFactProposedItemModel``——提取只产出短事实陈述）；
    - 证据引用 ``episode_id`` 可空（手工事实无情景事件背书——空表示
      「主人直接告知」），引号摘录上限 :data:`FACT_EVIDENCE_QUOTE_MAX_LENGTH`
      （成为 prompt 注入的逐字摘录，永不超预算）。

依赖方向：pydantic / 标准库 + 记忆域值域枚举（DTO 层不触 ORM / 不触服务）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from packages.harness.memory.models import MemoryProvenance

__all__ = [
    "FACT_CONTENT_MAX_LENGTH",
    "FACT_EVIDENCE_QUOTE_MAX_LENGTH",
    "FACT_COMPOSE_MAX_FACTS",
    "FACT_COMPOSE_MAX_CHARS",
    "FACT_MIN_CONFIDENCE",
    "PersonaFactCategory",
    "PersonaFactStatus",
    "PersonaFactSource",
    "FACT_CATEGORY_LABELS",
    "FactEvidenceRefModel",
    "FactCreateDTO",
    "FactCorrectDTO",
    "FactQueryDTO",
    "FactRecordDTO",
    "FactUpsertResultModel",
]

#: 事实正文长度上限（管理面创建 / 更正面——梦境提取面另有 200 字符上限）。
FACT_CONTENT_MAX_LENGTH: int = 1000

#: 证据摘录长度上限（逐字摘录——compose 注入的预算约束）。
FACT_EVIDENCE_QUOTE_MAX_LENGTH: int = 200

#: compose 注入的事实条数上限（现行高置信事实按置信度降序取前 N 条）。
FACT_COMPOSE_MAX_FACTS: int = 12

#: compose 注入的事实总字符上限（含类别标签与行符号——超出即截断）。
FACT_COMPOSE_MAX_CHARS: int = 1500

#: 梦境提取的置信度下限（低于此值的提案丢弃——不落库、计数 dropped）。
FACT_MIN_CONFIDENCE: float = 0.3


class PersonaFactCategory(StrEnum):
    """事实类别值域（ff_persona_facts.category）。

    - ``preference``：偏好（口味 / 作息 / 说话习惯 / 喜恶）；
    - ``biography``：背景（职业 / 经历 / 家庭与社会关系事实）；
    - ``relationship``：关系（对方的称呼 / 相处模式 / 边界约定）；
    - ``context``：近况（近期目标 / 状态变化 / 当下处境）。
    """

    PREFERENCE = "preference"
    BIOGRAPHY = "biography"
    RELATIONSHIP = "relationship"
    CONTEXT = "context"


class PersonaFactStatus(StrEnum):
    """事实状态值域（ff_persona_facts.status）。

    - ``active``：现行事实（唯一 active 由守卫标记 + 唯一键强制）；
    - ``superseded``：被更正版本取代（历史链可回溯——``superseded_by_id``
      指向取代者）；
    - ``rejected``：被主人驳回（不再参与 compose 注入；守卫标记同步清空，
      同身份事实可重新起链）。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class PersonaFactSource(StrEnum):
    """事实来源值域（ff_persona_facts.source）。

    - ``dream``：夜间梦境提取（provenance 恒 ``agent``）；
    - ``manual``：管理面手工录入（provenance 恒 ``owner``）；
    - ``correction``：主人更正（provenance 恒 ``owner``——更正链保留既有
      证据并追加更正说明）。
    """

    DREAM = "dream"
    MANUAL = "manual"
    CORRECTION = "correction"


#: 类别 → 中文标签（compose 注入行与面板展示的单一真源）。
FACT_CATEGORY_LABELS: dict[PersonaFactCategory, str] = {
    PersonaFactCategory.PREFERENCE: "偏好",
    PersonaFactCategory.BIOGRAPHY: "背景",
    PersonaFactCategory.RELATIONSHIP: "关系",
    PersonaFactCategory.CONTEXT: "近况",
}


class FactEvidenceRefModel(BaseModel):
    """事实证据引用（ff_persona_facts.evidence 数组元素 / 管理面读取面）。

    Attributes:
        episode_id: 情景事件主键（None = 手工事实无事件背书——主人直接
            告知的事实）；写路径校验事件存在且属于当前人格，失败按
            「丢弃该证据」处理（不阻断事实写入）。
        quote: 逐字摘录（≤200 字符——梦境提取面由系统从被引用事件正文
            逐字截取，手工面由主人填写；成为 compose 证据展开的展示面）。
        ts_ms: 证据事件时间（epoch 毫秒；手工事实为录入时刻）。
        provenance: 证据来源可信度（owner / agent——untrusted 永不出现在
            本表）。
    """

    episode_id: int | None = Field(
        default=None, ge=1, description="情景事件主键（None = 手工事实）"
    )
    quote: str = Field(
        min_length=1,
        max_length=FACT_EVIDENCE_QUOTE_MAX_LENGTH,
        description="逐字摘录（≤200 字符）",
    )
    ts_ms: int = Field(ge=0, description="证据事件时间（epoch 毫秒）")
    provenance: MemoryProvenance = Field(description="证据来源可信度（owner/agent）")


class FactCreateDTO(BaseModel):
    """事实创建入参（管理面手工录入面）。

    Attributes:
        category: 事实类别（封闭值域）。
        content: 事实陈述（1-1000 字符——服务层做去空白归一后落库）。
        confidence: 置信度（0-1；手工录入缺省 0.8——主人直接告知属高可信）。
        evidence: 证据引用列表（可空——手工事实可无事件背书）。
    """

    category: PersonaFactCategory = Field(description="事实类别")
    content: str = Field(
        min_length=1, max_length=FACT_CONTENT_MAX_LENGTH, description="事实陈述（1-1000 字符）"
    )
    confidence: float = Field(default=0.8, ge=0, le=1, description="置信度（0-1）")
    evidence: list[FactEvidenceRefModel] = Field(
        default_factory=list, description="证据引用列表（可空）"
    )


class FactCorrectDTO(BaseModel):
    """事实更正入参（主人纠正面——旧版本进入 supersede 历史链）。

    Attributes:
        content: 更正后的事实陈述（1-1000 字符）。
        confidence: 更正后的置信度（None = 保留原行置信度）。
    """

    content: str = Field(
        min_length=1,
        max_length=FACT_CONTENT_MAX_LENGTH,
        description="更正后的事实陈述（1-1000 字符）",
    )
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="更正后置信度（None = 保留）"
    )


class FactQueryDTO(BaseModel):
    """事实列表查询入参（游标分页——置信度降序）。

    Attributes:
        category: 类别过滤（None = 全部类别）。
        status: 状态过滤（缺省 active——面板默认只看现行事实）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    category: PersonaFactCategory | None = Field(
        default=None, description="类别过滤（None = 全部）"
    )
    status: PersonaFactStatus = Field(
        default=PersonaFactStatus.ACTIVE, description="状态过滤（缺省 active）"
    )
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class FactRecordDTO(BaseModel):
    """事实记录读取 DTO（ORM 行归一——管理面 / 面板读取面）。

    Attributes:
        id: 事实主键（更正 / 驳回 / 删除锚点）。
        category: 事实类别。
        content: 事实陈述。
        confidence: 置信度（0-1）。
        status: 事实状态（active / superseded / rejected）。
        provenance: 来源可信度（owner / agent）。
        source: 事实来源（dream / manual / correction）。
        evidence: 证据引用列表（逐字摘录 + 事件回链）。
        superseded_by_id: 取代者主键（被更正版本的链链接；active 为 None）。
        version: 更正链版本号（首版 1，更正递增）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="事实主键")
    category: PersonaFactCategory = Field(description="事实类别")
    content: str = Field(description="事实陈述")
    confidence: float = Field(description="置信度（0-1）")
    status: PersonaFactStatus = Field(description="事实状态")
    provenance: MemoryProvenance = Field(description="来源可信度（owner/agent）")
    source: PersonaFactSource = Field(description="事实来源（dream/manual/correction）")
    evidence: list[FactEvidenceRefModel] = Field(default_factory=list, description="证据引用列表")
    superseded_by_id: int | None = Field(default=None, description="取代者主键（active 为 None）")
    version: int = Field(description="更正链版本号（首版 1）")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class FactUpsertResultModel(BaseModel):
    """事实写入结果（服务内部面——梦境统计的 created / merged 判定输入）。

    非 wire 契约（不进入 DTO fixture）：写入面返回记录 + 是否新建，
    调用方据此累加 ``facts_created`` / ``facts_merged`` 统计。

    Attributes:
        record: 写入后的现行事实记录。
        created: 是否新建行（False = 同身份 active 行的元数据刷新——置信度
            取最大值 + 证据合并，不推进版本）。
    """

    record: FactRecordDTO = Field(description="写入后的现行事实记录")
    created: bool = Field(description="是否新建行（False = 元数据刷新）")
