"""qq 通道 runner 测试——有界队列 / 去重 / 丢弃计数 / 观测 / 生命周期。

零真实网络与零真实等待：路由器为进程内 fake（事件门控），时钟为可推进
伪时钟；断言只依赖队列读数与事件门，不使用固定 sleep。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.qq.qq_runner import (
    QQ_INBOUND_QUEUE_SIZE,
    QQ_STATS_INTERVAL_MS,
    QqWebhookRunner,
)

_WAIT_TIMEOUT: float = 5.0


class _Clock:
    """可推进的测试时钟（epoch 毫秒——观测节流输入）。"""

    def __init__(self, now_ms: int = 1_731_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


class RecordingRouter:
    """fake 路由器（记录入站；可选闸门阻塞在途处理）。"""

    def __init__(self, *, hold: asyncio.Event | None = None) -> None:
        self.received: list[InboundMessageModel] = []
        self.started = asyncio.Event()
        self._hold = hold

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        self.started.set()
        if self._hold is not None:
            await self._hold.wait()
        self.received.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.QQ, chat_id=message.chat_id, message_ids=["1"]
        )


class _FailOnceRouter:
    """fake 路由器（首条抛异常、后续正常处理——worker 隔离验证）。"""

    def __init__(self) -> None:
        self.attempted: list[str] = []
        self.handled: list[str] = []
        self.second_handled = asyncio.Event()

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        self.attempted.append(message.message_id)
        if len(self.attempted) == 1:
            raise RuntimeError("首条处理失败")
        self.handled.append(message.message_id)
        self.second_handled.set()
        return ChannelDeliveryResultModel(
            channel=ChannelType.QQ, chat_id=message.chat_id, message_ids=["1"]
        )


def _message_event(
    message_id: int = 1001,
    *,
    post_type: str = "message",
    text: str = "你好",
) -> dict[str, Any]:
    return {
        "post_type": post_type,
        "time": 1731000000,
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": 10086,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def _runner(
    *,
    router: Any,
    clock: _Clock,
    inbound_queue_size: int = 4,
    worker_count: int = 1,
) -> QqWebhookRunner:
    return QqWebhookRunner(
        router=router,
        inbound_queue_size=inbound_queue_size,
        worker_count=worker_count,
        clock=clock,
    )


async def _wait_until(predicate: Any) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_TIMEOUT
    while not predicate():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("等待条件超时")
        await asyncio.wait_for(asyncio.sleep(0), timeout=remaining)


class TestQueueBackpressure:
    async def test_default_queue_capacity_matches_wecom_style(self) -> None:
        assert QQ_INBOUND_QUEUE_SIZE == 64

    async def test_queue_overflow_drops_counts_and_logs(self) -> None:
        """队列满：丢弃 + 计数 + ERROR 含 msgid / 深度；不登记去重。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock, inbound_queue_size=2)
        assert runner.submit(_message_event(1)) is True
        assert runner.submit(_message_event(2)) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        try:
            assert runner.submit(_message_event(3)) is False
        finally:
            logger.remove(handler_id)
        assert runner.dropped_inbound == 1
        assert runner.pending_inbound == 2
        assert any(
            "QQ 入站队列已满" in message
            and "message_id=3" in message
            and "depth=2" in message
            and "dropped=1" in message
            for message in captured
        )

    async def test_dropped_message_is_not_dedup_registered_and_retry_succeeds(self) -> None:
        """丢弃不登记去重：腾出队列后同 msgid 重投可重新消费。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock(), inbound_queue_size=1)
        assert runner.submit(_message_event(10)) is True
        assert runner.submit(_message_event(11)) is False
        assert runner.dropped_inbound == 1
        runner._inbound_queue.get_nowait()
        assert runner.submit(_message_event(11)) is True
        assert runner.pending_inbound == 1
        assert runner.dropped_inbound == 1

    async def test_duplicate_message_absorbed_by_dedup(self) -> None:
        """同 message_id 重投只入队一次（首见登记）。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        assert runner.submit(_message_event(20)) is True
        assert runner.submit(_message_event(20)) is True
        assert runner.pending_inbound == 1

    async def test_unroutable_event_not_dedup_registered(self) -> None:
        """不可路由事件不登记：随后同 message_id 的可路由消息照常入队。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        assert runner.submit(_message_event(30, post_type="notice")) is False
        assert runner.pending_inbound == 0
        assert runner.submit(_message_event(30)) is True
        assert runner.pending_inbound == 1


class TestLifecycle:
    async def test_worker_consumes_and_stop_leaves_no_residue(self) -> None:
        """worker 正常消费；停机后队列清空且 submit 拒绝新事件。"""
        router = RecordingRouter()
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_message_event(40)) is True
            await _wait_until(lambda: len(router.received) == 1)
            assert runner.pending_inbound == 0
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)
        assert runner.pending_inbound == 0
        assert runner.stopping is True
        assert runner.submit(_message_event(41)) is False

    async def test_worker_isolates_handler_exception_and_continues(self) -> None:
        """worker 记录首条异常后继续消费第二条，runner 不停机。"""
        router = _FailOnceRouter()
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_message_event(70)) is True
            assert runner.submit(_message_event(71)) is True
            await asyncio.wait_for(router.second_handled.wait(), timeout=_WAIT_TIMEOUT)
            assert router.attempted == ["70", "71"]
            assert router.handled == ["71"]
            assert any(
                "QQ 入站消息处理异常" in message and "message_id=70" in message
                for message in captured
            )
            assert runner.stopping is False
            assert task.done() is False
        finally:
            logger.remove(handler_id)
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)

    async def test_stop_cancels_inflight_worker_and_clears_queue(self) -> None:
        """停机取消在途 worker 并清空待处理队列（零残留）。"""
        hold = asyncio.Event()
        router = RecordingRouter(hold=hold)
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_message_event(50)) is True
            await _wait_until(lambda: router.started.is_set())
            assert runner.submit(_message_event(51)) is True
            assert runner.pending_inbound == 1
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)
        assert runner.pending_inbound == 0
        assert router.received == []


class TestPeriodicObservation:
    async def test_stats_logged_after_interval_with_backlog(self) -> None:
        """推进到间隔点且有积压时输出 INFO（深度 / 增量）。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock)
        assert runner.submit(_message_event(60)) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            clock.advance_ms(QQ_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert any(
            "QQ 入站队列观测" in message and "pending=1" in message and "dropped_delta=0" in message
            for message in captured
        )

    async def test_stats_silent_without_backlog_or_drop(self) -> None:
        """无积压且无丢弃时不输出（间隔到达也静默）。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            clock.advance_ms(QQ_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert not any("QQ 入站队列观测" in message for message in captured)

    async def test_stats_throttled_before_interval(self) -> None:
        """未到间隔点即使有积压也不输出。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            assert runner.submit(_message_event(61)) is True
        finally:
            logger.remove(handler_id)
        assert not any("QQ 入站队列观测" in message for message in captured)
