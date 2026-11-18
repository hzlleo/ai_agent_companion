"""调度器一次性延迟作业测试——到期执行一次 / 不重复 / 参数校验。

覆盖：注入时钟与零耗时休眠驱动到期执行；执行前从作业表移除（不重复
触发、同名可复用）；非到期不执行；负延迟与重名拒绝；到期后作业名从
观测面消失。全部异步、零真实等待。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.scheduler import Scheduler


class FakeClock:
    """假时钟（epoch 毫秒——测试手动推进）。"""

    def __init__(self, start_ms: int) -> None:
        """初始化。"""
        self.now = start_ms

    def __call__(self) -> int:
        """返回当前假时刻。"""
        return self.now


async def test_one_shot_runs_once_after_delay_and_is_removed() -> None:
    """一次性作业：到期执行一次后从作业表移除，不重复触发。"""
    clock = FakeClock(1_000_000)
    scheduler = Scheduler(clock=clock, sleep=asyncio.sleep)
    runs: list[str] = []

    async def _job() -> None:
        runs.append("ran")

    scheduler.schedule_once("flow:chat:1", _job, delay_ms=2000)
    assert scheduler.job_names == ["flow:chat:1"]
    scheduler._dispatch_due(clock.now + 1999)
    await asyncio.sleep(0)
    assert runs == []
    scheduler._dispatch_due(clock.now + 2000)
    await asyncio.sleep(0)
    assert runs == ["ran"]
    assert scheduler.job_names == []
    scheduler._dispatch_due(clock.now + 10_000)
    await asyncio.sleep(0)
    assert runs == ["ran"]


async def test_one_shot_name_reusable_after_removal() -> None:
    """执行后同名一次性作业可再次注册（心流批次重排依赖该语义）。"""
    clock = FakeClock(1_000_000)
    scheduler = Scheduler(clock=clock, sleep=asyncio.sleep)
    calls: list[int] = []

    async def _job() -> None:
        calls.append(1)

    scheduler.schedule_once("flow:chat:1", _job, delay_ms=0)
    scheduler._dispatch_due(clock.now)
    await asyncio.sleep(0)
    scheduler.schedule_once("flow:chat:1", _job, delay_ms=0)
    scheduler._dispatch_due(clock.now)
    await asyncio.sleep(0)
    assert calls == [1, 1]


async def test_one_shot_rejects_negative_delay_and_duplicate_name() -> None:
    """负延迟与在途重名 fail loud（装配错误面）。"""
    clock = FakeClock(1_000_000)
    scheduler = Scheduler(clock=clock, sleep=asyncio.sleep)

    async def _job() -> None:
        return None

    with pytest.raises(BusinessException) as exc_info:
        scheduler.schedule_once("bad", _job, delay_ms=-1)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    scheduler.schedule_once("dup", _job, delay_ms=1000)
    with pytest.raises(BusinessException) as exc_info:
        scheduler.schedule_once("dup", _job, delay_ms=1000)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_one_shot_dispatch_does_not_break_interval_jobs() -> None:
    """一次性作业与间隔作业共存：间隔作业照常锚定推进。"""
    clock = FakeClock(1_000_000)
    scheduler = Scheduler(clock=clock, sleep=asyncio.sleep)
    interval_runs: list[int] = []

    async def _interval() -> None:
        interval_runs.append(1)

    async def _once() -> None:
        return None

    scheduler.register_interval("tick", _interval, every_seconds=1)
    scheduler.schedule_once("once", _once, delay_ms=500)
    scheduler._dispatch_due(clock.now + 500)
    await asyncio.sleep(0)
    assert scheduler.job_names == ["tick"]
    scheduler._dispatch_due(clock.now + 1000)
    await asyncio.sleep(0)
    assert interval_runs == [1]
    assert scheduler.job_names == ["tick"]
