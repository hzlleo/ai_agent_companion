"""harness/memory retrieval 写读强化测试——检索命中策展行的访问计数推进。

覆盖面：调度条件（reinforcer + 后台管理器双就绪才调度；停机期 / 缺一
则零调度）；命中集过滤（仅 ``ref_type=curated`` 参与、去重排序）；强化
实现落库（access_count + 1 / last_accessed_ms / 人格隔离）；失败吞没
（强化面抛异常不影响检索结果）；检索响应零阻塞（返回先于写入完成）。

DB 经共享 SQLite fixture；存储 / 嵌入器 / 管理器全为假件（零真实网络）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.dtos import CuratedUpsertDTO
from packages.harness.memory.models import CuratedKind, MemoryProvenance
from packages.harness.memory.orm import CuratedMemoryRow
from packages.harness.memory.retrieval import MemoryIndexer, MemorySearchService
from packages.harness.memory.retrieval_models import (
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)
from packages.harness.memory.service import CuratedAccessReinforcer, MemoryService
from packages.harness.memory.sqlite_store import MemoryIndexLiteRow, SQLiteFallbackStore
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context

#: 注入时钟（epoch 毫秒）。
NOW = 1_760_000_000_000


class FakeStore:
    """假检索存储（返回预置命中——捕获检索调用）。"""

    def __init__(self, results: Sequence[MemorySearchResultDTO]) -> None:
        """初始化（预置命中集）。"""
        self._results = list(results)
        self.calls: list[tuple[MemorySearchQueryDTO, list[float] | None]] = []

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[MemorySearchResultDTO]:
        """检索（返回预置命中）。"""
        self.calls.append((query, query_vector))
        return list(self._results)


class FakeEmbedder:
    """假嵌入器（记录调用；返回与输入同长的定长向量）。"""

    def __init__(self) -> None:
        """初始化（空调用清单——真实链路的零调用断言面）。"""
        self.calls: list[list[str]] = []

    async def __call__(self, texts: Sequence[str]) -> Any:
        """批量向量化（固定二维向量 + 记录调用文本）。"""
        self.calls.append(list(texts))
        return _EmbedResponse([[0.1, 0.2] for _ in texts])


class _EmbedResponse:
    """嵌入响应形状（vectors 字段面——最小桩）。"""

    def __init__(self, vectors: list[list[float]]) -> None:
        """初始化（向量列表）。"""
        self.vectors = vectors


class RecordingReinforcer:
    """记录型强化面（捕获 bump 调用——可注入失败）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化（可选失败注入）。"""
        self.calls: list[tuple[int, list[int], int]] = []
        self._fail = fail

    async def bump(
        self, persona_id: int, memory_ids: Sequence[int], accessed_ms: int
    ) -> None:
        """捕获调用（可抛异常）。"""
        self.calls.append((persona_id, list(memory_ids), accessed_ms))
        if self._fail:
            raise RuntimeError("强化失败（桩）")


class StoppedManager:
    """停机态假管理器（is_shutting_down=True——零调度断言面）。"""

    def __init__(self) -> None:
        """初始化（空登记集）。"""
        self.tasks: list[asyncio.Task[Any]] = []

    @property
    def is_shutting_down(self) -> bool:
        """停机标志（恒 True）。"""
        return True

    def register(self, task: asyncio.Task[Any]) -> None:
        """登记任务。"""
        self.tasks.append(task)


def _result(ref_type: MemoryRefType, ref_id: int) -> MemorySearchResultDTO:
    """构造单条检索命中（最小形态）。"""
    return MemorySearchResultDTO(
        ref_type=ref_type,
        ref_id=ref_id,
        kind="memory",
        provenance=MemoryProvenance.OWNER,
        ts_ms=NOW,
        content=f"命中{ref_id}",
        score=1.0,
    )


def _query(persona_id: int = 7) -> MemorySearchQueryDTO:
    """构造关键词检索查询（零向量依赖）。"""
    return MemorySearchQueryDTO(
        persona_id=persona_id, query="汤面", mode=MemorySearchMode.KEYWORD
    )


async def test_search_schedules_only_curated_hits() -> None:
    """调度过滤：仅 curated 命中进入强化批次（episode 不强化；去重排序）。"""
    reinforcer = RecordingReinforcer()
    manager = BackgroundTaskManager()
    service = MemorySearchService(
        store=FakeStore(
            [
                _result(MemoryRefType.EPISODE, 11),
                _result(MemoryRefType.CURATED, 5),
                _result(MemoryRefType.CURATED, 3),
                _result(MemoryRefType.CURATED, 5),
            ]
        ),
        embedder=FakeEmbedder(),
        reinforcer=reinforcer,
        background_manager=manager,
        clock=lambda: NOW,
    )
    results = await service.search(_query())
    assert len(results) == 4
    assert len(manager.tasks) == 1
    await asyncio.gather(*list(manager.tasks))
    assert reinforcer.calls == [(7, [3, 5], NOW)]


async def test_search_without_manager_skips_reinforcement() -> None:
    """缺后台管理器：即使注入强化面也零调度（不阻塞检索响应）。"""
    reinforcer = RecordingReinforcer()
    service = MemorySearchService(
        store=FakeStore([_result(MemoryRefType.CURATED, 5)]),
        embedder=FakeEmbedder(),
        reinforcer=reinforcer,
    )
    await service.search(_query())
    assert reinforcer.calls == []


async def test_search_without_reinforcer_skips_reinforcement() -> None:
    """缺强化面：零调度（默认形态——既有行为零变更）。"""
    manager = BackgroundTaskManager()
    service = MemorySearchService(
        store=FakeStore([_result(MemoryRefType.CURATED, 5)]),
        embedder=FakeEmbedder(),
        background_manager=manager,
    )
    await service.search(_query())
    assert manager.tasks == set()


async def test_search_shutting_down_manager_skips_reinforcement() -> None:
    """停机期：不新起强化任务。"""
    reinforcer = RecordingReinforcer()
    manager = StoppedManager()
    service = MemorySearchService(
        store=FakeStore([_result(MemoryRefType.CURATED, 5)]),
        embedder=FakeEmbedder(),
        reinforcer=reinforcer,
        background_manager=manager,
    )
    await service.search(_query())
    assert manager.tasks == []


async def test_search_only_episode_hits_skips_reinforcement() -> None:
    """仅 episode 命中：零强化任务（情景层无访问计数列）。"""
    reinforcer = RecordingReinforcer()
    manager = BackgroundTaskManager()
    service = MemorySearchService(
        store=FakeStore([_result(MemoryRefType.EPISODE, 1)]),
        embedder=FakeEmbedder(),
        reinforcer=reinforcer,
        background_manager=manager,
    )
    await service.search(_query())
    assert manager.tasks == set()


async def test_search_survives_reinforcer_failure() -> None:
    """强化面失败：后台任务吞没异常，检索结果正常返回。"""
    reinforcer = RecordingReinforcer(fail=True)
    manager = BackgroundTaskManager()
    service = MemorySearchService(
        store=FakeStore([_result(MemoryRefType.CURATED, 5)]),
        embedder=FakeEmbedder(),
        reinforcer=reinforcer,
        background_manager=manager,
        clock=lambda: NOW,
    )
    results = await service.search(_query())
    assert [result.ref_id for result in results] == [5]
    await asyncio.gather(*list(manager.tasks))
    assert reinforcer.calls == [(7, [5], NOW)]


# ---------------------------------------------------------------------------
# 强化实现落库（共享 SQLite）+ 人格隔离
# ---------------------------------------------------------------------------


async def _create_persona(db_session: Any, name: str) -> PersonaRecordDTO:
    """经真实 PersonaService 创建人格记录。"""
    return await PersonaService().create(
        db_session,
        PersonaCreateDTO.model_validate(
            {"name": name, "status": "active", "card": {"name": "大肥鱼"}}
        ),
    )


@pytest.fixture
async def reinforce_env(
    shared_db_engine: AsyncEngine,
) -> Any:
    """强化落库环境（共享引擎外部安装到 holder；双人格 + 各一条策展记忆）。

    Yields:
        ``(session_factory, persona_a, persona_b, curated_a_id, curated_b_id,
        db_session)`` 六元组（holder 用后复位）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    session_factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
    memory_service = MemoryService()
    async with session_factory() as db_session:
        persona_a = await _create_persona(db_session, "reinforce-a")
        persona_b = await _create_persona(db_session, "reinforce-b")
        async with bind_persona_context(persona_a.id):
            record_a = await memory_service.upsert_curated(
                db_session,
                CuratedUpsertDTO(
                    kind=CuratedKind.MEMORY, topic="口味偏好", content="喜欢汤面"
                ),
            )
        async with bind_persona_context(persona_b.id):
            record_b = await memory_service.upsert_curated(
                db_session,
                CuratedUpsertDTO(
                    kind=CuratedKind.MEMORY, topic="口味偏好", content="喜欢米饭"
                ),
            )
        yield session_factory, persona_a, persona_b, record_a.id, record_b.id, db_session
    db_engine_holder.reset()


