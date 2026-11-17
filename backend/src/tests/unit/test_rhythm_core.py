"""rhythm 域纯函数核心测试——睡眠窗口 / 延迟采样 / 打字模拟 / 醒来推进。

测试策略：全部经注入时刻与注入随机源确定性驱动（零真实等待 / 零概率
抖动）；**时区无关构造**——测试时刻经 naive ``datetime(...).timestamp()``
构建（本地墙钟语义与被测函数的 naive-local 解析在任何时区机器上一致，
DST 切换日外的普通日期为锚）。延迟采样断言区间与确定性（种子化），
快答路径经脚本化假随机源精确触发。
"""

from __future__ import annotations

import random
from datetime import datetime

from packages.harness.rhythm.core import (
    TYPING_DURATION_CAP_MS,
    is_sleeping,
    next_active_window_start,
    reply_delay_ms,
    resolve_effective,
    typing_duration_ms,
)
from packages.harness.rhythm.rhythm_models import EffectiveRhythm, RhythmSettings


def _at(hour: int, minute: int, day: int = 15) -> int:
    """naive 本地时刻 → epoch 毫秒（时区无关构造——与被测同墙钟语义）。

    Args:
        hour: 小时（0-23）。
        minute: 分钟（0-59）。
        day: 日（默认 15——避开月末 / DST 切换日）。

    Returns:
        epoch 毫秒。
    """
    return int(datetime(2026, 1, day, hour, minute).timestamp() * 1000)


def _effective(**settings_overrides: object) -> EffectiveRhythm:
    """构造生效节律（resolve_effective 全局面直通形态）。

    Args:
        settings_overrides: RhythmSettings 字段覆写。

    Returns:
        :class:`EffectiveRhythm` 实例。
    """
    return resolve_effective(RhythmSettings(**settings_overrides), None)


class ScriptedRng:
    """脚本化随机源——按序列回放 random() 值（快答触发 / 采样位置精确控制）。"""

    def __init__(self, values: list[float]) -> None:
        """初始化（值序列耗尽后重复末值——断言前置足够长度）。

        Args:
            values: 回放值序列（0-1）。
        """
        self._values = values
        self.calls: list[str] = []

    def random(self) -> float:
        """回放下一值（调用记录——快答触发 / 采样位置两跳消费）。"""
        self.calls.append("random")
        if not self._values:
            return 0.5
        return self._values.pop(0)

    def randint(self, low: int, high: int) -> int:
        """整型采样（快径延迟面契约——本测试不消费，恒返回下界）。"""
        self.calls.append(f"randint:{low}:{high}")
        return low


class TestIsSleeping:
    async def test_cross_midnight_window_boundaries(self) -> None:
        """跨午夜窗口（23:30→08:00）：含 bedtime / 不含 wake / 窗外清醒。"""
        effective = _effective()
        assert is_sleeping(_at(23, 30), effective) is True  # bedtime 含端点
        assert is_sleeping(_at(23, 29), effective) is False
        assert is_sleeping(_at(0, 0), effective) is True
        assert is_sleeping(_at(3, 0), effective) is True
        assert is_sleeping(_at(7, 59), effective) is True
        assert is_sleeping(_at(8, 0), effective) is False  # wake 不含端点
        assert is_sleeping(_at(12, 0), effective) is False
        assert is_sleeping(_at(22, 0), effective) is False

    async def test_same_day_window(self) -> None:
        """同日窗口（13:00→15:00 午休形态）：窗口内睡 / 两端外醒。"""
        effective = _effective(bedtime="13:00", wake_time="15:00")
        assert is_sleeping(_at(12, 59), effective) is False
        assert is_sleeping(_at(13, 0), effective) is True
        assert is_sleeping(_at(14, 30), effective) is True
        assert is_sleeping(_at(15, 0), effective) is False

    async def test_degenerate_empty_window_never_sleeps(self) -> None:
        """退化形态（bedtime == wake）：空窗口恒醒。"""
        effective = _effective(bedtime="08:00", wake_time="08:00")
        assert is_sleeping(_at(8, 0), effective) is False
        assert is_sleeping(_at(23, 59), effective) is False


