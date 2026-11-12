"""harness/pools llm_pool 测试——熔断状态机 / per-entry 信号量 / 组合门。

熔断器时钟注入固定时钟（零 sleep 确定性驱动）；信号量经真实
``asyncio.Semaphore`` 并发语义验证。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.models import PoolsConfig
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.pools.llm_pool import (
    CircuitBreaker,
    CircuitState,
    LlmEntryPool,
    build_llm_entry_pool,
    make_entry_key,
)


class _FakeClock:
    """固定时钟（步进可控——熔断状态机确定性驱动）。"""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def _entry(name: str = "e", *, concurrency: int | None = None, model: str = "m"):
    """构造测试条目。"""
    raw: dict[str, object] = {
        "name": name,
        "base_url": f"http://{name}/v1",
        "model": model,
    }
    if concurrency is not None:
        raw["concurrency"] = concurrency
    return parse_model_entry(raw)


# ---------------------------------------------------------------------------
# CircuitBreaker 状态机
# ---------------------------------------------------------------------------


async def test_breaker_starts_closed_and_allows() -> None:
    """初始态 closed：放行 + 零失败计数。"""
    breaker = CircuitBreaker("t")
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.is_open is False
    assert breaker.consecutive_failures == 0


async def test_breaker_trips_open_after_threshold() -> None:
    """连续失败达阈值 → open（拒绝一切请求）。"""
    breaker = CircuitBreaker("t", fail_max=3)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False
    assert breaker.is_open is True


async def test_breaker_success_resets_counter() -> None:
    """成功清零连续计数（closed 态自愈）。"""
    breaker = CircuitBreaker("t", fail_max=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED  # 未达阈值


async def test_breaker_cooldown_then_half_open_probe() -> None:
    """open 冷却期满 → 惰性转 half_open → 放行单次探测。"""
    clock = _FakeClock()
    breaker = CircuitBreaker("t", fail_max=1, reset_timeout_ms=60_000, clock=clock)
    breaker.record_failure()
    assert breaker.is_open is True

    clock.advance(59_999)
    assert breaker.allow_request() is False  # 冷却未满

    clock.advance(1)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow_request() is True  # 探测放行


async def test_breaker_half_open_probe_success_closes() -> None:
    """半开探测成功 → closed（完全恢复）。"""
    clock = _FakeClock()
    breaker = CircuitBreaker("t", fail_max=1, reset_timeout_ms=1_000, clock=clock)
    breaker.record_failure()
    clock.advance(1_000)
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


async def test_breaker_half_open_probe_failure_reopens() -> None:
    """半开探测失败 → 重回 open（冷却重新计时）。"""
    clock = _FakeClock()
    breaker = CircuitBreaker("t", fail_max=1, reset_timeout_ms=1_000, clock=clock)
    breaker.record_failure()
    clock.advance(1_000)
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    clock.advance(999)
    assert breaker.allow_request() is False  # 新冷却周期
    clock.advance(1)
    assert breaker.state == CircuitState.HALF_OPEN


async def test_breaker_cooldown_remaining_ms_reporting() -> None:
    """冷却剩余毫秒上报（退避提示契约；非 open 态恒 0）。"""
    clock = _FakeClock()
    breaker = CircuitBreaker("t", fail_max=1, reset_timeout_ms=10_000, clock=clock)
    assert breaker.cooldown_remaining_ms == 0
    breaker.record_failure()
    clock.advance(3_000)
    assert breaker.cooldown_remaining_ms == 7_000
    breaker.record_success()
    assert breaker.cooldown_remaining_ms == 0


async def test_breaker_reset_force_closes() -> None:
    """强制复位（测试 / 热重建通道）。"""
    breaker = CircuitBreaker("t", fail_max=1)
    breaker.record_failure()
    assert breaker.is_open is True
    breaker.reset()
    assert breaker.state == CircuitState.CLOSED


async def test_breaker_minimum_bounds() -> None:
    """参数下限钳制：fail_max>=1 / reset_timeout_ms>=0。"""
    breaker = CircuitBreaker("t", fail_max=0, reset_timeout_ms=-5)
    assert breaker.fail_max == 1
    assert breaker.reset_timeout_ms == 0


# ---------------------------------------------------------------------------
# LlmEntryPool 信号量注册表
# ---------------------------------------------------------------------------


async def test_entry_key_normalization() -> None:
    """条目键：base_url 去尾斜杠（/v1/ 与 /v1 同端点同键）。"""
    entry_a = parse_model_entry({"name": "a", "base_url": "http://h/v1", "model": "m"})
    entry_b = parse_model_entry({"name": "b", "base_url": "http://h/v1/", "model": "m"})
    assert make_entry_key(entry_a) == make_entry_key(entry_b)
    entry_c = parse_model_entry({"name": "c", "base_url": "http://h/v1", "model": "m2"})
    assert make_entry_key(entry_a) != make_entry_key(entry_c)  # 同端点不同 model 分键


async def test_entry_semaphore_concurrency_from_entry() -> None:
    """条目声明并发：per-entry 信号量限值生效。"""
    pool = LlmEntryPool(PoolsConfig())
    sem = pool.get_entry_semaphore(_entry("e", concurrency=3))
    assert sem._value == 3  # type: ignore[attr-defined]


async def test_entry_semaphore_falls_back_to_global_limit() -> None:
    """条目未声明并发：回落全局 POOLS_LLM_SEMAPHORE_LIMIT。"""
    pool = LlmEntryPool(PoolsConfig(llm_semaphore_limit=7))
    sem = pool.get_entry_semaphore(_entry("e"))
    assert sem._value == 7  # type: ignore[attr-defined]


async def test_entry_semaphore_same_key_idempotent() -> None:
    """同键同限值：幂等返回同一信号量实例。"""
    pool = LlmEntryPool(PoolsConfig())
    entry = _entry("e", concurrency=2)
    assert pool.get_entry_semaphore(entry) is pool.get_entry_semaphore(entry)


async def test_entry_semaphore_keep_max_on_conflict() -> None:
    """同键限值冲突：保留较大值（下调不生效——热重建通道职责）。"""
    pool = LlmEntryPool(PoolsConfig())
    sem_small = pool.get_entry_semaphore(_entry("e", concurrency=2))
    sem_big = pool.get_entry_semaphore(_entry("e", concurrency=5))
    assert sem_big._value == 5  # type: ignore[attr-defined]
    # 下调请求保留较大值
    assert pool.get_entry_semaphore(_entry("e", concurrency=1)) is sem_big
    assert pool.limits()[make_entry_key(_entry("e"))] == 5
    assert sem_small is not sem_big


async def test_entry_semaphore_limit_upgrade_replaces() -> None:
    """限值上调：立即替换信号量（新等待者用新值）。"""
    pool = LlmEntryPool(PoolsConfig())
    pool.get_entry_semaphore(_entry("e", concurrency=2))
    upgraded = pool.get_entry_semaphore(_entry("e", concurrency=6))
    assert upgraded._value == 6  # type: ignore[attr-defined]


async def test_entry_semaphore_enforces_concurrency() -> None:
    """信号量真实限流语义：并发上限内并行、超限排队。"""
    pool = LlmEntryPool(PoolsConfig())
    entry = _entry("e", concurrency=2)
    in_flight: list[int] = []
    peak: list[int] = []

    async def _task() -> None:
        async with pool.entry_gate(entry):
            in_flight.append(1)
            peak.append(len(in_flight))
            await asyncio.sleep(0.01)
            in_flight.pop()

    await asyncio.gather(*[_task() for _ in range(6)])
    assert max(peak) <= 2
    assert len(peak) == 6


async def test_entry_gate_rejects_when_breaker_open() -> None:
    """组合门：熔断开启时拒绝（LLM_REQUEST_FAILED + 冷却剩余毫秒）。"""
    clock = _FakeClock()
    pool = LlmEntryPool(PoolsConfig(cb_fail_max=1, cb_reset_timeout_seconds=30))
    entry = _entry("e")
    breaker = pool.get_entry_breaker(entry)
    breaker._clock = clock  # type: ignore[attr-defined]  注入固定时钟
    breaker.record_failure()

    with pytest.raises(BusinessException) as exc_info:
        async with pool.entry_gate(entry):
            pass
    assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
    assert exc_info.value.details is not None
    assert "cooldown_remaining_ms" in exc_info.value.details


async def test_entry_gate_allows_and_yields_when_closed() -> None:
    """组合门：闭合态正常获取信号量并放行。"""
    pool = LlmEntryPool(PoolsConfig())
    entry = _entry("e", concurrency=2)
    async with pool.entry_gate(entry) as sem:
        assert sem is not None
    # 门退出后信号量归还（可再次进入）
    async with pool.entry_gate(entry):
        pass


async def test_entry_breaker_per_entry_isolation() -> None:
    """熔断器按条目键隔离：坏条目熔断不牵连同端点好模型。"""
    pool = LlmEntryPool(PoolsConfig(cb_fail_max=1))
    bad = _entry("bad", model="m-bad")
    good = _entry("good", model="m-good")
    pool.get_entry_breaker(bad).record_failure()
    assert pool.get_entry_breaker(bad).is_open is True
    assert pool.get_entry_breaker(good).is_open is False


async def test_reconfigure_clears_semaphores_keeps_breakers() -> None:
    """热重建：信号量清空（限值下调生效），熔断记忆保留。"""
    pool = LlmEntryPool(PoolsConfig())
    entry = _entry("e")  # 未声明并发 → 全局回落（热重建后新限值生效）
    pool.get_entry_semaphore(entry)
    pool.get_entry_breaker(entry).record_failure()

    pool.reconfigure(PoolsConfig(llm_semaphore_limit=2, cb_fail_max=9))
    assert pool.snapshot()["semaphores"] == 0
    assert pool.snapshot()["breakers"] == 1  # 熔断记忆保留
    # 下调后的限值生效（重建后按新配置注册）
    sem = pool.get_entry_semaphore(entry)
    assert sem._value == 2  # type: ignore[attr-defined]


async def test_close_clears_all() -> None:
    """close：全部注册状态清空。"""
    pool = LlmEntryPool(PoolsConfig())
    entry = _entry("e")
    pool.get_entry_semaphore(entry)
    pool.get_entry_breaker(entry)
    pool.close()
    assert pool.snapshot() == {"semaphores": 0, "breakers": 0}
    assert pool.limits() == {}


async def test_breaker_states_snapshot() -> None:
    """熔断状态快照（可观测性）。"""
    pool = LlmEntryPool(PoolsConfig(cb_fail_max=1))
    entry = _entry("e")
    pool.get_entry_breaker(entry).record_failure()
    states = pool.breaker_states()
    assert states[make_entry_key(entry)] == CircuitState.OPEN


async def test_build_llm_entry_pool_factory() -> None:
    """便捷工厂：启动期全量预注册条目信号量。"""
    entries = [_entry("a", concurrency=2), _entry("b", concurrency=3)]
    pool = build_llm_entry_pool(PoolsConfig(), entries)
    assert pool.snapshot()["semaphores"] == 2
    assert pool.get_entry_semaphore(entries[0]) is pool.get_entry_semaphore(entries[0])
