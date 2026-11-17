"""rhythm 域服务门面测试——RhythmService 门判定（注入时钟 / 随机源）。

覆盖矩阵：直通形态（enabled=False 恒放行零延迟）/ 睡眠窗口内拒绝 +
建议延迟 = 距醒来毫秒数 / 清醒放行 + 延迟采样区间 / should_reply_now
布尔投影 / 缺省 now_ms 走注入时钟 / effective 观测面。
"""

from __future__ import annotations

import random
from datetime import datetime

from packages.harness.rhythm.core import resolve_effective
from packages.harness.rhythm.rhythm_models import RhythmSettings
from packages.harness.rhythm.service import RhythmService


def _at(hour: int, minute: int) -> int:
    """naive 本地时刻 → epoch 毫秒（时区无关构造——与被测同墙钟语义）。

    Args:
        hour: 小时（0-23）。
        minute: 分钟（0-59）。

    Returns:
        epoch 毫秒。
    """
    return int(datetime(2026, 1, 15, hour, minute).timestamp() * 1000)


def _service(*, enabled: bool = True, rng: random.Random | None = None) -> RhythmService:
    """构造被测服务（全局面生效节律——23:30→08:00 窗口）。

    Args:
        enabled: 门控开关。
        rng: 随机源（None = 种子化实例——确定性采样）。

    Returns:
        :class:`RhythmService` 实例。
    """
    return RhythmService(
        effective=resolve_effective(RhythmSettings(), None),
        enabled=enabled,
        rng=rng if rng is not None else random.Random(42),
    )


class TestGateReply:
    async def test_disabled_passthrough(self) -> None:
        """直通形态（enabled=False）：睡眠时段也放行 + 零延迟 + 说明文案。"""
        service = _service(enabled=False)
        gate = await service.gate_reply(_at(0, 30))
        assert gate.allowed is True
        assert gate.suggested_delay_ms == 0
        assert "未启用" in gate.reason

    async def test_sleeping_denies_with_until_wake_delay(self) -> None:
        """睡眠窗口内（00:30）：拒绝 + 建议延迟 = 距今日 08:00 的毫秒数。"""
        service = _service()
        gate = await service.gate_reply(_at(0, 30))
        assert gate.allowed is False
        assert gate.suggested_delay_ms == _at(8, 0) - _at(0, 30)
        assert "睡眠窗口" in gate.reason
        assert "23:30" in gate.reason and "08:00" in gate.reason

    async def test_awake_allows_with_sampled_delay(self) -> None:
        """清醒时段（12:00）：放行 + 延迟采样落在 [1500, 45000]。"""
        service = _service()
        gate = await service.gate_reply(_at(12, 0))
        assert gate.allowed is True
        assert 1500 <= gate.suggested_delay_ms <= 45000
        assert "清醒" in gate.reason

    async def test_bedtime_boundary_denies(self) -> None:
        """bedtime 含端点（23:30 即入睡）：拒绝。"""
        service = _service()
        gate = await service.gate_reply(_at(23, 30))
        assert gate.allowed is False

    async def test_wake_boundary_allows(self) -> None:
        """wake 不含端点（08:00 即清醒）：放行。"""
        service = _service()
        gate = await service.gate_reply(_at(8, 0))
        assert gate.allowed is True

    async def test_default_now_uses_injected_clock(self) -> None:
        """缺省 now_ms：走注入时钟（固定时钟——零真实时间依赖）。"""
        service = RhythmService(
            effective=resolve_effective(RhythmSettings(), None),
            enabled=True,
            clock=lambda: _at(0, 30),
            rng=random.Random(1),
        )
        gate = await service.gate_reply()
        assert gate.allowed is False


class TestShouldReplyNow:
    async def test_boolean_projection_of_gate(self) -> None:
        """布尔投影：睡眠 False / 清醒 True / 直通 True。"""
        sleeping = await _service().should_reply_now(_at(0, 30))
        awake = await _service().should_reply_now(_at(12, 0))
        passthrough = await _service(enabled=False).should_reply_now(_at(0, 30))
        assert sleeping is False
        assert awake is True
        assert passthrough is True


class TestObservationFace:
    async def test_effective_property_exposes_resolved(self) -> None:
        """观测面：effective 属性透出生效节律（GUI / 日志消费面）。"""
        service = _service()
        assert service.effective.bedtime == "23:30"
        assert service.effective.wake_minute == 8 * 60
