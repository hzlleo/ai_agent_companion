"""企微长连接通道 Settings 测试——WECOM_ env 面 / 默认值 / 不变式 / 隔离构造。

零真实配置：全局单例读取根 conftest 硬覆盖值；隔离实例经显式字段构造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    ChannelsSettings,
    Settings,
    WecomChannelSettings,
    settings,
)
from packages.harness.config.settings import build_isolated_settings

#: WecomChannelSettings 全量 env 面键 + 密钥本值变量（S3 硬覆盖一致性 +
#: 默认值测试清理面）。
_WECOM_ENV_KEYS = (
    "WECOM_ENABLED",
    "WECOM_BOT_ID",
    "WECOM_SECRET_ENV",
    "WECOM_WS_ENDPOINT",
    "WECOM_REQUEST_TIMEOUT_SECONDS",
    "WECOM_HEARTBEAT_INTERVAL_SECONDS",
    "WECOM_HEARTBEAT_TIMEOUT_SECONDS",
    "WECOM_OPEN_TIMEOUT_SECONDS",
    "WECOM_BACKOFF_BASE_SECONDS",
    "WECOM_BACKOFF_MAX_SECONDS",
    "WECOM_BACKOFF_JITTER_RATIO",
    "WECOM_REPLY_WINDOW_SECONDS",
    "WECOM_SECRET",
    "WECOM_TEST_SECRET",
)


async def test_settings_channels_wecom_section_maps_env() -> None:
    """channels.wecom 分节与 WECOM_* env 面对齐（conftest 硬覆盖值）。"""
    wecom = settings.channels.wecom
    assert wecom.enabled is False
    assert wecom.bot_id == ""
    assert wecom.secret_env == "WECOM_TEST_SECRET"
    assert wecom.ws_endpoint == "wss://openws.work.weixin.qq.com"
    assert wecom.request_timeout_seconds == 15.0
    assert wecom.heartbeat_interval_seconds == 30.0
    assert wecom.heartbeat_timeout_seconds == 10.0
    assert wecom.open_timeout_seconds == 10.0
    assert wecom.backoff_base_seconds == 1.0
    assert wecom.backoff_max_seconds == 60.0
    assert wecom.backoff_jitter_ratio == 0.3
    assert wecom.reply_window_seconds == 86400.0


async def test_wecom_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """WecomChannelSettings 代码默认值（env 面全清——零 env 依赖形态）。"""
    for key in _WECOM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = WecomChannelSettings(env_file=None)
    assert default.enabled is False
    assert default.bot_id == ""
    assert default.secret_env == "WECOM_SECRET"
    assert default.ws_endpoint == "wss://openws.work.weixin.qq.com"
    assert default.reply_window_seconds == 86400.0


async def test_wecom_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """WECOM_* env 逐键覆写（字段名 → env 名映射契约）。"""
    monkeypatch.setenv("WECOM_ENABLED", "true")
    monkeypatch.setenv("WECOM_BOT_ID", "bot-prod-1")
    monkeypatch.setenv("WECOM_SECRET_ENV", "WECOM_PROD_SECRET")
    monkeypatch.setenv("WECOM_WS_ENDPOINT", "wss://proxy.example.com/ws")
    monkeypatch.setenv("WECOM_HEARTBEAT_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("WECOM_BACKOFF_JITTER_RATIO", "0")
    wecom = WecomChannelSettings(env_file=None)
    assert wecom.enabled is True
    assert wecom.bot_id == "bot-prod-1"
    assert wecom.secret_env == "WECOM_PROD_SECRET"
    assert wecom.ws_endpoint == "wss://proxy.example.com/ws"
    assert wecom.heartbeat_interval_seconds == 45.0
    assert wecom.backoff_jitter_ratio == 0.0


async def test_enabled_requires_bot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """启用但 BotID 为空 → 校验拒绝（无身份无法订阅）。"""
    for key in _WECOM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        WecomChannelSettings(enabled=True, bot_id="   ", env_file=None)


async def test_backoff_bounds_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避封顶不得小于基数（装配错误面）。"""
    for key in _WECOM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        WecomChannelSettings(backoff_base_seconds=10.0, backoff_max_seconds=5.0, env_file=None)


async def test_heartbeat_budget_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳应答超时必须小于心跳间隔（否则心跳永远判定失败）。"""
    for key in _WECOM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        WecomChannelSettings(
            heartbeat_interval_seconds=10.0,
            heartbeat_timeout_seconds=10.0,
            env_file=None,
        )


async def test_jitter_ratio_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """抖动比例值域 0-1（越界拒绝）。"""
    for key in _WECOM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        WecomChannelSettings(backoff_jitter_ratio=1.5, env_file=None)


async def test_build_isolated_settings_wecom_passthrough() -> None:
    """隔离构造：channels.wecom 分节显式透传 + 缺省回退（关闭形态）。"""
    channels = ChannelsSettings(
        wecom=WecomChannelSettings(enabled=True, bot_id="bot-isolated")
    )
    isolated = build_isolated_settings(channels=channels)
    assert isolated.channels is channels
    assert isolated.channels.wecom.enabled is True
    assert isolated.channels.wecom.bot_id == "bot-isolated"
    assert isolated.channels.telegram.enabled is False

    default_isolated = build_isolated_settings()
    assert default_isolated.channels.wecom.enabled is False


async def test_settings_nested_delimiter_overrides_wecom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CHANNELS__WECOM__*`` 嵌套覆写形态（Settings env_nested_delimiter）。"""
    monkeypatch.setenv("CHANNELS__WECOM__ENABLED", "true")
    monkeypatch.setenv("CHANNELS__WECOM__BOT_ID", "bot-nested")
    monkeypatch.setenv("CHANNELS__WECOM__REPLY_WINDOW_SECONDS", "3600")
    fresh = Settings(env_file=None)
    assert fresh.channels.wecom.enabled is True
    assert fresh.channels.wecom.bot_id == "bot-nested"
    assert fresh.channels.wecom.reply_window_seconds == 3600.0
