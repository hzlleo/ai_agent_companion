"""qq 通道适配器测试——NapCatQQAdapter（fake 客户端零真实网络）。

覆盖矩阵：

    - chat_id grammar 双形态（private / group → 对应 NapCat 动作 + 目标
      ID 解析 + 投递结果归一）；
    - 投递映射（ok 响应 message_id 归一字符串；wire 未回传 message_id
      → 空 message_ids）；
    - grammar 非法（未知前缀 / 非数字 / 非正整数）→ VALIDATION_ERROR；
    - 客户端失败上抛（适配器协议契约——路由器兜底隔离）；
    - 协议 v2：附件段数组发射（文本段先 + 媒体段后 / base64 包装 /
      url 直传）；file 类附件逐附件失败承载（OneBot v11 无文件段——
      文档化边界）；silent 优雅忽略（no-op）；逐附件结果聚合（共享
      平台消息 ID + 附件序对齐）；段发送失败上抛（v1 语义）。
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    OutboundMessageModel,
)
from packages.harness.channels.qq.qq_adapter import NapCatQQAdapter
from packages.harness.channels.qq.qq_models import (
    QQ_FILE_ATTACHMENT_UNSUPPORTED_MESSAGE,
    Ob11ApiResponseModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode


class FakeNapCatClient:
    """fake NapCatOneBotClient——记录调用形状 / 可编程失败。"""

    def __init__(
        self,
        *,
        response: Ob11ApiResponseModel | None = None,
        send_error: Exception | None = None,
    ) -> None:
        """初始化 fake 客户端。

        Args:
            response: 脚本化动作响应（None = 缺省 message_id 形态）。
            send_error: 发送故障注入（None = 恒成功）。
        """
        self.private_calls: list[dict[str, Any]] = []
        self.group_calls: list[dict[str, Any]] = []
        self.private_segment_calls: list[dict[str, Any]] = []
        self.group_segment_calls: list[dict[str, Any]] = []
        self._response = response or Ob11ApiResponseModel(
            status="ok", retcode=0, data={"message_id": -100200300}
        )
        self._send_error = send_error

    async def send_private_msg(self, user_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 私聊发送（记录调用；可编程抛错）。"""
        self.private_calls.append({"user_id": user_id, "text": text})
        if self._send_error is not None:
            raise self._send_error
        return self._response

    async def send_group_msg(self, group_id: int, text: str) -> Ob11ApiResponseModel:
        """fake 群聊发送（记录调用；可编程抛错）。"""
        self.group_calls.append({"group_id": group_id, "text": text})
        if self._send_error is not None:
            raise self._send_error
        return self._response

    async def send_private_msg_segments(self, user_id: int, segments: Any) -> Ob11ApiResponseModel:
        """fake v2 私聊段数组发送（记录段 wire 形态；可编程抛错）。"""
        self.private_segment_calls.append(
            {"user_id": user_id, "segments": [segment.model_dump() for segment in segments]}
        )
        if self._send_error is not None:
            raise self._send_error
        return self._response

    async def send_group_msg_segments(self, group_id: int, segments: Any) -> Ob11ApiResponseModel:
        """fake v2 群聊段数组发送（记录段 wire 形态；可编程抛错）。"""
        self.group_segment_calls.append(
            {"group_id": group_id, "segments": [segment.model_dump() for segment in segments]}
        )
        if self._send_error is not None:
            raise self._send_error
        return self._response


def _outbound(chat_id: str, text: str = "你好") -> OutboundMessageModel:
    """构造出站消息（测试直供形态）。

    Args:
        chat_id: 目标会话 ID（grammar 形态）。
        text: 回复文本。

    Returns:
        :class:`OutboundMessageModel` 实例。
    """
    return OutboundMessageModel(chat_id=chat_id, text=text)


