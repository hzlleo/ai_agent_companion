"""harness/memory SQLiteFallbackStore 测试——共享 SQLite 引擎回退路径。

覆盖面：upsert / keyword 检索往返（人格隔离 + 过滤 + 朴素相关度排序 +
limit）/ 同键覆盖（ON CONFLICT）/ dense / hybrid 不支持（清晰
VALIDATION_ERROR）/ keyword 携带向量拒绝 / delete / 引擎未初始化
fail loud / ensure_schema 幂等。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
)
from packages.harness.memory.sqlite_store import SQLiteFallbackStore


@pytest.fixture
async def fallback_store(shared_db_engine: AsyncEngine) -> AsyncIterator[SQLiteFallbackStore]:
    """回退存储（共享引擎外部安装到 holder——用后复位槽位）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎。

    Yields:
        就绪的 :class:`SQLiteFallbackStore`。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    yield SQLiteFallbackStore()
    db_engine_holder.reset()


def _entry(**overrides: object) -> MemoryIndexEntryDTO:
    """构造索引条目（默认人格 7 / episode:11；测试铺底辅助）。"""
    payload: dict[str, object] = {
        "persona_id": 7,
        "ref_type": "episode",
        "ref_id": 11,
        "kind": "chat",
        "provenance": "owner",
        "ts_ms": 1000,
        "content": "今天吃了很多小鱼干，小鱼干真好吃",
    }
    payload.update(overrides)
    return MemoryIndexEntryDTO.model_validate(payload)


def _query(**overrides: object) -> MemorySearchQueryDTO:
    """构造 keyword 检索查询（默认人格 7；测试铺底辅助）。"""
    payload: dict[str, object] = {
        "persona_id": 7,
        "query": "小鱼干",
        "mode": MemorySearchMode.KEYWORD,
        "limit": 10,
    }
    payload.update(overrides)
    return MemorySearchQueryDTO.model_validate(payload)


async def test_keyword_round_trip_with_persona_isolation(
    fallback_store: SQLiteFallbackStore,
) -> None:
    """upsert → keyword 检索往返：命中 + 朴素相关度排序 + 人格隔离。"""
    await fallback_store.upsert(
        [
            _entry(ref_id=11, content="今天吃了很多小鱼干，小鱼干真好吃"),
            _entry(ref_id=12, content="小鱼干 appears once"),
            _entry(ref_id=13, content="完全无关的内容"),
            _entry(persona_id=8, ref_id=99, content="别人格的小鱼干"),
        ],
        [[0.1]] * 4,
    )

    results = await fallback_store.search(_query(), None)

    assert [result.ref_id for result in results] == [11, 12]
    first, second = results
    assert first.score == 2.0  # 出现两次——朴素相关度降序
    assert second.score == 1.0
    assert first.content.startswith("今天吃了")


async def test_keyword_filters_and_limit(
    fallback_store: SQLiteFallbackStore,
) -> None:
    """过滤面：kind IN / provenance / ts 闭区间 + limit 截断。"""
    await fallback_store.upsert(
        [
            _entry(ref_id=1, kind="chat", ts_ms=100),
            _entry(ref_id=2, kind="duty", ts_ms=150),
            _entry(ref_id=3, kind="chat", provenance="agent", ts_ms=200),
            _entry(ref_id=4, kind="chat", ts_ms=300),
        ],
        [[0.1]] * 4,
    )

    by_kind = await fallback_store.search(_query(kinds=["chat"]), None)
    assert sorted(r.ref_id for r in by_kind) == [1, 3, 4]

    # 闭区间语义（与 SeekDB 路径一致）：end_ms=200 含 ts=200 行；
    # 同分稳定序 = ts 降序（朴素相关度相同——时间新在前）
    closed = await fallback_store.search(_query(start_ms=100, end_ms=200), None)
    assert [r.ref_id for r in closed] == [3, 2, 1]
    open_like = await fallback_store.search(_query(start_ms=100, end_ms=199), None)
    assert [r.ref_id for r in open_like] == [2, 1]

    by_provenance = await fallback_store.search(_query(provenance="agent"), None)
    assert [r.ref_id for r in by_provenance] == [3]

    limited = await fallback_store.search(_query(limit=2), None)
    assert len(limited) == 2


