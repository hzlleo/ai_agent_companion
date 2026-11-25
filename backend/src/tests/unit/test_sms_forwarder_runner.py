"""SmsForwarder runner 测试——有界队列 / 去重 / 丢弃计数 / 观测 / 生命周期。

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
from packages.harness.channels.sms_forwarder.sms_models import (
    SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS,
)
from packages.harness.channels.sms_forwarder.sms_runner import (
    SMS_FORWARDER_INBOUND_QUEUE_SIZE,
    SMS_FORWARDER_STATS_INTERVAL_MS,
    SmsForwarderWebhookRunner,
)
from tests.async_wait import wait_until

_WAIT_TIMEOUT: float = 5.0


class _Clock:
    """可推进的测试时钟（epoch 毫秒——观测节流 / 去重窗口输入）。"""

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
            channel=ChannelType.SMS_FORWARDER,
            chat_id=message.chat_id,
            message_ids=[],
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
            channel=ChannelType.SMS_FORWARDER,
            chat_id=message.chat_id,
            message_ids=[],
        )


def _payload(
    *,
    msgid: str = "m-1",
    text: str = "你好",
    kind: str = "SMS",
    sender: str = "13800138000",
) -> dict[str, Any]:
    """构造转发载荷（测试直供形态）。"""
    return {
        "from": sender,
        "content": text,
        "kind": kind,
        "msgid": msgid,
        "timestamp": 1_731_000_000_000,
    }


def _runner(
    *,
    router: Any,
    clock: _Clock,
    inbound_queue_size: int = 4,
    worker_count: int = 1,
    notifications_enabled: bool = False,
    owner_numbers: frozenset[str] = frozenset(),
) -> SmsForwarderWebhookRunner:
    """构造被测 runner（小容量队列——过载用例）。"""
    return SmsForwarderWebhookRunner(
        router=router,
        notifications_enabled=notifications_enabled,
        owner_numbers=owner_numbers,
        inbound_queue_size=inbound_queue_size,
        worker_count=worker_count,
        clock=clock,
    )


class TestQueueBackpressure:
    async def test_default_queue_capacity_matches_qq_style(self) -> None:
        """默认队列容量与 QQ / 企微同风格（64）。"""
        assert SMS_FORWARDER_INBOUND_QUEUE_SIZE == 64

    async def test_queue_overflow_drops_counts_and_logs(self) -> None:
        """队列满：丢弃 + 计数 + ERROR 含键 / 深度；不登记去重。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock, inbound_queue_size=2)
        assert runner.submit(_payload(msgid="1")) is True
        assert runner.submit(_payload(msgid="2")) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        try:
            assert runner.submit(_payload(msgid="3")) is False
        finally:
            logger.remove(handler_id)
        assert runner.dropped_inbound == 1
        assert runner.pending_inbound == 2
        assert any(
            "SmsForwarder 入站队列已满" in message
            and "msgid:3" in message
            and "depth=2" in message
            and "dropped=1" in message
            for message in captured
        )

    async def test_dropped_message_not_dedup_registered_so_repeat_can_be_consumed(self) -> None:
        """丢弃不登记去重：腾出队列后同 msgid 再次到达（偶发重复）仍可消费。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock(), inbound_queue_size=1)
        assert runner.submit(_payload(msgid="10")) is True
        assert runner.submit(_payload(msgid="11")) is False
        assert runner.dropped_inbound == 1
        runner._inbound_queue.get_nowait()
        assert runner.submit(_payload(msgid="11")) is True
        assert runner.pending_inbound == 1
        assert runner.dropped_inbound == 1

    async def test_duplicate_msgid_absorbed_by_dedup(self) -> None:
        """同 msgid 重投只入队一次（首见登记）。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        assert runner.submit(_payload(msgid="20")) is True
        assert runner.submit(_payload(msgid="20")) is True
        assert runner.pending_inbound == 1

    async def test_content_dedup_within_window_then_expires(self) -> None:
        """无 msgid：同发送者 + 同正文在窗口内只入队一次，窗口过期后重新接受。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock, inbound_queue_size=4)
        payload = {"from": "13800138000", "content": "重复短信", "kind": "SMS"}
        assert runner.submit(payload) is True
        assert runner.submit(payload) is True
        assert runner.pending_inbound == 1
        clock.advance_ms(SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS)
        assert runner.submit(payload) is True
        assert runner.pending_inbound == 2

    async def test_unroutable_event_not_dedup_registered(self) -> None:
        """不可路由事件不登记：随后同 msgid 的可路由消息照常入队。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        assert runner.submit(_payload(msgid="30", kind="notification")) is False
        assert runner.pending_inbound == 0
        assert runner.submit(_payload(msgid="30", kind="SMS")) is True
        assert runner.pending_inbound == 1

    async def test_notifications_enabled_accepts_notification(self) -> None:
        """通知开关开启：kind=notification 正常入队。"""
        runner = _runner(
            router=RecordingRouter(),
            clock=_Clock(),
            notifications_enabled=True,
        )
        assert runner.submit(_payload(msgid="31", kind="notification")) is True
        assert runner.pending_inbound == 1


class TestOwnerRouting:
    async def test_owner_number_marked_on_inbound(self) -> None:
        """属主清单命中时入站消息 is_owner=True（worker 消费面可见）。"""
        router = RecordingRouter()
        runner = _runner(
            router=router,
            clock=_Clock(),
            owner_numbers=frozenset({"13800138000"}),
        )
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_payload(msgid="40")) is True
            await wait_until(lambda: len(router.received) == 1)
            assert router.received[0].is_owner is True
            assert router.received[0].chat_id == "sms:13800138000"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)


class TestLifecycle:
    async def test_worker_consumes_and_stop_leaves_no_residue(self) -> None:
        """worker 正常消费；停机后队列清空且 submit 拒绝新事件。"""
        router = RecordingRouter()
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_payload(msgid="50")) is True
            await wait_until(lambda: len(router.received) == 1)
            assert runner.pending_inbound == 0
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)
        assert runner.pending_inbound == 0
        assert runner.stopping is True
        assert runner.submit(_payload(msgid="51")) is False

    async def test_worker_isolates_handler_exception_and_continues(self) -> None:
        """worker 记录首条异常后继续消费第二条，runner 不停机。"""
        router = _FailOnceRouter()
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_payload(msgid="70")) is True
            assert runner.submit(_payload(msgid="71")) is True
            await asyncio.wait_for(router.second_handled.wait(), timeout=_WAIT_TIMEOUT)
            assert router.attempted == ["70", "71"]
            assert router.handled == ["71"]
            assert any(
                "SmsForwarder 入站消息处理异常" in message and "message_id=70" in message
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
            assert runner.submit(_payload(msgid="60")) is True
            await wait_until(lambda: router.started.is_set())
            assert runner.submit(_payload(msgid="61")) is True
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
        assert runner.submit(_payload(msgid="80")) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            clock.advance_ms(SMS_FORWARDER_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert any(
            "SmsForwarder 入站队列观测" in message
            and "pending=1" in message
            and "dropped_delta=0" in message
            for message in captured
        )

    async def test_stats_silent_without_backlog_or_drop(self) -> None:
        """无积压且无丢弃时不输出（间隔到达也静默）。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            clock.advance_ms(SMS_FORWARDER_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert not any("SmsForwarder 入站队列观测" in message for message in captured)

    async def test_stats_throttled_before_interval(self) -> None:
        """未到间隔点即使有积压也不输出。"""
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            assert runner.submit(_payload(msgid="81")) is True
        finally:
            logger.remove(handler_id)
        assert not any("SmsForwarder 入站队列观测" in message for message in captured)
