"""app 通道装配测试——抖音 web lifespan（enabled / 底座缺失 / 失败隔离 / 停机）。

覆盖矩阵：

    - disabled（缺省）：零抖音对象构造（runner / task 槽位 None）；
    - enabled + 底座就绪：runner 任务启动 + 托管注册 + 适配器注册
      （ChannelType.DOUYIN）+ 客户端与 runner 参数接线（chat_url / 会话标识 /
      轮询与退避）+ 停机传导（request_stop → 任务收尾 → 槽位复位）；
    - 底座缺失（浏览器 sidecar 未装配 / 探测失败）：只跳过抖音通道——其余
      通道与 app 正常；
    - 与企微并存：同一路由器承载两通道适配器（会话按通道隔离）。

零真实网络：浏览器 sidecar 客户端经工厂注入 fake（探测 / invoke 全本地）；
抖音 runner 经 fake 类替换（run 等待停机事件）——真实轮询语义在
test_douyin_runner 覆盖。
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
from packages.harness.channels.douyin.douyin_client import DouyinWebClient
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    DouyinChannelSettings,
    Settings,
    TelegramSettings,
    ToolsBrowserSidecarSettings,
    WecomChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.tools.tool_models import ToolResultModel


def _settings(
    tmp_path: Path,
    *,
    douyin_enabled: bool,
    sidecar_enabled: bool = True,
    douyin_chat_url: str = "https://www.douyin.com/im",
    douyin_conversation_id: str = "c-1",
    wecom_enabled: bool = False,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选启用抖音 / 底座 / 企微）。

    Args:
        tmp_path: 临时目录（SQLite 文件名落盘）。
        douyin_enabled: 是否启用抖音通道。
        sidecar_enabled: 是否启用浏览器 sidecar 底座。
        douyin_chat_url: 会话页 URL。
        douyin_conversation_id: 目标会话标识。
        wecom_enabled: 是否启用企微通道（并存场景）。

    Returns:
        隔离 Settings 实例。
    """
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-douyin.db")),
        channels=ChannelsSettings(
            wecom=WecomChannelSettings(
                enabled=wecom_enabled, bot_id="bot-test-1", secret_env="WECOM_TEST_SECRET"
            ),
            douyin=DouyinChannelSettings(
                enabled=douyin_enabled,
                chat_url=douyin_chat_url,
                conversation_id=douyin_conversation_id,
                poll_interval_seconds=7.0,
                backoff_base_seconds=3.0,
                backoff_max_seconds=30.0,
                max_messages_per_poll=12,
            ),
            telegram=TelegramSettings(enabled=False),
        ),
        tools_browser_sidecar=ToolsBrowserSidecarSettings(
            enabled=sidecar_enabled,
            token_env="BROWSER_SIDECAR_TEST_TOKEN",
        ),
    )


class FakeDouyinRunner:
    """fake DouyinChannelRunner——记录构造参数 / 停机请求 / run 生命周期。"""

    instances: list[FakeDouyinRunner] = []

    def __init__(
        self,
        *,
        client: DouyinWebClient,
        router: ChannelRouter,
        conversation_id: str,
        poll_interval_seconds: float,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        max_messages_per_poll: int,
    ) -> None:
        self.client = client
        self.router = router
        self.conversation_id = conversation_id
        self.poll_interval_seconds = poll_interval_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.max_messages_per_poll = max_messages_per_poll
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeDouyinRunner.instances.append(self)

    def request_stop(self) -> None:
        """fake 停机请求（放行 run）。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake 轮询循环（await 停机事件）。"""
        self.ran = True
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（适配器协议满足）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.DOUYIN, chat_id=message.chat_id, message_ids=[]
        )


class FakeSidecarClient:
    """fake 浏览器 sidecar 客户端（探测 / invoke 全本地——抖音客户端消费面）。"""

    def __init__(self) -> None:
        self.probe_calls = 0
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    @property
    def has_token(self) -> bool:
        """fake 持有 token（装配前判据）。"""
        return True

    async def probe_health(self) -> bool:
        """fake 健康探测（恒 True）。"""
        self.probe_calls += 1
        return True

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """fake 工具调用（记录参数）。"""
        self.invocations.append((tool, dict(arguments)))
        return ToolResultModel(tool=tool, ok=True, result={"result": {"messages": []}})


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    """每测试清理 fake runner 类级实例登记。"""
    FakeDouyinRunner.instances = []


