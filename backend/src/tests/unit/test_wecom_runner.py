"""wecom 通道 runner 测试——fake 客户端 / fake 路由器 / 伪时钟（零真实网络）。

覆盖矩阵：

    - 入站：单聊 / 群聊映射、msgid 去重、事件帧不路由、不可路由形态跳过、
      队列过载丢弃计数、worker 处理异常隔离；
    - 回复上下文：新鲜时走 ``aibot_respond_msg``（透传回调 req_id）、过期 /
      缺失降级为主动推送（chat_type 正确）、访问器暴露；
    - 文本分段：超平台上限时首段回复 + 续段推送；
    - 附件：base64 / 本地路径 / url（共享 HTTP 池）三源上传与推送映射、
      贴纸归图片、上传失败与非法载荷逐附件失败承载、url 无池拒绝；
    - 生命周期：request_stop 传导到客户端、run 收尾 worker。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    OutboundMessageModel,
)
from packages.harness.channels.wecom.wecom_models import (
    WECOM_MARKDOWN_MAX_BYTES,
    WeComCallbackFrameModel,
    WeComMediaKind,
)
from packages.harness.channels.wecom.wecom_runner import WeComChannelRunner
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.wecom_fakes import FakeRouter, FakeWeComClient, make_callback_frame


class _Clock:
    """可推进的测试时钟（epoch 毫秒——回复窗口判定输入）。"""

    def __init__(self, now_ms: int = 1_731_000_000_000) -> None:
        """初始化时钟。

        Args:
            now_ms: 初始时刻（epoch 毫秒）。
        """
        self.now_ms = now_ms

    def __call__(self) -> int:
        """读取当前时刻（epoch 毫秒）。"""
        return self.now_ms

    def advance_ms(self, delta_ms: int) -> None:
        """推进时钟。

        Args:
            delta_ms: 推进毫秒数。
        """
        self.now_ms += delta_ms


def _runner(
    *,
    client: Any = None,
    router: Any = None,
    clock: _Clock | None = None,
    http_client: httpx.AsyncClient | None = None,
    reply_window_seconds: float = 86400.0,
    inbound_queue_size: int = 16,
    worker_count: int = 1,
    dedup_capacity: int = 1024,
) -> WeComChannelRunner:
    """构造测试 runner（fake 客户端 / 路由器——零真实连接）。

    Args:
        client: 客户端替身（None = 新建）。
        router: 路由器替身（None = 新建）。
        clock: 测试时钟（None = 新建）。
        http_client: 共享 HTTP 替身（附件 url 源）。
        reply_window_seconds: 回复窗口秒数。
        inbound_queue_size: 入站队列容量。
        worker_count: 入站 worker 数。
        dedup_capacity: msgid 去重窗口容量。

    Returns:
        配置好的 runner。
    """
    return WeComChannelRunner(
        client=client if client is not None else FakeWeComClient(),
        router=router if router is not None else FakeRouter(),
        reply_window_seconds=reply_window_seconds,
        http_client=http_client,
        inbound_queue_size=inbound_queue_size,
        worker_count=worker_count,
        dedup_capacity=dedup_capacity,
        clock=clock if clock is not None else _Clock(),
    )


def _text_frame(
    *,
    msgid: str = "msg-1",
    userid: str = "u1",
    chattype: str = "single",
    chatid: str | None = None,
    content: str = "你好",
    req_id: str = "cb-req-1",
) -> WeComCallbackFrameModel:
    """构造文本消息回调帧。

    Args:
        msgid: 消息唯一标志（去重键）。
        userid: 发送者 userid。
        chattype: 会话类型（single / group）。
        chatid: 群聊 ID（群聊必填）。
        content: 消息文本。
        req_id: 回调请求标识（回复上下文原料）。

    Returns:
        回调帧模型。
    """
    body: dict[str, Any] = {
        "msgid": msgid,
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": "text",
        "text": {"content": content},
    }
    if chatid is not None:
        body["chatid"] = chatid
    return WeComCallbackFrameModel.model_validate(
        {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}
    )


async def _wait_until(predicate: Any, *, timeout_seconds: float = 2.0) -> None:
    """让出控制点直到条件成立（有界等待——超时即失败）。

    Args:
        predicate: 零参判定函数。
        timeout_seconds: 等待上限秒数。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("条件在等待上限内未成立")


