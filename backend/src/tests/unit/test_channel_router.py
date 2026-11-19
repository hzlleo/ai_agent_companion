"""通道路由器测试——ChannelRouter（fake ChatService / fake 适配器 / fake 会话工厂）。

覆盖矩阵：

    - 每聊天会话懒建与复用（channel:chat_id 身份键 + 持久化调用形状）；
    - 人格上下文绑定（bind_persona_context → persona_id_var 在 reply 面可见）；
    - 出站投递形状（reply_to 引用 / 兜底不引用）；
    - 单消息错误隔离（LLM 失败 → 短中文兜底；无激活人格 NOT_FOUND →
      业务异常消息透传；兜底回发自身失败不级联）；
    - 未注册适配器（CHANNEL_DISABLED 静默形态）与纯内存降级形态；
    - per-chat 并发串行化（同聊天并发消息恰一次建会话 + 回复严格串行；
      不同聊天并发互不阻塞——推送式 webhook 并发面回归锚）。

零真实网络 / 零真实配置：全部依赖 fake；并发等待经 asyncio.Event 门控
（确定性事件等待——零固定睡眠 / 零轮询）。
"""

from __future__ import annotations

import asyncio
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
from packages.harness.channels.channel_service import (
    CHANNEL_FALLBACK_REPLY_TEXT,
    ChannelRouter,
)
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.chara_card import CharaCardModel

#: 测试人格固定主键（persona_id_var 断言用——字符串形态）。
_PERSONA_ID = 7


def _inbound(
    chat_id: str = "10086",
    text: str = "你好",
    message_id: str = "42",
    is_group_chat: bool = False,
) -> InboundMessageModel:
    """构造入站消息（测试直供形态）。

    Args:
        chat_id: 会话 ID。
        text: 消息文本。
        message_id: 消息 ID。
        is_group_chat: 群聊标记（provenance 分级回归用）。

    Returns:
        :class:`InboundMessageModel` 实例。
    """
    return InboundMessageModel(
        channel=ChannelType.TELEGRAM,
        chat_id=chat_id,
        message_id=message_id,
        sender_id="7",
        text=text,
        is_group_chat=is_group_chat,
    )


class FakeChatService:
    """fake ChatService——记录 create_session / reply 调用形状。"""

    def __init__(
        self,
        *,
        reply_error: Exception | None = None,
        create_error: Exception | None = None,
    ) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.reply_error = reply_error
        self.create_error = create_error

    async def create_session(
        self,
        *,
        persona: object | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建（记录调用形状；可编程抛错）。"""
        if self.create_error is not None:
            raise self.create_error
        self.create_calls.append({"persona": persona, "db_session": db_session, "title": title})
        return ChatSessionStateModel(
            persona_id=_PERSONA_ID,
            persona_name="小鱼",
            card=CharaCardModel(name="小鱼"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance=None
    ) -> str:
        """fake 回复（记录人格上下文 + 复用态断言原料 + 来源分级）。"""
        self.reply_calls.append(
            {
                "state": state,
                "text": user_text,
                "persona_ctx": persona_id_var.get(),
                "session_id": state.session_id,
                "sender_provenance": sender_provenance,
            }
        )
        if self.reply_error is not None:
            raise self.reply_error
        return f"echo:{user_text}"


class FakeAdapter:
    """fake 通道适配器——记录出站消息。"""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[OutboundMessageModel] = []
        self.fail_after = fail_after
        self._calls = 0

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送（可编程在第 N 次后失败——兜底级联测试用）。"""
        self._calls += 1
        if self.fail_after is not None and self._calls > self.fail_after:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="平台发送失败（测试注入）"
            )
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=[str(1000 + self._calls)],
        )


class FakeSessionFactory:
    """fake 数据库会话工厂（产出哨兵会话对象——调用形状断言用）。"""

    def __init__(self) -> None:
        self.sentinel = object()
        self.opened = 0

    def __call__(self) -> Any:
        @asynccontextmanager
        async def _cm() -> Any:
            self.opened += 1
            yield self.sentinel

        return _cm()


def _router(
    service: FakeChatService,
    *,
    session_factory: FakeSessionFactory | None = None,
) -> ChannelRouter:
    """构造被测路由器（fake ChatService 工厂 + 可选 fake 会话工厂）。

    Args:
        service: fake ChatService（工厂返回同一实例——调用计数聚合）。
        session_factory: fake 会话工厂（None = 纯内存形态）。

    Returns:
        :class:`ChannelRouter` 实例。
    """
    return ChannelRouter(
        chat_service_factory=lambda: service,
        session_factory=session_factory,
    )


