"""intent 域罐头文案模板（Unit 文件——文案与逻辑分离）。

约束 19 邻接约定：罐头文案与匹配逻辑物理分离（同 LLM prompt 的
Unit 文件卫生——这些**不是** LLM prompt，是确定性模板，但同样保持
「字节级可审、无逻辑渗入」）。模板变量：``{persona_name}``（展示名
优先的称呼）与 ``{time}``（本地 HH:MM）。

问候时段带（naive 本地小时——rhythm 域同墙钟约定）：

    - 早上（05:00-11:59）
    - 下午（12:00-17:59）
    - 晚上（18:00-22:59）
    - 深夜（23:00-04:59，或提供节律上下文时 = 人格睡眠窗口内）
"""

from __future__ import annotations

__all__ = [
    "TIME_QUERY_TEXT",
    "GREETING_MORNING_TEXT",
    "GREETING_AFTERNOON_TEXT",
    "GREETING_EVENING_TEXT",
    "GREETING_LATE_NIGHT_TEXT",
    "IDENTITY_TEXT",
    "HELP_TEXT",
    "DIRECTIVE_ACK_TEXT",
    "DIRECTIVE_CONFIRMED_TEXT",
]

#: TIME_QUERY 回复模板（{time} = 本地 HH:MM；{persona_name} = 称呼）。
TIME_QUERY_TEXT: str = "现在是{time}呀～{persona_name}刚瞄了一眼时间"

#: GREETING 早上带文案。
GREETING_MORNING_TEXT: str = "早上好呀～{persona_name}在这里"

#: GREETING 下午带文案。
GREETING_AFTERNOON_TEXT: str = "下午好呀～{persona_name}在这里"

#: GREETING 晚上带文案。
GREETING_EVENING_TEXT: str = "晚上好呀～{persona_name}在这里"

#: GREETING 深夜带文案（人格睡眠窗口内 / 固定 23-05 带共用）。
GREETING_LATE_NIGHT_TEXT: str = "这么晚还没睡呀？{persona_name}陪着你呢，早点休息哦～"

#: IDENTITY 回复模板（{persona_name} = 称呼）。
IDENTITY_TEXT: str = "我是{persona_name}呀，你的专属小伙伴，一直在这里陪你～"

#: DIRECTIVE 命中但未落库（副作用失败 / 未接线）的附和文案——不承诺
#: 「记住」（避免下一轮组装读不到指令时的自相矛盾）。
DIRECTIVE_ACK_TEXT: str = "嗯嗯，我明白啦～"

#: DIRECTIVE 落库成功后的确认文案（{directive} = 归一后的指令正文）。
DIRECTIVE_CONFIRMED_TEXT: str = "好，我记住啦：{directive}"

#: HELP 回复模板（静态能力清单——与既有能力面对齐：陪伴对话 / 记忆 /
#: 时间问答；值班巡检与联网搜索为可选装配能力）。
HELP_TEXT: str = (
    "我能陪你聊天、记住我们一起说过的事，也能随时告诉你现在几点；"
    "值班巡检、联网搜索这些能力也在慢慢解锁～有什么想问的尽管说！"
)
