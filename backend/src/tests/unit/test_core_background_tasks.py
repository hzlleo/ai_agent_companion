"""``core.background_tasks`` 单元测试——管理器 + 内存任务队列。

覆盖矩阵：

    - ``BackgroundTaskManager``：注册幂等 / done_callback 自动移除 /
      ``mark_shutting_down`` / ``shutdown``（无任务 / 自然退出 / 超时强杀）；
    - ``TaskQueue``：生命周期（start 幂等 / 未启动 stop 安全 / 未启动
      submit 拒绝 / 停机期 submit 拒绝）；
    - 任务执行：completed / failed / cancelled（超时停机触发）状态机 +
      ``TaskRecord`` 字段断言；
    - 上下文快照：提交方 ContextVar 随任务传递（ctx.run 恢复）；
    - 记录淘汰：``max_results`` FIFO 淘汰 / ``list_tasks`` 倒序。
"""

from __future__ import annotations

import asyncio

import pytest

from packages.harness.core.background_tasks import (
    BackgroundTaskManager,
    TaskQueue,
    TaskRecord,
    background_task_manager,
    task_queue,
)
from packages.harness.core.context import session_id_var
from packages.harness.core.enums import TaskStatus
from packages.harness.core.errors import BusinessException


class TestBackgroundTaskManagerRegistration:
    async def test_register_and_tasks_view(self) -> None:
        """注册后 tasks 视图可见。"""
        mgr = BackgroundTaskManager()

        async def _noop() -> None:
            pass

        task = asyncio.create_task(_noop())
        mgr.register(task)
        assert task in mgr.tasks
        await task
        # done_callback 自动移除
        assert task not in mgr.tasks

    async def test_register_is_idempotent(self) -> None:
        """同一 task 重复注册幂等（集合去重，单回调）。"""
        mgr = BackgroundTaskManager()

        started = asyncio.Event()

        async def _wait_gate() -> None:
            await started.wait()

        task = asyncio.create_task(_wait_gate())
        mgr.register(task)
        mgr.register(task)
        assert len(mgr.tasks) == 1
        started.set()
        await mgr.shutdown(timeout=1.0)

    async def test_mark_shutting_down_flag(self) -> None:
        """``mark_shutting_down`` 立即置位（幂等可重复调用）。"""
        mgr = BackgroundTaskManager()
        assert mgr.is_shutting_down is False
        mgr.mark_shutting_down()
        assert mgr.is_shutting_down is True
        mgr.mark_shutting_down()
        assert mgr.is_shutting_down is True


class TestBackgroundTaskManagerShutdown:
    async def test_shutdown_without_tasks_returns(self) -> None:
        """无在途任务直接返回。"""
        mgr = BackgroundTaskManager()
        await mgr.shutdown(timeout=1.0)
        assert mgr.is_shutting_down is True

    async def test_shutdown_drains_tasks_naturally(self) -> None:
        """宽限期内自然退出（无强制 cancel）。"""
        mgr = BackgroundTaskManager()
        done_order: list[str] = []

        async def _worker(name: str) -> None:
            await asyncio.sleep(0.01)
            done_order.append(name)

        for i in range(3):
            mgr.register(asyncio.create_task(_worker(f"w{i}")))
        await mgr.shutdown(timeout=5.0)
        assert sorted(done_order) == ["w0", "w1", "w2"]
        assert mgr.tasks == set()

    async def test_shutdown_timeout_force_cancels(self) -> None:
        """超时未退出的任务被强制 cancel（CancelledError 不上抛）。"""
        mgr = BackgroundTaskManager()
        cancelled_flags: list[bool] = []

        async def _hang_forever() -> None:
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                cancelled_flags.append(True)
                raise

        task = asyncio.create_task(_hang_forever())
        mgr.register(task)
        # 超时机制本身是被测对象——短宽限期 + 长挂起任务
        await mgr.shutdown(timeout=0.05)
        assert task.cancelled() is True
        assert cancelled_flags == [True]

    async def test_global_singleton_importable(self) -> None:
        """全局单例可导入且为管理器实例。"""
        assert isinstance(background_task_manager, BackgroundTaskManager)
        assert isinstance(task_queue, TaskQueue)


class TestTaskRecord:
    async def test_initial_state_is_pending(self) -> None:
        """初始记录：pending / 无结果 / 无错误 / 时间戳未落。"""
        rec = TaskRecord(task_id="t1", name="test")
        assert rec.task_id == "t1"
        assert rec.status == TaskStatus.PENDING
        assert rec.result is None
        assert rec.error is None
        assert rec.started_at is None
        assert rec.completed_at is None
        assert rec.name == "test"
        assert rec.created_at > 0


class TestTaskQueueLifecycle:
    async def test_start_is_idempotent(self) -> None:
        """二次 start 为 no-op；is_shutting_down 标志随 start/stop 流转。"""
        q = TaskQueue(workers=2)
        assert q.is_shutting_down is False
        await q.start()
        assert q.started is True
        await q.start()
        assert q.started is True
        await q.stop(timeout=1.0)
        # stop 后停机标志保持（下次 start 复位）
        assert q.is_shutting_down is True
        await q.start()
        assert q.is_shutting_down is False
        await q.stop(timeout=1.0)

    async def test_stop_without_start_is_safe(self) -> None:
        """未启动直接 stop 不抛异常。"""
        q = TaskQueue()
        await q.stop(timeout=1.0)
        assert q.started is False

    async def test_submit_before_start_raises(self) -> None:
        """未启动 submit 拒绝（INTERNAL_ERROR）。"""

        async def _noop() -> None:
            pass

        q = TaskQueue()
        with pytest.raises(BusinessException, match="未启动"):
            await q.submit("test", _noop)

    async def test_submit_during_shutdown_raises(self) -> None:
        """停机期 submit 拒绝（INTERNAL_ERROR——队列仍标记 started 时）。"""
        q = TaskQueue(workers=1)
        await q.start()
        # 模拟停机中：stop 半程状态（_shutting_down=True 且 _started=True）
        q._shutting_down = True
        try:

            async def _noop() -> None:
                pass

            with pytest.raises(BusinessException, match="停机"):
                await q.submit("test", _noop)
        finally:
            q._shutting_down = False
            await q.stop(timeout=1.0)


