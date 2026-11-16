"""harness/multimodal prompt Unit 文件——look_at 链路全部 LLM 提示词文本的
唯一存放地。

本工程约定（prompt 与逻辑分离，与 duty/duty_prompts.py 同约定）：**所有进入
LLM 请求的提示词文本必须且只能定义在本模块**（look_at 服务等逻辑文件禁止
内联 prompt 字符串）；模板文本变更属于行为变更，须同步：

    1. 上调对应模板的版本常量（``*_VERSION``）；
    2. 更新 byte-lock 测试（tests/unit/test_mm_prompts.py）中锁定的
       sha256 摘要——变更因此是「知情的、可审计的」。

模板 / 文案清单：

    - :data:`LOOK_AT_CHARTER_TEMPLATE`        —— 视觉理解任务书（system
      首块：主体 / 场景 / 文字内容（OCR）/ 图表数据 / 值得注意的细节
      五段结构化描述框架 + 聚焦区块占位符 ``{focus_section}`` + 红线：
      不猜测图中不存在的文字 / 数据）；
    - :data:`LOOK_AT_QUESTION_FOCUS_TEMPLATE` —— 聚焦区块（问题给定形态；
      占位符 ``{question}``——描述完成后紧扣提问作答）；
    - :data:`LOOK_AT_DEFAULT_FOCUS`           —— 聚焦区块（缺省形态——
      描述完成后整体总结）；
    - :data:`LOOK_AT_DEFAULT_INSTRUCTION`     —— user 文本分块缺省指令
      （问题缺省时随图片分块一同发送的短指令）。

渲染函数（纯函数——只做文本装配；链路逻辑在 look_at 服务）。
"""

from __future__ import annotations

__all__ = [
    "LOOK_AT_PROMPTS_VERSION",
    "LOOK_AT_CHARTER_VERSION",
    "LOOK_AT_CHARTER_TEMPLATE",
    "LOOK_AT_QUESTION_FOCUS_TEMPLATE",
    "LOOK_AT_DEFAULT_FOCUS",
    "LOOK_AT_DEFAULT_INSTRUCTION",
    "render_look_at_charter",
    "render_user_instruction",
]

#: look_at prompt 套件总版本（任一模板 / 文案变更时同步上调）。
LOOK_AT_PROMPTS_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# 版本常量——逐模板独立版本（byte-lock 测试按模板分别锁定摘要）
# ---------------------------------------------------------------------------

#: 视觉理解任务书模板版本（聚焦区块文案随任务书同版本管理——同一
#: system 首块的组成部分，独立拆分只会制造版本漂移面）。
LOOK_AT_CHARTER_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# 模板文本（byte-lock——修改须同步版本常量与测试摘要）
# ---------------------------------------------------------------------------

#: 视觉理解任务书（system 首块；占位符 ``{focus_section}``——聚焦区块
#: 由渲染函数按问题有无填充）。
LOOK_AT_CHARTER_TEMPLATE: str = (
    "【视觉理解任务书】\n"
    "请仔细观察这张图片，输出结构化的中文描述，依次包含以下五段：\n"
    "1. 主体：画面中的核心对象（人物 / 动物 / 物品等）及其外观、动作与状态；\n"
    "2. 场景：环境背景、空间关系与整体氛围；\n"
    "3. 文字内容：图中出现的全部文字（OCR 逐字转录，保留原有语言与排版位置）；\n"
    "4. 图表数据：若含图表 / 表格，读出其类型、坐标轴或表头与关键数值；无则注明「无图表」；\n"
    "5. 值得注意的细节：容易忽略但有信息量的局部（表情、标识、时间、数量、异常之处等）。\n"
    "{focus_section}"
    "红线：只描述图中真实可见的内容，不猜测图中不存在的文字或数据；"
    "辨识不清的部分须如实注明「不确定」，不得编造。"
)

#: 聚焦区块（问题给定形态；占位符 ``{question}``）。
LOOK_AT_QUESTION_FOCUS_TEMPLATE: str = (
    "聚焦回答：完成上述结构化描述后，紧扣提问「{question}」给出一段直接、具体的回答。\n"
)

#: 聚焦区块（缺省形态——无问题时整体总结收束）。
LOOK_AT_DEFAULT_FOCUS: str = "聚焦回答：完成上述结构化描述后，用一段简练的中文总结图片的整体内容。\n"

#: user 文本分块缺省指令（问题缺省时随图片分块一同发送）。
LOOK_AT_DEFAULT_INSTRUCTION: str = "请看这张图片，按任务书完成结构化描述。"


# ---------------------------------------------------------------------------
# 渲染函数（纯文本装配——无状态 / 无 I/O / 无循环逻辑）
# ---------------------------------------------------------------------------


def render_look_at_charter(question: str | None) -> str:
    """渲染视觉理解任务书（聚焦区块按问题有无填充）。

    Args:
        question: 聚焦问题（None / 空串 = 缺省聚焦——整体总结收束）。

    Returns:
        任务书文本（system 首块；含五段描述框架 + 聚焦区块 + 红线）。
    """
    if question:
        focus_section = LOOK_AT_QUESTION_FOCUS_TEMPLATE.format(question=question)
    else:
        focus_section = LOOK_AT_DEFAULT_FOCUS
    return LOOK_AT_CHARTER_TEMPLATE.format(focus_section=focus_section)


def render_user_instruction(question: str | None) -> str:
    """渲染 user 文本分块指令（问题给定时即问题本身；缺省用短指令）。

    Args:
        question: 聚焦问题（None / 空串 = 缺省短指令）。

    Returns:
        随图片分块一同发送的文本分块内容。
    """
    if question:
        return question
    return LOOK_AT_DEFAULT_INSTRUCTION
