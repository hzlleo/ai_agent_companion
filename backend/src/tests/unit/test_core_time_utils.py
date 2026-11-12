"""``core.time_utils`` 单元测试——epoch 毫秒工具 + 时长格式化三档规格。

覆盖矩阵：

    - ``now_ms``：单调递增 / 毫秒量级；
    - ``to_ms``：aware / naive datetime、None 安全；
    - ``from_ms``：UTC 往返、None 安全；
    - ``format_duration_ms``：RATIFIED 三档规格 + 边界 + 参数化单位。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.harness.core.time_utils import format_duration_ms, from_ms, now_ms, to_ms


class TestNowMs:
    async def test_returns_epoch_milliseconds(self) -> None:
        """now_ms 量级 = epoch 毫秒（> 10^12，秒级是 10^9）。"""
        value = now_ms()
        assert value > 1_000_000_000_000

    async def test_monotonic_non_decreasing(self) -> None:
        """连续调用单调非递减。"""
        first = now_ms()
        second = now_ms()
        assert second >= first


class TestToMs:
    async def test_utc_datetime(self) -> None:
        """aware UTC datetime → epoch 毫秒。"""
        dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert to_ms(dt) == 1_704_067_200_000

    async def test_epoch_origin(self) -> None:
        """epoch 原点 → 0。"""
        assert to_ms(datetime(1970, 1, 1, tzinfo=UTC)) == 0

    async def test_none_returns_zero(self) -> None:
        """None 安全 → 0。"""
        assert to_ms(None) == 0

    async def test_millisecond_precision(self) -> None:
        """毫秒精度保留（.5 秒 → 500ms）。"""
        dt = datetime(1970, 1, 1, 0, 0, 0, 500_000, tzinfo=UTC)
        assert to_ms(dt) == 500


class TestFromMs:
    async def test_roundtrip_with_to_ms(self) -> None:
        """from_ms(to_ms(dt)) 语义往返（UTC 无损）。"""
        dt = datetime(2026, 6, 15, 12, 30, 45, tzinfo=UTC)
        restored = from_ms(to_ms(dt))
        assert restored is not None
        assert restored == dt

    async def test_returns_utc_timezone(self) -> None:
        """返回值携带 UTC 时区。"""
        restored = from_ms(0)
        assert restored is not None
        assert restored.tzinfo is UTC

    async def test_none_returns_none(self) -> None:
        """None 安全 → None。"""
        assert from_ms(None) is None

    async def test_known_epoch(self) -> None:
        """已知 epoch 毫秒 → 确定 datetime。"""
        restored = from_ms(1_704_067_200_000)
        assert restored == datetime(2024, 1, 1, tzinfo=UTC)

    async def test_timedelta_arithmetic_consistent(self) -> None:
        """+1 天的 epoch 差恰为 86400000ms（跨方言一致性的基础）。"""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        plus_one_day = base + timedelta(days=1)
        assert to_ms(plus_one_day) - to_ms(base) == 86_400_000


class TestFormatDurationMs:
    async def test_spec_examples_exact_strings(self) -> None:
        """RATIFIED 规格字面示例：三档输出逐串锁定。"""
        assert format_duration_ms(150_228) == "2分30秒"
        assert format_duration_ms(3_450) == "3.5秒"
        assert format_duration_ms(850) == "850毫秒"

    async def test_tier_boundaries(self) -> None:
        """档位临界：999 仍毫秒档 / 1000 入秒档 / 59999 仍秒档 / 60000 入分档。"""
        assert format_duration_ms(999) == "999毫秒"
        assert format_duration_ms(1_000) == "1.0秒"
        assert format_duration_ms(59_999) == "60.0秒"
        assert format_duration_ms(60_000) == "1分"

    async def test_minutes_tier_omits_zero_seconds(self) -> None:
        """分档秒段为 0 时省略秒段；非零秒段保留整秒（毫秒残段截断）。"""
        assert format_duration_ms(120_000) == "2分"
        assert format_duration_ms(90_500) == "1分30秒"

    async def test_none_passthrough_and_negative_clamp(self) -> None:
        """None 原样返回（调用方映射「未提供」）；负数按 0 夹逼。"""
        assert format_duration_ms(None) is None
        assert format_duration_ms(-5_000) == "0毫秒"

    async def test_float_input_truncated(self) -> None:
        """float 输入按 int 截断（850.9 → 850毫秒）。"""
        assert format_duration_ms(850.9) == "850毫秒"

    async def test_units_are_parameterized(self) -> None:
        """单位文案参数化：报告层透传 locale 资源（en 形态）。"""
        assert (
            format_duration_ms(150_228, unit_ms="ms", unit_seconds="s", unit_minutes="min")
            == "2min30s"
        )
        assert format_duration_ms(850, unit_ms="ms", unit_seconds="s", unit_minutes="min") == "850ms"
        assert (
            format_duration_ms(120_000, unit_ms="ms", unit_seconds="s", unit_minutes="min")
            == "2min"
        )
