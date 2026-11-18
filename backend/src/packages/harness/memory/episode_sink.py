"""memory 域 chat 回合情景落库面——EpisodeSink（chat → episodic 管道闭合）。

定位（对话回合 → 情景层的记忆管道）：陪伴对话回合是情景层的主要事件来源，
``ChatService`` 在消息行落库后经本模块的 sink 把该回合并入
``ff_memory_episodes``（kind=chat）——情景层由此获得真实对话数据，
检索索引 / 夜间梦境 / 生命周期巩固全部消费该管道。

装配面（app 层单一真源）：

    - ``app.state.memory_service``（索引管道已绑定）→ 本模块 sink 注入
      同一服务实例（对话回合落库后同步触发检索索引，fire-and-forget）；
    - sink 经 ``app.state`` 传入通道 ChatService 工厂（逐消息服务构造）；
    - **未接线即零行为变化**：CLI / 单元测试 / 未装配内存服务的形态下
      ``ChatService`` 不持有 sink（``None``——不产生任何情景写入）。

写入语义（锁定决策）：

    - **回合已落库后写入**（``ChatService._persist_turn`` 提交消息行之后
      ——消息是事实源，情景事件是派生镜像；消息落库失败即不产生事件）；
    - **provenance 按角色**：user 回合 = ``owner``（主人说的——最高可信，
      策展层准入）；assistant 回合 = ``agent``（智能体产出——可信，策展层
      准入）；其余角色按 ``agent``（防御分支，chat 链路实际只产生
      user / assistant）；
    - **不参与内容指纹去重**（``dedup=False``）：每一回合都是独立发生的
      事件，重复短句（「好」「嗯」）是真实事件的重复而非摄入重复；
    - **失败仅日志**：情景写入失败（DB / 索引）不影响回复链路——sink
      ``record_chat_turn`` 恒不抛异常（协议契约 + 实现吞没）。

分发语义：注入 ``background_manager`` 时 fire-and-forget（对话回复不等
情景落库——记忆管道不应拖慢陪伴体验；停机经管理器 drain 收口）；未
注入时内联 await（CLI / 确定性测试路径——写入结果可同步观测）。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from loguru import logger

from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.service import MemoryService, SessionFactory
from packages.harness.persona.service import bind_persona_context

__all__ = ["CHAT_TURN_SOURCE", "EpisodeSinkFace", "ChatTurnEpisodeSink"]

#: 情景事件来源标识（kind=chat 行的 source——对话回合管道）。
CHAT_TURN_SOURCE: str = "chat"


class EpisodeSinkFace(Protocol):
    """chat 回合情景落库面协议（ChatService 的可选注入点）。

    实现契约：``record_chat_turn`` **恒不抛异常**（失败仅日志——情景
    写入是记忆管道面，绝不进入回复链路的失败路径）；调用方在消息行
    提交后调用（回合持久化是前提）。
    """

    async def record_chat_turn(
        self,
        persona_id: int | str,
        session_id: int,
        role: str,
        content: str,
        ts_ms: int,
        *,
        user_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> None:
        """记录单条 chat 回合为情景事件（kind=chat）。

        Args:
            persona_id: 会话归属人格主键（情景行人格作用域）。
            session_id: 会话主键（载荷关联字段）。
            role: 回合角色（user / assistant——决定 provenance）。
            content: 回合可见正文（情景载荷对应侧文本）。
            ts_ms: 回合时间（epoch 毫秒——与消息行同源）。
            user_provenance: user 回合的来源分级（私聊 owner / 群聊
                peer——群聊同侪的发言不晋升策展层与事实证据面）。
        """
        ...


class ChatTurnEpisodeSink:
    """情景落库面实现（MemoryService.append_episodes 承载）。

    装配：``session_factory``（生产 = ``session_scope``）+ 索引已绑定的
    ``MemoryService`` + 可选 ``background_manager``（fire-and-forget）。
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        memory_service: MemoryService,
        background_manager: BackgroundTaskManager | None = None,
    ) -> None:
        """初始化 sink。

        Args:
            session_factory: 会话工厂（``session_scope`` 同形——每次
                写入自开短事务）。
            memory_service: 记忆域服务（索引绑定形态——事件落库后触发
                检索索引链）。
            background_manager: 后台任务管理器（None = 内联 await——
                确定性路径；绑定 = fire-and-forget）。
        """
        self._session_factory = session_factory
        self._memory = memory_service
        self._background_manager = background_manager

    async def record_chat_turn(
        self,
        persona_id: int | str,
        session_id: int,
        role: str,
        content: str,
        ts_ms: int,
        *,
        user_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> None:
        """记录单条 chat 回合为情景事件（恒不抛异常）。

        空正文回合直接跳过（模型空回复 / 操作员空输入在回复链路上已被
        拒绝——防御分支不产生空情景行）。

        Args:
            persona_id: 会话归属人格主键。
            session_id: 会话主键。
            role: 回合角色（user / assistant）。
            content: 回合可见正文。
            ts_ms: 回合时间（epoch 毫秒）。
            user_provenance: user 回合的来源分级（私聊 owner / 群聊 peer）。
        """
        if not content:
            return
        dto = EpisodeCreateDTO(
            kind=EpisodeKind.CHAT,
            source=CHAT_TURN_SOURCE,
            content=self._turn_payload(role, content, session_id),
            provenance=self._provenance_for_role(role, user_provenance),
            ts_ms=ts_ms,
        )
        if self._should_fire_and_forget():
            self._schedule(persona_id, dto)
            return
        await self._persist(persona_id, dto)

    @staticmethod
    def _turn_payload(
        role: str, content: str, session_id: int
    ) -> ChatTurnPayloadModel:
        """按角色构建 chat 回合载荷（user / assistant 两侧文本位）。

        Args:
            role: 回合角色。
            content: 回合正文。
            session_id: 会话主键。

        Returns:
            chat 回合载荷（非用户回合填 assistant_content）。
        """
        if role == ChatMessageRole.USER.value:
            return ChatTurnPayloadModel(
                user_content=content, assistant_content="", session_id=session_id
            )
        return ChatTurnPayloadModel(
            user_content="", assistant_content=content, session_id=session_id
        )

    @staticmethod
    def _provenance_for_role(role: str, user_provenance: MemoryProvenance) -> MemoryProvenance:
        """按角色判定来源可信度（user=透传的发送者分级；其余=agent）。

        Args:
            role: 回合角色。
            user_provenance: user 回合的来源分级（owner 私聊 / peer 群聊）。

        Returns:
            来源可信度（user 回合 = 透传值；assistant 回合 = agent；
            untrusted 永不出现于 chat 回合）。
        """
        if role == ChatMessageRole.USER.value:
            return user_provenance
        return MemoryProvenance.AGENT

    def _should_fire_and_forget(self) -> bool:
        """判定分发形态（管理器绑定且非停机期 → fire-and-forget）。

        Returns:
            是否 fire-and-forget（停机期降级内联路径）。
        """
        manager = self._background_manager
        return manager is not None and not manager.is_shutting_down

    def _schedule(self, persona_id: int | str, dto: EpisodeCreateDTO) -> None:
        """调度后台写入（create_task + register——停机 drain 收口）。

        Args:
            persona_id: 会话归属人格主键。
            dto: 情景事件创建 DTO。
        """
        manager = self._background_manager
        if manager is None:  # pragma: no cover - _should_fire_and_forget 已保证非 None
            return
        task: asyncio.Task[None] = asyncio.create_task(self._persist(persona_id, dto))
        manager.register(task)

    async def _persist(self, persona_id: int | str, dto: EpisodeCreateDTO) -> None:
        """落库情景事件（自开短事务 + 人格上下文；失败仅日志）。

        Args:
            persona_id: 会话归属人格主键。
            dto: 情景事件创建 DTO。
        """
        try:
            async with self._session_factory() as session, bind_persona_context(persona_id):
                await self._memory.append_episodes(session, [dto])
        except Exception:  # best-effort: 情景写入失败不进回复链路（协议契约）
            logger.opt(exception=True).warning(
                "chat 回合情景落库失败（已吞没——不影响回复）: persona={}, session={}",
                persona_id,
                dto.content.session_id if isinstance(dto.content, ChatTurnPayloadModel) else None,
            )