def _install_sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecarClient:
    """把 sidecar 客户端工厂替换为返回 fake 的工厂（抖音底座注入面）。"""
    fake = FakeSidecarClient()

    def _fake_factory(http_client: Any, settings_face: Any) -> FakeSidecarClient:
        """返回 fake 客户端（捕获形态与真实工厂同签名）。"""
        return fake

    monkeypatch.setattr(app_main, "build_browser_sidecar_client", _fake_factory)
    return fake


async def _wait_until(predicate: Any, *, timeout_seconds: float = 5.0) -> None:
    """让出控制点直到条件成立（有界等待——超时即失败）。

    Args:
        predicate: 零参判定函数。
        timeout_seconds: 等待上限秒数。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("条件在等待上限内未成立")


class TestDisabledByDefault:
    async def test_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """disabled：零抖音对象构造 + 停机幂等（槽位恒 None）。"""
        app = create_app()
        settings = _settings(tmp_path, douyin_enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_douyin_runner is None
            assert app.state.channels_douyin_task is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_douyin_runner is None
        assert app.state.channels_douyin_task is None


class TestEnabledWiring:
    async def test_enabled_starts_managed_runner_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + 底座就绪：runner 任务 + 适配器注册 + 参数接线 + 停机传导。"""
        monkeypatch.setattr(app_main, "DouyinChannelRunner", FakeDouyinRunner)
        sidecar = _install_sidecar(monkeypatch)
        app = create_app()
        settings = _settings(tmp_path, douyin_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            runner = app.state.channels_douyin_runner
            task = app.state.channels_douyin_task
            assert isinstance(runner, FakeDouyinRunner)
            await _wait_until(lambda: runner.ran)
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            assert runner.client.chat_url == "https://www.douyin.com/im"
            assert runner.conversation_id == "c-1"
            assert runner.poll_interval_seconds == 7.0
            assert runner.backoff_base_seconds == 3.0
            assert runner.backoff_max_seconds == 30.0
            assert runner.max_messages_per_poll == 12
            assert runner.router is app.state.channels_router
            assert ChannelType.DOUYIN in runner.router.active_channels
            assert sidecar.probe_calls == 1
        finally:
            await shutdown_app_state(app)
        assert getattr(app.state, "channels_douyin_runner", None) is None
        assert getattr(app.state, "channels_douyin_task", None) is None
        assert getattr(app.state, "channels_router", None) is None
        assert runner.stop_requested
        assert task is not None and task.done() and task.exception() is None

    async def test_missing_substrate_skips_only_douyin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """底座未装配（sidecar disabled）：只跳过抖音——app 与其他通道正常。"""
        monkeypatch.setattr(app_main, "DouyinChannelRunner", FakeDouyinRunner)
        app = create_app()
        settings = _settings(tmp_path, douyin_enabled=True, sidecar_enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_douyin_runner is None
            assert app.state.channels_douyin_task is None
            assert app.state.channels_router is not None
            assert ChannelType.DOUYIN not in app.state.channels_router.active_channels
        finally:
            await shutdown_app_state(app)
        assert FakeDouyinRunner.instances == []

    async def test_douyin_and_wecom_share_router(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """抖音 + 企微并存：同一路由器承载两通道适配器（按通道隔离）。"""
        monkeypatch.setattr(app_main, "DouyinChannelRunner", FakeDouyinRunner)
        monkeypatch.setattr(app_main, "WeComChannelRunner", FakeWeComRunnerStub)
        _install_sidecar(monkeypatch)
        app = create_app()
        settings = _settings(tmp_path, douyin_enabled=True, wecom_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        douyin_runner = app.state.channels_douyin_runner
        wecom_runner = app.state.channels_wecom_runner
        try:
            await _wait_until(lambda: douyin_runner.ran and wecom_runner.ran)
            router = app.state.channels_router
            assert router is not None
            assert ChannelType.DOUYIN in router.active_channels
            assert ChannelType.WECOM in router.active_channels
            assert douyin_runner.router is router
            assert wecom_runner.router is router
        finally:
            await shutdown_app_state(app)
        assert douyin_runner.stop_requested is True
        assert wecom_runner.stop_requested is True


class FakeWeComRunnerStub(FakeDouyinRunner):
    """企微 runner 替身（复用 fake 生命周期面——企微语义在专门测试覆盖）。"""

    def __init__(self, *, client: Any, router: ChannelRouter, **kwargs: Any) -> None:
        super().__init__(
            client=client,
            router=router,
            conversation_id="",
            poll_interval_seconds=1.0,
            backoff_base_seconds=1.0,
            backoff_max_seconds=2.0,
            max_messages_per_poll=1,
        )
        self.kwargs = kwargs