class TestTaskQueueExecution:
    async def test_submit_and_complete(self) -> None:
        """submit → worker 消费 → completed（result / 时间戳落位）。"""
        q = TaskQueue(workers=2)
        await q.start()

        async def _add(a: int, b: int) -> int:
            return a + b

        task_id = await q.submit("add", _add, 1, 2)
        # stop() 先等待队列排空（join）——确定性收口，无轮询
        await q.stop(timeout=5.0)

        record = q.get_status(task_id)
        assert record is not None
        assert record.status == TaskStatus.COMPLETED
        assert record.result == 3
        assert record.started_at is not None
        assert record.completed_at is not None
        assert record.error is None

    async def test_failed_task_records_error(self) -> None:
        """任务抛异常 → failed（error 携带原因，worker 不死）。"""
        q = TaskQueue(workers=1)
        await q.start()

        async def _boom() -> None:
            raise RuntimeError("kaboom")

        task_id = await q.submit("boom", _boom)
        await q.stop(timeout=5.0)

        record = q.get_status(task_id)
        assert record is not None
        assert record.status == TaskStatus.FAILED
        assert "kaboom" in (record.error or "")
        assert record.completed_at is not None

    async def test_worker_survives_failure_and_consumes_next(self) -> None:
        """失败任务后 worker 继续消费后续任务（best-effort 吞异常语义）。"""
        q = TaskQueue(workers=1)
        await q.start()

        async def _boom() -> None:
            raise RuntimeError("first-fails")

        async def _ok() -> int:
            return 42

        bad_id = await q.submit("boom", _boom)
        good_id = await q.submit("ok", _ok)
        await q.stop(timeout=5.0)

        bad_record = q.get_status(bad_id)
        good_record = q.get_status(good_id)
        assert bad_record is not None and bad_record.status == TaskStatus.FAILED
        assert good_record is not None and good_record.status == TaskStatus.COMPLETED
        assert good_record.result == 42

    async def test_timeout_stop_cancels_running_task(self) -> None:
        """stop 超时 → worker 被 cancel → 在途任务落 cancelled 态。"""
        q = TaskQueue(workers=1)
        await q.start()

        async def _hang() -> None:
            await asyncio.sleep(30.0)

        task_id = await q.submit("hang", _hang)
        # 超时机制本身是被测对象：join 短超时触发 worker 强杀
        await q.stop(timeout=0.05)

        record = q.get_status(task_id)
        assert record is not None
        assert record.status == TaskStatus.CANCELLED

    async def test_get_status_unknown_returns_none(self) -> None:
        """未知 task_id → None。"""
        q = TaskQueue()
        assert q.get_status("nonexistent") is None

    async def test_list_tasks_recent_first(self) -> None:
        """list_tasks 倒序（最近入队在前）。"""
        q = TaskQueue(workers=1)
        await q.start()

        async def _noop(tag: str) -> str:
            return tag

        ids = [await q.submit(f"n{i}", _noop, f"n{i}") for i in range(3)]
        await q.stop(timeout=5.0)

        listed = q.list_tasks(limit=3)
        assert [rec.task_id for rec in listed] == list(reversed(ids))

    async def test_list_tasks_limit_and_empty(self) -> None:
        """limit 截断；空队列返回空列表。"""
        q = TaskQueue(workers=1)
        await q.start()

        async def _noop() -> None:
            pass

        for i in range(5):
            await q.submit(f"n{i}", _noop)
        await q.stop(timeout=5.0)
        assert len(q.list_tasks(limit=2)) == 2
        assert TaskQueue().list_tasks() == []


class TestTaskQueueContextPropagation:
    async def test_submit_context_snapshot_restored_in_task(self) -> None:
        """提交方 ContextVar 随任务传递（worker 侧 ctx.run 恢复）。"""
        q = TaskQueue(workers=1)
        await q.start()

        captured: list[str | None] = []

        async def _capture() -> None:
            captured.append(session_id_var.get())

        token = session_id_var.set("session-from-submitter")
        try:
            task_id = await q.submit("capture", _capture)
        finally:
            session_id_var.reset(token)
        await q.stop(timeout=5.0)

        record = q.get_status(task_id)
        assert record is not None
        assert record.status == TaskStatus.COMPLETED
        assert captured == ["session-from-submitter"]
        # worker 上下文不泄漏回提交方之外的任务
        assert session_id_var.get() is None


class TestTaskQueueRecordEviction:
    async def test_max_results_fifo_eviction(self) -> None:
        """max_results 淘汰：最旧记录被移除（新记录保留）。"""
        q = TaskQueue(workers=1, max_results=3)
        await q.start()

        async def _noop(tag: str) -> str:
            return tag

        first_id = await q.submit("first", _noop, "first")
        for i in range(3):
            await q.submit(f"evict-{i}", _noop, f"evict-{i}")
        await q.stop(timeout=5.0)

        assert q.get_status(first_id) is None
        assert len(q.list_tasks(limit=10)) == 3
