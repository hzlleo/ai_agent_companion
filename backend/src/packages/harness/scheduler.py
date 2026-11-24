"""harness 中央调度器——手滚 async 调度（锁定决策：不引入 APScheduler）。

职责链（单一运行面 :meth:`Scheduler.run`）::

    Scheduler(tick_seconds=?, clock=?, sleep=?)
        ↓ register_interval(name, factory, every_seconds)   间隔作业
        ↓ register_daily(name, factory, daily_at="HH:MM")   每日作业
        ↓ schedule_once(name, factory, delay_ms)            一次性延迟作业
        ↓ run()（managed background task——app lifespan 装配）
        ↓ tick 循环（默认 1s）：
        ↓     clock() 取当前 epoch 毫秒 → 逐作业判定到期
        ↓     到期 → **先锚定推进 next_run**（drift-safe）→ create_task
        ↓       执行（per-job 错误隔离——异常仅日志，调度不中断）
        ↓     await sleep(tick_seconds)
        ↓ request_stop() → 循环退出 → 在途作业排水（bounded → cancel 兜底）

锁定设计决策：

    - **手滚调度**（无 APScheduler / cron 解析 / 时区数据库依赖）：
      作业面为间隔（``every_seconds``）、每日定时（``daily_at`` "HH:MM"）
      与一次性延迟（``schedule_once``）三类——duty / dream / 提醒扫描与
      心流门控的延迟回复批次共用同一条计时链；
    - **naive 本地时间**（文档化）：每日作业的 "HH:MM" 按进程本地时区
      的墙钟语义解析（``datetime.fromtimestamp`` 无 tz 参数——与
      ``tools.get_current_time`` 同约定）；不引入 zoneinfo / DST 规则；
    - **drift-safe 锚定推进**（epoch 毫秒轴）：

        - 间隔作业：``next = 上次计划时刻 + interval``（非 ``now +
          interval``——执行耗时不会累计漂移；错过多槽时每 tick 至多
          补一槽，防风暴）；
        - 每日作业：``next = 参考时刻之后下一个 HH:MM 出现``（参考 =
          上次计划时刻——补槽同样每 tick 至多一次）；

    - **per-job 错误隔离**：作业工厂异常仅落日志（调度循环不受影响）；
      作业经 ``create_task`` 并发执行（长作业不阻塞 tick——在途集合
      停机排水收口）；
    - **时钟 / 休眠注入**：``clock``（epoch 毫秒）与 ``sleep`` 均可
      构造注入——测试确定性驱动（零真实等待）。

长轮询通道 runner **不是**调度器作业（独立 managed background task——
channels 域自持循环，见 app/main.py 装配顺序）。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.gateway import LlmGateway
from packages.harness.memory.dreaming import DreamService
from packages.harness.memory.store import MemoryStore
from packages.harness.persona.service import PersonaService, bind_persona_context

__all__ = [
    "DAILY_AT_PATTERN",
    "parse_daily_at",
    "next_daily_occurrence_ms",
    "Scheduler",
    "build_dream_trigger_job",
]

#: 每日定时 "HH:MM" 的合法形态（0-23 时 : 0-59 分——无 cron 语法）。
DAILY_AT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: 作业工厂契约——零参协程工厂（每轮触发时调用创建新协程）。
JobFactory = Callable[[], Coroutine[Any, Any, None]]

#: 时钟契约（epoch 毫秒——注入式，测试可控）。
Clock = Callable[[], int]

#: 休眠契约（秒——注入式，测试可控）。
Sleep = Callable[[float], Awaitable[None]]


def parse_daily_at(daily_at: str) -> tuple[int, int]:
    """解析每日定时 "HH:MM"（无 cron 语法——仅时: 分二元组）。

    Args:
        daily_at: 每日定时字符串（"22:00" 形态）。

    Returns:
        ``(hour, minute)`` 二元组。

    Raises:
        BusinessException: 形态非法（``VALIDATION_ERROR``——装配错误面）。
    """
    matched = DAILY_AT_PATTERN.match(daily_at.strip())
    if matched is None:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="每日定时须为 HH:MM 形态（24 小时制，如 22:00）",
            details={"daily_at": daily_at},
        )
    return int(matched.group(1)), int(matched.group(2))


def next_daily_occurrence_ms(now_ms: int, hour: int, minute: int) -> int:
    """计算参考时刻之后下一个 "HH:MM" 的 epoch 毫秒（naive 本地时间）。

    语义（文档化）：今日 ``HH:MM`` 严格晚于参考时刻则取今日；否则（已
    过或恰为当下）取明日——**下一个出现**语义。本地墙钟解析（无时区
    数据库；DST 切换日按本地时钟偏移自然对齐）。

    Args:
        now_ms: 参考时刻（epoch 毫秒——注入式）。
        hour: 小时（0-23）。
        minute: 分钟（0-59）。

    Returns:
        下一个出现的 epoch 毫秒（严格大于参考时刻）。
    """
    local = datetime.fromtimestamp(now_ms / 1000)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return int(candidate.timestamp() * 1000)


@dataclass
class _JobSpec:
    """注册作业（内部运行态——规格 + 锚定推进轴）。

    Attributes:
        name: 作业名（唯一——注册表键）。
        factory: 作业工厂（每轮触发创建新协程）。
        interval_ms: 间隔毫秒（间隔作业；每日作业为 None）。
        daily_at: 每日定时二元组（每日作业；间隔作业为 None）。
        next_run_ms: 下次计划执行时刻（epoch 毫秒——drift-safe 锚定轴）。
        one_shot: 是否为一次性作业（到期执行后从作业表移除，不再重排；
            心流门控的延迟回复经此面排程）。
    """

    name: str
    factory: JobFactory
    interval_ms: int | None = None
    daily_at: tuple[int, int] | None = None
    next_run_ms: int = 0
    one_shot: bool = False

    def advance(self) -> None:
        """锚定推进下次执行时刻（以当前计划时刻为锚——非 now）。

        间隔作业：``next = 计划时刻 + interval``；每日作业：``next =
        计划时刻之后下一个 HH:MM``。错过多槽时推进后仍可能到期——由
        调度循环按 tick 节奏逐槽补齐（每 tick 至多一槽，防风暴）。
        """
        anchor = self.next_run_ms
        if self.one_shot:
            return
        if self.interval_ms is not None:
            self.next_run_ms = anchor + self.interval_ms
        elif self.daily_at is not None:
            hour, minute = self.daily_at
            self.next_run_ms = next_daily_occurrence_ms(anchor, hour, minute)


@dataclass
class _SchedulerState:
    """调度器运行态（内部——作业表 + 在途集合 + 停机事件）。

    Attributes:
        jobs: 作业注册表（名 → 规格）。
        inflight: 在途作业任务集合（停机排水收口）。
        stop_event: 优雅停机事件。
    """

    jobs: dict[str, _JobSpec] = field(default_factory=dict)
    inflight: set[asyncio.Task[None]] = field(default_factory=set)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


class Scheduler:
    """中央调度器（间隔 / 每日 / 一次性延迟作业；tick 循环 + per-job 隔离）。

    构造注入（测试友好）：``clock`` / ``sleep`` 可替换——测试以假时钟
    与零耗时休眠确定性驱动；生产用缺省 ``core.now_ms`` / ``asyncio.sleep``。
    生命周期：``register_*`` 先行 → ``run()`` 经 ``create_task`` 托管
    （app lifespan 装配）→ ``request_stop()`` 优雅收束。
    """

    def __init__(
        self,
        *,
        tick_seconds: float = 1.0,
        clock: Clock = now_ms,
        sleep: Sleep = asyncio.sleep,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        """初始化调度器（未启动——注册面先行）。

        Args:
            tick_seconds: tick 循环间隔秒（> 0；到期判定分辨率下限）。
            clock: 时钟（epoch 毫秒；注入式——测试可控）。
            sleep: 休眠函数（秒；注入式——测试可控）。
            drain_timeout_seconds: 停机排水等待封顶秒（超时 cancel 兜底）。
        """
        if tick_seconds <= 0:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="tick_seconds 须为正数",
                details={"tick_seconds": tick_seconds},
            )
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._sleep = sleep
        self._drain_timeout_seconds = drain_timeout_seconds
        self._state = _SchedulerState()
        self._started = False

    # ------------------------------------------------------------------
    # 注册面（run 之前 / 运行期均可——热注册）
    # ------------------------------------------------------------------

    def register_interval(self, name: str, factory: JobFactory, *, every_seconds: float) -> None:
        """注册间隔作业（首次执行 = 注册时刻 + 间隔；此后锚定推进）。

        Args:
            name: 作业名（唯一——重复注册 ValueError fail loud）。
            factory: 作业工厂（每轮触发创建新协程）。
            every_seconds: 执行间隔秒（> 0；毫秒分辨率下限 1ms）。

        Raises:
            BusinessException: 作业名重复 / 间隔非正（``VALIDATION_ERROR``
                ——装配错误面）。
        """
        if every_seconds <= 0:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="every_seconds 须为正数",
                details={"every_seconds": every_seconds},
            )
        interval_ms = max(1, int(round(every_seconds * 1000)))
        now = self._clock()
        self._register(
            _JobSpec(
                name=name,
                factory=factory,
                interval_ms=interval_ms,
                next_run_ms=now + interval_ms,
            )
        )
        logger.info(
            "调度作业已注册（间隔）: name={}, every_seconds={}, first_run_ms={}",
            name,
            every_seconds,
            now + interval_ms,
        )

    def register_daily(self, name: str, factory: JobFactory, *, daily_at: str) -> None:
        """注册每日作业（naive 本地时间 "HH:MM"——下一个出现语义）。

        Args:
            name: 作业名（唯一——重复注册 ValueError fail loud）。
            factory: 作业工厂（每轮触发创建新协程）。
            daily_at: 每日定时（"HH:MM" 24 小时制——无 cron 语法）。

        Raises:
            BusinessException: 作业名重复 / 定时形态非法（``VALIDATION_ERROR``）。
        """
        hour, minute = parse_daily_at(daily_at)
        now = self._clock()
        first_run = next_daily_occurrence_ms(now, hour, minute)
        self._register(
            _JobSpec(
                name=name,
                factory=factory,
                daily_at=(hour, minute),
                next_run_ms=first_run,
            )
        )
        logger.info(
            "调度作业已注册（每日）: name={}, daily_at={}, first_run_ms={}",
            name,
            daily_at,
            first_run,
        )

    def schedule_once(self, name: str, factory: JobFactory, *, delay_ms: int) -> str:
        """注册一次性延迟作业（到期执行一次后自动移除）。

        消费面：心流门控的延迟 / 合并回复排程——延迟消息与合并批次经
        中央调度器统一计时，不新建线程或独立定时器。作业名由调用方
        给出（须在在途集合内唯一）；作业到期经 ``_dispatch_due`` 执行
        ——执行前即从注册表移除，**不会重复触发**。调度器停机时仍在途
        的一次性作业随停机排水收尾；尚未到期且调度器已退出时该作业
        不再执行（停机语义文档化）。

        Args:
            name: 作业名（在途集合内唯一——重复注册 fail loud）。
            factory: 作业工厂（到期调用一次创建协程）。
            delay_ms: 延迟毫秒（≥0；相对当前注入时钟的偏移）。

        Returns:
            作业名（调用方观测用；与入参同名）。

        Raises:
            BusinessException: 延迟为负 / 作业名已注册（``VALIDATION_ERROR``
                ——装配错误面）。
        """
        if delay_ms < 0:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="一次性作业延迟须为非负数",
                details={"name": name, "delay_ms": delay_ms},
            )
        now = self._clock()
        self._register(
            _JobSpec(
                name=name,
                factory=factory,
                next_run_ms=now + delay_ms,
                one_shot=True,
            )
        )
        logger.info(
            "调度作业已注册（一次性）: name={}, delay_ms={}, due_ms={}",
            name,
            delay_ms,
            now + delay_ms,
        )
        return name

    def _register(self, spec: _JobSpec) -> None:
        """落入注册表（重名 fail loud——装配错误面）。

        Args:
            spec: 作业规格。

        Raises:
            BusinessException: 作业名已注册（``VALIDATION_ERROR``）。
        """
        if spec.name in self._state.jobs:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="调度作业名已注册（重名装配错误）",
                details={"name": spec.name, "registered": list(self._state.jobs)},
            )
        self._state.jobs[spec.name] = spec

    # ------------------------------------------------------------------
    # 观测面
    # ------------------------------------------------------------------

    @property
    def started(self) -> bool:
        """是否处于运行态（run 已进入 tick 循环）。"""
        return self._started

    @property
    def job_names(self) -> list[str]:
        """已注册作业名列表（注册序）。"""
        return list(self._state.jobs)

    def next_run_ms(self, name: str) -> int:
        """查询作业下次计划执行时刻（drift-safe 锚定轴观测面）。

        Args:
            name: 作业名。

        Returns:
            下次计划执行时刻（epoch 毫秒）。

        Raises:
            BusinessException: 作业未注册（``NOT_FOUND``——观测面错误）。
        """
        spec = self._state.jobs.get(name)
        if spec is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="调度作业未注册",
                details={"name": name, "registered": list(self._state.jobs)},
            )
        return spec.next_run_ms

    # ------------------------------------------------------------------
    # 运行面（managed background task 消费）
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求优雅停机（tick 循环退出；在途作业排水收口）。"""
        self._state.stop_event.set()

    async def run(self) -> None:
        """tick 循环主体（到期判定 → 锚定推进 → 并发执行；幂等启动）。

        停机语义：``request_stop`` 置位后当前 sleep 醒来即退出（停机
        延迟 ≤ tick_seconds）；随后在途作业 bounded 排水（封顶
        ``drain_timeout_seconds``）——超时 cancel 兜底。

        Raises:
            BusinessException: 重复 run（已处于运行态——装配错误面）。
        """
        if self._started:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="调度器已在运行（重复 run 装配错误）",
            )
        self._started = True
        try:
            while not self._state.stop_event.is_set():
                self._dispatch_due(self._clock())
                await self._sleep(self._tick_seconds)
        finally:
            self._started = False
            await self._drain_inflight()

    # ------------------------------------------------------------------
    # 内部——到期分发 / 执行隔离 / 排水
    # ------------------------------------------------------------------

    def _dispatch_due(self, now: int) -> None:
        """到期判定 + 锚定推进 + 并发执行（每作业每 tick 至多一槽）。

        Args:
            now: 当前时刻（epoch 毫秒——注入时钟读取值）。
        """
        for spec in list(self._state.jobs.values()):
            if spec.next_run_ms > now:
                continue
            if spec.one_shot:
                # 一次性作业先移除再执行——不会重复触发，作业名可在执行后
                # 立即复用（心流门控的后续批次排程依赖该语义）。
                self._state.jobs.pop(spec.name, None)
            else:
                spec.advance()
            task: asyncio.Task[None] = asyncio.create_task(
                self._run_job(spec.name, spec.factory),
                name=f"scheduler-job:{spec.name}",
            )
            self._state.inflight.add(task)
            task.add_done_callback(self._on_job_done)

    async def _run_job(self, name: str, factory: JobFactory) -> None:
        """执行单轮作业（异常隔离——仅日志，不上抛不停摆）。

        Args:
            name: 作业名（日志面）。
            factory: 作业工厂。
        """
        started_ms = self._clock()
        try:
            await factory()
        except asyncio.CancelledError:
            logger.info("调度作业被取消（停机兜底路径）: job={}", name)
            raise
        except Exception:  # per-job 错误隔离——调度循环不受影响
            logger.opt(exception=True).error("调度作业执行失败（已隔离）: job={}", name)
        finally:
            logger.debug(
                "调度作业轮次收束: job={}, latency_ms={}",
                name,
                self._clock() - started_ms,
            )

    def _on_job_done(self, task: asyncio.Task[None]) -> None:
        """在途集合回收（done callback——终态异常双保险日志）。"""
        self._state.inflight.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("调度作业任务异常终态: {}", exc)

    async def _drain_inflight(self) -> None:
        """停机排水（bounded 等待在途作业 → 超时 cancel 兜底）。"""
        pending = [task for task in self._state.inflight if not task.done()]
        self._state.inflight.clear()
        if not pending:
            return
        logger.info(
            "调度器停机：等待 {} 个在途作业收尾（封顶 {}s）",
            len(pending),
            self._drain_timeout_seconds,
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=self._drain_timeout_seconds,
            )
            logger.info("调度器停机：在途作业全部收尾")
        except TimeoutError:
            logger.warning("调度器停机排水超时——强制取消剩余在途作业")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


