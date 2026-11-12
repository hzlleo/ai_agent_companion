"""异步工具 — ``run_blocking_ops`` + core 自持共享线程池。

全工程统一的同步/CPU 密集操作下沉入口。所有无法 async 化的第三方库
（jwt.decode、zipfile/tarfile、同步 SDK 等）必须通过此函数下沉到线程池，
避免阻塞事件循环。

设计要点（pools 域是 P0-3 交付面，core 不 import
pools/config——共享池由 core 自持）：

- 共享 ``ThreadPoolExecutor`` 生命周期由本模块管理：
  ``init_shared_thread_pool(max_workers)`` 在 lifespan 启动期调用，
  ``shutdown_shared_thread_pool()`` 在停机期调用；
- 未初始化时回退到事件循环默认执行器（同样是线程池——asyncio 默认
  ``ThreadPoolExecutor``），语义与 anyio 回退路径等价；
- 通过 ``contextvars.copy_context()`` 传播上下文（request_id /
  session_id / persona_id 等请求级 ContextVar）；
- ``run_in_executor`` 的 Future 被取消时线程仍执行完毕（abandon 语义），
  避免资源泄漏。

用法::

    from packages.harness.core.thread_pool import run_blocking_ops

    # 下沉 jwt.decode
    payload = await run_blocking_ops(jwt.decode, token, key, algorithms=["HS256"])

    # 下沉 zip 解压
    await run_blocking_ops(zipfile.ZipFile.extractall, zf, path=target_dir)
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

__all__ = [
    "init_shared_thread_pool",
    "shutdown_shared_thread_pool",
    "get_shared_thread_pool",
    "run_blocking_ops",
]

P = ParamSpec("P")
T = TypeVar("T")

# 共享线程池单例（None = 未初始化——回退到事件循环默认执行器）
_shared_pool: ThreadPoolExecutor | None = None
# 生命周期锁：init/shutdown 可能被 lifespan 与测试并发触发，快临界区加锁防竞态
_pool_lock = threading.Lock()


def init_shared_thread_pool(max_workers: int = 8) -> ThreadPoolExecutor:
    """初始化共享线程池（幂等——已初始化时返回既有实例）。

    由 app 层 lifespan 启动期调用；``max_workers`` 由 config 域 Settings
    映射传入（core 不读取全局配置）。

    Args:
        max_workers: 工作线程数（下限 1）。

    Returns:
        共享 ``ThreadPoolExecutor`` 实例。
    """
    global _shared_pool
    with _pool_lock:
        if _shared_pool is not None:
            return _shared_pool
        _shared_pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="companion-core",
        )
        return _shared_pool


def shutdown_shared_thread_pool(wait: bool = True) -> None:
    """关闭共享线程池（幂等——未初始化时为 no-op）。

    由 app 层 lifespan 停机期调用。

    Args:
        wait: 是否等待在途任务执行完毕（默认 True——abandon 语义的对偶，
            停机时给在途阻塞操作一个自然收尾窗口）。
    """
    global _shared_pool
    with _pool_lock:
        pool = _shared_pool
        _shared_pool = None
    if pool is not None:
        pool.shutdown(wait=wait)


def get_shared_thread_pool() -> ThreadPoolExecutor | None:
    """返回共享线程池单例（未初始化时为 ``None``）。

    Returns:
        共享 ``ThreadPoolExecutor``；未初始化返回 ``None``。
    """
    return _shared_pool


async def run_blocking_ops(
    func: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """将同步/CPU 密集函数下沉到线程池执行，避免阻塞事件循环。

    全工程统一的同步库桥接入口。优先使用 core 共享线程池（已初始化时），
    未初始化时回退到事件循环默认执行器（同样是线程池）。两条路径均通过
    ``contextvars.copy_context()`` 传播请求级上下文。

    Args:
        func: 同步函数（如 ``jwt.decode``、``zipfile.ZipFile.extractall``）。
        *args: 透传给 func 的位置参数。
        **kwargs: 透传给 func 的关键字参数。

    Returns:
        func 的返回值。

    Raises:
        Exception: func 内部抛出的任何异常原样上抛。
    """
    pool = _shared_pool
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    # ``loop.run_in_executor(executor, func, *args)`` 不接受 kwargs，
    # 用 lambda 包裹 ``ctx.run(func, *args, **kwargs)`` 以保持两条路径
    # 一致的语义（kwargs 透传 + contextvars 传播）。
    return await loop.run_in_executor(
        pool,
        lambda: ctx.run(func, *args, **kwargs),
    )
