"""wecom 通道 runner——长连接回调消费（去重 / 归一 / 路由）+ 出站投递适配。

协议依据：企业微信开发者中心《智能机器人长连接》
（https://developer.work.weixin.qq.com/document/path/101463）与《接收消息》
（https://developer.work.weixin.qq.com/document/path/100719）。本模块是通道层
与平台之间的编排面：入站经 :class:`~packages.harness.channels.wecom.wecom_client.WeComLongConnectionClient`
的回调帧接入，出站实现
:class:`~packages.harness.channels.channel_service.ChannelAdapterProtocol`。

入站链路（与 telegram / QQ 共用的路由器语义）::

    长连接回调帧（aibot_msg_callback）
        ↓ ① 事件帧分流（aibot_event_callback 不产出人类回合——事件类型记日志；
               disconnected_event 由客户端状态机消费）
        ↓ ② msgid 去重（官方文档指定的排重键；进程内有界 FIFO 窗口）
        ↓ ③ 回复上下文登记（req_id + 到达时刻——出站 aibot_respond_msg 的寻址原料）
        ↓ ④ map_wecom_callback_to_inbound（会话键 / 附件 / 群聊标记 / 合成来源）
        ↓ ⑤ 入站队列（有界）→ 固定 worker（解耦 LLM 耗时与收帧循环：
              心跳应答 / 后续回调不被一次长回复阻塞）
    ChannelRouter.handle_inbound（会话懒建 / 人格绑定 / ChatService.reply / 兜底）

出站链路（先文本后附件）::

    OutboundMessageModel
        ↓ ① chat_id grammar 解析（single:<userid> / group:<chatid> → chat_type）
        ↓ ② 文本分段（markdown content ≤20480 字节；换行边界优先）
        ↓ ③ 首段：回复上下文新鲜 → aibot_respond_msg（透传回调 req_id）；
        │    上下文缺失 / 过期 → aibot_send_msg（chatid + chat_type）
        │    续段：恒 aibot_send_msg（同一 req_id 多条回复语义官方未文档化）
        ↓ ④ 附件：解析字节（base64 直接 / url 经共享 HTTP 池下载 / path 本地读）
        │    → 分片上传取 media_id → aibot_send_msg（image / voice / file）
        ↓ ChannelDeliveryResultModel（message_ids 恒空——平台应答不含消息 ID；
              attachment_results 逐附件结局）

失败语义（与既有适配器同契约）：

    - **文本段失败上抛**（``CHANNEL_API_ERROR`` / ``CHANNEL_TOKEN_MISSING`` /
      ``VALIDATION_ERROR``）——路由器 ``handle_inbound`` 统一兜底隔离；
    - **单附件失败不上抛**：``AttachmentDeliveryResult(ok=False)`` 承载原因，
      文本与其余附件照常投递（语音超 2MB、格式平台不认、URL 下载失败等）；
    - 入站队列满 = 过载：丢弃并记 ERROR（平台侧重投由 msgid 去重兜底）；
    - 回调处理恒不击穿收帧循环（客户端面已隔离单帧异常）。

引用回复（文档化偏差）：``OutboundMessageModel.reply_to_message_id`` 不透传——
企微消息体无引用字段（引用是入站侧的 ``quote`` 结构），回复与原消息的关联由
``req_id`` 在协议层表达。
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from loguru import logger

from packages.harness.channels.channel_models import (
    AttachmentDeliveryResult,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.wecom.wecom_client import WeComLongConnectionClient
from packages.harness.channels.wecom.wecom_models import (
    WECOM_CMD_MSG_CALLBACK,
    WECOM_EVENT_DISCONNECTED,
    WECOM_GROUP_PREFIX,
    WECOM_SINGLE_PREFIX,
    WeComCallbackBodyModel,
    WeComCallbackFrameModel,
    WeComChatTargetModel,
    WeComMediaKind,
    WeComReplyContextModel,
    map_wecom_callback_to_inbound,
    parse_wecom_chat_id,
    split_wecom_markdown_text,
    wecom_media_kind_for_attachment,
)
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import run_blocking_ops

__all__ = [
    "WeComChannelRunner",
    "WeComMessageDedupWindow",
    "WECOM_DEDUP_WINDOW_SIZE",
    "WECOM_INBOUND_QUEUE_SIZE",
    "WECOM_INBOUND_WORKER_COUNT",
]

#: msgid 去重窗口容量（官方文档：msgid 用于事件排重；窗口超容按 FIFO 淘汰）。
WECOM_DEDUP_WINDOW_SIZE: Final[int] = 1024
#: 入站待处理队列容量（过载即丢弃并告警——平台重投由去重兜底）。
WECOM_INBOUND_QUEUE_SIZE: Final[int] = 64
#: 入站 worker 数（同聊天由路由器 per-chat 锁串行；多 worker 只提升跨聊天并行度）。
WECOM_INBOUND_WORKER_COUNT: Final[int] = 2
#: 附件上传缺省 MIME 子类型（media_type 缺线索时的文件名后缀）。
_UPLOAD_DEFAULT_SUBTYPE: Final[str] = "bin"


class WeComMessageDedupWindow:
    """msgid 去重窗口（进程内有界 FIFO；重启清零——与 QQ/webhook 同语义）。

    平台的回调重投（网络重试 / 断线重连窗口）由该窗口兜住；窗口之外的重投
    会重消费（重启后尤其如此——企微无等价 offset 游标）。
    """

    def __init__(self, capacity: int = WECOM_DEDUP_WINDOW_SIZE) -> None:
        """初始化去重窗口。

        Args:
            capacity: 窗口容量（≤0 视为 1）。
        """
        self._capacity = max(1, capacity)
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def remember(self, msgid: str) -> bool:
        """登记 msgid（首见 True / 重复 False；超容淘汰最旧）。

        Args:
            msgid: 回调唯一标志。

        Returns:
            首见返回 True；窗口内重复返回 False。
        """
        if msgid in self._seen:
            return False
        self._seen.add(msgid)
        self._order.append(msgid)
        while len(self._order) > self._capacity:
            self._seen.discard(self._order.popleft())
        return True


class WeComChannelRunner:
    """企微长连接通道 runner（回调消费 + 出站投递 + 生命周期）。

    一个对象承担三个面：

        - **入站面**：``run()`` 内注册回调处理器，把消息回调经去重 / 归一 /
          队列交给 ``ChannelRouter``；
        - **出站面**：``send_message`` 实现
          :class:`~packages.harness.channels.channel_service.ChannelAdapterProtocol`
          （文本分段 + 附件上传投递）；
        - **生命周期面**：``request_stop`` 优雅停机（长连接关闭 + worker 收尾）。
    """

    def __init__(
        self,
        *,
        client: WeComLongConnectionClient,
        router: ChannelRouter,
        reply_window_seconds: float = 86400.0,
        http_client: httpx.AsyncClient | None = None,
        dedup_capacity: int = WECOM_DEDUP_WINDOW_SIZE,
        inbound_queue_size: int = WECOM_INBOUND_QUEUE_SIZE,
        worker_count: int = WECOM_INBOUND_WORKER_COUNT,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化 runner（未启动——``run()`` 由装配方以后台任务驱动）。

        Args:
            client: 长连接客户端（共享同一对象承担发送面）。
            router: 通道路由器（入站消费面；本对象由装配方注册为其适配器）。
            reply_window_seconds: 回复上下文有效期秒数（官方文档：收到消息回调
                后 24 小时内可回复该会话——超期降级为主动推送）。
            http_client: 共享 HTTP 客户端（附件 ``url`` 源下载；None = 不支持
                url 源——逐附件失败承载）。
            dedup_capacity: msgid 去重窗口容量。
            inbound_queue_size: 入站队列容量（过载丢弃）。
            worker_count: 入站 worker 数（≤0 视为 1）。
            clock: 时钟（epoch 毫秒——回复窗口判定，测试注入固定值）。
        """
        self._client = client
        self._router = router
        self._reply_window_ms = int(reply_window_seconds * 1000)
        self._http = http_client
        self._dedup = WeComMessageDedupWindow(dedup_capacity)
        self._inbound_queue: asyncio.Queue[InboundMessageModel | None] = asyncio.Queue(
            maxsize=max(1, inbound_queue_size)
        )
        self._worker_count = max(1, worker_count)
        self._clock = clock
        self._reply_contexts: dict[str, WeComReplyContextModel] = {}
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._dropped_inbound = 0

    # ------------------------------------------------------------------
    # 观测面（装配 / 测试）
    # ------------------------------------------------------------------

    @property
    def client(self) -> WeComLongConnectionClient:
        """底层长连接客户端（装配观测面）。"""
        return self._client

    @property
    def dropped_inbound(self) -> int:
        """因队列过载丢弃的入站条数（观测面）。"""
        return self._dropped_inbound

    @property
    def pending_inbound(self) -> int:
        """队列中待处理的入站条数（观测面——过载判定的即时读数）。"""
        return self._inbound_queue.qsize()

    def reply_context(self, chat_id: str) -> WeComReplyContextModel | None:
        """读取会话的回复上下文（未登记 / 过期返回 None——观测面）。

        Args:
            chat_id: 会话键（``single:`` / ``group:`` grammar）。

        Returns:
            有效期内的回复上下文；否则 None。
        """
        context = self._reply_contexts.get(chat_id)
        if context is None:
            return None
        if self._clock() - context.ts_ms > self._reply_window_ms:
            return None
        return context

    # ------------------------------------------------------------------
    # 生命周期面
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求优雅停机（长连接 supervisor 与入站 worker 同步收尾）。"""
        self._stop_event.set()
        self._client.request_stop()

    @property
    def stop_requested(self) -> bool:
        """是否已请求停机。"""
        return self._stop_event.is_set()

    async def run(self) -> None:
        """启动入站消费与长连接 supervisor（长生命周期——装配方托管后台任务）。

        长连接 supervisor 正常退出（停机 / 被踢）后本方法收尾 worker 并返回；
        supervisor 以异常终止（订阅被拒等）时异常原样上抛，由任务终态日志承载。
        """
        self._client.set_callback_handler(self._handle_callback)
        for index in range(self._worker_count):
            self._workers.append(
                asyncio.create_task(
                    self._worker_loop(), name=f"wecom-inbound-worker-{index}"
                )
            )
        logger.info(
            "企微通道 runner 启动: workers={}, reply_window_ms={}",
            self._worker_count,
            self._reply_window_ms,
        )
        try:
            await self._client.run()
        finally:
            await self._shutdown_workers()
            logger.info(
                "企微通道 runner 已停止: dropped_inbound={}", self._dropped_inbound
            )

    async def _shutdown_workers(self) -> None:
        """停放入站 worker（哨兵优先；超时 cancel 兜底）。"""
        for _ in self._workers:
            try:
                self._inbound_queue.put_nowait(None)
            except asyncio.QueueFull:
                break
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    # ------------------------------------------------------------------
    # 入站面
    # ------------------------------------------------------------------

    async def _handle_callback(self, frame: WeComCallbackFrameModel) -> None:
        """回调帧处理器（立即返回——耗时回复在 worker 内执行）。

        Args:
            frame: 回调帧（消息 / 事件）。
        """
        if frame.cmd != WECOM_CMD_MSG_CALLBACK:
            self._log_event_frame(frame)
            return
        body = frame.body
        if body.msgid and not self._dedup.remember(body.msgid):
            logger.debug("企微回调重复投递（msgid 去重跳过）: {}", body.msgid)
            return
        chat_id = self._chat_id_for_body(body)
        if chat_id is not None and frame.headers.req_id:
            self._reply_contexts[chat_id] = WeComReplyContextModel(
                req_id=frame.headers.req_id, chat_id=chat_id, ts_ms=self._clock()
            )
        inbound = map_wecom_callback_to_inbound(frame, ts_ms=self._clock())
        if inbound is None:
            logger.debug(
                "企微回调不可路由（跳过）: msgid={}, msgtype={}, chattype={}",
                body.msgid,
                body.msgtype,
                body.chattype,
            )
            return
        try:
            self._inbound_queue.put_nowait(inbound)
        except asyncio.QueueFull:
            self._dropped_inbound += 1
            logger.error(
                "企微入站队列已满（丢弃该消息——平台重投由 msgid 去重兜底）: "
                "chat_id={}, msgid={}, dropped={}",
                inbound.chat_id,
                inbound.message_id,
                self._dropped_inbound,
            )

    @staticmethod
    def _log_event_frame(frame: WeComCallbackFrameModel) -> None:
        """记录事件帧（事件不产出人类回合——仅观测）。

        Args:
            frame: 事件回调帧。
        """
        event = frame.body.event
        eventtype = event.eventtype if event is not None else ""
        if eventtype == WECOM_EVENT_DISCONNECTED:
            return  # 客户端状态机已记录（被踢下线）
        logger.debug(
            "企微事件回调（不路由——仅观测）: eventtype={}, msgid={}",
            eventtype or "(空)",
            frame.body.msgid,
        )

    @staticmethod
    def _chat_id_for_body(body: WeComCallbackBodyModel) -> str | None:
        """按回调 body 推导会话键（与入站映射同一 grammar）。

        Args:
            body: 回调 body。

        Returns:
            会话键；会话类型 / 目标缺失时返回 None。
        """
        if body.chattype == WECOM_GROUP_PREFIX:
            return f"{WECOM_GROUP_PREFIX}:{body.chatid}" if body.chatid else None
        if body.chattype == WECOM_SINGLE_PREFIX:
            userid = body.sender.userid
            return f"{WECOM_SINGLE_PREFIX}:{userid}" if userid else None
        return None

    async def _worker_loop(self) -> None:
        """入站 worker（逐条交给路由器；路由器自身恒不抛——双重隔离）。"""
        while True:
            inbound = await self._inbound_queue.get()
            if inbound is None:
                return
            try:
                await self._router.handle_inbound(inbound)
            except Exception:  # 路由器契约恒不抛——防御性隔离仍在
                logger.opt(exception=True).error(
                    "企微入站消息处理异常（丢弃）: chat_id={}, msgid={}",
                    inbound.chat_id,
                    inbound.message_id,
                )

    # ------------------------------------------------------------------
    # 出站面（ChannelAdapterProtocol 实现）
    # ------------------------------------------------------------------

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站消息（文本分段 + 附件上传投递；先文本后附件）。

        Args:
            message: 出站消息（通道中立形态；``chat_id`` 须为 ``single:`` /
                ``group:`` grammar）。

        Returns:
            投递结果（``message_ids`` 恒空——企微应答不含消息 ID；
            ``attachment_results`` 按附件序对齐，部分成功可见）。

        Raises:
            BusinessException: chat_id grammar 非法（``VALIDATION_ERROR``）；
                **文本段**投递失败（``CHANNEL_API_ERROR`` /
                ``CHANNEL_TOKEN_MISSING``——路由器负责兜底隔离）。
        """
        target = parse_wecom_chat_id(message.chat_id)
        await self._deliver_text(message.chat_id, target, message.text)
        attachment_results: list[AttachmentDeliveryResult] = []
        for index, attachment in enumerate(message.attachments):
            outcome = await self._deliver_attachment(target, attachment, index)
            attachment_results.append(outcome)
        # 平台应答只有 errcode / errmsg——不返回消息 ID（文档化：message_ids 恒空）。
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECOM,
            chat_id=message.chat_id,
            message_ids=[],
            attachment_results=attachment_results,
        )

    async def _deliver_text(
        self, chat_id: str, target: WeComChatTargetModel, text: str
    ) -> None:
        """投递文本（分段；首段优先回复上下文，其余主动推送）。

        Args:
            chat_id: 会话键（回复上下文检索键）。
            target: 解析后的会话目标。
            text: 回复文本。

        Returns:
            无（投递成功即返回；平台应答不含消息 ID）。

        Raises:
            BusinessException: 任一分段投递失败（文本段失败按 v1 语义上抛）。
        """
        segments = split_wecom_markdown_text(text)
        if not segments:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="企微出站消息文本不能为空",
                details={"chat_id": chat_id},
            )
        context = self.reply_context(chat_id)
        for index, segment in enumerate(segments):
            if index == 0 and context is not None:
                await self._client.send_markdown_reply(
                    callback_req_id=context.req_id, content=segment
                )
            else:
                await self._client.send_markdown_push(target=target, content=segment)
        if len(segments) > 1:
            logger.info(
                "企微出站文本按平台上限分段投递: chat_id={}, segments={}",
                chat_id,
                len(segments),
            )

    async def _deliver_attachment(
        self,
        target: WeComChatTargetModel,
        attachment: ChannelMessageAttachment,
        index: int,
    ) -> AttachmentDeliveryResult:
        """上传并投递单个附件（失败不上抛——逐附件结果承载）。

        Args:
            target: 解析后的会话目标。
            attachment: 出站附件（base64 / url / path 三源恰其一）。
            index: 附件序（结果对齐用）。

        Returns:
            该附件投递结局（失败时 ``ok=False`` + 原因；成功时携带
            ``provider_message_id=None``——企微应答不含消息 ID）。
        """
        media_kind = wecom_media_kind_for_attachment(attachment)
        try:
            content = await self._attachment_bytes(attachment)
            media_id = await self._client.upload_media(
                content,
                media_kind=media_kind,
                filename=self._upload_filename(attachment, media_kind),
            )
            await self._client.send_media_push(
                target=target, media_kind=media_kind, media_id=media_id
            )
        except BusinessException as exc:
            logger.warning(
                "企微附件投递失败（逐附件结果承载）: kind={}, media_kind={}, error={}",
                attachment.kind.value,
                media_kind.value,
                exc.message,
            )
            return AttachmentDeliveryResult(index=index, ok=False, error=exc.message)
        except Exception as exc:  # 非业务异常（下载中断等）同样逐附件承载
            logger.opt(exception=True).warning(
                "企微附件投递异常（逐附件结果承载）: kind={}", attachment.kind.value
            )
            return AttachmentDeliveryResult(index=index, ok=False, error=str(exc))
        return AttachmentDeliveryResult(index=index, ok=True)

    async def _attachment_bytes(self, attachment: ChannelMessageAttachment) -> bytes:
        """解析附件内容字节（base64 直接 / url 下载 / path 本地读）。

        Args:
            attachment: 出站附件。

        Returns:
            明文文件字节。

        Raises:
            BusinessException: base64 非法（``VALIDATION_ERROR``）；url 源但未
                装配共享 HTTP 客户端 / 下载非 200 / 传输失败（``CHANNEL_API_ERROR``）；
                本地路径读取失败（``CHANNEL_API_ERROR``）。
        """
        if attachment.base64 is not None:
            try:
                return base64.b64decode("".join(attachment.base64.split()), validate=True)
            except ValueError as exc:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="企微附件 base64 载荷不可解码（合法 base64 形态 required）",
                ) from exc
        if attachment.url is not None:
            return await self._fetch_url(attachment.url)
        path = attachment.path
        if path is None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="企微附件缺少媒体源（url / base64 / path 恰其一）",
            )
        return await run_blocking_ops(lambda: _read_local_file(path))

    async def _fetch_url(self, url: str) -> bytes:
        """经共享 HTTP 客户端下载附件内容（无私有连接）。

        Args:
            url: 媒体 URL。

        Returns:
            下载字节。

        Raises:
            BusinessException: HTTP 客户端未装配 / 下载失败（``CHANNEL_API_ERROR``）。
        """
        if self._http is None:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="企微附件 url 源需要共享 HTTP 客户端（装配缺失）",
                details={"url": url},
            )
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"企微附件下载失败（传输异常）: {url}",
                details={"url": url, "error": str(exc)},
            ) from exc
        if response.status_code != 200:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"企微附件下载失败（HTTP {response.status_code}）: {url}",
                details={"url": url, "status_code": response.status_code},
            )
        return response.content

    @staticmethod
    def _upload_filename(attachment: ChannelMessageAttachment, media_kind: WeComMediaKind) -> str:
        """合成上传文件名（kind + MIME 子类型派生）。

        Args:
            attachment: 出站附件。
            media_kind: 上传素材类型。

        Returns:
            文件名（如 ``image.jpeg``；无 MIME 线索时 ``<kind>.bin``）。
        """
        subtype = (attachment.media_type or "").partition("/")[2]
        extension = subtype if subtype else _UPLOAD_DEFAULT_SUBTYPE
        return f"{media_kind.value}.{extension}"


def _read_local_file(path: str) -> bytes:
    """读取本地附件文件（阻塞 I/O——经共享线程池下沉调用）。

    Args:
        path: 本地文件路径。

    Returns:
        文件字节。

    Raises:
        BusinessException: 文件不存在 / 不可读（``CHANNEL_API_ERROR``）。
    """
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            message=f"企微附件本地路径读取失败: {path}",
            details={"path": path, "error": str(exc)},
        ) from exc

