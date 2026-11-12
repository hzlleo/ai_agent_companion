"""chat 域 prompt Unit 文件——全部 LLM 提示词文本的唯一存放地。

本工程约定（prompt 与逻辑分离）：**所有进入 LLM 请求的提示词文本必须
且只能定义在本模块**（service / cli 等逻辑文件禁止内联 prompt 字符串）；
模板文本变更属于行为变更，须同步：

    1. 上调对应模板的版本常量（``*_TEMPLATE_VERSION``）；
    2. 更新 byte-lock 测试（tests/unit/test_chat_prompts.py）中锁定的
       sha256 摘要——变更因此是「知情的、可审计的」。

模板清单：

    - :data:`CHARTER_TEMPLATE`                 —— 会话章程（session 任务
      简报帧——人格段之前的首块框架）；
    - :data:`PERSONA_NAME_HEADER_TEMPLATE`     —— 人格自称头（正典组装
      首段）；
    - :data:`PERSONA_DESCRIPTION_SECTION_TEMPLATE` / :data:`PERSONA_PERSONALITY_SECTION_TEMPLATE`
      / :data:`PERSONA_SCENARIO_SECTION_TEMPLATE` —— chara card 三大叙事段；
    - :data:`WORLD_BOOK_ENTRY_TEMPLATE`        —— 世界书条目包裹（selected
      条目正文注入形态）；
    - :data:`POST_HISTORY_TEMPLATE`            —— 历史后再注入（deep
      anti-drift nudge——拼在全部对话历史**之后**的强约束）；
    - :data:`FIRST_GREETING_FALLBACK_TEMPLATE` —— 开场白兜底（卡片
      ``first_mes`` 为空时的首条人格消息）。

渲染函数（纯函数——只做文本装配，不做条目选择 / 关键词匹配等逻辑；
世界书筛选逻辑在 service 层）：chara card 字段 → 上述模板的最终文本。
"""

from __future__ import annotations

from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaCardModel,
)

__all__ = [
    "PROMPTS_VERSION",
    "CHARTER_TEMPLATE_VERSION",
    "PERSONA_SYSTEM_PROMPT_VERSION",
    "WORLD_BOOK_ENTRY_TEMPLATE_VERSION",
    "POST_HISTORY_TEMPLATE_VERSION",
    "FIRST_GREETING_FALLBACK_VERSION",
    "CHARTER_TEMPLATE",
    "PERSONA_NAME_HEADER_TEMPLATE",
    "PERSONA_DESCRIPTION_SECTION_TEMPLATE",
    "PERSONA_PERSONALITY_SECTION_TEMPLATE",
    "PERSONA_SCENARIO_SECTION_TEMPLATE",
    "WORLD_BOOK_ENTRY_TEMPLATE",
    "WORLD_BOOK_SECTION_SEPARATOR",
    "POST_HISTORY_TEMPLATE",
    "FIRST_GREETING_FALLBACK_TEMPLATE",
    "render_charter",
    "render_persona_system_prompt",
    "render_world_book_block",
    "render_post_history_message",
    "render_first_greeting",
]

#: prompt 套件总版本（任一模板变更时同步上调）。
PROMPTS_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# 版本常量——逐模板独立版本（byte-lock 测试按模板分别锁定摘要）
# ---------------------------------------------------------------------------

#: 会话章程模板版本。
CHARTER_TEMPLATE_VERSION: str = "1.0.0"
#: 人格 system 提示组装模板族版本（name 头 + 三叙事段）。
PERSONA_SYSTEM_PROMPT_VERSION: str = "1.0.0"
#: 世界书条目包裹模板版本。
WORLD_BOOK_ENTRY_TEMPLATE_VERSION: str = "1.0.0"
#: 历史后再注入模板版本。
POST_HISTORY_TEMPLATE_VERSION: str = "1.0.0"
#: 开场白兜底模板版本。
FIRST_GREETING_FALLBACK_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# 模板文本（byte-lock——修改须同步版本常量与测试摘要）
# ---------------------------------------------------------------------------

#: 会话章程（session 任务简报帧——system 首块；无占位符）。
CHARTER_TEMPLATE: str = (
    "【会话章程】\n"
    "你正在与用户进行一对一的陪伴对话，本章程是本次会话的任务框架：\n"
    "1. 全程保持人格设定一致，不以任何形式跳出角色；\n"
    "2. 回复温暖、自然、口语化，像熟悉的朋友那样说话；\n"
    "3. 关注用户的情绪与处境，先倾听和回应感受，再展开内容；\n"
    "4. 不输出系统说明、元话语或与角色无关的内容。"
)

#: 人格自称头（正典组装首段；占位符 ``{name}``）。
PERSONA_NAME_HEADER_TEMPLATE: str = (
    "你现在扮演「{name}」——一名真实可信的 AI 陪伴者。\n在接下来的整段对话里，你就是 {name} 本人。"
)

