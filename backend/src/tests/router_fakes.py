"""通道路由器测试共享 fake 基建（主链路 / 心流门控 / 群聊多人格共用）。

职责边界：
    - ``FakeChatService``：会话创建与回复替身——记录调用形状（人格入参 /
      会话标题 / 上下文人格 / 来源分级），可编程会话创建失败、回复失败、
      人格级失败、回显形态与回复门控（进入即登记事件并阻塞到显式放行）；
    - ``FakeAdapter``：通道适配器替身——记录出站消息，可编程第 N 次后失败
      （兜底级联用例），出站消息 ID 形态经工厂函数按测试面选择；
    - ``FakeClock``：epoch 毫秒假时钟（测试手动推进）；
    - ``empty_session_factory``：空数据库会话工厂（fake 服务不消费会话）。

测试纪律：零真实网络 / 零真实配置；并发等待经 ``asyncio.Event`` 门控
（确定性事件等待——零固定睡眠 / 零轮询）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.chara_card import CharaCardModel

__all__ = [
    "PERSONA_ID",
    "BASE_MS",
    "FakeAdapter",
    "FakeChatService",
    "FakeClock",
    "chat_scoped_message_ids",
    "counter_message_ids",
    "empty_session_factory",
    "reply_index_message_ids",
]

#: 测试人格固定主键（persona_id_var 断言用）。
PERSONA_ID: int = 7

#: 假时钟起点（epoch 毫秒）。
BASE_MS: int = 1_700_000_000_000


def counter_message_ids(_message: OutboundMessageModel, call_index: int) -> list[str]:
    """序号式出站消息 ID（1000 起——主链路用例只断言非空与结果形态）。

    Args:
        _message: 出站消息（形态选择不需要）。
        call_index: 本次发送的序号（1 起）。

    Returns:
        单元素 ID 列表。
    """
    return [str(1000 + call_index)]


def chat_scoped_message_ids(message: OutboundMessageModel, call_index: int) -> list[str]:
    """会话作用域出站 ID（``out:<chat_id>:<序号>``——心流批量用例断言用）。

    Args:
        message: 出站消息（读取 chat_id）。
        call_index: 本次发送的序号（1 起）。

    Returns:
        单元素 ID 列表。
    """
    return [f"out:{message.chat_id}:{call_index}"]


def reply_index_message_ids(_message: OutboundMessageModel, call_index: int) -> list[str]:
    """应答序号式出站 ID（``out:<序号>``——群聊多应答用例断言用）。

    Args:
        _message: 出站消息（形态选择不需要）。
        call_index: 本次发送的序号（1 起）。

    Returns:
        单元素 ID 列表。
    """
    return [f"out:{call_index}"]


class FakeClock:
    """假时钟（epoch 毫秒——测试手动推进）。"""

    def __init__(self, start_ms: int = BASE_MS) -> None:
        """初始化。

        Args:
            start_ms: 起始时刻（epoch 毫秒）。
        """
        self.now = start_ms

    def __call__(self) -> int:
        """返回当前假时刻。"""
        return self.now


@asynccontextmanager
async def empty_session_factory() -> AsyncIterator[Any]:
    """空数据库会话工厂（产出空会话对象——fake 服务不消费）。"""
    yield object()


class FakeChatService:
    """fake ChatService——记录调用形状并可编程失败 / 回显 / 回复门控。"""

    def __init__(
        self,
        *,
        reply_error: Exception | None = None,
        create_error: Exception | None = None,
        failing_persona_ids: set[int] | None = None,
        echo_style: Literal["plain", "persona"] = "plain",
        gate_reply: bool = False,
    ) -> None:
        """初始化。

        Args:
            reply_error: 回复恒抛的异常（None = 正常回复）。
            create_error: 会话创建恒抛的异常（None = 正常创建）。
            failing_persona_ids: 回复必失败的人格主键集合（群聊失败隔离用）。
            echo_style: 回显形态——``plain`` 为 ``echo:<文本>``，
                ``persona`` 为 ``persona<人格主键>:<文本>``。
            gate_reply: True 时 reply 进入即登记事件并阻塞到 ``release``
                放行（per-chat 串行化断言用）。
        """
        self.create_calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.reply_error = reply_error
        self.create_error = create_error
        self.failing_persona_ids = failing_persona_ids or set()
        self.echo_style = echo_style
        self.gate_reply = gate_reply
        self.release: asyncio.Event = asyncio.Event()
        self._entered: dict[str, asyncio.Event] = {}

    def entered_event(self, text: str) -> asyncio.Event:
        """取指定消息文本的 reply 进入事件（懒建——等待面）。

        Args:
            text: 入站消息文本（用例按文本区分并发消息）。

        Returns:
            该文本首次进入 reply 时置位的事件。
        """
        return self._entered.setdefault(text, asyncio.Event())

    async def create_session(
        self,
        *,
        persona: object | None = None,
        db_session: object | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """fake 会话创建（记录调用形状；可编程抛错）。

        Args:
            persona: 应答人格记录（None = 主人格路径）。
            db_session: 数据库会话（None = 纯内存形态）。
            title: 会话标题（身份键）。

        Returns:
            会话状态（人格归属来自入参记录）。

        Raises:
            Exception: 构造时注入的 ``create_error``。
        """
        if self.create_error is not None:
            raise self.create_error
        persona_id = getattr(persona, "id", None)
        resolved_persona_id = persona_id if isinstance(persona_id, int) else PERSONA_ID
        self.create_calls.append(
            {
                "persona": persona,
                "persona_id": resolved_persona_id,
                "db_session": db_session,
                "title": title,
            }
        )
        self.events.append(f"create:{title}")
        return ChatSessionStateModel(
            persona_id=resolved_persona_id,
            persona_name=f"persona-{resolved_persona_id}",
            card=CharaCardModel(name=f"人格{resolved_persona_id}"),
        )

    async def reply(
        self, state: ChatSessionStateModel, user_text: str, *, sender_provenance: Any = None
    ) -> str:
        """fake 回复（记录人格上下文 / 来源分级；可编程失败 / 阻塞 / 回显）。

        Args:
            state: 会话状态。
            user_text: 用户文本。
            sender_provenance: 入站来源分级。

        Returns:
            按 ``echo_style`` 生成的回显文本。

        Raises:
            Exception: 注入的 ``reply_error`` 或该人格在失败集合内。
        """
        self.reply_calls.append(
            {
                "state": state,
                "persona_id": state.persona_id,
                "text": user_text,
                "persona_ctx": persona_id_var.get(),
                "session_id": state.session_id,
                "sender_provenance": sender_provenance,
            }
        )
        self.events.append(f"enter:{user_text}")
        if self.gate_reply:
            self.entered_event(user_text).set()
            await self.release.wait()
        self.events.append(f"exit:{user_text}")
        if self.reply_error is not None:
            raise self.reply_error
        if state.persona_id in self.failing_persona_ids:
            raise RuntimeError(f"persona-{state.persona_id} 回复失败")
        if self.echo_style == "persona":
            return f"persona{state.persona_id}:{user_text}"
        return f"echo:{user_text}"


class FakeAdapter:
    """fake 通道适配器——记录出站消息并可编程失败。"""

    def __init__(
        self,
        *,
        fail_after: int | None = None,
        message_ids: Callable[[OutboundMessageModel, int], list[str]] = counter_message_ids,
    ) -> None:
        """初始化。

        Args:
            fail_after: 第 N 次发送后开始失败（None = 恒成功；0 = 首次即失败）。
            message_ids: 出站消息 ID 工厂（用例按断言面选择形态）。
        """
        self.sent: list[OutboundMessageModel] = []
        self.fail_after = fail_after
        self._message_ids = message_ids
        self._calls = 0

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送（可编程在第 N 次后失败——兜底级联测试用）。

        Args:
            message: 出站消息。

        Returns:
            投递结果（ID 由工厂生成为例）。

        Raises:
            BusinessException: 越过 ``fail_after`` 阈值（平台发送失败）。
        """
        self._calls += 1
        if self.fail_after is not None and self._calls > self.fail_after:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="平台发送失败（测试注入）"
            )
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM,
            chat_id=message.chat_id,
            message_ids=self._message_ids(message, self._calls),
        )
