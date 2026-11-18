"""harness/memory episode_sink + ChatService 情景落库接线测试。

覆盖面：sink 落库形态（kind=chat / provenance 按角色 / 载荷两侧文本 /
ts 与 session 关联 / 不去重——重复回合各落一行）；fire-and-forget 分发
（假后台管理器登记任务）；失败吞没（记忆服务抛异常不进回复链路）；
ChatService 接线（reply 用户 + 助手回合各一条情景事件、未接线零写入、
sink 异常时回复仍正常返回）。

DB 经共享 SQLite fixture；网关 / 管理器全为假件（零真实网络）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.service import ChatService
from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
)
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.episode_sink import CHAT_TURN_SOURCE, ChatTurnEpisodeSink
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.orm import MemoryEpisodeRow
from packages.harness.memory.service import ChatSessionService, MemoryService
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context

#: 注入时钟（epoch 毫秒）。
NOW = 1_760_000_000_000


class FakeGateway:
    """假 LLM 网关（chat / stream 各一 canned 回复——零真实网络）。"""

    def __init__(self, *, content: str = "假回复") -> None:
        """初始化（回复文本固定）。"""
        self._content = content

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（返回固定文本）。"""
        return ChatResponseModel(content=self._content)

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> Any:
        """流式假调用（单块产出固定文本）。"""
        yield ChatStreamChunkModel(content=self._content)


class RecordingBackgroundManager:
    """假后台任务管理器（登记任务——测试显式 drain 观测）。"""

    def __init__(self) -> None:
        """初始化（空任务集）。"""
        self.tasks: list[asyncio.Task[None]] = []

    @property
    def is_shutting_down(self) -> bool:
        """停机标志（恒 False——测试面不模拟停机）。"""
        return False

    def register(self, task: asyncio.Task[Any]) -> None:
        """登记任务（drain 面）。"""
        self.tasks.append(task)

    async def drain(self) -> None:
        """显式等待全部登记任务完成（测试同步观测点）。"""
        if self.tasks:
            await asyncio.gather(*self.tasks)


class FailingMemoryService(MemoryService):
    """恒失败的记忆服务（append_episodes 抛异常——sink 吞没断言面）。"""

    async def append_episodes(
        self, session: AsyncSession, dtos: Sequence[EpisodeCreateDTO], *, dedup: bool = False
    ) -> list[Any]:
        """恒抛异常（模拟情景写入失败）。"""
        raise RuntimeError("情景写入失败（桩）")


class RaisingEpisodeSink:
    """恒抛异常的 sink（ChatService 显式兜底断言面）。"""

    def __init__(self) -> None:
        """初始化（空调用记录）。"""
        self.calls: list[tuple[Any, ...]] = []

    async def record_chat_turn(
        self,
        persona_id: int | str,
        session_id: int,
        role: str,
        content: str,
        ts_ms: int,
    ) -> None:
        """记录调用后抛异常（违反协议——防御面验证）。"""
        self.calls.append((persona_id, session_id, role, content, ts_ms))
        raise RuntimeError("sink 异常（桩）")


class RecordingEpisodeSink:
    """记录型 sink（ChatService 接线断言面）。"""

    def __init__(self) -> None:
        """初始化（空调用记录）。"""
        self.calls: list[tuple[Any, ...]] = []

    async def record_chat_turn(
        self,
        persona_id: int | str,
        session_id: int,
        role: str,
        content: str,
        ts_ms: int,
    ) -> None:
        """记录调用（不落库——接线面断言）。"""
        self.calls.append((persona_id, session_id, role, content, ts_ms))


async def _count_episodes(session: AsyncSession) -> int:
    """统计情景事件行数（跳过人格自动过滤——管理面口径）。"""
    stmt = select(func.count()).select_from(MemoryEpisodeRow)
    return int(
        (await session.execute(stmt, execution_options={"skip_persona_filter": True})).scalar_one()
    )


async def _create_persona(
    db_session: AsyncSession, name: str = "sink-feiyu"
) -> PersonaRecordDTO:
    """经真实 PersonaService 创建人格记录。"""
    return await PersonaService().create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": name,
                "status": "active",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )


@pytest.fixture
async def sink_env(
    shared_db_engine: AsyncEngine,
) -> Any:
    """Sink 环境（共享引擎外部安装到 holder；主人格铺底）。

    Yields:
        ``(session_factory, memory_service, persona, db_session)`` 四元组
        （holder 用后复位）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    session_factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
    async with session_factory() as db_session:
        persona = await _create_persona(db_session)
        yield session_factory, MemoryService(), persona, db_session
    db_engine_holder.reset()


# ---------------------------------------------------------------------------
# sink 落库形态
# ---------------------------------------------------------------------------


async def test_sink_persists_owner_and_agent_turns(sink_env: Any) -> None:
    """sink 落库：user → owner / assistant → agent；载荷两侧文本与关联键正确。"""
    session_factory, memory_service, persona, db_session = sink_env
    sink = ChatTurnEpisodeSink(session_factory=session_factory, memory_service=memory_service)
    await sink.record_chat_turn(persona.id, 42, "user", "今天有点累", NOW)
    await sink.record_chat_turn(persona.id, 42, "assistant", "那早点休息", NOW + 1)

    async with bind_persona_context(persona.id):
        rows = list(
            (await db_session.execute(select(MemoryEpisodeRow).order_by(MemoryEpisodeRow.id)))
            .scalars()
            .all()
        )
    assert [row.kind for row in rows] == [EpisodeKind.CHAT.value, EpisodeKind.CHAT.value]
    assert [row.provenance for row in rows] == [
        MemoryProvenance.OWNER.value,
        MemoryProvenance.AGENT.value,
    ]
    assert [row.source for row in rows] == [CHAT_TURN_SOURCE, CHAT_TURN_SOURCE]
    user_payload = ChatTurnPayloadModel.model_validate(rows[0].content)
    assistant_payload = ChatTurnPayloadModel.model_validate(rows[1].content)
    assert user_payload.user_content == "今天有点累"
    assert user_payload.assistant_content == ""
    assert assistant_payload.user_content == ""
    assert assistant_payload.assistant_content == "那早点休息"
    assert user_payload.session_id == 42
    assert [row.ts_ms for row in rows] == [NOW, NOW + 1]


async def test_sink_does_not_dedup_repeated_turns(sink_env: Any) -> None:
    """不去重：同内容重复回合各落一行（真实事件重复 ≠ 摄入重复）。"""
    session_factory, memory_service, persona, db_session = sink_env
    sink = ChatTurnEpisodeSink(session_factory=session_factory, memory_service=memory_service)
    await sink.record_chat_turn(persona.id, 7, "assistant", "好的", NOW)
    await sink.record_chat_turn(persona.id, 7, "assistant", "好的", NOW + 1)
    assert await _count_episodes(db_session) == 2


async def test_sink_skips_empty_content(sink_env: Any) -> None:
    """空正文回合跳过（不产生空情景行——防御分支）。"""
    session_factory, memory_service, persona, db_session = sink_env
    sink = ChatTurnEpisodeSink(session_factory=session_factory, memory_service=memory_service)
    await sink.record_chat_turn(persona.id, 7, "user", "", NOW)
    assert await _count_episodes(db_session) == 0


async def test_sink_fire_and_forget_registers_task(sink_env: Any) -> None:
    """fire-and-forget：管理器登记任务；drain 后事件落库。"""
    session_factory, memory_service, persona, db_session = sink_env
    manager = RecordingBackgroundManager()
    sink = ChatTurnEpisodeSink(
        session_factory=session_factory,
        memory_service=memory_service,
        background_manager=manager,
    )
    await sink.record_chat_turn(persona.id, 9, "user", "后台写入", NOW)
    assert len(manager.tasks) == 1
    await manager.drain()
    assert await _count_episodes(db_session) == 1


async def test_sink_swallows_memory_failure(sink_env: Any) -> None:
    """失败吞没：记忆服务抛异常时 sink 不向调用方抛出。"""
    session_factory, _, persona, _ = sink_env
    sink = ChatTurnEpisodeSink(
        session_factory=session_factory, memory_service=FailingMemoryService()
    )
    await sink.record_chat_turn(persona.id, 1, "user", "会失败", NOW)


# ---------------------------------------------------------------------------
# ChatService 接线
# ---------------------------------------------------------------------------


async def test_chat_reply_records_turns_via_sink(sink_env: Any) -> None:
    """reply 接线：用户 + 助手回合各一条 sink 调用（顺序 / 角色 / 会话键）。"""
    _, _, persona, db_session = sink_env
    recording = RecordingEpisodeSink()
    service = ChatService(
        gateway=FakeGateway(),
        session_service=ChatSessionService(),
        episode_sink=recording,
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    await service.reply(state, "在吗")
    assert [(call[1], call[2]) for call in recording.calls] == [
        (state.session_id, ChatMessageRole.USER.value),
        (state.session_id, ChatMessageRole.ASSISTANT.value),
    ]
    assert [call[3] for call in recording.calls] == ["在吗", "假回复"]
    assert all(call[0] == persona.id for call in recording.calls)


async def test_chat_stream_reply_records_turns_via_sink(sink_env: Any) -> None:
    """stream_reply 接线：流式完成后助手回合同样进 sink（用户回合先落）。"""
    _, _, persona, db_session = sink_env
    recording = RecordingEpisodeSink()
    service = ChatService(
        gateway=FakeGateway(),
        session_service=ChatSessionService(),
        episode_sink=recording,
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    async for _ in service.stream_reply(state, "在吗"):
        pass
    assert [call[2] for call in recording.calls] == [
        ChatMessageRole.USER.value,
        ChatMessageRole.ASSISTANT.value,
    ]


async def test_chat_without_sink_has_zero_episode_rows(sink_env: Any) -> None:
    """未接线 sink：reply 正常、情景表零行（既有行为零变更）。"""
    _, _, persona, db_session = sink_env
    service = ChatService(
        gateway=FakeGateway(), session_service=ChatSessionService()
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    assert await service.reply(state, "无 sink") == "假回复"
    assert await _count_episodes(db_session) == 0


async def test_chat_reply_survives_raising_sink(sink_env: Any) -> None:
    """sink 违约抛异常：回复仍返回（显式兜底——记忆面不进回复失败路径）。"""
    _, _, persona, db_session = sink_env
    raising = RaisingEpisodeSink()
    service = ChatService(
        gateway=FakeGateway(),
        session_service=ChatSessionService(),
        episode_sink=raising,
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    assert await service.reply(state, "会抛") == "假回复"
    assert len(raising.calls) == 2


async def test_sink_with_real_background_manager_register_path(
    sink_env: Any,
) -> None:
    """真实管理器路径：register 可调用（停机标志面与 sink 判定兼容）。"""
    session_factory, memory_service, persona, db_session = sink_env
    manager = BackgroundTaskManager()
    sink = ChatTurnEpisodeSink(
        session_factory=session_factory,
        memory_service=memory_service,
        background_manager=manager,
    )
    await sink.record_chat_turn(persona.id, 3, "user", "真实管理器", NOW)
    assert len(manager.tasks) == 1
    await asyncio.gather(*list(manager.tasks))
    assert await _count_episodes(db_session) == 1