async def test_reinforcer_bumps_access_count_and_ts(reinforce_env: Any) -> None:
    """强化落库：access_count + 1、last_accessed_ms = 命中时刻。"""
    session_factory, persona_a, _, curated_a_id, _, db_session = reinforce_env
    reinforcer = CuratedAccessReinforcer(
        session_factory=session_factory, memory_service=MemoryService()
    )
    await reinforcer.bump(persona_a.id, [curated_a_id], NOW)
    await reinforcer.bump(persona_a.id, [curated_a_id], NOW + 5)

    async with bind_persona_context(persona_a.id):
        row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == curated_a_id)
            )
        ).scalar_one()
    assert row.access_count == 2
    assert row.last_accessed_ms == NOW + 5


async def test_reinforcer_persona_isolation(reinforce_env: Any) -> None:
    """人格隔离：他人格 id 静默不命中（0 行更新，计数不变）。"""
    session_factory, persona_a, _, curated_a_id, curated_b_id, db_session = reinforce_env
    reinforcer = CuratedAccessReinforcer(
        session_factory=session_factory, memory_service=MemoryService()
    )
    await reinforcer.bump(persona_a.id, [curated_b_id], NOW)

    async with bind_persona_context(persona_a.id):
        row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == curated_a_id)
            )
        ).scalar_one()
    async with bind_persona_context(persona_a.id):
        other = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == curated_b_id)
            )
        ).scalars().first()
    assert row.access_count == 0
    assert other is None  # 他人格行在本作用域不可见


