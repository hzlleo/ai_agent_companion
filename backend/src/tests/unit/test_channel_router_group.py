"""通道路由器群聊多人格测试——选择 / 会话归属 / 冷却 / 失败隔离。

覆盖：功能关闭时群聊仍单人格应答；启用人格轮转（每条消息一个应答者）；
多人格接续（上限 2——逐人格独立会话与出站）；冷却排除与全冷却兜底；
单聊不参与轮转；单人格失败不影响其余人格；空候选回退主人格路径；
群聊来源分级保持 peer。全部 fake 依赖（零真实网络 / 注入时钟）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.persona_rotation_models import GroupPersonaSettings
from packages.harness.core.context import persona_id_var
from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO, PersonaStatus

_BASE_MS = 1_700_000_000_000


class FakeClock:
    """假时钟（epoch 毫秒——测试手动推进）。"""

    def __init__(self, start_ms: int = _BASE_MS) -> None:
        """初始化。"""
        self.now = start_ms

    def __call__(self) -> int:
        """返回当前假时刻。"""
        return self.now


def _record(persona_id: int, *, is_primary: bool = False) -> PersonaRecordDTO:
    """构造人格记录（测试直供）。"""
    return PersonaRecordDTO(
        id=persona_id,
        name=f"persona-{persona_id}",
        display_name=f"人格{persona_id}",
        status=PersonaStatus.ACTIVE,
        is_primary=is_primary,
        card=CharaCardModel(name=f"人格{persona_id}"),
        created_at=_BASE_MS,
        updated_at=_BASE_MS,
    )


class FakePersonaService:
    """fake 人格注册表（固定启用人格集合）。"""

    def __init__(self, records: list[PersonaRecordDTO]) -> None:
        """初始化。"""
        self.records = records
        self.list_calls = 0

    async def list_addressable(self, session: object) -> list[PersonaRecordDTO]:
        """返回固定启用人格集合。"""
        self.list_calls += 1
        return list(self.records)


class FakeChatService:
    """fake ChatService（按人格归属建会话 / 可编程人格级失败）。"""

    def __init__(self, *, failing_persona_ids: set[int] | None = None) -> None:
        """初始化。"""
        self.create_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.failing_persona_ids = failing_persona_ids or set()

    async def create_session(
        self,
        *,
        persona: PersonaRecordDTO | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建（人格归属来自入参记录）。"""
        persona_id = persona.id if persona is not None else 9
        self.create_calls.append(
            {"persona_id": persona_id, "db_session": db_session, "title": title}
        )
        return ChatSessionStateModel(
            persona_id=persona_id,
            persona_name=f"persona-{persona_id}",
            card=CharaCardModel(name=f"人格{persona_id}"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance: Any = None
    ) -> str:
        """fake 回复（人格级失败可编程）。"""
        self.reply_calls.append(
            {
                "persona_id": state.persona_id,
                "persona_ctx": persona_id_var.get(),
                "text": user_text,
                "sender_provenance": sender_provenance,
            }
        )
        if state.persona_id in self.failing_persona_ids:
            raise RuntimeError(f"persona-{state.persona_id} 回复失败")
        return f"persona{state.persona_id}:{user_text}"


class FakeAdapter:
    """fake 适配器（记录出站消息）。"""

    def __init__(self) -> None:
        """初始化。"""
        self.sent: list[OutboundMessageModel] = []

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """记录出站并返回成功结果。"""
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=[f"out:{len(self.sent)}"],
        )


@asynccontextmanager
async def _session_factory() -> Any:
    """fake 数据库会话工厂（空会话对象——fake 服务不消费）。"""
    yield object()


def _inbound(
    text: str = "群里有人吗",
    *,
    message_id: str = "42",
    is_group_chat: bool = True,
) -> InboundMessageModel:
    """构造入站消息。"""
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="group:10086",
        message_id=message_id,
        sender_id="200",
        text=text,
        is_group_chat=is_group_chat,
    )


def _router(
    *,
    service: FakeChatService,
    adapter: FakeAdapter,
    persona_service: FakePersonaService,
    settings: GroupPersonaSettings | None,
    clock: FakeClock | None = None,
) -> ChannelRouter:
    """装配路由器（注入 fake 依赖）。"""
    router = ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=_session_factory,
        group_settings=settings,
        persona_service=persona_service,
        clock=clock or FakeClock(),
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


