"""persona 域技能 Model Classes——程序性技能 / 触发短语 / compose 注入面契约。

技能（skill）是人格对**主人做事方式**的程序性记忆：反复出现的偏好动作
（「先给结论再解释」）、固定工作流（「每次写周报先列三件事」）、风格
约定（「回消息短句、不用书面语」）。与人物事实的分界：事实描述对方
「是什么样的人」，技能描述「事情该怎么做」——前者是画像，后者是操作
指令，故独立成表（``ff_persona_skills``，见 ``skills_orm``）。

值域与边界：

    - 状态 / 来源为封闭 StrEnum（服务层强制取值，越界在 DTO 边界即
      拒绝）；``source=mined`` 为夜间梦境挖矿产物的来源标记，mined 行
      与手工行一样可编辑 / 停用 / 删除（主人始终拥有最终控制权）；
    - 名称 / 描述 / 触发短语 / 正文的长度与条数上限由本模块常量锁定：
      触发短语是**字面短语**（不做模糊推理），越贴近主人口语命中越可靠；
    - ``SKILL_MINED_CONTENT_MAX_LENGTH`` 是梦境提取面的更窄上限（模型
      产出步骤正文须精炼），手工面可用满 ``SKILL_CONTENT_MAX_LENGTH``；
    - compose 注入帽（:data:`SKILL_COMPOSE_MAX_SKILLS` /
      :data:`SKILL_COMPOSE_MAX_CHARS`）由聊天域渲染函数施加——最多两条
      技能、总字符 2000（含标题与分隔），防止技能正文挤占对话上下文；
    - 置信度下限 :data:`SKILL_MIN_CONFIDENCE` 只作用于梦境提取提案
      （低于下限丢弃），手工录入可自由给值。

依赖方向：pydantic / 标准库（DTO 层不触 ORM / 不触服务）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "SKILL_NAME_MAX_LENGTH",
    "SKILL_SLUG_MAX_LENGTH",
    "SKILL_DESCRIPTION_MAX_LENGTH",
    "SKILL_TRIGGER_MIN_LENGTH",
    "SKILL_TRIGGER_MAX_LENGTH",
    "SKILL_TRIGGERS_MAX",
    "SKILL_CONTENT_MAX_LENGTH",
    "SKILL_MINED_CONTENT_MAX_LENGTH",
    "SKILL_MIN_CONFIDENCE",
    "SKILL_MANUAL_DEFAULT_CONFIDENCE",
    "SKILL_PROPOSAL_CAP",
    "SKILL_COMPOSE_MAX_SKILLS",
    "SKILL_COMPOSE_MAX_CHARS",
    "SkillStatus",
    "SkillSource",
    "SkillTriggerModel",
    "SkillCreateModel",
    "SkillUpdateModel",
    "SkillQueryDTO",
    "SkillRecordDTO",
    "SkillUpsertResultModel",
]

#: 技能名长度上限（管理面创建 / 编辑 / 梦境提案共同边界）。
SKILL_NAME_MAX_LENGTH: int = 120

#: slug 长度上限（name 归一化派生后的落库形态——超出按此截断）。
SKILL_SLUG_MAX_LENGTH: int = 80

#: 触发描述长度上限（何时使用该技能）。
SKILL_DESCRIPTION_MAX_LENGTH: int = 500

#: 单条触发短语长度下限（空短语无匹配意义）。
SKILL_TRIGGER_MIN_LENGTH: int = 1

#: 单条触发短语长度上限。
SKILL_TRIGGER_MAX_LENGTH: int = 40

#: 单技能触发短语条数上限。
SKILL_TRIGGERS_MAX: int = 8

#: 手工面技能正文长度上限（markdown 步骤正文）。
SKILL_CONTENT_MAX_LENGTH: int = 8000

#: 梦境提取面的正文长度上限（模型产出须精炼——更窄的提案边界）。
SKILL_MINED_CONTENT_MAX_LENGTH: int = 4000

#: 梦境提取的置信度下限（低于此值的提案丢弃——不落库、计数 dropped）。
SKILL_MIN_CONFIDENCE: float = 0.4

#: 手工录入的缺省置信度（主人直接写下的指令按较高可信处理）。
SKILL_MANUAL_DEFAULT_CONFIDENCE: float = 0.6

#: 梦境技能单次提取的条数上限（提案超出部分丢弃并计入 skills_dropped）。
SKILL_PROPOSAL_CAP: int = 5

#: compose 注入的技能条数上限（命中技能按使用次数降序取前 N 条）。
SKILL_COMPOSE_MAX_SKILLS: int = 2

#: compose 注入的技能总字符上限（含标题与分隔——超出即截断）。
SKILL_COMPOSE_MAX_CHARS: int = 2000

#: 单条触发短语的类型化形态（1-40 字符——DTO 边界即拒绝越界）。
SkillTriggerModel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SKILL_TRIGGER_MAX_LENGTH),
]


class SkillStatus(StrEnum):
    """技能状态值域（ff_persona_skills.status）。

    - ``active``：启用（参与 compose 匹配与注入）；
    - ``disabled``：停用（不参与匹配，面板仍可查看 / 编辑 / 重新启用）。
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class SkillSource(StrEnum):
    """技能来源值域（ff_persona_skills.source）。

    - ``manual``：管理面手工录入（不缺省进入梦境提案）；
    - ``mined``：夜间梦境从历史事件中挖矿产出（与手工行同等可编辑）。
    """

    MANUAL = "manual"
    MINED = "mined"


