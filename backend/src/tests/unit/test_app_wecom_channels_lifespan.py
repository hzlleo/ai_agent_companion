"""app 通道装配测试——企微长连接 lifespan（enabled / disabled / 失败隔离 / 双通道）。

覆盖矩阵：

    - disabled（缺省）：零企微对象构造（runner / task 槽位 None）；
    - enabled + fake runner：runner 任务启动 + 托管注册 + 适配器注册
      （ChannelType.WECOM）+ 真实长连接客户端构造（BotID / 端点 / 密钥变量名
      来自 Settings）与共享 HTTP 池注入 + 停机传导（request_stop → 任务收尾
      → 槽位复位）；
    - 构造期失败隔离：密钥环境变量缺失 → 该通道跳过（其余通道 / app 正常）；
    - 双通道并存：telegram + 企微共用同一 ChannelRouter（会话按通道隔离），
      停机逐一传导。

零真实网络：fake runner 类注入（真实客户端构造零网络——连接在 run() 内建立，
而 run 被 fake 替代）；失败路径用真实客户端（构造期只读本地 env）。
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
from packages.harness.channels.wecom.wecom_client import WeComLongConnectionClient
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    Settings,
    TelegramSettings,
    WecomChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from tests.async_wait import wait_until


def _settings(
    tmp_path: Path,
    *,
    wecom_enabled: bool,
    wecom_bot_id: str = "bot-test-1",
    wecom_secret_env: str = "WECOM_TEST_SECRET",
    telegram_enabled: bool = False,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选启用企微 / telegram 通道）。

    Args:
        tmp_path: 临时目录（SQLite 文件名落盘）。
        wecom_enabled: 是否启用企微通道。
        wecom_bot_id: 企微 BotID。
        wecom_secret_env: 企微密钥环境变量名。
        telegram_enabled: 是否启用 telegram 通道。

    Returns:
        隔离 Settings 实例。
    """
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-wecom.db")),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=telegram_enabled,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                state_dir=str(tmp_path / "channels-state"),
            ),
            wecom=WecomChannelSettings(
                enabled=wecom_enabled,
                bot_id=wecom_bot_id,
                secret_env=wecom_secret_env,
                reply_window_seconds=3600.0,
            ),
        ),
    )


class FakeWeComRunner:
    """fake WeComChannelRunner——记录构造参数 / 停机请求 / run 生命周期。"""

    instances: list[FakeWeComRunner] = []

    def __init__(
        self,
        *,
        client: WeComLongConnectionClient,
        router: ChannelRouter,
        reply_window_seconds: float,
        http_client: Any,
    ) -> None:
        self.client = client
        self.router = router
        self.reply_window_seconds = reply_window_seconds
        self.http_client = http_client
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeWeComRunner.instances.append(self)

    def request_stop(self) -> None:
        """fake 停机请求（放行 run）。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake supervisor 循环（await 停机事件）。"""
        self.ran = True
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（适配器协议满足）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECOM, chat_id=message.chat_id, message_ids=[]
        )


class FakeTelegramRunner(FakeWeComRunner):
    """fake TelegramChannelRunner——最小面（run 等待停机事件）。"""

    def __init__(self, *, client: Any, router: ChannelRouter, **kwargs: Any) -> None:
        super().__init__(
            client=client, router=router, reply_window_seconds=0.0, http_client=None
        )
        self.kwargs = kwargs

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（telegram 通道类型）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    """每测试清理 fake runner 类级实例登记。"""
    FakeWeComRunner.instances = []


class TestDisabledByDefault:
    async def test_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """disabled：零企微对象构造 + 停机幂等（槽位恒 None）。"""
        app = create_app()
        settings = _settings(tmp_path, wecom_enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_wecom_runner is None
            assert app.state.channels_wecom_task is None
            assert app.state.channels_router is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_wecom_runner is None
        assert app.state.channels_wecom_task is None


class TestEnabledWiring:
    async def test_enabled_starts_managed_runner_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled：runner 任务启动 + 托管注册 + 适配器注册 + 客户端参数接线。"""
        monkeypatch.setattr(app_main, "WeComChannelRunner", FakeWeComRunner)
        app = create_app()
        settings = _settings(tmp_path, wecom_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            runner = app.state.channels_wecom_runner
            task = app.state.channels_wecom_task
            assert isinstance(runner, FakeWeComRunner)
            await wait_until(lambda: runner.ran)
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            assert isinstance(runner.client, WeComLongConnectionClient)
            assert runner.client.bot_id == "bot-test-1"
            assert runner.client.secret_env == "WECOM_TEST_SECRET"
            assert runner.client.endpoint == "wss://openws.work.weixin.qq.com"
            assert runner.reply_window_seconds == 3600.0
            assert runner.http_client is app_main.get_pool_manager().get_http_client()
            assert runner.router is app.state.channels_router
            assert ChannelType.WECOM in runner.router.active_channels
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_wecom_runner is None
        assert app.state.channels_wecom_task is None
        assert app.state.channels_router is None
        assert runner.stop_requested is True
        assert task is not None and task.done() and task.exception() is None

    async def test_credential_failure_skips_only_wecom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """密钥环境变量缺失 → 跳过企微通道（不抛——其余通道继续）。"""
        monkeypatch.setattr(app_main, "WeComChannelRunner", FakeWeComRunner)
        monkeypatch.delenv("WECOM_ABSENT_SECRET", raising=False)
        app = create_app()
        settings = _settings(
            tmp_path, wecom_enabled=True, wecom_secret_env="WECOM_ABSENT_SECRET"
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_wecom_runner is None
            assert app.state.channels_wecom_task is None
            assert app.state.channels_router is not None
            assert ChannelType.WECOM not in app.state.channels_router.active_channels
        finally:
            await shutdown_app_state(app)
        assert FakeWeComRunner.instances == []

    async def test_dual_channel_shares_router(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """telegram + 企微双启用：同一路由器 + 两通道适配器 + 停机逐一传导。"""
        monkeypatch.setattr(app_main, "WeComChannelRunner", FakeWeComRunner)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeTelegramRunner)
        app = create_app()
        settings = _settings(tmp_path, wecom_enabled=True, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        wecom_runner = app.state.channels_wecom_runner
        telegram_runner = app.state.channels_runner
        try:
            await wait_until(lambda: wecom_runner.ran and telegram_runner.ran)
            router = app.state.channels_router
            assert router is not None
            assert ChannelType.WECOM in router.active_channels
            assert ChannelType.TELEGRAM in router.active_channels
            assert wecom_runner.router is router
            assert telegram_runner.router is router
        finally:
            await shutdown_app_state(app)
        assert wecom_runner.stop_requested is True
        assert telegram_runner.stop_requested is True
