"""channels 域服务——ChannelRouter（入站消息 → 人格会话 → 出站投递）。

职责链（单一入口 :meth:`ChannelRouter.handle_inbound`）::

    InboundMessageModel
        ↓ ① 适配器寻址（未注册通道 = CHANNEL_DISABLED，仅记录）
        ↓ ② per-chat 串行化锁获取（asyncio.Lock——同聊天消息全程串行，
              含会话懒建 / 回复 / 兜底回发；不同聊天互不阻塞）
        ↓ ③ 每聊天会话懒建（会话身份键 ``channel:chat_id``——复用直至
              进程重启；持久化装配时经注入的 session factory 落库）
        ↓ ④ 人格绑定（bind_persona_context——PersonaMixin 作用域）
        ↓ ⑤ ChatService.reply（组装 → 网关 → 用量记账自动发生）
        ↓ ⑥ 适配器发送（OutboundMessageModel → 平台 wire）

人格语义（多激活可寻址模型）：**主人格应答缺省通道会话**。会话懒建经
``ChatService.create_session(db_session=...)`` 内部解析当前主人格（注册
表事务性切换不变式的消费面）；已建会话按 ``persona_id`` 保持在位
（会话级寻址——通道消息不因主人格切换而换人格）；按聊天绑定人格 /
多人格路由属后续多通道扩展交付面。

持久化语义：注入 ``session_factory``（app lifespan 装配）时会话创建经
数据库会话落 ``ff_chat_sessions``（标题 = 会话身份键），其后
``ChatService.reply`` 的回合持久化由 ChatService 自持的 ``session_scope``
完成（本层零额外持久化路径）；未注入时为纯内存降级形态（真实
ChatService 将抛 ``INTERNAL_ERROR`` → 走兜底回发；仅测试 / 诊断用）。

错误隔离（铁律）：**单条消息的任何失败（LLM / 网关 / 持久化）不得击穿
runner**——``handle_inbound`` 恒不抛异常：逐消息捕获，经适配器回发短中文
兜底消息，错误细节只进日志。无主人格（``NOT_FOUND``）为已知确定态，
回发该业务异常的用户面消息（可操作指引）；其余异常一律回发通用兜底。

并发语义（per-chat 串行化）：QQ 推送式入站（webhook 每 POST 一协程）
与 Telegram 拉取式入站共用本路由器——``handle_inbound`` 以 per-chat
``asyncio.Lock`` 串行化同聊天消息的**全链路**（会话懒建 + 回复 + 兜底
回发）：并发首消息不会重复建会话行，同聊天回复严格按到达序逐条完成
（无回合交错）；不同聊天互不阻塞（锁粒度 = 会话身份键）。Telegram
runner 单循环本就串行（锁无竞争零开销）；QQ 并发上报由锁归一到
等效串行语义。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import (
    ChannelContextModel,
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.service import bind_persona_context

__all__ = [
    "CHANNEL_FALLBACK_REPLY_TEXT",
    "ChannelAdapterProtocol",
    "ChannelRouter",
    "ChannelSessionFactory",
]

#: 通用兜底回复文案（错误细节只进日志——用户面恒短句）。
CHANNEL_FALLBACK_REPLY_TEXT: str = "我这边出了点问题，稍后再试"

#: 数据库会话工厂契约（app lifespan 注入 ``session_scope``；None = 纯内存）。
ChannelSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: ChatService 工厂契约（逐消息调用——装配方闭包网关 / 持久化服务）。
ChatServiceFactory = Callable[[], ChatService]


class ChannelAdapterProtocol(Protocol):
    """通道适配器发送面协议（平台 runner 实现——Telegram 首个）。

    结构化类型：实现类提供同签名 ``send_message`` 即满足（无需显式继承）。
    """

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """发送出站消息（平台 wire 归一 + 超长分段由实现承担）。

        Args:
            message: 出站消息（通道中立形态）。

        Returns:
            投递结果（分段发送时 message_ids 多条）。

        Raises:
            BusinessException: 平台调用失败（``CHANNEL_API_ERROR`` 等——
                调用方（路由器）负责兜底隔离）。
        """
        ...


class ChannelRouter:
    """通道路由器（入站消息 → 人格会话循环 → 出站投递；恒不抛异常）。

    并发语义：per-chat ``asyncio.Lock`` 串行化同聊天 ``handle_inbound``
    全链路（锁生命周期与 :attr:`_sessions` 镜像——该聊天首条消息时懒建
    入 ``_locks``、复用直至进程重启、无淘汰；增长面 = 互异聊天数，与会话
    字典同界）。锁对象在事件循环内原子懒建（get/set 之间零 await）——
    并发首消息必然取到同一把锁，会话懒建的 check-then-create 竞态因此
    关闭（不会重复落库会话行）。

    构造注入（测试 / 装配友好）：ChatService 工厂与数据库会话工厂均可
    显式注入；测试注入 fake 工厂记录调用形状断言契约（零真实网络 /
    零真实配置）。
    """

    def __init__(
        self,
        *,
        chat_service_factory: ChatServiceFactory,
        session_factory: ChannelSessionFactory | None = None,
    ) -> None:
        """初始化路由器。

        Args:
            chat_service_factory: ChatService 工厂（逐消息调用；app 装配
                形态闭包 ``app.state.gateway`` + ``ChatSessionService``）。
            session_factory: 数据库会话工厂（None = 纯内存降级形态——
                真实 ChatService 会话创建将失败并走兜底回发）。
        """
        self._chat_service_factory = chat_service_factory
        self._session_factory = session_factory
        self._adapters: dict[ChannelType, ChannelAdapterProtocol] = {}
        self._sessions: dict[str, ChatSessionStateModel] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 装配面
    # ------------------------------------------------------------------

    def register_adapter(self, channel: ChannelType, adapter: ChannelAdapterProtocol) -> None:
        """注册通道适配器发送面（同通道重复注册后写覆盖前写）。

        Args:
            channel: 通道类型。
            adapter: 适配器发送面（平台 runner 实现）。
        """
        self._adapters[channel] = adapter

    @property
    def active_channels(self) -> list[ChannelType]:
        """已注册适配器的通道列表（装配观测面）。"""
        return list(self._adapters)

    def get_adapter(self, channel: ChannelType) -> ChannelAdapterProtocol | None:
        """读取通道适配器发送面（出站投递的公共读取面）。

        Args:
            channel: 通道类型。

        Returns:
            已注册的适配器发送面；未注册返回 None。
        """
        return self._adapters.get(channel)

    @property
    def session_count(self) -> int:
        """在途通道会话数（channel:chat_id 维度；观测面）。"""
        return len(self._sessions)

    @staticmethod
    def session_key(context: ChannelContextModel) -> str:
        """会话身份键（``channel:chat_id``——每聊天一个 ChatService 会话）。

        Args:
            context: 通道会话上下文。

        Returns:
            身份键字符串（同时用作持久化会话标题）。
        """
        return f"channel:{context.channel.value}:{context.chat_id}"

    # ------------------------------------------------------------------
    # 主入口（恒不抛异常——单消息错误隔离）
    # ------------------------------------------------------------------

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """处理单条入站消息（会话路由 + 人格回复 + 投递；失败兜底回发）。

        并发语义（锁生命周期）：本方法全程持有该聊天的 per-chat
        ``asyncio.Lock``——**锁获取先于会话查找 / 懒建**（并发首消息
        串行化，重复建会话行的竞态关闭），覆盖会话创建 + 回复 + 适配器
        发送 + 兜底回发的完整跨度；锁与 ``_sessions`` 条目同生命周期
        （首条消息懒建、复用直至进程重启、无淘汰）。不同聊天取不同锁，
        互不阻塞。

        Args:
            message: 入站消息（通道中立形态）。

        Returns:
            投递结果（兜底路径亦返回其投递结果；无适配器 / 兜底亦失败时
            返回空 message_ids 结果——**本方法恒不抛异常**）。
        """
        context = ChannelContextModel(channel=message.channel, chat_id=message.chat_id)
        adapter = self._adapters.get(message.channel)
        if adapter is None:
            logger.error(
                "通道未注册适配器（CHANNEL_DISABLED）: channel={}, chat_id={}",
                message.channel.value,
                message.chat_id,
            )
            return ChannelDeliveryResultModel(
                channel=message.channel, chat_id=message.chat_id, message_ids=[]
            )
        async with self._lock_for(self.session_key(context)):
            try:
                state = await self._get_or_create_session(context)
                async with bind_persona_context(state.persona_id):
                    sender_provenance = (
                        MemoryProvenance.PEER
                        if message.is_group_chat
                        else MemoryProvenance.OWNER
                    )
                    reply_text = await self._chat_service_factory().reply(
                        state, message.text, sender_provenance=sender_provenance
                    )
                return await adapter.send_message(
                    OutboundMessageModel(
                        chat_id=message.chat_id,
                        text=reply_text,
                        reply_to_message_id=message.message_id,
                    )
                )
            except Exception as exc:
                fallback_text = self._fallback_text_for(exc)
                logger.opt(exception=True).error(
                    "通道消息处理失败（兜底回发）: channel={}, chat_id={}, "
                    "message_id={}, fallback={!r}",
                    message.channel.value,
                    message.chat_id,
                    message.message_id,
                    fallback_text,
                )
                return await self._send_fallback(adapter, message, fallback_text)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _lock_for(self, key: str) -> asyncio.Lock:
        """按会话身份键取 per-chat 串行化锁（懒建——事件循环内原子）。

        事件循环单线程语义保证 get/set 之间零 await 即零竞态——并发
        首消息必然取到同一把锁（与 ``_sessions`` 条目同生命周期：该
        聊天首条消息时创建、复用直至进程重启、无淘汰）。

        Args:
            key: 会话身份键（``channel:chat_id``）。

        Returns:
            该聊天的串行化锁（未持锁态返回；调用方 ``async with`` 全程持有）。
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _get_or_create_session(self, context: ChannelContextModel) -> ChatSessionStateModel:
        """按会话身份键取在途会话；缺失时懒建（持久化装配时经数据库会话）。

        并发前提：本方法在调用方（``handle_inbound``）持有的 per-chat
        锁临界段内执行——check-then-create 无并发窗口（并发首消息经
        锁串行化，恰好一个协程完成懒建与 ``_sessions`` 落位）。

        Args:
            context: 通道会话上下文。

        Returns:
            该聊天的 :class:`ChatSessionStateModel`（首次消息时创建）。

        Raises:
            Exception: 会话创建链路失败（无主人格 / 持久化未装配等——
                由 ``handle_inbound`` 统一兜底隔离）。
        """
        key = self.session_key(context)
        state = self._sessions.get(key)
        if state is not None:
            return state
        service = self._chat_service_factory()
        if self._session_factory is None:
            # 纯内存降级形态：无数据库会话（真实 ChatService 此处抛
            # INTERNAL_ERROR → handle_inbound 兜底回发；fake 工厂可直供）
            state = await service.create_session(title=key)
        else:
            # 持久化形态：经注入会话落 ff_chat_sessions（标题 = 身份键；
            # 主人格由 ChatService.create_session 内部解析）
            async with self._session_factory() as db_session:
                state = await service.create_session(db_session=db_session, title=key)
        self._sessions[key] = state
        logger.info("通道会话已建立: key={}, persona_id={}", key, state.persona_id)
        return state

    async def _send_fallback(
        self,
        adapter: ChannelAdapterProtocol,
        message: InboundMessageModel,
        fallback_text: str,
    ) -> ChannelDeliveryResultModel:
        """兜底回发（不引用原消息；回发自身失败仅记录——不级联抛出）。

        Args:
            adapter: 通道适配器发送面。
            message: 触发失败的入站消息。
            fallback_text: 兜底文案。

        Returns:
            投递结果（回发失败时为空 message_ids 结果）。
        """
        try:
            return await adapter.send_message(
                OutboundMessageModel(chat_id=message.chat_id, text=fallback_text)
            )
        except Exception:
            logger.opt(exception=True).error(
                "通道兜底消息回发失败（放弃该消息）: channel={}, chat_id={}",
                message.channel.value,
                message.chat_id,
            )
            return ChannelDeliveryResultModel(
                channel=message.channel, chat_id=message.chat_id, message_ids=[]
            )

    @staticmethod
    def _fallback_text_for(exc: Exception) -> str:
        """按异常类别选兜底文案（错误细节只进日志）。

        Args:
            exc: 消息处理链路异常。

        Returns:
            ``NOT_FOUND``（无主人格——已知确定态）回发业务异常的用户面
            消息（含可操作指引）；其余一律通用短句兜底。
        """
        if isinstance(exc, BusinessException) and exc.code == ErrorCode.NOT_FOUND:
            return exc.message
        return CHANNEL_FALLBACK_REPLY_TEXT
