"""``core.thread_pool`` 单元测试——共享线程池生命周期 + ``run_blocking_ops``。

覆盖矩阵：

    - ``init_shared_thread_pool``：创建 / 幂等 / max_workers 下限夹逼；
    - ``shutdown_shared_thread_pool``：关闭 / 幂等 / wait 语义；
    - ``run_blocking_ops``：执行同步函数 / args+kwargs 透传 /
      contextvars 传播（共享池路径 + 默认执行器回退路径）/ 异常透传 /
      并发互不干扰；
    - 未初始化时 ``get_shared_thread_pool`` 返回 None（回退判定的口径）。
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest

from packages.harness.core.thread_pool import (
    get_shared_thread_pool,
    init_shared_thread_pool,
    run_blocking_ops,
    shutdown_shared_thread_pool,
)


def _sync_add(a: int, b: int) -> int:
    """同步加法（测试辅助）。"""
    return a + b


def _sync_with_kwargs(a: int, *, multiplier: int) -> int:
    """带 kwargs 的同步函数（测试辅助）。"""
    return a * multiplier


def _sync_raise(message: str) -> None:
    """抛出 ValueError 的同步函数（测试辅助）。"""
    raise ValueError(message)


_TEST_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_test_var_for_thread_pool", default=None
)


def _sync_read_context_var() -> str | None:
    """读取全局 ContextVar 的同步函数（测试辅助）。"""
    return _TEST_VAR.get()


class TestSharedThreadPoolLifecycle:
    async def test_init_creates_pool(self) -> None:
        """init 创建共享池单例。"""
        pool = init_shared_thread_pool(max_workers=4)
        assert isinstance(pool, ThreadPoolExecutor)
        assert get_shared_thread_pool() is pool

    async def test_init_is_idempotent(self) -> None:
        """重复 init 返回既有实例（不重建）。"""
        pool_first = init_shared_thread_pool(max_workers=4)
        pool_second = init_shared_thread_pool(max_workers=8)
        assert pool_first is pool_second

    async def test_max_workers_lower_clamped(self) -> None:
        """max_workers < 1 夹逼为 1（防 0 线程池）。"""
        pool = init_shared_thread_pool(max_workers=0)
        assert pool._max_workers == 1

    async def test_shutdown_clears_singleton(self) -> None:
        """shutdown 后单例置 None（后续调用回退默认执行器）。"""
        init_shared_thread_pool(max_workers=2)
        shutdown_shared_thread_pool()
        assert get_shared_thread_pool() is None

    async def test_shutdown_without_init_is_noop(self) -> None:
        """未初始化时 shutdown 为 no-op（不抛异常）。"""
        shutdown_shared_thread_pool()
        assert get_shared_thread_pool() is None

    async def test_reinit_after_shutdown(self) -> None:
        """shutdown 后可重新 init（新实例）。"""
        pool_first = init_shared_thread_pool(max_workers=2)
        shutdown_shared_thread_pool()
        pool_second = init_shared_thread_pool(max_workers=2)
        assert pool_first is not pool_second


class TestRunBlockingOpsExecution:
    async def test_executes_sync_function(self) -> None:
        """共享池路径：执行同步函数并返回结果。"""
        init_shared_thread_pool(max_workers=2)
        assert await run_blocking_ops(_sync_add, 2, 3) == 5

    async def test_passes_args_and_kwargs(self) -> None:
        """位置参数与关键字参数透传。"""
        init_shared_thread_pool(max_workers=2)
        assert await run_blocking_ops(_sync_with_kwargs, 5, multiplier=3) == 15

    async def test_fallback_default_executor(self) -> None:
        """未初始化共享池 → 回退事件循环默认执行器（同为线程池）。"""
        assert get_shared_thread_pool() is None
        assert await run_blocking_ops(_sync_add, 10, 20) == 30

    async def test_fallback_passes_kwargs(self) -> None:
        """回退路径同样透传 kwargs。"""
        assert await run_blocking_ops(_sync_with_kwargs, 7, multiplier=6) == 42

    async def test_returns_complex_type(self) -> None:
        """复杂返回类型（dict / list）原样返回。"""

        def _sync_return_dict() -> dict[str, list[int]]:
            return {"data": [1, 2, 3]}

        assert await run_blocking_ops(_sync_return_dict) == {"data": [1, 2, 3]}


class TestRunBlockingOpsContextVars:
    async def test_contextvar_propagated_via_shared_pool(self) -> None:
        """共享池路径：contextvars 传播到工作线程。"""
        init_shared_thread_pool(max_workers=2)
        token = _TEST_VAR.set("pool-path-value")
        try:
            assert await run_blocking_ops(_sync_read_context_var) == "pool-path-value"
        finally:
            _TEST_VAR.reset(token)

    async def test_contextvar_propagated_via_fallback(self) -> None:
        """回退路径：contextvars 同样传播。"""
        token = _TEST_VAR.set("fallback-path-value")
        try:
            assert await run_blocking_ops(_sync_read_context_var) == "fallback-path-value"
        finally:
            _TEST_VAR.reset(token)

    async def test_contextvar_isolation_between_calls(self) -> None:
        """两次调用各见各的 contextvar 值。"""
        token1 = _TEST_VAR.set("first")
        try:
            result1 = await run_blocking_ops(_sync_read_context_var)
        finally:
            _TEST_VAR.reset(token1)

        token2 = _TEST_VAR.set("second")
        try:
            result2 = await run_blocking_ops(_sync_read_context_var)
        finally:
            _TEST_VAR.reset(token2)

        assert result1 == "first"
        assert result2 == "second"


class TestRunBlockingOpsExceptions:
    async def test_propagates_exceptions_via_shared_pool(self) -> None:
        """共享池路径异常原样上抛。"""
        init_shared_thread_pool(max_workers=2)
        with pytest.raises(ValueError, match="pool error"):
            await run_blocking_ops(_sync_raise, "pool error")

    async def test_propagates_exceptions_via_fallback(self) -> None:
        """回退路径异常原样上抛。"""
        with pytest.raises(ValueError, match="fallback error"):
            await run_blocking_ops(_sync_raise, "fallback error")

    async def test_propagates_runtime_error(self) -> None:
        """RuntimeError 类型透传。"""

        def _raise_runtime(msg: str) -> None:
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="runtime failure"):
            await run_blocking_ops(_raise_runtime, "runtime failure")


class TestRunBlockingOpsConcurrency:
    async def test_concurrent_calls_interleave_safely(self) -> None:
        """并发调用互不干扰（各自上下文 / 各自结果）。"""
        init_shared_thread_pool(max_workers=4)
        tasks = [run_blocking_ops(_sync_add, i, i * 2) for i in range(8)]
        results = await asyncio.gather(*tasks)
        assert results == [i + i * 2 for i in range(8)]

    async def test_runs_off_event_loop(self) -> None:
        """阻塞函数执行期间事件循环不被卡死（并行 async 任务可推进）。"""
        init_shared_thread_pool(max_workers=2)
        progress: list[int] = []

        async def _ticker() -> None:
            for i in range(3):
                progress.append(i)
                await asyncio.sleep(0)

        def _sync_busy() -> int:
            total = 0
            for i in range(1_000_000):
                total += i
            return total

        ticker_task = asyncio.create_task(_ticker())
        await run_blocking_ops(_sync_busy)
        await ticker_task
        assert progress == [0, 1, 2]
