"""channels 域 Model Classes 测试——值域 / 校验 / 默认值 / 身份键。

零真实配置 / 零真实网络：纯模型构造断言。

覆盖矩阵（v1 + 协议 v2）：

    - ChannelType 值域（telegram + qq）；
    - 入站 / 出站 / 投递结果 v1 字段与校验；
    - v2 附件模型：kind 值域 / 恰一源校验矩阵（零源 / 双源 / 空串
      等同缺省）/ 默认值 / base64 容量指引常量；
    - v2 字段缺省：入站 attachments / synthetic / silent；出站
      attachments / silent；投递结果 attachment_results（v1 形态
      保持——缺省即 v1）；
    - 逐附件投递结果形态（index / ok / error / provider_message_id）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.channels.channel_models import (
    ATTACHMENT_BASE64_SIZE_GUIDE_BYTES,
    AttachmentDeliveryResult,
    ChannelAttachmentKind,
    ChannelContextModel,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter


async def test_channel_type_value_domain() -> None:
    """ChannelType：值域 = telegram + qq（StrEnum 字符串兼容——枚举为
    多通道扩展点，P4 chunk 4 加入 qq）。"""
    assert ChannelType.TELEGRAM.value == "telegram"
    assert ChannelType.TELEGRAM == "telegram"
    assert ChannelType.QQ.value == "qq"
    assert ChannelType.QQ == "qq"
    assert [member.value for member in ChannelType] == ["telegram", "qq"]


async def test_inbound_message_defaults_and_fields() -> None:
    """InboundMessageModel：全字段构造 + ts_ms 自动填充（epoch 毫秒）。"""
    message = InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id="42",
        sender_id="7",
        text="你好",
    )
    assert message.channel is ChannelType.TELEGRAM
    assert message.chat_id == "10086"
    assert message.message_id == "42"
    assert message.sender_id == "7"
    assert message.text == "你好"
    assert isinstance(message.ts_ms, int)
    assert message.ts_ms > 0


async def test_inbound_message_rejects_empty_fields() -> None:
    """InboundMessageModel：空 chat_id / 空文本拒绝（边界校验）。"""
    with pytest.raises(ValidationError):
        InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="",
            message_id="42",
            sender_id="7",
            text="你好",
        )
    with pytest.raises(ValidationError):
        InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_id="42",
            sender_id="7",
            text="",
        )


async def test_outbound_message_reply_to_nullable() -> None:
    """OutboundMessageModel：reply_to_message_id 可空（兜底消息不引用）。"""
    message = OutboundMessageModel(chat_id="10086", text="收到")
    assert message.reply_to_message_id is None
    referenced = OutboundMessageModel(chat_id="10086", text="收到", reply_to_message_id="42")
    assert referenced.reply_to_message_id == "42"
    with pytest.raises(ValidationError):
        OutboundMessageModel(chat_id="10086", text="")


async def test_channel_context_and_session_key() -> None:
    """ChannelContextModel + 会话身份键（channel:chat_id——路由器会话维度）。"""
    context = ChannelContextModel(channel=ChannelType.TELEGRAM, chat_id="10086")
    assert ChannelRouter.session_key(context) == "channel:telegram:10086"


async def test_delivery_result_defaults() -> None:
    """ChannelDeliveryResultModel：message_ids 缺省空列表（未送达形态）。"""
    result = ChannelDeliveryResultModel(channel=ChannelType.TELEGRAM, chat_id="10086")
    assert result.message_ids == []
    delivered = ChannelDeliveryResultModel(
        channel=ChannelType.TELEGRAM, chat_id="10086", message_ids=["11", "12"]
    )
    assert delivered.message_ids == ["11", "12"]


class TestAttachmentModelV2:
    async def test_attachment_kind_value_domain(self) -> None:
        """ChannelAttachmentKind：值域 = image / audio / file / sticker。"""
        assert [member.value for member in ChannelAttachmentKind] == [
            "image",
            "audio",
            "file",
            "sticker",
        ]
        assert ChannelAttachmentKind.IMAGE == "image"
        assert ChannelAttachmentKind.AUDIO == "audio"
        assert ChannelAttachmentKind.FILE == "file"
        assert ChannelAttachmentKind.STICKER == "sticker"

    async def test_attachment_single_source_accepted(self) -> None:
        """恰一源接受：url / base64 / path 三形态各自独立构造成功。"""
        for field, value in (
            ("url", "https://example.com/x.jpg"),
            ("base64", "aGVsbG8="),
            ("path", "tg-file://AgACAgIAAx0"),
        ):
            attachment = ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE, **{field: value}
            )
            assert getattr(attachment, field) == value

    async def test_attachment_zero_sources_rejected(self) -> None:
        """零源拒绝：三源皆缺 = 校验失败（恰一源 required）。"""
        with pytest.raises(ValidationError, match="媒体源"):
            ChannelMessageAttachment(kind=ChannelAttachmentKind.IMAGE)

    async def test_attachment_multiple_sources_rejected(self) -> None:
        """双源拒绝：url + base64 / url + path / base64 + path 均失败。"""
        with pytest.raises(ValidationError, match="媒体源"):
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE,
                url="https://example.com/x.jpg",
                base64="aGVsbG8=",
            )
        with pytest.raises(ValidationError, match="媒体源"):
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.AUDIO,
                url="https://example.com/x.mp3",
                path="file:///tmp/x.mp3",
            )
        with pytest.raises(ValidationError, match="媒体源"):
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.FILE,
                base64="aGVsbG8=",
                path="tg-file://AgAC",
            )

    async def test_attachment_empty_string_counts_as_absent(self) -> None:
        """空串等同缺省：url="" 且无他源 = 零源拒绝（不静默放行空引用）。"""
        with pytest.raises(ValidationError, match="媒体源"):
            ChannelMessageAttachment(kind=ChannelAttachmentKind.IMAGE, url="")

    async def test_attachment_optional_fields_default_none(self) -> None:
        """可选字段缺省：media_type / caption 为 None。"""
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.STICKER, path="tg-file://CAACAgIAAx0"
        )
        assert attachment.media_type is None
        assert attachment.caption is None

    async def test_attachment_metadata_fields(self) -> None:
        """线索字段承载：media_type / caption 构造透传。"""
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.AUDIO,
            media_type="audio/ogg",
            base64="aGVsbG8=",
            caption="今日心情",
        )
        assert attachment.media_type == "audio/ogg"
        assert attachment.caption == "今日心情"

    async def test_base64_size_guidance_constant(self) -> None:
        """base64 容量指引常量：10MB（文档化指引——文档与模型 docstring 同源）。"""
        assert ATTACHMENT_BASE64_SIZE_GUIDE_BYTES == 10 * 1024 * 1024
        docstring = ChannelMessageAttachment.__doc__ or ""
        assert "10MB" in docstring


class TestMessageFieldsV2:
    async def test_inbound_v2_defaults(self) -> None:
        """入站 v2 字段缺省：attachments=[] / synthetic=False / silent=False
        （缺省即 v1 纯文本形态——向后兼容证明面）。"""
        message = InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_id="42",
            sender_id="7",
            text="你好",
        )
        assert message.attachments == []
        assert message.synthetic is False
        assert message.silent is False

    async def test_inbound_v2_fields_settable(self) -> None:
        """入站 v2 字段可设：附件列表 / 合成事件标记 / 无通知标记。"""
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, path="tg-file://AgAC"
        )
        message = InboundMessageModel(
            channel=ChannelType.QQ,
            chat_id="private:10086",
            message_id="-400500600",
            sender_id="10086",
            text="看看",
            attachments=[attachment],
            synthetic=True,
            silent=True,
        )
        assert message.attachments == [attachment]
        assert message.synthetic is True
        assert message.silent is True

    async def test_outbound_v2_defaults(self) -> None:
        """出站 v2 字段缺省：attachments=[] / silent=False（缺省即 v1）。"""
        message = OutboundMessageModel(chat_id="10086", text="收到")
        assert message.attachments == []
        assert message.silent is False

    async def test_outbound_v2_fields_settable(self) -> None:
        """出站 v2 字段可设：附件列表 + 免通知请求。"""
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.AUDIO, base64="aGVsbG8=", media_type="audio/ogg"
        )
        message = OutboundMessageModel(
            chat_id="10086", text="语音来了", attachments=[attachment], silent=True
        )
        assert message.attachments == [attachment]
        assert message.silent is True

    async def test_delivery_result_attachment_results_default_empty(self) -> None:
        """投递结果 v2 字段缺省：attachment_results=[]（缺省即 v1）。"""
        result = ChannelDeliveryResultModel(channel=ChannelType.TELEGRAM, chat_id="10086")
        assert result.attachment_results == []

    async def test_attachment_delivery_result_shape(self) -> None:
        """逐附件投递结果：成功 / 部分失败两形态字段承载。"""
        ok_result = AttachmentDeliveryResult(index=0, ok=True, provider_message_id="901")
        assert ok_result.index == 0
        assert ok_result.ok is True
        assert ok_result.error is None
        assert ok_result.provider_message_id == "901"
        failed_result = AttachmentDeliveryResult(index=1, ok=False, error="平台拒绝")
        assert failed_result.error == "平台拒绝"
        assert failed_result.provider_message_id is None
        with pytest.raises(ValidationError):
            AttachmentDeliveryResult(index=-1, ok=True)

    async def test_delivery_result_with_attachment_results(self) -> None:
        """投递结果聚合形态：message_ids + 逐附件结果并存（部分成功可见）。"""
        result = ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_ids=["900"],
            attachment_results=[
                AttachmentDeliveryResult(index=0, ok=True, provider_message_id="901"),
                AttachmentDeliveryResult(index=1, ok=False, error="附件投递失败"),
            ],
        )
        assert len(result.attachment_results) == 2
        assert result.attachment_results[1].ok is False
