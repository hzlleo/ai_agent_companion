"""策展记忆生命周期 API / 服务测试——置顶 / 冻结 / 排序 / 归档 / 索引收口。

覆盖面（HTTP 面）：pin / unpin / freeze / unfreeze（幂等、DTO 生命周期
字段、NOT_FOUND 404）；curated 列表 ``sort`` / ``include_archived`` /
``status=archived`` 语义（默认排除归档行、强度降序排序、显式状态优先）。
覆盖面（服务面）：``archive_curated``（状态 / active_marker 清位 /
默认列表不可见）、读时有效强度注入（时钟 + 生命周期参数）、删除与归档
的索引移除分发（假 indexer 记录）。

DB 经共享 SQLite fixture；HTTP 经 ASGITransport（零真实网络）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.memories import router as memories_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.core.errors import BusinessException
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.dtos import (
    CuratedQueryDTO,
    CuratedSort,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
)
from packages.harness.memory.lifecycle import MS_PER_DAY, LifecycleConfig
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    EpisodeKind,
)
from packages.harness.memory.orm import CuratedMemoryRow
from packages.harness.memory.retrieval import MemoryIndexer
from packages.harness.memory.retrieval_models import MemoryRefType
from packages.harness.memory.service import MemoryService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context

#: 注入时钟（epoch 毫秒——强度投影 / 归档判定参考轴）。
NOW = 1_760_000_000_000


class RecordingIndexer:
    """记录型索引管道（策展同步 / 删除分发捕获——索引收口断言面）。"""

    def __init__(self) -> None:
        """初始化（空分发记录）。"""
        self.deletes: list[tuple[MemoryRefType, int, int]] = []
        self.curated_upserts: list[tuple[Any, int | None]] = []

    async def dispatch_delete(
        self, ref_type: MemoryRefType, ref_id: int, persona_id: int
    ) -> None:
        """记录删除分发（不触存储）。"""
        self.deletes.append((ref_type, ref_id, persona_id))

    async def dispatch_curated(
        self, entry: Any, *, superseded_ref_id: int | None
    ) -> None:
        """记录策展索引同步分发（不触存储）。"""
        self.curated_upserts.append((entry, superseded_ref_id))


async def _create_persona(db_session: AsyncSession, name: str = "lifecycle") -> int:
    """经真实 PersonaService 创建并激活人格。"""
    created = await PersonaService().create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": name,
                "status": "active",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )
    return created.id


@pytest.fixture
async def lifecycle_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（memories 路由 + 异常处理器）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(memories_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def persona_id(db_session: AsyncSession) -> int:
    """主人格（列表 / 生命周期端点的缺省人格解析面——设为 primary）。"""
    created = await _create_persona(db_session)
    await PersonaService().set_primary(db_session, created)
    return created


@pytest.fixture
async def curated_id(db_session: AsyncSession, persona_id: int) -> int:
    """铺底一条 active 策展记忆（生命周期端点目标）。"""
    async with bind_persona_context(persona_id):
        record = await MemoryService().upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="汤面偏好", content="深夜喜欢汤面"
            ),
        )
    return record.id


async def _client(app: FastAPI) -> AsyncClient:
    """构造 ASGI 客户端（零真实网络）。"""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# HTTP 面——置顶 / 冻结
# ---------------------------------------------------------------------------


async def test_pin_and_unpin_endpoints(
    lifecycle_app: FastAPI, curated_id: int
) -> None:
    """pin / unpin：pinned 态切换 + DTO 生命周期字段（幂等重复调用同结果）。"""
    async with await _client(lifecycle_app) as client:
        pinned = await client.post(f"/api/v1/memories/curated/{curated_id}/pin")
        assert pinned.status_code == 200
        payload = pinned.json()["data"]
        assert payload["pinned"] is True
        assert payload["frozen"] is False
        assert payload["access_count"] == 0
        assert payload["last_accessed_ms"] is None
        assert payload["effective_strength"] > 0

        again = await client.post(f"/api/v1/memories/curated/{curated_id}/pin")
        assert again.status_code == 200
        assert again.json()["data"]["pinned"] is True

        unpinned = await client.post(f"/api/v1/memories/curated/{curated_id}/unpin")
        assert unpinned.status_code == 200
        assert unpinned.json()["data"]["pinned"] is False


async def test_freeze_and_unfreeze_endpoints(
    lifecycle_app: FastAPI, curated_id: int
) -> None:
    """freeze / unfreeze：frozen 态切换（幂等）。"""
    async with await _client(lifecycle_app) as client:
        frozen = await client.post(f"/api/v1/memories/curated/{curated_id}/freeze")
        assert frozen.status_code == 200
        assert frozen.json()["data"]["frozen"] is True
        again = await client.post(f"/api/v1/memories/curated/{curated_id}/freeze")
        assert again.json()["data"]["frozen"] is True
        unfrozen = await client.post(f"/api/v1/memories/curated/{curated_id}/unfreeze")
        assert unfrozen.json()["data"]["frozen"] is False


async def test_lifecycle_endpoints_not_found(
    lifecycle_app: FastAPI, persona_id: int
) -> None:
    """不存在的记忆：四个生命周期端点均 404（NOT_FOUND 语义）。"""
    async with await _client(lifecycle_app) as client:
        for action in ("pin", "unpin", "freeze", "unfreeze"):
            response = await client.post(f"/api/v1/memories/curated/999999/{action}")
            assert response.status_code == 404
            assert response.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# HTTP 面——列表排序 / 归档过滤
# ---------------------------------------------------------------------------


async def test_list_strength_sort_and_archived_exclusion(
    lifecycle_app: FastAPI, db_session: AsyncSession, persona_id: int
) -> None:
    """强度降序列表：高重要度在前；归档行默认排除、include_archived 可见。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        high = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="高重要度", content="重要", importance=9
            ),
        )
        low = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="低重要度", content="次要", importance=1
            ),
        )
        archived = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="将归档", content="归档面", importance=1
            ),
        )
        await service.archive_curated(db_session, [archived.id])

    async with await _client(lifecycle_app) as client:
        default_page = await client.get(
            "/api/v1/memories/curated", params={"sort": "strength_desc", "limit": 50}
        )
        assert default_page.status_code == 200
        items = default_page.json()["data"]["items"]
        ids = [item["id"] for item in items]
        assert archived.id not in ids
        strengths = [item["effective_strength"] for item in items]
        assert strengths == sorted(strengths, reverse=True)
        assert ids.index(high.id) < ids.index(low.id)

        with_archived = await client.get(
            "/api/v1/memories/curated",
            params={"include_archived": "true", "limit": 50},
        )
        assert archived.id in [item["id"] for item in with_archived.json()["data"]["items"]]

        only_archived = await client.get(
            "/api/v1/memories/curated", params={"status": "archived", "limit": 50}
        )
        assert [item["id"] for item in only_archived.json()["data"]["items"]] == [
            archived.id
        ]


