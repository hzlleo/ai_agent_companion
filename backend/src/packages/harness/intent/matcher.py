"""intent 域匹配器——纯函数模式表匹配 + 罐头文案渲染（零 I/O）。

匹配链（单一入口 :func:`match_intent`）::

    原文 → NFKC 归一（全角→半角）+ 去全部空白 + casefold
        ↓ ① 精确层：整句等值比对（四类按 TIME_QUERY → GREETING →
              IDENTITY → HELP 优先序）——命中即高置信度
        ↓ ② 模糊层：子串包含比对（同优先序）——低置信度
        ↓ 未命中 → handled=False（文本照常流入 LLM）
        ↓ 命中 → 模板渲染（persona_name / 本地 HH:MM 注入）

优先序说明（文档化锁定）：跨类竞争时 TIME_QUERY 最高（时间问答信息
价值最高且无歧义——「你好，现在几点了」答时间优于答问候）；同类内
精确优先于模糊。置信度仅观测面（不参与路由）。

问候时段带：本地小时带（早上 0-11 / 下午 12-17 / 晚上 18-22）；
深夜带 = 固定 23-05（无节律上下文）或**人格睡眠窗口内**（提供
``effective`` 时——窗口内问候按深夜文案，人格视角的「这么晚」）。

依赖方向：intent_models / intent_texts / rhythm 纯函数（单向——
被 chat 域 reply 前钩与 intent 服务消费）。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Final

from packages.harness.intent.intent_models import IntentKind, IntentMatchModel
from packages.harness.intent.intent_texts import (
    GREETING_AFTERNOON_TEXT,
    GREETING_EVENING_TEXT,
    GREETING_LATE_NIGHT_TEXT,
    GREETING_MORNING_TEXT,
    HELP_TEXT,
    IDENTITY_TEXT,
    TIME_QUERY_TEXT,
)
from packages.harness.rhythm.core import is_sleeping
from packages.harness.rhythm.rhythm_models import EffectiveRhythm

__all__ = ["match_intent", "CONFIDENCE_EXACT", "CONFIDENCE_FUZZY"]

#: 精确整句命中置信度（观测面——不参与路由决策）。
CONFIDENCE_EXACT: Final[float] = 0.95
#: 模糊包含命中置信度。
CONFIDENCE_FUZZY: Final[float] = 0.65

#: 类间优先序（跨类模糊竞争时的裁定序——见模块 docstring）。
_KIND_PRECEDENCE: Final[tuple[IntentKind, ...]] = (
    IntentKind.TIME_QUERY,
    IntentKind.GREETING,
    IntentKind.IDENTITY,
    IntentKind.HELP,
)


def _normalize(text: str) -> str:
    """匹配归一（NFKC + 去全部空白 + casefold——模式与文本同空间）。

    Args:
        text: 原始文本。

    Returns:
        归一文本（全角→半角、任意空白剔除、大小写折叠；空文本原样）。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(folded.split())


#: 精确层模式表（整句等值——归一空间字面量；构造期统一归一防手误）。
_EXACT_PATTERNS_RAW: Final[dict[IntentKind, tuple[str, ...]]] = {
    IntentKind.TIME_QUERY: (
        "现在几点",
        "现在几点了",
        "几点了",
        "几点",
        "what time",
        "what time is it",
    ),
    IntentKind.GREETING: (
        "你好",
        "您好",
        "早上好",
        "早安",
        "下午好",
        "晚上好",
        "晚安",
        "早",
        "早呀",
        "在吗",
        "hi",
        "hello",
    ),
    IntentKind.IDENTITY: (
        "你是谁",
        "你叫什么",
        "你叫什么名字",
        "你叫啥",
        "你的名字",
        "who are you",
    ),
    IntentKind.HELP: (
        "帮助",
        "help",
        "你能干什么",
        "能干什么",
        "你会什么",
        "你会啥",
        "你能做什么",
        "有什么功能",
        "功能",
    ),
}

#: 模糊层模式表（子串包含——多字符安全面：单词 / 单字易误命中
#: （「早」×「早饭」、「hi」×「this」），短模式仅入精确层；时间模糊
#: 面锚定问法上下文（「几点」裸词 ×「几点睡觉合适」歧义——裸词仅
#: 精确层命中，歧义句照常走 LLM）。
_FUZZY_PATTERNS_RAW: Final[dict[IntentKind, tuple[str, ...]]] = {
    IntentKind.TIME_QUERY: ("现在几点", "几点了", "what time"),
    IntentKind.GREETING: (
        "你好",
        "您好",
        "早上好",
        "早安",
        "下午好",
        "晚上好",
        "晚安",
        "在吗",
        "hello",
    ),
    IntentKind.IDENTITY: ("你是谁", "你叫什么", "你叫啥"),
    IntentKind.HELP: ("帮助", "能干什么", "能做什么", "会什么", "有什么功能"),
}

