"""意图快径接线测试——ChatService.reply 前钩 + ChannelRouter 全链路。

覆盖矩阵（接线面——R2a-6）：

    - chat 级（fake 快径面 / 真快径服务）：命中 → 罐头回复落回合 +
      网关零调用 + 拟真延迟经休眠面；未命中 / 未注入 → 网关照常；
    - 校验边界：快径路径保持空白输入拒绝（VALIDATION_ERROR）；
    - 流式分叉：stream_reply 不走快径（CLI 消费面零变化）；
    - 持久化不变式：快径回合照常落 ff_chat_messages（用户 + 罐头
      助手各一行——记忆 / 梦境可见）；
    - router 级（真实 ChatService + 真实持久化栈 + fake 适配器 / 网关）：
      入站命中 → 罐头投递 + 网关零调用 + 落库；入站未命中 → 网关调用。

零真实网络 / 零真实等待：网关与适配器 fake；快径服务的随机源与休眠
面注入假件（延迟区间断言，不睡眠）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.database.session import session_scope
from packages.harness.intent.intent_models import IntentKind, IntentMatchModel
from packages.harness.intent.service import IntentShortcutService
from packages.harness.llm.llm_models import (
    ChatResponseModel,
    ChatStreamChunkModel,
)
from packages.harness.memory.orm import ChatMessageRow, ChatSessionRow
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context


def _at(hour: int, minute: int = 0) -> int:
    """naive 本地时刻 → epoch 毫秒（时区无关构造——与被测同墙钟语义）。

    Args:
        hour: 小时（0-23）。
        minute: 分钟（0-59；默认 0）。

    Returns:
        epoch 毫秒。
    """
    return int(datetime(2026, 1, 15, hour, minute).timestamp() * 1000)


async def _no_wait_sleep(seconds: float) -> None:
    """零等待休眠面（测试注入——拟真延迟不睡眠）。"""
    return None


class StaticRng:
    """静态随机源（randint 恒回放固定毫秒——延迟确定性）。"""

    def __init__(self, delay_ms: int = 1000) -> None:
        """初始化。

        Args:
            delay_ms: randint 回放值（毫秒）。
        """
        self._delay_ms = delay_ms

    def randint(self, low: int, high: int) -> int:
        """整型采样（恒回放值）。"""
        return self._delay_ms

    def random(self) -> float:
        """浮点采样（快径链路不消费）。"""
        return 0.5


class FakeGateway:
    """假 LLM 网关——记录非流式 / 流式调用；快径命中断言「零调用」证据。"""

    def __init__(self, response_content: str = "网关回复") -> None:
        """初始化。

        Args:
            response_content: 非流式回复文本。
        """
        self.response_content = response_content
        self.chat_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录参数）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        return ChatResponseModel(content=self.response_content)

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（记录参数 + 逐块产出）。"""
        self.stream_calls.append({"role": role, "messages": list(messages), **kwargs})
        yield ChatStreamChunkModel(content="网")
        yield ChatStreamChunkModel(content="关")
        yield ChatStreamChunkModel(content="", usage=None)


class FakeIntentFace:
    """fake 快径面——可编程匹配结果；记录调用形状。"""

    def __init__(self, match: IntentMatchModel | None = None) -> None:
        """初始化。

        Args:
            match: 可编程匹配结果（None = 恒未命中）。
        """
        self.match = match
        self.try_calls: list[dict[str, Any]] = []
        self.delay_calls: int = 0

    async def try_shortcut(
        self, text: str, persona: Any, now_ms: int | None = None
    ) -> IntentMatchModel:
        """fake 快径尝试（记录文本与人格对象）。"""
        self.try_calls.append({"text": text, "persona": persona})
        if self.match is None:
            return IntentMatchModel()
        return self.match

    async def realism_delay(self) -> None:
        """fake 拟真延迟（零等待——只计数）。"""
        self.delay_calls += 1


