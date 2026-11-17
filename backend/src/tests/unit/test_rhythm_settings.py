"""rhythm 域配置 / 覆写 / 生效值解析测试。

覆盖矩阵（镜像 test_search_settings 范式）：RHYTHM_ 前缀逐键 env 覆写 /
缺省值 / HH:MM 形态校验 / 延迟边界越界拒绝 / burst 概率域；
PersonaRhythmOverride 可选面 + delay_scale 值域；resolve_effective
合并语义（时刻覆写优先 / 延迟缩放 / 缩放后夹逼）；Settings 聚合嵌入 +
build_isolated_settings rhythm 参数透传与缺省。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import Settings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.rhythm.core import resolve_effective
from packages.harness.rhythm.rhythm_models import (
    EffectiveRhythm,
    PersonaRhythmOverride,
    RhythmSettings,
)


class TestRhythmSettingsEnvFace:
    async def test_defaults(self) -> None:
        """缺省值：关闭 + 23:30/08:00 + 1500/45000 + 45/800 + 0.12。"""
        settings = RhythmSettings()
        assert settings.enabled is False
        assert settings.bedtime == "23:30"
        assert settings.wake_time == "08:00"
        assert settings.timezone_note == "naive-local"
        assert settings.reply_delay_min_ms == 1500
        assert settings.reply_delay_max_ms == 45000
        assert settings.typing_speed_ms_per_char == 45
        assert settings.typing_base_ms == 800
        assert settings.burst_probability == 0.12

    async def test_env_prefix_key_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RHYTHM_ 前缀逐键覆写（env 面生效）。"""
        monkeypatch.setenv("RHYTHM_ENABLED", "true")
        monkeypatch.setenv("RHYTHM_BEDTIME", "22:00")
        monkeypatch.setenv("RHYTHM_WAKE_TIME", "07:30")
        monkeypatch.setenv("RHYTHM_REPLY_DELAY_MIN_MS", "900")
        monkeypatch.setenv("RHYTHM_REPLY_DELAY_MAX_MS", "20000")
        monkeypatch.setenv("RHYTHM_TYPING_SPEED_MS_PER_CHAR", "30")
        monkeypatch.setenv("RHYTHM_TYPING_BASE_MS", "500")
        monkeypatch.setenv("RHYTHM_BURST_PROBABILITY", "0.25")
        settings = RhythmSettings()
        assert settings.enabled is True
        assert settings.bedtime == "22:00"
        assert settings.wake_time == "07:30"
        assert settings.reply_delay_min_ms == 900
        assert settings.reply_delay_max_ms == 20000
        assert settings.typing_speed_ms_per_char == 30
        assert settings.typing_base_ms == 500
        assert settings.burst_probability == 0.25

    async def test_hhmm_shape_rejected(self) -> None:
        """HH:MM 形态非法（25:00 / 8 点 / 空白）：ValidationError。"""
        with pytest.raises(ValidationError):
            RhythmSettings(bedtime="25:00")
        with pytest.raises(ValidationError):
            RhythmSettings(wake_time="8点")
        with pytest.raises(ValidationError):
            RhythmSettings(bedtime="23:60")

    async def test_delay_bounds_rejected(self) -> None:
        """延迟边界：max < min 拒绝；min < 1 拒绝（log-uniform 域）。"""
        with pytest.raises(ValidationError):
            RhythmSettings(reply_delay_min_ms=5000, reply_delay_max_ms=1500)
        with pytest.raises(ValidationError):
            RhythmSettings(reply_delay_min_ms=0)

    async def test_burst_probability_bounds(self) -> None:
        """burst 概率域（0-1）：越界拒绝 / 边界值合法。"""
        with pytest.raises(ValidationError):
            RhythmSettings(burst_probability=1.5)
        with pytest.raises(ValidationError):
            RhythmSettings(burst_probability=-0.1)
        assert RhythmSettings(burst_probability=0.0).burst_probability == 0.0
        assert RhythmSettings(burst_probability=1.0).burst_probability == 1.0


