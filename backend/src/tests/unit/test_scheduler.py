"""harness/scheduler 测试——间隔 / 每日数学 + 分发 + 隔离 + 优雅停机。

覆盖面：parse_daily_at 形态校验；next_daily_occurrence_ms 下一出现语义
（今日未来 / 已过取明日 / 恰为当下取明日——跨午夜）；间隔作业 next-run
锚定（注册即 now+interval；触发后 next=锚+interval——drift-safe，时钟
跳远不漂移）；每日作业触发后锚定次日；tick 循环分发（注入时钟 + 零耗时
假休眠——确定性驱动）；per-job 错误隔离（单作业抛错不击穿调度与其它
作业）；重名 / 非法参数注册拒绝；run 重复启动拒绝；request_stop 优雅
收束 + 在途排水（超时 cancel 兜底）；梦境触发作业（fake 链路——激活
人格解析 + 绑定 + run_dream 透传 / 无激活跳过）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.scheduler import (
    Scheduler,
    build_dream_trigger_job,
    next_daily_occurrence_ms,
    parse_daily_at,
)


class FakeClock:
    """假时钟（epoch 毫秒——测试手动推进）。"""

    def __init__(self, start_ms: int) -> None:
        """初始化。"""
        self.now = start_ms

    def __call__(self) -> int:
        """返回当前假时刻。"""
        return self.now

    def advance_ms(self, ms: int) -> None:
        """推进假时钟。"""
        self.now += ms


class SleepController:
    """假休眠控制器（零耗时——可选在每次休眠后推进时钟）。

    每次休眠经 ``await asyncio.sleep(0)`` 让出事件循环（真 yield 点——
    假休眠若不挂起会饿死同循环的 run 任务与测试协程）。
    """

    def __init__(self, clock: FakeClock, *, advance_ms: int = 0) -> None:
        """初始化。"""
        self.clock = clock
        self.advance_ms = advance_ms
        self.sleeps: list[float] = []
        self.tick_count = 0

    async def __call__(self, seconds: float) -> None:
        """零耗时休眠（记录 tick + 推进时钟 + 真实让出事件循环）。"""
        self.sleeps.append(seconds)
        self.tick_count += 1
        if self.advance_ms:
            self.clock.advance_ms(self.advance_ms)
        await asyncio.sleep(0)


def _local_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """构造本地墙钟时刻的 epoch 毫秒（naive local——与被测同轴）。"""
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


# ---------------------------------------------------------------------------
# 纯函数面——每日时刻解析与下一出现
# ---------------------------------------------------------------------------


async def test_parse_daily_at_valid_and_invalid() -> None:
    """parse_daily_at：合法二元组 / 非法形态与越界拒绝。"""
    assert parse_daily_at("22:00") == (22, 0)
    assert parse_daily_at("00:00") == (0, 0)
    assert parse_daily_at("23:59") == (23, 59)
    assert parse_daily_at(" 09:05 ") == (9, 5)
    for bad in ["24:00", "9:5", "09:60", "0900", "aa:bb", "", "9:05:30"]:
        with pytest.raises(BusinessException) as exc_info:
            parse_daily_at(bad)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_next_daily_occurrence_today_when_future() -> None:
    """今日 HH:MM 在未来：取今日该时刻。"""
    now = _local_ms(2026, 11, 16, 8, 0)
    expected = _local_ms(2026, 11, 16, 22, 0)
    assert next_daily_occurrence_ms(now, 22, 0) == expected


async def test_next_daily_occurrence_tomorrow_when_passed() -> None:
    """今日 HH:MM 已过：取明日（跨午夜语义）。"""
    now = _local_ms(2026, 11, 16, 23, 30)
    expected = _local_ms(2026, 11, 17, 3, 0)
    assert next_daily_occurrence_ms(now, 3, 0) == expected


async def test_next_daily_occurrence_exact_now_goes_tomorrow() -> None:
    """恰为 HH:MM 当下（秒 / 毫秒非零）：取明日（严格大于语义）。"""
    base = _local_ms(2026, 11, 16, 22, 0)
    now = base + 1  # 22:00:00.001
    expected = _local_ms(2026, 11, 17, 22, 0)
    assert next_daily_occurrence_ms(now, 22, 0) == expected
    # 整点之前的毫秒（理论不可达的过去 1ms）同样取明日
    assert next_daily_occurrence_ms(base - 1, 22, 0) == base


async def test_next_daily_occurrence_month_boundary() -> None:
    """月末跨日：11-30 → 12-01（日历推进正确）。"""
    now = _local_ms(2026, 11, 30, 23, 0)
    expected = _local_ms(2026, 12, 1, 0, 30)
    assert next_daily_occurrence_ms(now, 0, 30) == expected


# ---------------------------------------------------------------------------
# 注册面——next-run 数学 + 校验
# ---------------------------------------------------------------------------


async def test_register_interval_next_run_anchored_to_now() -> None:
    """间隔作业注册：next = 注册时刻 + interval（毫秒轴）。"""
    clock = FakeClock(1_000_000)
    scheduler = Scheduler(clock=clock, sleep=_never_sleep)
    scheduler.register_interval("job", _noop_job, every_seconds=10)
    assert scheduler.next_run_ms("job") == 1_010_000
    assert scheduler.job_names == ["job"]


async def test_register_daily_next_run_is_next_occurrence() -> None:
    """每日作业注册：next = 下一个 HH:MM 出现（本地墙钟轴）。"""
    now = _local_ms(2026, 11, 16, 20, 0)
    clock = FakeClock(now)
    scheduler = Scheduler(clock=clock, sleep=_never_sleep)
    scheduler.register_daily("duty", _noop_job, daily_at="22:00")
    assert scheduler.next_run_ms("duty") == _local_ms(2026, 11, 16, 22, 0)


async def test_register_rejects_duplicate_name_and_bad_args() -> None:
    """注册校验：重名 / 非正间隔 / 非法时刻 / 非正 tick 拒绝。"""
    clock = FakeClock(0)
    scheduler = Scheduler(clock=clock, sleep=_never_sleep)
    scheduler.register_interval("job", _noop_job, every_seconds=5)
    with pytest.raises(BusinessException) as dup:
        scheduler.register_daily("job", _noop_job, daily_at="22:00")
    assert dup.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as bad_interval:
        scheduler.register_interval("neg", _noop_job, every_seconds=0)
    assert bad_interval.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as bad_daily:
        scheduler.register_daily("bad", _noop_job, daily_at="25:00")
    assert bad_daily.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as bad_tick:
        Scheduler(clock=clock, sleep=_never_sleep, tick_seconds=0)
    assert bad_tick.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as unknown:
        scheduler.next_run_ms("missing")
    assert unknown.value.code == ErrorCode.NOT_FOUND


async def test_interval_sub_second_clamps_to_one_ms() -> None:
    """亚毫秒间隔钳位 1ms（分辨率下限）。"""
    clock = FakeClock(0)
    scheduler = Scheduler(clock=clock, sleep=_never_sleep)
    scheduler.register_interval("fast", _noop_job, every_seconds=0.0001)
    assert scheduler.next_run_ms("fast") == 1


# ---------------------------------------------------------------------------
# tick 循环——分发 / drift-safe / 隔离 / 停机
# ---------------------------------------------------------------------------


async def _never_sleep(seconds: float) -> None:
    """永不休眠（注册面测试占位——不被调用）。"""
    raise AssertionError("注册面测试不应进入 run 循环")


async def _noop_job() -> None:
    """空作业。"""


async def test_run_dispatches_interval_job_and_advances_anchored() -> None:
    """分发 + drift-safe：作业触发后 next=锚+interval（时钟跳远不漂移）。"""
    clock = FakeClock(0)
    controller = SleepController(clock)
    fired: list[int] = []
    scheduler = Scheduler(clock=clock, sleep=controller)

    async def job() -> None:
        """记录触发时刻并请求停机（单次触发的确定性收束）。"""
        fired.append(clock.now)
        scheduler.request_stop()

    scheduler.register_interval("job", job, every_seconds=10)  # next=10_000
    # 时钟跳到 35s（错过 10 / 20 / 30 三槽）——首 tick 只触发一次
    clock.advance_ms(35_000)
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(task, timeout=5.0)
    assert fired == [35_000]
    # 锚定推进：next = 10_000 + 10_000 = 20_000（非 now + interval=45_000）
    assert scheduler.next_run_ms("job") == 20_000
    assert controller.sleeps  # tick 休眠确实发生


async def test_run_catches_up_missed_slots_one_per_tick() -> None:
    """补槽节奏：错过多槽时逐 tick 补齐（每 tick 至多一槽——防风暴）。"""
    clock = FakeClock(0)
    controller = SleepController(clock, advance_ms=1)  # 每 tick 推进 1ms
    fired: list[int] = []
    scheduler = Scheduler(clock=clock, sleep=controller)

    async def job() -> None:
        """记录触发时刻；第 3 次触发后请求停机（确定性收束）。"""
        fired.append(clock.now)
        if len(fired) >= 3:
            scheduler.request_stop()

    scheduler.register_interval("job", job, every_seconds=0.002)  # 2ms 间隔
    clock.advance_ms(7)  # 错过 ~4 槽
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(task, timeout=5.0)
    # 补槽按锚推进：2 → 4 → 6（毫秒轴精确数学；触发时刻随 tick 时钟）
    assert len(fired) == 3
    assert all(moment >= 7 for moment in fired)


async def test_run_daily_job_fires_at_boundary_and_anchors_next_day() -> None:
    """每日作业：跨过 22:00 边界触发一次；next 锚定次日同时刻。"""
    start = _local_ms(2026, 11, 16, 21, 59)
    clock = FakeClock(start)
    controller = SleepController(clock, advance_ms=500)  # 每 tick 推进 0.5s
    fired: list[int] = []
    scheduler = Scheduler(clock=clock, sleep=controller)

    async def job() -> None:
        """记录触发时刻并请求停机（单次触发的确定性收束）。"""
        fired.append(clock.now)
        scheduler.request_stop()

    scheduler.register_daily("duty", job, daily_at="22:00")
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(task, timeout=5.0)
    boundary = _local_ms(2026, 11, 16, 22, 0)
    assert len(fired) == 1
    assert fired[0] >= boundary
    assert scheduler.next_run_ms("duty") == _local_ms(2026, 11, 17, 22, 0)


async def test_run_per_job_error_isolation() -> None:
    """单作业抛错：调度不中断 + 其余作业照常触发（异常只进日志面）。"""
    clock = FakeClock(0)
    controller = SleepController(clock, advance_ms=5)
    good_fired: list[int] = []
    scheduler = Scheduler(clock=clock, sleep=controller)

    async def bad_job() -> None:
        """每轮抛错（隔离验证）。"""
        raise RuntimeError("作业崩溃（测试注入）")

    async def good_job() -> None:
        """记录触发；第 2 次触发后请求停机。"""
        good_fired.append(clock.now)
        if len(good_fired) >= 2:
            scheduler.request_stop()

    scheduler.register_interval("bad", bad_job, every_seconds=0.002)
    scheduler.register_interval("good", good_job, every_seconds=0.002)
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(task, timeout=5.0)
    assert scheduler.started is False  # 停机后运行态复位
    assert len(good_fired) == 2
    assert task.exception() is None  # 作业异常被隔离——run 正常收束


async def test_run_rejects_double_start() -> None:
    """重复 run：INTERNAL_ERROR（装配错误面）。"""
    clock = FakeClock(0)
    controller = SleepController(clock)
    scheduler = Scheduler(clock=clock, sleep=controller)
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)  # 让首个任务先进入运行态（started 置位）
    try:
        with pytest.raises(BusinessException) as exc_info:
            await scheduler.run()
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    finally:
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=5.0)


async def test_request_stop_before_run_exits_immediately() -> None:
    """先停机后 run：循环零分发直接收束（幂等停机语义）。"""
    clock = FakeClock(0)
    controller = SleepController(clock)
    scheduler = Scheduler(clock=clock, sleep=controller)
    scheduler.request_stop()
    await asyncio.wait_for(scheduler.run(), timeout=5.0)
    assert controller.sleeps == []


async def test_drain_waits_for_inflight_job() -> None:
    """停机排水：在途作业自然收尾后 run 才返回（宽限语义）。"""
    clock = FakeClock(0)
    controller = SleepController(clock)
    release = asyncio.Event()
    started = asyncio.Event()

    async def long_job() -> None:
        """在途长作业（事件释放后收尾）。"""
        started.set()
        await release.wait()

    scheduler = Scheduler(clock=clock, sleep=controller)
    scheduler.register_interval("long", long_job, every_seconds=0.001)
    clock.advance_ms(5)
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(started.wait(), timeout=5.0)
    scheduler.request_stop()
    release.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert task.exception() is None


async def test_drain_timeout_cancels_hanging_job() -> None:
    """排水超时：悬挂在途作业被 cancel 兜底（run 仍收束不抛）。"""
    clock = FakeClock(0)
    controller = SleepController(clock)
    started = asyncio.Event()

    async def hanging_job() -> None:
        """永不收尾的作业。"""
        started.set()
        await asyncio.Event().wait()

    scheduler = Scheduler(clock=clock, sleep=controller, drain_timeout_seconds=0.05)
    scheduler.register_interval("hang", hanging_job, every_seconds=0.001)
    clock.advance_ms(5)
    task = asyncio.create_task(scheduler.run())
    await asyncio.wait_for(started.wait(), timeout=5.0)
    scheduler.request_stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert task.exception() is None


# ---------------------------------------------------------------------------
# 梦境触发作业（fake 链路）
# ---------------------------------------------------------------------------


class _FakeDreamRunStats:
    """fake 梦境运行统计（promoted / superseded 属性面）。"""

    promoted = 1
    superseded = 0


class _FakeDreamRun:
    """fake 梦境运行记录（status / stats 属性面——对齐 DreamRunRecordDTO）。"""

    status = "success"
    stats = _FakeDreamRunStats()


class FakeDreamService:
    """fake DreamService（run_dream 调用捕获——含人格上下文轴）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.calls: list[dict[str, Any]] = []

    async def run_dream(self, session: Any, **kwargs: Any) -> Any:
        """记录调用（含人格上下文快照）并返回最小运行记录形态。"""
        self.calls.append({"persona_ctx": persona_id_var.get(), **kwargs})
        return _FakeDreamRun()