async def test_reinforcer_empty_ids_noop(reinforce_env: Any) -> None:
    """空命中集：零查询零写（短路——不打开空事务）。"""
    session_factory, persona_a, _, _, _, _ = reinforce_env
    reinforcer = CuratedAccessReinforcer(
        session_factory=session_factory, memory_service=MemoryService()
    )
    await reinforcer.bump(persona_a.id, [], NOW)


async def test_reinforcer_swallows_errors(reinforce_env: Any) -> None:
    """失败吞没：会话工厂抛异常时 bump 不向上抛（协议契约）。"""
    def broken_factory() -> Any:
        """恒抛异常的会话工厂。"""
        raise RuntimeError("工厂失败（桩）")

    reinforcer = CuratedAccessReinforcer(
        session_factory=broken_factory, memory_service=MemoryService()
    )
    await reinforcer.bump(1, [1], NOW)


# ---------------------------------------------------------------------------
# 真链集成（回退存储端到端——curated upsert → 索引 → 检索 → 强化 →
# supersede 键轮转 → 删除收口；零 FakeStore）
# ---------------------------------------------------------------------------


async def _curated_index_rows(
    session_factory: Any, persona_id: int
) -> set[tuple[int, str]]:
    """读取策展索引行集合（回退表——引用键 + 正文断言面）。"""
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryIndexLiteRow).where(
                        MemoryIndexLiteRow.persona_id == persona_id,
                        MemoryIndexLiteRow.ref_type == "curated",
                    )
                )
            )
            .scalars()
            .all()
        )
    return {(row.ref_id, row.content) for row in rows}


