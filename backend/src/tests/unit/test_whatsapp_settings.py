"""WhatsApp 配置与生产鉴权策略测试——默认值 / env 面 / fail-closed。"""

from __future__ import annotations

import pytest

from packages.harness.config.auth_policy import ensure_auth_policy
from packages.harness.config.models import (
    ChannelsSettings,
    RuntimeEnv,
    ServerConfig,
    Settings,
    WhatsAppChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode

#: WhatsAppChannelSettings 全量 env 面键（默认值测试清理面）。
_WHATSAPP_ENV_KEYS = (
    "WHATSAPP_ENABLED",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_API_VERSION",
    "WHATSAPP_API_BASE",
    "WHATSAPP_OWNER_NUMBERS",
)

_ADMIN_TOKEN_ENV = "ADMIN_AUTH_TOKEN"


async def test_whatsapp_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """代码默认值：关闭 / 凭据空 / Graph API 默认端点与版本 / 属主清单空。"""
    for key in _WHATSAPP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    settings = WhatsAppChannelSettings(env_file=None)
    assert settings.enabled is False
    assert settings.verify_token.get_secret_value() == ""
    assert settings.app_secret.get_secret_value() == ""
    assert settings.access_token.get_secret_value() == ""
    assert settings.phone_number_id == ""
    assert settings.api_version == "v21.0"
    assert settings.api_base == "https://graph.facebook.com"
    assert settings.owner_numbers == ""


async def test_whatsapp_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """WHATSAPP_* env 逐键覆写（字段名 → env 名映射契约）。"""
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "env-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "env-secret")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "env-access")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "999888")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v20.0")
    monkeypatch.setenv("WHATSAPP_API_BASE", "https://graph.example")
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBERS", "15551234567,妈妈")
    settings = WhatsAppChannelSettings(env_file=None)
    assert settings.enabled is True
    assert settings.verify_token.get_secret_value() == "env-verify"
    assert settings.app_secret.get_secret_value() == "env-secret"
    assert settings.access_token.get_secret_value() == "env-access"
    assert settings.phone_number_id == "999888"
    assert settings.api_version == "v20.0"
    assert settings.api_base == "https://graph.example"
    assert settings.owner_numbers == "15551234567,妈妈"


@pytest.mark.parametrize(
    "field_name", ["verify_token", "app_secret", "access_token"]
)
async def test_secret_fields_not_leaked_in_repr(field_name: str) -> None:
    """SecretStr 形态：repr / str 不泄露凭据本值。"""
    secret = "super-secret-whatsapp-value"
    settings = WhatsAppChannelSettings(**{field_name: secret})
    assert secret not in repr(settings)
    assert getattr(settings, field_name).get_secret_value() == secret


async def test_channels_section_contains_whatsapp() -> None:
    """ChannelsSettings 聚合面持有 WhatsApp 子配置。"""
    channels = ChannelsSettings()
    assert channels.whatsapp.enabled is False
    assert isinstance(channels.whatsapp, WhatsAppChannelSettings)


def _prod_settings(*, enabled: bool, **overrides: str) -> Settings:
    """构造生产语义的隔离 Settings（显式值，不依赖进程 .env）。"""
    values: dict[str, str] = {
        "verify_token": "prod-verify",
        "app_secret": "prod-secret",
        "access_token": "prod-access",
        "phone_number_id": "123456",
    }
    values.update(overrides)
    return build_isolated_settings(
        server=ServerConfig(admin_token_env=_ADMIN_TOKEN_ENV),
        channels=ChannelsSettings(
            whatsapp=WhatsAppChannelSettings(enabled=enabled, **values)
        ),
    ).model_copy(update={"env": RuntimeEnv.PROD})


async def test_prod_with_whatsapp_disabled_does_not_require_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 通道关闭：空凭据不阻断启动。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    ensure_auth_policy(
        _prod_settings(
            enabled=False,
            verify_token="",
            app_secret="",
            access_token="",
            phone_number_id="",
        )
    )


@pytest.mark.parametrize(
    ("field_name", "env_name"),
    [
        ("verify_token", "WHATSAPP_VERIFY_TOKEN"),
        ("app_secret", "WHATSAPP_APP_SECRET"),
        ("access_token", "WHATSAPP_ACCESS_TOKEN"),
        ("phone_number_id", "WHATSAPP_PHONE_NUMBER_ID"),
    ],
)
async def test_prod_with_whatsapp_missing_credential_rejects_startup(
    monkeypatch: pytest.MonkeyPatch, field_name: str, env_name: str
) -> None:
    """生产 + WhatsApp 启用 + 任一必填为空：拒绝启动（CONFIG_INVALID）。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    with pytest.raises(BusinessException) as exc_info:
        ensure_auth_policy(_prod_settings(enabled=True, **{field_name: ""}))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert env_name in exc_info.value.message


async def test_prod_with_whatsapp_full_credentials_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 四项凭据齐全：通过。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    ensure_auth_policy(_prod_settings(enabled=True))
