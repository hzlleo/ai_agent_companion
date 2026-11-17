"""telegram 通道 runner 测试——fake client / fake router / 注入休眠（零真实网络）。

覆盖矩阵：

    - 启动校验（getMe token 无效 fail fast / 暂态失败进入退避循环）；
    - offset 游标推进 + 批间传递 + JSON 状态文件原子持久化往返；
    - update 去重（同 update_id 重投只路由一次）+ 非文本更新跳过仍推进；
    - 失败退避（指数 + 封顶 + 成功复位）；
    - 优雅停机（request_stop → 循环退出）与发送面适配（Outbound 投递）；
    - 状态文件损坏 / 写失败降级（runner 不死）；
    - 协议 v2 入站：媒体消息 caption 兜底文本 + 附件提取（tg-file://
      伪方案 / 最大尺寸 photo / MIME 线索 / 纯媒体无 caption 跳过）；
    - 协议 v2 出站：silent 透传 disable_notification；附件按 kind 分流
      （base64 上传 / url JSON / tg-file 重发 / sticker caption 丢弃）；
      逐附件部分失败聚合（文本照常 + ok=False 承载）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.telegram import tg_runner as tg_runner_module
from packages.harness.channels.telegram.tg_models import (
    TgAudioModel,
    TgBotInfoModel,
    TgChatModel,
    TgDocumentModel,
    TgMessageModel,
    TgPhotoSizeModel,
    TgSendMessageResult,
    TgStickerModel,
    TgUpdateModel,
    TgUserModel,
    TgVoiceModel,
)
from packages.harness.channels.telegram.tg_runner import (
    TG_FILE_PSEUDO_SCHEME,
    TG_OFFSET_STATE_FILENAME,
    TelegramChannelRunner,
    extract_tg_attachments,
    map_tg_message_to_inbound,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _update(
    update_id: int,
    *,
    text: str | None = "你好",
    chat_id: int = 10086,
) -> TgUpdateModel:
    """构造 message update（测试直供形态）。

    Args:
        update_id: 更新 ID。
        text: 消息文本（None = 非文本消息）。
        chat_id: 会话 ID。

    Returns:
        :class:`TgUpdateModel` 实例。
    """
    return TgUpdateModel(
        update_id=update_id,
        message=TgMessageModel(
            message_id=update_id * 10,
            chat=TgChatModel(id=chat_id, type="private"),
            from_user=TgUserModel(id=7),
            text=text,
            date=1731000000,
        ),
    )


class FakeTgClient:
    """fake TelegramClient——脚本化批次 / 可编程失败 / 记录调用形状。

    v2 扩展面：``send_message`` 增 ``disable_notification``（None 缺省
    不记录——既有 3 键断言形态保持）；媒体发送记录进独立 ``media_calls``
    （可编程逐媒体失败——部分成功聚合断言原料）。
    """

    def __init__(
        self,
        *,
        batches: list[list[TgUpdateModel]] | None = None,
        fail_first_polls: int = 0,
        get_me_error: Exception | None = None,
        media_errors: dict[int, Exception] | None = None,
        send_error: Exception | None = None,
    ) -> None:
        """初始化 fake 客户端。

        Args:
            batches: 脚本化轮询批次（弹尽后恒空）。
            fail_first_polls: 前 N 次轮询可编程失败（退避测试）。
            get_me_error: getMe 故障注入（启动校验测试）。
            media_errors: 媒体发送故障注入（附件序 → 异常——逐附件
                部分失败聚合测试用）。
            send_error: 文本发送故障注入（None = 恒成功；v1 上抛语义
                回归测试用）。
        """
        self.polls: list[int | None] = []
        self._batches = list(batches or [])
        self._fail_first = fail_first_polls
        self.get_me_error = get_me_error
        self.sent: list[dict[str, Any]] = []
        self.media_calls: list[dict[str, Any]] = []
        self._media_errors = media_errors or {}
        self._send_error = send_error

    async def get_me(self) -> TgBotInfoModel:
        """fake getMe（可编程抛错）。"""
        if self.get_me_error is not None:
            raise self.get_me_error
        return TgBotInfoModel(id=42, username="companion_bot", first_name="C")

    async def get_updates(self, *, offset: int | None = None) -> list[TgUpdateModel]:
        """fake getUpdates（脚本批次弹尽后恒空 + 可编程连续失败）。"""
        await asyncio.sleep(0)
        if self._fail_first > 0:
            self._fail_first -= 1
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="轮询失败（测试注入）"
            )
        self.polls.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | str | None = None,
        disable_notification: bool | None = None,
    ) -> list[TgSendMessageResult]:
        """fake send_message（记录出站形状——disable_notification 仅在
        非 None 时记录，既有 3 键断言形态保持；可编程抛错）。"""
        if self._send_error is not None:
            raise self._send_error
        record: dict[str, Any] = {"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id}
        if disable_notification is not None:
            record["disable_notification"] = disable_notification
        self.sent.append(record)
        return [TgSendMessageResult(message_id=900, chat=TgChatModel(id=1, type="private"))]

    async def send_media_ref(
        self,
        *,
        chat_id: int | str,
        api_method: str,
        media_field: str,
        media_ref: str,
        caption: str | None = None,
        disable_notification: bool | None = None,
    ) -> TgSendMessageResult:
        """fake 媒体引用发送（记录调用形状；可编程按附件序抛错）。"""
        call_index = len(self.media_calls)
        self.media_calls.append(
            {
                "form": "ref",
                "chat_id": chat_id,
                "api_method": api_method,
                "media_field": media_field,
                "media_ref": media_ref,
                "caption": caption,
                "disable_notification": disable_notification,
            }
        )
        error = self._media_errors.get(call_index)
        if error is not None:
            raise error
        return TgSendMessageResult(
            message_id=1000 + call_index, chat=TgChatModel(id=1, type="private")
        )

    async def send_media_upload(
        self,
        *,
        chat_id: int | str,
        api_method: str,
        media_field: str,
        content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        caption: str | None = None,
        disable_notification: bool | None = None,
    ) -> TgSendMessageResult:
        """fake 媒体上传发送（记录调用形状；可编程按附件序抛错）。"""
        call_index = len(self.media_calls)
        self.media_calls.append(
            {
                "form": "upload",
                "chat_id": chat_id,
                "api_method": api_method,
                "media_field": media_field,
                "content": content,
                "filename": filename,
                "content_type": content_type,
                "caption": caption,
                "disable_notification": disable_notification,
            }
        )
        error = self._media_errors.get(call_index)
        if error is not None:
            raise error
        return TgSendMessageResult(
            message_id=1000 + call_index, chat=TgChatModel(id=1, type="private")
        )


class FakeRouter:
    """fake ChannelRouter——记录入站消息。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 路由（记录 + 恒成功投递形态）。"""
        await asyncio.sleep(0)
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=message.channel, chat_id=message.chat_id, message_ids=["900"]
        )


class RecordingSleep:
    """注入休眠——记录退避节奏（不真实等待；可编程在累计 N 次后请求停机）。"""

    def __init__(self, stop_after_delays: int | None = None) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after_delays
        self._stop_target: list[TelegramChannelRunner] = []

    def bind_runner(self, runner: TelegramChannelRunner) -> None:
        """绑定停机目标（runner 构造后回填——闭包持有引用）。"""
        self._stop_target.append(runner)

    async def __call__(self, delay: float) -> None:
        """记录延时并让出控制点（测试驱动节奏断言）。"""
        self.delays.append(delay)
        if self._stop_after is not None and len(self.delays) >= self._stop_after:
            for runner in self._stop_target:
                runner.request_stop()
        await asyncio.sleep(0)


def _runner(
    client: FakeTgClient,
    router: FakeRouter,
    *,
    state_dir: Path,
    sleep: RecordingSleep | None = None,
    backoff_base_seconds: float = 1.0,
    backoff_max_seconds: float = 60.0,
) -> TelegramChannelRunner:
    """构造被测 runner（fake client / fake router / 注入休眠）。"""
    return TelegramChannelRunner(
        client=client,
        router=router,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        state_dir=str(state_dir),
        sleep=sleep if sleep is not None else RecordingSleep(),
    )


async def _run_until(task: asyncio.Task[None], predicate: Any, *, timeout: float = 5.0) -> None:
    """等待谓词成立（真实时钟有界轮询——线程池往返需真实毫秒）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if task.done():
            return
        assert loop.time() < deadline, "等待 runner 推进超时（测试驱动死锁）"
        await asyncio.sleep(0.01)


