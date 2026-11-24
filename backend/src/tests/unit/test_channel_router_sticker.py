"""通道路由器表情收发桥测试——入站收录 / 出站附发 / 失败隔离 / 缺省零影响。

覆盖：入站 sticker 附件触发收录钩子（人格上下文已绑定）；本轮入站带表情时
随文本附发一张表情；入站无表情不发；收录失败 / 取表情失败均不阻断文本
回复；未装配钩子时出站附件为空（既有语义零变化）。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
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
from packages.harness.chat.persona_rotation_models import GroupPersonaSettings
from packages.harness.core import time_utils
from packages.harness.core.context import persona_id_var
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO, PersonaStatus
from tests.router_fakes import (
    BASE_MS,
    empty_session_factory,
    reply_index_message_ids,
)
from tests.router_fakes import (
    FakeAdapter as GroupFakeAdapter,
)
from tests.router_fakes import (
    FakeChatService as GroupFakeChatService,
)

_PERSONA_ID = 7


class FakeChatService:
    """fake ChatService（记录回复调用）。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.reply_calls: list[str] = []

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
        self.reply_calls.append(user_text)
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
    chat: FakeChatService | None = None,
) -> ChannelRouter:
    """装配路由器（注入 fake 依赖与可选表情钩子）。"""
    service = chat if chat is not None else FakeChatService()
    router = ChannelRouter(
        chat_service_factory=lambda: service,
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


async def test_empty_text_sticker_ingested_without_generation_or_fallback() -> None:
    """纯表情空文本：收录 1 次、生成 0 次、无兜底、空投递结果。"""
    adapter = FakeAdapter()
    chat = FakeChatService()
    ingested: list[int] = []

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        ingested.append(len(attachments))

    router = _router(adapter=adapter, ingest=ingest, chat=chat)
    result = await router.handle_inbound(_sticker(text=""))
    assert ingested == [1]
    assert chat.reply_calls == []
    assert adapter.sent == []
    assert result.message_ids == []
    assert router.no_user_text_skip_count == 1


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


def _record(persona_id: int, *, is_primary: bool = False) -> PersonaRecordDTO:
    """构造启用人格记录（群聊表情归属用例直供）。"""
    return PersonaRecordDTO(
        id=persona_id,
        name=f"persona-{persona_id}",
        display_name=f"人格{persona_id}",
        status=PersonaStatus.ACTIVE,
        is_primary=is_primary,
        card=CharaCardModel(name=f"人格{persona_id}"),
        created_at=BASE_MS,
        updated_at=BASE_MS,
    )


class _StickerPersonaService:
    """fake 人格注册表（固定启用人格集合——群聊表情用例）。"""

    def __init__(self, records: list[PersonaRecordDTO]) -> None:
        """初始化。"""
        self.records = records

    async def list_addressable(self, session: object) -> list[PersonaRecordDTO]:
        """返回固定启用人格集合。"""
        return list(self.records)


def _group_inbound(*, message_id: str = "42") -> InboundMessageModel:
    """构造群聊带 sticker 附件的入站消息。"""
    message = _sticker()
    message.chat_id = "group:10086"
    message.message_id = message_id
    message.is_group_chat = True
    return message


class _SteppingClock:
    """每次读数推进固定步长的时钟（暴露同入站多次读数导致的选择漂移）。"""

    def __init__(self, start_ms: int = BASE_MS, step_ms: int = 1) -> None:
        """初始化。

        Args:
            start_ms: 起始时刻（epoch 毫秒）。
            step_ms: 每次读数推进的毫秒数。
        """
        self.current = start_ms
        self.step_ms = step_ms
        self.calls = 0

    def __call__(self) -> int:
        """返回当前时刻并推进（记录读数次数）。"""
        self.calls += 1
        value = self.current
        self.current += self.step_ms
        return value

    def reset(self, start_ms: int = BASE_MS) -> None:
        """回到指定时刻并清零读数计数（逐入站独立观测）。

        Args:
            start_ms: 起始时刻（epoch 毫秒）。
        """
        self.current = start_ms
        self.calls = 0


def _group_router(
    *,
    adapter: GroupFakeAdapter,
    service: GroupFakeChatService,
    ingest: Any,
    max_responders: int = 2,
    cooldown_ms: int = 0,
    clock: Callable[[], int] | None = None,
) -> ChannelRouter:
    """装配群聊多人格路由器（注入 fake 依赖与表情收录钩子）。

    Args:
        adapter: fake 通道适配器。
        service: fake ChatService。
        ingest: 表情收录钩子。
        max_responders: 单轮应答人格上限。
        cooldown_ms: 人格应答冷却毫秒（0 = 不设冷却）。
        clock: 注入时钟（None = 真实时钟）。

    Returns:
        装配好的群聊路由器。
    """
    router = ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=empty_session_factory,
        group_settings=GroupPersonaSettings(
            enabled=True, max_responders=max_responders, cooldown_ms=cooldown_ms
        ),
        persona_service=_StickerPersonaService(
            [_record(1, is_primary=True), _record(2), _record(3)]
        ),
        sticker_ingest_hook=ingest,
        clock=clock if clock is not None else time_utils.now_ms,
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


async def test_group_sticker_ingested_into_every_responder_library() -> None:
    """群聊多人格：本轮两位应答人格各收录一份，第三人称不受影响。"""
    adapter = GroupFakeAdapter(message_ids=reply_index_message_ids)
    service = GroupFakeChatService(echo_style="persona")
    calls: list[tuple[str, str]] = []

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        calls.append((persona_id_var.get(), attachments[0].base64))

    router = _group_router(adapter=adapter, service=service, ingest=ingest)
    result = await router.handle_inbound(_group_inbound())
    assert result.message_ids == ["out:1", "out:2"]
    assert [persona for persona, _ in calls] == ["1", "2"]
    assert {payload for _, payload in calls} == {_sticker().attachments[0].base64}
    assert all(call["persona_id"] in {1, 2} for call in service.reply_calls)


async def test_group_sticker_same_hash_deduped_independently_per_library() -> None:
    """同哈希重复入站：收录调用逐次到达各人格库（非自去重），各库独立去重。"""
    adapter = GroupFakeAdapter(message_ids=reply_index_message_ids)
    service = GroupFakeChatService(echo_style="persona")
    calls: list[tuple[str, str]] = []
    libraries: dict[str, list[str]] = {}

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        persona = persona_id_var.get()
        digest = hashlib.sha256(attachments[0].base64.encode("ascii")).hexdigest()
        calls.append((persona, digest))
        libraries.setdefault(persona, []).append(digest)

    router = _group_router(adapter=adapter, service=service, ingest=ingest)
    await router.handle_inbound(_group_inbound(message_id="1"))
    await router.handle_inbound(_group_inbound(message_id="2"))
    assert len(calls) == 4
    assert {"1", "2", "3"} <= set(libraries)
    assert any(digests.count(digests[0]) == 2 for digests in libraries.values())
    assert all(len(set(digests)) == 1 for digests in libraries.values())


async def test_group_sticker_cooling_boundary_single_clock_read_keeps_sets_aligned() -> None:
    """冷却边界：单次时钟读数下收录集与应答集一致（不因二次读数漂移）。"""
    adapter = GroupFakeAdapter(message_ids=reply_index_message_ids)
    service = GroupFakeChatService(echo_style="persona")
    clock = _SteppingClock()
    ingested: list[str] = []

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        ingested.append(persona_id_var.get())

    router = _group_router(
        adapter=adapter,
        service=service,
        ingest=ingest,
        cooldown_ms=1,
        clock=clock,
    )
    await router.handle_inbound(_group_inbound(message_id="1"))
    assert ingested == ["1", "2"]
    ingested.clear()
    replied_before = len(service.reply_calls)
    clock.reset()
    result = await router.handle_inbound(_group_inbound(message_id="2"))
    replied = [
        str(call["persona_id"]) for call in service.reply_calls[replied_before:]
    ]
    assert clock.calls == 1
    assert result.message_ids != []
    assert replied == ["3"]
    assert ingested == replied


async def test_group_sticker_ingest_failure_isolated_from_reply() -> None:
    """单人格收录失败：其余人格照常收录，两位应答人格文本回复照常投递。"""
    adapter = GroupFakeAdapter(message_ids=reply_index_message_ids)
    service = GroupFakeChatService(echo_style="persona")
    ingested: list[str] = []

    async def ingest(attachments: list[ChannelMessageAttachment]) -> None:
        persona = persona_id_var.get()
        ingested.append(persona)
        if persona == "1":
            raise RuntimeError("persona-1 library down")

    router = _group_router(adapter=adapter, service=service, ingest=ingest)
    result = await router.handle_inbound(_group_inbound())
    assert result.message_ids == ["out:1", "out:2"]
    assert ingested == ["1", "2"]
    assert [message.text for message in adapter.sent] == [
        "persona1:看看这个",
        "persona2:看看这个",
    ]
