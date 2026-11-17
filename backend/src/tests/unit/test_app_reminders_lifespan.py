"""app 调度器提醒作业装配测试——扫描作业注册 / 投递面门禁 / 停机复位。

覆盖矩阵：

    - telegram 启用 + 属主会话配置（duty / dream 关闭）：调度器仅注册
      reminders 间隔作业（60s）+ reminder_service 槽位上挂；
    - telegram 启用但属主会话未配置：调度器不启动（无投递出口——提醒保持
      pending 的管理面形态）；
    - duty 启用 + telegram 关闭：作业集保持既有形态（reminders 零注册——
      既有 duty 测试断言零变更）；
    - 停机：reminder_service 槽位复位。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    DreamScheduleSettings,
    DutySettings,
    SchedulerSettings,
    Settings,
    TelegramSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.reminders.service import ReminderService


def _settings(
    tmp_path: Path,
    *,
    duty_enabled: bool = False,
    owner_chat_id: str | None = None,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + telegram enabled + 可选属主会话）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-reminders.db")),
        duty=DutySettings(enabled=duty_enabled),
        dream_schedule=DreamScheduleSettings(),
        scheduler=SchedulerSettings(tick_seconds=0.25),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=True,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                owner_chat_id=owner_chat_id,
                state_dir=str(tmp_path / "channels-state"),
            )
        ),
    )


class FakeScheduler:
    """fake 中央调度器——记录作业注册 / 停机请求 / run 生命周期。"""

    instances: list[FakeScheduler] = []

    def __init__(self, *, tick_seconds: float = 1.0, **kwargs: Any) -> None:
        """初始化（记录构造参数）。"""
        self.tick_seconds = tick_seconds
        self.kwargs = kwargs
        self.registered: dict[str, dict[str, Any]] = {}
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeScheduler.instances.append(self)

    def register_interval(self, name: str, factory: Any, *, every_seconds: float) -> None:
        """记录间隔注册。"""
        self.registered[name] = {"kind": "interval", "every_seconds": every_seconds}

    def register_daily(self, name: str, factory: Any, *, daily_at: str) -> None:
        """记录每日注册。"""
        self.registered[name] = {"kind": "daily", "daily_at": daily_at}

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake tick 循环（await 停机事件）。"""
        self.ran = True
        await self.stop_event.wait()

    @property
    def job_names(self) -> list[str]:
        """已注册作业名。"""
        return list(self.registered)


class FakeRunner:
    """fake TelegramChannelRunner（真实类零网络构造）。"""

    def __init__(self, **kwargs: Any) -> None:
        """初始化。"""
        self.kwargs = kwargs
        self.stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_event.set()

    async def run(self) -> None:
        """fake 长轮询循环。"""
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


@pytest.fixture(autouse=True)
def _reset_fake_instances() -> None:
    """每测试清理 fake 类级实例登记。"""
    FakeScheduler.instances = []


async def test_reminders_job_registered_with_owner_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """telegram + 属主会话可用：仅注册 reminders 间隔作业（60s）+ 服务槽位上挂。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
    app = create_app()
    settings = _settings(tmp_path, owner_chat_id="424242")

    await startup_app_state(app, settings, config_dir=tmp_path)
    try:
        scheduler = app.state.scheduler
        assert isinstance(scheduler, FakeScheduler)
        assert scheduler.registered == {
            "reminders": {"kind": "interval", "every_seconds": 60.0}
        }
        assert isinstance(app.state.reminder_service, ReminderService)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not scheduler.ran:
            assert loop.time() < deadline, "调度器任务未在期限内启动"
            await asyncio.sleep(0.01)
    finally:
        await shutdown_app_state(app)
    assert scheduler.stop_requested is True
    assert app.state.scheduler is None
    assert app.state.reminder_service is None


async def test_reminders_job_skipped_without_owner_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """telegram 启用但属主会话未配置：调度器不启动（无投递出口）。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
    app = create_app()
    settings = _settings(tmp_path, owner_chat_id=None)

    await startup_app_state(app, settings, config_dir=tmp_path)
    try:
        assert app.state.scheduler is None
        assert app.state.reminder_service is None
    finally:
        await shutdown_app_state(app)


async def test_duty_only_wiring_keeps_job_set_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """duty 启用 + 属主会话未配置：作业集仅 duty（reminders 零注册）。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
    app = create_app()
    settings = _settings(tmp_path, duty_enabled=True, owner_chat_id=None)

    await startup_app_state(app, settings, config_dir=tmp_path)
    try:
        scheduler = app.state.scheduler
        assert isinstance(scheduler, FakeScheduler)
        assert scheduler.job_names == ["duty"]
        assert app.state.reminder_service is None
    finally:
        await shutdown_app_state(app)
