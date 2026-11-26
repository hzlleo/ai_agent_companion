"""WhatsApp runner 测试——有界队列 / wamid 去重 / 丢弃计数 / 服务窗 / 生命周期。

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
from packages.harness.channels.whatsapp.whatsapp_models import WhatsAppServiceWindow
from packages.harness.channels.whatsapp.whatsapp_runner import (
    WHATSAPP_INBOUND_QUEUE_SIZE,
    WHATSAPP_INBOUND_WORKER_COUNT,
    WHATSAPP_STATS_INTERVAL_MS,
    WhatsAppWebhookRunner,
)
from tests.async_wait import wait_until

_WAIT_TIMEOUT: float = 5.0


class _Clock:
    """可推进的测试时钟（epoch 毫秒——观测节流 / 时间戳输入）。"""

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
            channel=ChannelType.WHATSAPP,
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
            channel=ChannelType.WHATSAPP,
            chat_id=message.chat_id,
            message_ids=[],
        )


def _message_entry(
    *,
    wamid: str,
    text: str = "你好",
    sender: str = "15551234567",
    timestamp: str = "1731000000",
) -> dict[str, Any]:
    """构造单条 messages[] 条目（测试直供形态）。"""
    return {
        "from": sender,
        "id": wamid,
        "timestamp": timestamp,
        "type": "text",
        "text": {"body": text},
    }


def _payload(
    *,
    wamid: str = "wamid.TEST-1",
    text: str = "你好",
    sender: str = "15551234567",
    timestamp: str = "1731000000",
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造单消息 webhook 载荷（测试直供形态）。"""
    value: dict[str, Any] = {
        "metadata": {"phone_number_id": "123456"},
        "messages": [
            _message_entry(
                wamid=wamid, text=text, sender=sender, timestamp=timestamp
            )
        ],
    }
    if statuses is not None:
        value["statuses"] = statuses
    return {"entry": [{"changes": [{"field": "messages", "value": value}]}]}


def _runner(
    *,
    router: Any,
    clock: _Clock,
    service_window: WhatsAppServiceWindow | None = None,
    inbound_queue_size: int = 4,
    worker_count: int = 1,
    owner_numbers: frozenset[str] = frozenset(),
) -> WhatsAppWebhookRunner:
    """构造被测 runner（小容量队列——过载用例）。"""
    return WhatsAppWebhookRunner(
        router=router,
        service_window=service_window or WhatsAppServiceWindow(),
        owner_numbers=owner_numbers,
        inbound_queue_size=inbound_queue_size,
        worker_count=worker_count,
        clock=clock,
    )


class TestQueueBackpressure:
    async def test_default_queue_and_worker_constants(self) -> None:
        """默认队列容量 64 / worker 2（与 QQ / 企微 / SmsForwarder 同风格）。"""
        assert WHATSAPP_INBOUND_QUEUE_SIZE == 64
        assert WHATSAPP_INBOUND_WORKER_COUNT == 2

    async def test_queue_overflow_drops_counts_and_logs(self) -> None:
        """队列满：丢弃 + 计数 + ERROR 含 wamid / 深度；不登记去重。"""
        runner = _runner(
            router=RecordingRouter(), clock=_Clock(), inbound_queue_size=2
        )
        assert runner.submit(_payload(wamid="wamid.1")) is True
        assert runner.submit(_payload(wamid="wamid.2")) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        try:
            assert runner.submit(_payload(wamid="wamid.3")) is False
        finally:
            logger.remove(handler_id)
        assert runner.dropped_inbound == 1
        assert runner.pending_inbound == 2
        assert any(
            "WhatsApp 入站队列已满" in message
            and "wamid.3" in message
            and "depth=2" in message
            and "dropped=1" in message
            for message in captured
        )

    async def test_batch_overflow_counts_every_remaining_message(self) -> None:
        """批内满队：首条入队，其余每条分别计数 + ERROR（各自 wamid / 累计）。"""
        runner = _runner(
            router=RecordingRouter(), clock=_Clock(), inbound_queue_size=1
        )
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messages": [
                                    _message_entry(wamid="wamid.B1"),
                                    _message_entry(wamid="wamid.B2"),
                                    _message_entry(wamid="wamid.B3"),
                                ]
                            },
                        }
                    ]
                }
            ]
        }
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        try:
            assert runner.submit(payload) is False
        finally:
            logger.remove(handler_id)
        assert runner.pending_inbound == 1
        assert runner.dropped_inbound == 2
        assert any(
            "WhatsApp 入站队列已满" in message
            and "wamid.B2" in message
            and "depth=1" in message
            and "dropped=1" in message
            for message in captured
        )
        assert any(
            "WhatsApp 入站队列已满" in message
            and "wamid.B3" in message
            and "depth=1" in message
            and "dropped=2" in message
            for message in captured
        )

    async def test_dropped_message_leaves_dedup_unregistered(self) -> None:
        """丢弃不登记去重：同 wamid 再次到达时不被去重窗拦截。"""
        runner = _runner(
            router=RecordingRouter(), clock=_Clock(), inbound_queue_size=1
        )
        assert runner.submit(_payload(wamid="wamid.10")) is True
        assert runner.submit(_payload(wamid="wamid.11")) is False
        assert runner.dropped_inbound == 1
        runner._inbound_queue.get_nowait()
        assert runner.submit(_payload(wamid="wamid.11")) is True
        assert runner.pending_inbound == 1
        assert runner.dropped_inbound == 1

    async def test_duplicate_wamid_absorbed_by_dedup(self) -> None:
        """同 wamid 重复推送只入队一次（首见登记）。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        assert runner.submit(_payload(wamid="wamid.20")) is True
        assert runner.submit(_payload(wamid="wamid.20")) is True
        assert runner.pending_inbound == 1

    async def test_status_events_counted_without_enqueue(self) -> None:
        """状态回执：只累计 status_events_ignored，不入队。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock())
        status_only = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "statuses": [
                                    {"id": "wamid.A", "status": "delivered"},
                                    {"id": "wamid.B", "status": "read"},
                                ]
                            },
                        }
                    ]
                }
            ]
        }
        assert runner.submit(status_only) is False
        assert runner.pending_inbound == 0
        assert runner.status_events_ignored == 2

    async def test_multi_message_payload_enqueues_all(self) -> None:
        """单次推送含多条消息：全部入队（各自 wamid 独立）。"""
        runner = _runner(router=RecordingRouter(), clock=_Clock(), inbound_queue_size=4)
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messages": [
                                    _message_entry(wamid="wamid.A"),
                                    _message_entry(wamid="wamid.B"),
                                ]
                            },
                        }
                    ]
                }
            ]
        }
        assert runner.submit(payload) is True
        assert runner.pending_inbound == 2


