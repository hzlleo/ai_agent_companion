"""GUI 微信通道 runner——轮询采集 / 截图变化检测 / 视觉解析 / 归一 / 路由。

架构定位：GUI 微信没有官方个人号消息 API，通道以沙盒内「可截图、可注入
键鼠」的 Xorg 会话为协议底座。本 runner 只做编排（轮询节奏 / 变化检测 /
视觉解析 / 标题校验 / 去重 / 归一 / 路由 / 退避），页面读写经注入的
:class:`~packages.harness.channels.wechat_gui.wechat_gui_client.WechatGuiSandboxClient`
与 :class:`~packages.harness.channels.wechat_gui.wechat_gui_reader.WechatGuiReader`。

入站链路::

    轮询节拍（poll_interval_seconds——逐启用联系人）
        ↓ open_chat（目标切换时经沙盒搜索打开）
        ↓ screenshot（沙盒抓图）
        ↓ 截图 sha256 与上次相同 → 跳过（不调视觉模型——省调用、防重复解析）
        ↓ reader 视觉解析（严格 JSON；低置信度失败）
        ↓ 标题校验：解析标题须与当前 open_chat 目标一致
        │   不一致按 title_mismatch_action（默认 skip 并计数）
        ↓ 消息去重（备注名 + 文本 + 时间窗哈希；进程内有界 FIFO）
    ChannelRouter.handle_inbound（per-chat 锁 / 心流门控 / 人格绑定 /
        ChatService.reply / 兜底回发——与其余通道完全同一套语义）

失败语义：轮询失败（沙盒不可达 / 视觉链失败 / 结构非法）按指数退避重试
（``backoff_base`` × 2^(n-1)、``backoff_max`` 封顶）；逐联系人隔离——一个
联系人失败不阻断同轮其余联系人；``handle_inbound`` 自身恒不抛，runner 侧
仅防万一的兜底日志。

边界（文档化）：单窗口语义——输入框属于当前打开的会话；多联系人轮询是
串行切换，不并行。群聊按单会话处理（``is_group_chat=False``），群内多人
身份保留在消息发送者字段。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from loguru import logger

from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.wechat_gui.wechat_gui_client import WechatGuiSandboxClient
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WECHAT_GUI_DEDUP_WINDOW_SIZE,
    TitleMismatchAction,
    WechatGuiContactModel,
    WechatGuiSnapshotModel,
    map_wechat_gui_snapshot_to_inbound,
)
from packages.harness.channels.wechat_gui.wechat_gui_reader import WechatGuiReader
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WECHAT_GUI_DEDUP_WINDOW_SIZE",
    "WechatGuiChannelRunner",
    "WechatGuiMessageDedupWindow",
]


class WechatGuiMessageDedupWindow:
    """消息去重窗口（进程内有界 FIFO——兜住截图重复与重启窗口重投）。

    去重键由「备注名 + 文本 + 时间窗」派生（GUI 侧没有平台消息 ID）；窗口
    超容按 FIFO 淘汰最旧键，重启后窗口清零（文档化边界：同一画面在重启后
    会重消费一次）。
    """

    def __init__(self, capacity: int = WECHAT_GUI_DEDUP_WINDOW_SIZE) -> None:
        """初始化去重窗口。

        Args:
            capacity: 窗口容量（≤0 视为 1）。
        """
        self._capacity = max(1, capacity)
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def seen(self, message_id: str) -> bool:
        """判断去重键是否已处理。

        Args:
            message_id: 去重键。

        Returns:
            True = 已处理（窗口内）。
        """
        return message_id in self._seen

    def remember(self, message_id: str) -> None:
        """登记已处理去重键（超容淘汰最旧）。

        Args:
            message_id: 去重键。
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
        """窗口内的已处理键（归一过滤输入）。"""
        return frozenset(self._seen)

    @property
    def size(self) -> int:
        """窗口当前条目数（观测面）。"""
        return len(self._order)


