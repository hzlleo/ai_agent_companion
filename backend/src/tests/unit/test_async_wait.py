"""事件循环等待工具测试——零墙钟收敛（只请求零延时让出）。

锁定的行为事实：``wait_until`` 的收敛不依赖 ``loop.time()`` / 固定睡眠，
只经 ``asyncio.sleep(0)`` 让出事件循环；失败由让出次数封顶并携带条件
描述。需要真实时间的并发（线程池往返 / 退避）不在本工具职责内。
"""

from __future__ import annotations

import asyncio

import pytest

import tests.async_wait as async_wait


async def test_wait_until_converges_with_only_zero_delay_yields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发翻转协程只经零延时让出推进时，等待方仍确定性收敛。"""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def _recorded_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(async_wait.asyncio, "sleep", _recorded_sleep)
    flag: list[bool] = []

    async def _flipper() -> None:
        for _ in range(25):
            await real_sleep(0)
        flag.append(True)

    flipper_task = asyncio.create_task(_flipper())
    try:
        await async_wait.wait_until(lambda: bool(flag), description="翻转标记")
    finally:
        await flipper_task
    assert delays
    assert set(delays) == {0}


async def test_wait_until_failure_message_carries_budget_and_description() -> None:
    """让出预算耗尽：断言消息含次数与调用方描述（定位失败点）。"""
    with pytest.raises(AssertionError, match="5 次事件循环让出内未成立: 永不成立"):
        await async_wait.wait_until(
            lambda: False, max_yields=5, description="永不成立"
        )


async def test_drain_loop_is_bounded_zero_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """负向断言窗口：固定次数零延时让出（不请求任何正数睡眠）。"""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def _recorded_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(async_wait.asyncio, "sleep", _recorded_sleep)
    await async_wait.drain_loop(iterations=7)
    assert delays == [0] * 7
