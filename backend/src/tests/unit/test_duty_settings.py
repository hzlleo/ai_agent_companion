"""harness/config duty / dream_schedule / scheduler 分节测试。

覆盖面：env 面逐键映射（DUTY_ / DREAM_SCHEDULE_ / SCHEDULER_ 前缀）；
默认值（enabled 全关闭 + 正典时刻 + 步数 8 + tick 1.0）；daily_at 形态
校验（合法 strip / 非法拒绝）；charter_text 空串归一 None；max_tool_steps
边界；Settings 聚合器嵌入 + build_isolated_settings 透传。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    DreamScheduleSettings,
    DutySettings,
    SchedulerSettings,
    Settings,
)
from packages.harness.config.settings import build_isolated_settings


class TestDutySettings:
    async def test_defaults_disabled(self) -> None:
        """默认值：关闭 + 22:00 + 步数 8 + 投递开启 + 无覆写。"""
        cfg = DutySettings()
        assert cfg.enabled is False
        assert cfg.daily_at == "22:00"
        assert cfg.max_tool_steps == 8
        assert cfg.report_to_owner is True
        assert cfg.charter_text is None

    async def test_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 面逐键映射（DUTY_ 前缀）。"""
        monkeypatch.setenv("DUTY_ENABLED", "true")
        monkeypatch.setenv("DUTY_DAILY_AT", "21:30")
        monkeypatch.setenv("DUTY_MAX_TOOL_STEPS", "12")
        monkeypatch.setenv("DUTY_REPORT_TO_OWNER", "false")
        monkeypatch.setenv("DUTY_CHARTER_TEXT", "自定义任务书")
        cfg = DutySettings()
        assert cfg.enabled is True
        assert cfg.daily_at == "21:30"
        assert cfg.max_tool_steps == 12
        assert cfg.report_to_owner is False
        assert cfg.charter_text == "自定义任务书"

    async def test_empty_charter_text_normalizes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """charter_text 空串 / 纯空白：归一 None（未覆写语义）。"""
        monkeypatch.setenv("DUTY_CHARTER_TEXT", "   ")
        assert DutySettings().charter_text is None
        assert DutySettings(charter_text="").charter_text is None

    async def test_daily_at_validation(self) -> None:
        """daily_at：合法 strip 归一；非法形态与越界拒绝。"""
        assert DutySettings(daily_at=" 09:05 ").daily_at == "09:05"
        for bad in ["24:00", "9:5", "09:60", "0900", ""]:
            with pytest.raises(ValidationError):
                DutySettings(daily_at=bad)

    async def test_max_tool_steps_bounds(self) -> None:
        """max_tool_steps：1-64 边界（越界拒绝）。"""
        assert DutySettings(max_tool_steps=1).max_tool_steps == 1
        assert DutySettings(max_tool_steps=64).max_tool_steps == 64
        with pytest.raises(ValidationError):
            DutySettings(max_tool_steps=0)
        with pytest.raises(ValidationError):
            DutySettings(max_tool_steps=65)


class TestDreamScheduleSettings:
    async def test_defaults_disabled(self) -> None:
        """默认值：关闭 + 03:00。"""
        cfg = DreamScheduleSettings()
        assert cfg.enabled is False
        assert cfg.daily_at == "03:00"

    async def test_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 面逐键映射（DREAM_SCHEDULE_ 前缀）。"""
        monkeypatch.setenv("DREAM_SCHEDULE_ENABLED", "true")
        monkeypatch.setenv("DREAM_SCHEDULE_DAILY_AT", "04:15")
        cfg = DreamScheduleSettings()
        assert cfg.enabled is True
        assert cfg.daily_at == "04:15"

    async def test_daily_at_validation(self) -> None:
        """daily_at：非法形态拒绝（与 duty 同规则）。"""
        with pytest.raises(ValidationError):
            DreamScheduleSettings(daily_at="25:00")


class TestSchedulerSettings:
    async def test_defaults(self) -> None:
        """默认值：tick 1.0 秒。"""
        assert SchedulerSettings().tick_seconds == 1.0

    async def test_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 面映射（SCHEDULER_ 前缀）。"""
        monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "0.25")
        assert SchedulerSettings().tick_seconds == 0.25

    async def test_tick_seconds_positive(self) -> None:
        """tick_seconds：非正拒绝。"""
        with pytest.raises(ValidationError):
            SchedulerSettings(tick_seconds=0)


class TestAggregation:
    async def test_settings_embeds_sections(self) -> None:
        """Settings 聚合器：三分节嵌入（默认关闭形态）。"""
        cfg = Settings()
        assert isinstance(cfg.duty, DutySettings)
        assert isinstance(cfg.dream_schedule, DreamScheduleSettings)
        assert isinstance(cfg.scheduler, SchedulerSettings)
        assert cfg.duty.enabled is False
        assert cfg.dream_schedule.enabled is False

    async def test_build_isolated_settings_passthrough(self) -> None:
        """build_isolated_settings：显式分节原样嵌入。"""
        cfg = build_isolated_settings(
            duty=DutySettings(enabled=True, daily_at="21:00", max_tool_steps=3),
            dream_schedule=DreamScheduleSettings(enabled=True),
            scheduler=SchedulerSettings(tick_seconds=0.5),
        )
        assert cfg.duty.enabled is True
        assert cfg.duty.daily_at == "21:00"
        assert cfg.duty.max_tool_steps == 3
        assert cfg.dream_schedule.enabled is True
        assert cfg.scheduler.tick_seconds == 0.5

    async def test_build_isolated_settings_defaults(self) -> None:
        """build_isolated_settings：缺省分节代码默认（关闭形态）。"""
        cfg = build_isolated_settings()
        assert cfg.duty.enabled is False
        assert cfg.dream_schedule.enabled is False
        assert cfg.scheduler.tick_seconds == 1.0

    async def test_nested_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """嵌套覆写形态：DUTY__DAILY_AT 进程环境变量优先。"""
        monkeypatch.setenv("DUTY__DAILY_AT", "20:00")
        cfg = Settings()
        assert cfg.duty.daily_at == "20:00"
