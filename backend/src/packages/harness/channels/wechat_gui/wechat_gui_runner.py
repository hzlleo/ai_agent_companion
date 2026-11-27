"""GUI 微信通道 runner——轮询采集 / 截图变化检测 / 视觉解析 / 归一 / 路由。

架构定位：GUI 微信没有官方个人号消息 API，通道以沙盒内「可截图、可注入
键鼠」的 Xorg 会话为协议底座。本 runner 只做编排（轮询节奏 / 变化检测 /
视觉解析 / 标题校验 / 会话水位 / 归一 / 路由 / 退避），页面读写经注入的
:class:`~packages.harness.channels.wechat_gui.wechat_gui_client.WechatGuiSandboxClient`
与 :class:`~packages.harness.channels.wechat_gui.wechat_gui_reader.WechatGuiReader`。

入站链路::

    轮询节拍（poll_interval_seconds——逐启用联系人）
        ↓ 单窗口互斥锁（与出站适配器共享；仅页面读写段持锁）
        ↓ open_chat（当前窗口目标不符时经沙盒搜索打开）
        ↓ screenshot（沙盒抓图）
        ↓ 截图 sha256 与上次已接受画面相同 → 跳过（不调视觉模型）
        ↓ reader 视觉解析（严格 JSON；低置信度失败）
        ↓ 标题校验：解析标题须与当前 open_chat 目标一致
        │   不一致按 title_mismatch_action（默认 skip 并计数）；目标复位
        ↓ 会话水位（可见消息序列尾部重叠 → 本轮新增消息；首帧只建基线）
    ［释放单窗口锁］
    ChannelRouter.handle_inbound（per-chat 锁 / 心流门控 / 人格绑定 /
        ChatService.reply / 兜底回发——与其余通道完全同一套语义）
        ↓ 投递尝试全部结束之后按当前帧落水位基线（失败不回退）

失败语义：轮询失败（沙盒不可达 / 视觉链失败 / 结构非法）按指数退避重试
（``backoff_base`` × 2^(n-1)、``backoff_max`` 封顶）；逐联系人隔离——一个
联系人失败不阻断同轮其余联系人；``handle_inbound`` 自身恒不抛，runner 侧
仅防万一的兜底日志。

单窗口互斥：截图、视觉解析与出站「校验 → 输入 → 回车」共享一把
``asyncio.Lock``（装配处与出站适配器同实例注入），避免轮询切走会话后
出站文本落进别人的窗口；``handle_inbound`` 在锁外执行（路由器可能回调
出站适配器，持锁会自锁）。

边界（文档化）：单窗口语义——输入框属于当前打开的会话；多联系人轮询是
串行切换，不并行。群聊按单会话处理（``is_group_chat=False``），群内多人
身份保留在消息发送者字段。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping

from loguru import logger

from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.wechat_gui.wechat_gui_client import WechatGuiSandboxClient
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    TitleMismatchAction,
    WechatGuiContactModel,
    WechatGuiSnapshotModel,
    WechatGuiWindowState,
    map_wechat_gui_messages_to_inbound,
)
from packages.harness.channels.wechat_gui.wechat_gui_reader import WechatGuiReader
from packages.harness.channels.wechat_gui.wechat_gui_watermark import (
    WechatGuiWatermark,
    WechatGuiWatermarkAdvance,
)
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WechatGuiChannelRunner",
]


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
        clock: Callable[[], int] = time_utils.now_ms,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        gui_lock: asyncio.Lock | None = None,
        window_state: WechatGuiWindowState | None = None,
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
            max_messages_per_poll: 单次归一消息上限（1-100）；同时作为会话
                水位基线保留长度。
            title_mismatch_action: 标题与目标不一致时的处置方式。
            clock: 时钟（epoch 毫秒——采集时刻与观测面）。
            sleep: 休眠注入（None = 停机可打断的默认实现；测试注入记录式
                fake 以便零真实等待）。
            gui_lock: 单窗口互斥锁（None = 自建；装配处与出站适配器共享）。
            window_state: 单窗口共享目标状态（None = 自建；装配处与出站
                适配器共享同一实例）。
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
        self._watermark = WechatGuiWatermark(retention=max_messages_per_poll)
        self._clock = clock
        self._sleep_override = sleep
        self._gui_lock = gui_lock if gui_lock is not None else asyncio.Lock()
        self._window_state = (
            window_state if window_state is not None else WechatGuiWindowState()
        )
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._task: asyncio.Task[None] | None = None
        self._last_screenshot_sha: dict[str, str] = {}
        self._consecutive_failures = 0
        self._title_mismatch_count = 0
        self._vlm_skipped_count = 0
        self._delivery_failure_count = 0
        self._poll_count = 0

    # ------------------------------------------------------------------
    # 观测面
    # ------------------------------------------------------------------

    @property
    def watermark_targets(self) -> tuple[str, ...]:
        """已建立会话水位的目标（观测面）。"""
        return self._watermark.tracked_targets

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
    def delivery_failure_count(self) -> int:
        """单条投递失败次数（观测面——水位照常前进，不重投）。"""
        return self._delivery_failure_count

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
                "GUI 微信通道 runner 已停机: watermark_targets={}, title_mismatch={}, "
                "vlm_skipped={}, delivery_failures={}",
                len(self._watermark.tracked_targets),
                self._title_mismatch_count,
                self._vlm_skipped_count,
                self._delivery_failure_count,
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
        """采集单个联系人并在锁外投递（页面读写段持单窗口锁）。

        Args:
            remark_name: 联系人备注名（会话目标）。
            contact: 联系人配置。

        Raises:
            BusinessException: 沙盒工具 / 视觉解析失败（可重试面）；标题
                不一致且处置方式为 ``fail``。
        """
        collected = await self._collect_target(remark_name)
        if collected is None:
            return
        advance, captured_at_ms = collected
        inbound = map_wechat_gui_messages_to_inbound(
            advance.new_messages,
            contact=contact,
            max_messages=self._max_messages_per_poll,
            now_ms=captured_at_ms,
        )
        if inbound:
            logger.debug(
                "GUI 微信采集到新消息: chat_id={}, count={}",
                inbound[0].chat_id,
                len(inbound),
            )
        try:
            for message in inbound:
                try:
                    await self._router.handle_inbound(message)
                except Exception:  # 路由器恒不抛——此处只防未来回归击穿轮询
                    self._delivery_failure_count += 1
                    logger.opt(exception=True).error(
                        "GUI 微信消息路由异常（继续后续消息）: message_id={}",
                        message.message_id,
                    )
                    continue
        finally:
            # 本轮全部投递尝试之后再落基线：投递失败不回退水位（逐消息隔离
            # 口径——失败消息不重投，避免把整屏旧消息再次倾泻）。
            self._watermark.commit(advance)

    async def _collect_target(
        self, remark_name: str
    ) -> tuple[WechatGuiWatermarkAdvance, int] | None:
        """单窗口锁内完成 打开会话 / 截图 / 解析 / 标题校验 / 水位比对。

        Args:
            remark_name: 联系人备注名（会话目标）。

        Returns:
            水位比对结果与采集时刻；截图未变化或标题不一致时返回 None。

        Raises:
            BusinessException: 沙盒工具 / 视觉解析失败；标题不一致且处置
                方式为 ``fail``（目标已复位，下一轮重新打开）。
        """
        async with self._gui_lock:
            if not self._window_state.matches(remark_name):
                self._window_state.mark_unknown()
                await self._client.open_chat(remark_name)
                self._window_state.mark_open(remark_name)
            screenshot = await self._client.screenshot()
            if self._last_screenshot_sha.get(remark_name) == screenshot.sha256:
                self._vlm_skipped_count += 1
                logger.debug(
                    "GUI 微信截图未变化，跳过视觉解析: remark_name={!r}", remark_name
                )
                return None
            self._poll_count += 1
            read = await self._reader.read(screenshot.image_base64)
            captured_at_ms = self._clock()
            snapshot = WechatGuiSnapshotModel(
                title=read.title,
                messages=read.messages,
                screenshot_sha256=screenshot.sha256,
                captured_at_ms=captured_at_ms,
            )
            if snapshot.title.strip() != remark_name:
                self._title_mismatch_count += 1
                # 未接受的画面不登记 sha：下一轮必须重新解析同一画面。
                self._last_screenshot_sha.pop(remark_name, None)
                self._window_state.mark_unknown()
                if self._title_mismatch_action is TitleMismatchAction.FAIL:
                    raise BusinessException(
                        code=ErrorCode.CHANNEL_API_ERROR,
                        message=(
                            f"GUI 微信标题校验失败：目标 {remark_name!r}，"
                            f"解析标题 {snapshot.title!r}"
                        ),
                    )
                logger.warning(
                    "GUI 微信标题校验不一致（按 skip 处置并计数——目标已复位）: "
                    "目标={!r}, 解析标题={!r}, 累计={}",
                    remark_name,
                    snapshot.title,
                    self._title_mismatch_count,
                )
                return None
            # 仅在解析成功且标题校验通过之后登记截图：瞬时失败不吞画面。
            self._last_screenshot_sha[remark_name] = screenshot.sha256
            advance = self._watermark.diff(remark_name, snapshot.messages)
            return advance, captured_at_ms

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