class TestSessionLifecycle:
    async def test_session_created_once_per_chat_and_reused(self) -> None:
        """每聊天会话懒建复用：同聊天两条消息一次 create、两次 reply 同态。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound(text="第一句"))
        await router.handle_inbound(_inbound(text="第二句"))

        assert len(service.create_calls) == 1
        assert len(service.reply_calls) == 2
        assert service.reply_calls[0]["state"] is service.reply_calls[1]["state"]
        assert router.session_count == 1

    async def test_distinct_chats_get_distinct_sessions(self) -> None:
        """不同聊天独立会话（channel:chat_id 身份键维度）。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound(chat_id="10086"))
        await router.handle_inbound(_inbound(chat_id="20000"))

        assert len(service.create_calls) == 2
        assert router.session_count == 2
        titles = [call["title"] for call in service.create_calls]
        assert titles == ["channel:telegram:10086", "channel:telegram:20000"]

    async def test_persistence_call_shape_with_session_factory(self) -> None:
        """持久化形态调用形状：create_session 携 db_session + 身份键标题。"""
        service = FakeChatService()
        session_factory = FakeSessionFactory()
        adapter = FakeAdapter()
        router = _router(service, session_factory=session_factory)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound())

        assert session_factory.opened == 1
        assert service.create_calls[0]["db_session"] is session_factory.sentinel
        assert service.create_calls[0]["title"] == "channel:telegram:10086"

    async def test_memory_only_mode_passes_no_db_session(self) -> None:
        """纯内存形态（session_factory=None）：create 不携带 db_session。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound())

        assert service.create_calls[0]["db_session"] is None
        assert result.message_ids != []


class TestReplyFlow:
    async def test_persona_context_bound_during_reply(self) -> None:
        """人格绑定：reply 执行期 persona_id_var == 会话人格（字符串形态）。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound())

        assert service.reply_calls[0]["persona_ctx"] == str(_PERSONA_ID)

    async def test_outbound_shape_reply_to_inbound(self) -> None:
        """出站形状：正常回复引用入站消息 ID。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound(message_id="77"))

        assert adapter.sent[0].text == "echo:你好"
        assert adapter.sent[0].reply_to_message_id == "77"
        assert result.channel is ChannelType.TELEGRAM
        assert result.message_ids != []

    async def test_active_channels_observation(self) -> None:
        """装配观测面：active_channels 反映注册表。"""
        router = _router(FakeChatService())
        assert router.active_channels == []
        router.register_adapter(ChannelType.TELEGRAM, FakeAdapter())
        assert router.active_channels == [ChannelType.TELEGRAM]


class TestErrorIsolation:
    async def test_llm_error_sends_short_fallback(self) -> None:
        """LLM/网关失败：短中文兜底回发（细节只进日志）——不引用原消息。"""
        service = FakeChatService(
            reply_error=BusinessException(
                code=ErrorCode.LLM_CALL_FAILED, message="LLM 调用失败，请稍后重试"
            )
        )
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound(message_id="77"))

        assert len(adapter.sent) == 1
        assert adapter.sent[0].text == CHANNEL_FALLBACK_REPLY_TEXT
        assert adapter.sent[0].reply_to_message_id is None
        assert result.message_ids != []

    async def test_unexpected_error_sends_short_fallback(self) -> None:
        """非业务异常（如 ValueError）：同样短兜底——恒不抛出。"""
        service = FakeChatService(reply_error=ValueError("意外崩溃"))
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound())

        assert adapter.sent[0].text == CHANNEL_FALLBACK_REPLY_TEXT
        assert result.message_ids != []

    async def test_no_active_persona_forwards_actionable_message(self) -> None:
        """无激活人格（NOT_FOUND）：回发业务异常的用户面消息（含指引）。"""
        guidance = "当前没有激活人格，请先创建并激活一个人格再开始对话"
        service = FakeChatService()
        service.reply_error = BusinessException(code=ErrorCode.NOT_FOUND, message=guidance)
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound())

        assert adapter.sent[0].text == guidance

    async def test_create_session_not_found_forwards_message(self) -> None:
        """会话创建即 NOT_FOUND（激活人格解析失败面）：同样透传指引消息。"""
        guidance = "当前没有激活人格，请先创建并激活一个人格再开始对话"
        service = FakeChatService(
            create_error=BusinessException(code=ErrorCode.NOT_FOUND, message=guidance)
        )
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound())

        assert service.create_calls == []
        assert service.reply_calls == []
        assert adapter.sent[0].text == guidance
        assert result.message_ids != []

    async def test_fallback_send_failure_does_not_raise(self) -> None:
        """兜底回发自身失败：返回空结果不级联抛出（单消息隔离铁律）。"""
        service = FakeChatService(
            reply_error=BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="x")
        )
        adapter = FakeAdapter(fail_after=0)
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        result = await router.handle_inbound(_inbound())

        assert adapter.sent == []
        assert result.message_ids == []

    async def test_unregistered_channel_returns_empty_result(self) -> None:
        """未注册适配器（CHANNEL_DISABLED）：空结果返回——不抛异常。"""
        service = FakeChatService()
        router = _router(service)

        result = await router.handle_inbound(_inbound())

        assert service.create_calls == []
        assert service.reply_calls == []
        assert result.message_ids == []

    async def test_failed_session_not_cached(self) -> None:
        """会话创建失败的聊天不留缓存态（下一条消息重试建会话）。"""
        guidance = "当前没有激活人格，请先创建并激活一个人格再开始对话"
        service = FakeChatService(
            create_error=BusinessException(code=ErrorCode.NOT_FOUND, message=guidance)
        )
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        await router.handle_inbound(_inbound())
        service.create_error = None
        await router.handle_inbound(_inbound())

        assert len(service.create_calls) == 1
        assert len(service.reply_calls) == 1
        assert router.session_count == 1


class GatedFakeChatService:
    """门控 fake ChatService——reply 进入即登记事件并阻塞在释放门。

    并发串行化断言原料：``events`` 按实际发生序记录 create / reply
    进入 / 退出；``release`` 为一次性释放门（asyncio.Event——确定性
    事件等待，零固定睡眠）。
    """

    def __init__(self) -> None:
        """初始化（释放门关闭态——reply 恒等待显式放行）。"""
        self.create_calls: int = 0
        self.events: list[str] = []
        self.release: asyncio.Event = asyncio.Event()
        self._entered: dict[str, asyncio.Event] = {}

    def entered_event(self, text: str) -> asyncio.Event:
        """取指定消息文本的 reply 进入事件（懒建——等待面）。"""
        return self._entered.setdefault(text, asyncio.Event())

    async def create_session(
        self,
        *,
        persona: object | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建（记录事件 + 计数——串行化断言原料）。"""
        self.events.append(f"create:{title}")
        self.create_calls += 1
        return ChatSessionStateModel(
            persona_id=_PERSONA_ID,
            persona_name="小鱼",
            card=CharaCardModel(name="小鱼"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance=None
    ) -> str:
        """fake 慢回复：登记进入事件 → 等待释放门 → 登记退出事件。"""
        self.events.append(f"enter:{user_text}")
        self.entered_event(user_text).set()
        await self.release.wait()
        self.events.append(f"exit:{user_text}")
        return f"echo:{user_text}"


class TestPerChatSerialization:
    """per-chat 串行化回归（QQ 推送式并发入站面——锁语义锚）。"""

    async def test_same_chat_concurrent_first_messages_serialize(self) -> None:
        """同聊天并发首消息：恰一次建会话 + 回复严格串行（无交错）。

        门控证明：消息一进入 reply（持锁）后，消息二经多个调度槽仍
        未进入 reply（锁前阻塞——若锁缺失必然立即进入，测试即红）；
        放行后事件序为「一进出一、二进出二」的完整串行形态。
        """
        service = GatedFakeChatService()
        adapter = FakeAdapter()
        router = _router(service)  # type: ignore[arg-type]
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        first = asyncio.create_task(router.handle_inbound(_inbound(text="一", message_id="1")))
        await asyncio.wait_for(service.entered_event("一").wait(), timeout=5.0)
        second = asyncio.create_task(router.handle_inbound(_inbound(text="二", message_id="2")))
        for _ in range(3):
            await asyncio.sleep(0)

        assert service.events == ["create:channel:telegram:10086", "enter:一"]
        assert "enter:二" not in service.events

        service.release.set()
        await asyncio.wait_for(first, timeout=5.0)
        await asyncio.wait_for(second, timeout=5.0)

        assert service.events == [
            "create:channel:telegram:10086",
            "enter:一",
            "exit:一",
            "enter:二",
            "exit:二",
        ]
        assert service.create_calls == 1
        assert router.session_count == 1
        assert len(adapter.sent) == 2

    async def test_distinct_chats_concurrent_no_cross_serialization(self) -> None:
        """不同聊天并发：互不阻塞（消息二在一未完成时并行进入 reply）。"""
        service = GatedFakeChatService()
        adapter = FakeAdapter()
        router = _router(service)  # type: ignore[arg-type]
        router.register_adapter(ChannelType.TELEGRAM, adapter)

        first = asyncio.create_task(
            router.handle_inbound(_inbound(chat_id="10086", text="一", message_id="1"))
        )
        await asyncio.wait_for(service.entered_event("一").wait(), timeout=5.0)
        second = asyncio.create_task(
            router.handle_inbound(_inbound(chat_id="20000", text="二", message_id="2"))
        )
        await asyncio.wait_for(service.entered_event("二").wait(), timeout=5.0)

        assert service.events == [
            "create:channel:telegram:10086",
            "enter:一",
            "create:channel:telegram:20000",
            "enter:二",
        ]
        assert "exit:一" not in service.events

        service.release.set()
        await asyncio.wait_for(first, timeout=5.0)
        await asyncio.wait_for(second, timeout=5.0)

        assert service.create_calls == 2
        assert router.session_count == 2
        assert len(adapter.sent) == 2


class TestV2PassThrough:
    async def test_inbound_attachments_pass_through_unmodified(self) -> None:
        """v2 附件透传：入站消息经路由器全程不被消耗 / 剥离（回复循环
        纯文本——附件供未来消费者（R2d/R2e）从 InboundMessageModel 读取；
        零路由器改动证明面）。合成事件另由「恒跳过」契约覆盖（见下一类）。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)
        attachment = ChannelMessageAttachment(
            kind=ChannelAttachmentKind.IMAGE, path="tg-file://AgAC"
        )
        inbound = InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_id="42",
            sender_id="7",
            text="看看这张",
            attachments=[attachment],
            synthetic=False,
        )

        result = await router.handle_inbound(inbound)

        assert service.reply_calls[0]["text"] == "看看这张"
        assert inbound.attachments == [attachment]
        assert inbound.synthetic is False
        assert adapter.sent[0].text == "echo:看看这张"
        assert adapter.sent[0].attachments == []
        assert result.attachment_results == []


class TestSyntheticEvents:
    """合成事件恒跳过：与门控装配无关（路由器前置拦截的行为契约）。"""

    async def test_synthetic_inbound_is_skipped_without_reply(self) -> None:
        """synthetic=True：空投递结果，不建会话 / 不回复 / 不投递。"""
        service = FakeChatService()
        adapter = FakeAdapter()
        router = _router(service)
        router.register_adapter(ChannelType.TELEGRAM, adapter)
        inbound = InboundMessageModel(
            channel=ChannelType.TELEGRAM,
            chat_id="10086",
            message_id="42",
            sender_id="7",
            text="心跳",
            synthetic=True,
        )

        result = await router.handle_inbound(inbound)

        assert result.message_ids == []
        assert service.create_calls == []
        assert service.reply_calls == []
        assert adapter.sent == []
        assert router.session_count == 0
        assert router.synthetic_skip_count == 1


class TestSenderProvenance:
    """入站来源分级：私聊 owner / 群聊 peer（提示词攻击防御的落档面）。"""

    async def test_private_chat_reply_carries_owner_provenance(self) -> None:
        """私聊消息 → ChatService.reply 收到 owner 来源分级。"""
        from packages.harness.memory.models import MemoryProvenance

        chat = FakeChatService()
        router = _router(chat)
        router.register_adapter(ChannelType.TELEGRAM, FakeAdapter())
        await router.handle_inbound(_inbound(text="早呀"))
        assert chat.reply_calls[0]["sender_provenance"] == MemoryProvenance.OWNER

    async def test_group_chat_reply_carries_peer_provenance(self) -> None:
        """群聊消息 → ChatService.reply 收到 peer 来源分级（同侪发言不晋升）。"""
        from packages.harness.memory.models import MemoryProvenance

        chat = FakeChatService()
        router = _router(chat)
        router.register_adapter(ChannelType.TELEGRAM, FakeAdapter())
        await router.handle_inbound(_inbound(chat_id="group:42", text="你好", is_group_chat=True))
        assert chat.reply_calls[0]["sender_provenance"] == MemoryProvenance.PEER
