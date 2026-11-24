"""app 通道装配测试——SmsForwarder lifespan（enabled / disabled / 停机复位）。

覆盖矩阵：

    - disabled（缺省）：零通道对象构造（router / runner / task 槽位 None）；
    - enabled：仅入站适配器注册（ChannelType.SMS_FORWARDER）+ 有界入站
      runner / task 槽位上挂与停机复位 + 通知开关 / 属主清单接线；
    - 出站边界：注册适配器的发送面恒空投递（仅入站）。

零真实网络：本通道无外部客户端（手机端推送入站），装配面只消费隔离
Settings 与临时 SQLite。
"""

from __future__ import annotations

from pathlib import Path

from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.channels.channel_models import ChannelType, OutboundMessageModel
from packages.harness.channels.sms_forwarder.sms_adapter import SmsForwarderAdapter
from packages.harness.channels.sms_forwarder.sms_runner import SmsForwarderWebhookRunner
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    Settings,
    SmsForwarderChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.background_tasks import background_task_manager


def _settings(
    tmp_path: Path,
    *,
    enabled: bool,
    notifications_enabled: bool = False,
    owner_numbers: str = "",
) -> Settings:
    """构造隔离 Settings（tmp SQLite + SmsForwarder 开关面）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-sms-channels.db")),
        channels=ChannelsSettings(
            sms_forwarder=SmsForwarderChannelSettings(
                enabled=enabled,
                token="test-only-sms-forwarder-token",
                notifications_enabled=notifications_enabled,
                owner_numbers=owner_numbers,
            )
        ),
    )


class TestDisabledByDefault:
    async def test_sms_disabled_config_is_noop(self, tmp_path: Path) -> None:
        """SmsForwarder disabled（全通道关闭）：零通道对象构造 + 停机幂等。"""
        app = create_app()
        settings = _settings(tmp_path, enabled=False)
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.channels_router is None
            assert app.state.channels_sms_forwarder_runner is None
            assert app.state.channels_sms_forwarder_task is None
        finally:
            await shutdown_app_state(app)
        assert app.state.channels_router is None


class TestEnabledWiring:
    async def test_sms_enabled_registers_inbound_only_adapter_and_runner(
        self, tmp_path: Path
    ) -> None:
        """enabled：仅入站适配器 + 有界 runner / 托管任务 + 配置接线。"""
        app = create_app()
        settings = _settings(
            tmp_path,
            enabled=True,
            notifications_enabled=True,
            owner_numbers="13800138000,妈妈",
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        runner = app.state.channels_sms_forwarder_runner
        task = app.state.channels_sms_forwarder_task
        try:
            router = app.state.channels_router
            assert router is not None
            assert router.active_channels == [ChannelType.SMS_FORWARDER]
            adapter = router.get_adapter(ChannelType.SMS_FORWARDER)
            assert isinstance(adapter, SmsForwarderAdapter)
            assert isinstance(runner, SmsForwarderWebhookRunner)
            assert runner._notifications_enabled is True
            assert runner._owner_numbers == frozenset({"13800138000", "妈妈"})
            assert task is not None and not task.done()
            assert task in background_task_manager.tasks
            assert runner.pending_inbound == 0
            delivery = await adapter.send_message(
                OutboundMessageModel(chat_id="sms:13800138000", text="人格回复")
            )
            assert delivery.message_ids == []
            assert adapter.dropped_outbound == 1
        finally:
            await shutdown_app_state(app)
        assert runner.stopping is True
        assert app.state.channels_sms_forwarder_runner is None
        assert app.state.channels_sms_forwarder_task is None
        assert app.state.channels_router is None
