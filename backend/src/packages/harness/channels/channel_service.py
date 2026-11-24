"""channels 域服务——ChannelRouter（入站消息 → 人格会话 → 出站投递）。

职责链（单一入口 :meth:`ChannelRouter.handle_inbound`）::

    InboundMessageModel
        ↓ ① 适配器寻址（未注册通道 = CHANNEL_DISABLED，仅记录）
        ↓ ② 合成事件前置拦截（``synthetic=True`` 恒不回复——与
              ``FLOW_ENABLED`` 无关；不建会话、不回调心流，仅记 debug 计数）
        ↓ ③ per-chat 串行化锁获取（asyncio.Lock——同聊天消息全程串行，
              含会话懒建 / 门控 / 回复 / 兜底回发；不同聊天互不阻塞）
        ↓ ④ 心流门控判定（FLOW_ENABLED——立即回复 / 延迟 / 合并 / 不回复；
              延迟与合并批次经中央调度器一次性作业排程，不新建定时器）
        ↓ ⑤ 每聊天会话懒建（会话身份键 ``channel:chat_id``——复用直至
              进程重启；持久化装配时经注入的 session factory 落库）
        ↓ ⑥ 群聊多人格选择（GROUP_PERSONA_ENABLED——应答者轮转 / 冷却 /
              重复抑制；每个应答人格使用自己的持久化会话）
        ↓ ⑦ 人格绑定（bind_persona_context——PersonaMixin 作用域）
        ↓ ⑧ ChatService.reply（组装 → 网关 → 用量记账自动发生）
        ↓ ⑨ 适配器发送（OutboundMessageModel → 平台 wire）

合成事件（``synthetic=True``，默认协议值 False）：**路由器恒跳过**——
机器生成事件（heartflow / mood / 平台系统消息）不生成回复、不建会话、
不进入心流节拍状态；该语义与 ``FLOW_ENABLED`` 无关（门控关闭的部署同样
不回复）。拦截点位于适配器寻址之后、per-chat 锁之前，跳过计数经 debug
日志面观测（``synthetic_skip_count``）。

心流门控（``FLOW_ENABLED``，默认关闭）：入站先经判定层折算「现在该不该
开口」——睡眠窗口延迟至醒来、连发合并为一次回复、未读积压阶梯延迟、
关系亲近 / 亲密者跳过节流优先应答。门控关闭时本层直接走既有单人格回复
路径（零排程 / 零合并 / 零关系查询）；判定矩阵与配置见
:mod:`packages.harness.chat.flow_gate` / ``flow_models``。合成事件不进
门控（路由器已前置跳过）；判定层的 ``SKIP / SYNTHETIC_EVENT`` 结论保留
为纯函数契约与纵深防御。
延迟与合并批次通过注入的调度器门面 ``schedule_once`` 排程（中央调度器
统一计时）；调度器不可用时立即回复（fail-safe，记 warning）。

群聊多人格（``GROUP_PERSONA_ENABLED``，默认关闭）：群聊消息在启用人格
集合中按轮转 / 冷却 / 重复抑制规则选择应答者（数量上限 ``max_responders``，
1 = 轮流单人格应答，>1 = 多人格依次接续同一轮对话）。每个应答人格拥有
自己的持久化会话——会话标题后缀 ``:persona:<id>``，会话行
``ff_chat_sessions.persona_id`` 即该会话全部消息的应答归属（``replied_by``
来源），无需消息级冗余列或迁移。单聊消息不参与多人格轮转（既有语义
零变化）；群聊未启用人格或选择为空时回退主人格单人格路径。

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
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelContextModel,
    ChannelDeliveryResultModel,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.flow_gate import (
    clear_backlog,
    clear_pending,
    evaluate_flow_gate,
    mark_pending,
    merge_buffered_texts,
    record_inbound,
    remaining_quiet_ms,
    rhythm_state_from_gate,
)
from packages.harness.chat.flow_models import (
    FlowChatStateModel,
    FlowGateInputModel,
    FlowGateSettings,
    FlowRhythmStateModel,
    FlowVerdict,
)
from packages.harness.chat.persona_rotation import select_group_responders
from packages.harness.chat.persona_rotation_models import (
    GroupPersonaSettings,
    GroupResponderCandidateModel,
    GroupResponderStateModel,
)
from packages.harness.chat.service import ChatService
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.relationship_models import RelationshipSummaryModel
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.rhythm.rhythm_models import RhythmGate

__all__ = [
    "CHANNEL_FALLBACK_REPLY_TEXT",
    "ChannelAdapterProtocol",
    "ChannelRouter",
    "ChannelSessionFactory",
    "DelayedSchedulerFace",
    "PersonaListFace",
    "RelationshipLookupFace",
    "RhythmGateFace",
    "StickerIngestHook",
    "StickerReplyHook",
]

#: 通用兜底回复文案（错误细节只进日志——用户面恒短句）。
CHANNEL_FALLBACK_REPLY_TEXT: str = "我这边出了点问题，稍后再试"

#: 数据库会话工厂契约（app lifespan 注入 ``session_scope``；None = 纯内存）。
ChannelSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: ChatService 工厂契约（逐消息调用——装配方闭包网关 / 持久化服务）。
ChatServiceFactory = Callable[[], ChatService]

#: 延迟作业工厂契约（到期调用一次创建协程——调度器门面消费）。
DelayedJobFactory = Callable[[], Coroutine[Any, Any, None]]

#: 关系摘要查询契约（人格主键 + 判定时刻 → 关系摘要；未接线为 None）。
RelationshipLookupFace = Callable[[int, int], Awaitable[RelationshipSummaryModel | None]]

#: 入站表情收录契约（人格上下文已绑定——桥内部自开会话；未接线为 None）。
StickerIngestHook = Callable[[list[ChannelMessageAttachment]], Awaitable[None]]

#: 出站表情选取契约（人格上下文已绑定——返回 None = 本轮不发表情）。
StickerReplyHook = Callable[[], Awaitable[ChannelMessageAttachment | None]]


class RhythmGateFace(Protocol):
    """节律门面协议（rhythm 域 ``RhythmService`` 的结构化消费契约）。

    结构化类型：真实服务实现同签名 ``gate_reply`` 即满足；测试注入 fake
    同形态。门控关闭 / 注入面缺失时本层不调用（默认放行）。
    """

    async def gate_reply(self, now_ms: int | None = None) -> RhythmGate:
        """读取节律门判定（睡眠窗口 / 拟人延迟建议）。

        Args:
            now_ms: 判定时刻（epoch 毫秒；None = 服务注入时钟）。

        Returns:
            节律门结果（allowed + suggested_delay_ms + reason）。
        """
        ...


class PersonaListFace(Protocol):
    """启用人格读取面协议（persona 域注册表的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.service.PersonaService`
    天然满足（``list_addressable``——全部启用人格、主键升序）；测试注入
    fake 同形态。
    """

    async def list_addressable(self, session: AsyncSession) -> list[PersonaRecordDTO]:
        """读取可寻址人格集（全部启用人格）。

        Args:
            session: 注入的异步 Session。

        Returns:
            启用人格记录列表（空列表 = 无启用人格）。
        """
        ...


class DelayedSchedulerFace(Protocol):
    """延迟调度门面协议（中央调度器 ``Scheduler`` 的结构化消费契约）。

    结构化类型：实现同签名 ``schedule_once`` 即满足；门控关闭或注入面
    缺失时本层不排程（立即回复）。
    """

    def schedule_once(
        self, name: str, factory: DelayedJobFactory, *, delay_ms: int
    ) -> str:
        """注册一次性延迟作业（到期执行一次后自动移除）。

        Args:
            name: 作业名（在途集合内唯一）。
            factory: 作业工厂（到期调用一次创建协程）。
            delay_ms: 延迟毫秒（≥0）。

        Returns:
            作业名。
        """
        ...


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
    """通道路由器（入站消息 → 门控 / 人格会话循环 → 出站投递；恒不抛异常）。

    并发语义：per-chat ``asyncio.Lock`` 串行化同聊天 ``handle_inbound``
    全链路（锁生命周期与 :attr:`_sessions` 镜像——该聊天首条消息时懒建
    入 ``_locks``、复用直至进程重启、无淘汰；增长面 = 互异聊天数，与会话
    字典同界）。锁对象在事件循环内原子懒建（get/set 之间零 await）——
    并发首消息必然取到同一把锁，会话懒建的 check-then-create 竞态因此
    关闭（不会重复落库会话行）；延迟批次执行时重复获取同一把锁——与
    立即回复严格串行，同聊天永无并发生成。

    构造注入（测试 / 装配友好）：ChatService 工厂与数据库会话工厂均可
    显式注入；心流门控 / 群聊多人格 / 节律门面 / 调度器门面 / 关系查询
    均为可选注入面——缺省运行时与引入前完全一致（单人格、立即回复、
    零排程）。
    """

    def __init__(
        self,
        *,
        chat_service_factory: ChatServiceFactory,
        session_factory: ChannelSessionFactory | None = None,
        flow_settings: FlowGateSettings | None = None,
        group_settings: GroupPersonaSettings | None = None,
        rhythm_service: RhythmGateFace | None = None,
        scheduler_provider: Callable[[], DelayedSchedulerFace | None] | None = None,
        persona_service: PersonaListFace | None = None,
        relationship_lookup: RelationshipLookupFace | None = None,
        sticker_ingest_hook: StickerIngestHook | None = None,
        sticker_reply_hook: StickerReplyHook | None = None,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化路由器。

        Args:
            chat_service_factory: ChatService 工厂（逐消息调用；app 装配
                形态闭包 ``app.state.gateway`` + ``ChatSessionService``）。
            session_factory: 数据库会话工厂（None = 纯内存降级形态——
                真实 ChatService 会话创建将失败并走兜底回发；群聊多人格
                与关系查询在无数据库会话时自动降级为单人格路径）。
            flow_settings: 心流门控配置（None = 门控关闭——直通路径；
                app 装配传 ``settings.flow``）。
            group_settings: 群聊多人格配置（None = 关闭——群聊沿用单
                主人格应答；app 装配传 ``settings.group_persona``）。
            rhythm_service: 节律门面（None = 无节律数据——门控按「允许
                即时回复、零延迟」处理；即门控开启但节律层未装配时默认
                放行）。
            scheduler_provider: 延迟调度门面提供者（None = 无法排程——
                门控裁决为延迟 / 合并时降级立即回复）。提供者在门控触发
                时才被调用——app 启动顺序中调度器晚于通道层装配，延迟
                绑定读取运行期槽位。
            persona_service: 人格注册表服务（群聊多人格的启用人格读取面；
                None = 现场构造默认实例）。
            relationship_lookup: 关系摘要查询面（None = 门控不消费关系
                输入；注入后仅在门控启用且回复主路径解析出人格时调用）。
            sticker_ingest_hook: 入站表情收录桥（None = 不收录；注入后在
                入站含 sticker 附件时以应答人格上下文调用——桥内部自开
                数据库会话）。
            sticker_reply_hook: 出站表情选取桥（None = 不发；注入且本轮入站
                带 sticker 附件时取一张表情随文本回复投递）。
            clock: 时钟（epoch 毫秒——门控判定与投递观测面的注入式时间源）。
        """
        self._chat_service_factory = chat_service_factory
        self._session_factory = session_factory
        self._flow_settings = (
            flow_settings if flow_settings is not None and flow_settings.enabled else None
        )
        self._group_settings = (
            group_settings if group_settings is not None and group_settings.enabled else None
        )
        self._rhythm_service = rhythm_service
        self._scheduler_provider = scheduler_provider
        self._persona_service = persona_service if persona_service is not None else PersonaService()
        self._relationship_lookup = relationship_lookup
        self._sticker_ingest_hook = sticker_ingest_hook
        self._sticker_reply_hook = sticker_reply_hook
        self._clock = clock
        self._adapters: dict[ChannelType, ChannelAdapterProtocol] = {}
        self._sessions: dict[str, ChatSessionStateModel] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._flow_states: dict[str, FlowChatStateModel] = {}
        self._group_states: dict[str, GroupResponderStateModel] = {}
        self._chat_group_flags: dict[str, bool] = {}
        self._chat_owner_flags: dict[str, bool] = {}
        self._synthetic_skip_count = 0
        self._empty_text_skip_count = 0
        self._flow_job_seq = 0
        self._scheduler_missing_warned = False

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
        """在途通道会话数（channel:chat_id / 群聊人格会话维度；观测面）。"""
        return len(self._sessions)

    @property
    def flow_state_count(self) -> int:
        """持有心流节拍状态的会话数（观测面）。"""
        return len(self._flow_states)

    def flow_state_for(self, key: str) -> FlowChatStateModel | None:
        """读取单个会话的心流节拍状态快照（观测面——只读副本）。

        Args:
            key: 会话身份键（``channel:<通道>:<chat_id>``）。

        Returns:
            节拍状态副本（调用方修改不影响路由器内部态）；该聊天尚无
            节拍状态时返回 None。
        """
        state = self._flow_states.get(key)
        return None if state is None else state.model_copy(deep=True)

    @property
    def synthetic_skip_count(self) -> int:
        """累计跳过的合成事件条数（观测面——恒不回复语义的计数面）。"""
        return self._synthetic_skip_count

    @property
    def empty_text_skip_count(self) -> int:
        """累计跳过生成的空文本入站条数（观测面——仅附件消息的计数面）。"""
        return self._empty_text_skip_count

    @staticmethod
    def session_key(context: ChannelContextModel) -> str:
        """会话身份键（``channel:chat_id``——每聊天一个 ChatService 会话）。

        Args:
            context: 通道会话上下文。

        Returns:
            身份键字符串（同时用作持久化会话标题；群聊多人格模式下
            人格会话标题为 ``channel:chat_id:persona:<id>``）。
        """
        return f"channel:{context.channel.value}:{context.chat_id}"

    # ------------------------------------------------------------------
    # 主入口（恒不抛异常——单消息错误隔离）
    # ------------------------------------------------------------------

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """处理单条入站消息（门控 + 会话路由 + 人格回复 + 投递；失败兜底回发）。

        并发语义（锁生命周期）：本方法全程持有该聊天的 per-chat
        ``asyncio.Lock``——**锁获取先于会话查找 / 懒建**（并发首消息
        串行化，重复建会话行的竞态关闭），覆盖会话创建 + 门控 + 回复 +
        适配器发送 + 兜底回发的完整跨度；锁与 ``_sessions`` 条目同生命
        周期（首条消息懒建、复用直至进程重启、无淘汰）。不同聊天取不同
        锁，互不阻塞。

        心流门控：启用时先入节拍状态并判定；``SKIP`` 直接返回空投递
        结果，``DEFER`` / ``MERGE`` 入缓冲并排程（已存在有效排程时不重复
        排程），仅 ``REPLY`` 走即时回复。调度器不可用时延迟裁决降级为
        立即回复（fail-safe）。

        合成事件：``message.synthetic=True`` 在门控与锁之前恒跳过——返回
        空投递结果，不建会话、不写心流节拍状态、不调用心流判定（与
        ``FLOW_ENABLED`` 无关）；跳过计数累加并记 debug 日志。

        空文本入站（纯图片 / 纯表情附件）：表情收录与心流游标照常执行，
        但**不调用生成**（不产生兜底文案、不写空回合）——返回空投递结果；
        跳过计数累加并记 debug 日志（``empty_text_skip_count``）。

        群聊多人格：启用时按选择结论逐人格应答并逐条投递（每人格自己的
        持久化会话）；选择为空 / 未启用时回退主人格单人格路径。

        Args:
            message: 入站消息（通道中立形态）。

        Returns:
            投递结果（兜底路径亦返回其投递结果；无适配器 / 合成事件 /
            兜底亦失败 / 延迟或合并排程时返回空 message_ids 结果——
            **本方法恒不抛异常**）。
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
        if message.synthetic:
            self._synthetic_skip_count += 1
            logger.debug(
                "合成事件恒不回复（与 FLOW_ENABLED 无关，路由器前置跳过）: "
                "channel={}, chat_id={}, message_id={}, skipped_total={}",
                message.channel.value,
                message.chat_id,
                message.message_id,
                self._synthetic_skip_count,
            )
            return ChannelDeliveryResultModel(
                channel=message.channel, chat_id=message.chat_id, message_ids=[]
            )
        async with self._lock_for(self.session_key(context)):
            try:
                self._chat_group_flags[self.session_key(context)] = message.is_group_chat
                self._chat_owner_flags[self.session_key(context)] = message.is_owner
                group_pool = await self._group_responder_pool(message)
                primary_state: ChatSessionStateModel | None = None
                if group_pool is None:
                    primary_state = await self._get_or_create_session(context)
                    gate_persona_id = primary_state.persona_id
                else:
                    # 群聊多人格：入站表情只收录进主人格（第一位候选，选择序
                    # 稳定原料）的素材库——多应答者下表情归属本身歧义，取首位
                    # 候选是确定性归属；应答侧各人格仍各用自己库的素材。
                    gate_persona_id = group_pool[0].id
                has_sticker = any(
                    attachment.kind is ChannelAttachmentKind.STICKER
                    for attachment in message.attachments
                )
                if has_sticker:
                    await self._ingest_sticker_attachments(
                        message.attachments, gate_persona_id
                    )
                if self._flow_settings is not None:
                    gate_result = await self._apply_flow_gate(
                        context,
                        adapter,
                        message,
                        gate_persona_id,
                    )
                    if gate_result is not None:
                        return gate_result
                if not message.text.strip():
                    # 仅附件入站：表情已收录、心流游标已推进（门控 / 会话
                    # 懒建保持既有顺序），但不进入生成——空文本会触发
                    # ChatService 的 VALIDATION_ERROR 兜底文案并写空回合。
                    self._empty_text_skip_count += 1
                    logger.debug(
                        "空文本入站（跳过生成——表情收录 / 游标照常）: "
                        "channel={}, chat_id={}, message_id={}, skipped_total={}",
                        message.channel.value,
                        message.chat_id,
                        message.message_id,
                        self._empty_text_skip_count,
                    )
                    return ChannelDeliveryResultModel(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        message_ids=[],
                    )
                if group_pool is not None:
                    group_delivery = await self._reply_via_group(
                        context,
                        adapter,
                        group_pool,
                        message.text,
                        reply_to_message_id=message.message_id,
                        include_sticker_reply=has_sticker,
                    )
                    if group_delivery is not None:
                        await self._finalize_flow_state(context)
                        return group_delivery
                    primary_state = await self._get_or_create_session(context)
                if primary_state is None:
                    primary_state = await self._get_or_create_session(context)
                delivery = await self._send_single_reply(
                    adapter,
                    primary_state,
                    context.chat_id,
                    message.text,
                    is_group_chat=message.is_group_chat,
                    is_owner=message.is_owner,
                    reply_to_message_id=message.message_id,
                    include_sticker_reply=has_sticker,
                )
                await self._finalize_flow_state(context)
                return delivery
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
    # 心流门控（判定 + 排程 + 延迟批次执行）
    # ------------------------------------------------------------------

    async def _apply_flow_gate(
        self,
        context: ChannelContextModel,
        adapter: ChannelAdapterProtocol,
        message: InboundMessageModel,
        persona_id: int,
    ) -> ChannelDeliveryResultModel | None:
        """记录入站并执行门控判定（返回非 None 表示本消息已由门控接管）。

        合成事件不会到达本方法（路由器在门控前恒跳过 ``synthetic=True``）；
        判定层的 ``SKIP / SYNTHETIC_EVENT`` 结论保留为纯函数契约。

        仅附件入站（空文本）只推进到达时刻与积压计数，不把空串追加进
        待回复缓冲——连发 / 节律判定照常，合并批次不含空条目。

        Args:
            context: 通道会话上下文。
            adapter: 通道适配器（延迟批次投递面）。
            message: 入站消息。
            persona_id: 关系摘要查询人格（群聊取首选人格 / 单聊取会话人格）。

        Returns:
            ``SKIP`` / 已排程（延迟或合并）时返回空投递结果；``REPLY`` 时
            返回 None（调用方继续即时回复）。
        """
        settings = self._flow_settings
        if settings is None:
            return None
        key = self.session_key(context)
        now = self._clock()
        state = self._flow_states.get(key, FlowChatStateModel())
        if (
            state.pending
            and state.pending_due_ms is not None
            and now - state.pending_due_ms > settings.pending_grace_ms
        ):
            logger.warning(
                "心流延迟任务超期未执行（视为丢失，重新排程）: key={}, due_ms={}, now_ms={}",
                key,
                state.pending_due_ms,
                now,
            )
            state = clear_pending(state)
        state = record_inbound(
            state, message.text, now, append_text=bool(message.text.strip())
        )
        self._flow_states[key] = state
        rhythm = await self._rhythm_snapshot(now)
        relationship = await self._relationship_summary(persona_id, now)
        decision = evaluate_flow_gate(
            FlowGateInputModel(
                now_ms=now,
                is_synthetic=message.synthetic,
                rhythm=rhythm,
                relationship=relationship,
                state=state,
            ),
            settings,
        )
        logger.debug(
            "心流门控裁决: key={}, verdict={}, reason={}, delay_ms={}, backlog={}",
            key,
            decision.verdict.value,
            decision.reason.value,
            decision.delay_ms,
            state.backlog_count,
        )
        if decision.verdict == FlowVerdict.SKIP:
            # 机器事件不进入人类回合，也不清空未读积压与待回复缓冲——
            # 已在排程中的批次仍需原样回复。
            return ChannelDeliveryResultModel(
                channel=message.channel, chat_id=message.chat_id, message_ids=[]
            )
        if state.pending:
            return ChannelDeliveryResultModel(
                channel=message.channel, chat_id=message.chat_id, message_ids=[]
            )
        if decision.verdict == FlowVerdict.REPLY:
            return None
        scheduled = self._schedule_flow_batch(
            context, adapter, delay_ms=decision.delay_ms, now_ms=now
        )
        if not scheduled:
            return None
        return ChannelDeliveryResultModel(
            channel=message.channel, chat_id=message.chat_id, message_ids=[]
        )

    def _schedule_flow_batch(
        self,
        context: ChannelContextModel,
        adapter: ChannelAdapterProtocol,
        *,
        delay_ms: int,
        now_ms: int,
    ) -> bool:
        """把延迟 / 合并批次排程到中央调度器（失败返回 False 走即时回复）。

        Args:
            context: 通道会话上下文。
            adapter: 通道适配器（批次投递面）。
            delay_ms: 延迟毫秒。
            now_ms: 排程时刻（epoch 毫秒）。

        Returns:
            True = 已排程；False = 调度器不可用（调用方立即回复）。
        """
        key = self.session_key(context)
        scheduler = self._scheduler_provider() if self._scheduler_provider is not None else None
        if scheduler is None:
            if not self._scheduler_missing_warned:
                logger.warning(
                    "心流门控需要延迟调度面但调度器未就绪——延迟裁决降级为立即回复"
                    "（FLOW_ENABLED 时请确认中央调度器已装配）"
                )
                self._scheduler_missing_warned = True
            return False
        self._flow_job_seq += 1
        job_name = f"flow:{key}:{self._flow_job_seq}"

        async def _batch_job() -> None:
            """延迟批次作业（到期执行一次——合并且静默窗口满足后回复）。"""
            await self._run_flow_batch(context, adapter, job_name)

        try:
            scheduler.schedule_once(job_name, _batch_job, delay_ms=max(0, delay_ms))
        except Exception:  # 排程面失败不阻断消息——降级即时回复
            logger.opt(exception=True).warning(
                "心流延迟批次排程失败（降级为立即回复）: key={}, job={}", key, job_name
            )
            return False
        state = self._flow_states.get(key, FlowChatStateModel())
        self._flow_states[key] = mark_pending(state, due_ms=now_ms + max(0, delay_ms))
        logger.info(
            "心流延迟批次已排程: key={}, job={}, delay_ms={}, reason_state=pending",
            key,
            job_name,
            delay_ms,
        )
        return True

    async def _run_flow_batch(
        self,
        context: ChannelContextModel,
        adapter: ChannelAdapterProtocol,
        job_name: str,
    ) -> None:
        """执行延迟批次（同聊天串行锁内合并缓冲并回复；失败不击穿调度器）。

        Args:
            context: 通道会话上下文。
            adapter: 通道适配器。
            job_name: 排程作业名（日志面）。
        """
        settings = self._flow_settings
        if settings is None:
            return
        key = self.session_key(context)
        async with self._lock_for(key):
            state = self._flow_states.get(key)
            if state is None:
                logger.debug("心流延迟批次无节拍状态（跳过）: job={}", job_name)
                return
            now = self._clock()
            rhythm = await self._rhythm_snapshot(now)
            if not rhythm.allowed:
                remaining = rhythm.suggested_delay_ms
                self._flow_states[key] = clear_pending(state)
                if self._schedule_flow_batch(
                    context, adapter, delay_ms=remaining, now_ms=now
                ):
                    logger.info(
                        "心流延迟批次遇睡眠窗口（重排至醒来）: job={}, delay_ms={}",
                        job_name,
                        remaining,
                    )
                    return
            quiet = remaining_quiet_ms(state, now, settings)
            if quiet > 0:
                self._flow_states[key] = clear_pending(state)
                if self._schedule_flow_batch(context, adapter, delay_ms=quiet, now_ms=now):
                    logger.info(
                        "心流延迟批次仍有新入站（静默窗口未满，重排）: job={}, delay_ms={}",
                        job_name,
                        quiet,
                    )
                    return
            merged = merge_buffered_texts(state.buffered_texts, settings)
            state = clear_pending(state)
            self._flow_states[key] = state
            if not merged.text.strip():
                logger.debug("心流延迟批次缓冲无可回复文本（跳过）: job={}", job_name)
                return
            if merged.dropped_count > 0:
                logger.info(
                    "心流连发合并超出上限（丢弃最老条目）: job={}, used={}, dropped={}",
                    job_name,
                    merged.used_count,
                    merged.dropped_count,
                )
            is_group = bool(state.recent_arrival_ms) and self._flow_group_hint(key)
            is_owner = self._chat_owner_flags.get(key, True)
            try:
                group_pool = await self._group_responder_pool_for_flag(is_group)
                if group_pool is not None:
                    delivery = await self._reply_via_group(
                        context, adapter, group_pool, merged.text, reply_to_message_id=None
                    )
                    if delivery is None:
                        delivery = await self._reply_with_primary(
                            adapter, context, merged.text, is_group, is_owner
                        )
                else:
                    delivery = await self._reply_with_primary(
                        adapter, context, merged.text, is_group, is_owner
                    )
                logger.info(
                    "心流延迟批次已投递: job={}, used={}, message_ids={}",
                    job_name,
                    merged.used_count,
                    delivery.message_ids,
                )
                await self._finalize_flow_state(context)
            except Exception as exc:
                logger.opt(exception=True).error(
                    "心流延迟批次回复失败（回发兜底文案）: job={}", job_name
                )
                await self._send_fallback(
                    adapter,
                    InboundMessageModel(
                        channel=context.channel,
                        chat_id=context.chat_id,
                        message_id=f"flow:{job_name}",
                        sender_id="0",
                        text=merged.text,
                        is_group_chat=is_group,
                    ),
                    self._fallback_text_for(exc),
                )

    def _flow_group_hint(self, key: str) -> bool:
        """读取该聊天的群聊标记（延迟批次选择应答面）。

        Args:
            key: 会话身份键。

        Returns:
            True = 该聊天最近入站为群聊消息。
        """
        return self._chat_group_flags.get(key, False)

    async def _reply_with_primary(
        self,
        adapter: ChannelAdapterProtocol,
        context: ChannelContextModel,
        text: str,
        is_group_chat: bool,
        is_owner: bool = True,
    ) -> ChannelDeliveryResultModel:
        """以主人格单会话回复（群聊多人格关闭 / 选择为空时的既有路径）。

        Args:
            adapter: 通道适配器。
            context: 通道会话上下文。
            text: 回复输入文本（已合并或单条）。
            is_group_chat: 是否群聊（来源分级）。
            is_owner: 私聊发送者是否为主人（延迟批次沿用该聊天最近入站
                标记；群聊下不参与分级）。

        Returns:
            投递结果。
        """
        state = await self._get_or_create_session(context)
        return await self._send_single_reply(
            adapter,
            state,
            context.chat_id,
            text,
            is_group_chat=is_group_chat,
            is_owner=is_owner,
            reply_to_message_id=None,
        )

    async def _send_single_reply(
        self,
        adapter: ChannelAdapterProtocol,
        state: ChatSessionStateModel,
        chat_id: str,
        user_text: str,
        *,
        is_group_chat: bool,
        reply_to_message_id: str | None,
        is_owner: bool = True,
        include_sticker_reply: bool = False,
    ) -> ChannelDeliveryResultModel:
        """单人格回复并投递（人格上下文绑定 + 来源分级）。

        Args:
            adapter: 通道适配器。
            state: 会话状态。
            chat_id: 目标平台会话 ID。
            user_text: 用户输入文本。
            is_group_chat: 是否群聊（群聊恒按 peer 落档）。
            reply_to_message_id: 引用回复目标（None = 不引用）。
            is_owner: 私聊发送者是否为主人（False = 第三方转发按 peer 落档；
                群聊下不参与分级）。
            include_sticker_reply: 是否随文本附发一张表情（入站带 sticker
                附件且表情桥已装配时生效）。

        Returns:
            投递结果。
        """
        async with bind_persona_context(state.persona_id):
            sender_provenance = (
                MemoryProvenance.OWNER
                if is_owner and not is_group_chat
                else MemoryProvenance.PEER
            )
            reply_text = await self._chat_service_factory().reply(
                state, user_text, sender_provenance=sender_provenance
            )
        attachments: list[ChannelMessageAttachment] = []
        if include_sticker_reply and self._sticker_reply_hook is not None:
            try:
                sticker = await self._sticker_reply_hook()
            except Exception:  # 表情是装饰面——失败不阻断文本回复
                logger.opt(exception=True).warning("通道表情选取失败（跳过附发）")
                sticker = None
            if sticker is not None:
                attachments.append(sticker)
        return await adapter.send_message(
            OutboundMessageModel(
                chat_id=chat_id,
                text=reply_text,
                reply_to_message_id=reply_to_message_id,
                attachments=attachments,
            )
        )

    async def _ingest_sticker_attachments(
        self,
        attachments: Sequence[ChannelMessageAttachment],
        persona_id: int,
    ) -> None:
        """以应答人格上下文收录入站表情（失败仅告警——不阻断回复）。

        Args:
            attachments: 入站附件列表（内部只筛 sticker 类）。
            persona_id: 应答人格主键（桥内数据库写入的人格作用域）。
        """
        hook = self._sticker_ingest_hook
        if hook is None:
            return
        stickers = [
            attachment
            for attachment in attachments
            if attachment.kind is ChannelAttachmentKind.STICKER
        ]
        if not stickers:
            return
        try:
            async with bind_persona_context(persona_id):
                await hook(stickers)
        except Exception:
            logger.opt(exception=True).warning(
                "入站表情收录失败（跳过——不影响回复）: persona_id={}, count={}",
                persona_id,
                len(stickers),
            )

    # ------------------------------------------------------------------
    # 群聊多人格（选择 + 逐人格会话 + 投递归属）
    # ------------------------------------------------------------------

    async def _group_responder_pool(
        self, message: InboundMessageModel
    ) -> list[PersonaRecordDTO] | None:
        """读取群聊多人格候选（未启用 / 单聊 / 无数据库会话时为 None）。

        Args:
            message: 入站消息（群聊标记判定面）。

        Returns:
            启用人格列表（主人格在前、其余按主键升序）；None = 走主人格
            单人格路径（功能关闭 / 单聊 / 无候选 / 读取失败）。
        """
        if self._group_settings is None or not message.is_group_chat:
            return None
        return await self._list_active_personas()

    async def _group_responder_pool_for_flag(
        self, is_group_chat: bool
    ) -> list[PersonaRecordDTO] | None:
        """按群聊标记读取候选（延迟批次执行面的无消息形态）。

        Args:
            is_group_chat: 该聊天是否为群聊。

        Returns:
            启用人格列表；None = 走主人格单人格路径。
        """
        if self._group_settings is None or not is_group_chat:
            return None
        return await self._list_active_personas()

    async def _list_active_personas(self) -> list[PersonaRecordDTO] | None:
        """读取启用人格集合（主人格在前——选择序稳定原料）。

        Returns:
            启用人格列表；无数据库会话 / 空集 / 读取失败时返回 None
            （调用方回退主人格单人格路径）。
        """
        if self._session_factory is None:
            return None
        try:
            async with self._session_factory() as db_session:
                records = await self._persona_service.list_addressable(db_session)
        except Exception:  # 读取失败不阻断回复——回退单人格路径
            logger.opt(exception=True).warning(
                "群聊多人格候选读取失败（回退主人格单人格路径）"
            )
            return None
        if not records:
            return None
        ordered = sorted(records, key=lambda record: (0 if record.is_primary else 1, record.id))
        return ordered

    async def _reply_via_group(
        self,
        context: ChannelContextModel,
        adapter: ChannelAdapterProtocol,
        pool: Sequence[PersonaRecordDTO],
        user_text: str,
        *,
        reply_to_message_id: str | None,
        include_sticker_reply: bool = False,
    ) -> ChannelDeliveryResultModel | None:
        """按轮转 / 冷却 / 重复抑制结论逐人格应答并投递。

        每个应答人格使用自己的持久化会话（标题带 ``:persona:<id>`` 后缀）
        ——会话行 ``ff_chat_sessions.persona_id`` 即该会话全部消息的应答
        归属。逐人格失败隔离：单人格回复 / 投递失败记日志后继续其余人格；
        **全部失败**时抛出首个异常交给外层兜底（用户面仍收到一条短提示）。

        Args:
            context: 通道会话上下文。
            adapter: 通道适配器。
            pool: 启用人格候选（主人格在前）。
            user_text: 回复输入文本（单条或合并文本）。
            reply_to_message_id: 首条应答的引用目标（None = 不引用；其余
                人格的应答不再引用同一入站，避免引用噪声）。
            include_sticker_reply: 每位应答人格是否随文本附发自己库中的
                表情（入站带 sticker 附件且表情桥已装配时生效）。

        Returns:
            聚合投递结果；选择为空时返回 None（调用方回退单人格路径）。
        """
        settings = self._group_settings
        if settings is None:
            return None
        key = self.session_key(context)
        now = self._clock()
        group_state = self._group_states.get(key, GroupResponderStateModel())
        candidates = [
            GroupResponderCandidateModel(
                persona_id=record.id,
                is_primary=record.is_primary,
                last_reply_ms=group_state.last_reply_ms_by_persona.get(record.id),
            )
            for record in pool
        ]
        selection = select_group_responders(
            candidates,
            group_state,
            settings,
            now_ms=now,
            is_group_chat=True,
        )
        if not selection.responder_persona_ids:
            logger.info(
                "群聊多人格选择为空（回退主人格单人格路径）: key={}, reason={}",
                key,
                selection.reason.value,
            )
            return None
        logger.info(
            "群聊多人格应答选择: key={}, responders={}, reason={}, cooling={}",
            key,
            selection.responder_persona_ids,
            selection.reason.value,
            selection.cooling_persona_ids,
        )
        by_id = {record.id: record for record in pool}
        message_ids: list[str] = []
        first_exception: Exception | None = None
        replied: list[int] = []
        updated_last_reply = dict(group_state.last_reply_ms_by_persona)
        for index, persona_id in enumerate(selection.responder_persona_ids):
            record = by_id.get(persona_id)
            if record is None:
                continue
            try:
                state = await self._get_or_create_persona_session(context, record)
                delivery = await self._send_single_reply(
                    adapter,
                    state,
                    context.chat_id,
                    user_text,
                    is_group_chat=True,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                    include_sticker_reply=include_sticker_reply,
                )
            except Exception as exc:  # 逐人格隔离——其余人格照常应答
                logger.opt(exception=True).error(
                    "群聊人格应答失败（继续其余人格）: key={}, persona_id={}",
                    key,
                    persona_id,
                )
                if first_exception is None:
                    first_exception = exc
                continue
            message_ids.extend(delivery.message_ids)
            replied.append(persona_id)
            updated_last_reply[persona_id] = self._clock()
        if not replied:
            if first_exception is not None:
                raise first_exception
            return None
        self._group_states[key] = GroupResponderStateModel(
            last_reply_ms_by_persona=updated_last_reply,
            last_responder_ids=replied,
        )
        return ChannelDeliveryResultModel(
            channel=context.channel,
            chat_id=context.chat_id,
            message_ids=message_ids,
        )

    async def _get_or_create_persona_session(
        self, context: ChannelContextModel, record: PersonaRecordDTO
    ) -> ChatSessionStateModel:
        """按（聊天 × 人格）二元组取在途会话；缺失时懒建并落库。

        Args:
            context: 通道会话上下文。
            record: 应答人格记录（会话归属人格）。

        Returns:
            该人格在该聊天的会话状态（持久化装配时携带 ``session_id``）。
        """
        key = f"{self.session_key(context)}:persona:{record.id}"
        state = self._sessions.get(key)
        if state is not None:
            return state
        service = self._chat_service_factory()
        if self._session_factory is None:
            state = await service.create_session(persona=record, title=key)
        else:
            async with self._session_factory() as db_session:
                state = await service.create_session(
                    persona=record, db_session=db_session, title=key
                )
        self._sessions[key] = state
        logger.info(
            "群聊人格会话已建立: key={}, persona_id={}, session_id={}",
            key,
            record.id,
            state.session_id,
        )
        return state

    # ------------------------------------------------------------------
    # 门控辅助（节律 / 关系 / 状态收尾）
    # ------------------------------------------------------------------

    async def _rhythm_snapshot(self, now_ms: int) -> FlowRhythmStateModel:
        """读取节律门现状（无节律服务 = 允许即时回复、零延迟）。

        Args:
            now_ms: 判定时刻（epoch 毫秒）。

        Returns:
            :class:`~packages.harness.chat.flow_models.FlowRhythmStateModel`
            （节律服务缺失 / 判定失败时按默认放行处理）。
        """
        if self._rhythm_service is None:
            return rhythm_state_from_gate(allowed=True, suggested_delay_ms=0)
        try:
            gate = await self._rhythm_service.gate_reply(now_ms)
        except Exception:  # 节律门失败不阻断回复——按默认放行处理
            logger.opt(exception=True).warning(
                "节律门判定失败（门控按默认放行处理）: now_ms={}", now_ms
            )
            return rhythm_state_from_gate(allowed=True, suggested_delay_ms=0)
        return rhythm_state_from_gate(
            allowed=gate.allowed, suggested_delay_ms=gate.suggested_delay_ms
        )

    async def _relationship_summary(
        self, persona_id: int, now_ms: int
    ) -> RelationshipSummaryModel | None:
        """读取关系摘要（门控输入——未接线 / 读取失败为 None 不参与判定）。

        Args:
            persona_id: 关系归属人格主键。
            now_ms: 判定时刻（epoch 毫秒）。

        Returns:
            关系进展摘要；查询面未接线或失败时返回 None。
        """
        if self._relationship_lookup is None:
            return None
        try:
            return await self._relationship_lookup(persona_id, now_ms)
        except Exception:  # 关系摘要失败不阻断门控——按无关系数据处理
            logger.opt(exception=True).warning(
                "心流门控关系摘要读取失败（按无关系数据处理）: persona_id={}",
                persona_id,
            )
            return None

    async def _finalize_flow_state(self, context: ChannelContextModel) -> None:
        """回复成功后清空该聊天的未读积压与待回复缓冲。

        Args:
            context: 通道会话上下文。
        """
        key = self.session_key(context)
        state = self._flow_states.get(key)
        if state is None:
            return
        self._flow_states[key] = clear_backlog(clear_pending(state))

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
