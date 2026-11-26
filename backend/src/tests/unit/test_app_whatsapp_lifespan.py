"""app 通道装配测试——WhatsApp lifespan（enabled / disabled / 停机复位）。

覆盖矩阵：

    - disabled（缺省）：零通道对象构造（router / runner / task 槽位 None）；
    - enabled：文本出站适配器注册（ChannelType.WHATSAPP）+ 有界入站
      runner / task 槽位上挂与停机复位 + 属主清单接线 + 入站登记与出站
      适配器共享同一服务窗实例；
    - 出站边界：未收到入站时适配器以受控业务错误拒绝（不发起网络调用）。

零真实网络：出站判定在 HTTP 调用之前完成；共享池 client 不实际发请求。
出站 base 指向本机不可路由端口——即便判定被绕过也会立即失败（不打真网）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.channels.channel_models import ChannelType, OutboundMessageModel
from packages.harness.channels.whatsapp.whatsapp_adapter import WhatsAppAdapter
from packages.harness.channels.whatsapp.whatsapp_runner import WhatsAppWebhookRunner
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    Settings,
    WhatsAppChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.errors import BusinessException, ErrorCode


class _RequestRecorder:
    """HTTP 请求记录器（httpx request 事件钩子——断言零网络调用）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> None:
        self.requests.append(request)


def _settings(
    tmp_path: Path,
    *,
    enabled: bool,
    owner_numbers: str = "",
) -> Settings:
    """构造隔离 Settings（tmp SQLite + WhatsApp 开关面；base 本机不可路由）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-whatsapp.db")),
        channels=ChannelsSettings(
            whatsapp=WhatsAppChannelSettings(
                enabled=enabled,
                verify_token="test-only-verify-token",
                app_secret="test-only-app-secret",
                access_token="test-only-access-token",
                phone_number_id="123456",
                api_base="http://127.0.0.1:9",
                owner_numbers=owner_numbers,
            )
        ),
    )


class TestDisabledByDefault:
    async def test_whatsapp_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """WhatsApp disabled（全通道关闭）：零通道对象构造 + 停机幂等。"""
        app = create_app()
        settings = _settings(tmp_path, enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_router is None
            assert app.state.channels_whatsapp_runner is None
            assert app.state.channels_whatsapp_task is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_router is None


class TestEnabledWiring:
    async def test_whatsapp_enabled_registers_adapter_runner_and_window(
        self, tmp_path: Path
    ) -> None:
        """enabled：适配器 + 有界 runner / 托管任务 + 共享服务窗 + 配置接线。"""
        app = create_app()
        settings = _settings(
            tmp_path, enabled=True, owner_numbers="+86 138 0013 8000,妈妈"
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        runner = app.state.channels_whatsapp_runner
        task = app.state.channels_whatsapp_task
        try:
            router = app.state.channels_router
            assert router is not None
            assert router.active_channels == [ChannelType.WHATSAPP]
            adapter = router.get_adapter(ChannelType.WHATSAPP)
            assert isinstance(adapter, WhatsAppAdapter)
            assert isinstance(runner, WhatsAppWebhookRunner)
            assert runner._owner_numbers == frozenset({"8613800138000", "妈妈"})
            assert adapter.service_window is runner.service_window
            assert runner.service_window.last_inbound_ms("8613800138000") is None
            recorder = _RequestRecorder()
            adapter.client.http_client.event_hooks["request"].append(recorder)
            with pytest.raises(BusinessException) as exc_info:
                await adapter.send_message(
                    OutboundMessageModel(chat_id="wa:8613800138000", text="主动招呼")
                )
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
            assert recorder.requests == []
            runner.service_window.record_inbound("8613800138000", 1_731_000_000_000)
            assert adapter.service_window.last_inbound_ms("8613800138000") == 1_731_000_000_000
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
        finally:
            await shutdown_app_state(app)
        assert runner.stopping is True
        assert app.state.channels_whatsapp_runner is None
        assert app.state.channels_whatsapp_task is None
        assert app.state.channels_router is None