class TestChatIdGrammar:
    async def test_private_form_routes_send_private_msg(self) -> None:
        """private grammar：send_private_msg(user_id) + 投递结果归一。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("private:10086"))
        assert client.private_calls == [{"user_id": 10086, "text": "你好"}]
        assert client.group_calls == []
        assert result.channel.value == "qq"
        assert result.chat_id == "private:10086"
        assert result.message_ids == ["-100200300"]

    async def test_group_form_routes_send_group_msg(self) -> None:
        """group grammar：send_group_msg(group_id) + 投递结果归一。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("group:67890", text="收到"))
        assert client.group_calls == [{"group_id": 67890, "text": "收到"}]
        assert client.private_calls == []
        assert result.channel.value == "qq"
        assert result.chat_id == "group:67890"
        assert result.message_ids == ["-100200300"]

    async def test_client_face_exposed(self) -> None:
        """装配观测面：client 属性透传底层客户端（启动校验消费面）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        assert adapter.client is client


class TestDeliveryMapping:
    async def test_missing_message_id_yields_empty_ids(self) -> None:
        """wire 未回传 message_id：送达成功但 message_ids 空（无引用凭据）。"""
        client = FakeNapCatClient(response=Ob11ApiResponseModel(status="ok", retcode=0, data=None))
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("private:10086"))
        assert result.message_ids == []

    async def test_string_message_id_passthrough(self) -> None:
        """wire 回传字符串 message_id：原样承载（实现兼容形态）。"""
        client = FakeNapCatClient(
            response=Ob11ApiResponseModel(status="ok", retcode=0, data={"message_id": "-777"})
        )
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(_outbound("group:1"))
        assert result.message_ids == ["-777"]


class TestGrammarValidation:
    async def test_unknown_prefix_rejected(self) -> None:
        """未知前缀：VALIDATION_ERROR（grammar 契约面）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("channel:10086"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_missing_prefix_rejected(self) -> None:
        """无前缀裸 ID：VALIDATION_ERROR（grammar 契约面）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("10086"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_numeric_target_rejected(self) -> None:
        """非数字目标 ID：VALIDATION_ERROR（QQ 号 / 群号形态契约）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("private:abc"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_positive_target_rejected(self) -> None:
        """非正整数目标 ID：VALIDATION_ERROR（QQ 号 / 群号为正整数）。"""
        adapter = NapCatQQAdapter(client=FakeNapCatClient())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("group:0"))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


class TestFailurePropagation:
    async def test_client_failure_propagates_for_router_isolation(self) -> None:
        """客户端失败上抛（适配器协议契约——路由器 handle_inbound 兜底）。"""
        client = FakeNapCatClient(
            send_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="平台发送失败（测试注入）"
            )
        )
        adapter = NapCatQQAdapter(client=client)
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(_outbound("private:10086"))
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


