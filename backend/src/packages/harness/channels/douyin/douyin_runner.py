"""douyin 通道 runner——会话轮询（采集 / 去重 / 归一 / 路由）+ 出站投递适配。

架构定位（同底座约束）：抖音 web 没有可用的官方个人消息 API，通道以浏览器
sidecar 的已登录页面为**协议底座**——本 runner 只做编排（轮询节奏 / 去重 /
归一 / 路由 / 退避），页面读写经 :class:`~packages.harness.channels.douyin.douyin_client.DouyinWebClient`
（注入的浏览器调用面）。因此换浏览器实现（playwright 变体 / 远程宿主机）只需
替换注入面，不触碰通道语义。

入站链路（与 telegram / QQ / 企微共用路由器语义）::

    轮询节拍（poll_interval_seconds）
        ↓ 会话页就绪（配置了 DOUYIN_CHAT_URL 时经 navigate 打开一次）
        ↓ 采集快照（evaluate → DouyinChatSnapshotModel；上限 max_messages_per_poll）
        ↓ map_douyin_snapshot_to_inbound（自身回声剔除 / 系统消息 synthetic /
        │   图片附件 / 已处理标识过滤 / 时间序截断）
        ↓ 去重窗口登记（message_id 有界 FIFO 1024——同 QQ / 企微语义）
    ChannelRouter.handle_inbound（per-chat 锁 / 心流门控 / 人格绑定 /
        ChatService.reply / 兜底回发——其余通道完全同一套语义）

出站链路::

    OutboundMessageModel
        ↓ chat_id grammar 解析（im:<conversation_id>）
        ↓ evaluate 发送脚本（写入会话输入框并触发发送）
        ↓ ChannelDeliveryResultModel（message_ids 恒空——DOM 侧没有平台
             回执 ID；附件逐条 ok=False 承载「底座只支持文本」）

失败语义（与既有通道同契约）：

    - 轮询失败（导航 / 求值 / 结构非法）按指数退避重试（``backoff_base`` ×
      2^(n-1)、``backoff_max`` 封顶）——sidecar 晚于后端就绪、页面停留在
      登录页等部署时序问题不杀通道；
    - ``ChannelRouter.handle_inbound`` 自身恒不抛（逐消息隔离），runner 侧
      仅防万一的兜底日志；
    - 出站文本失败上抛（``CHANNEL_API_ERROR`` / ``VALIDATION_ERROR``）由路由器
      兜底隔离；附件不支持不上抛（逐附件结果承载）。

文档化边界：出站依赖页面停留在入站会话（web 输入框是单会话 UI）；多会话
并行收发属后续交付面；`silent` 在抖音 web 无免通知语义——优雅忽略。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final

from loguru import logger

from packages.harness.channels.channel_models import (
    AttachmentDeliveryResult,
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.douyin.douyin_client import DouyinWebClient
from packages.harness.channels.douyin.douyin_models import (
    DouyinNormalizeParamsModel,
    build_douyin_chat_id,
    map_douyin_snapshot_to_inbound,
    parse_douyin_chat_id,
)
from packages.harness.core import time_utils

__all__ = [
    "DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR",
    "DOUYIN_DEDUP_WINDOW_SIZE",
    "DouyinChannelRunner",
    "DouyinMessageDedupWindow",
]

#: message_id 去重窗口容量（进程内有界 FIFO——重启清零，与 QQ / 企微同语义）。
DOUYIN_DEDUP_WINDOW_SIZE: Final[int] = 1024

#: 附件不支持的错误文案（底座只支持文本出站——逐附件结果承载）。
DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR: Final[str] = (
    "抖音 web 底座当前只支持文本出站（附件投递属后续交付面）"
)


class DouyinMessageDedupWindow:
    """消息去重窗口（进程内有界 FIFO——兜住重复采集 / 重启窗口重投）。

    抖音 web 的采集是**页面快照重复读取**：同一条消息在每次轮询都会再次出现，
    因此去重是通道正确性的硬前提（而非可选的防重投优化）。窗口超容按 FIFO
    淘汰最旧标识；重启后窗口清零，页面仍停留在旧消息时会重消费一次（与
    QQ / 企微的重启窗口语义一致，文档化）。
    """

    def __init__(self, capacity: int = DOUYIN_DEDUP_WINDOW_SIZE) -> None:
        """初始化去重窗口。

        Args:
            capacity: 窗口容量（≤0 视为 1）。
        """
        self._capacity = max(1, capacity)
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def seen(self, message_id: str) -> bool:
        """判断消息标识是否已处理。

        Args:
            message_id: 平台消息标识。

        Returns:
            True = 已处理（窗口内）。
        """
        return message_id in self._seen

    def remember(self, message_id: str) -> None:
        """登记已处理消息标识（超容淘汰最旧）。

        Args:
            message_id: 平台消息标识。
        """
        if message_id in self._seen:
            return
        self._seen.add(message_id)
        self._order.append(message_id)
        while len(self._order) > self._capacity:
            evicted = self._order.popleft()
            self._seen.discard(evicted)

    @property
    def ids(self) -> frozenset[str]:
        """窗口内的已处理标识（采集净化的过滤输入）。"""
        return frozenset(self._seen)

    @property
    def size(self) -> int:
        """窗口当前条目数（观测面）。"""
        return len(self._order)


class DouyinChannelRunner:
    """抖音 web 通道 runner（轮询采集 + 出站发送——实现通道适配器协议）。

    单实例不变式：``run`` 以在途任务句柄幂等（重复调用直接返回）——同一
   浏览器 profile 只有一个轮询循环，避免同一页面被并发读写。
    """

    def __init__(
        self,
        *,
        client: DouyinWebClient,
        router: ChannelRouter,
        conversation_id: str = "",
        poll_interval_seconds: float = 5.0,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        max_messages_per_poll: int = 20,
        dedup_capacity: int = DOUYIN_DEDUP_WINDOW_SIZE,
        clock: Callable[[], int] = time_utils.now_ms,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """初始化 runner。

        Args:
            client: 抖音 web 客户端（浏览器调用面注入——测试用 fake）。
            router: 通道路由器（入站消息消费面）。
            conversation_id: 目标会话标识（空串 = 由页面 URL 推断 / 兜底
                ``default``）。
            poll_interval_seconds: 轮询间隔秒数（> 0——采集节奏是出站请求量
                的唯一闸）。
            backoff_base_seconds: 轮询失败退避基数秒。
            backoff_max_seconds: 退避封顶秒数（≥ 基数）。
            max_messages_per_poll: 单次采集消息上限（1-100）。
            dedup_capacity: 去重窗口容量。
            clock: 时钟（epoch 毫秒——缺失消息时间的兜底与观测面）。
            sleep: 休眠注入（None = 停机可打断的默认实现；测试注入记录式
                fake 以便零真实等待）。
        """
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须 > 0")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds 不得小于 backoff_base_seconds")
        self._client = client
        self._router = router
        self._conversation_id = conversation_id.strip()
        self._poll_interval_seconds = poll_interval_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._max_messages_per_poll = max_messages_per_poll
        self._dedup = DouyinMessageDedupWindow(dedup_capacity)
        self._clock = clock
        self._sleep_override = sleep
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._task: asyncio.Task[None] | None = None
        self._chat_opened = False
        self._consecutive_failures = 0

    @property
    def dedup_size(self) -> int:
        """去重窗口条目数（观测面）。"""
        return self._dedup.size

    @property
    def consecutive_failures(self) -> int:
        """当前连续轮询失败次数（观测面——成功采集即复位）。"""
        return self._consecutive_failures

    def request_stop(self) -> None:
        """请求优雅停机（置位后轮询与退避等待即时收口）。"""
        self._stop_requested = True
        self._stop_event.set()

    async def run(self) -> None:
        """轮询循环（单实例不变式；停机请求 / 任务取消时正常返回）。

        每轮：确保会话页就绪 → 采集快照 → 归一 → 逐条路由 → 去重登记 →
        按轮询间隔休眠。任一环节异常按指数退避重试（``request_stop`` 可即时
        打断退避）；``asyncio.CancelledError`` 原样上抛（停机 cancel 路径不被
        吞）。
        """
        current = asyncio.current_task()
        if self._task is not None and not self._task.done():
            logger.debug("抖音通道 runner 已在运行（重复 run 幂等返回）")
            return
        self._task = current
        logger.info(
            "抖音 web 通道 runner 启动: conversation_id={!r}, poll_interval_s={}, "
            "max_messages_per_poll={}",
            self._conversation_id,
            self._poll_interval_seconds,
            self._max_messages_per_poll,
        )
        try:
            while not self._stop_requested:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._consecutive_failures += 1
                    delay = self._backoff_delay(self._consecutive_failures)
                    logger.opt(exception=True).warning(
                        "抖音通道轮询失败（退避后重试）: failures={}, delay_s={}",
                        self._consecutive_failures,
                        delay,
                    )
                    await self._pause(delay)
                    continue
                self._consecutive_failures = 0
                await self._pause(self._poll_interval_seconds)
        finally:
            logger.info("抖音 web 通道 runner 已停机: dedup_size={}", self._dedup.size)
            self._task = None

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站消息（文本经页面发送；附件逐条标记不支持）。

        Args:
            message: 通道中立出站消息。

        Returns:
            投递结果（``message_ids`` 恒空——DOM 侧无平台回执 ID；
            ``attachment_results`` 逐附件标记不支持）。

        Raises:
            BusinessException: chat_id grammar 非法（``VALIDATION_ERROR``）/
                文本发送失败（``CHANNEL_API_ERROR``）——路由器兜底隔离。
        """
        conversation_id = parse_douyin_chat_id(message.chat_id)
        await self._client.send_text(text=message.text)
        logger.debug(
            "抖音通道出站文本已提交: conversation_id={!r}, chars={}",
            conversation_id,
            len(message.text),
        )
        attachment_results = [
            AttachmentDeliveryResult(
                index=index,
                ok=False,
                error=DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR,
            )
            for index, _attachment in enumerate(message.attachments)
        ]
        return ChannelDeliveryResultModel(
            channel=ChannelType.DOUYIN,
            chat_id=message.chat_id,
            message_ids=[],
            attachment_results=attachment_results,
        )

    async def _poll_once(self) -> None:
        """执行一轮采集与路由（会话页就绪 → 快照 → 归一 → 路由 → 登记）。"""
        if not self._chat_opened:
            await self._client.open_chat()
            self._chat_opened = True
        snapshot = await self._client.fetch_snapshot(
            conversation_id=self._conversation_id,
            limit=self._max_messages_per_poll,
        )
        chat_id = build_douyin_chat_id(snapshot.conversation_id or self._conversation_id)
        inbound = map_douyin_snapshot_to_inbound(
            snapshot,
            params=DouyinNormalizeParamsModel(
                chat_id=chat_id,
                known_message_ids=self._dedup.ids,
                max_messages=self._max_messages_per_poll,
                now_ms=self._clock(),
            ),
        )
        if not inbound:
            return
        logger.debug("抖音通道采集到新消息: chat_id={}, count={}", chat_id, len(inbound))
        for message in inbound:
            try:
                await self._router.handle_inbound(message)
            except Exception:  # 路由器恒不抛——此处只防未来回归击穿轮询
                logger.opt(exception=True).error(
                    "抖音通道消息路由异常（继续后续消息）: message_id={}",
                    message.message_id,
                )
                continue
            self._dedup.remember(message.message_id)

    def _backoff_delay(self, failures: int) -> float:
        """计算退避秒数（指数增长 + 封顶）。

        Args:
            failures: 连续失败次数（1 起）。

        Returns:
            退避秒数（``backoff_base × 2^(failures-1)``，不超过封顶）。
        """
        exponent = max(0, failures - 1)
        return min(self._backoff_max_seconds, self._backoff_base_seconds * (2.0**exponent))

    async def _pause(self, seconds: float) -> None:
        """休眠（停机可打断——测试注入记录式 fake 实现零真实等待）。

        Args:
            seconds: 休眠秒数（≤0 直接返回）。
        """
        if seconds <= 0 or self._stop_requested:
            return
        if self._sleep_override is not None:
            await self._sleep_override(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
