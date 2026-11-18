"""harness/memory lifecycle 测试——时间衰减 / 使用强化 / 归档判定（纯数学）。

覆盖面：有效强度精确公式（半衰期边界 / 强化对数增益 / 置顶免衰减 /
``pinned_decay_exempt=False`` 的退化形态 / 未来锚点钳位 / 负数归一）；
归档判定门矩阵（仅现行 active / 置顶豁免 / 冻结豁免 / 最小年龄边界 /
阈值严格小于 / 边界不归档）；纯函数零 I/O（无 DB / 无网络）。
"""

from __future__ import annotations

import math

from packages.harness.memory.lifecycle import (
    MS_PER_DAY,
    LifecycleConfig,
    effective_strength,
    should_archive,
)
from packages.harness.memory.models import CuratedStatus

#: 注入时钟（epoch 毫秒——衰减参考轴）。
NOW = 1_760_000_000_000


async def test_effective_strength_decay_half_life_exact() -> None:
    """半衰期边界精确：恰过一个半衰期强度减半（无访问强化）。"""
    config = LifecycleConfig()
    fresh = effective_strength(4, NOW, 0, NOW, config)
    one_half_life = effective_strength(4, NOW - 30 * MS_PER_DAY, 0, NOW, config)
    two_half_lives = effective_strength(4, NOW - 60 * MS_PER_DAY, 0, NOW, config)
    assert fresh == 4.0
    assert one_half_life == 2.0
    assert two_half_lives == 1.0


async def test_effective_strength_reinforcement_log_gain() -> None:
    """使用强化：``1 + log1p(access_count) * weight`` 精确乘入。"""
    config = LifecycleConfig(reinforcement_weight=0.25)
    strength = effective_strength(4, NOW, 7, NOW, config)
    expected = 4.0 * (1.0 + math.log1p(7) * 0.25)
    assert math.isclose(strength, expected, rel_tol=1e-12)


async def test_effective_strength_pinned_decay_exempt() -> None:
    """置顶免衰减：强度不含衰减因子（强化仍生效）。"""
    config = LifecycleConfig(reinforcement_weight=0.25)
    strength = effective_strength(5, NOW - 90 * MS_PER_DAY, 3, NOW, config, pinned=True)
    expected = 5.0 * (1.0 + math.log1p(3) * 0.25)
    assert math.isclose(strength, expected, rel_tol=1e-12)


async def test_effective_strength_pinned_decay_exemption_disabled() -> None:
    """``pinned_decay_exempt=False``：置顶行仍按普通公式衰减。"""
    exempt_config = LifecycleConfig()
    plain_config = LifecycleConfig(pinned_decay_exempt=False)
    pinned = effective_strength(5, NOW - 30 * MS_PER_DAY, 0, NOW, exempt_config, pinned=True)
    decayed = effective_strength(5, NOW - 30 * MS_PER_DAY, 0, NOW, plain_config, pinned=True)
    assert pinned == 5.0
    assert decayed == 2.5


async def test_effective_strength_future_anchor_clamped() -> None:
    """未来锚点钳位：age=0（不奖励未来时间）。"""
    config = LifecycleConfig()
    assert effective_strength(3, NOW + 10 * MS_PER_DAY, 0, NOW, config) == 3.0


async def test_effective_strength_negative_inputs_normalized() -> None:
    """负数归一：重要度 / 访问次数按 0（强度恒非负）。"""
    config = LifecycleConfig()
    assert effective_strength(-2, NOW, 0, NOW, config) == 0.0
    assert effective_strength(4, NOW, -5, NOW, config) == 4.0


async def test_should_archive_requires_all_gates() -> None:
    """门矩阵：现行 active + 非置顶 + 非冻结 + 超龄 + 强度低于阈值才归档。"""
    config = LifecycleConfig()
    common = {
        "importance": 2,
        "anchor_ms": NOW - 60 * MS_PER_DAY,
        "access_count": 0,
        "pinned": False,
        "frozen": False,
        "status": CuratedStatus.ACTIVE,
        "now_ms": NOW,
        "config": config,
    }
    assert should_archive(**common) is True
    assert should_archive(**{**common, "pinned": True}) is False
    assert should_archive(**{**common, "frozen": True}) is False
    assert should_archive(**{**common, "status": CuratedStatus.SUPERSEDED}) is False
    assert should_archive(**{**common, "status": CuratedStatus.ARCHIVED}) is False


async def test_should_archive_min_age_boundary_not_archived() -> None:
    """最小年龄边界：年龄不超过阈值 → 不归档（新记忆豁免）。"""
    config = LifecycleConfig(min_age_days=7)
    common = {
        "importance": 1,
        "access_count": 0,
        "pinned": False,
        "frozen": False,
        "status": CuratedStatus.ACTIVE,
        "now_ms": NOW,
        "config": config,
    }
    assert should_archive(**{**common, "anchor_ms": NOW - 7 * MS_PER_DAY}) is False
    assert should_archive(**{**common, "anchor_ms": NOW - 8 * MS_PER_DAY}) is True


async def test_should_archive_threshold_strictly_less() -> None:
    """阈值语义严格小于：恰等于阈值不归档。"""
    config = LifecycleConfig(archive_threshold=1.0, min_age_days=7)
    exact = should_archive(
        importance=2,
        anchor_ms=NOW - 30 * MS_PER_DAY,
        access_count=0,
        pinned=False,
        frozen=False,
        status=CuratedStatus.ACTIVE,
        now_ms=NOW,
        config=config,
    )
    assert exact is False
    below = should_archive(
        importance=2,
        anchor_ms=NOW - 31 * MS_PER_DAY,
        access_count=0,
        pinned=False,
        frozen=False,
        status=CuratedStatus.ACTIVE,
        now_ms=NOW,
        config=config,
    )
    assert below is True


async def test_should_archive_reinforced_memory_survives() -> None:
    """使用强化可挽救老记忆：访问计数抬高强度越过阈值 → 不归档。"""
    config = LifecycleConfig(archive_threshold=1.0)
    archived = should_archive(
        importance=5,
        anchor_ms=NOW - 90 * MS_PER_DAY,
        access_count=0,
        pinned=False,
        frozen=False,
        status=CuratedStatus.ACTIVE,
        now_ms=NOW,
        config=config,
    )
    reinforced = should_archive(
        importance=5,
        anchor_ms=NOW - 90 * MS_PER_DAY,
        access_count=100,
        pinned=False,
        frozen=False,
        status=CuratedStatus.ACTIVE,
        now_ms=NOW,
        config=config,
    )
    assert archived is True
    assert reinforced is False


async def test_should_archive_accepts_status_string() -> None:
    """状态入参接受字符串值（ORM 行形态——非枚举构造）。"""
    config = LifecycleConfig()
    assert (
        should_archive(
            importance=1,
            anchor_ms=NOW - 90 * MS_PER_DAY,
            access_count=0,
            pinned=False,
            frozen=False,
            status="active",
            now_ms=NOW,
            config=config,
        )
        is True
    )
