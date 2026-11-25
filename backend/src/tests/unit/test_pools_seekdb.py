"""harness/pools seekdb_compat + PoolManager SeekDB 池生命周期测试——零真实连接。

覆盖面：兼容层（create_seekdb_pool 传参形态——autocommit / minsize /
maxsize / db；密码 env 解析；错误基类）/ PoolManager（enabled=False
零池；enabled=True 启动共享池 + LIFO 停机关闭；getter 未装配 fail
loud；创建失败 fail loud）/ build_memory_store 存储选择（回退 / 共享池
注入 / 自持池）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from pydantic import SecretStr

import packages.harness.pools.manager as manager_module
from packages.harness.config.models import DatabaseConfig, PoolsConfig, SeekDBSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.retrieval import build_memory_store
from packages.harness.memory.seekdb_store import SeekDBStore
from packages.harness.memory.sqlite_store import SQLiteFallbackStore
from packages.harness.pools import seekdb_compat
from packages.harness.pools.manager import PoolManager


class FakePool:
    """假 aiomysql 池（close / wait_closed 记录——生命周期断言面）。"""

    def __init__(self) -> None:
        """初始化（未关闭）。"""
        self.closed = False
        self.wait_closed_called = False

    def acquire(self) -> AbstractAsyncContextManager[object]:
        """获取连接（异步上下文管理器——本测试面不实际执行语句）。"""

        @asynccontextmanager
        async def _cm() -> AsyncIterator[object]:
            yield object()

        return _cm()

    def close(self) -> None:
        """标记关闭。"""
        self.closed = True

    async def wait_closed(self) -> None:
        """记录等待关闭。"""
        self.wait_closed_called = True


def _settings(authority_database: DatabaseConfig | None = None, **seekdb_overrides: Any):
    """构造隔离 Settings（seekdb 分节显式值；缺省 sqlite 库——零主库依赖）。"""
    values: dict[str, Any] = {
        "host": None,
        "port": None,
        "user": None,
        "password_env": None,
        "database": "ai_agent_companion_search",
    }
    values.update(seekdb_overrides)
    seekdb = SeekDBSettings.model_validate(values)
    return build_isolated_settings(
        database=(
            authority_database
            if authority_database is not None
            else DatabaseConfig(type="sqlite", name=":memory:")
        ),
        pools=PoolsConfig(),
        seekdb=seekdb,
    )


# ---------------------------------------------------------------------------
# seekdb_compat
# ---------------------------------------------------------------------------


async def test_create_seekdb_pool_passes_autocommit_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池创建传参：autocommit=True + minsize/maxsize + db（假驱动注入）。"""
    captured: dict[str, Any] = {}

    class _StubAiomysql:
        """假 aiomysql 模块（create_pool 捕获 kwargs）。"""

        @staticmethod
        async def create_pool(**kwargs: Any) -> FakePool:
            """记录创建参数并返回假池。"""
            captured.update(kwargs)
            return FakePool()

    monkeypatch.setattr(seekdb_compat, "aiomysql", _StubAiomysql())

    pool = await seekdb_compat.create_seekdb_pool(
        host="seek.local",
        port=2881,
        user="root",
        password="secret",
        database="test",
        pool_min=2,
        pool_max=5,
    )

    assert isinstance(pool, FakePool)
    assert captured == {
        "minsize": 2,
        "maxsize": 5,
        "host": "seek.local",
        "port": 2881,
        "user": "root",
        "password": "secret",
        "db": "test",
        "autocommit": True,
    }


async def test_resolve_seekdb_password_env_indirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """密码 env 间接：从指定变量读取；未设置返回空串（告警形态）。"""
    monkeypatch.setenv("MY_SEEKDB_PASSWORD", "hunter2")
    assert seekdb_compat.resolve_seekdb_password("MY_SEEKDB_PASSWORD") == "hunter2"

    monkeypatch.delenv("MY_SEEKDB_PASSWORD", raising=False)
    assert seekdb_compat.resolve_seekdb_password("MY_SEEKDB_PASSWORD") == ""