async def test_group_mode_disabled_uses_single_primary_session() -> None:
    """功能关闭：群聊沿用单人格既有路径（无 persona 后缀会话）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True)]),
        settings=GroupPersonaSettings(enabled=False),
    )
    result = await router.handle_inbound(_inbound())
    assert result.message_ids == ["out:1"]
    assert service.create_calls[0]["title"] == "channel:telegram:group:10086"
    assert service.reply_calls[0]["sender_provenance"] == MemoryProvenance.PEER


async def test_group_rotation_hands_turn_to_previous_non_responder() -> None:
    """轮转：第二条消息改由上一轮未发言人格应答（独立会话归属）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=1, cooldown_ms=0),
    )
    await router.handle_inbound(_inbound("第一条", message_id="1"))
    await router.handle_inbound(_inbound("第二条", message_id="2"))
    replied = [call["persona_id"] for call in service.reply_calls]
    assert replied == [1, 2]
    titles = [call["title"] for call in service.create_calls]
    assert "channel:telegram:group:10086:persona:1" in titles
    assert "channel:telegram:group:10086:persona:2" in titles
    assert [call["persona_ctx"] for call in service.reply_calls] == ["1", "2"]


async def test_group_multi_responder_cap_and_reply_attribution() -> None:
    """多人格接续：上限内逐人格回复，首条引用入站、其余不引用。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=2, cooldown_ms=0),
    )
    result = await router.handle_inbound(_inbound("大家好", message_id="99"))
    assert result.message_ids == ["out:1", "out:2"]
    assert [message.reply_to_message_id for message in adapter.sent] == ["99", None]
    assert [message.text for message in adapter.sent] == [
        "persona1:大家好",
        "persona2:大家好",
    ]
    assert len(service.create_calls) == 2
    assert all(call["db_session"] is not None for call in service.create_calls)


async def test_group_cooldown_excludes_recent_and_falls_back_when_all_cooling() -> None:
    """冷却：刚发言者被排除；全员冷却时选最早发言者兜底。"""
    clock = FakeClock()
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=1, cooldown_ms=60_000),
        clock=clock,
    )
    await router.handle_inbound(_inbound("第一条", message_id="1"))
    await router.handle_inbound(_inbound("第二条", message_id="2"))
    await router.handle_inbound(_inbound("第三条", message_id="3"))
    assert [call["persona_id"] for call in service.reply_calls] == [1, 2, 1]


async def test_private_chat_ignores_group_rotation() -> None:
    """单聊：即使群聊多人格启用，也走单人格路径。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=2),
    )
    await router.handle_inbound(_inbound("私聊", is_group_chat=False))
    assert len(service.reply_calls) == 1
    assert service.create_calls[0]["title"] == "channel:telegram:group:10086"


async def test_single_persona_failure_isolated_from_other_responders() -> None:
    """逐人格失败隔离：一个失败不影响其余人格应答。"""
    service = FakeChatService(failing_persona_ids={1})
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=2, cooldown_ms=0),
    )
    result = await router.handle_inbound(_inbound("有人吗", message_id="7"))
    assert result.message_ids == ["out:1"]
    assert adapter.sent[0].text == "persona2:有人吗"


async def test_all_persona_failures_fall_back_to_short_message() -> None:
    """全部人格失败：外层兜底回发短中文提示（不静默丢消息）。"""
    service = FakeChatService(failing_persona_ids={1, 2})
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([_record(1, is_primary=True), _record(2)]),
        settings=GroupPersonaSettings(enabled=True, max_responders=2, cooldown_ms=0),
    )
    result = await router.handle_inbound(_inbound("有人吗", message_id="8"))
    assert result.message_ids == ["out:1"]
    assert adapter.sent[0].text == "我这边出了点问题，稍后再试"


async def test_persona_pool_read_failure_falls_back_to_primary_path() -> None:
    """启用人格读取失败：回退主人格单人格路径（不阻断回复）。"""
    service = FakeChatService()
    adapter = FakeAdapter()

    class _FailingPersonaService:
        async def list_addressable(self, session: object) -> list[PersonaRecordDTO]:
            raise RuntimeError("注册表不可用")

    router = ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=_session_factory,
        group_settings=GroupPersonaSettings(enabled=True, max_responders=2),
        persona_service=_FailingPersonaService(),
    )
    router.register_adapter(ChannelType.TELEGRAM, adapter)
    result = await router.handle_inbound(_inbound())
    assert result.message_ids == ["out:1"]
    assert service.create_calls[0]["title"] == "channel:telegram:group:10086"
    assert len(service.reply_calls) == 1


async def test_empty_pool_falls_back_to_primary_path() -> None:
    """空候选：回退主人格单人格路径（无 persona 后缀会话）。"""
    service = FakeChatService()
    adapter = FakeAdapter()
    router = _router(
        service=service,
        adapter=adapter,
        persona_service=FakePersonaService([]),
        settings=GroupPersonaSettings(enabled=True),
    )
    result = await router.handle_inbound(_inbound())
    assert result.message_ids == ["out:1"]
    assert service.create_calls[0]["title"] == "channel:telegram:group:10086"
    assert len(service.reply_calls) == 1
