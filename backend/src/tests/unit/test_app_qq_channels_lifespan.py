"""app 通道装配测试——QQ 通道 lifespan（enabled / disabled / warn-and-continue）。

覆盖矩阵：

    - disabled（缺省）：零通道对象构造（router / runner / task 槽位 None）；
    - enabled + fake 客户端：适配器注册（ChannelType.QQ）+ 启动校验
      get_login_info 恰一次 + 无 runner / task 槽位（推送式入站——无
      长驻任务）+ 出站发送面经注册适配器可达 + 停机幂等；
    - 启动校验失败：warn-and-continue（启动不抛 + 适配器仍注册——与
      telegram token 缺失 fail loud 的差异面）；
    - QQ + telegram 双启用：同一 ChannelRouter 服务双通道（会话按
      channel:chat_id 隔离）+ telegram runner 任务照常启动；
    - 装配顺序契约：公共池未启动 → INTERNAL_ERROR（任一通道启用面）。

零真实网络：fake NapCatOneBotClient 类注入（真实适配器 + 真实 webhook
处理器承载；真实 client 构造零网络但 get_login_info 会触达部署机地址
——enabled 路径一律 fake）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.channels.channel_models import (
    ChannelContextModel,
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.qq.qq_adapter import NapCatQQAdapter
from packages.harness.channels.qq.qq_models import (
    Ob11ApiResponseModel,
    Ob11LoginInfoModel,
)
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    QqChannelSettings,
    Settings,
    TelegramSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.pools.manager import PoolManager


class FakeNapCatClient:
    """fake NapCatOneBotClient——记录构造参数 / 登录校验 / 发送形状。"""

    instances: list[FakeNapCatClient] = []

    def __init__(
        self,
        http_client: Any,
        *,
        base_url: str = "http://127.0.0.1:3000",
        access_token_env: str = "QQ_ACCESS_TOKEN",
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self.http_client = http_client
        self.kwargs = {
            "base_url": base_url,
            "access_token_env": access_token_env,
            "request_timeout_seconds": request_timeout_seconds,
        }
        self.login_calls = 0
        self.login_error: Exception | None = None
        self.private_calls: list[dict[str, Any]] = []
        self.group_calls: list[dict[str, Any]] = []
        FakeNapCatClient.instances.append(self)

    async def get_login_info(self) -> Ob11LoginInfoModel:
        """fake 登录校验（可编程抛错——warn-and-continue 测试面）。"""
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error
        return Ob11LoginInfoModel(user_id=123456789, nickname="小鱼机器人")

    async def send_private_msg(self, user_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 私聊发送（记录出站形状）。"""
        self.private_calls.append({"user_id": user_id, "text": text})
        return Ob11ApiResponseModel(status="ok", retcode=0, data={"message_id": -1})

    async def send_group_msg(self, group_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 群聊发送（记录出站形状）。"""
        self.group_calls.append({"group_id": group_id, "text": text})
        return Ob11ApiResponseModel(status="ok", retcode=0, data={"message_id": -2})


class FakeTelegramRunner:
    """fake TelegramChannelRunner——最小面（run 等待停机事件 + 发送面）。"""

    def __init__(self, *, client: Any, router: ChannelRouter, **kwargs: Any) -> None:
        self.client = client
        self.router = router
        self.kwargs = kwargs
        self.stop_event = asyncio.Event()
        self.stop_requested = False

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake 长轮询循环（await 停机事件）。"""
        await self.stop_event.wait()

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（适配器协议满足）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


@pytest.fixture(autouse=True)
def _reset_fake_client_instances() -> None:
    """每测试清理 fake 客户端类级实例登记。"""
    FakeNapCatClient.instances = []


def _settings(tmp_path: Path, *, qq_enabled: bool, telegram_enabled: bool = False) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选启用 QQ / telegram 通道）。

    Args:
        tmp_path: pytest 内建一次性目录。
        qq_enabled: 是否启用 QQ 通道。
        telegram_enabled: 是否启用 Telegram 通道（双启用测试用）。

    Returns:
        隔离 :class:`Settings` 实例。
    """
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-qq-channels.db")),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=telegram_enabled,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                state_dir=str(tmp_path / "channels-state"),
            ),
            qq=QqChannelSettings(
                enabled=qq_enabled,
                access_token_env="QQ_TEST_ACCESS_TOKEN",
                webhook_token_env="QQ_TEST_WEBHOOK_TOKEN",
            ),
        ),
    )


class TestDisabledByDefault:
    async def test_qq_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """QQ disabled（telegram 亦关）：零通道对象构造 + 停机幂等。"""
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_router is None
            assert app.state.channels_runner is None
            assert app.state.channels_task is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_router is None


class TestEnabledWiring:
    async def test_qq_enabled_registers_adapter_without_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled：适配器注册 + 启动校验恰一次 + 无 runner / task 槽位。"""
        monkeypatch.setattr(app_main, "NapCatOneBotClient", FakeNapCatClient)
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            router = app.state.channels_router
            assert isinstance(router, ChannelRouter)
            assert app.state.channels_runner is None
            assert app.state.channels_task is None
            adapter = router.get_adapter(ChannelType.QQ)
            assert isinstance(adapter, NapCatQQAdapter)
            assert router.active_channels == [ChannelType.QQ]
            fake_client = FakeNapCatClient.instances[0]
            assert fake_client.kwargs["base_url"] == "http://127.0.0.1:3000"
            assert fake_client.kwargs["access_token_env"] == "QQ_TEST_ACCESS_TOKEN"
            assert fake_client.login_calls == 1
            # 出站发送面经注册适配器可达（注册面契约——非仅构造）
            result = await adapter.send_message(
                OutboundMessageModel(chat_id="private:10086", text="你好")
            )
            assert fake_client.private_calls == [{"user_id": 10086, "text": "你好"}]
            assert result.chat_id == "private:10086"
            assert result.message_ids == ["-1"]
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_router is None

    async def test_qq_enabled_uses_shared_pool_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """共享池硬规则：QQ 客户端注入公共池 HTTP client（无私建连接）。"""
        monkeypatch.setattr(app_main, "NapCatOneBotClient", FakeNapCatClient)
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            manager = app_main.get_pool_manager()
            assert isinstance(manager, PoolManager)
            assert FakeNapCatClient.instances[0].http_client is manager.get_http_client()
        finally:
            await shutdown_app_state(app)

    async def test_qq_and_telegram_share_single_router(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """双启用：单一 ChannelRouter 服务双通道 + telegram 任务照常启动。"""
        monkeypatch.setattr(app_main, "NapCatOneBotClient", FakeNapCatClient)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeTelegramRunner)
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=True, telegram_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            router = app.state.channels_router
            assert router is not None
            assert router.active_channels == [ChannelType.TELEGRAM, ChannelType.QQ]
            task = app.state.channels_task
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            # 会话身份键天然隔离（channel:chat_id 前缀含通道名 + 目标类型）
            qq_key = router.session_key(
                ChannelContextModel(channel=ChannelType.QQ, chat_id="private:1")
            )
            tg_key = router.session_key(
                ChannelContextModel(channel=ChannelType.TELEGRAM, chat_id="1")
            )
            assert qq_key != tg_key
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_task is None


class TestWarnAndContinue:
    async def test_startup_validation_failure_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """启动校验失败（NapCat 不可达形态）：warn-and-continue——启动
        不抛 + 适配器仍注册（与 telegram token 缺失 fail loud 的差异面）。"""

        class UnreachableNapCatClient(FakeNapCatClient):
            """get_login_info 恒失败的 fake（NapCat 不可达形态）。"""

            async def get_login_info(self) -> Ob11LoginInfoModel:
                """模拟 NapCat 不可达（CHANNEL_API_ERROR——传输失败面）。"""
                self.login_calls += 1
                raise BusinessException(
                    code=ErrorCode.CHANNEL_API_ERROR, message="NapCat 不可达（测试注入）"
                )

        monkeypatch.setattr(app_main, "NapCatOneBotClient", UnreachableNapCatClient)
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=True)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            router = app.state.channels_router
            assert router is not None
            assert isinstance(router.get_adapter(ChannelType.QQ), NapCatQQAdapter)
            assert UnreachableNapCatClient.instances[0].login_calls == 1
        finally:
            await shutdown_app_state(app)


class TestOrderingContract:
    async def test_qq_startup_requires_started_pools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """装配顺序契约：公共池未启动时 QQ enabled 装配 fail loud（INTERNAL_ERROR）。"""
        monkeypatch.setattr(app_main, "NapCatOneBotClient", FakeNapCatClient)
        monkeypatch.setattr(app_main, "get_pool_manager", lambda: None)
        app = create_app()
        settings = _settings(tmp_path, qq_enabled=True)
        with pytest.raises(BusinessException) as exc_info:
            await app_main._startup_channels(app, settings)
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