# =========================================================================== []
# 梦境触发作业（DreamService 包装——每日定时消费面）
# =========================================================================== []


def build_dream_trigger_job(
    *,
    dream_service: DreamService,
    gateway: LlmGateway,
    persona_service: PersonaService,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    store: MemoryStore | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> JobFactory:
    """构建梦境触发作业工厂（主人格解析 → 绑定 → run_dream）。

    链路：注入会话 → ``get_primary_persona``（无主人格 → 日志跳过，正常
    返回）→ ``bind_persona_context`` → ``DreamService.run_dream``
    （dream 角色复用既有链路——fail-closed 返回终态记录）。异常面由
    调度器 per-job 隔离兜底（run_dream 自身已 fail-closed；此处残余
    异常仅会话 / 人格解析链路）。

    Args:
        dream_service: 梦境整理服务（dreaming chunk 复用——as-is）。
        gateway: LLM 网关（dream 角色路由）。
        persona_service: 人格注册表服务（主人格解析）。
        session_factory: 数据库会话工厂（每轮作业开新会话——app 装配
            注入 ``session_scope``）。
        store: 记忆检索存储（None = 主题相关度降级 boost=0）。
        clock_ms: 注入时钟（可调用，返回 epoch 毫秒；None = 真实时间
            ——直接透传给 run_dream 作为开始 / 结束读数轴）。

    Returns:
        作业工厂（零参协程——``scheduler.register_daily`` 消费）。
    """

    async def _dream_job() -> None:
        """单轮梦境作业（无主人格为正常跳过——日志面）。"""
        async with session_factory() as session:
            try:
                record = await persona_service.get_primary_persona(session)
            except BusinessException as exc:
                # 无主人格为已知确定态（NOT_FOUND）→ 日志跳过正常返回；
                # 其余业务异常交调度器 per-job 隔离兜底（与原语义一致）。
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                logger.info("梦境调度跳过：无主人格")
                return
            async with bind_persona_context(record.id):
                run = await dream_service.run_dream(
                    session,
                    gateway=gateway,
                    persona_name=record.display_name,
                    store=store,
                    clock_ms=clock_ms,
                )
        logger.info(
            "梦境调度轮次收束: status={}, promoted={}, superseded={}",
            run.status,
            run.stats.promoted,
            run.stats.superseded,
        )

    return _dream_job