async def test_seekdb_error_base_is_pymysql_error() -> None:
    """错误基类：aiomysql re-export 的 pymysql.err.Error。"""
    import pymysql

    assert seekdb_compat.seekdb_error_base() is pymysql.err.Error


# ---------------------------------------------------------------------------
# PoolManager——SeekDB 池生命周期
# ---------------------------------------------------------------------------


async def test_pool_manager_seekdb_disabled_keeps_slot_empty() -> None:
    """enabled=False（默认）：启动零 SeekDB 池（回退路径不受影响）。"""
    manager = PoolManager(_settings())
    await manager.start()
    try:
        assert manager.seekdb_pool is None
        with pytest.raises(BusinessException) as exc_info:
            manager.get_seekdb_pool()
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    finally:
        await manager.stop()


async def test_pool_manager_seekdb_enabled_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enabled=True：启动创建共享池（配置映射）→ LIFO 停机关闭。"""
    created: list[dict[str, Any]] = []
    fake_pool = FakePool()

    async def _fake_create_pool(**kwargs: Any) -> FakePool:
        """假池工厂（记录创建参数）。"""
        created.append(kwargs)
        return fake_pool

    monkeypatch.setattr(manager_module, "create_seekdb_pool", _fake_create_pool)
    monkeypatch.setenv("SEEKDB_TEST_PASSWORD", "pool-secret")

    manager = PoolManager(
        _settings(
            DatabaseConfig(
                type="mysql", host="seek.local", port=2901, user="seeker", name="ai_agent_companion"
            ),
            enabled=True,
            host="seek.local",
            port=2901,
            user="seeker",
            password_env="SEEKDB_TEST_PASSWORD",
            database="mem_idx",
            pool_min=2,
            pool_max=3,
        )
    )
    await manager.start()
    try:
        assert manager.seekdb_pool is fake_pool
        assert manager.get_seekdb_pool() is fake_pool
        assert created == [
            {
                "host": "seek.local",
                "port": 2901,
                "user": "seeker",
                "password": "pool-secret",
                "database": "mem_idx",
                "pool_min": 2,
                "pool_max": 3,
            }
        ]
        assert fake_pool.closed is False
    finally:
        await manager.stop()

    assert fake_pool.closed is True
    assert fake_pool.wait_closed_called is True
    assert manager.seekdb_pool is None


async def test_pool_manager_seekdb_pool_creation_fail_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池创建失败：fail loud（INTERNAL_ERROR——部署顺序契约）。"""

    async def _boom(**kwargs: Any) -> FakePool:
        """失败工厂（模拟实例不可达）。"""
        raise OSError("connection refused")

    monkeypatch.setattr(manager_module, "create_seekdb_pool", _boom)
    manager = PoolManager(_settings(enabled=True))
    with pytest.raises(BusinessException) as exc_info:
        await manager.start()
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert manager.started is False


