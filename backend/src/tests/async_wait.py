"""测试事件循环等待工具——有界让出收敛（零墙钟轮询）。

测试纪律：并发信号的收敛只依赖事件循环让出（``asyncio.sleep(0)``）——
固定睡眠与墙钟 deadline 在慢机 / 快机上的时序不同，会让「条件已成立」与
「等待已超时」互相翻面。本模块以迭代次数封顶（默认 10_000 次让出）替代
时间上限：条件未在让出预算内成立即断言失败，并携带调用方描述。

需要真实时间的场景（退避 / 超时语义 / 线程池往返）不使用本工具，由调用
方显式 ``asyncio.sleep(正数)`` / ``wait_for`` 并就地注明理由。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

__all__ = ["drain_loop", "wait_until"]


async def wait_until(
    predicate: Callable[[], bool],
    *,
    max_yields: int = 10_000,
    description: str = "条件",
) -> None:
    """让出事件循环直到判定成立（迭代封顶——不读墙钟）。

    Args:
        predicate: 零参判定函数（返回 True 即收敛）。
        max_yields: 最大让出次数（超出即失败——防死等）。
        description: 失败消息中的条件描述（定位调用点）。

    Raises:
        AssertionError: 让出预算耗尽时判定仍不成立。
    """
    for _ in range(max_yields):
        if predicate():
            return
        await asyncio.sleep(0)
    if predicate():
        return
    raise AssertionError(f"等待条件在 {max_yields} 次事件循环让出内未成立: {description}")


async def drain_loop(*, iterations: int = 50) -> None:
    """固定次数让出事件循环（负向断言的确定性轮转窗口）。

    用于「此后不应再发生某事」的断言：给工作协程完整的队列轮转机会，
    但以次数而非时间封顶（与 :func:`wait_until` 同一零墙钟口径）。

    Args:
        iterations: 让出次数（默认 50——覆盖单 worker 队列轮转）。
    """
    for _ in range(iterations):
        await asyncio.sleep(0)
