"""通道路由器表情收发桥测试——入站收录 / 出站附发 / 失败隔离 / 缺省零影响。

覆盖：入站 sticker 附件触发收录钩子（人格上下文已绑定）；本轮入站带表情时
随文本附发一张表情；入站无表情不发；收录失败 / 取表情失败均不阻断文本
回复；未装配钩子时出站附件为空（既有语义零变化）。
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.core.context import persona_id_var
from packages.harness.persona.chara_card import CharaCardModel

_PERSONA_ID = 7


class FakeChatService:
    """fake ChatService（记录回复调用）。"""

    async def create_session(
        self,
        *,
        persona: object | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建。"""
        return ChatSessionStateModel(
            persona_id=_PERSONA_ID,
            persona_name="小鱼",
            card=CharaCardModel(name="小鱼"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance: Any = None
    ) -> str:
        """fake 回复。"""
        return f"echo:{user_text}"


class FakeAdapter:
    """fake 适配器（记录出站消息）。"""

    def __init__(self) -> None:
        self.sent: list[OutboundMessageModel] = []

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """记录出站并返回成功结果。"""
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=[f"out:{len(self.sent)}"],
            attachment_results=[],
        )


@asynccontextmanager
async def _session_factory() -> Any:
    """fake 数据库会话工厂。"""
    yield object()


def _sticker(text: str = "看看这个") -> InboundMessageModel:
    """构造带 sticker 附件的入站消息。"""
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id="42",
        sender_id="7",
        text=text,
        attachments=[
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.STICKER,
                media_type="image/png",
                base64=base64.b64encode(b"sticker").decode("ascii"),
                caption="看看这个",
            )
        ],
    )


def _out_sticker() -> ChannelMessageAttachment:
    """构造出站表情附件。"""
    return ChannelMessageAttachment(
        kind=ChannelAttachmentKind.STICKER,
        media_type="image/png",
        base64=base64.b64encode(b"reply-sticker").decode("ascii"),
    )


def _router(
    *,
    adapter: FakeAdapter,
    ingest: Any = None,
    reply: Any = None,
) -> ChannelRouter:
    """装配路由器（注入 fake 依赖与可选表情钩子）。"""
    router = ChannelRouter(
        chat_service_factory=lambda: FakeChatService(),
        session_factory=_session_factory,
        sticker_ingest_hook=ingest,
        sticker_reply_hook=reply,
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


async def test_inbound_sticker_ingested_with_persona_context() -> None:
    """入站 sticker：收录钩子在应答人格上下文内调用。"""
    adapter = FakeAdapter()
    captured: dict[str, Any] = {}

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        captured["count"] = len(attachments)
        captured["persona"] = persona_id_var.get()

    router = _router(adapter=adapter, ingest=ingest, reply=lambda: _return(_out_sticker()))
    result = await router.handle_inbound(_sticker())
    assert result.message_ids == ["out:1"]
    assert captured == {"count": 1, "persona": str(_PERSONA_ID)}


async def _return(value: ChannelMessageAttachment | None) -> ChannelMessageAttachment | None:
    """异步返回固定值（供 lambda 以外的手写钩子复用）。"""
    return value


async def test_outbound_sticker_attached_when_inbound_has_sticker() -> None:
    """入站带表情且桥返回素材：出站消息携带一张 sticker 附件。"""
    adapter = FakeAdapter()
    router = _router(adapter=adapter, reply=lambda: _return(_out_sticker()))
    await router.handle_inbound(_sticker())
    assert len(adapter.sent) == 1
    attachments = adapter.sent[0].attachments
    assert len(attachments) == 1
    assert attachments[0].kind is ChannelAttachmentKind.STICKER
    assert attachments[0].base64 == base64.b64encode(b"reply-sticker").decode("ascii")


async def test_no_outbound_sticker_without_inbound_sticker() -> None:
    """入站无表情：取表情钩子不被调用，出站零附件（既有形态）。"""
    adapter = FakeAdapter()
    calls: list[int] = []

    async def reply() -> ChannelMessageAttachment | None:
        calls.append(1)
        return _out_sticker()

    router = _router(adapter=adapter, reply=reply)
    plain = _sticker()
    plain.attachments = []
    await router.handle_inbound(plain)
    assert calls == []
    assert adapter.sent[0].attachments == []


async def test_hooks_absent_keeps_legacy_shape() -> None:
    """未装配钩子：sticker 入站照常回复且出站零附件。"""
    adapter = FakeAdapter()
    router = _router(adapter=adapter)
    await router.handle_inbound(_sticker())
    assert adapter.sent[0].attachments == []
    assert adapter.sent[0].text == "echo:看看这个"


async def test_ingest_and_reply_failures_do_not_block_reply() -> None:
    """收录失败 / 取表情失败：文本回复照常投递（表情是装饰面）。"""
    adapter = FakeAdapter()

    async def broken_ingest(attachments: list[ChannelMessageAttachment]) -> None:
        raise RuntimeError("ingest down")

    async def broken_reply() -> ChannelMessageAttachment | None:
        raise RuntimeError("reply down")

    router = _router(adapter=adapter, ingest=broken_ingest, reply=broken_reply)
    result = await router.handle_inbound(_sticker())
    assert result.message_ids == ["out:1"]
    assert adapter.sent[0].attachments == []
    assert adapter.sent[0].text == "echo:看看这个"
