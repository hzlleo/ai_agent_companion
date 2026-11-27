"""GUI 微信通道提示词 Unit 文件——截图解析链路的全部 LLM 提示词文本。

本工程约定（提示词与逻辑分离）：进入 LLM 请求的提示词文本只能定义在本
模块；``wechat_gui_reader`` 等逻辑文件不得内联提示词字符串。模板文本变更
属于行为变更，须同步上调版本常量并更新 byte-lock 测试中的 sha256 摘要。

模板 / 文案清单：

    - :data:`WECHAT_GUI_READER_CHARTER_TEMPLATE` —— 截图解析任务书
      （system 首块：只依据可见像素作答 / 严格 JSON 输出 / 标题与消息
      字段语义 / 非文本消息占位 / 置信度自评）；
    - :data:`WECHAT_GUI_READER_INSTRUCTION_TEMPLATE` —— user 文本分块
      指令（携带单次解析消息上限）。

渲染函数只做文本装配（无状态 / 无 I/O）。
"""

from __future__ import annotations

__all__ = [
    "WECHAT_GUI_PROMPTS_VERSION",
    "WECHAT_GUI_READER_CHARTER_VERSION",
    "WECHAT_GUI_READER_CHARTER_TEMPLATE",
    "WECHAT_GUI_READER_INSTRUCTION_TEMPLATE",
    "render_wechat_gui_reader_charter",
    "render_wechat_gui_reader_instruction",
]

#: GUI 微信提示词套件总版本（任一模板变更时同步上调）。
WECHAT_GUI_PROMPTS_VERSION: str = "1.1.0"

#: 截图解析任务书版本。
WECHAT_GUI_READER_CHARTER_VERSION: str = "1.1.0"

#: 截图解析任务书（system 首块；严格 JSON 输出契约）。
WECHAT_GUI_READER_CHARTER_TEMPLATE: str = (
    "【微信截图解析任务书】\n"
    "你是微信桌面客户端的截图解析器。只依据截图中真实可见的像素内容作答，"
    "不臆测被遮挡或未显示的信息；看不清的字段留空或如实降低置信度。\n"
    "请识别两项内容：\n"
    "1. 当前聊天窗口标题栏显示的联系人备注名或群名（只取标题文本本身，"
    "去掉未读计数、返回箭头等装饰字符）；\n"
    "2. 聊天区域内最新若干条消息，按从上到下的时间顺序输出；每条消息包含"
    "发送者显示名、正文、是否由本账号发出、以及截图可见的时间——尽力读取"
    "界面上的相对时间，读不出时填 0。\n"
    "非文本消息（图片、表情、语音、文件、链接卡片等）以方括号占位文本表示，"
    "例如「[图片]」「[语音]」；不要把气泡装饰、时间分隔条或输入框内容当作消息。\n"
    "严格输出一个 UTF-8 JSON 对象，不要输出 Markdown 代码围栏或任何额外说明：\n"
    '{"title": "当前聊天窗口标题", "messages": [{"sender": "发送者显示名", '
    '"text": "消息正文", "is_self": false, "ts_ms": 0}], "confidence": 0.0}\n'
    "字段约束：title 非空字符串；messages 为数组（可为空）；sender 与 text 为"
    "字符串；is_self 为布尔值；ts_ms 为整数（消息时间的毫秒表示；界面只显示"
    "相对时间时尽力读取，读不出填 0）；confidence 为 0.0 到 1.0 之间的小数，"
    "表示你对标题与消息识别的整体把握。"
)

#: user 文本分块指令模板（占位符 ``{max_messages}``——单次解析消息上限）。
WECHAT_GUI_READER_INSTRUCTION_TEMPLATE: str = (
    "请解析这张微信聊天窗口截图，最多输出最新 {max_messages} 条消息；"
    "按任务书严格输出 JSON。"
)


def render_wechat_gui_reader_charter() -> str:
    """渲染截图解析任务书（system 首块）。

    Returns:
        任务书文本（严格 JSON 输出契约）。
    """
    return WECHAT_GUI_READER_CHARTER_TEMPLATE


def render_wechat_gui_reader_instruction(*, max_messages: int) -> str:
    """渲染 user 文本分块指令。

    Args:
        max_messages: 单次解析消息上限（正数——调用方已校验）。

    Returns:
        随图片分块一同发送的指令文本。
    """
    return WECHAT_GUI_READER_INSTRUCTION_TEMPLATE.replace(
        "{max_messages}", str(max_messages)
    )
