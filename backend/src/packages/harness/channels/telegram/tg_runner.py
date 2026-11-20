"""telegram 通道 runner——getUpdates 长轮询循环 + 优雅停机 + 退避 + offset 持久化。

生命周期形态（app lifespan 装配，见 app/main.py + docs/backend/channels.md）::

    TelegramChannelRunner(client, router, ...)
        ↓ asyncio.create_task(runner.run())        app lifespan 启动
        ↓ background_task_manager.register(task)   managed 后台任务
    run():
        ① getMe 启动校验——token 无效（CHANNEL_TOKEN_MISSING）即抛出
           fail fast（任务终止 + 错误日志；其余失败仅告警进退避循环）；
        ② offset 恢复——state_dir 下 JSON 状态文件（重启不重消费）；
        ③ 长轮询循环——getUpdates(offset) → 逐 update 去重 + 映射
           InboundMessageModel → router.handle_inbound（恒不抛——单消息
           错误隔离在路由器层完成）→ 批末推进 offset 并持久化；
        ④ 失败退避——连续失败按指数退避（基数可配、封顶可配、sleep
           可注入）；成功即复位；
        ⑤ 优雅停机——``request_stop()`` 置位 asyncio.Event，轮询间检查
           退出（在途长轮询最多再等一个窗口；app 停机路径 bounded wait
           超时后 cancel 兜底）。

入站 v2 映射（协议 v2——wire 就绪，消费者 R2d/R2e 到场）：

    - 文本与媒体互斥（Bot API 语义）：媒体消息以 **caption 兜底为文本**
      （用户随媒体说的话）；纯媒体无 caption 归一为 ``text=""`` 入站
      （附件照常搭载——路由器跳过生成但保留表情收录）；纯文本消息 v1
      形态零变更；
    - 媒体字段（photo / voice / audio / document / sticker）提取为
      ``ChannelMessageAttachment``——**下载免取**（download-free v2）：
      附件以 ``path="tg-file://{file_id}"`` 伪方案承载平台文件引用
      （真实媒体拉取需 getFile + 文件 URL 二段流程——deferred 到 R2d
      贴纸 / 语音工作）；caption 挂首附件（供未来消费者渲染附件 +
      说明）；photo 取最大尺寸（宽 × 高）；voice / audio / document
      的 MIME 进 ``media_type``（photo / sticker 无 MIME 线索 → None）；
    - 提取顺序 photo → voice → audio → document → sticker（真实 Bot API
      消息至多携带一种媒体——固定顺序仅为确定性兜底）。

出站 v2 投递（先文本后附件——逐附件聚合，不抛）：

    - 文本段：v1 调用形态零变更（``silent=True`` 时增传
      ``disable_notification``——False 缺省不传参，v1 调用形状保持）；
    - 附件逐个投递（kind → Bot API 方法映射见 ``_tg_media_method_for``）：
      url 源 → JSON 引用形态；base64 源 → 解码字节 multipart 上传；
      path 源仅接受 ``tg-file://`` 伪方案（file_id 重发——剥离方案
      前缀透传）；**单附件失败不上抛**（``AttachmentDeliveryResult``
      ok=False + error 承载——文本与其余附件照常投递）；
    - 附件投递不携带引用回复与 caption 于 sticker（sendSticker 无
      caption 参数——平台差异文档化于 channel_models）。

offset 持久化（文档化决策）：``state_dir/telegram_offset.json`` 单键
``{"offset": N}`` 文件；写经「临时文件 + fsync + ``os.replace`` 原子换名」
（registry_admin 同模式——崩溃窗口内至多旧值或新值，无半写态）。与
registry_admin 的差异：**不做 .bak 备份轮换**——offset 是单键可重建
状态（丢失 = 重消费一遍待处理更新，幂等无害），不值得备份成本。读写
均经 ``run_blocking_ops`` 下沉线程池（阻塞 I/O 不占事件循环）。文件
损坏 / 写失败均降级（告警 + 内存态继续），**不让 runner 死**。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from packages.harness.channels.channel_models import (
    AttachmentDeliveryResult,
    ChannelAttachmentKind,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.telegram.tg_client import TelegramClient
from packages.harness.channels.telegram.tg_models import TgMessageModel, TgUpdateModel
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.core.time_utils import now_ms

__all__ = [
    "TelegramChannelRunner",
    "TG_OFFSET_STATE_FILENAME",
    "TG_FILE_PSEUDO_SCHEME",
    "extract_tg_attachments",
    "map_tg_message_to_inbound",
]

#: offset 状态文件名（落在 ``state_dir`` 下）。
TG_OFFSET_STATE_FILENAME: str = "telegram_offset.json"

#: 入站附件平台文件引用伪方案前缀（``tg-file://{file_id}``——下载免取
#: v2 契约：真实媒体拉取 deferred 到 R2d；出站方向仅接受同形态作为
#: file_id 重发凭据）。
TG_FILE_PSEUDO_SCHEME: str = "tg-file://"

#: multipart 上传缺省 MIME（附件未携带 media_type 线索时）。
_UPLOAD_DEFAULT_CONTENT_TYPE: str = "application/octet-stream"

#: 去重窗口容量（update_id 已见集合上限——超出按 FIFO 淘汰；offset 游标
#: 已在服务端去重，本集合只兜底「确认前重投」窗口）。
_SEEN_UPDATE_CAP: int = 1024

#: 可注入休眠契约（测试注入 fake 记录退避节奏）。
SleepFn = Callable[[float], Awaitable[None]]


def _tg_media_method_for(attachment: ChannelMessageAttachment) -> tuple[str, str]:
    """解析附件 kind → Bot API 媒体发送方法 / 参数名二元组。

    映射表（docs/backend/channels.md「协议 v2」同源）：image →
    sendPhoto/photo；audio → media_type 含 ogg/opus 走 sendVoice/voice
    （语音消息形态），否则 sendAudio/audio（音乐文件形态）；file →
    sendDocument/document；sticker → sendSticker/sticker。

    Args:
        attachment: 出站附件。

    Returns:
        ``(api_method, media_field)`` 二元组（如 ``("sendPhoto", "photo")``）。
    """
    if attachment.kind is ChannelAttachmentKind.IMAGE:
        return ("sendPhoto", "photo")
    if attachment.kind is ChannelAttachmentKind.AUDIO:
        media_type = (attachment.media_type or "").lower()
        if "ogg" in media_type or "opus" in media_type:
            return ("sendVoice", "voice")
        return ("sendAudio", "audio")
    if attachment.kind is ChannelAttachmentKind.FILE:
        return ("sendDocument", "document")
    return ("sendSticker", "sticker")


def _upload_filename_for(attachment: ChannelMessageAttachment) -> str:
    """合成 multipart 上传文件名（kind + media_type 子类型派生）。

    Args:
        attachment: 出站附件（base64 源——上传形态）。

    Returns:
        文件名（如 ``image.jpeg`` / ``audio.ogg``；无 MIME 线索时
        ``<kind>.bin``——Bot API 仅 sendDocument 呈现文件名，其余方法
        忽略）。
    """
    subtype = (attachment.media_type or "").partition("/")[2]
    return f"{attachment.kind.value}.{subtype if subtype else 'bin'}"


def extract_tg_attachments(message: TgMessageModel) -> list[ChannelMessageAttachment]:
    """提取 Telegram 媒体字段为通道中立附件（下载免取——伪方案引用）。

    Args:
        message: Bot API message wire 对象。

    Returns:
        附件列表（无媒体字段时为空——纯文本消息形态）；每附件
        ``path="tg-file://{file_id}"``，caption 挂首附件（存在时）。
    """
    specs: list[tuple[ChannelAttachmentKind, str, str | None]] = []
    if message.photo:
        best = max(message.photo, key=lambda size: size.width * size.height)
        specs.append((ChannelAttachmentKind.IMAGE, best.file_id, None))
    if message.voice is not None:
        specs.append((ChannelAttachmentKind.AUDIO, message.voice.file_id, message.voice.mime_type))
    if message.audio is not None:
        specs.append((ChannelAttachmentKind.AUDIO, message.audio.file_id, message.audio.mime_type))
    if message.document is not None:
        specs.append(
            (ChannelAttachmentKind.FILE, message.document.file_id, message.document.mime_type)
        )
    if message.sticker is not None:
        specs.append((ChannelAttachmentKind.STICKER, message.sticker.file_id, None))
    attachments: list[ChannelMessageAttachment] = []
    for position, (kind, file_id, media_type) in enumerate(specs):
        attachments.append(
            ChannelMessageAttachment(
                kind=kind,
                media_type=media_type,
                path=f"{TG_FILE_PSEUDO_SCHEME}{file_id}",
                caption=message.caption if position == 0 else None,
            )
        )
    return attachments


def _decode_attachment_base64(value: str) -> bytes:
    """解码附件 base64 载荷为原始字节（严格校验——非法载荷拒绝）。

    Args:
        value: base64 编码字符串（``ChannelMessageAttachment.base64`` 源）。

    Returns:
        解码后的原始媒体字节。

    Raises:
        BusinessException: 载荷非合法 base64（``VALIDATION_ERROR``——
            逐附件结果承载，不上抛到消息面）。
    """
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="附件 base64 载荷不可解码（合法 base64 形态 required）",
        ) from exc


def map_tg_message_to_inbound(message: TgMessageModel) -> InboundMessageModel | None:
    """映射 Bot API message 为通道中立入站消息（不可路由形态返回 None）。

    路由门槛（v2）：文本或 caption 存在时以 caption 兜底为文本；仅媒体
    无 caption 时归一为 ``text=""`` 入站（附件照常搭载，路由器跳过生成）；
    既无文本又无附件才返回 None（调用方跳过路由、offset 照常推进）。

    Args:
        message: Bot API message wire 对象。

    Returns:
        入站消息（媒体附件已提取）；既无文本又无附件时返回 None（调用方
        跳过路由、offset 照常推进）。
    """
    text = message.text or message.caption
    attachments = extract_tg_attachments(message)
    if not text and not attachments:
        return None
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id=str(message.chat.id),
        message_id=str(message.message_id),
        sender_id=str(message.from_user.id) if message.from_user else "0",
        text=text or "",
        ts_ms=message.date * 1000,
        attachments=attachments,
        is_group_chat=message.chat.type in {"group", "supergroup", "channel"},
    )


class TelegramChannelRunner:
    """Telegram 通道长轮询 runner（长生命周期 ``run()`` + 发送面适配器）。

    一个对象承担两个面：

        - **轮询面**：``run()`` 长轮询循环（app lifespan 经 create_task
          启动 + background_task_manager 托管）；
        - **发送面**：``send_message(OutboundMessageModel)`` 实现
          :class:`packages.harness.channels.channel_service.ChannelAdapterProtocol`
          （路由器出站投递的 Telegram 适配——与轮询共享同一 client）。
    """

    def __init__(
        self,
        *,
        client: TelegramClient,
        router: ChannelRouter,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        state_dir: str = "data/channels",
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        """初始化 runner（未启动——``run()`` 由装配方驱动）。

        Args:
            client: Telegram Bot API 客户端（共享 httpx client 注入）。
            router: 通道路由器（入站消费 + 本对象注册为其发送面适配器）。
            backoff_base_seconds: 轮询失败退避基数秒（指数退避起点）。
            backoff_max_seconds: 退避封顶秒数。
            state_dir: 通道状态目录（offset 持久化文件落此）。
            sleep: 休眠函数（缺省 ``asyncio.sleep``；测试注入 fake 断言
                退避节奏——不真实等待）。
        """
        self._client = client
        self._router = router
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._state_dir = Path(state_dir)
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._offset: int | None = None
        self._seen_update_ids: set[int] = set()
        self._seen_order: deque[int] = deque()

    # ------------------------------------------------------------------
    # 生命周期面
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求优雅停机（置位事件——当前在途轮询完成后退出循环）。"""
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        """是否已请求停机。"""
        return self._stop_event.is_set()

    @property
    def offset(self) -> int | None:
        """当前已确认 offset 游标（None = 尚未消费任何更新）。"""
        return self._offset

    async def run(self) -> None:
        """长轮询主循环（长生命周期——由 app lifespan 作后台任务驱动）。

        Raises:
            BusinessException: 启动校验发现 token 无效
                （``CHANNEL_TOKEN_MISSING``——fail fast，任务终止）。其余
                一切失败均被本循环消化（退避重试 / 单消息隔离），不让
                任务死亡；``asyncio.CancelledError`` 原样上抛（停机 cancel
                路径）。
        """
        await self._startup_validate()
        self._offset = await self._load_offset()
        logger.info("Telegram 通道轮询启动: offset={}", self._offset)
        consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                updates = await self._client.get_updates(offset=self._offset)
                consecutive_failures = 0
                await self._process_updates(updates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                delay = self._backoff_delay(consecutive_failures)
                logger.warning(
                    "Telegram getUpdates 失败（连续 {} 次，退避 {:.1f}s）: {}",
                    consecutive_failures,
                    delay,
                    exc,
                )
                await self._sleep(delay)
        logger.info("Telegram 通道 runner 已停止: final_offset={}", self._offset)

    # ------------------------------------------------------------------
    # 发送面（ChannelAdapterProtocol 实现）
    # ------------------------------------------------------------------

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站消息（通道中立 → Bot API；超长分段由 client 承担）。

        v2 投递顺序（先文本后附件）：文本段 v1 调用形态零变更
        （``silent=True`` 时增传 ``disable_notification``——False 缺省
        不传参，v1 fake 契约形状保持）；附件逐个投递并聚合逐附件结果
        （单附件失败不上抛——见模块 docstring「出站 v2 投递」）。

        Args:
            message: 出站消息（通道中立形态；v2 附件 / silent 字段缺省
                时行为与 v1 完全一致）。

        Returns:
            投递结果（文本分段 + 成功附件的平台消息 ID 一并计入；
            ``attachment_results`` 按附件序对齐——部分成功可见）。

        Raises:
            BusinessException: **文本段**平台调用失败
                （``CHANNEL_API_ERROR`` / ``CHANNEL_TOKEN_MISSING``
                ——路由器负责兜底隔离；附件失败不上抛，走逐附件结果）。
        """
        text_kwargs: dict[str, Any] = {}
        if message.silent:
            text_kwargs["disable_notification"] = True
        results = await self._client.send_message(
            chat_id=message.chat_id,
            text=message.text,
            reply_to_message_id=message.reply_to_message_id,
            **text_kwargs,
        )
        message_ids = [str(result.message_id) for result in results]
        attachment_results: list[AttachmentDeliveryResult] = []
        for index, attachment in enumerate(message.attachments):
            outcome = await self._deliver_attachment(
                message.chat_id, attachment, index, silent=message.silent
            )
            attachment_results.append(outcome)
            if outcome.ok and outcome.provider_message_id is not None:
                message_ids.append(outcome.provider_message_id)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=message_ids,
            attachment_results=attachment_results,
        )

    async def _deliver_attachment(
        self,
        chat_id: str,
        attachment: ChannelMessageAttachment,
        index: int,
        *,
        silent: bool,
    ) -> AttachmentDeliveryResult:
        """投递单个出站附件（源形态解析 → 平台调用；失败不上抛）。

        Args:
            chat_id: 目标会话 ID。
            attachment: 出站附件（url / base64 / tg-file:// path 三源）。
            index: 附件序（``OutboundMessageModel.attachments`` 下标——
                结果对齐用）。
            silent: 免通知投递（透传平台 disable_notification）。

        Returns:
            该附件投递结局（失败时 ``ok=False`` + ``error`` 承载原因——
            文本与其余附件照常投递，不上抛）。
        """
        api_method, media_field = _tg_media_method_for(attachment)
        caption = None if attachment.kind is ChannelAttachmentKind.STICKER else attachment.caption
        path_value = attachment.path
        try:
            if attachment.url is not None:
                result = await self._client.send_media_ref(
                    chat_id=chat_id,
                    api_method=api_method,
                    media_field=media_field,
                    media_ref=attachment.url,
                    caption=caption,
                    disable_notification=silent,
                )
            elif attachment.base64 is not None:
                result = await self._client.send_media_upload(
                    chat_id=chat_id,
                    api_method=api_method,
                    media_field=media_field,
                    content=_decode_attachment_base64(attachment.base64),
                    filename=_upload_filename_for(attachment),
                    content_type=attachment.media_type or _UPLOAD_DEFAULT_CONTENT_TYPE,
                    caption=caption,
                    disable_notification=silent,
                )
            elif path_value is not None and path_value.startswith(TG_FILE_PSEUDO_SCHEME):
                result = await self._client.send_media_ref(
                    chat_id=chat_id,
                    api_method=api_method,
                    media_field=media_field,
                    media_ref=path_value[len(TG_FILE_PSEUDO_SCHEME) :],
                    caption=caption,
                    disable_notification=silent,
                )
            else:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=(
                        "出站附件 path 仅支持 tg-file:// 伪方案（file_id 重发）形态: "
                        f"{path_value!r}"
                    ),
                    details={"chat_id": chat_id, "path": path_value},
                )
        except BusinessException as exc:
            logger.warning(
                "Telegram 附件投递失败（逐附件结果承载）: chat_id={}, kind={}, error={}",
                chat_id,
                attachment.kind.value,
                exc.message,
            )
            return AttachmentDeliveryResult(index=index, ok=False, error=exc.message)
        return AttachmentDeliveryResult(
            index=index, ok=True, provider_message_id=str(result.message_id)
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _startup_validate(self) -> None:
        """启动校验 getMe（token 无效 fail fast；暂态失败仅告警继续）。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``——
                fail fast：任务带清晰错误终止，不进入轮询循环）。
        """
        try:
            bot = await self._client.get_me()
        except BusinessException as exc:
            if exc.code == ErrorCode.CHANNEL_TOKEN_MISSING:
                logger.error("Telegram 通道启动校验失败（token 无效）: {}", exc.message)
                raise
            logger.warning("Telegram getMe 启动校验暂不可达（进入退避重试循环）: {}", exc.message)
            return
        logger.info(
            "Telegram 通道启动校验通过: bot_id={}, bot_username={}",
            bot.id,
            bot.username or "(未提供)",
        )

    async def _process_updates(self, updates: Sequence[TgUpdateModel]) -> None:
        """处理一批更新（去重 + 映射 + 路由 + offset 推进持久化）。

        非消息更新（edited_message / callback_query 等）与不可路由消息
        （既无文本又无附件）跳过不路由，但 **offset 照常推进**（确认语义
        ——否则服务端会无限重投）。

        Args:
            updates: 本批更新（可能为空——长轮询窗口自然耗尽形态）。
        """
        if not updates:
            return
        for update in updates:
            if not self._remember_update(update.update_id):
                logger.debug("Telegram update 重复投递（去重跳过）: {}", update.update_id)
                continue
            message = update.message
            inbound = None if message is None else map_tg_message_to_inbound(message)
            if inbound is None:
                logger.debug("Telegram 不可路由更新（跳过路由）: {}", update.update_id)
                continue
            await self._router.handle_inbound(inbound)
        new_offset = max(update.update_id for update in updates) + 1
        if self._offset is None or new_offset > self._offset:
            self._offset = new_offset
            await self._persist_offset(new_offset)

    def _remember_update(self, update_id: int) -> bool:
        """登记 update_id（去重窗口；窗口超容按 FIFO 淘汰最旧）。

        Args:
            update_id: 更新 ID。

        Returns:
            首见返回 True；重复返回 False。
        """
        if update_id in self._seen_update_ids:
            return False
        self._seen_update_ids.add(update_id)
        self._seen_order.append(update_id)
        while len(self._seen_order) > _SEEN_UPDATE_CAP:
            self._seen_update_ids.discard(self._seen_order.popleft())
        return True

    def _backoff_delay(self, consecutive_failures: int) -> float:
        """指数退避延时（基数 × 2^(n-1)，封顶 max；失败次数下限 1）。

        Args:
            consecutive_failures: 连续失败次数（≥ 1）。

        Returns:
            本轮退避秒数。
        """
        exponent = max(0, consecutive_failures - 1)
        exponential = self._backoff_base_seconds * (2**exponent)
        return float(min(exponential, self._backoff_max_seconds))

    # ------------------------------------------------------------------
    # offset 持久化（阻塞 I/O 经 run_blocking_ops 下沉）
    # ------------------------------------------------------------------

    def _offset_file(self) -> Path:
        """offset 状态文件路径（``state_dir/telegram_offset.json``）。"""
        return self._state_dir / TG_OFFSET_STATE_FILENAME

    async def _load_offset(self) -> int | None:
        """恢复 offset 游标（文件缺失 / 损坏均降级为 None——首次拉取形态）。

        Returns:
            已持久化的 offset（None = 无状态文件 / 不可解析）。
        """
        path = self._offset_file()

        def _read_blocking() -> int | None:
            if not path.is_file():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "Telegram offset 状态文件不可解析（降级为首次拉取）: path={}, err={}",
                    path,
                    exc,
                )
                return None
            offset = data.get("offset") if isinstance(data, dict) else None
            return offset if isinstance(offset, int) else None

        return await run_blocking_ops(_read_blocking)

    async def _persist_offset(self, offset: int) -> None:
        """持久化 offset 游标（原子临时文件 + 换名；失败降级继续内存态）。

        Args:
            offset: 待持久化的游标值。
        """
        path = self._offset_file()
        content = json.dumps({"offset": offset}, ensure_ascii=False) + "\n"

        def _write_blocking() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.tmp-{now_ms()}")
            try:
                with tmp_path.open("w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            os.replace(tmp_path, path)

        try:
            await run_blocking_ops(_write_blocking)
        except Exception:
            # 不让 runner 死：写失败仅告警（重启可能重消费——幂等无害）
            logger.opt(exception=True).error(
                "Telegram offset 持久化失败（继续内存态）: path={}", path
            )