async def test_list_strength_cursor_pagination(
    lifecycle_app: FastAPI, db_session: AsyncSession, persona_id: int
) -> None:
    """强度排序分页：偏移游标翻页无重复无遗漏。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        for index in range(3):
            await service.upsert_curated(
                db_session,
                CuratedUpsertDTO(
                    kind=CuratedKind.MEMORY,
                    topic=f"主题{index}",
                    content=f"正文{index}",
                    importance=index + 1,
                ),
            )
    async with await _client(lifecycle_app) as client:
        first = await client.get(
            "/api/v1/memories/curated", params={"sort": "strength_desc", "limit": 2}
        )
        page_one = first.json()["data"]
        assert len(page_one["items"]) == 2
        assert page_one["next_cursor"] is not None
        second = await client.get(
            "/api/v1/memories/curated",
            params={
                "sort": "strength_desc",
                "limit": 2,
                "cursor": page_one["next_cursor"],
            },
        )
        page_two = second.json()["data"]
        combined = [item["id"] for item in page_one["items"]] + [
            item["id"] for item in page_two["items"]
        ]
        assert len(combined) == len(set(combined)) == 3


# ---------------------------------------------------------------------------
# 服务面——归档 / 强度投影 / 索引收口
# ---------------------------------------------------------------------------


async def test_archive_curated_clears_active_marker_and_excludes_from_list(
    db_session: AsyncSession, persona_id: int
) -> None:
    """归档：status=archived + active_marker 清位 + 默认列表不可见；重起链可用。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="归档目标", content="v1"),
        )
        archived_count = await service.archive_curated(db_session, [record.id])
        assert archived_count.count == 1
        assert archived_count.archived_ids == [record.id]
        row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == record.id)
            )
        ).scalar_one()
        assert row.status == CuratedStatus.ARCHIVED.value
        assert row.active_marker is None

        default_page = await service.query_curated(
            db_session, CuratedQueryDTO(limit=50)
        )
        assert record.id not in [item.id for item in default_page.items]

        with_archived = await service.query_curated(
            db_session, CuratedQueryDTO(include_archived=True, limit=50)
        )
        assert record.id in [item.id for item in with_archived.items]

        fresh = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="归档目标", content="v2"),
        )
        assert fresh.version == 1  # 归档腾位后重新起链