class TestReplyDelayMs:
    async def test_bounded_within_min_max(self) -> None:
        """边界夹逼：种子化大量采样恒在 [min, max] 内。"""
        effective = _effective()
        rng = random.Random(20260115)
        for _ in range(200):
            delay = reply_delay_ms(0, effective, rng)
            assert effective.reply_delay_min_ms <= delay <= effective.reply_delay_max_ms

    async def test_deterministic_with_seed(self) -> None:
        """种子化确定性：同种子两次采样序列完全一致。"""
        effective = _effective()
        first = [reply_delay_ms(0, effective, random.Random(7)) for _ in range(5)]
        second = [reply_delay_ms(0, effective, random.Random(7)) for _ in range(5)]
        assert first == second

    async def test_burst_draws_from_fast_tail(self) -> None:
        """快答路径：burst 触发时采样落在 [min, min*2) 快尾。"""
        effective = _effective()
        # 第一跳 random()=0.0 < 0.12 触发快答；第二跳 0.5 为快尾采样位置。
        rng = ScriptedRng([0.0, 0.5])
        delay = reply_delay_ms(0, effective, rng)
        assert effective.reply_delay_min_ms <= delay < effective.reply_delay_min_ms * 2

    async def test_no_burst_draws_full_range(self) -> None:
        """常规路径：burst 未触发时全域 log-uniform 采样（中位 ≈ 几何均值）。"""
        effective = _effective()
        rng = ScriptedRng([0.5, 0.5])
        delay = reply_delay_ms(0, effective, rng)
        # 位置 0.5 的 log-uniform 值 = sqrt(min*max) ≈ 8216（容差断言）。
        assert abs(delay - 8215) <= 2

    async def test_equal_bounds_constant(self) -> None:
        """min == max：恒返回该值（零宽采样域）。"""
        effective = _effective(reply_delay_min_ms=2000, reply_delay_max_ms=2000)
        assert reply_delay_ms(0, effective, random.Random(1)) == 2000

    async def test_burst_tail_clamped_to_max(self) -> None:
        """快尾上界夹逼：min*2 超过 max 时快尾不超过 max。"""
        effective = _effective(reply_delay_min_ms=30000, reply_delay_max_ms=40000)
        rng = ScriptedRng([0.0, 0.99])
        delay = reply_delay_ms(0, effective, rng)
        assert 30000 <= delay <= 40000


class TestTypingDurationMs:
    async def test_linear_accumulation(self) -> None:
        """线性累计：base + chars × speed（20 字 → 800 + 900 = 1700）。"""
        simulation = typing_duration_ms(20, _effective())
        assert simulation.duration_ms == 1700
        assert simulation.chars == 20

    async def test_zero_chars_returns_base(self) -> None:
        """零字符：仅基础耗时。"""
        simulation = typing_duration_ms(0, _effective())
        assert simulation.duration_ms == 800
        assert simulation.chars == 0

    async def test_capped_at_limit(self) -> None:
        """封顶：超长文本耗时恒为 8s 上限（8000ms）。"""
        simulation = typing_duration_ms(10000, _effective())
        assert simulation.duration_ms == TYPING_DURATION_CAP_MS
        assert simulation.chars == 10000

    async def test_negative_len_clamped_to_zero(self) -> None:
        """负长度夹逼 0（模型 ge=0 契约的输入侧守卫）。"""
        simulation = typing_duration_ms(-5, _effective())
        assert simulation.chars == 0
        assert simulation.duration_ms == 800


class TestNextActiveWindowStart:
    async def test_sleeping_before_wake_today(self) -> None:
        """入睡中（00:30，wake 08:00）：下一个醒来 = 今日 08:00。"""
        effective = _effective()
        assert next_active_window_start(_at(0, 30), effective) == _at(8, 0)

    async def test_just_before_wake(self) -> None:
        """醒前一分钟（07:59）：下一醒来 = 一分钟后的今日 08:00。"""
        effective = _effective()
        assert next_active_window_start(_at(7, 59), effective) == _at(8, 0)

    async def test_after_wake_today_is_tomorrow(self) -> None:
        """已醒（09:00，今日 wake 已过）：下一醒来 = 明日 08:00。"""
        effective = _effective()
        assert next_active_window_start(_at(9, 0), effective) == _at(8, 0, day=16)

    async def test_exactly_at_wake_is_tomorrow(self) -> None:
        """恰在 wake 时刻（08:00）：严格晚于语义 → 明日 08:00。"""
        effective = _effective()
        assert next_active_window_start(_at(8, 0), effective) == _at(8, 0, day=16)

    async def test_result_strictly_after_now(self) -> None:
        """不变式：产出恒严格晚于参考时刻（任意参考点）。"""
        effective = _effective()
        for moment in (_at(0, 0), _at(8, 0), _at(12, 0), _at(23, 59)):
            assert next_active_window_start(moment, effective) > moment