#: 构造期归一后的生效模式表（字面量手误防线——归一空间一致性内建）。
_EXACT_PATTERNS: Final[dict[IntentKind, tuple[str, ...]]] = {
    kind: tuple(_normalize(pattern) for pattern in patterns)
    for kind, patterns in _EXACT_PATTERNS_RAW.items()
}
_FUZZY_PATTERNS: Final[dict[IntentKind, tuple[str, ...]]] = {
    kind: tuple(_normalize(pattern) for pattern in patterns)
    for kind, patterns in _FUZZY_PATTERNS_RAW.items()
}


def _format_local_hhmm(now_ms: int) -> str:
    """epoch 毫秒 → 本地 "HH:MM"（naive 本地墙钟——rhythm 域同约定）。

    Args:
        now_ms: 当前时刻（epoch 毫秒——注入式）。

    Returns:
        ``HH:MM`` 形态的本地时刻字符串。
    """
    return datetime.fromtimestamp(now_ms / 1000).strftime("%H:%M")


def _greeting_text(now_ms: int, persona_name: str, effective: EffectiveRhythm | None) -> str:
    """问候文案选带（本地小时带 + 可选节律深夜带）。

    深夜带判定：提供 ``effective`` 时 = 人格睡眠窗口内（``is_sleeping``
    ——窗口内问候按「这么晚」文案）；否则固定 23:00-04:59。其余按
    早上（<12）/ 下午（12-17）/ 晚上（18-22）三带。

    Args:
        now_ms: 当前时刻（epoch 毫秒——注入式）。
        persona_name: 人格称呼（模板注入）。
        effective: 生效节律（None = 固定小时带）。

    Returns:
        渲染后的问候文案。
    """
    hour = datetime.fromtimestamp(now_ms / 1000).hour
    late_night = (
        is_sleeping(now_ms, effective) if effective is not None else (hour >= 23 or hour < 5)
    )
    if late_night:
        return GREETING_LATE_NIGHT_TEXT.format(persona_name=persona_name)
    if hour < 12:
        return GREETING_MORNING_TEXT.format(persona_name=persona_name)
    if hour < 18:
        return GREETING_AFTERNOON_TEXT.format(persona_name=persona_name)
    return GREETING_EVENING_TEXT.format(persona_name=persona_name)


def _render_response(
    kind: IntentKind, persona_name: str, now_ms: int, effective: EffectiveRhythm | None
) -> str:
    """按意图类型渲染罐头回复（模板变量注入）。

    Args:
        kind: 命中的意图类型。
        persona_name: 人格称呼（展示名优先——提取在服务层完成）。
        now_ms: 当前时刻（epoch 毫秒——TIME_QUERY / GREETING 消费）。
        effective: 生效节律（GREETING 深夜带消费；None = 固定小时带）。

    Returns:
        渲染后的罐头回复文本（非空）。
    """
    if kind is IntentKind.TIME_QUERY:
        return TIME_QUERY_TEXT.format(time=_format_local_hhmm(now_ms), persona_name=persona_name)
    if kind is IntentKind.GREETING:
        return _greeting_text(now_ms, persona_name, effective)
    if kind is IntentKind.IDENTITY:
        return IDENTITY_TEXT.format(persona_name=persona_name)
    return HELP_TEXT


def match_intent(
    text: str,
    persona_name: str,
    now_ms: int,
    effective: EffectiveRhythm | None = None,
) -> IntentMatchModel:
    """意图匹配（纯函数——模式表比对 + 模板渲染；零 I/O 零异常）。

    Args:
        text: 用户消息原文（空 / 纯空白归一后为空 → 恒未命中）。
        persona_name: 人格称呼（展示名优先——服务层提取后注入）。
        now_ms: 当前时刻（epoch 毫秒——注入式；TIME_QUERY 答案与
            GREETING 选带消费）。
        effective: 生效节律（None = 问候深夜带用固定 23-05 小时带；
            提供时深夜带 = 人格睡眠窗口）。

    Returns:
        :class:`~packages.harness.intent.intent_models.IntentMatchModel`
        （未命中 = handled=False + kind=None + 空文案——文本照常流入
        LLM）。
    """
    normalized = _normalize(text)
    if not normalized:
        return IntentMatchModel()
    for kind in _KIND_PRECEDENCE:
        if normalized in _EXACT_PATTERNS[kind]:
            return IntentMatchModel(
                kind=kind,
                confidence=CONFIDENCE_EXACT,
                response_text=_render_response(kind, persona_name, now_ms, effective),
                handled=True,
            )
    for kind in _KIND_PRECEDENCE:
        if any(pattern in normalized for pattern in _FUZZY_PATTERNS[kind]):
            return IntentMatchModel(
                kind=kind,
                confidence=CONFIDENCE_FUZZY,
                response_text=_render_response(kind, persona_name, now_ms, effective),
                handled=True,
            )
    return IntentMatchModel()
