"""qq 通道 runner——webhook 入站有界队列消费（去重 / 丢弃计数 / 生命周期）。

与企微 runner 同范式（``channels/wecom/wecom_runner.py``）：webhook 端点
只做鉴权 / 体校验 / 去重登记 / 入队，随后立即 200 确认；LLM 回合耗时由
后台固定 worker 承担，NapCat 的上报等待不再被一次长回复阻塞::

    POST /api/v1/channels/qq/webhook
        ↓ token 校验（app/api/channels_qq）
    QqWebhookRunner.submit(payload)          ← 本模块（同步、恒不抛）
        ① parse_webhook_event（不可路由事件不登记去重）
        ② message_id 去重窗（进程内有界 FIFO 1024）
        ③ 入站队列（有界）put_nowait
            └─ 队列满 → 丢弃并计数（ERROR 日志含 msgid / 队列深度；
               不登记去重——平台重投可重新消费）
    200 {"status": "ok"}
        ↓ 固定 worker（后台任务，跨聊天并行、同聊天由路由器 per-chat 锁串行）
    ChannelRouter.handle_inbound（会话懒建 / 人格绑定 / 心流门控 / ChatService）

容量与 worker 数为**代码常量非 env**（与企微一致）：部署形态由通道开关
决定，过载丢弃是既定降级策略，不暴露成运营旋钮。关闭时取消 worker 并
清空队列（会话级资源零残留）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final, Protocol

from loguru import logger

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    InboundMessageModel,
)
from packages.harness.channels.qq.qq_webhook import (
    QqMessageDedupWindow,
    parse_webhook_event,
)
from packages.harness.core import time_utils

__all__ = [
    "QqInboundRouterFace",
    "QqWebhookRunner",
    "QQ_DEDUP_WINDOW_SIZE",
    "QQ_INBOUND_QUEUE_SIZE",
    "QQ_INBOUND_WORKER_COUNT",
    "QQ_STATS_INTERVAL_MS",
]

#: message_id 去重窗口容量（与单事件直调路径同容量——同语义）。
QQ_DEDUP_WINDOW_SIZE: Final[int] = 1024
#: 入站待处理队列容量（过载即丢弃并告警——丢弃不登记去重，平台重投可重新消费）。
QQ_INBOUND_QUEUE_SIZE: Final[int] = 64
#: 入站 worker 数（同聊天由路由器 per-chat 锁串行；多 worker 只提升跨聊天并行度）。
QQ_INBOUND_WORKER_COUNT: Final[int] = 2
#: 队列观测 INFO 间隔毫秒（仅深度 > 0 或丢弃有增量时输出）。
QQ_STATS_INTERVAL_MS: Final[int] = 60_000


class QqInboundRouterFace(Protocol):
    """runner 消费的入站路由面（生产 ChannelRouter；测试 fake 同形态）。"""

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """处理一条入站消息（调用方承担异常隔离）。

        Args:
            message: 通道中立入站消息。

        Returns:
            投递结果（runner 不消费，仅保持路由器契约）。
        """
        ...


class QqWebhookRunner:
    """QQ webhook 入站消费 runner（有界队列 + 固定 worker + 生命周期）。"""

    def __init__(
        self,
        *,
        router: QqInboundRouterFace,
        dedup_capacity: int = QQ_DEDUP_WINDOW_SIZE,
        inbound_queue_size: int = QQ_INBOUND_QUEUE_SIZE,
        worker_count: int = QQ_INBOUND_WORKER_COUNT,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化 runner（未启动——``run()`` 由装配方以后台任务驱动）。

        Args:
            router: 通道路由器（入站消费面）。
            dedup_capacity: message_id 去重窗口容量。
            inbound_queue_size: 入站队列容量（≤0 视为 1）。
            worker_count: 入站 worker 数（≤0 视为 1）。
            clock: 时钟（epoch 毫秒——周期观测节流，测试注入固定值）。
        """
        self._router = router
        self._dedup = QqMessageDedupWindow(dedup_capacity)
        self._inbound_queue: asyncio.Queue[InboundMessageModel] = asyncio.Queue(
            maxsize=max(1, inbound_queue_size)
        )
        self._worker_count = max(1, worker_count)
        self._clock = clock
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._dropped_inbound = 0
        self._last_stats_ms = self._clock()
        self._last_dropped_at_stats = 0

    @property
    def dropped_inbound(self) -> int:
        """因队列过载丢弃的入站条数（观测面）。"""
        return self._dropped_inbound

    @property
    def pending_inbound(self) -> int:
        """队列中待处理的入站条数（观测面）。"""
        return self._inbound_queue.qsize()

    @property
    def stopping(self) -> bool:
        """是否已请求停机（停机后 submit 拒绝新事件）。"""
        return self._stop_event.is_set()

    def submit(self, payload: dict[str, Any]) -> bool:
        """解析并尝试入队单条 webhook 事件（恒不抛、不阻塞）。

        Args:
            payload: OneBot v11 事件上报载荷（HTTP 层已保证 JSON 对象）。

        Returns:
            True = 已入队（或重复投递被去重吸收）；False = 不可路由 /
            已停机 / 队列满丢弃。
        """
        if self._stop_event.is_set():
            logger.debug("QQ runner 已停机（确认丢弃）: pending={}", self.pending_inbound)
            return False
        inbound = parse_webhook_event(payload)
        if inbound is None:
            return False
        message_id = int(inbound.message_id)
        if self._dedup.contains(message_id):
            logger.debug("QQ webhook 消息重复投递（去重跳过）: message_id={}", message_id)
            return True
        try:
            self._inbound_queue.put_nowait(inbound)
        except asyncio.QueueFull:
            self._dropped_inbound += 1
            logger.error(
                "QQ 入站队列已满（丢弃该消息——未登记去重，平台重投可重新消费）: "
                "message_id={}, chat_id={}, depth={}, dropped={}",
                message_id,
                inbound.chat_id,
                self._inbound_queue.qsize(),
                self._dropped_inbound,
            )
            self._maybe_log_stats()
            return False
        self._dedup.remember(message_id)
        self._maybe_log_stats()
        return True

    def request_stop(self) -> None:
        """请求停机（worker 在 ``run()`` 收尾路径取消）。"""
        self._stop_event.set()

    async def run(self) -> None:
        """启动入站 worker 并等待停机请求（由装配方托管为后台任务）。"""
        for index in range(self._worker_count):
            self._workers.append(
                asyncio.create_task(
                    self._worker_loop(), name=f"qq-inbound-worker-{index}"
                )
            )
        logger.info(
            "QQ 通道 runner 启动: workers={}, queue_size={}",
            self._worker_count,
            self._inbound_queue.maxsize,
        )
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_workers()
            logger.info(
                "QQ 通道 runner 已停止: dropped_inbound={}, pending={}",
                self._dropped_inbound,
                self.pending_inbound,
            )

    async def _shutdown_workers(self) -> None:
        """取消 worker、清空待处理队列（会话级资源零残留）。"""
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while True:
            try:
                self._inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _worker_loop(self) -> None:
        """入站 worker（逐条交给路由器；路由器自身恒不抛——双重隔离）。"""
        while True:
            inbound = await self._inbound_queue.get()
            try:
                await self._router.handle_inbound(inbound)
            except Exception:
                logger.opt(exception=True).error(
                    "QQ 入站消息处理异常（丢弃）: chat_id={}, message_id={}",
                    inbound.chat_id,
                    inbound.message_id,
                )

    def _maybe_log_stats(self) -> None:
        """按注入时钟节流输出队列深度 / 丢弃增量（仅在有积压或丢弃时）。"""
        now = self._clock()
        if now - self._last_stats_ms < QQ_STATS_INTERVAL_MS:
            return
        self._last_stats_ms = now
        dropped_delta = self._dropped_inbound - self._last_dropped_at_stats
        self._last_dropped_at_stats = self._dropped_inbound
        pending = self.pending_inbound
        if pending <= 0 and dropped_delta <= 0:
            return
        logger.info(
            "QQ 入站队列观测: pending={}, capacity={}, dropped_delta={}, dropped_total={}",
            pending,
            self._inbound_queue.maxsize,
            dropped_delta,
            self._dropped_inbound,
        )