class SkillCreateModel(BaseModel):
    """技能创建入参（管理面手工录入面）。

    Attributes:
        name: 技能名（1-120 字符——slug 由服务层按名称归一化派生）。
        description: 何时使用的触发描述（1-500 字符）。
        triggers: 触发短语（1-8 条、每条 1-40 字符——主人可能说出的
            字面短语）。
        content: markdown 步骤正文（1-8000 字符）。
        confidence: 置信度（0-1；缺省 0.6——手工录入属较高可信）。
    """

    name: str = Field(
        min_length=1, max_length=SKILL_NAME_MAX_LENGTH, description="技能名（1-120 字符）"
    )
    description: str = Field(
        min_length=1,
        max_length=SKILL_DESCRIPTION_MAX_LENGTH,
        description="何时使用的触发描述（1-500 字符）",
    )
    triggers: list[SkillTriggerModel] = Field(
        min_length=1,
        max_length=SKILL_TRIGGERS_MAX,
        description="触发短语（1-8 条、每条 1-40 字符）",
    )
    content: str = Field(
        min_length=1,
        max_length=SKILL_CONTENT_MAX_LENGTH,
        description="markdown 步骤正文（1-8000 字符）",
    )
    confidence: float = Field(
        default=SKILL_MANUAL_DEFAULT_CONFIDENCE, ge=0, le=1, description="置信度（0-1）"
    )


class SkillUpdateModel(BaseModel):
    """技能编辑入参（原地更新——字段全部可选，版本递增）。

    编辑不产生历史行（技能是操作指令而非信念断言，理由见 ``skills_orm``
    模块文档）；``name`` 变更会重新派生 slug 并按唯一键校验冲突。

    Attributes:
        name: 新技能名（None = 保留）。
        description: 新触发描述（None = 保留）。
        triggers: 新触发短语集（None = 保留；提供时整集替换）。
        content: 新步骤正文（None = 保留）。
        confidence: 新置信度（None = 保留）。
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=SKILL_NAME_MAX_LENGTH,
        description="新技能名（None = 保留）",
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=SKILL_DESCRIPTION_MAX_LENGTH,
        description="新触发描述（None = 保留）",
    )
    triggers: list[SkillTriggerModel] | None = Field(
        default=None,
        min_length=1,
        max_length=SKILL_TRIGGERS_MAX,
        description="新触发短语集（None = 保留；提供时整集替换）",
    )
    content: str | None = Field(
        default=None,
        min_length=1,
        max_length=SKILL_CONTENT_MAX_LENGTH,
        description="新步骤正文（None = 保留）",
    )
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="新置信度（None = 保留）"
    )


class SkillQueryDTO(BaseModel):
    """技能列表查询入参（游标分页——使用次数降序）。

    Attributes:
        status: 状态过滤（None = 全部状态）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    status: SkillStatus | None = Field(default=None, description="状态过滤（None = 全部）")
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class SkillRecordDTO(BaseModel):
    """技能记录读取 DTO（ORM 行归一——管理面 / 面板 / compose 读取面）。

    Attributes:
        id: 技能主键（编辑 / 停用 / 删除锚点）。
        name: 技能名。
        slug: 归一化 kebab 标识（同人格唯一）。
        description: 何时使用的触发描述。
        triggers: 触发短语列表。
        content: markdown 步骤正文。
        status: 状态（active / disabled）。
        source: 来源（manual / mined）。
        confidence: 置信度（0-1）。
        use_count: compose 命中次数（写读强化轴）。
        last_used_ms: 最近一次命中时间（epoch 毫秒；未命中为 None）。
        version: 原地编辑版本号（首版 1）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="技能主键")
    name: str = Field(description="技能名")
    slug: str = Field(description="归一化 kebab 标识（同人格唯一）")
    description: str = Field(description="何时使用的触发描述")
    triggers: list[str] = Field(default_factory=list, description="触发短语列表")
    content: str = Field(description="markdown 步骤正文")
    status: SkillStatus = Field(description="状态（active/disabled）")
    source: SkillSource = Field(description="来源（manual/mined）")
    confidence: float = Field(description="置信度（0-1）")
    use_count: int = Field(description="compose 命中次数")
    last_used_ms: int | None = Field(default=None, description="最近一次命中时间（epoch 毫秒）")
    version: int = Field(description="原地编辑版本号（首版 1）")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class SkillUpsertResultModel(BaseModel):
    """技能写入结果（服务内部面——梦境统计的 created / merged 判定输入）。

    非 wire 契约（不进入 DTO fixture）：写入面返回记录 + 新建 / 合并
    判定，调用方据此累加 ``skills_created`` / ``skills_merged`` 统计。

    Attributes:
        record: 写入后的技能记录。
        created: 是否新建行（True = 新技能）。
        merged: 是否并入既有行（True = 同 slug 的元数据刷新——置信度取
            最大值 + 触发短语并集，不推进版本）。
    """

    record: SkillRecordDTO = Field(description="写入后的技能记录")
    created: bool = Field(description="是否新建行")
    merged: bool = Field(description="是否并入既有行（元数据刷新）")
