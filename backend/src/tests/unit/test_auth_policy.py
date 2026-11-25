"""生产环境鉴权启动策略测试——fail-closed 矩阵与 startup 接线。

覆盖矩阵：``ENV=prod`` 空管理面 token 拒绝 / 非空通过；开发与测试环境
保持空 token 放行；``ENV=prod`` 且 QQ 通道启用时空入站 token 拒绝；
``startup_app_state`` 在组件装配前调用策略函数（recorder 断言）。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

import app.main as app_main
import packages.harness.config.auth_policy as auth_policy
from app.main import create_app, startup_app_state
from packages.harness.config.auth_policy import (
    WEBHOOK_PATH_AUTH_CHECKS,
    ensure_auth_policy,
)
from packages.harness.config.models import (
    ChannelsSettings,
    QqChannelSettings,
    RuntimeEnv,
    ServerConfig,
    Settings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.unit.test_inbound_body_structure import (
    _direct_exempt_router_names,
    _imported_router_modules,
)

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


def _router_prefix(tree: ast.Module) -> str:
    """取 API 模块内 ``router = APIRouter(prefix=...)`` 的路由前缀。

    Args:
        tree: API 模块 AST。

    Returns:
        路由前缀字符串（缺失即断言失败）。
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "router" not in targets or not isinstance(node.value, ast.Call):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    raise AssertionError("API 模块缺少 router = APIRouter(prefix=...)")


def _post_subpaths(tree: ast.Module) -> list[str]:
    """取模块内 ``@router.post(<path>)`` 的子路径。

    Args:
        tree: API 模块 AST。

    Returns:
        子路径列表（空字符串表示前缀本身）。
    """
    subpaths: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "post"
                and isinstance(func.value, ast.Name)
                and func.value.id == "router"
            ):
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                subpaths.append(str(decorator.args[0].value))
            else:
                subpaths.append("")
    return subpaths


async def test_exempt_post_routes_have_auth_policy_entry() -> None:
    """main.py 豁免鉴权的 POST 路由全部登记在 path 前缀→检查项映射中。"""
    main_tree = ast.parse(Path(app_main.__file__).read_text())
    imported = _imported_router_modules(main_tree)
    direct = _direct_exempt_router_names(main_tree)

    checked = 0
    for router_name in sorted(direct):
        module_name = imported.get(router_name)
        if module_name is None:
            continue
        module = importlib.import_module(module_name)
        module_path = inspect.getsourcefile(module)
        assert module_path is not None
        module_tree = ast.parse(Path(module_path).read_text())
        prefix = _router_prefix(module_tree)
        for subpath in _post_subpaths(module_tree):
            full_path = prefix + subpath
            assert any(
                full_path.startswith(prefix_key)
                for prefix_key in WEBHOOK_PATH_AUTH_CHECKS
            ), f"豁免 POST 路由 {full_path} 缺少 auth-policy 检查项"
            checked += 1
    assert checked == 3


async def test_prod_startup_dispatches_every_webhook_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产环境启动策略逐项执行映射表内的全部入站凭据检查（无遗漏分支）。"""
    monkeypatch.setenv(_ADMIN_TOKEN_ENV, "test-only-prod-admin-token")
    calls: list[str] = []
    patched = {
        prefix: (lambda _settings, key=prefix: calls.append(key))
        for prefix in WEBHOOK_PATH_AUTH_CHECKS
    }
    monkeypatch.setattr(auth_policy, "WEBHOOK_PATH_AUTH_CHECKS", patched)
    ensure_auth_policy(_prod_settings())
    assert calls == list(WEBHOOK_PATH_AUTH_CHECKS)