class FakeAdapter:
    """fake 通道适配器——记录出站消息。"""

    def __init__(self) -> None:
        """初始化（空出站记录）。"""
        self.sent: list[OutboundMessageModel] = []

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送（恒成功投递形态）。"""
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


def _state() -> ChatSessionStateModel:
    """直供会话状态（纯内存形态——快径前钩单测面）。"""
    return ChatSessionStateModel(
        persona_id=7,
        persona_name="feiyu",
        display_name="大肥鱼",
        card=CharaCardModel(name="大肥鱼"),
    )


def _hit(kind: IntentKind = IntentKind.TIME_QUERY, text: str = "现在是09:05呀") -> IntentMatchModel:
    """构造命中形态的匹配结果。

    Args:
        kind: 意图类型。
        text: 罐头回复文本。

    Returns:
        :class:`IntentMatchModel`（handled=True）。
    """
    return IntentMatchModel(kind=kind, confidence=0.95, response_text=text, handled=True)


def _inbound(text: str, message_id: str) -> InboundMessageModel:
    """构造入站消息（router 级测试直供形态）。

    Args:
        text: 消息文本。
        message_id: 消息 ID。

    Returns:
        :class:`InboundMessageModel` 实例。
    """
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id="10086",
        message_id=message_id,
        sender_id="7",
        text=text,
    )


async def _count_rows(db_session: AsyncSession, row_type: type[object]) -> int:
    """统计表行数（跨人格管理面口径——跳过人格自动过滤）。

    Args:
        db_session: 数据库会话。
        row_type: ORM 行类型。

    Returns:
        行数。
    """
    stmt = select(func.count()).select_from(row_type)
    return int(
        (
            await db_session.execute(stmt, execution_options={"skip_persona_filter": True})
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# chat 级：reply 前钩
# ---------------------------------------------------------------------------


class TestReplyFrontHook:
    async def test_hit_returns_canned_and_skips_gateway(self) -> None:
        """命中：罐头回复落回合 + 网关零调用 + 拟真延迟恰好一次。"""
        gateway = FakeGateway()
        face = FakeIntentFace(_hit())
        service = ChatService(gateway=gateway, intent_shortcut=face)
        state = _state()

        content = await service.reply(state, "现在几点")

        assert content == "现在是09:05呀"
        assert gateway.chat_calls == []
        assert face.delay_calls == 1
        assert [t.role.value for t in state.turns] == ["user", "assistant"]
        assert state.turns[0].content == "现在几点"
        assert state.turns[1].content == "现在是09:05呀"

    async def test_hit_passes_session_state_as_persona(self) -> None:
        """人格传参：会话状态直供（persona_name / display_name 提取原料）。"""
        face = FakeIntentFace(_hit())
        service = ChatService(gateway=FakeGateway(), intent_shortcut=face)
        state = _state()

        await service.reply(state, "现在几点")

        assert face.try_calls[0]["persona"] is state

    async def test_miss_falls_through_to_gateway(self) -> None:
        """未命中：照常走网关（既有行为零变化）。"""
        gateway = FakeGateway()
        face = FakeIntentFace(None)
        service = ChatService(gateway=gateway, intent_shortcut=face)
        state = _state()

        content = await service.reply(state, "随便聊聊")

        assert content == "网关回复"
        assert len(gateway.chat_calls) == 1
        assert face.delay_calls == 0

    async def test_no_face_keeps_legacy_behavior(self) -> None:
        """未注入快径面（intent_shortcut=None）：既有 reply 行为。"""
        gateway = FakeGateway()
        service = ChatService(gateway=gateway)
        state = _state()

        content = await service.reply(state, "现在几点")

        assert content == "网关回复"
        assert len(gateway.chat_calls) == 1

    async def test_blank_input_rejected_on_shortcut_path(self) -> None:
        """空白校验保持：快径路径同样拒绝空输入（VALIDATION_ERROR）。"""
        face = FakeIntentFace(_hit())
        service = ChatService(gateway=FakeGateway(), intent_shortcut=face)
        with pytest.raises(BusinessException) as exc_info:
            await service.reply(_state(), "   ")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_stream_reply_bypasses_shortcut(self) -> None:
        """流式分叉：stream_reply 不走快径（CLI 消费面零变化）。"""
        face = FakeIntentFace(_hit())
        gateway = FakeGateway()
        service = ChatService(gateway=gateway, intent_shortcut=face)
        state = _state()

        chunks: list[str] = []
        async for chunk in service.stream_reply(state, "你好"):
            chunks.append(chunk)

        assert "".join(chunks) == "网关"
        assert len(gateway.stream_calls) == 1
        assert face.try_calls == []
        assert face.delay_calls == 0

    async def test_real_service_full_shortcut_path(self) -> None:
        """真快径服务全链路：固定时钟 + 静态随机源 / 假休眠面（确定性）。"""
        slept: list[float] = []

        async def recording_sleep(seconds: float) -> None:
            """记录秒值的零等待休眠面。"""
            slept.append(seconds)

        intent = IntentShortcutService(
            rng=StaticRng(delay_ms=1000), sleep=recording_sleep, clock=lambda: _at(9, 5)
        )
        gateway = FakeGateway()
        service = ChatService(gateway=gateway, intent_shortcut=intent)
        state = _state()

        content = await service.reply(state, "现在几点了")

        assert "09:05" in content
        assert gateway.chat_calls == []
        assert slept == [1.0]
        assert [t.role.value for t in state.turns] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# 持久化不变式 + router 级全链路
# ---------------------------------------------------------------------------


def _persona_payload() -> dict[str, Any]:
    """构造最小人格创建请求体。"""
    return {
        "name": "shortcut-feiyu",
        "status": "active",
        "card": {
            "name": "大肥鱼",
            "description": "圆滚滚的橘色胖鱼",
            "personality": "元气",
            "first_mes": "你回来啦！",
        },
    }


async def _create_primary_persona(db_session: AsyncSession) -> PersonaRecordDTO:
    """创建并设主的人格（router 会话懒建的主人格解析前置）。

    Args:
        db_session: 数据库会话。

    Returns:
        主人格记录 DTO。
    """
    service = PersonaService()
    record = await service.create(db_session, PersonaCreateDTO.model_validate(_persona_payload()))
    return await service.set_primary(db_session, record.id)


@pytest.fixture
async def persist_env(
    shared_db_engine: AsyncEngine,
) -> AsyncIterator[
    tuple[FakeGateway, ChatService, ChatSessionService, PersonaRecordDTO, AsyncSession]
]:
    """持久化接线环境（共享引擎安装到 holder；主人格铺底）。

    Yields:
        ``(gateway, chat_service, session_service, persona_record, db_session)``
        五元组（holder 用后复位）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    session_service = ChatSessionService()
    gateway = FakeGateway()
    intent = IntentShortcutService(
        rng=StaticRng(delay_ms=1000), sleep=_no_wait_sleep, clock=lambda: _at(9, 5)
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        intent_shortcut=intent,
    )
    async with async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)() as db_session:
        persona = await _create_primary_persona(db_session)
        yield gateway, service, session_service, persona, db_session
    db_engine_holder.reset()


