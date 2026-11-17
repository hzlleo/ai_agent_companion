"""memory 检索层 SQL 构建器测试——精确字符串断言（SeekDB 事实卡锁定面）。

覆盖面：ensure_schema DDL（VECTOR(dim) 插值 / ik 全文 / hnsw 向量
索引 / HEAP / 引用唯一键）/ upsert（行数占位符展开 + ON DUPLICATE KEY
覆盖列）/ delete（三谓词）/ search 三模式（dense 无 MATCH + APPROXIMATE；
keyword 有 MATCH 无 APPROXIMATE + ORDER BY MATCH DESC；hybrid = Form B
DBMS_HYBRID_SEARCH.SEARCH 单语句 + JSON DSL 载荷——载荷精确形态 /
全过滤组合 / 人格谓词恒在 / 字节确定性）/ 过滤器组合与参数序 / 模式
违规 fail loud / LIMIT 钳位（DTO 边界）/ 向量 JSON 字面量编码 /
hybrid 执行面（假 aiomysql 池——SEARCH 单行单列 JSON 载荷解析 + 行
映射 + 畸形行跳过 + 载荷形态异常 fail loud）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval_models import (
    MemorySearchMode,
    MemorySearchQueryDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.seekdb_store import SeekDBStore
from packages.harness.memory.store import (
    build_delete_sql,
    build_ensure_schema_sql,
    build_hybrid_search_payload,
    build_search_sql,
    build_upsert_sql,
    encode_vector_literal,
)


def _query(**overrides: object) -> MemorySearchQueryDTO:
    """构造检索查询 DTO（默认 dense / 人格 7 / 无过滤；测试铺底辅助）。"""
    payload: dict[str, object] = {
        "persona_id": 7,
        "query": "小鱼干",
        "mode": MemorySearchMode.DENSE,
        "limit": 10,
    }
    payload.update(overrides)
    return MemorySearchQueryDTO.model_validate(payload)


# ---------------------------------------------------------------------------
# ensure_schema DDL
# ---------------------------------------------------------------------------


async def test_ensure_schema_sql_exact_shape() -> None:
    """DDL 精确形态：VECTOR(dim) 插值 + ik 全文 + hnsw 向量索引 + HEAP + 引用唯一键。"""
    sql = build_ensure_schema_sql(1024)
    assert sql == (
        "CREATE TABLE IF NOT EXISTS ff_memory_index (\n"
        "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
        "  persona_id BIGINT NOT NULL,\n"
        "  ref_type VARCHAR(32) NOT NULL,\n"
        "  ref_id BIGINT NOT NULL,\n"
        "  kind VARCHAR(32) NOT NULL,\n"
        "  provenance VARCHAR(16) NOT NULL,\n"
        "  ts_ms BIGINT NOT NULL,\n"
        "  content TEXT NOT NULL,\n"
        "  embedding VECTOR(1024),\n"
        "  FULLTEXT INDEX idx_ff_mem_ft (content) WITH PARSER ik PARSER_PROPERTIES=(ik_mode='max_word'),\n"
        "  VECTOR INDEX idx_ff_mem_vec (embedding) "
        "WITH (DISTANCE=cosine, TYPE=hnsw, LIB=vsag),\n"
        "  UNIQUE KEY uk_ff_mem_ref (persona_id, ref_type, ref_id)\n"
        ") ORGANIZATION = HEAP"
    )


async def test_ensure_schema_sql_dim_interpolated() -> None:
    """维度配置化插值：768 维进 DDL（VECTOR(768)）。"""
    assert "  embedding VECTOR(768),\n" in build_ensure_schema_sql(768)
    with pytest.raises(BusinessException) as exc_info:
        build_ensure_schema_sql(0)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# upsert / delete
# ---------------------------------------------------------------------------


async def test_upsert_sql_single_row_exact_shape() -> None:
    """upsert 单行：8 占位符 + ON DUPLICATE KEY UPDATE 五列覆盖。"""
    sql = build_upsert_sql(1)
    assert sql == (
        "INSERT INTO ff_memory_index\n"
        "  (persona_id, ref_type, ref_id, kind, provenance, ts_ms, content, embedding)\n"
        "VALUES\n"
        "  (%s, %s, %s, %s, %s, %s, %s, %s)\n"
        "ON DUPLICATE KEY UPDATE\n"
        "  content = VALUES(content),\n"
        "  embedding = VALUES(embedding),\n"
        "  kind = VALUES(kind),\n"
        "  provenance = VALUES(provenance),\n"
        "  ts_ms = VALUES(ts_ms)"
    )


async def test_upsert_sql_row_count_placeholders() -> None:
    """行数占位符展开：n=3 时 VALUES 三行、每行 8 个 %s（共 24）。"""
    sql = build_upsert_sql(3)
    values_block = sql.split("VALUES\n", maxsplit=1)[1].split("\nON DUPLICATE")[0]
    rows = values_block.split(",\n  ")
    assert len(rows) == 3
    assert all(row.strip() == "(%s, %s, %s, %s, %s, %s, %s, %s)" for row in rows)
    assert sql.count("%s") == 24
    with pytest.raises(BusinessException) as exc_info:
        build_upsert_sql(0)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_delete_sql_exact_shape() -> None:
    """delete：人格 + 类型 + 引用键三谓词（参数序 persona / ref_type / ref_id）。"""
    assert build_delete_sql() == (
        "DELETE FROM ff_memory_index\nWHERE persona_id = %s\n  AND ref_type = %s\n  AND ref_id = %s"
    )


# ---------------------------------------------------------------------------
# search——dense 模式
# ---------------------------------------------------------------------------


async def test_search_sql_dense_no_match_clause() -> None:
    """dense：无 MATCH 谓词 + cosine_distance score + ORDER BY score APPROXIMATE。"""
    sql, params = build_search_sql(_query(), [0.1, 0.2])
    assert sql == (
        "SELECT id, ref_type, ref_id, kind, provenance, ts_ms, content,\n"
        "       cosine_distance(embedding, %s) AS score\n"
        "FROM ff_memory_index\n"
        "WHERE persona_id = %s\n"
        "ORDER BY score APPROXIMATE LIMIT %s"
    )
    assert params == ["[0.1,0.2]", 7, 10]


async def test_search_sql_dense_all_filters_composition() -> None:
    """dense + 全过滤组合：kind IN / provenance / ts 闭区间 + 参数序（SQL 出现序）。"""
    sql, params = build_search_sql(
        _query(
            kinds=["chat", "duty"],
            provenance=MemoryProvenance.OWNER,
            start_ms=100,
            end_ms=200,
        ),
        [0.5],
    )
    assert sql == (
        "SELECT id, ref_type, ref_id, kind, provenance, ts_ms, content,\n"
        "       cosine_distance(embedding, %s) AS score\n"
        "FROM ff_memory_index\n"
        "WHERE persona_id = %s\n"
        "  AND kind IN (%s, %s)\n"
        "  AND provenance = %s\n"
        "  AND ts_ms >= %s\n"
        "  AND ts_ms <= %s\n"
        "ORDER BY score APPROXIMATE LIMIT %s"
    )
    assert params == ["[0.5]", 7, "chat", "duty", "owner", 100, 200, 10]


# ---------------------------------------------------------------------------
# search——keyword 模式
# ---------------------------------------------------------------------------


async def test_search_sql_keyword_match_without_approximate() -> None:
    """keyword：MATCH 三处（SELECT score / WHERE / ORDER BY）+ DESC + 无 APPROXIMATE。"""
    sql, params = build_search_sql(_query(mode=MemorySearchMode.KEYWORD), None)
    assert sql == (
        "SELECT id, ref_type, ref_id, kind, provenance, ts_ms, content,\n"
        "       MATCH(content) AGAINST(%s) AS score\n"
        "FROM ff_memory_index\n"
        "WHERE persona_id = %s\n"
        "  AND MATCH(content) AGAINST(%s)\n"
        "ORDER BY MATCH(content) AGAINST(%s) DESC LIMIT %s"
    )
    assert params == ["小鱼干", 7, "小鱼干", "小鱼干", 10]
    assert "APPROXIMATE" not in sql


# ---------------------------------------------------------------------------
# search——hybrid 模式（Form B：DBMS_HYBRID_SEARCH.SEARCH + JSON DSL 载荷）
# ---------------------------------------------------------------------------


async def test_hybrid_payload_minimal_shape() -> None:
    """hybrid 载荷最小形态：query_string must + persona 隔离 term + knn。"""
    payload = build_hybrid_search_payload(_query(mode=MemorySearchMode.HYBRID), [0.3, 0.4])
    assert payload == (
        '{"query":{"bool":{"must":[{"query_string":{"fields":["content"],'
        '"query":"\\u5c0f\\u9c7c\\u5e72"}}],"filter":[{"term":{"persona_id":7}}]}},'
        '"knn":{"field":"embedding","k":10,"query_vector":[0.3,0.4]}}'
    )


async def test_hybrid_payload_full_filter_composition() -> None:
    """hybrid 载荷全过滤组合：persona 恒首 + kind terms + provenance + ts 双边界。"""
    payload = build_hybrid_search_payload(
        _query(
            mode=MemorySearchMode.HYBRID,
            kinds=["chat", "duty"],
            provenance=MemoryProvenance.OWNER,
            start_ms=100,
            end_ms=200,
        ),
        [0.5],
    )
    assert payload == (
        '{"query":{"bool":{"must":[{"query_string":{"fields":["content"],'
        '"query":"\\u5c0f\\u9c7c\\u5e72"}}],"filter":['
        '{"term":{"persona_id":7}},'
        '{"terms":{"kind":["chat","duty"]}},'
        '{"term":{"provenance":"owner"}},'
        '{"range":{"ts_ms":{"gte":100}}},'
        '{"range":{"ts_ms":{"lte":200}}}'
        ']}},"knn":{"field":"embedding","k":10,"query_vector":[0.5]}}'
    )


async def test_hybrid_payload_persona_filter_never_omitted() -> None:
    """人格隔离谓词：任何过滤组合下 persona term 恒在且恒为 filter 首条。"""
    combos = [
        _query(mode=MemorySearchMode.HYBRID),
        _query(mode=MemorySearchMode.HYBRID, kinds=["chat"]),
        _query(mode=MemorySearchMode.HYBRID, provenance=MemoryProvenance.AGENT),
        _query(mode=MemorySearchMode.HYBRID, start_ms=1, end_ms=2),
        _query(
            mode=MemorySearchMode.HYBRID,
            kinds=["chat", "duty"],
            provenance=MemoryProvenance.OWNER,
            start_ms=100,
            end_ms=200,
        ),
    ]
    for query in combos:
        payload = json.loads(build_hybrid_search_payload(query, [0.1]))
        filters = payload["query"]["bool"]["filter"]
        assert filters[0] == {"term": {"persona_id": query.persona_id}}


async def test_hybrid_payload_deterministic_bytes() -> None:
    """载荷字节确定性：同查询（DTO 字段序不同）两次构建产出同字节串。"""
    first = build_hybrid_search_payload(
        _query(mode=MemorySearchMode.HYBRID, limit=5), [0.1, 0.2]
    )
    second = build_hybrid_search_payload(
        MemorySearchQueryDTO.model_validate(
            {"query": "小鱼干", "persona_id": 7, "mode": MemorySearchMode.HYBRID, "limit": 5}
        ),
        [0.1, 0.2],
    )
    assert first == second


async def test_search_sql_hybrid_form_b_statement() -> None:
    """hybrid SQL 形态：Form B 单语句 SEARCH + DSL 载荷为唯一参数。"""
    sql, params = build_search_sql(_query(mode=MemorySearchMode.HYBRID, limit=5), [0.3, 0.4])
    assert sql == "SELECT DBMS_HYBRID_SEARCH.SEARCH('ff_memory_index', %s)"
    assert params == [
        build_hybrid_search_payload(_query(mode=MemorySearchMode.HYBRID, limit=5), [0.3, 0.4])
    ]
    assert "MATCH" not in sql
    assert "APPROXIMATE" not in sql


# ---------------------------------------------------------------------------
# 模式违规 / LIMIT 钳位 / 向量字面量
# ---------------------------------------------------------------------------


async def test_search_mode_vector_contract_violations() -> None:
    """模式违规：dense/hybrid 缺向量、keyword 携带向量均 VALIDATION_ERROR。"""
    with pytest.raises(BusinessException) as dense_exc:
        build_search_sql(_query(mode=MemorySearchMode.DENSE), None)
    assert dense_exc.value.code == ErrorCode.VALIDATION_ERROR

    with pytest.raises(BusinessException) as hybrid_exc:
        build_search_sql(_query(mode=MemorySearchMode.HYBRID), None)
    assert hybrid_exc.value.code == ErrorCode.VALIDATION_ERROR

    with pytest.raises(BusinessException) as keyword_exc:
        build_search_sql(_query(mode=MemorySearchMode.KEYWORD), [0.1])
    assert keyword_exc.value.code == ErrorCode.VALIDATION_ERROR


async def test_search_limit_clamp_at_dto_boundary() -> None:
    """LIMIT 钳位：DTO 边界强制（1-50 之外构造即拒绝——SQL 层无需二次钳位）。"""
    with pytest.raises(ValidationError):
        _query(limit=0)
    with pytest.raises(ValidationError):
        _query(limit=51)
    sql, params = build_search_sql(_query(limit=1), [0.1])
    assert sql.endswith("ORDER BY score APPROXIMATE LIMIT %s")
    assert params[-1] == 1


async def test_encode_vector_literal_compact_json() -> None:
    """向量字面量：紧凑 JSON 数组字符串（无空格分隔）。"""
    assert encode_vector_literal([0.1, 0.2]) == "[0.1,0.2]"
    assert encode_vector_literal([1.0]) == "[1.0]"


# ---------------------------------------------------------------------------
# hybrid 执行面（假 aiomysql 池——SEARCH 单行单列载荷解析 + 行映射）
# ---------------------------------------------------------------------------


class _CellCursor:
    """假 aiomysql 游标（SEARCH 语句回放罐装单行单列结果）。"""

    def __init__(self, pool: _CellPool) -> None:
        """初始化游标（绑定所属池——捕获面共享）。"""
        self._pool = pool
        self.description: Sequence[Sequence[Any]] | None = None
        self._rows: list[Sequence[Any]] = []

    async def execute(self, query: str, args: Sequence[Any] | None = None) -> int:
        """执行语句（捕获；SELECT 回放罐装行）。"""
        self._pool.executed.append((query, list(args) if args is not None else None))
        if query.startswith("SELECT"):
            self.description = (("DBMS_HYBRID_SEARCH.SEARCH(...)",),)
            self._rows = list(self._pool.rows)
        return 0

    async def fetchall(self) -> list[Sequence[Any]]:
        """取回全部行。"""
        return self._rows


class _CellConnection:
    """假 aiomysql 连接（cursor 经 async with 获取）。"""

    def __init__(self, pool: _CellPool) -> None:
        """初始化连接（绑定所属池）。"""
        self._pool = pool

    def cursor(self) -> AbstractAsyncContextManager[_CellCursor]:
        """获取游标（异步上下文管理器）。"""

        @asynccontextmanager
        async def _cm() -> AsyncIterator[_CellCursor]:
            yield _CellCursor(self._pool)

        return _cm()


class _CellPool:
    """假 aiomysql 池（SEARCH 单行单列回放——hybrid 执行面）。"""

    def __init__(self, rows: Sequence[Sequence[Any]]) -> None:
        """初始化池（罐装结果行 + 语句捕获清单）。"""
        self.executed: list[tuple[str, list[Any] | None]] = []
        self.rows = rows

    def acquire(self) -> AbstractAsyncContextManager[_CellConnection]:
        """获取连接（异步上下文管理器——退出即归还）。"""

        @asynccontextmanager
        async def _cm() -> AsyncIterator[_CellConnection]:
            yield _CellConnection(self)

        return _cm()


def _seekdb_config() -> SeekDBStoreConfig:
    """构造存储配置（4 维——与罐装向量一致）。"""
    return SeekDBStoreConfig.model_validate({"vector_dim": 4})


def _hybrid_hits_json() -> str:
    """罐装 SEARCH 命中载荷（两行——全表列 + _score，融合分降序）。"""
    return json.dumps(
        [
            {
                "id": 11,
                "persona_id": 7,
                "ref_type": "episode",
                "ref_id": 11,
                "kind": "chat",
                "provenance": "owner",
                "ts_ms": 1000,
                "content": "今天吃了小鱼干",
                "embedding": "[0.1,0.2,0.3,0.4]",
                "_score": 1.5978,
            },
            {
                "id": 12,
                "persona_id": 7,
                "ref_type": "curated",
                "ref_id": 21,
                "kind": "memory",
                "provenance": "agent",
                "ts_ms": 2000,
                "content": "主人喜欢小鱼干",
                "embedding": "[0.4,0.3,0.2,0.1]",
                "_score": 1.496,
            },
        ]
    )


async def test_hybrid_execution_maps_rows_to_dtos() -> None:
    """hybrid 执行：SEARCH 语句 + 载荷参数直达 + 行 dict → DTO（score=_score）。"""
    pool = _CellPool([(_hybrid_hits_json(),)])
    store = SeekDBStore(_seekdb_config(), pool=pool)

    results = await store.search(_query(mode=MemorySearchMode.HYBRID), [0.1, 0.2, 0.3, 0.4])

    sql, params = pool.executed[0]
    assert sql == "SELECT DBMS_HYBRID_SEARCH.SEARCH('ff_memory_index', %s)"
    assert params == [
        build_hybrid_search_payload(_query(mode=MemorySearchMode.HYBRID), [0.1, 0.2, 0.3, 0.4])
    ]
    assert len(results) == 2
    first = results[0]
    assert first.ref_type.value == "episode"
    assert first.ref_id == 11
    assert first.kind == "chat"
    assert first.provenance.value == "owner"
    assert first.ts_ms == 1000
    assert first.content == "今天吃了小鱼干"
    assert first.score == pytest.approx(1.5978)
    assert results[1].ref_id == 21
    assert results[1].score == pytest.approx(1.496)


async def test_hybrid_execution_null_cell_is_empty_result() -> None:
    """hybrid 执行：SQL NULL 单元格（live 实测空集形态）→ 空结果列表。"""
    pool = _CellPool([(None,)])
    store = SeekDBStore(_seekdb_config(), pool=pool)

    results = await store.search(_query(mode=MemorySearchMode.HYBRID), [0.1])

    assert results == []


async def test_hybrid_execution_skips_malformed_rows() -> None:
    """hybrid 执行：畸形行（缺 _score / 非法 ref_type / 非对象）跳过，缺展示列取缺省。"""
    payload = json.dumps(
        [
            {"ref_type": "episode", "ref_id": 11, "kind": "chat", "provenance": "owner",
             "ts_ms": 1000, "content": "缺 _score 的行"},
            {"ref_type": "bogus", "ref_id": 12, "provenance": "owner", "_score": 1.0},
            "不是对象的行",
            {"ref_type": "curated", "ref_id": 13, "provenance": "agent", "_score": 2.0,
             "content": "缺 kind 与 ts_ms 的行"},
        ]
    )
    pool = _CellPool([(payload,)])
    store = SeekDBStore(_seekdb_config(), pool=pool)

    results = await store.search(_query(mode=MemorySearchMode.HYBRID), [0.1])

    assert len(results) == 1
    kept = results[0]
    assert kept.ref_id == 13
    assert kept.kind == ""
    assert kept.ts_ms == 0
    assert kept.content == "缺 kind 与 ts_ms 的行"
    assert kept.score == pytest.approx(2.0)


async def test_hybrid_execution_payload_anomalies_fail_loud() -> None:
    """hybrid 执行：载荷形态异常（零行 / 零列 / 多列 / 非字符串 / 非法 JSON / 非数组）→ INTERNAL_ERROR。"""
    for rows in ([], [()], [("col_a", "col_b")], [(123,)], [("{not json",)], [(json.dumps({"hits": []}),)]):
        pool = _CellPool(rows)
        store = SeekDBStore(_seekdb_config(), pool=pool)
        with pytest.raises(BusinessException) as exc_info:
            await store.search(_query(mode=MemorySearchMode.HYBRID), [0.1])
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