class TestInbound:
    async def test_single_chat_message_routed(self) -> None:
        """单聊文本 → 路由器收到 single: 会话键的入站消息。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame())
            await _wait_until(lambda: len(router.received) == 1)
            inbound = router.received[0]
            assert inbound.chat_id == "single:u1"
            assert inbound.text == "你好"
            assert inbound.message_id == "msg-1"
            assert inbound.is_group_chat is False
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_group_message_marked_group(self) -> None:
        """群聊回调 → group: 会话键 + 群聊标记（来源分级）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(
                _text_frame(chattype="group", chatid="chat-1", userid="u9")
            )
            await _wait_until(lambda: len(router.received) == 1)
            assert router.received[0].chat_id == "group:chat-1"
            assert router.received[0].is_group_chat is True
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_duplicate_msgid_deduplicated(self) -> None:
        """同 msgid 重投只路由一次（官方文档指定 msgid 为排重键）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            frame = _text_frame(msgid="dup-1")
            await client.emit_frame(frame)
            await _wait_until(lambda: len(router.received) == 1)
            await client.emit_frame(frame)
            await asyncio.sleep(0.02)
            assert len(router.received) == 1
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_event_frame_not_routed(self) -> None:
        """事件回调不产出回合（enter_chat 仅观测）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            event = WeComCallbackFrameModel.model_validate(
                {
                    "cmd": "aibot_event_callback",
                    "headers": {"req_id": "ev-1"},
                    "body": {
                        "msgid": "ev-msg-1",
                        "msgtype": "event",
                        "event": {"eventtype": "enter_chat"},
                    },
                }
            )
            await client.emit_frame(event)
            await asyncio.sleep(0.02)
            assert router.received == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_unroutable_frame_skipped(self) -> None:
        """未知消息类型不可路由（不投递到路由器）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            frame = WeComCallbackFrameModel.model_validate_json(
                make_callback_frame(
                    {
                        "msgid": "m-unknown",
                        "chattype": "single",
                        "from": {"userid": "u1"},
                        "msgtype": "location",
                    }
                )
            )
            await client.emit_frame(frame)
            await asyncio.sleep(0.02)
            assert router.received == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_queue_overflow_drops_and_counts(self) -> None:
        """队列过载丢弃并计数（单 worker 被闸门占住时第二条入队、第三条丢弃）。"""
        router = FakeRouter()
        router.hold = asyncio.Event()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router, inbound_queue_size=1, worker_count=1)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(msgid="q1"))
            # 等 worker 取走第一条（队列空）后 worker 被闸门占住——此时容量 1 的
            # 队列可容纳 q2，q3 必然丢弃。
            await _wait_until(lambda: runner.pending_inbound == 0)
            await client.emit_frame(_text_frame(msgid="q2"))
            await client.emit_frame(_text_frame(msgid="q3"))
            assert runner.dropped_inbound == 1
            router.hold.set()
            await _wait_until(lambda: len(router.received) == 2)
        finally:
            router.hold.set()
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_router_failure_isolated(self) -> None:
        """路由器异常不终止 worker（后续消息照常处理）。"""
        router = FakeRouter()
        router.fail_next = True
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(msgid="f1"))
            await client.emit_frame(_text_frame(msgid="f2"))
            await _wait_until(lambda: len(router.received) == 1)
            assert router.received[0].message_id == "f2"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestReplyContext:
    async def test_context_registered_and_exposed(self) -> None:
        """消息回调登记回复上下文（req_id + 到达时刻）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(req_id="cb-42"))
            context = runner.reply_context("single:u1")
            assert context is not None
            assert context.req_id == "cb-42"
            assert runner.reply_context("single:nobody") is None
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_expired_context_hidden(self) -> None:
        """超过回复窗口的上下文不可见（降级主动推送）。"""
        clock = _Clock()
        client = FakeWeComClient()
        runner = _runner(client=client, clock=clock, reply_window_seconds=60.0)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(req_id="cb-9"))
            assert runner.reply_context("single:u1") is not None
            clock.advance_ms(61_000)
            assert runner.reply_context("single:u1") is None
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestOutboundText:
    async def test_reply_uses_fresh_context(self) -> None:
        """新鲜上下文 → 首段走回复命令（透传回调 req_id）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(req_id="cb-77"))
            result = await runner.send_message(
                OutboundMessageModel(chat_id="single:u1", text="好的", silent=True)
            )
            assert client.markdown_replies == [("cb-77", "好的")]
            assert client.markdown_pushes == []
            assert result.message_ids == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_push_when_context_missing(self) -> None:
        """无上下文 → 主动推送（群聊 chat_type=2）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await runner.send_message(
                OutboundMessageModel(chat_id="group:chat-9", text="播报")
            )
            assert client.markdown_replies == []
            target, content = client.markdown_pushes[0]
            assert target.target_id == "chat-9"
            assert target.chat_type == 2
            assert content == "播报"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_push_when_context_expired(self) -> None:
        """上下文过期 → 主动推送（不误用陈旧 req_id）。"""
        clock = _Clock()
        client = FakeWeComClient()
        runner = _runner(client=client, clock=clock, reply_window_seconds=60.0)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(req_id="cb-old"))
            clock.advance_ms(120_000)
            await runner.send_message(OutboundMessageModel(chat_id="single:u1", text="迟到"))
            assert client.markdown_replies == []
            assert len(client.markdown_pushes) == 1
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_long_text_segmented(self) -> None:
        """超平台上限文本分段：首段回复、续段推送。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        long_text = "a" * (WECOM_MARKDOWN_MAX_BYTES + 5)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(req_id="cb-long"))
            await runner.send_message(
                OutboundMessageModel(chat_id="single:u1", text=long_text)
            )
            assert len(client.markdown_replies) == 1
            assert len(client.markdown_pushes) == 1
            assert len(client.markdown_replies[0][1]) == WECOM_MARKDOWN_MAX_BYTES
            assert client.markdown_pushes[0][1] == "aaaaa"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_invalid_chat_id_rejected(self) -> None:
        """非法 chat_id grammar → VALIDATION_ERROR。"""
        runner = _runner()
        with pytest.raises(BusinessException) as exc_info:
            await runner.send_message(OutboundMessageModel(chat_id="user", text="hi"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


class TestOutboundAttachments:
    async def test_base64_image_uploaded_and_pushed(self) -> None:
        """base64 图片 → 上传（image 类型）+ 附件推送（image 消息）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        payload = b"image-bytes"
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE,
            media_type="image/png",
            base64=base64.b64encode(payload).decode("ascii"),
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="看图", attachments=[attachment])
        )
        assert client.uploaded_content == [payload]
        assert client.uploads[0][1] is WeComMediaKind.IMAGE
        assert client.uploads[0][2] == "image.png"
        assert client.media_pushes[0][1] is WeComMediaKind.IMAGE
        assert result.attachment_results[0].ok is True

    async def test_sticker_maps_to_image(self) -> None:
        """贴纸归图片素材类型（企微无独立贴纸消息）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.STICKER, base64=base64.b64encode(b"sticker").decode("ascii")
        )
        await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="表情", attachments=[attachment])
        )
        assert client.uploads[0][1] is WeComMediaKind.IMAGE
        assert client.uploads[0][2] == "image.bin"

    async def test_audio_maps_to_voice(self) -> None:
        """音频附件 → voice 素材类型。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.AUDIO,
            media_type="audio/amr",
            base64=base64.b64encode(b"voice-bytes").decode("ascii"),
        )
        await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="语音", attachments=[attachment])
        )
        assert client.uploads[0][1] is WeComMediaKind.VOICE
        assert client.media_pushes[0][1] is WeComMediaKind.VOICE

    async def test_local_path_attachment_read(self, tmp_path: Path) -> None:
        """path 源 → 本地文件读取（阻塞 I/O 经共享线程池下沉）后上传。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        file_path = tmp_path / "note.txt"
        file_path.write_bytes(b"file-content")
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.FILE, path=str(file_path)
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="文件", attachments=[attachment])
        )
        assert client.uploaded_content == [b"file-content"]
        assert result.attachment_results[0].ok is True

    async def test_url_attachment_downloaded_via_shared_client(self) -> None:
        """url 源 → 共享 HTTP 客户端下载后上传（不私建连接）。"""
        requests: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(status_code=200, content=b"remote-bytes")

        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            client = FakeWeComClient()
            runner = _runner(client=client, http_client=http_client)
            attachment = ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE, url="https://cdn.example.com/a.png"
            )
            result = await runner.send_message(
                OutboundMessageModel(chat_id="single:u1", text="远程图", attachments=[attachment])
            )
        assert requests == ["https://cdn.example.com/a.png"]
        assert client.uploaded_content == [b"remote-bytes"]
        assert result.attachment_results[0].ok is True

    async def test_url_without_http_client_fails_per_attachment(self) -> None:
        """未装配共享 HTTP 客户端 → 该附件 ok=False（文本照常投递）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, url="https://cdn.example.com/a.png"
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="远程图", attachments=[attachment])
        )
        assert result.attachment_results[0].ok is False
        assert "HTTP" in (result.attachment_results[0].error or "")
        assert client.uploads == []
        assert len(client.markdown_pushes) == 1

    async def test_upload_failure_reported_per_attachment(self) -> None:
        """上传失败 → 逐附件 ok=False 承载原因；文本与其他附件照常投递。"""
        client = FakeWeComClient()
        client.upload_error = BusinessException(
            code=ErrorCode.VALIDATION_ERROR, message="企微附件超出上限"
        )
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, base64=base64.b64encode(b"x").decode("ascii")
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="文本照发", attachments=[attachment])
        )
        assert len(client.markdown_pushes) == 1
        assert result.attachment_results[0].ok is False
        assert result.attachment_results[0].error == "企微附件超出上限"

    async def test_invalid_base64_reported_per_attachment(self) -> None:
        """非法 base64 载荷 → 逐附件失败承载（不漏到文本面）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, base64="not-base64!!"
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="带附件", attachments=[attachment])
        )
        assert result.attachment_results[0].ok is False
        assert "base64" in (result.attachment_results[0].error or "")

    async def test_multiple_attachments_partial_success(self) -> None:
        """多附件部分成功：索引对齐且成败独立。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        ok_attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, base64=base64.b64encode(b"ok").decode("ascii")
        )
        bad_attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.FILE, url="https://cdn.example.com/f.bin"
        )
        result = await runner.send_message(
            OutboundMessageModel(
                chat_id="single:u1", text="混合", attachments=[ok_attachment, bad_attachment]
            )
        )
        assert [item.index for item in result.attachment_results] == [0, 1]
        assert [item.ok for item in result.attachment_results] == [True, False]


