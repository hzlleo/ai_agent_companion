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


def _settings(**seekdb_overrides: Any):
    """构造隔离 Settings（seekdb 分节显式值；sqlite 库——零主库依赖）。"""
    seekdb = SeekDBSettings.model_validate(seekdb_overrides or {})
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=":memory:"),
        pools=PoolsConfig(),
        seekdb=seekdb,
    )


# ---------------------------------------------------------------------------
# seekdb_compat
# ---------------------------------------------------------------------------


async def test_create_seekdb_pool_passes_autocommit_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池创建传参：autocommit=True + minsize/maxsize + db（via 动态加载面）。"""
    captured: dict[str, Any] = {}

    class _StubAiomysql:
        """假 aiomysql 模块（create_pool 捕获 kwargs）。"""

        @staticmethod
        async def create_pool(**kwargs: Any) -> FakePool:
            """记录创建参数并返回假池。"""
            captured.update(kwargs)
            return FakePool()

    monkeypatch.setattr(seekdb_compat, "_AIOMYSQL_MODULE", _StubAiomysql())

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
