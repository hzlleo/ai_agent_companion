"""channels 域 Settings 测试——TELEGRAM_ env 面 / 默认值 / 校验 / 隔离构造。

零真实配置：全局单例读取根 conftest 硬覆盖的 env 值；隔离实例经
``build_isolated_settings`` 显式值构造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    ChannelsSettings,
    Settings,
    TelegramSettings,
    settings,
)
from packages.harness.config.settings import build_isolated_settings

#: TelegramSettings 全量 env 面键（S3 硬覆盖一致性 + 默认值测试清理面）。
_TELEGRAM_ENV_KEYS = (
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN_ENV",
    "TELEGRAM_API_BASE",
    "TELEGRAM_POLL_TIMEOUT_SECONDS",
    "TELEGRAM_OWNER_CHAT_ID",
    "TELEGRAM_BACKOFF_BASE_SECONDS",
    "TELEGRAM_BACKOFF_MAX_SECONDS",
    "TELEGRAM_STATE_DIR",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TEST_BOT_TOKEN",
)


async def test_settings_channels_section_maps_env() -> None:
    """channels.telegram 分节与 TELEGRAM_* env 面对齐（conftest 硬覆盖值）。"""
    telegram = settings.channels.telegram
    assert telegram.enabled is False
    assert telegram.bot_token_env == "TELEGRAM_TEST_BOT_TOKEN"
    assert telegram.api_base == "https://api.telegram.org"
    assert telegram.poll_timeout_seconds == 25
    assert telegram.owner_chat_id is None
    assert telegram.backoff_base_seconds == 1.0
    assert telegram.backoff_max_seconds == 60.0
    assert telegram.state_dir == "data/channels"


async def test_telegram_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """TelegramSettings 代码默认值（env 面全清——零 env 依赖形态）。"""
    for key in _TELEGRAM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = TelegramSettings(env_file=None)
    assert default.enabled is False
    assert default.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert default.api_base == "https://api.telegram.org"
    assert default.poll_timeout_seconds == 25
    assert default.owner_chat_id is None
    assert default.backoff_base_seconds == 1.0
    assert default.backoff_max_seconds == 60.0
    assert default.state_dir == "data/channels"


async def test_telegram_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """TELEGRAM_* env 逐键覆写（字段名 → env 名映射契约）。"""
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "50")
    monkeypatch.setenv("TELEGRAM_BACKOFF_BASE_SECONDS", "0.5")
    monkeypatch.setenv("TELEGRAM_BACKOFF_MAX_SECONDS", "30")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", "/tmp/tg-state")
    telegram = TelegramSettings(env_file=None)
    assert telegram.enabled is True
    assert telegram.poll_timeout_seconds == 50
    assert telegram.backoff_base_seconds == 0.5
    assert telegram.backoff_max_seconds == 30.0
    assert telegram.state_dir == "/tmp/tg-state"


async def test_telegram_owner_chat_id_empty_string_normalizes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """owner_chat_id env 空字符串归一 None（未配置语义）。"""
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "")
    assert TelegramSettings(env_file=None).owner_chat_id is None
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "12345")
    assert TelegramSettings(env_file=None).owner_chat_id == "12345"


async def test_telegram_backoff_bounds_validation() -> None:
    """退避边界校验：backoff_max < backoff_base 拒绝（装配错误面）。"""
    with pytest.raises(ValidationError):
        TelegramSettings(
            backoff_base_seconds=10.0, backoff_max_seconds=1.0, env_file=None
        )


async def test_telegram_poll_timeout_bounds_validation() -> None:
    """长轮询超时边界：Bot API 上限 50 秒（越界拒绝）。"""
    with pytest.raises(ValidationError):
        TelegramSettings(poll_timeout_seconds=51, env_file=None)
    with pytest.raises(ValidationError):
        TelegramSettings(poll_timeout_seconds=0, env_file=None)


async def test_build_isolated_settings_channels_passthrough() -> None:
    """隔离构造：channels 分节显式透传 + 缺省回退（telegram 关闭形态）。"""
    channels = ChannelsSettings(telegram=TelegramSettings(enabled=True))
    isolated = build_isolated_settings(channels=channels)
    assert isolated.channels is channels
    assert isolated.channels.telegram.enabled is True

    default_isolated = build_isolated_settings()
    assert default_isolated.channels.telegram.enabled is False


async def test_settings_nested_delimiter_overrides_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CHANNELS__TELEGRAM__*`` 嵌套覆写形态（Settings env_nested_delimiter）。"""
    monkeypatch.setenv("CHANNELS__TELEGRAM__ENABLED", "true")
    monkeypatch.setenv("CHANNELS__TELEGRAM__POLL_TIMEOUT_SECONDS", "10")
    fresh = Settings(env_file=None)
    assert fresh.channels.telegram.enabled is True
    assert fresh.channels.telegram.poll_timeout_seconds == 10
