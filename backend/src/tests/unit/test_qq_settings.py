"""QQ 通道 Settings 测试——QQ_ env 面 / 默认值 / 校验 / 隔离构造。

零真实配置：全局单例读取根 conftest 硬覆盖的 env 值；隔离实例经
``build_isolated_settings`` 显式值构造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    ChannelsSettings,
    QqChannelSettings,
    Settings,
    settings,
)
from packages.harness.config.settings import build_isolated_settings

#: QqChannelSettings 全量 env 面键 + 双 token 本值变量（S3 硬覆盖一致性
#: + 默认值测试清理面）。
_QQ_ENV_KEYS = (
    "QQ_ENABLED",
    "QQ_HTTP_API_BASE_URL",
    "QQ_ACCESS_TOKEN_ENV",
    "QQ_WEBHOOK_TOKEN_ENV",
    "QQ_REQUEST_TIMEOUT_SECONDS",
    "QQ_ACCESS_TOKEN",
    "QQ_TEST_ACCESS_TOKEN",
    "QQ_WEBHOOK_TOKEN",
    "QQ_TEST_WEBHOOK_TOKEN",
)


async def test_settings_channels_qq_section_maps_env() -> None:
    """channels.qq 分节与 QQ_* env 面对齐（conftest 硬覆盖值）。"""
    qq = settings.channels.qq
    assert qq.enabled is False
    assert qq.http_api_base_url == "http://127.0.0.1:3000"
    assert qq.access_token_env == "QQ_TEST_ACCESS_TOKEN"
    assert qq.webhook_token_env == "QQ_TEST_WEBHOOK_TOKEN"
    assert qq.request_timeout_seconds == 15.0


async def test_qq_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """QqChannelSettings 代码默认值（env 面全清——零 env 依赖形态）。"""
    for key in _QQ_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = QqChannelSettings(env_file=None)
    assert default.enabled is False
    assert default.http_api_base_url == "http://127.0.0.1:3000"
    assert default.access_token_env == "QQ_ACCESS_TOKEN"
    assert default.webhook_token_env == "QQ_WEBHOOK_TOKEN"
    assert default.request_timeout_seconds == 15.0


async def test_qq_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """QQ_* env 逐键覆写（字段名 → env 名映射契约）。"""
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("QQ_HTTP_API_BASE_URL", "http://192.168.1.10:6099")
    monkeypatch.setenv("QQ_ACCESS_TOKEN_ENV", "QQ_PROD_ACCESS_TOKEN")
    monkeypatch.setenv("QQ_WEBHOOK_TOKEN_ENV", "QQ_PROD_WEBHOOK_TOKEN")
    monkeypatch.setenv("QQ_REQUEST_TIMEOUT_SECONDS", "9.5")
    qq = QqChannelSettings(env_file=None)
    assert qq.enabled is True
    assert qq.http_api_base_url == "http://192.168.1.10:6099"
    assert qq.access_token_env == "QQ_PROD_ACCESS_TOKEN"
    assert qq.webhook_token_env == "QQ_PROD_WEBHOOK_TOKEN"
    assert qq.request_timeout_seconds == 9.5


async def test_qq_request_timeout_bounds_validation() -> None:
    """请求超时边界：非正数拒绝（装配错误面）。"""
    with pytest.raises(ValidationError):
        QqChannelSettings(request_timeout_seconds=0, env_file=None)
    with pytest.raises(ValidationError):
        QqChannelSettings(request_timeout_seconds=-1.0, env_file=None)


async def test_qq_base_url_must_be_non_empty() -> None:
    """基础 URL 非空校验（空串拒绝）。"""
    with pytest.raises(ValidationError):
        QqChannelSettings(http_api_base_url="", env_file=None)


async def test_build_isolated_settings_qq_passthrough() -> None:
    """隔离构造：channels.qq 分节显式透传 + 缺省回退（关闭形态）。"""
    channels = ChannelsSettings(qq=QqChannelSettings(enabled=True))
    isolated = build_isolated_settings(channels=channels)
    assert isolated.channels is channels
    assert isolated.channels.qq.enabled is True
    assert isolated.channels.telegram.enabled is False

    default_isolated = build_isolated_settings()
    assert default_isolated.channels.qq.enabled is False


async def test_settings_nested_delimiter_overrides_qq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CHANNELS__QQ__*`` 嵌套覆写形态（Settings env_nested_delimiter）。"""
    monkeypatch.setenv("CHANNELS__QQ__ENABLED", "true")
    monkeypatch.setenv("CHANNELS__QQ__REQUEST_TIMEOUT_SECONDS", "8")
    fresh = Settings(env_file=None)
    assert fresh.channels.qq.enabled is True
    assert fresh.channels.qq.request_timeout_seconds == 8.0
