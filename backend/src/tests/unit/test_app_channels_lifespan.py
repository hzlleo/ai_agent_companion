"""app 通道装配测试——lifespan 通道层启停（enabled / disabled / fail-fast）。

覆盖矩阵：

    - disabled（缺省）：零通道对象构造（runner / task / router 槽位 None）；
    - enabled + fake runner：任务启动 + background_task_manager 托管 +
      优雅停机（request_stop → 任务收尾）+ 槽位复位；
    - 停机顺序：通道停机先于公共池（LIFO——runner 消费共享 HTTP client）；
    - enabled + 真实类 + token 缺失：启动期 fail fast（CHANNEL_TOKEN_MISSING）；
    - runner 任务异常终止：停机不抛（异常收割 + 日志面）；
    - runner 无视停机请求：bounded 等待超时后 cancel 兜底。

零真实网络：fake runner 类注入（真实 client/router 构造无网络副作用）；
token 缺失路径用真实类（构造期本地 env 解析，零网络）。
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
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.telegram.tg_client import TelegramClient
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    Settings,
    TelegramSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.errors import BusinessException, ErrorCode


def _settings(tmp_path: Path, *, telegram_enabled: bool) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选启用 telegram 通道）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-channels.db")),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=telegram_enabled,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                state_dir=str(tmp_path / "channels-state"),
            )
        ),
    )


class FakeRunner:
    """fake TelegramChannelRunner——记录构造参数 / 停机请求 / run 生命周期。"""

    instances: list[FakeRunner] = []

    def __init__(
        self,
        *,
        client: TelegramClient,
        router: ChannelRouter,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        state_dir: str,
    ) -> None:
        self.client = client
        self.router = router
        self.kwargs = {
            "backoff_base_seconds": backoff_base_seconds,
            "backoff_max_seconds": backoff_max_seconds,
            "state_dir": state_dir,
        }
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeRunner.instances.append(self)

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake 长轮询循环（await 停机事件——模拟在途轮询窗口）。"""
        self.ran = True
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（适配器协议满足）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


class ExplodingRunner(FakeRunner):
    """run() 即抛错的 fake（异常终态收割路径）。"""

    async def run(self) -> None:
        """模拟启动即崩溃（如 getMe 之后的致命错误）。"""
        self.ran = True
        raise RuntimeError("runner 崩溃（测试注入）")


class DelayedCrashRunner(FakeRunner):
    """run() 短暂运行后抛错的 fake（停机等待窗口内异常收尾路径）。"""

    async def run(self) -> None:
        """模拟在途崩溃（停机 wait_for 等待期间死亡）。"""
        self.ran = True
        await asyncio.sleep(0.05)
        raise RuntimeError("在途崩溃（测试注入）")


class HangingRunner(FakeRunner):
    """无视停机请求的 fake（cancel 兜底路径）。"""

    def request_stop(self) -> None:
        """记录请求但不置位事件（模拟长轮询悬挂）。"""
        self.stop_requested = True

    async def run(self) -> None:
        """永不退出（只有 cancel 能终止）。"""
        self.ran = True
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def _reset_fake_runner_instances() -> None:
    """每测试清理 fake runner 类级实例登记。"""
    FakeRunner.instances = []


class TestDisabledByDefault:
    async def test_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """disabled：零通道对象构造 + 停机幂等（槽位恒 None）。"""
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_runner is None
            assert app.state.channels_task is None
            assert app.state.channels_router is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_runner is None
        assert app.state.channels_task is None