class TestPersonaRhythmOverride:
    async def test_all_optional_defaults(self) -> None:
        """全可选缺省：None / None / None（不覆写任何面）。"""
        override = PersonaRhythmOverride()
        assert override.bedtime is None
        assert override.wake_time is None
        assert override.delay_scale is None

    async def test_override_bounds(self) -> None:
        """覆写值域：delay_scale 0.5-2.0 越界拒绝；HH:MM 形态校验。"""
        assert PersonaRhythmOverride(delay_scale=0.5).delay_scale == 0.5
        assert PersonaRhythmOverride(delay_scale=2.0).delay_scale == 2.0
        with pytest.raises(ValidationError):
            PersonaRhythmOverride(delay_scale=0.4)
        with pytest.raises(ValidationError):
            PersonaRhythmOverride(delay_scale=2.1)
        with pytest.raises(ValidationError):
            PersonaRhythmOverride(bedtime="24:00")


class TestResolveEffective:
    async def test_no_override_passthrough(self) -> None:
        """无覆写：全局面直通 + 分钟物化 + delay_scale 1.0。"""
        settings = RhythmSettings()
        effective = resolve_effective(settings, None)
        assert isinstance(effective, EffectiveRhythm)
        assert effective.bedtime == "23:30"
        assert effective.wake_time == "08:00"
        assert effective.bedtime_minute == 23 * 60 + 30
        assert effective.wake_minute == 8 * 60
        assert effective.reply_delay_min_ms == 1500
        assert effective.reply_delay_max_ms == 45000
        assert effective.delay_scale == 1.0

    async def test_empty_override_passthrough(self) -> None:
        """空覆写（全 None 字段）：等价无覆写。"""
        effective = resolve_effective(RhythmSettings(), PersonaRhythmOverride())
        assert effective.bedtime == "23:30"
        assert effective.delay_scale == 1.0

    async def test_time_override_wins(self) -> None:
        """时刻覆写优先：bedtime / wake_time 换新 + 分钟随动。"""
        override = PersonaRhythmOverride(bedtime="01:00", wake_time="09:15")
        effective = resolve_effective(RhythmSettings(), override)
        assert effective.bedtime == "01:00"
        assert effective.wake_time == "09:15"
        assert effective.bedtime_minute == 60
        assert effective.wake_minute == 9 * 60 + 15

    async def test_delay_scale_applied(self) -> None:
        """温度缩放：min/max 同乘 1.5（四舍五入）+ scale 物化。"""
        override = PersonaRhythmOverride(delay_scale=1.5)
        effective = resolve_effective(RhythmSettings(), override)
        assert effective.reply_delay_min_ms == 2250
        assert effective.reply_delay_max_ms == 67500
        assert effective.delay_scale == 1.5

    async def test_delay_scale_floor_clamped(self) -> None:
        """缩放夹逼：极小边界（min=1 × 0.5）下限夹逼 ≥1ms + 上限保序。"""
        settings = RhythmSettings(reply_delay_min_ms=1, reply_delay_max_ms=2)
        effective = resolve_effective(settings, PersonaRhythmOverride(delay_scale=0.5))
        assert effective.reply_delay_min_ms == 1
        assert effective.reply_delay_max_ms >= effective.reply_delay_min_ms


class TestSettingsAggregate:
    async def test_settings_embeds_rhythm(self) -> None:
        """Settings 聚合器嵌入 rhythm 分节（缺省构造读 env 面）。"""
        settings = Settings()
        assert isinstance(settings.rhythm, RhythmSettings)
        assert settings.rhythm.enabled is False

    async def test_build_isolated_settings_passthrough(self) -> None:
        """build_isolated_settings：显式实例原样嵌入（不重读 env）。"""
        rhythm = RhythmSettings(enabled=True, bedtime="21:00")
        isolated = build_isolated_settings(rhythm=rhythm)
        assert isolated.rhythm is rhythm
        default_isolated = build_isolated_settings()
        assert default_isolated.rhythm.enabled is False
        assert default_isolated.rhythm.bedtime == "23:30"
