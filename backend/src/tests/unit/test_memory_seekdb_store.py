"""harness/memory SeekDBStore 测试——假 aiomysql 池（零真实连接）。

覆盖面：upsert 参数形态（行展开 + 向量 JSON 字符串 + 维度校验）/
ensure_schema 惰性 once 守卫（DDL 只发一次）/ search 行映射
（description 列名 → 结果 DTO）/ delete 三谓词参数 / 错误映射（驱动
异常 → INTERNAL_ERROR）/ 池所有权（注入池 close 为 no-op；自持池
close 关闭——monkeypatch 池创建工厂）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.seekdb_store import SeekDBStore
from packages.harness.memory.store import build_ensure_schema_sql, build_upsert_sql
from packages.harness.pools import seekdb_compat

#: 假 SELECT 列（build_search_sql 的 SELECT 列序）。
_RESULT_COLUMNS: tuple[str, ...] = (
    "id",
    "ref_type",
    "ref_id",
    "kind",
    "provenance",
    "ts_ms",
    "content",
    "score",
)


class FakeCursor:
    """假 aiomysql 游标（捕获 (sql, args)；SELECT 回放罐装行）。"""

    def __init__(self, pool: FakeSeekDbPool) -> None:
        """初始化游标（绑定所属池——捕获面共享）。"""
        self._pool = pool
        self.description: Sequence[Sequence[Any]] | None = None
        self._rows: list[Sequence[Any]] = []

    async def execute(self, query: str, args: Sequence[Any] | None = None) -> int:
        """执行语句（捕获；SELECT 装配 description + 罐装行）。"""
        self._pool.executed.append((query, list(args) if args is not None else None))
        if self._pool.error_on_execute is not None:
            raise self._pool.error_on_execute
        if query.lstrip().upper().startswith("SELECT"):
            self.description = tuple((name,) for name in _RESULT_COLUMNS)
            self._rows = list(self._pool.rows)
        return 0

    async def fetchall(self) -> list[Sequence[Any]]:
        """取回全部行（SELECT 路径）。"""
        return self._rows


class FakeConnection:
    """假 aiomysql 连接（cursor 经 async with 获取）。"""

    def __init__(self, pool: FakeSeekDbPool) -> None:
        """初始化连接（绑定所属池）。"""
        self._pool = pool

    def cursor(self) -> AbstractAsyncContextManager[FakeCursor]:
        """获取游标（异步上下文管理器）。"""

        @asynccontextmanager
        async def _cm() -> AsyncIterator[FakeCursor]:
            yield FakeCursor(self._pool)

        return _cm()


class FakeSeekDbPool:
    """假 aiomysql 池（鸭子类型协议面——捕获 (sql, params)）。"""

    def __init__(self) -> None:
        """初始化池（捕获清单 + 罐装行 + 可注入错误）。"""
        self.executed: list[tuple[str, list[Any] | None]] = []
        self.rows: list[Sequence[Any]] = []
        self.error_on_execute: Exception | None = None
        self.closed = False
        self.wait_closed_called = False

    def acquire(self) -> AbstractAsyncContextManager[FakeConnection]:
        """获取连接（异步上下文管理器——退出即归还）。"""

        @asynccontextmanager
        async def _cm() -> AsyncIterator[FakeConnection]:
            yield FakeConnection(self)

        return _cm()

    def close(self) -> None:
        """标记关闭。"""
        self.closed = True

    async def wait_closed(self) -> None:
        """等待关闭（记录调用）。"""
        self.wait_closed_called = True


def _config(**overrides: Any) -> SeekDBStoreConfig:
    """构造存储配置（默认 1024 维；测试铺底辅助）。"""
    payload: dict[str, Any] = {"vector_dim": 4}
    payload.update(overrides)
    return SeekDBStoreConfig.model_validate(payload)


def _entry(**overrides: Any) -> MemoryIndexEntryDTO:
    """构造索引条目（默认人格 7 / episode:11；测试铺底辅助）。"""
    payload: dict[str, Any] = {
        "persona_id": 7,
        "ref_type": "episode",
        "ref_id": 11,
        "kind": "chat",
        "provenance": "owner",
        "ts_ms": 1000,
        "content": "今天吃了小鱼干",
    }
    payload.update(overrides)
    return MemoryIndexEntryDTO.model_validate(payload)


def _search_query(**overrides: Any) -> MemorySearchQueryDTO:
    """构造检索查询（默认 dense / 人格 7；测试铺底辅助）。"""
    payload: dict[str, Any] = {
        "persona_id": 7,
        "query": "小鱼干",
        "mode": MemorySearchMode.DENSE,
        "limit": 10,
    }
    payload.update(overrides)
    return MemorySearchQueryDTO.model_validate(payload)


async def test_upsert_lazy_ensure_schema_once_then_params() -> None:
    """upsert：首写前惰性发 DDL（一次）+ 批量占位符参数（向量 JSON 字符串）。"""
    pool = FakeSeekDbPool()
    store = SeekDBStore(_config(), pool=pool)
    vec = [0.1, 0.2, 0.3, 0.4]

    await store.upsert([_entry(), _entry(ref_id=12)], [vec, [0.5, 0.6, 0.7, 0.8]])

    assert len(pool.executed) == 2
    first_sql, first_params = pool.executed[0]
    assert first_sql == build_ensure_schema_sql(4)
    assert first_params is None

    second_sql, second_params = pool.executed[1]
    assert second_sql == build_upsert_sql(2)
    assert second_params == [
        7,
        "episode",
        11,
        "chat",
        "owner",
        1000,
        "今天吃了小鱼干",
        "[0.1,0.2,0.3,0.4]",
        7,
        "episode",
        12,
        "chat",
        "owner",
        1000,
        "今天吃了小鱼干",
        "[0.5,0.6,0.7,0.8]",
    ]

    # 惰性 once：第二次 upsert 不再发 DDL
    await store.upsert([_entry(ref_id=13)], [vec])
    assert len(pool.executed) == 3
    assert pool.executed[2][0] == build_upsert_sql(1)


async def test_upsert_contract_violations() -> None:
    """upsert 契约：条目 / 向量数量不一致 + 维度不符均 VALIDATION_ERROR。"""
    pool = FakeSeekDbPool()
    store = SeekDBStore(_config(), pool=pool)
    vec = [0.1, 0.2, 0.3, 0.4]

    with pytest.raises(BusinessException) as count_exc:
        await store.upsert([_entry()], [vec, vec])
    assert count_exc.value.code == ErrorCode.VALIDATION_ERROR

    with pytest.raises(BusinessException) as dim_exc:
        await store.upsert([_entry()], [[0.1, 0.2]])
    assert dim_exc.value.code == ErrorCode.VALIDATION_ERROR
    assert pool.executed == []  # 校验先于 DDL / 写入


async def test_search_row_mapping_and_params() -> None:
    """search：行映射（列名 dict → 结果 DTO）+ SQL / 参数经构建器产出。"""
    pool = FakeSeekDbPool()
    pool.rows = [
        (1, "episode", 11, "chat", "owner", 1000, "今天吃了小鱼干", 0.25),
        (2, "curated", 21, "memory", "agent", 2000, "主人喜欢小鱼干", 0.75),
    ]
    store = SeekDBStore(_config(), pool=pool)

    results = await store.search(_search_query(), [0.1, 0.2, 0.3, 0.4])

    sql, params = pool.executed[0]
    assert sql.startswith("SELECT id, ref_type")
    assert params == ["[0.1,0.2,0.3,0.4]", 7, 10]
    assert len(results) == 2
    first = results[0]
    assert first.ref_type == MemoryRefType.EPISODE
    assert first.ref_id == 11
    assert first.kind == "chat"
    assert first.provenance.value == "owner"
    assert first.ts_ms == 1000
    assert first.content == "今天吃了小鱼干"
    assert first.score == pytest.approx(0.25)
    second = results[1]
    assert second.ref_type == MemoryRefType.CURATED
    assert second.score == pytest.approx(0.75)


async def test_delete_params_order() -> None:
    """delete：参数序 persona / ref_type / ref_id（三谓词隔离面）。"""
    pool = FakeSeekDbPool()
    store = SeekDBStore(_config(), pool=pool)

    await store.delete(MemoryRefType.EPISODE, 11, 7)

    sql, params = pool.executed[0]
    assert sql.startswith("DELETE FROM ff_memory_index")
    assert params == [7, "episode", 11]


async def test_driver_error_mapped_to_internal() -> None:
    """错误映射：驱动异常（pymysql.err.Error 族）→ INTERNAL_ERROR。"""
    import pymysql

    pool = FakeSeekDbPool()
    pool.error_on_execute = pymysql.err.OperationalError(1042, "conn fail")
    store = SeekDBStore(_config(), pool=pool)

    with pytest.raises(BusinessException) as exc_info:
        await store.search(_search_query(), [0.1, 0.2, 0.3, 0.4])
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "SeekDB" in exc_info.value.message


async def test_close_injected_pool_is_noop() -> None:
    """池所有权：注入池 close 为 no-op（生命周期归注入方 / PoolManager）。"""
    pool = FakeSeekDbPool()
    store = SeekDBStore(_config(), pool=pool)
    await store.close()
    assert pool.closed is False
    assert pool.wait_closed_called is False


async def test_close_own_pool_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池所有权：自持池（惰性创建）close 关闭 + wait_closed（monkeypatch 池工厂）。"""
    fake_pool = FakeSeekDbPool()

    async def _fake_create_pool(**kwargs: Any) -> FakeSeekDbPool:
        """假池工厂（记录创建参数）。"""
        fake_pool.executed.append(("__create_pool__", list(kwargs.values())))
        return fake_pool

    monkeypatch.setattr(
        "packages.harness.memory.seekdb_store.create_seekdb_pool", _fake_create_pool
    )
    monkeypatch.setenv("SEEKDB_TEST_PASSWORD", "secret-pass")

    store = SeekDBStore(SeekDBStoreConfig(password_env="SEEKDB_TEST_PASSWORD", vector_dim=4))
    await store.upsert([_entry()], [[0.1, 0.2, 0.3, 0.4]])

    create_call = fake_pool.executed[0]
    assert create_call[0] == "__create_pool__"
    assert fake_pool.closed is False

    await store.close()
    assert fake_pool.closed is True
    assert fake_pool.wait_closed_called is True


async def test_compat_error_base_is_pymysql_error() -> None:
    """兼容层错误基类：aiomysql re-export 的 pymysql.err.Error。"""
    import pymysql

    assert seekdb_compat.seekdb_error_base() is pymysql.err.Error
