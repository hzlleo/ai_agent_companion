"""src/tests 共享 fixtures — core / config / database / pools 测试资源。

测试纪律（对齐 docs/test-strategy/）：

- 全部测试 ``async def test_``（asyncio_mode=auto；同步测试棘轮零容忍）；
- 零真实配置：logging 配置经 fixture 显式构造（``LoggingConfig`` +
  ``tmp_path`` 落盘路径）；config 运行时状态经自动 fixture 每测试复位，
  不触碰任何全局 settings / .env / config.yaml；
- 共享资源经 fixture 隔离：缓存单例 / 线程池单例 / config 运行时状态用后
  即复位，测试间零泄漏；
- 共享 DB fixture（P0-3 落地）：SQLite in-memory + StaticPool 会话级引擎
  共享 + 每测试自动清表——多测试依赖的数据库资源不私建池（见
  docs/test-strategy/README.md 铁律 3）。aiosqlite 的 future 按调用在当前
  事件循环上创建并由工作线程回调（无跨循环持久绑定），会话级引擎在
  function 级测试循环间共享安全（已实证）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from packages.harness.config import loader as config_loader
from packages.harness.config.multimodal_fallback_loader import (
    reset_multimodal_fallback_runtime_state,
)
from packages.harness.core.cache import MemoryTTLCache, reset_default_cache
from packages.harness.core.logging_models import LoggingConfig
from packages.harness.core.thread_pool import shutdown_shared_thread_pool
from packages.harness.database.base import Base
from tests.db_models import PersonaNoteRow, PlainCounterRow  # noqa: F401  模型注册副作用导入

# ---------------------------------------------------------------------------
# 数据库测试模型见 tests/db_models.py（唯一定义，防 conftest 双模块身份
# 下的表名冲突——见该模块 docstring）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# core 测试资源（P0-2 既有）
# ---------------------------------------------------------------------------


@pytest.fixture
def logging_config(tmp_path: Path) -> LoggingConfig:
    """构造测试用日志配置（tmp_path 落盘——零真实配置依赖）。

    Args:
        tmp_path: pytest 内建一次性目录。

    Returns:
        指向 tmp_path 下文本 / JSON 日志文件的 ``LoggingConfig`` 实例。
    """
    return LoggingConfig(
        level="DEBUG",
        log_file=str(tmp_path / "logs" / "test-app.log"),
        log_json_file=str(tmp_path / "logs" / "test-app.json.log"),
        rotation="1 day",
        retention="30 days",
    )


@pytest.fixture
def memory_cache() -> MemoryTTLCache:
    """独立内存缓存实例（不挂默认单例——装饰器测试用 ``default_cache``）。

    Returns:
        小容量 ``MemoryTTLCache``（max_entries=100）。
    """
    return MemoryTTLCache(max_entries=100)


@pytest.fixture
async def default_cache() -> AsyncIterator[MemoryTTLCache]:
    """默认缓存单例装配/复位 fixture（用后置 None——测试间零泄漏）。

    Yields:
        已初始化的默认 ``MemoryTTLCache`` 实例。
    """
    from packages.harness.core.cache import init_default_cache

    cache = init_default_cache(max_entries=100)
    yield cache
    reset_default_cache()


@pytest.fixture(autouse=True)
async def _reset_shared_thread_pool() -> AsyncIterator[None]:
    """自动 fixture：每测试后关闭共享线程池（防单例跨测试泄漏）。

    Yields:
        无载荷（仅承担 teardown）。
    """
    yield
    shutdown_shared_thread_pool(wait=True)


# ---------------------------------------------------------------------------
# config 运行时状态复位（P0-3：loader / 解析器 / 回退链跨测试零残留）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_config_runtime_state() -> AsyncIterator[None]:
    """自动 fixture：每测试后复位 config 运行时状态（llm 段 / 工具清单 /
    模型解析器索引 / 多模态回退链 / 重载回调）。

    Yields:
        无载荷（仅承担 teardown）。
    """
    yield
    config_loader.reset_config_runtime_state()
    reset_multimodal_fallback_runtime_state()


# ---------------------------------------------------------------------------
# 共享数据库 fixture（P0-3：SQLite StaticPool 会话级引擎 + 每测试自动清表）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def shared_db_engine() -> AsyncIterator[AsyncEngine]:
    """会话级共享 SQLite 引擎（in-memory + StaticPool 单连接）。

    StaticPool 确保全部 session 共享同一底层连接（SQLite in-memory 是
    per-connection 的）；aiosqlite future 按调用创建于当前事件循环、由
    工作线程经 future 所属循环回调——引擎跨 function 级测试循环共享安全。
    表结构在本 fixture 内一次性建立（``create_all`` checkfirst 幂等）。

    Yields:
        共享 :class:`AsyncEngine`（会话结束 dispose）。
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
async def _shared_db_auto_cleanup(shared_db_engine: AsyncEngine) -> AsyncIterator[None]:
    """自动清表：每测试后清空共享库全部表行（保留 schema）。

    autouse 保证任何共享引擎消费者（``db_session`` 或直接
    ``shared_db_engine``）都获得清表——行级清理按逆依赖序 DELETE
    （父表先清防 FK 约束）。铁律 3 的「自动清表」由本 fixture 承担。

    Yields:
        无载荷（仅承担 teardown）。
    """
    yield
    factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
    async with factory() as cleanup:
        for table in reversed(Base.metadata.sorted_tables):
            await cleanup.execute(delete(table))
        await cleanup.commit()


@pytest.fixture
async def db_session(shared_db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每测试数据库 Session（绑定共享引擎；清表由 autouse fixture 承担）。

    Args:
        shared_db_engine: 会话级共享引擎。

    Yields:
        绑定共享引擎的 :class:`AsyncSession`（不自动 commit）。
    """
    factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