async def test_curated_pipeline_index_search_reinforce_rotate_delete(
    shared_db_engine: AsyncEngine,
) -> None:
    """真链：upsert → 索引行 → 检索命中 → 强化落库 → supersede 轮转 → 删除。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    try:
        session_factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
        embedder = FakeEmbedder()
        store = SQLiteFallbackStore()
        index_manager = BackgroundTaskManager()
        indexer = MemoryIndexer(
            embedder=embedder, store=store, background_manager=index_manager
        )
        memory_service = MemoryService(indexer=indexer, clock=lambda: NOW)
        reinforce_manager = BackgroundTaskManager()
        search_service = MemorySearchService(
            store=store,
            embedder=embedder,
            reinforcer=CuratedAccessReinforcer(
                session_factory=session_factory, memory_service=MemoryService()
            ),
            background_manager=reinforce_manager,
            clock=lambda: NOW,
        )
        async with session_factory() as db_session:
            persona = await _create_persona(db_session, "curated-pipeline")

            # ① curated upsert → 索引条目落库（回退存储 keyword 面）。
            async with bind_persona_context(persona.id):
                first = await memory_service.upsert_curated(
                    db_session,
                    CuratedUpsertDTO(
                        kind=CuratedKind.MEMORY, topic="口味偏好", content="喜欢汤面"
                    ),
                )
            await asyncio.gather(*list(index_manager.tasks))
            assert embedder.calls == []  # 回退存储能力面短路——零 embed 用量
            assert await _curated_index_rows(session_factory, persona.id) == {
                (first.id, "口味偏好：喜欢汤面")
            }

            # ② 检索命中策展行 → 后台强化推进访问计数。
            results = await search_service.search(_query(persona.id))
            curated_hits = [
                result
                for result in results
                if result.ref_type == MemoryRefType.CURATED
            ]
            assert [hit.ref_id for hit in curated_hits] == [first.id]
            assert reinforce_manager.tasks
            await asyncio.gather(*list(reinforce_manager.tasks))
            async with session_factory() as check_session, bind_persona_context(
                persona.id
            ):
                refreshed = (
                    await check_session.execute(
                        select(CuratedMemoryRow).where(
                            CuratedMemoryRow.id == first.id
                        )
                    )
                ).scalar_one()
            assert refreshed.access_count == 1
            assert refreshed.last_accessed_ms == NOW

            # ③ supersede：旧索引条目删除、新条目落库（引用键轮转）。
            async with bind_persona_context(persona.id):
                second = await memory_service.upsert_curated(
                    db_session,
                    CuratedUpsertDTO(
                        kind=CuratedKind.MEMORY, topic="口味偏好", content="喜欢米饭"
                    ),
                )
            await asyncio.gather(*list(index_manager.tasks))
            assert second.id != first.id
            assert await _curated_index_rows(session_factory, persona.id) == {
                (second.id, "口味偏好：喜欢米饭")
            }

            # ④ 删除策展记忆：索引条目同步移除。
            async with bind_persona_context(persona.id):
                await memory_service.delete_curated(db_session, second.id)
            await asyncio.gather(*list(index_manager.tasks))
            assert await _curated_index_rows(session_factory, persona.id) == set()
    finally:
        db_engine_holder.reset()