class TestEnabledWiring:
    async def test_enabled_starts_managed_runner_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled：runner 任务启动 + 托管注册 + 适配器注册 + 真实 client/router。"""
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            runner = app.state.channels_runner
            task = app.state.channels_task
            assert isinstance(runner, FakeRunner)
            # create_task 尚未调度——让出控制点等 run() 进入在途等待
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while not runner.ran:
                assert loop.time() < deadline, "runner 任务未在期限内启动"
                await asyncio.sleep(0.01)
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            # 真实 client / router 构造 + 适配器注册（发送面可达）
            assert isinstance(runner.client, TelegramClient)
            assert isinstance(runner.router, ChannelRouter)
            assert app.state.channels_router is runner.router
            assert runner.router.active_channels == [ChannelType.TELEGRAM]
            assert runner.kwargs["state_dir"] == str(tmp_path / "channels-state")
        finally:
            await shutdown_app_state(app)
        # 停机：request_stop 已调用 + 任务收尾 + 槽位复位
        assert app.state.channels_runner is None
        assert app.state.channels_task is None
        assert app.state.channels_router is None
        assert runner.stop_requested is True
        assert task is not None and task.done() and task.exception() is None

    async def test_shutdown_order_channels_before_pools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停机顺序：channels → pools → db → thread_pool（LIFO 锁定）。"""
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)

        order: list[str] = []
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

        real_close = app_main.close_database

        async def traced_close() -> None:
            order.append("db")
            await real_close()

        monkeypatch.setattr(app_main, "close_database", traced_close)

        real_thread_pool = app_main.shutdown_shared_thread_pool

        def traced_thread_pool(wait: bool = True) -> None:
            order.append("thread_pool")
            real_thread_pool(wait)

        monkeypatch.setattr(app_main, "shutdown_shared_thread_pool", traced_thread_pool)

        await shutdown_app_state(app)
        assert order == ["channels", "pools", "db", "thread_pool"]


class TestFailurePaths:
    async def test_channels_startup_requires_started_pools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """装配顺序契约：公共池未启动时 enabled 装配 fail loud（INTERNAL_ERROR）。"""
        monkeypatch.setattr(app_main, "get_pool_manager", lambda: None)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        with pytest.raises(BusinessException) as exc_info:
            await app_main._startup_channels(app, settings)
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR

    async def test_token_missing_fails_fast_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + token env 缺失：启动期 CHANNEL_TOKEN_MISSING（真实类零网络）。"""
        monkeypatch.delenv("TELEGRAM_TEST_BOT_TOKEN", raising=False)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        with pytest.raises(BusinessException) as exc_info:
            await startup_app_state(app, settings, config_dir=tmp_path)
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
        assert "TELEGRAM_TEST_BOT_TOKEN" in exc_info.value.message
        # 前置组件已启动——停机收尾必须幂等可用
        await shutdown_app_state(app)
        assert app.state.channels_runner is None

    async def test_task_termination_harvested_on_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """runner 异常终止：停机收割终态不抛（异常只在日志面）。"""
        monkeypatch.setattr(app_main, "TelegramChannelRunner", ExplodingRunner)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        task = app.state.channels_task
        assert task is not None
        # 收割终态（return_exceptions——异常只进日志面，不向测试抛出）
        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), timeout=5.0
        )
        await shutdown_app_state(app)
        assert app.state.channels_task is None

    async def test_inflight_crash_harvested_during_shutdown_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停机等待窗口内任务崩溃：异常收割不抛（warning 日志面）。"""
        monkeypatch.setattr(app_main, "TelegramChannelRunner", DelayedCrashRunner)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        task = app.state.channels_task
        runner = app.state.channels_runner
        assert task is not None and runner is not None
        await shutdown_app_state(app)
        assert task.done()
        assert task.cancelled() is False
        assert app.state.channels_task is None

    async def test_hanging_task_cancelled_after_bounded_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """runner 无视停机请求：bounded 等待超时 → cancel 兜底（任务被取消）。"""
        monkeypatch.setattr(app_main, "TelegramChannelRunner", HangingRunner)
        monkeypatch.setattr(app_main, "_CHANNELS_STOP_TIMEOUT_SECONDS", 0.05)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        runner = app.state.channels_runner
        task = app.state.channels_task
        assert runner is not None and task is not None
        await shutdown_app_state(app)
        assert runner.stop_requested is True
        assert task.cancelled() is True
        assert app.state.channels_task is None