async def test_keyword_like_wildcard_escaped(
    fallback_store: SQLiteFallbackStore,
) -> None:
    """LIKE 通配符转义：查询含 %/_ 时按字面匹配（不放大命中）。"""
    await fallback_store.upsert(
        [
            _entry(ref_id=1, content="包含 100% 的进度"),
            _entry(ref_id=2, content="包含 a_b 的下划线"),
            _entry(ref_id=3, content="普通的进度文本"),
        ],
        [[0.1]] * 3,
    )

    percent = await fallback_store.search(_query(query="100%"), None)
    assert [r.ref_id for r in percent] == [1]

    underscore = await fallback_store.search(_query(query="a_b"), None)
    assert [r.ref_id for r in underscore] == [2]


async def test_upsert_same_key_overwrites(
    fallback_store: SQLiteFallbackStore,
) -> None:
    """同键覆盖：同 (persona, ref_type, ref_id) upsert 覆盖 content / ts。"""
    await fallback_store.upsert([_entry(content="旧内容")], [[0.1]])
    await fallback_store.upsert([_entry(content="新内容，小鱼干出现", ts_ms=2000)], [[0.2]])

    results = await fallback_store.search(_query(query="新内容"), None)
    assert len(results) == 1
    assert results[0].content == "新内容，小鱼干出现"
    assert results[0].ts_ms == 2000

    # 旧内容不再命中
    old = await fallback_store.search(_query(query="旧内容"), None)
    assert old == []


async def test_vector_modes_unsupported(fallback_store: SQLiteFallbackStore) -> None:
    """dense / hybrid 模式：清晰 VALIDATION_ERROR（回退存储不支持向量检索）。"""
    for mode in (MemorySearchMode.DENSE, MemorySearchMode.HYBRID):
        with pytest.raises(BusinessException) as exc_info:
            await fallback_store.search(_query(mode=mode), [0.1, 0.2])
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert "回退存储不支持向量检索" in exc_info.value.message


async def test_keyword_with_vector_rejected(fallback_store: SQLiteFallbackStore) -> None:
    """keyword 模式携带向量：VALIDATION_ERROR（模式违规面）。"""
    with pytest.raises(BusinessException) as exc_info:
        await fallback_store.search(_query(), [0.1])
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_upsert_count_mismatch_rejected(fallback_store: SQLiteFallbackStore) -> None:
    """upsert 契约：条目 / 向量数量不一致 VALIDATION_ERROR（回退路径同契约）。"""
    with pytest.raises(BusinessException) as exc_info:
        await fallback_store.upsert([_entry()], [])
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_delete_removes_row(fallback_store: SQLiteFallbackStore) -> None:
    """delete：三谓词删除（他人格同 ref_id 不受影响）。"""
    await fallback_store.upsert(
        [_entry(ref_id=11), _entry(persona_id=8, ref_id=11)],
        [[0.1]] * 2,
    )
    await fallback_store.delete(MemoryRefType.EPISODE, 11, 7)

    # 人格 7 视角零命中；人格 8 的同 ref_id 行不受影响（隔离谓词）
    mine = await fallback_store.search(_query(), None)
    assert mine == []
    theirs = await fallback_store.search(_query(persona_id=8), None)
    assert [r.ref_id for r in theirs] == [11]


async def test_engine_missing_fails_loud(
    fallback_store: SQLiteFallbackStore, shared_db_engine: AsyncEngine
) -> None:
    """引擎未初始化：INTERNAL_ERROR fail loud（调用期读取 holder）。"""
    db_engine_holder.reset()
    try:
        with pytest.raises(BusinessException) as exc_info:
            await fallback_store.search(_query(), None)
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    finally:
        # 复位安装（teardown 前保持 holder 可用——autouse 清表 fixture 消费共享引擎）
        db_engine_holder.async_engine = shared_db_engine


async def test_ensure_schema_idempotent(
    fallback_store: SQLiteFallbackStore, shared_db_engine: AsyncEngine
) -> None:
    """ensure_schema 幂等：重复调用 no-op（once 守卫 + checkfirst）。"""
    await fallback_store.ensure_schema()
    await fallback_store.ensure_schema()
    # 表可写（建表成功面）
    await fallback_store.upsert([_entry()], [[0.1]])
    results = await fallback_store.search(_query(), None)
    assert len(results) == 1
