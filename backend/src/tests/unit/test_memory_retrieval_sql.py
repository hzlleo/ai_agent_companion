"""memory 检索层 SQL 构建器测试——精确字符串断言（SeekDB 事实卡锁定面）。

覆盖面：ensure_schema DDL（VECTOR(dim) 插值 / jieba 全文 / hnsw 向量
索引 / HEAP / 引用唯一键）/ upsert（行数占位符展开 + ON DUPLICATE KEY
覆盖列）/ delete（三谓词）/ search 三模式（dense 无 MATCH + APPROXIMATE；
keyword 有 MATCH 无 APPROXIMATE + ORDER BY MATCH DESC；hybrid MATCH +
APPROXIMATE）/ 过滤器组合与参数序 / 模式违规 fail loud / LIMIT 钳位
（DTO 边界）/ 向量 JSON 字面量编码。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval_models import (
    MemorySearchMode,
    MemorySearchQueryDTO,
)
from packages.harness.memory.store import (
    build_delete_sql,
    build_ensure_schema_sql,
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
    """DDL 精确形态：VECTOR(dim) 插值 + jieba 全文 + hnsw 向量索引 + HEAP + 引用唯一键。"""
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
        "  FULLTEXT INDEX idx_ff_mem_ft (content) WITH PARSER jieba,\n"
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
# search——hybrid 模式
# ---------------------------------------------------------------------------


async def test_search_sql_hybrid_match_and_approximate() -> None:
    """hybrid：MATCH 预过滤谓词 + cosine_distance score + APPROXIMATE。"""
    sql, params = build_search_sql(_query(mode=MemorySearchMode.HYBRID), [0.3, 0.4])
    assert sql == (
        "SELECT id, ref_type, ref_id, kind, provenance, ts_ms, content,\n"
        "       cosine_distance(embedding, %s) AS score\n"
        "FROM ff_memory_index\n"
        "WHERE persona_id = %s\n"
        "  AND MATCH(content) AGAINST(%s)\n"
        "ORDER BY score APPROXIMATE LIMIT %s"
    )
    assert params == ["[0.3,0.4]", 7, "小鱼干", 10]


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
