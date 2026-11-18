"""persona 域核心记忆 Model Classes——自编辑核心记忆块 / 修订链 / 编辑契约。

核心记忆（core memory）是人格的**可自编辑长期自我模型**：固定四个块的
短文本（自我认知 / 关于对方 / 我们的故事 / 备忘），由人格在对话中经
``edit_core_memory`` 工具主动维护，也允许主人从管理面直接改写或整块
锁定。与人物事实（``facts_models``）的分界：

    - 事实是**结构化断言**（类别 / 置信度 / 证据链 / 更正版本）——由
      系统从情景事件提取与纠错，条目可多可少；
    - 核心记忆是**连续自我叙述**（每个块一篇短文本）——人格自己随时改写
      「我是谁 / 对方是谁 / 我们之间发生过什么」，块集合固定、每块至多
      一篇，修订历史只追加不留原地覆盖。

值域与边界（锁定）：

    - 块键是封闭四元组（:class:`CoreBlockKey`）——不存在第五个块，也不
      允许按对话动态造块；空块不进入 compose 注入；
    - 内容上限 :data:`CORE_MEMORY_CONTENT_MAX_LENGTH`（2-2000 字符）：
      主人可清空（0 字符），人格写入须非空（1-2000）；实际阈值可由服务
      构造注入收窄（见 ``persona.core_memory`` 构造旋钮）；
    - ``locked`` 是**主人专属写闸**：锁定后人格的工具调用被结构性拒绝，
      只有管理面的主人编辑能改写该块（解锁同样只能由主人做）；
    - 修订来源双轴：``provenance``（谁改的：owner / agent）与 ``source``
      （从哪改的：admin 管理面 / chat 对话）——两轴组合在服务层强制配对
      （admin 恒 owner、chat 恒 agent）；
    - compose 注入帽 :data:`CORE_MEMORY_COMPOSE_MAX_CHARS` 由聊天域渲染
      函数施加（块标题 + 正文 + 分隔合计，超出即截断停止——低优先级块
      不进注入）。

依赖方向：pydantic / 标准库（DTO 层不触 ORM / 不触服务）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CORE_MEMORY_CONTENT_MAX_LENGTH",
    "CORE_MEMORY_COMPOSE_MAX_CHARS",
    "CORE_MEMORY_REVISIONS_PAGE_LIMIT",
    "CORE_MEMORY_TOOL_NAME",
    "CORE_MEMORY_LOCKED_SUFFIX",
    "CORE_BLOCK_ORDER",
    "CORE_BLOCK_LABELS",
    "CoreBlockKey",
    "CoreMemoryActor",
    "CoreMemorySource",
    "AgentEditMode",
    "CoreMemoryBlockDTO",
    "CoreMemoryRevDTO",
    "OwnerEditModel",
    "AgentEditModel",
    "AgentEditResultModel",
]

#: 核心记忆块内容上限（管理面与工具写入共同边界；服务构造可收窄）。
CORE_MEMORY_CONTENT_MAX_LENGTH: int = 2000

#: compose 注入的总字符上限（块标题 + 正文 + 分隔——超出即截断停止）。
CORE_MEMORY_COMPOSE_MAX_CHARS: int = 2400

#: 修订列表的缺省返回条数（最新在前；API 上限同值放宽见路由层）。
CORE_MEMORY_REVISIONS_PAGE_LIMIT: int = 20

#: 自编辑核心记忆的工具名（聊天工具循环的 LLM 工具面标识）。
CORE_MEMORY_TOOL_NAME: str = "edit_core_memory"

#: 锁定块的展示后缀（compose 注入块标题与面板标识共用）。
CORE_MEMORY_LOCKED_SUFFIX: str = "（锁定）"


class CoreBlockKey(StrEnum):
    """核心记忆块键值域（ff_core_memory_blocks.block_key）。

    - ``self_identity``：自我认知（人格对自己身份 / 性格 / 处境的稳定理解）；
    - ``owner_profile``：关于对方（对主人的稳定认知——称呼 / 习惯 / 边界）；
    - ``relationship_story``：我们的故事（共同经历与关系里程碑）；
    - ``open_notes``：备忘（尚未归入前三块的长期事项）。
    """

    SELF_IDENTITY = "self_identity"
    OWNER_PROFILE = "owner_profile"
    RELATIONSHIP_STORY = "relationship_story"
    OPEN_NOTES = "open_notes"


#: 块键的固定展示序（懒建种子顺序 / 列表顺序 / compose 注入顺序）。
CORE_BLOCK_ORDER: tuple[CoreBlockKey, ...] = (
    CoreBlockKey.SELF_IDENTITY,
    CoreBlockKey.OWNER_PROFILE,
    CoreBlockKey.RELATIONSHIP_STORY,
    CoreBlockKey.OPEN_NOTES,
)

#: 块键 → 中文名称（compose 块标题 / 工具反馈 / 面板展示的单一真源）。
CORE_BLOCK_LABELS: dict[CoreBlockKey, str] = {
    CoreBlockKey.SELF_IDENTITY: "自我认知",
    CoreBlockKey.OWNER_PROFILE: "关于对方",
    CoreBlockKey.RELATIONSHIP_STORY: "我们的故事",
    CoreBlockKey.OPEN_NOTES: "备忘",
}


class CoreMemoryActor(StrEnum):
    """核心记忆的写入者值域（块的 ``updated_by`` 与修订的 ``provenance``）。

    - ``owner``：主人（管理面编辑——含锁定开关）；
    - ``agent``：人格自身（对话内 ``edit_core_memory`` 工具调用）。

    修订来源 ``source`` 与之强制配对（admin ↔ owner、chat ↔ agent），
    由服务层在写入前校验。
    """

    OWNER = "owner"
    AGENT = "agent"


class CoreMemorySource(StrEnum):
    """核心记忆修订的来源值域（ff_core_memory_revisions.source）。

    - ``admin``：管理面 API（主人编辑）；
    - ``chat``：对话内工具调用（人格自编辑）。
    """

    ADMIN = "admin"
    CHAT = "chat"


class AgentEditMode(StrEnum):
    """人格自编辑的写入模式。

    - ``replace``：整块替换（正文即新全文）;
    - ``append``：在既有正文末尾追加一段（既有非空时以换行分隔）。
    """

    REPLACE = "replace"
    APPEND = "append"


class CoreMemoryBlockDTO(BaseModel):
    """核心记忆块读取 DTO（ORM 行归一——管理面 / 面板 / compose 读取面）。

    Attributes:
        id: 块主键（修订列表的归属锚点）。
        block: 块键（封闭四元组）。
        content: 块正文（0-2000 字符；空块不进入 compose 注入）。
        locked: 是否锁定（True = 仅主人可改写）。
        version: 编辑版本号（首版 1，任何内容 / 锁定状态变更递增）。
        updated_by: 最近一次写入者（owner / agent）。
        last_edited_session_id: 最近一次对话内编辑的会话主键（管理面编辑
            为 None）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="块主键")
    block: CoreBlockKey = Field(description="块键（封闭四元组）")
    content: str = Field(description="块正文（0-2000 字符；空块不注入）")
    locked: bool = Field(description="是否锁定（True = 仅主人可改写）")
    version: int = Field(description="编辑版本号（首版 1）")
    updated_by: CoreMemoryActor = Field(description="最近一次写入者（owner/agent）")
    last_edited_session_id: int | None = Field(
        default=None, description="最近一次对话内编辑的会话主键（管理面为 None）"
    )
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class CoreMemoryRevDTO(BaseModel):
    """核心记忆修订记录 DTO（append-only 历史——面板抽屉读取面）。

    Attributes:
        id: 修订主键（倒序游标——越大越新）。
        block: 修订所属块键。
        version: 修订产生时的块版本号（与块行当时版本一致）。
        content: 修订后的块全文（逐字留存——历史不可变）。
        provenance: 写入者（owner / agent）。
        source: 写入来源（admin / chat）。
        session_id: 对话内编辑的会话主键（管理面修订为 None）。
        ts_ms: 修订时刻（epoch 毫秒——倒序排序的第二键）。
        created_at: 落库时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="修订主键")
    block: CoreBlockKey = Field(description="修订所属块键")
    version: int = Field(description="修订产生时的块版本号")
    content: str = Field(description="修订后的块全文")
    provenance: CoreMemoryActor = Field(description="写入者（owner/agent）")
    source: CoreMemorySource = Field(description="写入来源（admin/chat）")
    session_id: int | None = Field(default=None, description="对话内编辑的会话主键（管理面为 None）")
    ts_ms: int = Field(description="修订时刻（epoch 毫秒）")
    created_at: int = Field(description="落库时间（epoch 毫秒）")


class OwnerEditModel(BaseModel):
    """主人编辑入参（管理面——可清空正文 / 可切换锁定）。

    Attributes:
        content: 新块正文（0-2000 字符——空串表示清空该块）。
        locked: 锁定开关（None = 保持不变；True = 仅主人可改写；False =
            解锁，人格可再次自编辑）。
    """

    model_config = {"extra": "forbid"}

    content: str = Field(
        max_length=CORE_MEMORY_CONTENT_MAX_LENGTH,
        description="新块正文（0-2000 字符；空串 = 清空）",
    )
    locked: bool | None = Field(default=None, description="锁定开关（None = 保持不变）")


class AgentEditModel(BaseModel):
    """人格自编辑入参（``edit_core_memory`` 工具参数面）。

    Attributes:
        block: 目标块键（封闭四元组——工具 schema 的 enum 与之一致）。
        content: 写入正文（1-2000 字符——replace 为全文、append 为追加段）。
        mode: 写入模式（replace 整块替换 / append 末尾追加，缺省 replace）。
    """

    model_config = {"extra": "forbid"}

    block: CoreBlockKey = Field(description="目标块键（封闭四元组）")
    content: str = Field(
        min_length=1,
        max_length=CORE_MEMORY_CONTENT_MAX_LENGTH,
        description="写入正文（1-2000 字符）",
    )
    mode: AgentEditMode = Field(default=AgentEditMode.REPLACE, description="写入模式（replace/append）")


class AgentEditResultModel(BaseModel):
    """人格自编辑结果（工具反馈面——恒不抛异常的 LLM 侧终态）。

    Attributes:
        ok: 是否写入成功（False = 已锁定 / 超出上限——LLM 可换块或缩短重试）。
        message: 中文反馈（成功含结果长度；失败含原因与当前长度）。
        length: 写入后的正文长度（失败时为当前正文长度——供模型判断余量）。
    """

    ok: bool = Field(description="是否写入成功")
    message: str = Field(description="中文反馈（成功含结果长度；失败含原因）")
    length: int = Field(ge=0, description="写入后（失败时为当前）正文长度")
