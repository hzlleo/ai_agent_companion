"""memory 域梦境 prompt Unit 文件——梦境整理全部 LLM 提示词文本的唯一存放地。

本工程约定（prompt 与逻辑分离，与 chat 域 prompts.py 同约定）：**所有
进入 LLM 请求的提示词文本必须且只能定义在本模块**（dreaming 服务 /
API 等逻辑文件禁止内联 prompt 字符串）；模板文本变更属于行为变更，
须同步：

    1. 上调对应模板的版本常量（``*_TEMPLATE_VERSION``）；
    2. 更新 byte-lock 测试（tests/unit/test_dreaming_prompts.py）中锁定的
       sha256 摘要——变更因此是「知情的、可审计的」。

模板清单：

    - :data:`MERGE_TEMPLATE` —— 巩固合并模板（候选事件 + 既有主题 →
      严格 JSON 数组提案；**人格中立**——人格卡片不进入本 prompt，
      合并是记忆层面的蒸馏、与人格声线无关，文档化决策）；
    - :data:`FACTS_TEMPLATE` —— 人物事实提取模板（候选事件 → 关于
      主人的稳定事实提案；同样**人格中立**——提取只消费事件正文，
      收获物的归属由编排层写入人格作用域的事实表）；
    - :data:`SKILLS_TEMPLATE` —— 技能挖矿模板（候选事件 + 既有技能清单
      → 「以后怎么做」的程序性技能提案；**人格中立**——提取只看事件
      正文与既有技能名，收获物归属由编排层写入人格作用域的技能表）；
    - :data:`DIARY_TEMPLATE` —— 日记模板（当日事件 → 第一人称中文
      日记；人格声线**仅**经调用方替换 ``{persona_name}`` 渲染变量进入
      ——模板本身不携带任何人格字段）。

渲染函数（纯函数——只做文本装配，不做事件选择 / 打分 / 门控逻辑；
选择与门控逻辑在 dreaming 服务层）：DTO 序列 → 上述模板的最终文本。
覆写语义：``template_override`` 非空时整体替换正典模板（与
chat.prompts 的 ``system_prompt`` 覆写同模式——覆写文本须自带同名
占位符）。
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.dtos import CuratedRecordDTO, EpisodeRecordDTO
from packages.harness.memory.retrieval import flatten_episode_content
from packages.harness.persona.skills_models import SkillRecordDTO

__all__ = [
    "DREAMING_PROMPTS_VERSION",
    "MERGE_TEMPLATE_VERSION",
    "FACTS_TEMPLATE_VERSION",
    "SKILLS_TEMPLATE_VERSION",
    "DIARY_TEMPLATE_VERSION",
    "MERGE_TEMPLATE",
    "FACTS_TEMPLATE",
    "SKILLS_TEMPLATE",
    "DIARY_TEMPLATE",
    "render_merge_prompt",
    "render_facts_prompt",
    "render_skills_prompt",
    "render_diary_prompt",
]

#: 梦境 prompt 套件总版本（任一模板变更时同步上调）。
DREAMING_PROMPTS_VERSION: str = "1.2.0"

# ---------------------------------------------------------------------------
# 版本常量——逐模板独立版本（byte-lock 测试按模板分别锁定摘要）
# ---------------------------------------------------------------------------

#: 巩固合并模板版本。
MERGE_TEMPLATE_VERSION: str = "1.0.0"
#: 人物事实提取模板版本。
FACTS_TEMPLATE_VERSION: str = "1.0.0"
#: 技能挖矿模板版本。
SKILLS_TEMPLATE_VERSION: str = "1.0.0"
#: 日记模板版本。
DIARY_TEMPLATE_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# 模板文本（byte-lock——修改须同步版本常量与测试摘要）
# ---------------------------------------------------------------------------

#: 巩固合并模板（人格中立；占位符 ``{episodes_block}`` / ``{topics_block}``）。
#: 事件「编号」即情景事件主键（episode_id）——提案 merged_from 直接
#: 引用该编号（与编排层「merged_from ⊆ 候选事件 id 集」校验对齐）。
MERGE_TEMPLATE: str = (
    "【记忆整理任务】\n"
    "你正在执行一次夜间的记忆巩固整理。下面给出候选事件（已按编号列出）"
    "和既有的长期记忆主题。请把值得长期记住的内容蒸馏成策展记忆。\n"
    "\n"
    "【候选事件】\n"
    "{episodes_block}\n"
    "\n"
    "【既有记忆主题】\n"
    "{topics_block}\n"
    "\n"
    "【输出要求】\n"
    "严格输出一个 JSON 数组，不要输出数组以外的任何文字或代码块标记。"
    "数组每个元素包含以下字段：\n"
    '- "topic"：主题键（简短中文短语；内容属于既有主题时必须沿用该主题名）；\n'
    '- "kind"：策展类型，只能是 "memory" / "user_note" / "relationship" 之一；\n'
    '- "content"：蒸馏后的记忆正文（简洁的中文陈述句）；\n'
    '- "importance"：重要度，1 到 10 的整数；\n'
    '- "merged_from"：本条记忆来源的候选事件编号列表（数组，元素为上面的事件编号，至少一个）。\n'
    "\n"
    "【整理原则】\n"
    "1. 只保留值得长期记住的内容：偏好、事实、经历、关系变化；琐碎寒暄不保留；\n"
    "2. 多条事件表达同一件事时合并为一条，merged_from 列出全部来源编号；\n"
    "3. 与既有主题相同的内容并入该主题（沿用其 topic），以便更新该记忆；\n"
    "4. 不臆造事件里没有的信息；没有值得保留的内容时输出空数组 []。"
)

#: 人物事实提取模板（人格中立；占位符 ``{episodes_block}``——编号即情景
#: 事件主键，提案 ``evidence_episode_ids`` 直接引用该编号）。
FACTS_TEMPLATE: str = (
    "【人物事实提取任务】\n"
    "你正在执行一次夜间的人物事实提取。下面给出候选事件（已按编号列出）。"
    "请从这些事件里提取关于「对方」（用户 / 主人）的、值得长期记住的稳定事实。\n"
    "\n"
    "【候选事件】\n"
    "{episodes_block}\n"
    "\n"
    "【输出要求】\n"
    "严格输出一个 JSON 数组，不要输出数组以外的任何文字或代码块标记。"
    "数组每个元素包含以下字段：\n"
    '- "category"：事实类别，只能是 "preference"（偏好）/ "biography"（背景）/ '
    '"relationship"（关系）/ "context"（近况）之一；\n'
    '- "content"：事实陈述（简洁的中文陈述句，1-200 字符，以「对方」或「主人」为主语）；\n'
    '- "confidence"：置信度，0 到 1 的小数；\n'
    '- "evidence_episode_ids"：本条事实依据的候选事件编号列表'
    "（数组，至少一个，必须是上面列出的编号）。\n"
    "\n"
    "【提取原则】\n"
    "1. 只提取持久且具体的事实：偏好、背景经历、关系状态、近况变化；"
    "一次性寒暄与临时情绪不提取；\n"
    "2. 不臆造事件里没有的信息；一条事实只陈述一件事；最多输出 10 条；\n"
    "3. 每条事实必须由所引用事件的原文直接支撑（系统会从被引用事件里逐字截取证据摘录）；\n"
    "4. 事件编号之外的引用无效；没有值得保留的事实时输出空数组 []。"
)

#: 技能挖矿模板（人格中立；占位符 ``{episodes_block}`` / ``{skills_block}``
#: ——编号即情景事件主键，提案 ``evidence_episode_ids`` 直接引用该编号；
#: 既有技能清单只列名称与 slug，供模型沿用同一技能名以触发合并）。
SKILLS_TEMPLATE: str = (
    "【技能挖矿任务】\n"
    "你正在执行一次夜间的技能整理。下面给出候选事件（已按编号列出）与既有技能清单。"
    "请从这些事件里归纳出主人希望「以后怎么做」的稳定做法。\n"
    "\n"
    "【候选事件】\n"
    "{episodes_block}\n"
    "\n"
    "【既有技能】\n"
    "{skills_block}\n"
    "\n"
    "【输出要求】\n"
    "严格输出一个 JSON 数组，不要输出数组以外的任何文字或代码块标记。"
    "数组每个元素包含以下字段：\n"
    '- "name"：技能名（简短中文短语，1-120 字符；与既有技能是同一做法时必须沿用该名称）；\n'
    '- "description"：何时使用该技能的触发描述（简洁的中文陈述句，1-500 字符）；\n'
    '- "triggers"：触发短语数组（1-8 条，每条 1-40 字符，写主人可能直接说出口的原话片段）；\n'
    '- "content"：技能正文，markdown 步骤列表（1-4000 字符，用「1. 2. 3.」分步写清楚怎么做）；\n'
    '- "confidence"：置信度，0 到 1 的小数；\n'
    '- "evidence_episode_ids"：本条技能依据的候选事件编号列表（数组，至少一个，'
    "必须是上面列出的编号）。\n"
    "\n"
    "【挖矿原则】\n"
    "1. 只归纳可复用的做事方式：主人反复强调的做法、偏好的回复结构、固定的工作流、"
    "明确的风格约定；一次性的具体事件不挖；\n"
    "2. 技能描述「怎么做」，不重复事实层已有的「对方是谁」；\n"
    "3. 触发短语必须是主人日常会说出的字面短语，不要写抽象类别名；\n"
    "4. 与既有技能是同一做法时沿用其名称（系统按名称合并，不会重复新建）；\n"
    "5. 不臆造事件里没有的要求；最多输出 5 条；没有可挖的技能时输出空数组 []。"
)

#: 日记模板（占位符 ``{persona_name}`` / ``{episodes_block}``——人格声线
#: 仅经调用方替换 persona_name 进入）。
DIARY_TEMPLATE: str = (
    "【日记写作任务】\n"
    "你是 {persona_name}。以下是今天发生的事（按时间顺序）：\n"
    "\n"
    "{episodes_block}\n"
    "\n"
    "请以 {persona_name} 的第一人称口吻写一篇今天的中文日记：几句话即可，"
    "语气自然温暖，像写给自己的私密日记；不要提及「系统」「任务」「记录」"
    "这类字眼；只输出日记正文。"
)

#: 事件 / 主题为空时的占位文本。
_EMPTY_BLOCK_PLACEHOLDER: str = "（无）"


def _episode_text(record: EpisodeRecordDTO) -> str:
    """事件载荷 → prompt 文本行（按 kind 取文本字段，与检索层同一规则）。

    复用 :func:`flatten_episode_content` 的取段规则（单一真源）；载荷
    无文本（全部文本段为空）时返回占位文本——梦境门控不因无文本剔除
    事件（分数轴只看 importance / 时间 / 相关度，文档化决策）。

    Args:
        record: 情景事件记录 DTO。

    Returns:
        载荷文本（无文本载荷返回占位文本）。
    """
    try:
        return flatten_episode_content(record.content)
    except BusinessException:
        return _EMPTY_BLOCK_PLACEHOLDER


def _format_merge_episodes(candidates: Sequence[EpisodeRecordDTO]) -> str:
    """候选事件 → 合并 prompt 的编号事件块（编号 = 事件主键）。

    Args:
        candidates: 过门候选事件（调用方保证次序——门控按分数降序）。

    Returns:
        逐行文本块（``事件 #{id}：[kind=... source=... importance=... ts=...]`` + 载荷文本）。
    """
    if not candidates:
        return _EMPTY_BLOCK_PLACEHOLDER
    lines = [
        f"事件 #{record.id}：[kind={record.kind.value} source={record.source} "
        f"importance={record.importance} ts={record.ts_ms}]\n{_episode_text(record)}"
        for record in candidates
    ]
    return "\n".join(lines)


def _format_topics(topics: Sequence[CuratedRecordDTO]) -> str:
    """既有 active 主题 → 合并 prompt 的主题块。

    Args:
        topics: 现行 active 策展记忆（调用方保证已按 active 过滤）。

    Returns:
        逐行文本块（``- 主题名（kind=..., importance=...）：正文``）。
    """
    if not topics:
        return _EMPTY_BLOCK_PLACEHOLDER
    return "\n".join(
        f"- {topic.topic}（kind={topic.kind.value} importance={topic.importance}）：{topic.content}"
        for topic in topics
    )


def _format_diary_episodes(episodes: Sequence[EpisodeRecordDTO]) -> str:
    """当日事件 → 日记 prompt 的事件块（时间序——调用方保证）。

    Args:
        episodes: 当日可信事件（untrusted 已由调用方过滤——硬门）。

    Returns:
        逐行文本块（``- [kind=... ts=...]`` + 载荷文本）。
    """
    if not episodes:
        return _EMPTY_BLOCK_PLACEHOLDER
    return "\n".join(
        f"- [kind={record.kind.value} ts={record.ts_ms}] {_episode_text(record)}"
        for record in episodes
    )


def _format_skills_block(skills: Sequence[SkillRecordDTO]) -> str:
    """既有启用技能 → 技能挖矿 prompt 的清单块。

    只列名称与 slug（模型需要认出「这是同一技能」以触发合并；正文与
    触发短语不进入 prompt——控制 prompt 体积并避免模型照抄旧内容）。

    Args:
        skills: 启用态技能记录（调用方保证已按 active 过滤、名称升序）。

    Returns:
        逐行文本块（``- 名称（slug: ...）``；无技能时为占位文本）。
    """
    if not skills:
        return _EMPTY_BLOCK_PLACEHOLDER
    return "\n".join(f"- {skill.name}（slug: {skill.slug}）" for skill in skills)


def render_merge_prompt(
    candidates: Sequence[EpisodeRecordDTO],
    active_topics: Sequence[CuratedRecordDTO],
    *,
    template_override: str | None = None,
) -> str:
    """渲染巩固合并 prompt（人格中立——输入只有事件与主题，无人格字段）。

    Args:
        candidates: 过门候选事件（分数降序——门控输出）。
        active_topics: 既有 active 策展记忆。
        template_override: 覆写模板（None = 正典模板；非空时整体替换——
            须自带 ``{episodes_block}`` / ``{topics_block}`` 同名占位符）。

    Returns:
        合并 prompt 最终文本。
    """
    template = template_override if template_override is not None else MERGE_TEMPLATE
    return template.format(
        episodes_block=_format_merge_episodes(candidates),
        topics_block=_format_topics(active_topics),
    )


def render_facts_prompt(
    candidates: Sequence[EpisodeRecordDTO],
    *,
    template_override: str | None = None,
) -> str:
    """渲染人物事实提取 prompt（人格中立——输入只有候选事件，无人格字段）。

    Args:
        candidates: 过门候选事件（分数降序——门控输出；编号即事件主键，
            提案 ``evidence_episode_ids`` 引用该编号）。
        template_override: 覆写模板（None = 正典模板；非空时整体替换——
            须自带 ``{episodes_block}`` 同名占位符）。

    Returns:
        事实提取 prompt 最终文本。
    """
    template = template_override if template_override is not None else FACTS_TEMPLATE
    return template.format(episodes_block=_format_merge_episodes(candidates))


def render_skills_prompt(
    candidates: Sequence[EpisodeRecordDTO],
    active_skills: Sequence[SkillRecordDTO],
    *,
    template_override: str | None = None,
) -> str:
    """渲染技能挖矿 prompt（人格中立——输入只有候选事件与既有技能名）。

    Args:
        candidates: 过门候选事件（分数降序——门控输出；编号即事件主键，
            提案 ``evidence_episode_ids`` 引用该编号）。
        active_skills: 启用态技能清单（只有名称 / slug 进入模板——供模型
            沿用同名以触发合并；正文不进入 prompt）。
        template_override: 覆写模板（None = 正典模板；非空时整体替换——
            须自带 ``{episodes_block}`` / ``{skills_block}`` 同名占位符）。

    Returns:
        技能挖矿 prompt 最终文本。
    """
    template = template_override if template_override is not None else SKILLS_TEMPLATE
    return template.format(
        episodes_block=_format_merge_episodes(candidates),
        skills_block=_format_skills_block(active_skills),
    )


def render_diary_prompt(
    persona_name: str,
    day_episodes: Sequence[EpisodeRecordDTO],
    *,
    template_override: str | None = None,
) -> str:
    """渲染日记 prompt（人格声线仅经 ``persona_name`` 渲染变量进入）。

    Args:
        persona_name: 人格展示名（调用方解析目标人格后替换——模板本身
            不携带任何人格字段，无 persona 泄漏面）。
        day_episodes: 当日可信事件（时间升序；untrusted 已过滤）。
        template_override: 覆写模板（None = 正典模板；非空时整体替换——
            须自带 ``{persona_name}`` / ``{episodes_block}`` 同名占位符）。

    Returns:
        日记 prompt 最终文本。

    Raises:
        BusinessException: persona_name 为空（``VALIDATION_ERROR``——空
            名会渲染出无声线指向的日记指令，属调用方装配错误）。
    """
    if not persona_name.strip():
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="日记 prompt 的人格名不可为空",
            details={"persona_name": persona_name},
        )
    template = template_override if template_override is not None else DIARY_TEMPLATE
    return template.format(
        persona_name=persona_name,
        episodes_block=_format_diary_episodes(day_episodes),
    )
