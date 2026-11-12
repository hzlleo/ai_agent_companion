"""LLM 条目池——per-entry 并发信号量 + 熔断状态机（纯池管道，无 HTTP 调用）。

本模块是 P0-4 LLM 网关的池管道前置交付：

    - :class:`CircuitBreaker`  —— 三态熔断状态机（closed / open /
      half-open），失败阈值 + 冷却时长均为 epoch-ms 计时（``now_ms`` 注入
      可测），零 sleep 依赖；
    - :class:`LlmEntryPool`    —— 按模型条目（provider / base_url / model
      三元组键）注册并发信号量与熔断器；``entry_gate`` 为组合门（信号量
      + 熔断预检），网关调用点经它进入。

熔断状态机::

    closed ──连续失败达 fail_max──> open
       ↑ 成功（计数清零）              │
       │                          冷却期满
       └──半开探测成功── half_open <──┘
                                   │ 探测失败
                                   └──> open（冷却重新计时）

信号量冲突规则（keep-max）：同键重复注册且限值相同幂等返回；新限值更大
立即替换（上调生效）；更小保留较大值并告警（下调依赖 ``clear`` 后按新
配置重建——热重建通道）。

P0 决策：NO Redis——全部状态进程内（单实例部署；多实例阶段再评审）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum

from loguru import logger

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.config.models import PoolsConfig
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms

__all__ = [
    "CircuitState",
    "CircuitBreaker",
    "LlmEntryPool",
    "make_entry_key",
]

#: 条目键三元组类型（provider, base_url 去尾斜杠, model）。
EntryKey = tuple[str, str, str]


def make_entry_key(entry: LlmModelEntry) -> EntryKey:
    """构造条目键（provider / base_url / model 三元组，base_url 去尾斜杠）。

    Args:
        entry: 模型条目。

    Returns:
        归一化三元组键——同端点不同 model 的条目互不阻塞。
    """
    return (entry.provider, entry.base_url.rstrip("/"), entry.model)


class CircuitState(StrEnum):
    """熔断器状态值域。

    - ``closed``：闭合（正常放行，计数失败）；
    - ``open``：开启（冷却期内拒绝一切请求）；
    - ``half_open``：半开（冷却期满，放行单次探测请求）。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """三态熔断器——失败阈值 + 冷却时长（epoch-ms 计时，时钟可注入）。

    线程/循环安全：单事件循环内使用（状态变更无 await 点）。

    Attributes:
        name: 熔断器名称（日志标识，通常为条目键）。
        fail_max: 连续失败阈值（达此值熔断）。
        reset_timeout_ms: 冷却时长（毫秒；冷却期满进入半开探测）。
    """

    def __init__(
        self,
        name: str,
        *,
        fail_max: int = 5,
        reset_timeout_ms: int = 60_000,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        """初始化熔断器（closed 态，零失败计数）。

        Args:
            name: 熔断器名称（日志标识）。
            fail_max: 连续失败阈值（下限 1）。
            reset_timeout_ms: 冷却时长毫秒（下限 0——0 即冷却即刻期满）。
            clock: 时钟函数注入点（默认 ``core.time_utils.now_ms``；
                测试注入固定时钟实现确定性状态机驱动，零 sleep）。
        """
        self.name = name
        self.fail_max = max(1, fail_max)
        self.reset_timeout_ms = max(0, reset_timeout_ms)
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at_ms = 0

    @property
    def state(self) -> CircuitState:
        """当前状态（查询即时性——open 态冷却期满时惰性翻转为 half_open）。"""
        if self._state == CircuitState.OPEN and self._cooldown_elapsed():
            self._state = CircuitState.HALF_OPEN
            logger.debug("熔断器冷却期满进入半开探测: {}", self.name)
        return self._state

    @property
    def is_open(self) -> bool:
        """是否处于拒绝态（open——half_open 视为可探测，不算拒绝态）。"""
        return self.state == CircuitState.OPEN

    @property
    def cooldown_remaining_ms(self) -> int:
        """open 态冷却剩余毫秒（非 open 态为 0；退避提示用）。"""
        if self._state != CircuitState.OPEN:
            return 0
        return max(0, self.reset_timeout_ms - (self._clock() - self._opened_at_ms))

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败计数（可观测性）。"""
        return self._consecutive_failures

    def allow_request(self) -> bool:
        """是否放行本次请求（open 态拒绝；冷却期满惰性转半开后放行探测）。

        Returns:
            放行返回 True；open 冷却期内返回 False。
        """
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        """记录一次成功——half_open 探测成功转 closed，计数清零。"""
        if self._state == CircuitState.HALF_OPEN:
            logger.info("熔断器半开探测成功，恢复闭合: {}", self.name)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """记录一次失败——closed 计数累积达阈值转 open；half_open 探测失败重回 open。"""
        if self._state == CircuitState.HALF_OPEN:
            self._trip_open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.fail_max:
            self._trip_open()

    def reset(self) -> None:
        """强制复位为 closed 态（测试 / 热重建用）。"""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at_ms = 0

    def _trip_open(self) -> None:
        """翻转为 open 态并重新计时冷却。"""
        self._state = CircuitState.OPEN
        self._opened_at_ms = self._clock()
        logger.warning(
            "熔断器开启（连续失败 {} 次达阈值），冷却 {} 毫秒: {}",
            self._consecutive_failures,
            self.reset_timeout_ms,
            self.name,
        )

    def _cooldown_elapsed(self) -> bool:
        """冷却是否期满（epoch-ms 比较）。"""
        return self._clock() - self._opened_at_ms >= self.reset_timeout_ms


class LlmEntryPool:
    """按模型条目的并发信号量 + 熔断器注册表（网关池管道）。

    信号量限值优先级：条目 ``concurrency`` → ``PoolsConfig.
    llm_semaphore_limit`` 回落。热重建：``clear`` 清空后按新配置重建
    （限值下调唯一生效路径——keep-max 规则不在进程内自动降级）。
    """

    def __init__(self, pools: PoolsConfig) -> None:
        """初始化条目池。

        Args:
            pools: 池配置（信号量回落限值 / 熔断阈值 / 冷却时长）。
        """
        self._pools = pools
        self._sems: dict[EntryKey, asyncio.Semaphore] = {}
        self._limits: dict[EntryKey, int] = {}
        self._breakers: dict[EntryKey, CircuitBreaker] = {}

    def entry_semaphore_limit(self, entry: LlmModelEntry) -> int:
        """解析条目生效并发限值（条目声明 → 全局回落）。

        Args:
            entry: 模型条目。

        Returns:
            生效限值（下限 1）。
        """
        limit = (
            entry.concurrency if entry.concurrency is not None else self._pools.llm_semaphore_limit
        )
        return max(1, limit)

    def get_entry_semaphore(self, entry: LlmModelEntry) -> asyncio.Semaphore:
        """获取条目专属并发信号量（keep-max 冲突规则）。

        Args:
            entry: 模型条目。

        Returns:
            该条目键专属的 :class:`asyncio.Semaphore`。
        """
        key = make_entry_key(entry)
        limit = self.entry_semaphore_limit(entry)

        old_limit = self._limits.get(key)
        if old_limit == limit and key in self._sems:
            return self._sems[key]
        if old_limit is not None:
            old_sem = self._sems.get(key)
            if old_sem is not None and limit < old_limit:
                logger.warning(
                    "条目信号量限值冲突，保留较大值: key={}, 已注册={}, 请求={}",
                    key,
                    old_limit,
                    limit,
                )
                return old_sem
            logger.info(
                "条目信号量限值上调，替换信号量: key={}, 旧={}, 新={}",
                key,
                old_limit,
                limit,
            )
        sem = asyncio.Semaphore(limit)
        self._sems[key] = sem
        self._limits[key] = limit
        logger.info("条目并发限值已注册: key={}, limit={}", key, limit)
        return sem

    def get_entry_breaker(self, entry: LlmModelEntry) -> CircuitBreaker:
        """获取条目专属熔断器（阈值来自池配置；首次访问按需创建）。

        Args:
            entry: 模型条目。

        Returns:
            该条目键专属的 :class:`CircuitBreaker`。
        """
        key = make_entry_key(entry)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                str(key),
                fail_max=self._pools.cb_fail_max,
                reset_timeout_ms=self._pools.cb_reset_timeout_seconds * 1000,
            )
            self._breakers[key] = breaker
        return breaker

    @asynccontextmanager
    async def entry_gate(self, entry: LlmModelEntry) -> AsyncIterator[asyncio.Semaphore]:
        """条目调用组合门——熔断预检 + 信号量获取（网关调用点入口）。

        网关（P0-4）用法::

            async with llm_entry_pool.entry_gate(entry):
                await client.chat(...)   # 成功 → breaker.record_success()
                                        # 失败 → breaker.record_failure()

        Args:
            entry: 模型条目。

        Yields:
            已获取的条目信号量（语义占位——调用方在门内执行单次调用）。

        Raises:
            BusinessException: 条目熔断器开启（``LLM_REQUEST_FAILED``，
                details 含条目键与冷却剩余毫秒——调用方可据此退避或走
                回退链）。
        """
        breaker = self.get_entry_breaker(entry)
        if breaker.is_open:
            raise BusinessException(
                code=ErrorCode.LLM_REQUEST_FAILED,
                details={
                    "reason": "模型条目熔断中",
                    "entry": make_entry_key(entry),
                    "cooldown_remaining_ms": breaker.cooldown_remaining_ms,
                },
            )
        sem = self.get_entry_semaphore(entry)
        async with sem:
            yield sem

    def snapshot(self) -> dict[str, int]:
        """当前注册规模快照（信号量 / 熔断器计数；可观测性）。

        Returns:
            ``{"semaphores": n, "breakers": m}``。
        """
        return {"semaphores": len(self._sems), "breakers": len(self._breakers)}

    def breaker_states(self) -> dict[EntryKey, CircuitState]:
        """全部熔断器状态快照（可观测性 / 测试）。

        Returns:
            ``{entry_key: CircuitState}``。
        """
        return {key: breaker.state for key, breaker in self._breakers.items()}

    def reconfigure(self, pools: PoolsConfig) -> None:
        """热重建——换池配置并清空信号量注册表（限值下调生效路径）。

        熔断器保留既有状态（配置只影响新建阈值；进程内熔断记忆跨配置
        重建保持——避免热重建瞬间旧故障记忆清零导致故障端点被立即打满）。

        Args:
            pools: 新池配置。
        """
        self._pools = pools
        self._sems.clear()
        self._limits.clear()
        logger.info("LLM 条目池已热重建（信号量注册表清空，熔断记忆保留）")

    def close(self) -> None:
        """清空全部注册状态（停机用——信号量无需显式释放）。"""
        self._sems.clear()
        self._limits.clear()
        self._breakers.clear()

    def limits(self) -> dict[EntryKey, int]:
        """当前注册限值快照（测试 / 可观测性）。

        Returns:
            ``{entry_key: limit}``。
        """
        return dict(self._limits)


def build_llm_entry_pool(pools: PoolsConfig, entries: Sequence[LlmModelEntry]) -> LlmEntryPool:
    """便捷工厂——按池配置与条目集预注册信号量。

    Args:
        pools: 池配置。
        entries: 模型条目序列（启动期全量预注册）。

    Returns:
        已注册全部条目信号量的 :class:`LlmEntryPool`。
    """
    pool = LlmEntryPool(pools)
    for entry in entries:
        pool.get_entry_semaphore(entry)
    return pool
