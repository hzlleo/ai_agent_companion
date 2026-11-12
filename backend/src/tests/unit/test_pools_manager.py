"""harness/pools manager + http_pool 测试——生命周期 / 装配接线 / MockTransport 消费。

Settings 经隔离构造（显式值）；HTTP 池经 ``httpx.MockTransport`` 消费验证
（零真实出站）。
"""

from __future__ import annotations

import httpx
import pytest

from packages.harness.config.models import DatabaseConfig, PoolsConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import get_shared_thread_pool
from packages.harness.pools.http_pool import build_shared_http_client
from packages.harness.pools.llm_pool import LlmEntryPool
from packages.harness.pools.manager import (
    PoolManager,
    get_pool_manager,
    init_pool_manager,
)


def _settings(**pools_overrides: object):
    """构造隔离 Settings（pools 分节显式值）。"""
    pools = PoolsConfig.model_validate(pools_overrides or {})
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=":memory:"),
        pools=pools,
    )


async def test_pool_manager_start_assembles_all_pools() -> None:
    """start：core 线程池接线 + HTTP client + LLM 条目池三面装配。"""
    manager = PoolManager(_settings(thread_pool_size=4, http_max_connections=33))
    await manager.start()
    try:
        assert manager.started is True
        # core 共享线程池按配置大小装配（PoolManager → core 接线契约）
        thread_pool = get_shared_thread_pool()
        assert thread_pool is not None
        assert thread_pool._max_workers == 4  # type: ignore[attr-defined]
        # HTTP client 已建
        assert isinstance(manager.http_client, httpx.AsyncClient)
        # LLM 条目池已建
        assert isinstance(manager.llm_entry_pool, LlmEntryPool)
    finally:
        await manager.stop()


async def test_pool_manager_start_idempotent() -> None:
    """重复 start：no-op（既有池不被重建）。"""
    manager = PoolManager(_settings())
    await manager.start()
    try:
        first_client = manager.http_client
        await manager.start()
        assert manager.http_client is first_client
    finally:
        await manager.stop()


async def test_pool_manager_stop_releases_all() -> None:
    """stop：三槽位释放 + core 线程池关闭（LIFO）。"""
    manager = PoolManager(_settings())
    await manager.start()
    client = manager.http_client
    await manager.stop()

    assert manager.started is False
    assert manager.http_client is None
    assert manager.llm_entry_pool is None
    assert get_shared_thread_pool() is None  # core 线程池已关闭
    # HTTP client 已 aclose（is_closed 置位）
    assert client.is_closed


async def test_pool_manager_stop_idempotent() -> None:
    """未 start / 重复 stop：no-op。"""
    manager = PoolManager(_settings())
    await manager.stop()  # 未 start
    await manager.start()
    await manager.stop()
    await manager.stop()


async def test_getters_before_start_raise() -> None:
    """未 start：get_* 抛 INTERNAL_ERROR（快速暴露编程错误）。"""
    manager = PoolManager(_settings())
    with pytest.raises(BusinessException) as http_exc:
        manager.get_http_client()
    assert http_exc.value.code == ErrorCode.INTERNAL_ERROR
    with pytest.raises(BusinessException) as llm_exc:
        manager.get_llm_entry_pool()
    assert llm_exc.value.code == ErrorCode.INTERNAL_ERROR
    with pytest.raises(BusinessException) as thread_exc:
        manager.get_thread_pool()
    assert thread_exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_getters_after_start_return_instances() -> None:
    """start 后：get_* 返回装配实例（与槽位同对象）。"""
    manager = PoolManager(_settings())
    await manager.start()
    try:
        assert manager.get_http_client() is manager.http_client
        assert manager.get_llm_entry_pool() is manager.llm_entry_pool
        assert manager.get_thread_pool() is get_shared_thread_pool()
    finally:
        await manager.stop()


async def test_global_singleton_init_and_get() -> None:
    """全局单例：init 构造 + get 返回同对象（延迟初始化模式）。"""
    import packages.harness.pools.manager as manager_module

    settings = _settings()
    manager = init_pool_manager(settings)
    try:
        assert get_pool_manager() is manager
        # 经模块属性读取（from-import 绑定不随模块全局重绑定刷新）
        assert manager_module.pool_manager is manager
    finally:
        # 单例槽位复位（不 stop——本测试未 start）
        manager_module.pool_manager = None


async def test_build_shared_http_client_limits_and_timeout() -> None:
    """HTTP client 构建：limits / timeout 全量来自池配置。"""
    pools = PoolsConfig(
        http_max_connections=42,
        http_max_keepalive_connections=11,
        http_connect_timeout_seconds=2.5,
        http_timeout_seconds=17.0,
    )
    client = build_shared_http_client(pools)
    try:
        assert client.timeout.connect == 2.5
        assert client.timeout.read == 17.0
        assert client.timeout.write == 17.0
        assert client.timeout.pool == 17.0
    finally:
        await client.aclose()


async def test_shared_http_client_consumed_via_mock_transport() -> None:
    """HTTP 池消费面：MockTransport 走通请求/响应（零真实出站）。"""
    import asyncio

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    pools = PoolsConfig()
    transport_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=pools.http_max_connections,
            max_keepalive_connections=pools.http_max_keepalive_connections,
        ),
        timeout=httpx.Timeout(
            pools.http_timeout_seconds, connect=pools.http_connect_timeout_seconds
        ),
        transport=httpx.MockTransport(_handler),
    )
    try:
        response = await transport_client.get("http://test.local/api/v1/ping")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "path": "/api/v1/ping"}

        # 并发批量请求共享同一 client（批量友好）
        responses = await asyncio.gather(
            *[transport_client.get("http://test.local/batch") for _ in range(5)]
        )
        assert all(r.status_code == 200 for r in responses)
    finally:
        await transport_client.aclose()