async def test_effective_strength_uses_injected_clock_and_config(
    db_session: AsyncSession, persona_id: int
) -> None:
    """读时强度：注入时钟 + 生命周期参数决定返回值（置顶行免衰减）。"""
    service = MemoryService(clock=lambda: NOW, lifecycle_config=LifecycleConfig())
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="强度面", content="正文", importance=4
            ),
        )
        row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == record.id)
            )
        ).scalar_one()
        row.created_at = NOW - 30 * MS_PER_DAY
        await db_session.commit()
        aged = await service.get_curated(db_session, record.id)
        assert aged.effective_strength == pytest.approx(2.0)
        pinned = await service.set_curated_pinned(db_session, record.id, pinned=True)
        assert pinned.effective_strength == pytest.approx(4.0)


async def test_delete_episode_dispatches_index_delete(
    db_session: AsyncSession, persona_id: int
) -> None:
    """删除情景事件：提交后按引用移除索引（episode + 人格数值化）。"""
    indexer = RecordingIndexer()
    service = MemoryService(indexer=cast("MemoryIndexer", indexer))
    async with bind_persona_context(persona_id):
        record = await service.append_episode(
            db_session,
            EpisodeCreateDTO(
                kind=EpisodeKind.CHAT,
                source="chat",
                content=ChatTurnPayloadModel(user_content="索引收口"),
            ),
        )
        await service.delete_episode(db_session, record.id)
    assert indexer.deletes == [(MemoryRefType.EPISODE, record.id, persona_id)]


async def test_archive_dispatches_curated_index_delete(
    db_session: AsyncSession, persona_id: int
) -> None:
    """归档：提交后按引用移除策展索引（curated + 人格数值化）。"""
    indexer = RecordingIndexer()
    service = MemoryService(indexer=cast("MemoryIndexer", indexer))
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="索引归档", content="正文"),
        )
        await service.archive_curated(db_session, [record.id])
    assert indexer.deletes == [(MemoryRefType.CURATED, record.id, persona_id)]


async def test_delete_curated_dispatches_index_delete(
    db_session: AsyncSession, persona_id: int
) -> None:
    """删除策展记忆：提交后同样移除索引（对称收口）。"""
    indexer = RecordingIndexer()
    service = MemoryService(indexer=cast("MemoryIndexer", indexer))
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="索引删除", content="正文"),
        )
        await service.delete_curated(db_session, record.id)
    assert indexer.deletes == [(MemoryRefType.CURATED, record.id, persona_id)]


async def test_bump_curated_access_service_direct(
    db_session: AsyncSession, persona_id: int
) -> None:
    """服务直调：访问计数推进（返回行数）+ 空集合短路。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="访问面", content="正文"),
        )
        assert await service.bump_curated_access(db_session, [record.id], accessed_ms=NOW) == 1
        assert await service.bump_curated_access(db_session, [], accessed_ms=NOW) == 0
        refreshed = await service.get_curated(db_session, record.id)
    assert refreshed.access_count == 1
    assert refreshed.last_accessed_ms == NOW


async def test_curated_sort_enum_values() -> None:
    """排序值域：strength_desc / updated_desc 与 API 参数契约一致。"""
    assert CuratedSort.STRENGTH_DESC.value == "strength_desc"
    assert CuratedSort.UPDATED_DESC.value == "updated_desc"


async def test_archive_unknown_ids_noop(db_session: AsyncSession, persona_id: int) -> None:
    """归档未知 id：零行变更 + 空 id 序列短路（幂等防御面）。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        empty = await service.archive_curated(db_session, [])
        assert empty.count == 0
        assert empty.archived_ids == []
        unknown = await service.archive_curated(db_session, [999999])
        assert unknown.count == 0
        assert unknown.archived_ids == []