class TestPersistenceInvariant:
    async def test_shortcut_turns_persisted(self, persist_env: tuple) -> None:
        """快径回合落库：用户 + 罐头助手各一行（记忆 / 梦境可见）。"""
        gateway, service, session_service, persona, db_session = persist_env
        state = await service.create_session(
            persona=persona, db_session=db_session, title="快径落库"
        )

        content = await service.reply(state, "现在几点")

        assert "09:05" in content
        assert gateway.chat_calls == []
        assert await _count_rows(db_session, ChatMessageRow) == 2
        async with bind_persona_context(persona.id):
            detail = await session_service.get_session(db_session, state.session_id or 0)
        assert [m.role for m in detail.messages] == ["user", "assistant"]
        assert detail.messages[0].content == "现在几点"
        assert "09:05" in detail.messages[1].content


class TestRouterLevelWiring:
    async def test_inbound_shortcut_delivers_canned_without_gateway(
        self, persist_env: tuple
    ) -> None:
        """router 级命中：入站「现在几点」→ 罐头投递 + 网关零调用 + 落库。"""
        gateway, service, _, persona, db_session = persist_env
        adapter = FakeAdapter()
        router = ChannelRouter(
            chat_service_factory=lambda: service,
            session_factory=session_scope,
        )
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound("现在几点", "42"))

        assert result.message_ids != []
        assert len(adapter.sent) == 1
        assert "09:05" in adapter.sent[0].text
        assert adapter.sent[0].reply_to_message_id == "42"
        assert gateway.chat_calls == []
        assert await _count_rows(db_session, ChatMessageRow) == 2
        assert await _count_rows(db_session, ChatSessionRow) == 1

    async def test_inbound_non_matching_calls_gateway(self, persist_env: tuple) -> None:
        """router 级未命中：入站自由文本 → 网关调用 + 网关回复投递。"""
        gateway, service, _, _, _ = persist_env
        adapter = FakeAdapter()
        router = ChannelRouter(
            chat_service_factory=lambda: service,
            session_factory=session_scope,
        )
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound("随便聊聊今天的事", "43"))

        assert len(gateway.chat_calls) == 1
        assert adapter.sent[0].text == "网关回复"
