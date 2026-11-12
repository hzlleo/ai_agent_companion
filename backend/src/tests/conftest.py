"""src/tests 共享 fixtures — core 测试资源（跨 unit / integration 复用）。

测试纪律（对齐 docs/test-strategy/）：

- 全部测试 ``async def test_``（asyncio_mode=auto；同步测试棘轮零容忍）；
- 零真实配置：logging 配置经 fixture 显式构造（``LoggingConfig`` +
  ``tmp_path`` 落盘路径），不触碰任何全局 settings / .env / config.yaml；
- 共享资源经 fixture 隔离：缓存单例 / 线程池单例用后即复位，测试间零泄漏。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from packages.harness.core.cache import MemoryTTLCache, reset_default_cache
from packages.harness.core.logging_models import LoggingConfig
from packages.harness.core.thread_pool import shutdown_shared_thread_pool


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
