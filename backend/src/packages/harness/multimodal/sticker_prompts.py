"""multimodal 表情包标注提示词（prompt Unit 文件——与模型和逻辑分离）。

本模块只承载 VLM 标注使用的提示词文本与版本常量：标注任务书（system）
要求模型输出严格 JSON（描述 + 标签），标签用于管理面检索与后续发送选材。
提示词变更是知情的、可审计的——版本常量随文本一起演进，测试对每段模板
做 sha256 byte-lock。

输出契约（服务层解析）：

    {"description": "一句中文描述", "tags": ["标签", "..."]}

红线：只描述画面真实可见的内容；不猜测画面外的剧情；标签用短词，不写
整句；辨识不清时用「不确定」而不是编造。
"""

from __future__ import annotations

#: 标注提示词版本（文本变更时同步递增）。
STICKER_PROMPTS_VERSION: str = "1.0.0"

#: 标注任务书（system 首块——严格 JSON 输出契约 + 描述与标签口径）。
STICKER_ANNOTATION_CHARTER_TEMPLATE: str = (
    "你是表情包素材标注助手。请观察用户提供的一张表情包图片，输出严格 JSON，不要输出 JSON 之外的任何文字。\n"
    "JSON 结构：{\"description\": \"一句中文描述\", \"tags\": [\"标签1\", \"标签2\"]}。\n"
    "description：一句话说明画面主体、动作与情绪（不超过 60 个汉字；图中若有文字，可点明文字内容）。\n"
    "tags：2-6 个中文短标签，每个不超过 12 个汉字，覆盖情绪、场景、动作或角色类型，便于日后按标签检索。\n"
    "红线：只描述画面真实可见的内容，不猜测画面外的剧情；辨识不清时写「不确定」，不要编造；"
    "标签用短词，不要写整句。"
)

#: 用户侧指令（user 内容分块中的文本块——与图像块并列）。
STICKER_ANNOTATION_INSTRUCTION_TEMPLATE: str = (
    "请标注这张表情包：给出不超过 60 字的画面描述，以及 2-6 个中文短标签。"
    "只输出 JSON，不要解释。"
)


def render_sticker_annotation_charter() -> str:
    """渲染标注任务书（system 首块）。

    Returns:
        任务书文本（当前版本模板原文）。
    """
    return STICKER_ANNOTATION_CHARTER_TEMPLATE


def render_sticker_annotation_instruction() -> str:
    """渲染用户侧标注指令。

    Returns:
        指令文本（当前版本模板原文）。
    """
    return STICKER_ANNOTATION_INSTRUCTION_TEMPLATE
