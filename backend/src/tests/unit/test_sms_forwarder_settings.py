"""SmsForwarder 配置与生产鉴权策略测试——默认值 / env 面 / fail-closed。"""

from __future__ import annotations

import pytest

from packages.harness.config.auth_policy import (
    ensure_auth_policy,
    resolve_sms_forwarder_token,
)
from packages.harness.config.models import (
    ChannelsSettings,
    RuntimeEnv,
    ServerConfig,
    Settings,
    SmsForwarderChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode

#: SmsForwarderSettings 全量 env 面键（默认值测试清理面）。
_SMS_ENV_KEYS = (
    "SMS_FORWARDER_ENABLED",
    "SMS_FORWARDER_TOKEN",
    "SMS_FORWARDER_NOTIFICATIONS_ENABLED",
    "SMS_FORWARDER_OWNER_NUMBERS",
)

_ADMIN_TOKEN_ENV = "ADMIN_AUTH_TOKEN"


async def test_sms_forwarder_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """代码默认值：关闭 / 空 token / 通知关闭 / 属主清单空。"""
    for key in _SMS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    settings = SmsForwarderChannelSettings(env_file=None)
    assert settings.enabled is False
    assert settings.token.get_secret_value() == ""
    assert settings.notifications_enabled is False
    assert settings.owner_numbers == ""


async def test_sms_forwarder_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMS_FORWARDER_* env 逐键覆写（字段名 → env 名映射契约）。"""
    monkeypatch.setenv("SMS_FORWARDER_ENABLED", "true")
    monkeypatch.setenv("SMS_FORWARDER_TOKEN", "env-token")
    monkeypatch.setenv("SMS_FORWARDER_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("SMS_FORWARDER_OWNER_NUMBERS", "13800138000,妈妈")
    settings = SmsForwarderChannelSettings(env_file=None)
    assert settings.enabled is True
    assert settings.token.get_secret_value() == "env-token"
    assert settings.notifications_enabled is True
    assert settings.owner_numbers == "13800138000,妈妈"


async def test_sms_forwarder_token_not_leaked_in_repr() -> None:
    """SecretStr 形态：repr / str 不泄露 token 本值。"""
    settings = SmsForwarderChannelSettings(token="super-secret-token")
    assert "super-secret-token" not in repr(settings)
    assert settings.token.get_secret_value() == "super-secret-token"


async def test_channels_section_contains_sms_forwarder() -> None:
    """ChannelsSettings 聚合面持有 SmsForwarder 子配置。"""
    channels = ChannelsSettings()
    assert channels.sms_forwarder.enabled is False
    assert isinstance(channels.sms_forwarder, SmsForwarderChannelSettings)


async def test_resolve_sms_forwarder_token_trims() -> None:
    """token 解析去首尾空白（空串 = 鉴权关闭）。"""
    trimmed = build_isolated_settings(
        channels=ChannelsSettings(
            sms_forwarder=SmsForwarderChannelSettings(token="  token-value  ")
        )
    )
    assert resolve_sms_forwarder_token(trimmed) == "token-value"
    empty = build_isolated_settings(
        channels=ChannelsSettings(sms_forwarder=SmsForwarderChannelSettings(token=""))
    )
    assert resolve_sms_forwarder_token(empty) == ""


def _prod_settings(*, sms_enabled: bool, sms_token: str) -> Settings:
    """构造生产语义的隔离 Settings（显式值，不依赖进程 .env）。"""
    return build_isolated_settings(
        server=ServerConfig(admin_token_env=_ADMIN_TOKEN_ENV),
        channels=ChannelsSettings(
            sms_forwarder=SmsForwarderChannelSettings(
                enabled=sms_enabled, token=sms_token
            )
        ),
    ).model_copy(update={"env": RuntimeEnv.PROD})


async def test_prod_with_sms_enabled_and_empty_token_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + SmsForwarder 启用 + 空 token：拒绝启动（CONFIG_INVALID）。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    with pytest.raises(BusinessException) as exc_info:
        ensure_auth_policy(_prod_settings(sms_enabled=True, sms_token=""))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert "SMS_FORWARDER_TOKEN" in exc_info.value.message


async def test_prod_with_sms_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产 + 非空 SmsForwarder token：通过。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    ensure_auth_policy(_prod_settings(sms_enabled=True, sms_token="prod-sms-token"))


async def test_prod_with_sms_disabled_does_not_require_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 通道关闭：空 token 不阻断启动。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    ensure_auth_policy(_prod_settings(sms_enabled=False, sms_token=""))


async def test_dev_with_sms_enabled_and_empty_token_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开发环境 + 空 token：保持「空 = 鉴权关闭」行为。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "")
    settings = build_isolated_settings(
        channels=ChannelsSettings(
            sms_forwarder=SmsForwarderChannelSettings(enabled=True, token="")
        )
    ).model_copy(update={"env": RuntimeEnv.DEV})
    ensure_auth_policy(settings)
