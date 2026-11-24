"""WhatsApp 通道 runner——webhook 入站有界队列消费（去重 / 丢弃计数 / 服务窗）。

与 QQ / 企微 / SmsForwarder runner 同范式：webhook 端点只做 hub 校验 /
签名校验 / 体校验 / 去重登记 / 入队，随后立即 200 确认；LLM 回合耗时由
后台固定 worker 承担，Meta 的 webhook 等待不再被一次长回复阻塞::

    POST /api/v1/channels/whatsapp
        ↓ hub 校验（GET）/ HMAC 签名 + 体校验（POST——app/api/channels_whatsapp）
    WhatsAppWebhookRunner.submit(payload)   ← 本模块（同步、恒不抛）
        ① parse_whatsapp_webhook_payload（不可路由 / 状态回执不登记去重）
        ② wamid 去重窗（进程内有界 FIFO 1024）
        ③ 服务窗记录（每会话最近入站毫秒——出站 24h 判定原料）
        ④ 入站队列（有界）put_nowait
            └─ 队列满 → 丢弃并计数（ERROR 日志含 wamid / 队列深度 / 累计；
                不登记去重——Meta 对非 2xx 重投可重新消费）
    200 {"status": "ok"}
        ↓ 固定 worker（后台任务，跨聊天并行、同聊天由路由器 per-chat 锁串行）
    ChannelRouter.handle_inbound（会话懒建 / 人格绑定 / ChatService）

容量与 worker 数为**代码常量非 env**（与 QQ / 企微 / SmsForwarder 一致）：
部署形态由通道开关决定，过载丢弃是既定降级策略，不暴露成运营旋钮。关闭时
取消 worker 并清空队列（会话级资源零残留）。服务窗状态随 runner 实例存活
（进程内、单实例限制——文档化于 channels.md 与 WhatsAppServiceWindow）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

from loguru import logger

from packages.harness.channels.channel_models import InboundMessageModel
from packages.harness.channels.whatsapp.whatsapp_models import (
    WhatsAppDedupWindow,
    WhatsAppInboundRouterFace,
    WhatsAppServiceWindow,
    parse_whatsapp_webhook_payload,
)
from packages.harness.core import time_utils

__all__ = [
    "WHATSAPP_INBOUND_QUEUE_SIZE",
    "WHATSAPP_INBOUND_WORKER_COUNT",
    "WHATSAPP_STATS_INTERVAL_MS",
    "WhatsAppWebhookRunner",
]

#: 入站待处理队列容量（过载即丢弃并告警——丢弃不登记去重，Meta 重投可重新消费）。
WHATSAPP_INBOUND_QUEUE_SIZE: Final[int] = 64
#: 入站 worker 数（同聊天由路由器 per-chat 锁串行；多 worker 只提升跨聊天并行度）。
WHATSAPP_INBOUND_WORKER_COUNT: Final[int] = 2
#: 队列观测 INFO 间隔毫秒（仅深度 > 0 或丢弃有增量时输出）。
WHATSAPP_STATS_INTERVAL_MS: Final[int] = 60_000


class WhatsAppWebhookRunner:
    """WhatsApp webhook 入站消费 runner（有界队列 + 固定 worker + 生命周期）。"""

    def __init__(
        self,
        *,
        router: WhatsAppInboundRouterFace,
        service_window: WhatsAppServiceWindow,
        owner_numbers: frozenset[str],
        dedup_capacity: int = 1024,
        inbound_queue_size: int = WHATSAPP_INBOUND_QUEUE_SIZE,
        worker_count: int = WHATSAPP_INBOUND_WORKER_COUNT,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化 runner（未启动——``run()`` 由装配方以后台任务驱动）。

        Args:
            router: 通道路由器（入站消费面）。
            service_window: 24 小时服务窗状态（与出站适配器共享同一实例）。
            owner_numbers: 属主号码归一集合（命中按 owner 落档）。
            dedup_capacity: wamid 去重窗口容量。
            inbound_queue_size: 入站队列容量（≤0 视为 1）。
            worker_count: 入站 worker 数（≤0 视为 1）。
            clock: 时钟（epoch 毫秒——时间戳回落 / 观测节流）。
        """
        self._router = router
        self._service_window = service_window
        self._owner_numbers = owner_numbers
        self._dedup = WhatsAppDedupWindow(dedup_capacity)
        self._inbound_queue: asyncio.Queue[InboundMessageModel] = asyncio.Queue(
            maxsize=max(1, inbound_queue_size)
        )
        self._worker_count = max(1, worker_count)
        self._clock = clock
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._dropped_inbound = 0
        self._status_events_ignored = 0
        self._last_stats_ms = self._clock()
        self._last_dropped_at_stats = 0

    @property
    def service_window(self) -> WhatsAppServiceWindow:
        """24 小时服务窗状态（与出站适配器共享——观测 / 测试面）。"""
        return self._service_window

    @property
    def dropped_inbound(self) -> int:
        """因队列过载丢弃的入站条数（观测面）。"""
        return self._dropped_inbound

    @property
    def status_events_ignored(self) -> int:
        """累计忽略的状态回执条数（观测面——statuses[] 不进路由）。"""
        return self._status_events_ignored

    @property
    def pending_inbound(self) -> int:
        """队列中待处理的入站条数（观测面）。"""
        return self._inbound_queue.qsize()

    @property
    def stopping(self) -> bool:
        """是否已请求停机（停机后 submit 拒绝新事件）。"""
        return self._stop_event.is_set()

    def submit(self, payload: dict[str, Any]) -> bool:
        """解析并尝试入队一次 webhook 推送（恒不抛、不阻塞）。

        Args:
            payload: WhatsApp webhook 推送载荷（HTTP 层已保证 JSON 对象）。

        Returns:
            True = 全部可路由消息已入队（或重复投递被去重吸收）；
            False = 无可路由消息 / 已停机 / 存在队列满丢弃。
        """
        if self._stop_event.is_set():
            logger.debug("WhatsApp runner 已停机（确认丢弃）: pending={}", self.pending_inbound)
            return False
        batch = parse_whatsapp_webhook_payload(
            payload,
            owner_numbers=self._owner_numbers,
            clock=self._clock,
        )
        if batch.status_events_ignored:
            self._status_events_ignored += batch.status_events_ignored
            logger.debug(
                "WhatsApp 状态回执忽略（仅观测）: count={}, total={}",
                batch.status_events_ignored,
                self._status_events_ignored,
            )
        if not batch.messages:
            return False
        accepted_any = False
        for parsed in batch.messages:
            if self._stop_event.is_set():
                logger.debug(
                    "WhatsApp runner 已停机（批量剩余消息确认丢弃）: pending={}",
                    self.pending_inbound,
                )
                return False
            if self._dedup.contains(parsed.dedup_key):
                logger.debug(
                    "WhatsApp 消息重复投递（去重跳过）: key={}", parsed.dedup_key
                )
                accepted_any = True
                continue
            try:
                self._inbound_queue.put_nowait(parsed.inbound)
            except asyncio.QueueFull:
                self._dropped_inbound += 1
                logger.error(
                    "WhatsApp 入站队列已满（丢弃该消息——未登记去重，Meta 重投可重新消费）: "
                    "wamid={}, chat_id={}, depth={}, dropped={}",
                    parsed.inbound.message_id,
                    parsed.inbound.chat_id,
                    self._inbound_queue.qsize(),
                    self._dropped_inbound,
                )
                self._maybe_log_stats()
                return False
            self._dedup.remember(parsed.dedup_key)
            self._service_window.record_inbound(
                parsed.inbound.sender_id, parsed.inbound.ts_ms
            )
            accepted_any = True
        self._maybe_log_stats()
        return accepted_any

    def request_stop(self) -> None:
        """请求停机（worker 在 ``run()`` 收尾路径取消）。"""
        self._stop_event.set()

    async def run(self) -> None:
        """启动入站 worker 并等待停机请求（由装配方托管为后台任务）。"""
        for index in range(self._worker_count):
            self._workers.append(
                asyncio.create_task(
                    self._worker_loop(), name=f"whatsapp-inbound-worker-{index}"
                )
            )
        logger.info(
            "WhatsApp 通道 runner 启动: workers={}, queue_size={}",
            self._worker_count,
            self._inbound_queue.maxsize,
        )
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_workers()
            logger.info(
                "WhatsApp 通道 runner 已停止: dropped_inbound={}, pending={}, "
                "status_events_ignored={}",
                self._dropped_inbound,
                self.pending_inbound,
                self._status_events_ignored,
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
                    "WhatsApp 入站消息处理异常（丢弃）: chat_id={}, message_id={}",
                    inbound.chat_id,
                    inbound.message_id,
                )

    def _maybe_log_stats(self) -> None:
        """按注入时钟节流输出队列深度 / 丢弃增量（仅在有积压或丢弃时）。"""
        now = self._clock()
        if now - self._last_stats_ms < WHATSAPP_STATS_INTERVAL_MS:
            return
        self._last_stats_ms = now
        dropped_delta = self._dropped_inbound - self._last_dropped_at_stats
        self._last_dropped_at_stats = self._dropped_inbound
        pending = self.pending_inbound
        if pending <= 0 and dropped_delta <= 0:
            return
        logger.info(
            "WhatsApp 入站队列观测: pending={}, capacity={}, dropped_delta={}, "
            "dropped_total={}",
            pending,
            self._inbound_queue.maxsize,
            dropped_delta,
            self._dropped_inbound,
        )
