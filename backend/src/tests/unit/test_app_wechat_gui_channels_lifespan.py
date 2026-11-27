"""app 通道装配测试——GUI 微信实验轨 lifespan（默认关闭 / 装配 / 跳过）。

零真实网络：装配期 ``/health`` 探测经 monkeypatch 固定（健康 / 不可达两态），
runner 由 fake 替换（run 等待停机事件）；视觉解析同样只在 runner 内被调用。
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
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    Settings,
    TelegramSettings,
    WechatGuiSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from tests.async_wait import wait_until


def _settings(
    tmp_path: Path,
    *,
    enabled: bool,
    token: str = "agent-token",
    contacts_json: str = '[{"remark_name": "小林"}]',
) -> Settings:
    """构造隔离 Settings（tmp SQLite + GUI 微信通道开关）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-wechat-gui.db")),
        channels=ChannelsSettings(
            telegram=TelegramSettings(enabled=False),
            wechat_gui=WechatGuiSettings(
                enabled=enabled,
                sandbox_url="http://127.0.0.1:8091",
                token=token,
                contacts_json=contacts_json,
                poll_interval_seconds=7.0,
                backoff_base_seconds=3.0,
                backoff_max_seconds=30.0,
                max_messages_per_poll=12,
            ),
        ),
    )


class FakeWechatGuiRunner:
    """fake runner——记录构造参数 / 停机请求 / run 生命周期。"""

    instances: list[FakeWechatGuiRunner] = []

    def __init__(
        self,
        *,
        client: Any,
        reader: Any,
        router: ChannelRouter,
        contacts: dict[str, Any],
        poll_interval_seconds: float,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        max_messages_per_poll: int,
        title_mismatch_action: Any,
        gui_lock: Any,
        window_state: Any,
    ) -> None:
        self.client = client
        self.reader = reader
        self.router = router
        self.contacts = contacts
        self.gui_lock = gui_lock
        self.window_state = window_state
        self.poll_interval_seconds = poll_interval_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.max_messages_per_poll = max_messages_per_poll
        self.title_mismatch_action = title_mismatch_action
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeWechatGuiRunner.instances.append(self)

    def request_stop(self) -> None:
        """fake 停机请求（放行 run）。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake 轮询循环（await 停机事件）。"""
        self.ran = True
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（适配器协议满足——真实装配注册的是 adapter）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECHAT_GUI, chat_id=message.chat_id, message_ids=[]
        )

    @property
    def targets(self) -> tuple[str, ...]:
        """观测面替身。"""
        return tuple(self.contacts)


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """每测试清理 fake runner 登记，并把装配期健康探测固定为健康。"""
    FakeWechatGuiRunner.instances = []

    async def _healthy(self: Any) -> bool:
        return True

    monkeypatch.setattr(app_main.WechatGuiSandboxClient, "probe_health", _healthy)


class TestDisabledByDefault:
    async def test_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """disabled：零通道对象构造 + 停机幂等。"""
        app = create_app()
        settings = _settings(tmp_path, enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_wechat_gui_runner is None
            assert app.state.channels_wechat_gui_task is None
            assert app.state.channels_router is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_wechat_gui_runner is None
        assert app.state.channels_wechat_gui_task is None


class TestEnabledWiring:
    async def test_enabled_starts_managed_runner_and_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + token 就绪：runner 任务 + 适配器注册 + 参数接线 + 停机传导。"""
        monkeypatch.setattr(app_main, "WechatGuiChannelRunner", FakeWechatGuiRunner)
        app = create_app()
        settings = _settings(tmp_path, enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            runner = app.state.channels_wechat_gui_runner
            task = app.state.channels_wechat_gui_task
            assert isinstance(runner, FakeWechatGuiRunner)
            await wait_until(lambda: runner.ran)
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            assert runner.poll_interval_seconds == 7.0
            assert runner.backoff_base_seconds == 3.0
            assert runner.backoff_max_seconds == 30.0
            assert runner.max_messages_per_poll == 12
            assert list(runner.contacts) == ["小林"]
            assert runner.client.base_url == "http://127.0.0.1:8091"
            assert runner.client.has_token is True
            assert runner.router is app.state.channels_router
            assert runner.gui_lock is not None
            router = app.state.channels_router
            assert ChannelType.WECHAT_GUI in router.active_channels
            adapter = router.get_adapter(ChannelType.WECHAT_GUI)
            assert adapter is not None
            assert adapter._gui_lock is runner.gui_lock
            assert adapter._window_state is runner.window_state
        finally:
            await shutdown_app_state(app)
        assert getattr(app.state, "channels_wechat_gui_runner", None) is None
        assert getattr(app.state, "channels_wechat_gui_task", None) is None
        assert getattr(app.state, "channels_router", None) is None
        assert runner.stop_requested
        assert task is not None and task.done() and task.exception() is None

    async def test_missing_token_skips_only_wechat_gui(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 为空：只跳过本通道——路由器照常装配（其余通道不受影响）。"""
        monkeypatch.setattr(app_main, "WechatGuiChannelRunner", FakeWechatGuiRunner)
        app = create_app()
        settings = _settings(tmp_path, enabled=True, token="")
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_wechat_gui_runner is None
            assert app.state.channels_wechat_gui_task is None
            router = app.state.channels_router
            assert router is not None
            assert ChannelType.WECHAT_GUI not in router.active_channels
        finally:
            await shutdown_app_state(app)
        assert FakeWechatGuiRunner.instances == []

    async def test_empty_contacts_still_starts_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """联系人为空：仍装配 runner（运行期零目标，配置可热改后重启生效）。"""
        monkeypatch.setattr(app_main, "WechatGuiChannelRunner", FakeWechatGuiRunner)
        app = create_app()
        settings = _settings(tmp_path, enabled=True, contacts_json="[]")
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            runner = app.state.channels_wechat_gui_runner
            assert isinstance(runner, FakeWechatGuiRunner)
            assert runner.targets == ()
        finally:
            await shutdown_app_state(app)


class TestProbeFailure:
    async def test_unreachable_health_skips_only_wechat_gui(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """装配期 /health 不可达：记录并跳过本通道（不抛出，其余通道照常）。"""
        monkeypatch.setattr(app_main, "WechatGuiChannelRunner", FakeWechatGuiRunner)

        async def _unreachable(self: Any) -> bool:
            return False

        monkeypatch.setattr(
            app_main.WechatGuiSandboxClient, "probe_health", _unreachable
        )
        app = create_app()
        settings = _settings(tmp_path, enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_wechat_gui_runner is None
            assert app.state.channels_wechat_gui_task is None
            router = app.state.channels_router
            assert router is not None
            assert ChannelType.WECHAT_GUI not in router.active_channels
        finally:
            await shutdown_app_state(app)
        assert FakeWechatGuiRunner.instances == []