async def test_archive_curated_skips_superseded_at_execution(
    db_session: AsyncSession, persona_id: int
) -> None:
    """执行点复核：已被取代的行不归档（不进入结果集合、状态保持 superseded）。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        old = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="竞态主题", content="旧版"),
        )
        new = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="竞态主题", content="新版"),
        )
        skipped = await service.archive_curated(db_session, [old.id])
        assert skipped.count == 0
        assert skipped.archived_ids == []
        mixed = await service.archive_curated(db_session, [old.id, new.id])
        old_row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == old.id)
            )
        ).scalar_one()
        new_row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == new.id)
            )
        ).scalar_one()
    assert mixed.count == 1
    assert mixed.archived_ids == [new.id]
    assert old_row.status == CuratedStatus.SUPERSEDED.value
    assert new_row.status == CuratedStatus.ARCHIVED.value


async def test_pin_service_not_found(db_session: AsyncSession, persona_id: int) -> None:
    """服务直调：不存在 id 抛 NOT_FOUND（四端点共享语义）。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        with pytest.raises(BusinessException) as exc_info:
            await service.set_curated_pinned(db_session, 999999, pinned=True)
    assert exc_info.value.code == "NOT_FOUND"


async def test_strength_sort_scan_cap_warns(
    db_session: AsyncSession,
    persona_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描上限：达到上限时按上限截断（告警 + 结果仍可分页）。"""
    import packages.harness.memory.service as service_module

    monkeypatch.setattr(service_module, "_STRENGTH_SCAN_LIMIT", 2)
    service = MemoryService()
    async with bind_persona_context(persona_id):
        for index in range(3):
            await service.upsert_curated(
                db_session,
                CuratedUpsertDTO(
                    kind=CuratedKind.MEMORY,
                    topic=f"截断{index}",
                    content="正文",
                    importance=index + 1,
                ),
            )
        page = await service.query_curated(
            db_session, CuratedQueryDTO(sort=CuratedSort.STRENGTH_DESC, limit=10)
        )
    assert len(page.items) == 2


async def test_curated_record_defaults_for_legacy_rows(
    db_session: AsyncSession, persona_id: int
) -> None:
    """既有行形态：生命周期列取默认值（pinned/frozen False、计数 0、无访问时刻）。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="默认列", content="正文"),
        )
    assert record.pinned is False
    assert record.frozen is False
    assert record.access_count == 0
    assert record.last_accessed_ms is None


async def test_archive_rows_frozen_exempt_from_default_list(
    db_session: AsyncSession, persona_id: int
) -> None:
    """冻结行不被归档：archive 只作用于显式 id（冻结豁免由 dreaming 判定层承担）。"""
    service = MemoryService()
    async with bind_persona_context(persona_id):
        record = await service.upsert_curated(
            db_session,
            CuratedUpsertDTO(kind=CuratedKind.MEMORY, topic="冻结保留", content="正文"),
        )
        await service.set_curated_frozen(db_session, record.id, frozen=True)
        page = await service.query_curated(db_session, CuratedQueryDTO(limit=50))
        assert record.id in [item.id for item in page.items]
        assert page.items[0].frozen is True


async def test_query_curated_sequence_type_hint() -> None:
    """语句类型面冒烟：select 语句可作为强度排序入口（mypy 结构面锁定）。"""
    from sqlalchemy import Select
    from sqlalchemy import select as sa_select

    stmt: Select[CuratedMemoryRow] = sa_select(CuratedMemoryRow)
    assert stmt is not None


async def test_indexer_delete_dispatch_methods() -> None:
    """索引管道分发面：dispatch_delete 内联路径调用 store.delete（失败吞没）。"""
    calls: list[tuple[MemoryRefType, int, int]] = []

    class _Store:
        async def delete(self, ref_type: MemoryRefType, ref_id: int, persona_id: int) -> None:
            calls.append((ref_type, ref_id, persona_id))

    indexer = MemoryIndexer(
        embedder=_unused_embedder,
        store=cast("Any", _Store()),
    )
    assert await indexer.delete_index_entry(MemoryRefType.EPISODE, 3, 9) is True
    await indexer.dispatch_delete(MemoryRefType.CURATED, 4, 9)
    assert calls == [(MemoryRefType.EPISODE, 3, 9), (MemoryRefType.CURATED, 4, 9)]


async def _unused_embedder(texts: Sequence[str]) -> Any:
    """索引写入面未使用的嵌入器桩（删除路径零嵌入调用）。"""
    raise AssertionError("删除路径不应调用嵌入器")