async def test_pool_manager_seekdb_fail_loud_releases_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池创建失败路径：已建共享 HTTP client 先释放（aclose 并置 None）。"""
    import httpx

    async def _boom(**kwargs: Any) -> FakePool:
        """失败工厂（模拟实例不可达）。"""
        raise OSError("connection refused")

    created: list[httpx.AsyncClient] = []

    def _fake_build_client(pools: Any) -> httpx.AsyncClient:
        """记录真实 client 构造（追踪其是否被 aclose）。"""
        client = httpx.AsyncClient()
        created.append(client)
        return client

    monkeypatch.setattr(manager_module, "create_seekdb_pool", _boom)
    monkeypatch.setattr(manager_module, "build_shared_http_client", _fake_build_client)

    manager = PoolManager(_settings(enabled=True))
    with pytest.raises(BusinessException):
        await manager.start()

    assert manager.started is False
    # 半装配资源已释放：槽位置 None 且底层 client 已 aclose
    assert manager.http_client is None
    assert created, "HTTP client 应已被构造"
    assert all(client.is_closed for client in created)


# ---------------------------------------------------------------------------
# build_memory_store——存储选择
# ---------------------------------------------------------------------------


async def test_build_memory_store_fallback_when_disabled() -> None:
    """存储选择：enabled=False → SQLiteFallbackStore（零配置开发路径）。"""
    store = build_memory_store(_settings())
    assert isinstance(store, SQLiteFallbackStore)


async def test_build_memory_store_seekdb_shared_pool_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储选择：enabled=True + 管理器已启动 → SeekDBStore 注入共享池。"""
    fake_pool = FakePool()

    async def _fake_create_pool(**kwargs: Any) -> FakePool:
        """假池工厂。"""
        return fake_pool

    monkeypatch.setattr(manager_module, "create_seekdb_pool", _fake_create_pool)
    settings = _settings(enabled=True)
    manager = manager_module.init_pool_manager(settings)
    await manager.start()
    try:
        store = build_memory_store(settings)
        assert isinstance(store, SeekDBStore)
        assert store._pool is fake_pool
        assert store._owns_pool is False
        # 注入池形态：close 为 no-op（生命周期归 PoolManager）
        await store.close()
        assert fake_pool.closed is False
    finally:
        current = manager_module.pool_manager
        if current is not None:
            await current.stop()
        manager_module.pool_manager = None


async def test_build_memory_store_seekdb_own_pool_without_manager() -> None:
    """存储选择：enabled=True + 无已启动管理器 → 自持池形态（惰性创建）。"""
    store = build_memory_store(_settings(enabled=True))
    assert isinstance(store, SeekDBStore)
    assert store._owns_pool is True
    # 未触发任何访问——池未创建（惰性）
    assert store._pool is None
    await store.close()  # 无自持池——幂等 no-op


async def test_seekdb_single_stack_invariant_rejects_split_instance() -> None:
    """单栈不变式：检索池显式 host / port 与权威库不一致 → 启动 fail loud。"""
    database = DatabaseConfig(
        type="mysql", host="db.local", port=2881, user="root", name="ai_agent_companion"
    )
    settings = _settings(database, enabled=True, host="other.local", port=2881)
    with pytest.raises(BusinessException) as exc_info:
        seekdb_compat.resolve_seekdb_config(settings)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert exc_info.value.details["authority_host"] == "db.local"

    port_settings = _settings(database, enabled=True, host="db.local", port=2901)
    with pytest.raises(BusinessException) as port_error:
        seekdb_compat.resolve_seekdb_config(port_settings)
    assert port_error.value.code == ErrorCode.CONFIG_INVALID


async def test_seekdb_single_stack_invariant_inherits_authority() -> None:
    """单栈解析：留空字段继承权威库；显式一致覆盖通过并读取权威密码变量。"""
    database = DatabaseConfig(
        type="mysql",
        host="db.local",
        port=2881,
        user="authority-user",
        password=SecretStr("authority-pass"),
        name="ai_agent_companion",
    )
    inherited = seekdb_compat.resolve_seekdb_config(_settings(database, enabled=True))
    assert inherited.host == "db.local"
    assert inherited.port == 2881
    assert inherited.user == "authority-user"
    assert inherited.password_env == "DATABASE_PASSWORD"

    explicit = seekdb_compat.resolve_seekdb_config(
        _settings(database, enabled=True, host="db.local", port=2881, user="search-user")
    )
    assert explicit.host == "db.local"
    assert explicit.port == 2881
    assert explicit.user == "search-user"


async def test_pool_manager_start_rejects_split_instance_before_allocating() -> None:
    """单栈不变式在资源装配前 fail loud：不建 HTTP client / 不置 started。"""
    database = DatabaseConfig(
        type="mysql", host="db.local", port=2881, user="root", name="ai_agent_companion"
    )
    manager = PoolManager(_settings(database, enabled=True, host="other.local", port=2881))
    with pytest.raises(BusinessException) as exc_info:
        await manager.start()
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert manager.started is False
    assert manager.http_client is None
    assert manager.seekdb_pool is None
