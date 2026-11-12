"""app/main 装配 + app/api/health 测试——工厂 / 生命周期函数对 / HTTP 面。

测试策略：

    - create_app 静态装配（路由注册面断言）；
    - startup/shutdown 函数对直调（真实组件：隔离 Settings + tmp SQLite +
      真实 PoolManager——无需 ASGI lifespan runner）；
    - 停机 LIFO 追踪（pools → db → thread_pool 顺序锁定）；
    - health / personas 经 ASGITransport 走真实 app 对象（零真实网络；
      ASGITransport 不触发 lifespan——health 无 DB 依赖契约的实证）。
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.main as app_main
from app.api import health as health_module
from app.api.health import APP_VERSION, health
from app.main import create_app, lifespan, shutdown_app_state, startup_app_state
from packages.harness.config.models import DatabaseConfig, Settings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.gateway import LlmGateway
from packages.harness.pools.manager import get_pool_manager


@pytest.fixture
def isolated_settings(tmp_path: Path) -> Settings:
    """隔离 Settings（tmp SQLite 库文件）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-main.db"))
    )


async def test_create_app_registers_routes() -> None:
    """工厂：路由（personas + health）已注册 + 元数据（标题 / 版本）。"""
    app = create_app()
    # OpenAPI 路径面是注册真源（本 FastAPI 版本 app.routes 为惰性
    # _IncludedRouter 占位——请求期才展开；openapi() 触发同构展开）。
    paths = set(app.openapi()["paths"].keys())
    assert "/api/v1/personas" in paths
    assert "/api/v1/personas/{persona_id}" in paths
    assert "/api/v1/personas/{persona_id}/activate" in paths
    assert "/api/v1/health" in paths
    assert app.title == "ai_agent_companion"
    assert app.version == APP_VERSION


async def test_startup_app_state_drives_real_components(isolated_settings, tmp_path: Path) -> None:
    """startup 函数对：真实组件全启（池 / DB / gateway 上挂）。"""
    app = create_app()
    await startup_app_state(app, isolated_settings, config_dir=tmp_path)
    try:
        manager = get_pool_manager()
        assert manager is not None
        assert manager.started is True
        assert manager.http_client is not None
        assert db_engine_holder.async_engine is not None
        assert db_engine_holder.session_factory is not None
        assert isinstance(app.state.gateway, LlmGateway)
    finally:
        await shutdown_app_state(app)


async def test_shutdown_app_state_lifo_order(
    isolated_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shutdown 函数对：LIFO 停机（pools → db → thread_pool）+ 全复位。"""
    app = create_app()
    await startup_app_state(app, isolated_settings, config_dir=tmp_path)
    manager = get_pool_manager()
    assert manager is not None

    order: list[str] = []
    real_stop = manager.stop

    async def traced_stop() -> None:
        order.append("pools")
        await real_stop()

    manager.stop = traced_stop

    real_close = app_main.close_database

    async def traced_close() -> None:
        order.append("db")
        await real_close()

    monkeypatch.setattr(app_main, "close_database", traced_close)

    real_thread_pool = app_main.shutdown_shared_thread_pool

    def traced_thread_pool(wait: bool = True) -> None:
        order.append("thread_pool")
        real_thread_pool(wait)

    monkeypatch.setattr(app_main, "shutdown_shared_thread_pool", traced_thread_pool)

    await shutdown_app_state(app)

    assert order == ["pools", "db", "thread_pool"]
    assert manager.started is False
    assert db_engine_holder.async_engine is None
    assert app.state.gateway is None


async def test_shutdown_app_state_idempotent_without_startup() -> None:
    """shutdown 幂等：未 startup 直接 shutdown 为 no-op（不抛异常）。"""
    app = create_app()
    await shutdown_app_state(app)
    assert db_engine_holder.async_engine is None


async def test_lifespan_context_full_cycle(
    isolated_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lifespan 上下文：startup → 就绪窗口 → shutdown（隔离 settings 驱动）。"""
    monkeypatch.setattr(app_main, "global_settings", isolated_settings)
    app = create_app()
    async with app.router.lifespan_context(app):
        manager = get_pool_manager()
        assert manager is not None and manager.started is True
        assert isinstance(app.state.gateway, LlmGateway)
        assert db_engine_holder.async_engine is not None
    assert get_pool_manager() is None or get_pool_manager().started is False
    assert db_engine_holder.async_engine is None


async def test_health_endpoint_via_asgitransport() -> None:
    """health HTTP 面：真实 app 对象零 lifespan 即 200（无 DB 依赖实证）。"""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "200"
    data = body["data"]
    assert data["status"] == "ok"
    assert data["version"] == APP_VERSION
    assert isinstance(data["ts_ms"], int)
    assert data["ts_ms"] > 0


async def test_health_direct_call_contract() -> None:
    """health 直调契约：ResultDTO 包裹 HealthStatusDTO（liveness 字段齐备）。"""
    result = await health()
    assert result.success is True
    assert result.code == "200"
    assert result.data is not None
    assert result.data.status == "ok"
    assert result.data.version == APP_VERSION
    assert result.data.ts_ms > 0


async def test_app_version_fallback_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本回落：发行元数据缺失时回落固定版本（pythonpath 直跑形态）。"""

    def raise_missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(health_module.metadata, "version", raise_missing)
    assert health_module._resolve_app_version() == "0.1.0"


async def test_personas_route_reachable_through_assembled_app(
    shared_db_engine: AsyncEngine,
) -> None:
    """personas 路由：经装配完成的 app 可达（外部安装共享引擎）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = create_app()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/personas")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["items"] == []
    finally:
        db_engine_holder.reset()


async def test_lifespan_is_async_context_manager_factory() -> None:
    """lifespan 导出面：asynccontextmanager 工厂（异步上下文协议齐备）。"""
    ctx = lifespan(create_app())
    assert hasattr(ctx, "__aenter__")
    assert hasattr(ctx, "__aexit__")


async def test_shutdown_app_state_preserves_external_engine(
    shared_db_engine: AsyncEngine,
) -> None:
    """外部安装引擎治理：shutdown 不 dispose 外部引擎（仅槽位复位）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = create_app()
    await shutdown_app_state(app)
    assert db_engine_holder.async_engine is None
    # 外部引擎仍可正常出连接（生命周期归共享 fixture）。
    async with shared_db_engine.connect() as conn:
        assert conn is not None
