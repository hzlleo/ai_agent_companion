"""生产环境鉴权启动策略测试——fail-closed 矩阵与 startup 接线。

覆盖矩阵：``ENV=prod`` 空管理面 token 拒绝 / 非空通过；开发与测试环境
保持空 token 放行；``ENV=prod`` 且 QQ 通道启用时空入站 token 拒绝；
``startup_app_state`` 在组件装配前调用策略函数（recorder 断言）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.main as app_main
from app.main import create_app, startup_app_state
from packages.harness.config.auth_policy import ensure_auth_policy
from packages.harness.config.models import (
    ChannelsSettings,
    QqChannelSettings,
    RuntimeEnv,
    ServerConfig,
    Settings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode

#: 管理面 token 的 env 间接变量名（与 conftest 硬覆盖一致）。
_ADMIN_TOKEN_ENV = "ADMIN_AUTH_TOKEN"

#: QQ 入站 token 的 env 间接变量名（conftest 硬覆盖为测试专用名）。
_QQ_WEBHOOK_TOKEN_ENV = "QQ_TEST_WEBHOOK_TOKEN"


def _prod_settings(*, qq_enabled: bool = False) -> Settings:
    """构造生产语义的隔离 Settings（显式值，不依赖进程 .env）。

    Args:
        qq_enabled: 是否启用 QQ 通道。

    Returns:
        生产环境标识的隔离 Settings。
    """
    return build_isolated_settings(
        server=ServerConfig(admin_token_env=_ADMIN_TOKEN_ENV),
        channels=ChannelsSettings(
            qq=QqChannelSettings(
                enabled=qq_enabled, webhook_token_env=_QQ_WEBHOOK_TOKEN_ENV
            )
        ),
    ).model_copy(update={"env": RuntimeEnv.PROD})


async def test_prod_with_empty_admin_token_rejects_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 空 ADMIN_AUTH_TOKEN：拒绝启动（CONFIG_INVALID + 明确文案）。"""
    monkeypatch.delenv(_ADMIN_TOKEN_ENV, raising=False)
    with pytest.raises(BusinessException) as exc_info:
        ensure_auth_policy(_prod_settings())
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert "生产环境必须设置 ADMIN_AUTH_TOKEN" in exc_info.value.message


async def test_prod_with_configured_admin_token_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + 非空 ADMIN_AUTH_TOKEN：通过（QQ 未启用不要求 webhook token）。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    monkeypatch.delenv(_QQ_WEBHOOK_TOKEN_ENV, raising=False)
    ensure_auth_policy(_prod_settings())


async def test_dev_with_empty_admin_token_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开发环境 + 空 token：保持既有「空 = 鉴权关闭」行为。"""
    monkeypatch.delenv(_ADMIN_TOKEN_ENV, raising=False)
    settings = build_isolated_settings().model_copy(update={"env": RuntimeEnv.DEV})
    ensure_auth_policy(settings)


async def test_prod_with_qq_enabled_and_empty_webhook_token_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 + QQ 启用 + 空 QQ_WEBHOOK_TOKEN：拒绝启动（CONFIG_INVALID）。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    monkeypatch.delenv(_QQ_WEBHOOK_TOKEN_ENV, raising=False)
    with pytest.raises(BusinessException) as exc_info:
        ensure_auth_policy(_prod_settings(qq_enabled=True))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert "生产环境启用 QQ 通道时必须设置 QQ_WEBHOOK_TOKEN" in exc_info.value.message


async def test_startup_calls_auth_policy_before_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """startup 接线：鉴权策略函数在 lifespan 装配链中被调用（隔离 Settings 透传）。"""
    isolated = build_isolated_settings()
    calls: list[Settings] = []
    monkeypatch.setattr(app_main, "ensure_auth_policy", lambda s: calls.append(s))
    app = create_app()
    await startup_app_state(app, isolated, config_dir=tmp_path)
    try:
        assert calls == [isolated]
    finally:
        await app_main.shutdown_app_state(app)
