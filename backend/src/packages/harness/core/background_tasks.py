"""全局后台任务管理器——注册/跟踪/优雅终止后台任务。

设计决策（drain + 后台任务 await + 强制 cancel）
====================================================================

应用收到 ``SIGTERM`` 后，lifespan shutdown 流程会调用
``BackgroundTaskManager.shutdown(timeout=...)``：

1. 设置 ``is_shutting_down = True`` —— 后续代码可通过此标志感知停机，
   避免注册新任务或启动新的后台循环。
2. ``asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)``
   —— 给所有在途后台任务一段宽限期自然退出。
3. 超时仍未退出的任务调用 ``task.cancel()`` 强制取消，并再次 ``gather``
   收集 ``CancelledError``（用 ``return_exceptions=True`` 避免抛出）。
4. 完成回调 ``add_done_callback`` 会在任务正常结束后自动从 ``_tasks``
   集合中移除，避免集合无限增长。

全局单例
-------
``background_task_manager`` 是进程级单例，供：

- app 层 lifespan 在 shutdown 时调用 ``shutdown()``
- 任意业务代码通过 ``register()`` 注册长期运行的后台任务

用法::

    from packages.harness.core.background_tasks import background_task_manager

    task = asyncio.create_task(my_background_loop())
    background_task_manager.register(task)

    # 应用关闭时（lifespan shutdown）
    await background_task_manager.shutdown(timeout=30.0)

``TaskQueue`` 提供基于内存队列的后台任务调度与状态跟踪。
替代裸 ``asyncio.create_task``——任务入队后由固定数量的 worker 协程消费，
支持状态查询（pending/running/completed/failed）与结果获取。
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable, Set
from typing import Any, Final

from loguru import logger

from packages.harness.core.enums import TaskStatus
from packages.harness.core.errors import BusinessException, ErrorCode

# 默认优雅停机超时（秒）。
# 此常量仅作为本模块内部默认值；运行时应由 app 层从 config 读取并显式传入。
_DEFAULT_SHUTDOWN_TIMEOUT: Final[float] = 30.0

# 队列元素：task_id / name / func / args / kwargs / ctx（入队时的
# ContextVar 快照——worker 侧 ctx.run 恢复请求级上下文）。
_QueueItem = tuple[
    str,
    str,
    Callable[..., Awaitable[Any]],
    tuple[Any, ...],
    dict[str, Any],
    contextvars.Context,
]


class BackgroundTaskManager:
    """全局后台任务管理器——注册/跟踪/优雅终止后台任务。

    线程安全性
    ----------
    本类仅在单个 asyncio 事件循环内使用（FastAPI lifespan + 中间件 +
    路由处理器共享同一 loop）。``set`` 的 add/discard 与
    ``asyncio.Task.add_done_callback`` 均在 loop 线程内执行，无需加锁。

    重复注册
    -------
    ``register()`` 使用 ``set`` 去重——同一 task 多次注册不会产生多个
    ``done_callback``，也不会重复加入集合。

    完成回调
    -------
    每个 task 通过 ``add_done_callback(self._tasks.discard)`` 注册回调，
    任务结束后自动从集合中移除，避免集合无限增长。
    回调在 loop 线程同步执行（``asyncio.Future`` 的语义），开销极低。
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._shutting_down: bool = False

    # ------------------------------------------------------------------
    # 注册 / 查询
    # ------------------------------------------------------------------

    def register(self, task: asyncio.Task[object]) -> None:
        """注册一个后台 task 到管理器。

        - 同一 task 多次注册幂等（``set`` 去重）。
        - 注册时立即挂上 ``done_callback``，任务结束后自动从集合移除。
        - 停机阶段（``is_shutting_down=True``）仍允许注册——调用方负责
          决定是否在停机时启动新任务；管理器只负责跟踪与终止。

        Args:
            task: ``asyncio.create_task`` 返回的 Task 对象。
        """
        if task in self._tasks:
            # 幂等：已注册过则不重复挂回调
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def tasks(self) -> Set[asyncio.Task[object]]:
        """当前已注册、尚未完成的后台任务集合（只读视图）。

        返回的是 ``set`` 的引用快照——调用方不应修改。
        若需稳定快照，请显式 ``set(mgr.tasks)``。
        """
        return self._tasks

    @property
    def is_shutting_down(self) -> bool:
        """是否已进入优雅停机阶段。

        ``shutdown()`` 调用后置为 ``True``，全程不再重置。
        业务代码可在启动新后台循环前检查此标志，避免停机期间启动新任务。
        """
        return self._shutting_down

    def mark_shutting_down(self) -> None:
        """立即标记为停机状态（不 drain 任务）。

        供信号 handler 在收到 SIGTERM/SIGINT 的第一时间调用——
        此时 lifespan shutdown 尚未开始，但 ``is_shutting_down``
        标志需要立即生效，让中间件/入口层拒绝启动新的后台任务。

        实际的 task drain 仍由 :meth:`shutdown` 在 lifespan shutdown
        阶段完成（本方法仅设置标志，幂等可重复调用）。
        """
        self._shutting_down = True

    # ------------------------------------------------------------------
    # 优雅停机
    # ------------------------------------------------------------------

    async def shutdown(self, timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """优雅终止所有已注册的后台任务。

        流程：
            1. 标记 ``is_shutting_down=True``
            2. 若无任务，直接返回
            3. ``wait_for(gather(*tasks, return_exceptions=True), timeout=timeout)``
               给在途任务一段宽限期自然退出
            4. 超时后对仍未退出的任务调用 ``cancel()``，并再次 ``gather``
               收集 ``CancelledError``（``return_exceptions=True``）

        Args:
            timeout: 宽限期超时秒数（默认 30s）。

        注意:
            - ``return_exceptions=True`` 确保 ``gather`` 不因单个任务异常而
              提前返回，所有任务都能被等待。
            - 超时分支中二次 ``gather`` 也用 ``return_exceptions=True``，
              避免 ``CancelledError`` 上抛污染调用方。
            - 已通过 ``done_callback`` 自动移出集合的完成任务不会重复处理。
        """
        self._shutting_down = True

        if not self._tasks:
            logger.info("BackgroundTaskManager.shutdown: 无在途后台任务，直接返回")
            return

        logger.info(
            "BackgroundTaskManager.shutdown: 等待 {} 个后台任务退出（timeout={}s）",
            len(self._tasks),
            timeout,
        )

        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
            logger.info(
                "BackgroundTaskManager.shutdown: 所有后台任务已自然退出（共 {} 个）",
                len(self._tasks),
            )
        except TimeoutError:
            # 超时——对仍未退出的任务强制 cancel
            pending = [t for t in self._tasks if not t.done()]
            logger.warning(
                "BackgroundTaskManager.shutdown: 超时 {}s 后仍有 {} 个任务未退出，强制 cancel",
                timeout,
                len(pending),
            )
            for task in pending:
                task.cancel()
            # 收集 cancel 结果（CancelledError 不会上抛）
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info("BackgroundTaskManager.shutdown: 强制 cancel 完成")


# 全局单例——供 app 层 lifespan 与各业务模块共享
background_task_manager = BackgroundTaskManager()


# =========================================================================== []
# 内存任务队列 + 状态跟踪
# =========================================================================== []


class TaskRecord:
    """单条任务的状态记录。

    Attributes:
        task_id: 任务 ID（UUID）。
        status: 当前状态（pending/running/completed/failed/cancelled，
            见 ``core.enums.TaskStatus``）。
        result: 任务返回值（仅 ``completed`` 时有值）。
        error: 失败原因字符串（仅 ``failed`` 时有值）。
        created_at: 入队时间（单调时钟秒）。
        started_at: 开始执行时间。
        completed_at: 完成/失败时间。
        name: 任务名称（可选，便于日志追踪）。
    """

    __slots__ = (
        "task_id",
        "status",
        "result",
        "error",
        "created_at",
        "started_at",
        "completed_at",
        "name",
    )

    def __init__(self, task_id: str, name: str = "") -> None:
        """初始化任务记录（pending 态）。

        Args:
            task_id: 任务 ID（UUID 字符串）。
            name: 任务名称（日志与状态查询用，可选）。
        """
        self.task_id = task_id
        self.status: TaskStatus = TaskStatus.PENDING
        self.result: Any = None
        self.error: str | None = None
        self.created_at: float = time.monotonic()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.name = name


class TaskQueue:
    """内存任务队列——单实例后台任务调度与状态跟踪。

    替代裸 ``asyncio.create_task``。任务入队后由固定数量的
    worker 协程并发消费，支持状态查询（pending/running/completed/failed）
    与结果获取。

    限制
    ----
    - 仅限单实例部署（多实例需改用分布式队列；P0 单实例决策）
    - ``maxsize`` 满时 ``submit`` 阻塞直到有 worker 空闲
    - 任务结果保留至 ``max_results`` 条，超出后按 FIFO 淘汰

    用法::

        queue = TaskQueue(workers=4)
        await queue.start()
        task_id = await queue.submit("my_task", my_async_func, arg1, arg2)
        status = queue.get_status(task_id)
        await queue.stop()
    """

    def __init__(
        self,
        workers: int = 4,
        maxsize: int = 1000,
        max_results: int = 10000,
    ) -> None:
        """初始化任务队列（未启动）。

        Args:
            workers: worker 协程数量。
            maxsize: 队列最大容量；满时 ``submit`` 阻塞。
            max_results: 历史任务状态保留条数（FIFO 淘汰）。
        """
        self._workers_count = max(1, workers)
        self._maxsize = max(1, maxsize)
        self._max_results = max(1, max_results)
        self._queue: asyncio.Queue[_QueueItem] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._records: dict[str, TaskRecord] = {}
        self._results_order: list[str] = []
        self._started: bool = False
        self._shutting_down: bool = False

    @property
    def started(self) -> bool:
        """队列是否已启动。"""
        return self._started

    @property
    def is_shutting_down(self) -> bool:
        """是否已进入停机阶段。"""
        return self._shutting_down

    async def start(self) -> None:
        """启动 worker 协程。重复调用安全——已启动时为 no-op。"""
        if self._started:
            return
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=self._maxsize)
        self._queue = queue
        self._shutting_down = False
        for i in range(self._workers_count):
            worker = asyncio.create_task(self._worker_loop(i, queue))
            self._workers.append(worker)
        self._started = True
        logger.info(
            "TaskQueue 已启动: workers={}, maxsize={}",
            self._workers_count,
            self._maxsize,
        )

    async def submit(
        self,
        name: str,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """提交任务到队列。

        Args:
            name: 任务名称（用于日志与状态查询）。
            func: 异步可调用对象。
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            任务 ID（UUID 字符串）。

        Raises:
            BusinessException: 队列未启动或正在停机。
        """
        if not self._started or self._queue is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="TaskQueue 未启动，请先调用 start()",
            )
        if self._shutting_down:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="TaskQueue 正在停机，拒绝新任务",
            )

        task_id = str(uuid.uuid4())
        record = TaskRecord(task_id=task_id, name=name)
        self._records[task_id] = record
        self._results_order.append(task_id)
        self._evict_if_needed()

        # 上下文快照随任务传递：worker 协程创建于队列启动期（空上下文），
        # 不快照则请求级 ContextVar（session_id_var / persona_id_var 等）
        # 全部丢失——与 create_task 路径（天然快照当前上下文）语义对齐，
        # 消除两条调度路径的可见性差异。
        ctx = contextvars.copy_context()
        await self._queue.put((task_id, name, func, args, kwargs, ctx))
        logger.debug("TaskQueue 任务已入队: id={}, name={}", task_id, name)
        return task_id

    def get_status(self, task_id: str) -> TaskRecord | None:
        """查询任务状态。

        Args:
            task_id: 任务 ID。

        Returns:
            ``TaskRecord`` 实例；不存在时返回 ``None``。
        """
        return self._records.get(task_id)

    def list_tasks(self, limit: int = 100) -> list[TaskRecord]:
        """列出最近的任务记录（按入队时间倒序——最近提交的在前）。

        Args:
            limit: 返回的最大条数。

        Returns:
            ``TaskRecord`` 列表（最近入队的在前）。
        """
        if not self._results_order:
            return []
        recent = self._results_order[-limit:]
        # 倒序：最近入队的在前
        return [self._records[tid] for tid in reversed(recent) if tid in self._records]

    async def _worker_loop(self, worker_id: int, queue: asyncio.Queue[_QueueItem]) -> None:
        """单个 worker 协程的主循环。

        Args:
            worker_id: worker 序号（日志标识）。
            queue: 任务队列（start() 期创建并传入——worker 只经此参数
                访问队列，规避 Optional None 分支）。
        """
        logger.debug("TaskQueue worker-{} 启动", worker_id)
        while True:
            try:
                task_id, name, func, args, kwargs, ctx = await queue.get()
            except asyncio.CancelledError:
                logger.debug("TaskQueue worker-{} 被取消", worker_id)
                raise
            record = self._records.get(task_id)
            if record is None:
                # 状态记录已被淘汰，跳过执行
                queue.task_done()
                continue

            record.status = TaskStatus.RUNNING
            record.started_at = time.monotonic()

            try:
                # 在提交方上下文快照内驱动任务：ensure_future 于 ctx.run 内
                # 创建 Task，Task 上下文拷贝自快照——协程帧在提交方上下文
                # 中执行；worker 侧仅 await 该 Task（等待不涉及帧执行）。
                coro = func(*args, **kwargs)
                inner_task = ctx.run(asyncio.ensure_future, coro)
                try:
                    result = await inner_task
                except asyncio.CancelledError:
                    inner_task.cancel()
                    raise
                record.result = result
                record.status = TaskStatus.COMPLETED
                record.completed_at = time.monotonic()
                logger.debug("TaskQueue 任务完成: id={}, name={}", task_id, name)
            except asyncio.CancelledError:
                record.status = TaskStatus.CANCELLED
                record.completed_at = time.monotonic()
                logger.debug("TaskQueue 任务被取消: id={}, name={}", task_id, name)
                raise
            except Exception as exc:  # best-effort: worker 必须吞掉任务异常继续消费
                record.error = str(exc)
                record.status = TaskStatus.FAILED
                record.completed_at = time.monotonic()
                logger.opt(exception=True).warning(
                    "TaskQueue 任务失败: id={}, name={}, err={}", task_id, name, exc
                )
            finally:
                queue.task_done()

    def _evict_if_needed(self) -> None:
        """淘汰过期的任务记录（FIFO，超过 ``max_results`` 时移除最旧的）。"""
        while len(self._results_order) > self._max_results:
            old_id = self._results_order.pop(0)
            self._records.pop(old_id, None)

    async def stop(self, timeout: float = 30.0) -> None:
        """停止队列：标记停机 + 等待在途任务完成 + 取消 worker。

        Args:
            timeout: 等待队列排空的超时秒数。
        """
        if not self._started:
            return
        self._shutting_down = True

        # 等待队列中已入队的任务被消费完
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "TaskQueue.stop: 等待队列排空超时（{}s），强制取消 worker", timeout
                )

        # 取消所有 worker
        for worker in self._workers:
            if not worker.done():
                worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False
        logger.info("TaskQueue 已停止")


# 全局单例——供业务模块共享
task_queue = TaskQueue()