class TestServiceWindowRecord:
    async def test_submit_records_last_inbound_for_outbound_window(self) -> None:
        """入队成功后记录会话最近入站毫秒（出站服务窗判定原料）。"""
        window = WhatsAppServiceWindow()
        clock = _Clock()
        runner = _runner(router=RecordingRouter(), clock=clock, service_window=window)
        assert runner.submit(_payload(timestamp="1731000000")) is True
        assert runner.service_window is window
        assert window.last_inbound_ms("15551234567") == 1_731_000_000_000


class TestOwnerRouting:
    async def test_owner_number_marked_on_inbound(self) -> None:
        """属主清单命中时入站消息 is_owner=True（worker 消费面可见）。"""
        router = RecordingRouter()
        runner = _runner(
            router=router,
            clock=_Clock(),
            owner_numbers=frozenset({"15551234567"}),
        )
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_payload(wamid="wamid.40")) is True
            await wait_until(lambda: len(router.received) == 1)
            assert router.received[0].is_owner is True
            assert router.received[0].chat_id == "wa:15551234567"
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
            assert runner.submit(_payload(wamid="wamid.50")) is True
            await wait_until(lambda: len(router.received) == 1)
            assert runner.pending_inbound == 0
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT)
        assert runner.pending_inbound == 0
        assert runner.stopping is True
        assert runner.submit(_payload(wamid="wamid.51")) is False

    async def test_worker_isolates_handler_exception_and_continues(self) -> None:
        """worker 记录首条异常后继续消费第二条，runner 不停机。"""
        router = _FailOnceRouter()
        runner = _runner(router=router, clock=_Clock(), inbound_queue_size=4)
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="ERROR")
        task = asyncio.create_task(runner.run())
        try:
            assert runner.submit(_payload(wamid="wamid.70")) is True
            assert runner.submit(_payload(wamid="wamid.71")) is True
            await asyncio.wait_for(router.second_handled.wait(), timeout=_WAIT_TIMEOUT)
            assert router.attempted == ["wamid.70", "wamid.71"]
            assert router.handled == ["wamid.71"]
            assert any(
                "WhatsApp 入站消息处理异常" in message and "wamid.70" in message
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
            assert runner.submit(_payload(wamid="wamid.60")) is True
            await wait_until(lambda: router.started.is_set())
            assert runner.submit(_payload(wamid="wamid.61")) is True
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
        assert runner.submit(_payload(wamid="wamid.80")) is True
        captured: list[str] = []
        handler_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
        try:
            clock.advance_ms(WHATSAPP_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert any(
            "WhatsApp 入站队列观测" in message
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
            clock.advance_ms(WHATSAPP_STATS_INTERVAL_MS)
            runner._maybe_log_stats()
        finally:
            logger.remove(handler_id)
        assert not any("WhatsApp 入站队列观测" in message for message in captured)
