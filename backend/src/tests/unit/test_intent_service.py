"""intent 域服务测试——IntentShortcutService（委托 / 称呼提取 / 拟真延迟）。

覆盖矩阵：try_shortcut 纯委托（匹配形状透传）；人格称呼提取（display_name
优先 / 空回落 persona_name）；恒不抛异常（matcher 异常归一未命中）；
realism_delay 延迟区间（脚本化随机源 + 假休眠面——零真实等待，断言
秒值落在 [0.8, 1.5]）；缺省时刻走注入时钟；IntentSettings env 面。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from packages.harness.config.models import Settings
from packages.harness.intent.intent_models import IntentKind, IntentSettings
from packages.harness.intent.intent_texts import IDENTITY_TEXT
from packages.harness.intent.matcher import match_intent
from packages.harness.intent.service import (
    INTENT_SHORTCUT_DELAY_MAX_MS,
    INTENT_SHORTCUT_DELAY_MIN_MS,
    IntentShortcutService,
)
from packages.harness.rhythm.core import resolve_effective
from packages.harness.rhythm.rhythm_models import RhythmSettings


def _at(hour: int, minute: int = 0) -> int:
    """naive 本地时刻 → epoch 毫秒（时区无关构造——与被测同墙钟语义）。

    Args:
        hour: 小时（0-23）。
        minute: 分钟（0-59；默认 0）。

    Returns:
        epoch 毫秒。
    """
    return int(datetime(2026, 1, 15, hour, minute).timestamp() * 1000)


class FakeNaming:
    """fake 人格命名面（persona_name / display_name 二元组）。"""

    def __init__(self, persona_name: str, display_name: str) -> None:
        """初始化。

        Args:
            persona_name: 注册名。
            display_name: 展示名（空串 = 未设置形态）。
        """
        self.persona_name = persona_name
        self.display_name = display_name


class ScriptedRng:
    """脚本化随机源——randint 回放固定毫秒值（延迟区间断言原料）。"""

    def __init__(self, delay_ms: int) -> None:
        """初始化。

        Args:
            delay_ms: randint 回放值（毫秒）。
        """
        self._delay_ms = delay_ms
        self.randint_calls: list[tuple[int, int]] = []

    def randint(self, low: int, high: int) -> int:
        """整型采样（记录调用域——区间断言）。"""
        self.randint_calls.append((low, high))
        return self._delay_ms

    def random(self) -> float:
        """浮点采样（本测试不消费——恒中位）。"""
        return 0.5


class TestTryShortcut:
    async def test_pure_delegation_to_matcher(self) -> None:
        """纯委托：匹配形状与 matcher 直调完全一致。"""
        service = IntentShortcutService(clock=lambda: _at(9, 5))
        direct = match_intent("现在几点", "小鱼", _at(9, 5))
        via_service = await service.try_shortcut("现在几点", FakeNaming("小鱼", ""))
        assert via_service.kind == direct.kind
        assert via_service.confidence == direct.confidence
        assert via_service.response_text == direct.response_text
        assert via_service.handled == direct.handled
        assert "09:05" in via_service.response_text

    async def test_display_name_preferred(self) -> None:
        """称呼提取：display_name 非空优先（罐头文案注入展示名）。"""
        service = IntentShortcutService(clock=lambda: _at(10, 0))
        match = await service.try_shortcut("你是谁", FakeNaming("feiyu", "大肥鱼"))
        assert match.response_text == IDENTITY_TEXT.format(persona_name="大肥鱼")

    async def test_display_name_empty_falls_back(self) -> None:
        """称呼提取：display_name 空 → 回落注册名。"""
        service = IntentShortcutService(clock=lambda: _at(10, 0))
        match = await service.try_shortcut("你是谁", FakeNaming("feiyu", ""))
        assert match.response_text == IDENTITY_TEXT.format(persona_name="feiyu")

    async def test_never_raises_on_internal_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """恒不抛：matcher 内部异常归一未命中（文本照常流入 LLM）。"""
        service = IntentShortcutService(clock=lambda: _at(10, 0))

        def _explode(text: str, persona_name: str, now_ms: int, effective: object = None) -> object:
            """注入的故障 matcher（模拟匹配层内部崩溃）。"""
            raise RuntimeError("匹配层崩溃（测试注入）")

        monkeypatch.setattr("packages.harness.intent.service.match_intent", _explode)
        match = await service.try_shortcut("你是谁", FakeNaming("小鱼", ""))
        assert match.handled is False
        assert match.kind is None
        assert match.response_text == ""

    async def test_unmatched_passthrough(self) -> None:
        """未命中：handled=False（LLM 路径兜底契约）。"""
        service = IntentShortcutService(clock=lambda: _at(10, 0))
        match = await service.try_shortcut("今天天气怎么样", FakeNaming("小鱼", ""))
        assert match.handled is False

    async def test_default_now_uses_injected_clock(self) -> None:
        """缺省 now_ms：走注入时钟（固定 09:05 ——时间答案确定性）。"""
        service = IntentShortcutService(clock=lambda: _at(9, 5))
        match = await service.try_shortcut("现在几点", FakeNaming("小鱼", ""))
        assert match.kind is IntentKind.TIME_QUERY
        assert "09:05" in match.response_text

    async def test_effective_forwarded_to_matcher(self) -> None:
        """生效节律透传：深夜带按睡眠窗口裁定（22:30 + bedtime 22:00）。"""
        effective = resolve_effective(RhythmSettings(), None)
        service = IntentShortcutService(effective=effective, clock=lambda: _at(12, 0))
        match = await service.try_shortcut("你好", FakeNaming("小鱼", ""))
        assert match.kind is IntentKind.GREETING
        # 12:00 不在缺省睡眠窗口（23:30-08:00）内 → 下午带文案。
        assert "下午好" in match.response_text


class TestRealismDelay:
    async def test_delay_range_and_sleep_seconds(self) -> None:
        """拟真延迟：randint 域 [800, 1500] + 休眠面收到秒值 [0.8, 1.5]。"""
        rng = ScriptedRng(delay_ms=1200)
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            """假休眠面（记录秒值——零真实等待）。"""
            slept.append(seconds)

        service = IntentShortcutService(rng=rng, sleep=fake_sleep)
        await service.realism_delay()
        assert rng.randint_calls == [(INTENT_SHORTCUT_DELAY_MIN_MS, INTENT_SHORTCUT_DELAY_MAX_MS)]
        assert slept == [1.2]
        assert 0.8 <= slept[0] <= 1.5

    async def test_bounds_constants_locked(self) -> None:
        """区间常量锁定：800 / 1500（「顺手秒回」语义——不走节律门完整延迟）。"""
        assert INTENT_SHORTCUT_DELAY_MIN_MS == 800
        assert INTENT_SHORTCUT_DELAY_MAX_MS == 1500
        assert INTENT_SHORTCUT_DELAY_MIN_MS < INTENT_SHORTCUT_DELAY_MAX_MS


class TestIntentSettingsEnvFace:
    async def test_default_enabled(self) -> None:
        """缺省开启（纯本地零成本——与 search 等外部成本面开关相反）。"""
        assert IntentSettings().enabled is True

    async def test_env_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """INTENT_ENABLED=false：env 面关闭。"""
        monkeypatch.setenv("INTENT_ENABLED", "false")
        assert IntentSettings().enabled is False

    async def test_settings_aggregate_embeds_intent(self) -> None:
        """Settings 聚合器嵌入 intent 分节。"""
        settings = Settings()
        assert isinstance(settings.intent, IntentSettings)
        assert settings.intent.enabled is True