class FakePersonaService:
    """fake 人格服务（激活人格可配置）。"""

    def __init__(self, record: Any) -> None:
        """初始化。"""
        self.record = record

    async def get_active_persona(self, session: Any) -> Any:
        """返回配置的激活人格。"""
        return self.record


class _FakeSession:
    """占位会话（梦境 fake 链路不触达真实 DB）。"""


class _FakeSessionFactory:
    """fake 会话工厂（async with 形态）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.opened = 0

    def __call__(self) -> Any:
        """返回异步上下文管理器形态。"""
        self.opened += 1
        return self

    async def __aenter__(self) -> _FakeSession:
        """进入会话。"""
        return _FakeSession()

    async def __aexit__(self, *args: Any) -> None:
        """退出会话。"""


class _FakeGateway:
    """占位网关（梦境 fake 链路不触达真实网关）。"""


async def test_dream_job_runs_dream_for_active_persona() -> None:
    """梦境作业：激活人格解析 → 绑定上下文 → run_dream 透传参数。"""
    dream = FakeDreamService()
    persona_record = type("R", (), {"id": 701, "display_name": "大肥鱼"})()
    session_factory = _FakeSessionFactory()
    factory = build_dream_trigger_job(
        dream_service=dream,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
        persona_service=FakePersonaService(persona_record),  # type: ignore[arg-type]
        session_factory=session_factory,
        store=None,
        now_ms=lambda: 123,
    )
    await factory()
    assert len(dream.calls) == 1
    call = dream.calls[0]
    assert call["persona_ctx"] == "701"
    assert call["persona_name"] == "大肥鱼"
    assert call["now_ms"] == 123
    assert call["store"] is None
    assert session_factory.opened == 1


async def test_dream_job_skips_without_active_persona() -> None:
    """无激活人格：日志跳过——run_dream 零调用（正常路径）。"""
    dream = FakeDreamService()
    factory = build_dream_trigger_job(
        dream_service=dream,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
        persona_service=FakePersonaService(None),  # type: ignore[arg-type]
        session_factory=_FakeSessionFactory(),
    )
    await factory()
    assert dream.calls == []