class WechatGuiChannelRunner:
    """GUI 微信通道 runner（轮询采集 + 路由——单实例不变式）。

    单实例不变式：``run`` 以在途任务句柄幂等（重复调用直接返回）——同一
    沙盒画面只有一个轮询循环，避免同一窗口被并发读写。
    """

    def __init__(
        self,
        *,
        client: WechatGuiSandboxClient,
        reader: WechatGuiReader,
        router: ChannelRouter,
        contacts: Mapping[str, WechatGuiContactModel],
        poll_interval_seconds: float = 5.0,
        backoff_base_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        max_messages_per_poll: int = 20,
        title_mismatch_action: TitleMismatchAction = TitleMismatchAction.SKIP,
        dedup_capacity: int = WECHAT_GUI_DEDUP_WINDOW_SIZE,
        clock: Callable[[], int] = time_utils.now_ms,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """初始化 runner。

        Args:
            client: 沙盒客户端（注入——测试用 fake）。
            reader: 截图解析器（注入——测试用 fake）。
            router: 通道路由器（入站消息消费面）。
            contacts: 备注名 → 联系人配置（仅 ``enabled=True`` 的目标被轮询）。
            poll_interval_seconds: 轮询间隔秒数（> 0）。
            backoff_base_seconds: 轮询失败退避基数秒。
            backoff_max_seconds: 退避封顶秒数（≥ 基数）。
            max_messages_per_poll: 单次归一消息上限（1-100）。
            title_mismatch_action: 标题与目标不一致时的处置方式。
            dedup_capacity: 去重窗口容量。
            clock: 时钟（epoch 毫秒——采集时刻与观测面）。
            sleep: 休眠注入（None = 停机可打断的默认实现；测试注入记录式
                fake 以便零真实等待）。
        """
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须 > 0")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds 不得小于 backoff_base_seconds")
        if not 1 <= max_messages_per_poll <= 100:
            raise ValueError("max_messages_per_poll 必须在 1-100 之间")
        self._client = client
        self._reader = reader
        self._router = router
        self._contacts = {
            key: contact for key, contact in contacts.items() if contact.enabled
        }
        self._poll_interval_seconds = poll_interval_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._max_messages_per_poll = max_messages_per_poll
        self._title_mismatch_action = title_mismatch_action
        self._dedup = WechatGuiMessageDedupWindow(dedup_capacity)
        self._clock = clock
        self._sleep_override = sleep
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._task: asyncio.Task[None] | None = None
        self._current_target: str | None = None
        self._last_screenshot_sha: dict[str, str] = {}
        self._consecutive_failures = 0
        self._title_mismatch_count = 0
        self._vlm_skipped_count = 0
        self._poll_count = 0

    # ------------------------------------------------------------------
    # 观测面
    # ------------------------------------------------------------------

    @property
    def dedup_size(self) -> int:
        """去重窗口条目数（观测面）。"""
        return self._dedup.size

    @property
    def consecutive_failures(self) -> int:
        """当前连续轮询失败次数（观测面——成功采集即复位）。"""
        return self._consecutive_failures

    @property
    def title_mismatch_count(self) -> int:
        """标题与目标备注名不一致的累计次数（观测面）。"""
        return self._title_mismatch_count

    @property
    def vlm_skipped_count(self) -> int:
        """因截图未变化而跳过视觉解析的累计次数（观测面）。"""
        return self._vlm_skipped_count

    @property
    def poll_count(self) -> int:
        """实际调用视觉解析的轮次（观测面）。"""
        return self._poll_count

    @property
    def targets(self) -> tuple[str, ...]:
        """启用的轮询目标（观测面——配置顺序）。"""
        return tuple(self._contacts)

    def request_stop(self) -> None:
        """请求优雅停机（置位后轮询与退避等待即时收口）。"""
        self._stop_requested = True
        self._stop_event.set()

    # ------------------------------------------------------------------
    # 轮询循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """轮询循环（单实例不变式；停机请求 / 任务取消时正常返回）。

        每轮逐启用联系人采集；单联系人异常不阻断同轮其余联系人，但整轮有
        失败时按指数退避重试（``request_stop`` 可即时打断退避）。
        ``asyncio.CancelledError`` 原样上抛（停机 cancel 路径不被吞）。
        """
        current = asyncio.current_task()
        if self._task is not None and not self._task.done():
            logger.debug("GUI 微信通道 runner 已在运行（重复 run 幂等返回）")
            return
        self._task = current
        logger.info(
            "GUI 微信通道 runner 启动: targets={}, poll_interval_s={}",
            self.targets,
            self._poll_interval_seconds,
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
                        "GUI 微信通道轮询失败（退避后重试）: failures={}, delay_s={}",
                        self._consecutive_failures,
                        delay,
                    )
                    await self._pause(delay)
                    continue
                self._consecutive_failures = 0
                await self._pause(self._poll_interval_seconds)
        finally:
            logger.info(
                "GUI 微信通道 runner 已停机: dedup_size={}, title_mismatch={}, vlm_skipped={}",
                self._dedup.size,
                self._title_mismatch_count,
                self._vlm_skipped_count,
            )
            self._task = None

    async def _poll_once(self) -> None:
        """执行一轮采集（逐启用联系人；失败隔离但整轮失败触发退避）。"""
        if not self._contacts:
            logger.debug("GUI 微信通道无启用联系人——本轮跳过采集")
            return
        round_failures: list[Exception] = []
        for remark_name, contact in self._contacts.items():
            try:
                await self._poll_target(remark_name, contact)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单联系人失败不阻断同轮其余联系人
                round_failures.append(exc)
                logger.opt(exception=True).warning(
                    "GUI 微信联系人轮询失败（继续同轮其余联系人）: remark_name={!r}",
                    remark_name,
                )
        if round_failures:
            raise round_failures[0]

    async def _poll_target(self, remark_name: str, contact: WechatGuiContactModel) -> None:
        """采集单个联系人（变化检测 / 解析 / 标题校验 / 归一 / 路由）。

        Args:
            remark_name: 联系人备注名（会话目标）。
            contact: 联系人配置。

        Raises:
            BusinessException: 沙盒工具 / 视觉解析失败（可重试面）；标题
                不一致且处置方式为 ``fail``。
        """
        if self._current_target != remark_name:
            await self._client.open_chat(remark_name)
            self._current_target = remark_name
        screenshot = await self._client.screenshot()
        if self._last_screenshot_sha.get(remark_name) == screenshot.sha256:
            self._vlm_skipped_count += 1
            logger.debug(
                "GUI 微信截图未变化，跳过视觉解析: remark_name={!r}", remark_name
            )
            return
        self._last_screenshot_sha[remark_name] = screenshot.sha256
        self._poll_count += 1
        read = await self._reader.read(screenshot.image_base64)
        snapshot = WechatGuiSnapshotModel(
            title=read.title,
            messages=read.messages,
            screenshot_sha256=screenshot.sha256,
            captured_at_ms=self._clock(),
        )
        if snapshot.title.strip() != remark_name:
            self._title_mismatch_count += 1
            if self._title_mismatch_action is TitleMismatchAction.FAIL:
                raise BusinessException(
                    code=ErrorCode.CHANNEL_API_ERROR,
                    message=(
                        f"GUI 微信标题校验失败：目标 {remark_name!r}，"
                        f"解析标题 {snapshot.title!r}"
                    ),
                )
            logger.warning(
                "GUI 微信标题校验不一致（按 skip 处置并计数）: 目标={!r}, 解析标题={!r}, "
                "累计={}",
                remark_name,
                snapshot.title,
                self._title_mismatch_count,
            )
            return
        inbound = map_wechat_gui_snapshot_to_inbound(
            snapshot,
            contact=contact,
            known_message_ids=self._dedup.ids,
            max_messages=self._max_messages_per_poll,
            now_ms=snapshot.captured_at_ms,
        )
        if not inbound:
            return
        logger.debug(
            "GUI 微信采集到新消息: chat_id={}, count={}",
            inbound[0].chat_id,
            len(inbound),
        )
        for message in inbound:
            try:
                await self._router.handle_inbound(message)
            except Exception:  # 路由器恒不抛——此处只防未来回归击穿轮询
                logger.opt(exception=True).error(
                    "GUI 微信消息路由异常（继续后续消息）: message_id={}",
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


#: 通道类型常量（装配面观测 / 文档引用——避免各处手写字符串）。
WECHAT_GUI_CHANNEL: Final[ChannelType] = ChannelType.WECHAT_GUI
