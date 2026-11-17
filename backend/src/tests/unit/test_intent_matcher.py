"""intent 域匹配器测试——模式表 / 归一 / 优先序 / 时段带（表驱动）。

覆盖矩阵：四类各自命中（精确 / 模糊）与非命中边界（全角 / 大小写 /
空白 / 多语句 / 短模式误命中防线）；跨类优先序（时间 > 问候）；
置信度分层；TIME_QUERY 注入时刻格式化（时区无关构造）；GREETING
四时段带（固定小时带 + 节律深夜带）；persona_name 注入。
"""

from __future__ import annotations

from datetime import datetime

from packages.harness.intent.intent_models import IntentKind
from packages.harness.intent.intent_texts import (
    GREETING_LATE_NIGHT_TEXT,
    HELP_TEXT,
    IDENTITY_TEXT,
)
from packages.harness.intent.matcher import (
    CONFIDENCE_EXACT,
    CONFIDENCE_FUZZY,
    match_intent,
)
from packages.harness.rhythm.core import resolve_effective
from packages.harness.rhythm.rhythm_models import (
    EffectiveRhythm,
    PersonaRhythmOverride,
    RhythmSettings,
)


def _at(hour: int, minute: int = 0) -> int:
    """naive 本地时刻 → epoch 毫秒（时区无关构造——与被测同墙钟语义）。

    Args:
        hour: 小时（0-23）。
        minute: 分钟（0-59；默认 0）。

    Returns:
        epoch 毫秒。
    """
    return int(datetime(2026, 1, 15, hour, minute).timestamp() * 1000)


def _effective(**overrides: object) -> EffectiveRhythm:
    """构造生效节律（resolve_effective 直通形态——深夜带测试原料）。

    Args:
        overrides: PersonaRhythmOverride 字段覆写。

    Returns:
        生效节律实例。
    """
    return resolve_effective(RhythmSettings(), PersonaRhythmOverride(**overrides))


class TestTimeQuery:
    async def test_exact_matches_table(self) -> None:
        """精确命中表：整句问法 → kind=TIME_QUERY + 置信度 0.95。"""
        for text in ("现在几点", "现在几点了", "几点了", "几点", "what time", "What Time Is It"):
            match = match_intent(text, "小鱼", _at(9, 5))
            assert match.handled is True, text
            assert match.kind is IntentKind.TIME_QUERY, text
            assert match.confidence == CONFIDENCE_EXACT

    async def test_fuzzy_matches_table(self) -> None:
        """模糊命中表：上下文锚定的包含问法 → 置信度 0.65。"""
        for text in ("我想知道现在几点了呀", "现在几点啦", "现在几点了呢", "what time is it now?"):
            match = match_intent(text, "小鱼", _at(9, 5))
            assert match.handled is True, text
            assert match.kind is IntentKind.TIME_QUERY, text
            assert match.confidence == CONFIDENCE_FUZZY

    async def test_answer_formats_injected_time(self) -> None:
        """时间答案来自注入时刻：HH:MM 本地格式 + persona_name 注入。"""
        match = match_intent("现在几点", "小鱼", _at(9, 5))
        assert "09:05" in match.response_text
        assert "小鱼" in match.response_text

    async def test_non_matches_table(self) -> None:
        """非命中表：时间相近但非问法 → 未命中（照常走 LLM）。"""
        for text in ("几点睡觉比较合适", "时间过得真快", "about timing", "告诉我几点比较好"):
            match = match_intent(text, "小鱼", _at(9, 5))
            assert match.handled is False, text
            assert match.kind is None