class TestLifecycle:
    async def test_stop_propagates_to_client(self) -> None:
        """request_stop 传导到长连接客户端（supervisor 一并停机）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        task = asyncio.create_task(runner.run())
        await _wait_until(lambda: client.run_started)
        runner.request_stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert client.stop_requested is True
        assert runner.stop_requested is True


class TestDedupWindow:
    async def test_window_eviction_allows_old_msgid_again(self) -> None:
        """窗口超容按 FIFO 淘汰（淘汰后的旧 msgid 可以再次通过）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router, dedup_capacity=1)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            await client.emit_frame(_text_frame(msgid="old-1"))
            await client.emit_frame(_text_frame(msgid="new-1"))
            await client.emit_frame(_text_frame(msgid="old-1"))
            await _wait_until(lambda: len(router.received) == 3)
            assert [message.message_id for message in router.received] == [
                "old-1",
                "new-1",
                "old-1",
            ]
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestObservationSurface:
    async def test_client_property_exposes_connection_client(self) -> None:
        """client 属性暴露底层长连接客户端（装配观测面）。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        assert runner.client is client

    async def test_disconnected_event_frame_not_routed(self) -> None:
        """断开事件帧不产出回合（客户端状态机消费）。"""
        router = FakeRouter()
        client = FakeWeComClient()
        runner = _runner(client=client, router=router)
        task = asyncio.create_task(runner.run())
        try:
            await _wait_until(lambda: client.run_started)
            frame = WeComCallbackFrameModel.model_validate(
                {
                    "cmd": "aibot_event_callback",
                    "headers": {"req_id": "ev-2"},
                    "body": {
                        "msgid": "ev-msg-2",
                        "msgtype": "event",
                        "event": {"eventtype": "disconnected_event"},
                    },
                }
            )
            await client.emit_frame(frame)
            await asyncio.sleep(0.02)
            assert router.received == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestAttachmentFailureSurfaces:
    async def test_unexpected_upload_error_reported_per_attachment(self) -> None:
        """非业务异常（传输中断等）同样逐附件承载，不上抛。"""

        class _ExplodingClient(FakeWeComClient):
            async def upload_media(
                self, content: bytes, *, media_kind: WeComMediaKind, filename: str
            ) -> str:
                raise RuntimeError("fake 连接中断")

        client = _ExplodingClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, base64=base64.b64encode(b"x").decode("ascii")
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="文本", attachments=[attachment])
        )
        assert result.attachment_results[0].ok is False
        assert "fake 连接中断" in (result.attachment_results[0].error or "")

    async def test_url_http_error_reported_per_attachment(self) -> None:
        """下载非 200 → 逐附件失败承载（文本照常投递）。"""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=404, content=b"missing")

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http_client:
            client = FakeWeComClient()
            runner = _runner(client=client, http_client=http_client)
            attachment = ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE, url="https://cdn.example.com/gone.png"
            )
            result = await runner.send_message(
                OutboundMessageModel(chat_id="single:u1", text="远程", attachments=[attachment])
            )
        assert result.attachment_results[0].ok is False
        assert "404" in (result.attachment_results[0].error or "")

    async def test_url_transport_error_reported_per_attachment(self) -> None:
        """传输异常 → 逐附件失败承载。"""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fake 连接失败", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http_client:
            client = FakeWeComClient()
            runner = _runner(client=client, http_client=http_client)
            attachment = ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE, url="https://cdn.example.com/x.png"
            )
            result = await runner.send_message(
                OutboundMessageModel(chat_id="single:u1", text="远程", attachments=[attachment])
            )
        assert result.attachment_results[0].ok is False
        assert "传输异常" in (result.attachment_results[0].error or "")

    async def test_missing_local_path_reported_per_attachment(self, tmp_path: Path) -> None:
        """本地路径不存在 → 逐附件失败承载。"""
        client = FakeWeComClient()
        runner = _runner(client=client)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.FILE, path=str(tmp_path / "absent.bin")
        )
        result = await runner.send_message(
            OutboundMessageModel(chat_id="single:u1", text="文件", attachments=[attachment])
        )
        assert result.attachment_results[0].ok is False
        assert "读取失败" in (result.attachment_results[0].error or "")
