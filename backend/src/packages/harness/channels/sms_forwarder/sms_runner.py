"""SmsForwarder 通道 runner——webhook 入站有界队列消费（去重 / 丢弃计数 / 生命周期）。

与 QQ / 企微 runner 同范式：webhook 端点只做鉴权 / 体校验 / 去重登记 /
入队，随后立即 200 确认；LLM 回合耗时由后台固定 worker 承担，手机端
转发请求的等待不再被一次长回复阻塞::

    POST /api/v1/channels/sms-forwarder
        ↓ token 校验（app/api/channels_sms_forwarder）
    SmsForwarderWebhookRunner.submit(payload)   ← 本模块（同步、恒不抛）
        ① parse_sms_forwarder_payload（不可路由事件不登记去重）
        ② 去重窗（msgid 常驻 FIFO；无 msgid 按内容 + 时间窗）
        ③ 入站队列（有界）put_nowait
            └─ 队列满 → 丢弃并计数（ERROR 日志含 msgid / 队列深度；
                端点恒 200（上报方仅非 200 重试）→ 满队丢弃即最终丢失；
                不登记去重仅吸收偶发重复）
    200 {"status": "ok"}
        ↓ 固定 worker（后台任务，跨聊天并行、同聊天由路由器 per-chat 锁串行）
    ChannelRouter.handle_inbound（会话懒建 / 人格绑定 / ChatService）

容量与 worker 数为**代码常量非 env**（与 QQ / 企微一致）：部署形态由通道
开关决定，过载丢弃是既定降级策略，不暴露成运营旋钮。关闭时取消 worker 并
清空队列（会话级资源零残留）。仓库没有可复用的通用有界 runner 基类——
QQ / 企微各自持有同范式实现（队列元素与解析面不同），本模块沿用同一范式
而不抽象共享基类，避免为一个消费者改公共面。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

from loguru import logger

from packages.harness.channels.channel_models import InboundMessageModel
from packages.harness.channels.sms_forwarder.sms_models import (
    SmsForwarderDedupWindow,
    SmsForwarderInboundRouterFace,
    parse_sms_forwarder_payload,
)
from packages.harness.core import time_utils

__all__ = [
    "SMS_FORWARDER_DEDUP_WINDOW_SIZE",
    "SMS_FORWARDER_INBOUND_QUEUE_SIZE",
    "SMS_FORWARDER_INBOUND_WORKER_COUNT",
    "SMS_FORWARDER_STATS_INTERVAL_MS",
    "SmsForwarderWebhookRunner",
]

#: 去重窗口容量（与直调路径同容量——同语义）。
SMS_FORWARDER_DEDUP_WINDOW_SIZE: Final[int] = 1024
#: 入站待处理队列容量（过载即丢弃并告警——端点恒 200，满队丢弃即最终丢失；
#: 不登记去重仅吸收偶发重复）。
SMS_FORWARDER_INBOUND_QUEUE_SIZE: Final[int] = 64
#: 入站 worker 数（同聊天由路由器 per-chat 锁串行；多 worker 只提升跨聊天并行度）。
SMS_FORWARDER_INBOUND_WORKER_COUNT: Final[int] = 2
#: 队列观测 INFO 间隔毫秒（仅深度 > 0 或丢弃有增量时输出）。
SMS_FORWARDER_STATS_INTERVAL_MS: Final[int] = 60_000


class SmsForwarderWebhookRunner:
    """SmsForwarder webhook 入站消费 runner（有界队列 + 固定 worker + 生命周期）。"""

    def __init__(
        self,
        *,
        router: SmsForwarderInboundRouterFace,
        notifications_enabled: bool,
        owner_numbers: frozenset[str],
        dedup_capacity: int = SMS_FORWARDER_DEDUP_WINDOW_SIZE,
        inbound_queue_size: int = SMS_FORWARDER_INBOUND_QUEUE_SIZE,
        worker_count: int = SMS_FORWARDER_INBOUND_WORKER_COUNT,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化 runner（未启动——``run()`` 由装配方以后台任务驱动）。

        Args:
            router: 通道路由器（入站消费面）。
            notifications_enabled: 是否接收 ``kind=notification`` 转发。
            owner_numbers: 属主号码 / 联系名归一集合（命中按 owner 落档）。
            dedup_capacity: 去重窗口容量。
            inbound_queue_size: 入站队列容量（≤0 视为 1）。
            worker_count: 入站 worker 数（≤0 视为 1）。
            clock: 时钟（epoch 毫秒——时间戳回落 / 去重窗口 / 观测节流）。
        """
        self._router = router
        self._notifications_enabled = notifications_enabled
        self._owner_numbers = owner_numbers
        self._dedup = SmsForwarderDedupWindow(dedup_capacity)
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
        """解析并尝试入队单条转发载荷（恒不抛、不阻塞）。

        Args:
            payload: SmsForwarder 转发载荷（HTTP 层已保证 JSON 对象）。

        Returns:
            True = 已入队（或重复投递被去重吸收）；False = 不可路由 /
            已停机 / 队列满丢弃。
        """
        if self._stop_event.is_set():
            logger.debug("SmsForwarder runner 已停机（确认丢弃）: pending={}", self.pending_inbound)
            return False
        parsed = parse_sms_forwarder_payload(
            payload,
            notifications_enabled=self._notifications_enabled,
            owner_numbers=self._owner_numbers,
            clock=self._clock,
        )
        if parsed is None:
            return False
        now_ms = self._clock()
        if self._dedup.contains(parsed.dedup_key, now_ms):
            logger.debug(
                "SmsForwarder 消息重复投递（去重跳过）: key={}", parsed.dedup_key
            )
            return True
        try:
            self._inbound_queue.put_nowait(parsed.inbound)
        except asyncio.QueueFull:
            self._dropped_inbound += 1
            logger.error(
                "SmsForwarder 入站队列已满（丢弃该消息——端点恒 200，满队丢弃即最终丢失；未登记去重仅吸收偶发重复）: "
                "key={}, chat_id={}, message_id={}, depth={}, dropped={}",
                parsed.dedup_key,
                parsed.inbound.chat_id,
                parsed.inbound.message_id,
                self._inbound_queue.qsize(),
                self._dropped_inbound,
            )
            self._maybe_log_stats()
            return False
        self._dedup.remember(parsed.dedup_key, now_ms, parsed.dedup_ttl_ms)
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
                    self._worker_loop(), name=f"sms-forwarder-inbound-worker-{index}"
                )
            )
        logger.info(
            "SmsForwarder 通道 runner 启动: workers={}, queue_size={}",
            self._worker_count,
            self._inbound_queue.maxsize,
        )
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_workers()
            logger.info(
                "SmsForwarder 通道 runner 已停止: dropped_inbound={}, pending={}",
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
                    "SmsForwarder 入站消息处理异常（丢弃）: chat_id={}, message_id={}",
                    inbound.chat_id,
                    inbound.message_id,
                )

    def _maybe_log_stats(self) -> None:
        """按注入时钟节流输出队列深度 / 丢弃增量（仅在有积压或丢弃时）。"""
        now = self._clock()
        if now - self._last_stats_ms < SMS_FORWARDER_STATS_INTERVAL_MS:
            return
        self._last_stats_ms = now
        dropped_delta = self._dropped_inbound - self._last_dropped_at_stats
        self._last_dropped_at_stats = self._dropped_inbound
        pending = self.pending_inbound
        if pending <= 0 and dropped_delta <= 0:
            return
        logger.info(
            "SmsForwarder 入站队列观测: pending={}, capacity={}, dropped_delta={}, "
            "dropped_total={}",
            pending,
            self._inbound_queue.maxsize,
            dropped_delta,
            self._dropped_inbound,
        )