class TestGreeting:
    async def test_exact_matches_table(self) -> None:
        """精确命中表：问候整句（中 / 英 / 在吗）。"""
        for text in ("你好", "您好", "早上好", "早安", "晚安", "早", "早呀", "在吗", "hi", "Hello"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is True, text
            assert match.kind is IntentKind.GREETING, text

    async def test_fuzzy_matches_table(self) -> None:
        """模糊命中表：问候语作为子句出现。"""
        for text in ("宝贝早上好呀", "晚安啦", "你好呀在吗", "hello there"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is True, text
            assert match.kind is IntentKind.GREETING, text

    async def test_short_pattern_false_positive_guard(self) -> None:
        """短模式误命中防线：「早」/「hi」仅精确层——含其的字词不误判。"""
        for text in ("早饭吃什么", "this is nice", "thin king"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is False, text

    async def test_band_by_hour_table(self) -> None:
        """时段带表（固定小时带——无节律上下文）。"""
        cases = [
            (_at(9, 0), "早上好"),
            (_at(0, 30), GREETING_LATE_NIGHT_TEXT.split("？")[0]),
            (_at(14, 0), "下午好"),
            (_at(20, 0), "晚上好"),
            (_at(23, 30), GREETING_LATE_NIGHT_TEXT.split("？")[0]),
            (_at(4, 59), GREETING_LATE_NIGHT_TEXT.split("？")[0]),
        ]
        for now, expected_fragment in cases:
            match = match_intent("你好", "小鱼", now)
            assert match.kind is IntentKind.GREETING
            assert expected_fragment in match.response_text, match.response_text

    async def test_band_with_rhythm_late_night_window(self) -> None:
        """节律深夜带：提供生效节律时，睡眠窗口内（bedtime 覆写 22:00 后
        22:30 已在窗口）按深夜文案——早于固定 23 点带。"""
        effective = _effective(bedtime="22:00", wake_time="09:00")
        match = match_intent("你好", "小鱼", _at(22, 30), effective)
        assert match.kind is IntentKind.GREETING
        assert "这么晚" in match.response_text

    async def test_band_with_rhythm_awake_keeps_hour_band(self) -> None:
        """节律清醒时保持小时带（窗口外不触发深夜文案）。"""
        effective = _effective(bedtime="22:00", wake_time="09:00")
        match = match_intent("你好", "小鱼", _at(20, 0), effective)
        assert "晚上好" in match.response_text


class TestIdentityAndHelp:
    async def test_identity_matches_table(self) -> None:
        """身份命中表：精确 + 模糊 + persona_name 注入。"""
        for text in (
            "你是谁",
            "你叫什么",
            "你叫什么名字",
            "你叫啥",
            "who are you",
            "你知道你是谁吗",
        ):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is True, text
            assert match.kind is IntentKind.IDENTITY, text
        rendered = match_intent("你是谁", "小鱼", _at(10, 0))
        assert rendered.response_text == IDENTITY_TEXT.format(persona_name="小鱼")

    async def test_help_matches_table(self) -> None:
        """帮助命中表：精确 + 模糊；静态能力清单文案。"""
        for text in ("帮助", "help", "你能干什么", "你会什么", "有什么功能", "我需要帮助"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is True, text
            assert match.kind is IntentKind.HELP, text
        rendered = match_intent("帮助", "小鱼", _at(10, 0))
        assert rendered.response_text == HELP_TEXT

    async def test_identity_and_help_non_matches(self) -> None:
        """非命中表：相近但非身份 / 帮助问法。"""
        for text in ("他是谁", "我叫什么", "help me move this box to there"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is False, text


class TestNormalizationAndPrecedence:
    async def test_fullwidth_normalized(self) -> None:
        """全角归一：全角英数（NFKC）照常命中。"""
        match = match_intent("ｈｅｌｌｏ", "小鱼", _at(10, 0))
        assert match.handled is True
        assert match.kind is IntentKind.GREETING

    async def test_whitespace_stripped(self) -> None:
        """空白剔除：中英空白（含多语句标点隔断）不阻断命中。"""
        match = match_intent("你 好", "小鱼", _at(10, 0))
        assert match.handled is True and match.kind is IntentKind.GREETING
        match = match_intent("  what   time  ", "小鱼", _at(9, 5))
        assert match.kind is IntentKind.TIME_QUERY

    async def test_case_folded(self) -> None:
        """大小写折叠：WHAT TIME / Hello 照常命中。"""
        assert match_intent("WHAT TIME", "小鱼", _at(9, 5)).kind is IntentKind.TIME_QUERY
        assert match_intent("Hello", "小鱼", _at(10, 0)).kind is IntentKind.GREETING

    async def test_multi_statement_time_wins_over_greeting(self) -> None:
        """多语句 + 跨类竞争：时间优先序压过问候（信息价值裁定）。"""
        match = match_intent("在吗？现在几点了", "小鱼", _at(9, 5))
        assert match.kind is IntentKind.TIME_QUERY

    async def test_empty_and_blank_never_match(self) -> None:
        """空 / 纯空白：恒未命中（LLM 路径兜底）。"""
        for text in ("", "   ", "\u3000\t\n"):
            match = match_intent(text, "小鱼", _at(10, 0))
            assert match.handled is False
            assert match.kind is None
            assert match.response_text == ""
            assert match.confidence == 0.0

    async def test_unhandled_shape_contract(self) -> None:
        """未命中形状契约：kind=None + 空文案 + 零置信度 + handled=False。"""
        match = match_intent("今天天气怎么样", "小鱼", _at(10, 0))
        assert match.handled is False
        assert match.kind is None
        assert match.response_text == ""
        assert match.confidence == 0.0