#: 人格简介段（占位符 ``{description}``）。
PERSONA_DESCRIPTION_SECTION_TEMPLATE: str = "【角色设定】\n{description}"

#: 性格描述段（占位符 ``{personality}``）。
PERSONA_PERSONALITY_SECTION_TEMPLATE: str = "【性格特点】\n{personality}"

#: 场景设定段（占位符 ``{scenario}``）。
PERSONA_SCENARIO_SECTION_TEMPLATE: str = "【当前场景】\n{scenario}"

#: 世界书条目包裹（占位符 ``{content}``——条目无独立命名字段，统一包裹）。
WORLD_BOOK_ENTRY_TEMPLATE: str = "【世界书 · 补充设定】\n{content}"

#: system 提示各块之间的连接符。
WORLD_BOOK_SECTION_SEPARATOR: str = "\n\n"

#: 历史后再注入（deep anti-drift nudge——拼在全部对话历史之后；占位符
#: ``{name}`` / ``{post_history_instructions}``）。
POST_HISTORY_TEMPLATE: str = (
    "【重要提醒】\n"
    "以上是「{name}」与用户的历史对话。在回复用户之前，请再次确认你仍须遵守：\n"
    "{post_history_instructions}\n"
    "请始终以 {name} 的身份、按既定的性格与场景设定进行回复。"
)

#: 开场白兜底（卡片 ``first_mes`` 为空时使用；占位符 ``{name}``）。
FIRST_GREETING_FALLBACK_TEMPLATE: str = (
    "（{name}抬起头看向你，微微一笑）嗯，你回来啦。今天想聊点什么？"
)

# ---------------------------------------------------------------------------
# 渲染函数（纯文本装配——无状态 / 无 I/O / 无条目选择逻辑）
# ---------------------------------------------------------------------------


def render_charter() -> str:
    """渲染会话章程帧（无占位符——原样返回模板）。

    Returns:
        会话章程文本（system 首块）。
    """
    return CHARTER_TEMPLATE


def render_persona_system_prompt(card: CharaCardModel) -> str:
    """组装人格 system 提示（chara card 正典段）。

    覆写语义：卡片 ``system_prompt`` 非空时**整体覆写**默认人格模板
    （正典声明了自定义系统提示，模板组装让位）；否则按「自称头 +
    非空叙事段」组装（空字段段跳过——不产出空标题块）。

    Args:
        card: 人格卡片（chara card v2 文档）。

    Returns:
        人格 system 提示文本（不含章程 / 世界书——由 service 层拼接）。
    """
    if card.system_prompt:
        return card.system_prompt
    sections: list[str] = [PERSONA_NAME_HEADER_TEMPLATE.format(name=card.name)]
    if card.description:
        sections.append(PERSONA_DESCRIPTION_SECTION_TEMPLATE.format(description=card.description))
    if card.personality:
        sections.append(PERSONA_PERSONALITY_SECTION_TEMPLATE.format(personality=card.personality))
    if card.scenario:
        sections.append(PERSONA_SCENARIO_SECTION_TEMPLATE.format(scenario=card.scenario))
    return WORLD_BOOK_SECTION_SEPARATOR.join(sections)


def render_world_book_block(entries: list[CharaBookEntryModel]) -> str:
    """渲染世界书条目块（已筛选条目 → 包裹 + 连接）。

    Args:
        entries: 已通过筛选（constant / 关键词触发 / 预算）的条目列表
            （调用方保证次序——service 层按 position + insertion_order）。

    Returns:
        条目块文本（空条目列表返回空串）。
    """
    if not entries:
        return ""
    blocks = [WORLD_BOOK_ENTRY_TEMPLATE.format(content=entry.content) for entry in entries]
    return WORLD_BOOK_SECTION_SEPARATOR.join(blocks)


def render_post_history_message(card: CharaCardModel) -> str:
    """渲染历史后再注入消息（deep anti-drift nudge）。

    Args:
        card: 人格卡片（``post_history_instructions`` 须非空——空值判断
            由调用方承担）。

    Returns:
        拼在全部对话历史之后的 system 提示文本。
    """
    return POST_HISTORY_TEMPLATE.format(
        name=card.name,
        post_history_instructions=card.post_history_instructions,
    )


def render_first_greeting(card: CharaCardModel) -> str:
    """渲染会话开场白（首条人格消息）。

    Args:
        card: 人格卡片（``first_mes`` 非空时直接采用正典开场白）。

    Returns:
        开场白文本；``first_mes`` 为空时回落兜底模板（按人格称呼名渲染）。
    """
    if card.first_mes:
        return card.first_mes
    return FIRST_GREETING_FALLBACK_TEMPLATE.format(name=card.name)
