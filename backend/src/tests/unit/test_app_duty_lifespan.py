"""app 调度器装配测试——lifespan 值班 / 梦境调度启停（enabled / disabled / 顺序）。

覆盖矩阵：

    - 全 disabled（缺省）：零调度器对象构造（scheduler / task / duty 槽位
      None）；
    - duty enabled（FakeScheduler 注入）：作业注册（duty 每日 + 时刻 +
      tick 透传）+ 任务启动 + background_task_manager 托管 + 优雅停机 +
      槽位复位；工具注册表装配（内置工具面 + search_memory 经检索服务
      + look_at 工具经装配网关注册）；
    - dream only enabled：dream 作业注册（duty 槽位保持 None）；
    - 真实 Scheduler 烟雾装配（daily_at 取一小时后——测试窗口内零触发）；
    - 停机顺序：调度器先于通道（LIFO——duty 出站消费通道路由器）；
    - 通道联动：telegram enabled 时 duty 服务持有路由器（出站可达），
      通道关闭时路由器 None（报告仅落记忆——文档化形态）。

零真实网络：FakeScheduler 类注入（真实装配对象零调用副作用）；真实
Scheduler 烟雾用一小时后的 daily_at 保证测试窗口内零触发。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
from packages.harness.core.background_tasks import background_task_manager
from tests.async_wait import wait_until


def _settings(
    tmp_path: Path,
    *,
    duty: DutySettings | None = None,
    dream: DreamScheduleSettings | None = None,
    scheduler: SchedulerSettings | None = None,
    telegram_enabled: bool = False,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选调度 / 通道分节）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-duty.db")),
        duty=duty if duty is not None else DutySettings(),
        dream_schedule=dream if dream is not None else DreamScheduleSettings(),
        scheduler=scheduler if scheduler is not None else SchedulerSettings(),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=telegram_enabled,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                state_dir=str(tmp_path / "channels-state"),
            )
        ),
    )


class FakeScheduler:
    """fake 中央调度器——记录注册 / 停机请求 / run 生命周期。"""

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
    """fake TelegramChannelRunner（通道联动测试——真实类零网络构造）。"""

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
def _reset_fake_scheduler_instances() -> None:
    """每测试清理 fake scheduler 类级实例登记。"""
    FakeScheduler.instances = []


class TestDisabledByDefault:
    async def test_all_disabled_is_noop(self, tmp_path: Path) -> None:
        """全 disabled：零调度器对象构造 + 停机幂等（槽位恒 None）。"""
        app = create_app()
        settings = _settings(tmp_path)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.scheduler is None
            assert app.state.scheduler_task is None
            assert app.state.duty_service is None
            assert app.state.duty_registry is None
        finally:
            await shutdown_app_state(app)
        assert app.state.scheduler is None
        assert app.state.scheduler_task is None


class TestEnabledWiring:
    async def test_duty_enabled_registers_daily_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """duty enabled：FakeScheduler 注册 duty 每日作业 + tick 透传 + 托管任务。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        app = create_app()
        settings = _settings(
            tmp_path,
            duty=DutySettings(enabled=True, daily_at="21:30", max_tool_steps=3),
            scheduler=SchedulerSettings(tick_seconds=0.25),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            scheduler = app.state.scheduler
            task = app.state.scheduler_task
            assert isinstance(scheduler, FakeScheduler)
            assert scheduler.tick_seconds == 0.25
            assert scheduler.registered["duty"] == {"kind": "daily", "daily_at": "21:30"}
            assert scheduler.job_names == ["duty"]
            # create_task 尚未调度——让出控制点等 run() 进入在途等待
            await wait_until(lambda: scheduler.ran, description="调度器任务进入等待")
            assert scheduler.ran is True
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            # 工具注册表装配：内置工具面（get_current_time + search_memory）
            # + look_at 工具（生产接线——装配网关构建服务后注册）
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == ["get_current_time", "search_memory", "look_at"]
            assert app.state.duty_service is not None
        finally:
            await shutdown_app_state(app)
        assert scheduler.stop_requested is True
        assert task is not None and task.done() and task.exception() is None
        assert app.state.scheduler is None
        assert app.state.scheduler_task is None
        assert app.state.duty_service is None

    async def test_dream_only_enabled_registers_dream_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dream only enabled：仅 dream 作业（duty 槽位保持 None）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        app = create_app()
        settings = _settings(
            tmp_path,
            dream=DreamScheduleSettings(enabled=True, daily_at="03:30"),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            scheduler = app.state.scheduler
            assert isinstance(scheduler, FakeScheduler)
            assert scheduler.job_names == ["dream"]
            assert scheduler.registered["dream"]["daily_at"] == "03:30"
            assert app.state.duty_service is None
            assert app.state.duty_registry is None
        finally:
            await shutdown_app_state(app)

    async def test_real_scheduler_smoke_wiring(self, tmp_path: Path) -> None:
        """真实 Scheduler 烟雾：装配 + 启动 + 优雅停机（daily_at 一小时后
        ——测试窗口内零触发，零网关调用）。"""
        daily_at = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")
        app = create_app()
        settings = _settings(
            tmp_path,
            duty=DutySettings(enabled=True, daily_at=daily_at),
            scheduler=SchedulerSettings(tick_seconds=0.05),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        scheduler = app.state.scheduler
        task = app.state.scheduler_task
        try:
            # create_task 尚未调度——让出控制点等 run() 进入运行态
            await wait_until(
                lambda: scheduler is not None and scheduler.started is True,
                description="调度器进入运行态",
            )
            assert scheduler is not None and scheduler.started is True
            assert scheduler.job_names == ["duty"]
            assert task is not None and not task.done()
        finally:
            await shutdown_app_state(app)
        assert scheduler is not None and scheduler.started is False
        assert task is not None and task.done() and task.exception() is None


class TestChannelsInteraction:
    async def test_duty_with_channels_holds_router(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """通道联动：telegram enabled 时 duty 服务持有通道路由器（出站可达）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        app = create_app()
        settings = _settings(
            tmp_path,
            duty=DutySettings(enabled=True),
            telegram_enabled=True,
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            duty_service = app.state.duty_service
            assert duty_service is not None
            assert app.state.channels_router is not None
            assert duty_service._channel_router is app.state.channels_router
            assert duty_service._owner_chat_id is None  # 属主未配置——仅落记忆形态
        finally:
            await shutdown_app_state(app)

    async def test_shutdown_order_scheduler_before_channels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停机顺序：scheduler → channels → pools（LIFO——duty 出站先收口）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        app = create_app()
        settings = _settings(
            tmp_path,
            duty=DutySettings(enabled=True),
            telegram_enabled=True,
        )
        await startup_app_state(app, settings, config_dir=tmp_path)

        order: list[str] = []
        real_scheduler_stop = app_main._shutdown_scheduler

        async def traced_scheduler_stop(app_obj: Any) -> None:
            order.append("scheduler")
            await real_scheduler_stop(app_obj)

        monkeypatch.setattr(app_main, "_shutdown_scheduler", traced_scheduler_stop)

        real_channels_stop = app_main._shutdown_channels

        async def traced_channels_stop(app_obj: Any) -> None:
            order.append("channels")
            await real_channels_stop(app_obj)

        monkeypatch.setattr(app_main, "_shutdown_channels", traced_channels_stop)

        manager = app_main.get_pool_manager()
        assert manager is not None
        real_stop = manager.stop

        async def traced_stop() -> None:
            order.append("pools")
            await real_stop()

        manager.stop = traced_stop

        await shutdown_app_state(app)
        assert order == ["scheduler", "channels", "pools"]