class TestAttachmentSegmentsV2:
    async def test_image_attachment_emits_segment_array(self) -> None:
        """image + url 附件：段数组形态（文本段先 + image 段后，file=url 直传）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(
            OutboundMessageModel(
                chat_id="private:10086",
                text="看看",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE,
                        media_type="image/jpeg",
                        url="https://example.com/cat.jpg",
                    )
                ],
            )
        )
        assert client.private_calls == []
        assert len(client.private_segment_calls) == 1
        call = client.private_segment_calls[0]
        assert call["user_id"] == 10086
        assert call["segments"] == [
            {"type": "text", "data": {"text": "看看"}},
            {"type": "image", "data": {"file": "https://example.com/cat.jpg"}},
        ]
        assert result.message_ids == ["-100200300"]
        assert [outcome.model_dump() for outcome in result.attachment_results] == [
            {
                "index": 0,
                "ok": True,
                "error": None,
                "provider_message_id": "-100200300",
            }
        ]

    async def test_base64_attachment_wraps_scheme(self) -> None:
        """base64 附件：段数据 file 包装 ``base64://<载荷>`` 形态。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        await adapter.send_message(
            OutboundMessageModel(
                chat_id="group:67890",
                text="群里看",
                attachments=[
                    ChannelMessageAttachment(kind=ChannelAttachmentKind.AUDIO, base64="aGVsbG8=")
                ],
            )
        )
        assert len(client.group_segment_calls) == 1
        segments = client.group_segment_calls[0]["segments"]
        assert segments[0] == {"type": "text", "data": {"text": "群里看"}}
        assert segments[1] == {"type": "record", "data": {"file": "base64://aGVsbG8="}}

    async def test_sticker_maps_to_image_segment(self) -> None:
        """sticker 附件：QQ 无独立贴纸段——按 image 段发送（文档化映射）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        await adapter.send_message(
            OutboundMessageModel(
                chat_id="private:10086",
                text="表情",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.STICKER, path="file:///stickers/x.webp"
                    )
                ],
            )
        )
        segments = client.private_segment_calls[0]["segments"]
        assert segments[1] == {
            "type": "image",
            "data": {"file": "file:///stickers/x.webp"},
        }

    async def test_file_attachment_surfaces_unsupported_result(self) -> None:
        """file 附件：OneBot v11 无文件段——ok=False 文档化失败，文本与其余
        附件照常投递（部分成功聚合面）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(
            OutboundMessageModel(
                chat_id="private:10086",
                text="两附件",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, url="https://x/1.jpg"
                    ),
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.FILE,
                        media_type="application/pdf",
                        base64="aGVsbG8=",
                    ),
                ],
            )
        )
        segments = client.private_segment_calls[0]["segments"]
        assert [segment["type"] for segment in segments] == ["text", "image"]
        assert [outcome.ok for outcome in result.attachment_results] == [True, False]
        assert result.attachment_results[1].error == QQ_FILE_ATTACHMENT_UNSUPPORTED_MESSAGE
        assert result.attachment_results[0].provider_message_id == "-100200300"
        assert result.message_ids == ["-100200300"]

    async def test_all_attachments_unsupported_still_delivers_text(self) -> None:
        """全部附件不支持：文本段独立成段数组仍投递（文本可达性优先）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(
            OutboundMessageModel(
                chat_id="group:67890",
                text="只有文件",
                attachments=[
                    ChannelMessageAttachment(kind=ChannelAttachmentKind.FILE, base64="aGVsbG8=")
                ],
            )
        )
        segments = client.group_segment_calls[0]["segments"]
        assert segments == [{"type": "text", "data": {"text": "只有文件"}}]
        assert result.message_ids == ["-100200300"]
        assert result.attachment_results[0].ok is False

    async def test_silent_is_graceful_noop(self) -> None:
        """silent=True：QQ 无通知概念——优雅忽略（正常投递，不抛不改形）。"""
        client = FakeNapCatClient()
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(
            OutboundMessageModel(chat_id="private:10086", text="悄悄说", silent=True)
        )
        assert client.private_calls == [{"user_id": 10086, "text": "悄悄说"}]
        assert client.private_segment_calls == []
        assert result.message_ids == ["-100200300"]
        assert result.attachment_results == []

    async def test_segment_send_failure_propagates(self) -> None:
        """段数组发送失败：整条上抛（v1 语义——单消息内无部分失败面）。"""
        client = FakeNapCatClient(
            send_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="段发送失败（测试注入）"
            )
        )
        adapter = NapCatQQAdapter(client=client)
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(
                OutboundMessageModel(
                    chat_id="private:10086",
                    text="必失败",
                    attachments=[
                        ChannelMessageAttachment(
                            kind=ChannelAttachmentKind.IMAGE, url="https://x/1.jpg"
                        )
                    ],
                )
            )
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_no_message_id_yields_none_provider_id(self) -> None:
        """wire 未回传 message_id：附件送达成功但凭据 None（无引用凭据形态）。"""
        client = FakeNapCatClient(response=Ob11ApiResponseModel(status="ok", retcode=0, data=None))
        adapter = NapCatQQAdapter(client=client)
        result = await adapter.send_message(
            OutboundMessageModel(
                chat_id="private:10086",
                text="看",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.IMAGE, url="https://x/1.jpg"
                    )
                ],
            )
        )
        assert result.message_ids == []
        assert result.attachment_results[0].ok is True
        assert result.attachment_results[0].provider_message_id is None