class TestStartupValidation:
    async def test_invalid_token_fails_fast(self, tmp_path: Path) -> None:
        """getMe token 无效：run() 即抛 CHANNEL_TOKEN_MISSING（fail fast）。"""
        client = FakeTgClient(
            get_me_error=BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING, message="Telegram bot token 无效"
            )
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        with pytest.raises(BusinessException) as exc_info:
            await runner.run()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
        assert client.polls == []
        assert router.handled == []

    async def test_transient_get_me_failure_continues(self, tmp_path: Path) -> None:
        """getMe 暂态失败（非 token 类）：告警后进入轮询循环（不终止）。"""
        client = FakeTgClient(
            get_me_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="暂不可达（测试注入）"
            ),
            batches=[[_update(1)]],
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert len(router.handled) == 1
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestOffsetHandling:
    async def test_offset_advances_and_persists(self, tmp_path: Path) -> None:
        """offset 推进：批内最大 update_id + 1，并原子落盘状态文件。"""
        client = FakeTgClient(batches=[[_update(5), _update(8)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert client.polls[0] is None
            assert all(offset == 9 for offset in client.polls[1:])
            assert runner.offset == 9
            state_file = tmp_path / TG_OFFSET_STATE_FILENAME
            assert state_file.is_file()
            assert json.loads(state_file.read_text(encoding="utf-8")) == {"offset": 9}
            assert list(tmp_path.glob(f"{TG_OFFSET_STATE_FILENAME}.tmp-*")) == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_persisted_offset_round_trip(self, tmp_path: Path) -> None:
        """持久化往返：重启后首个 getUpdates 携带已持久化 offset（不重消费）。"""
        first_client = FakeTgClient(batches=[[_update(5)]])
        first_runner = _runner(first_client, FakeRouter(), state_dir=tmp_path)
        first_task = asyncio.create_task(first_runner.run())
        await _run_until(first_task, lambda: len(first_client.polls) >= 1)
        first_runner.request_stop()
        await asyncio.wait_for(first_task, timeout=5.0)

        second_client = FakeTgClient()
        second_runner = _runner(second_client, FakeRouter(), state_dir=tmp_path)
        second_task = asyncio.create_task(second_runner.run())
        try:
            await _run_until(second_task, lambda: len(second_client.polls) >= 1)
            assert second_client.polls[0] == 6
            assert all(offset == 6 for offset in second_client.polls)
        finally:
            second_runner.request_stop()
            await asyncio.wait_for(second_task, timeout=5.0)

    async def test_corrupt_state_file_degrades_to_first_poll(self, tmp_path: Path) -> None:
        """状态文件损坏：降级为首次拉取（offset=None——告警不死）。"""
        state_file = tmp_path / TG_OFFSET_STATE_FILENAME
        state_file.write_text("not-json{", encoding="utf-8")
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert client.polls[0] is None
            assert runner.offset is None
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestUpdateProcessing:
    async def test_duplicate_update_routed_once(self, tmp_path: Path) -> None:
        """去重：同 update_id 重投（服务端未确认形态）只路由一次。"""
        client = FakeTgClient(batches=[[_update(5)], [_update(5), _update(6)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 2)
            assert [message.text for message in router.handled] == ["你好", "你好"]
            assert runner.offset == 7
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_non_text_updates_skipped_but_offset_advances(self, tmp_path: Path) -> None:
        """非文本更新（无 message / 无 text）：跳过路由但 offset 照常推进。"""
        client = FakeTgClient(
            batches=[
                [
                    TgUpdateModel(update_id=1),
                    _update(2, text=None),
                    _update(3),
                ]
            ]
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert len(router.handled) == 1
            assert router.handled[0].message_id == "30"
            assert runner.offset == 4
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_inbound_mapping_normalizes_ids_and_ts(self, tmp_path: Path) -> None:
        """入站映射：ID 字符串化 + epoch 秒 → 毫秒归一。"""
        client = FakeTgClient(batches=[[_update(5, chat_id=20000)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 1)
            message = router.handled[0]
            assert message.channel is ChannelType.TELEGRAM
            assert message.chat_id == "20000"
            assert message.sender_id == "7"
            assert message.ts_ms == 1731000000 * 1000
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_anonymous_sender_normalized_zero(self, tmp_path: Path) -> None:
        """匿名发送者（from 缺失——频道帖子形态）：归一 "0"。"""
        update = TgUpdateModel(
            update_id=1,
            message=TgMessageModel(
                message_id=10,
                chat=TgChatModel(id=100, type="channel"),
                from_user=None,
                text="频道帖",
                date=1731000000,
            ),
        )
        client = FakeTgClient(batches=[[update]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 1)
            assert router.handled[0].sender_id == "0"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestBackoff:
    async def test_exponential_backoff_capped(self, tmp_path: Path) -> None:
        """退避节奏：基数指数 → 封顶（2, 4, 8, 8）；恢复成功后不再退避。"""
        client = FakeTgClient(batches=[[_update(1)]], fail_first_polls=4)
        sleep = RecordingSleep()
        runner = _runner(
            client,
            FakeRouter(),
            state_dir=tmp_path,
            sleep=sleep,
            backoff_base_seconds=2.0,
            backoff_max_seconds=8.0,
        )
        task = asyncio.create_task(runner.run())
        try:
            # 恢复成功的首个 poll 落地后，失败计数已冻结为 4（后续无退避）
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert sleep.delays == [2.0, 4.0, 8.0, 8.0]
            assert client.polls[0] is None
            assert all(offset == 2 for offset in client.polls[1:])
            assert runner.offset == 2
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_repeated_failures_keep_polling(self, tmp_path: Path) -> None:
        """连续失败不终止循环（runner 不死——退避后继续轮询）。"""
        client = FakeTgClient(fail_first_polls=100)
        sleep = RecordingSleep(stop_after_delays=3)
        runner = _runner(client, FakeRouter(), state_dir=tmp_path, sleep=sleep)
        sleep.bind_runner(runner)
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(task, timeout=5.0)
        assert sleep.delays == [1.0, 2.0, 4.0]
        assert task.exception() is None
        assert client.polls == []


class TestGracefulStop:
    async def test_request_stop_exits_loop_cleanly(self, tmp_path: Path) -> None:
        """优雅停机：request_stop → 循环退出（任务正常完成无异常）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        await _run_until(task, lambda: len(client.polls) >= 1)
        assert runner.stop_requested is False
        runner.request_stop()
        await asyncio.wait_for(task, timeout=5.0)
        assert task.exception() is None
        assert runner.stop_requested is True

    async def test_offset_persist_failure_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """offset 写失败降级：告警继续（runner 不死；内存 offset 已推进）。"""
        client = FakeTgClient(batches=[[_update(5)]])
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)

        def _explode(func: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(func, "__name__", "") == "_write_blocking":
                raise OSError("disk full (test injected)")
            return asyncio.get_running_loop().run_in_executor(None, lambda: func(*args, **kwargs))

        monkeypatch.setattr(tg_runner_module, "run_blocking_ops", _explode)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert runner.offset == 6
            assert not (tmp_path / TG_OFFSET_STATE_FILENAME).is_file()
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestSendAdapterFace:
    async def test_send_message_maps_delivery_result(self, tmp_path: Path) -> None:
        """发送面适配：Outbound → client 调用形状 → 投递结果归一。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        result = await runner.send_message(
            OutboundMessageModel(chat_id="10086", text="回复", reply_to_message_id="42")
        )
        assert client.sent == [{"chat_id": "10086", "text": "回复", "reply_to": "42"}]
        assert result.channel is ChannelType.TELEGRAM
        assert result.chat_id == "10086"
        assert result.message_ids == ["900"]


def _media_update(
    update_id: int,
    *,
    chat_id: int = 10086,
    caption: str | None = None,
    photo: list[TgPhotoSizeModel] | None = None,
    voice: TgVoiceModel | None = None,
    audio: TgAudioModel | None = None,
    document: TgDocumentModel | None = None,
    sticker: TgStickerModel | None = None,
) -> TgUpdateModel:
    """构造媒体 message update（测试直供形态——无 text，媒体 + 可选 caption）。

    Args:
        update_id: 更新 ID。
        chat_id: 会话 ID。
        caption: 媒体说明（None = 纯媒体形态）。
        photo: 图片尺寸数组。
        voice: 语音对象。
        audio: 音频文件对象。
        document: 通用文件对象。
        sticker: 贴纸对象。

    Returns:
        update wire 对象。
    """
    return TgUpdateModel(
        update_id=update_id,
        message=TgMessageModel(
            message_id=update_id * 10,
            chat=TgChatModel(id=chat_id, type="private"),
            from_user=TgUserModel(id=7),
            text=None,
            caption=caption,
            photo=photo,
            voice=voice,
            audio=audio,
            document=document,
            sticker=sticker,
            date=1731000000,
        ),
    )


class TestInboundMediaV2:
    async def test_photo_with_caption_routes_and_carries_attachment(self, tmp_path: Path) -> None:
        """媒体 + caption：caption 兜底为文本 + 附件伪方案引用（v2 入站）。"""
        update = _media_update(
            5,
            caption="看看这只猫",
            photo=[
                TgPhotoSizeModel(file_id="SMALL", width=10, height=10),
                TgPhotoSizeModel(file_id="BIG", width=800, height=600),
            ],
        )
        client = FakeTgClient(batches=[[update]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 1)
            message = router.handled[0]
            assert message.text == "看看这只猫"
            assert len(message.attachments) == 1
            attachment = message.attachments[0]
            assert attachment.kind is ChannelAttachmentKind.IMAGE
            assert attachment.path == f"{TG_FILE_PSEUDO_SCHEME}BIG"
            assert attachment.caption == "看看这只猫"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_pure_media_without_caption_skipped(self, tmp_path: Path) -> None:
        """纯媒体无 caption：维持 v1 跳过语义（不路由，offset 照常推进）。"""
        update = _media_update(5, sticker=TgStickerModel(file_id="CAACAgIAAx0"))
        client = FakeTgClient(batches=[[update, _update(6)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert [message.text for message in router.handled] == ["你好"]
            assert runner.offset == 7
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_voice_message_maps_mime_and_pseudo_scheme(self) -> None:
        """语音消息：audio 类 + MIME 线索 + 伪方案（纯函数直测面）。"""
        update = _media_update(
            7,
            caption="听我说",
            voice=TgVoiceModel(file_id="VOICE-FID", mime_type="audio/ogg"),
        )
        inbound = map_tg_message_to_inbound(
            update.message or TgMessageModel(message_id=1, chat=TgChatModel(id=1))
        )
        assert inbound is not None
        assert inbound.text == "听我说"
        attachment = inbound.attachments[0]
        assert attachment.kind is ChannelAttachmentKind.AUDIO
        assert attachment.media_type == "audio/ogg"
        assert attachment.path == f"{TG_FILE_PSEUDO_SCHEME}VOICE-FID"

    async def test_document_message_maps_file_kind(self) -> None:
        """文件消息：file 类 + MIME 线索（纯函数直测面）。"""
        update = _media_update(
            8,
            caption="文件给你",
            document=TgDocumentModel(
                file_id="DOC-FID", file_name="report.pdf", mime_type="application/pdf"
            ),
        )
        inbound = map_tg_message_to_inbound(
            update.message or TgMessageModel(message_id=1, chat=TgChatModel(id=1))
        )
        assert inbound is not None
        attachment = inbound.attachments[0]
        assert attachment.kind is ChannelAttachmentKind.FILE
        assert attachment.media_type == "application/pdf"
        assert attachment.path == f"{TG_FILE_PSEUDO_SCHEME}DOC-FID"

    async def test_extract_attachments_empty_for_text_message(self) -> None:
        """纯文本消息：无媒体字段 → 附件空列表（v1 形态保持）。"""
        message = TgMessageModel(
            message_id=1,
            chat=TgChatModel(id=1, type="private"),
            text="你好",
            date=1731000000,
        )
        assert extract_tg_attachments(message) == []
        inbound = map_tg_message_to_inbound(message)
        assert inbound is not None
        assert inbound.attachments == []

    async def test_map_unroutable_returns_none(self) -> None:
        """无文本无 caption：映射返回 None（调用方跳过路由）。"""
        message = TgMessageModel(
            message_id=1,
            chat=TgChatModel(id=1, type="private"),
            text=None,
            photo=[TgPhotoSizeModel(file_id="P")],
            date=1731000000,
        )
        assert map_tg_message_to_inbound(message) is None


class TestOutboundV2:
    async def test_silent_passes_disable_notification(self, tmp_path: Path) -> None:
        """silent=True：文本调用携带 disable_notification（v1 缺省不传参）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        await runner.send_message(OutboundMessageModel(chat_id="10086", text="嘘", silent=True))
        assert client.sent == [
            {
                "chat_id": "10086",
                "text": "嘘",
                "reply_to": None,
                "disable_notification": True,
            }
        ]
        await runner.send_message(OutboundMessageModel(chat_id="10086", text="正常"))
        assert "disable_notification" not in client.sent[1]

    async def test_attachment_base64_uploads_multipart(self, tmp_path: Path) -> None:
        """image + base64：sendPhoto multipart 上传（解码字节 / 合成文件名 /
        MIME / caption / silent 透传）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        payload = base64.b64encode(b"fake-jpeg-bytes").decode("ascii")
        result = await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="图来了",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE,
                        media_type="image/jpeg",
                        base64=payload,
                        caption="随手拍",
                    )
                ],
                silent=True,
            )
        )
        assert len(client.media_calls) == 1
        call = client.media_calls[0]
        assert call["form"] == "upload"
        assert call["api_method"] == "sendPhoto"
        assert call["media_field"] == "photo"
        assert call["content"] == b"fake-jpeg-bytes"
        assert call["filename"] == "image.jpeg"
        assert call["content_type"] == "image/jpeg"
        assert call["caption"] == "随手拍"
        assert call["disable_notification"] is True
        assert result.message_ids == ["900", "1000"]
        assert result.attachment_results[0].ok is True
        assert result.attachment_results[0].provider_message_id == "1000"

    async def test_attachment_audio_ogg_routes_send_voice(self, tmp_path: Path) -> None:
        """audio + ogg MIME：sendVoice（语音形态）；mpeg 走 sendAudio。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="语音",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.AUDIO,
                        media_type="audio/ogg",
                        url="https://example.com/v.ogg",
                    ),
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.AUDIO,
                        media_type="audio/mpeg",
                        url="https://example.com/a.mp3",
                    ),
                ],
            )
        )
        assert [call["api_method"] for call in client.media_calls] == [
            "sendVoice",
            "sendAudio",
        ]
        assert client.media_calls[0]["media_field"] == "voice"
        assert client.media_calls[1]["media_field"] == "audio"

    async def test_attachment_url_sends_json_ref(self, tmp_path: Path) -> None:
        """file + url：sendDocument JSON 引用形态（直传 URL）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="文件",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.FILE,
                        media_type="application/pdf",
                        url="https://example.com/report.pdf",
                        caption="报表",
                    )
                ],
            )
        )
        call = client.media_calls[0]
        assert call["form"] == "ref"
        assert call["api_method"] == "sendDocument"
        assert call["media_ref"] == "https://example.com/report.pdf"
        assert call["caption"] == "报表"

    async def test_attachment_sticker_path_resends_file_id(self, tmp_path: Path) -> None:
        """sticker + tg-file:// path：sendSticker file_id 重发（方案前缀剥
        离）+ caption 丢弃（sendSticker 无 caption 参数——平台差异）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="贴纸",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.STICKER,
                        path=f"{TG_FILE_PSEUDO_SCHEME}CAACAgIAAx0",
                        caption="不该出现",
                    )
                ],
            )
        )
        call = client.media_calls[0]
        assert call["api_method"] == "sendSticker"
        assert call["media_ref"] == "CAACAgIAAx0"
        assert call["caption"] is None

    async def test_partial_attachment_failure_aggregated(self, tmp_path: Path) -> None:
        """部分失败聚合：文本 ok + 附件一 ok + 附件二失败 → 结果承载不抛。"""
        client = FakeTgClient(
            media_errors={
                1: BusinessException(
                    code=ErrorCode.CHANNEL_API_ERROR, message="平台拒绝该图片（测试注入）"
                )
            }
        )
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        result = await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="两张图",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, url="https://x/1.jpg"
                    ),
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, url="https://x/2.jpg"
                    ),
                ],
            )
        )
        assert client.sent[0]["text"] == "两张图"
        assert result.message_ids == ["900", "1000"]
        assert [outcome.ok for outcome in result.attachment_results] == [True, False]
        assert result.attachment_results[1].error is not None
        assert "平台拒绝" in result.attachment_results[1].error
        assert result.attachment_results[1].provider_message_id is None

    async def test_invalid_base64_surfaces_attachment_error(self, tmp_path: Path) -> None:
        """非法 base64 载荷：逐附件 ok=False（VALIDATION_ERROR 文案承载）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        result = await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="坏载荷",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, base64="!!!not-base64!!!"
                    )
                ],
            )
        )
        assert result.attachment_results[0].ok is False
        assert "base64" in (result.attachment_results[0].error or "")
        assert result.message_ids == ["900"]

    async def test_unsupported_path_form_surfaces_attachment_error(self, tmp_path: Path) -> None:
        """非伪方案 path：逐附件 ok=False（出站 path 仅接受 tg-file:// 形态）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        result = await runner.send_message(
            OutboundMessageModel(
                chat_id="10086",
                text="本地路径",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, path="/tmp/local.jpg"
                    )
                ],
            )
        )
        assert result.attachment_results[0].ok is False
        assert "tg-file://" in (result.attachment_results[0].error or "")

    async def test_text_failure_still_raises_for_router_fallback(self, tmp_path: Path) -> None:
        """文本段失败：整条上抛（v1 语义保持——路由器兜底隔离面）。"""
        client = FakeTgClient(
            send_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="文本发送失败（测试注入）"
            )
        )
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        with pytest.raises(BusinessException):
            await runner.send_message(OutboundMessageModel(chat_id="10086", text="必失败"))
