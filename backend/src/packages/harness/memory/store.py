"""memory 域检索存储抽象——MemoryStore 协议 + 纯 SQL 构建器。

MemoryStore 协议（存储选择见 ``memory.retrieval.build_memory_store``）：

    - :class:`SeekDBStore`        —— 生产路径（SeekDB v1.4.0，MySQL 线协议
      2881 端口，raw SQL——向量 + 全文 + 标量混合检索，见 seekdb_store）；
    - :class:`SQLiteFallbackStore` —— 回退路径（共享 SQLAlchemy 引擎上的
      keyword-only 检索——零配置开发 / 无 SeekDB 部署 / 测试面，见
      sqlite_store）。

纯 SQL 构建器（**本模块的核心价值**——SQL 形态与 SeekDB 事实卡
逐字对齐，测试以精确字符串断言锁定）：

    - :func:`build_ensure_schema_sql` —— 索引表 DDL（VECTOR(dim) 插值 /
      jieba 全文索引 / hnsw 向量索引 / HEAP 组织 / 引用唯一键）；
    - :func:`build_upsert_sql`        —— 批量 upsert（ON DUPLICATE KEY
      UPDATE——按引用唯一键幂等覆盖）；
    - :func:`build_delete_sql`        —— 按引用删除（人格 + 类型 + 引用键）；
    - :func:`build_search_sql`        —— 三模式混合检索（Form A：单条 SQL，
      WHERE 谓词下推 ANN 扫描）。

SeekDB SQL 形态约束（事实卡锁定，逐条对应构建器实现）：

    - ``ORDER BY ... APPROXIMATE LIMIT`` 必须成套（无 APPROXIMATE 退化
      全表扫）——仅向量排序路径使用；keyword 模式无 ANN，用
      ``ORDER BY MATCH(...) AGAINST(...) DESC LIMIT``（无 APPROXIMATE）；
    - 向量字面量是 JSON 数组**字符串**（``'[".
      ``:qv``）——经 :func:`encode_vector_literal` 编码后作绑定参数；
    - 单向量条件（本层查询形态恒单条 cosine_distance）；
    - ``persona_id`` 谓词**每条查询必带**（索引表无 PersonaMixin 概念，
      人格隔离由 SQL 谓词强制——全部构建器路径包含该谓词）。

模式违规（查询模式与向量参数不匹配）在构建器内 fail loud
（``VALIDATION_ERROR``——错误码纪律：本 chunk 不新增错误码成员，
mode 违规归请求形态错误；偏差报告见交付说明）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)

__all__ = [
    "MemoryStore",
    "build_ensure_schema_sql",
    "build_upsert_sql",
    "build_delete_sql",
    "build_search_sql",
    "encode_vector_literal",
]


class MemoryStore(Protocol):
    """记忆检索存储协议（异步——SeekDB 生产 / SQLite 回退的共同面）。

    实现约定（协议级，逐实现文档化偏离）：

        - ``upsert`` 条目与向量**同序同长**（配对由调用方保证）；
        - ``search`` 的 ``query_vector`` 语义随模式：dense / hybrid 必填
          （None → ``VALIDATION_ERROR``）；keyword 必须为 None（携带向量
          → ``VALIDATION_ERROR``——模式违规面）；
        - ``delete`` 按引用唯一键 (persona_id, ref_type, ref_id) 删除；
        - ``close`` 幂等（注入式池的实现为 no-op——生命周期归注入方）。
    """

    async def ensure_schema(self) -> None:
        """确保索引表 schema 就绪（幂等——重复调用 no-op）。"""
        ...

    async def upsert(self, entries: list[MemoryIndexEntryDTO], vectors: list[list[float]]) -> None:
        """批量写入索引条目（同键覆盖——upsert 语义）。

        Args:
            entries: 索引条目列表（与向量同序同长）。
            vectors: 向量列表（JSON 字符串化后随行绑定）。
        """
        ...

    async def delete(self, ref_type: MemoryRefType, ref_id: int, persona_id: int) -> None:
        """按引用唯一键删除索引行。

        Args:
            ref_type: 引用类型（episode / curated）。
            ref_id: 引用行主键。
            persona_id: 人格主键（隔离谓词）。
        """
        ...

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[MemorySearchResultDTO]:
        """按模式检索（模式 / 向量匹配契约见类 docstring）。

        Args:
            query: 检索查询 DTO（persona_id 必带——隔离谓词）。
            query_vector: 查询向量（dense / hybrid 必填；keyword 须 None）。

        Returns:
            结果 DTO 列表（score 方向随模式——见 retrieval_models）。
        """
        ...

    async def close(self) -> None:
        """关闭存储持有的资源（幂等）。"""
        ...


# =========================================================================== []
# 纯 SQL 构建器（无副作用——测试精确字符串断言面）
# =========================================================================== []


def build_ensure_schema_sql(vector_dim: int) -> str:
    """构建索引表 DDL（SeekDB——VECTOR(dim) / jieba 全文 / hnsw 向量索引）。

    形态要点（事实卡锁定）：

        - ``VECTOR({vector_dim})`` —— 维度自配置插值（默认 1024 =
          Qwen3-Embedding-0.6B）；
        - ``ORGANIZATION = HEAP`` —— DBMS_HYBRID_SEARCH（Form B，后续
          chunk）的前置条件；
        - 全文索引中文分词 ``WITH PARSER jieba``；
        - 向量索引 ``DISTANCE=cosine, TYPE=hnsw, LIB=vsag``；
        - ``UNIQUE KEY uk_ff_mem_ref`` —— 引用唯一键（upsert 的
          ON DUPLICATE KEY 作用面）。

    Args:
        vector_dim: 向量维度（须与 embed 模型输出一致）。

    Returns:
        单条 DDL 语句字符串（无结尾分号——驱动单语句执行）。

    Raises:
        BusinessException: vector_dim 非法（<1——装配错误面，
            ``VALIDATION_ERROR``）。
    """
    if vector_dim < 1:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="向量维度非法",
            details={"vector_dim": vector_dim},
        )
    return (
        "CREATE TABLE IF NOT EXISTS ff_memory_index (\n"
        "  id BIGINT PRIMARY KEY AUTO_INCREMENT,\n"
        "  persona_id BIGINT NOT NULL,\n"
        "  ref_type VARCHAR(32) NOT NULL,\n"
        "  ref_id BIGINT NOT NULL,\n"
        "  kind VARCHAR(32) NOT NULL,\n"
        "  provenance VARCHAR(16) NOT NULL,\n"
        "  ts_ms BIGINT NOT NULL,\n"
        "  content TEXT NOT NULL,\n"
        f"  embedding VECTOR({vector_dim}),\n"
        "  FULLTEXT INDEX idx_ff_mem_ft (content) WITH PARSER jieba,\n"
        "  VECTOR INDEX idx_ff_mem_vec (embedding) "
        "WITH (DISTANCE=cosine, TYPE=hnsw, LIB=vsag),\n"
        "  UNIQUE KEY uk_ff_mem_ref (persona_id, ref_type, ref_id)\n"
        ") ORGANIZATION = HEAP"
    )


def build_upsert_sql(n_rows: int) -> str:
    """构建批量 upsert 语句（占位符行数 = n_rows——ON DUPLICATE KEY UPDATE）。

    upsert 语义：同 (persona_id, ref_type, ref_id) 唯一键命中时覆盖
    content / embedding / kind / provenance / ts_ms（重嵌入 / 重写场景
    幂等）。**文档化决策**：首选 ON DUPLICATE KEY UPDATE（事实卡确认
    其作用于唯一键）；若 SeekDB 运行期拒绝该语法，回退路径为
    DELETE+INSERT（同键先删后插，事务外幂等性等价）——真实实例经
    冒烟集成测试验证（tests/integration/test_seekdb_smoke.py）。

    Args:
        n_rows: 行数（占位符行展开数；须 >=1）。

    Returns:
        单条 upsert 语句字符串（参数顺序：每行 8 个占位符——
        persona_id / ref_type / ref_id / kind / provenance / ts_ms /
        content / embedding(JSON 字符串)）。

    Raises:
        BusinessException: n_rows < 1（空批量由调用方短路，构建器拒收
            ——``VALIDATION_ERROR``）。
    """
    if n_rows < 1:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="upsert 行数非法（须 >=1）",
            details={"n_rows": n_rows},
        )
    row_placeholder = "(%s, %s, %s, %s, %s, %s, %s, %s)"
    values_block = ",\n  ".join([row_placeholder] * n_rows)
    return (
        "INSERT INTO ff_memory_index\n"
        "  (persona_id, ref_type, ref_id, kind, provenance, ts_ms, content, embedding)\n"
        "VALUES\n"
        f"  {values_block}\n"
        "ON DUPLICATE KEY UPDATE\n"
        "  content = VALUES(content),\n"
        "  embedding = VALUES(embedding),\n"
        "  kind = VALUES(kind),\n"
        "  provenance = VALUES(provenance),\n"
        "  ts_ms = VALUES(ts_ms)"
    )


def build_delete_sql() -> str:
    """构建按引用删除语句（人格 + 类型 + 引用键三谓词——隔离面）。

    Returns:
        单条 DELETE 语句字符串（参数顺序：persona_id / ref_type / ref_id）。
    """
    return (
        "DELETE FROM ff_memory_index\nWHERE persona_id = %s\n  AND ref_type = %s\n  AND ref_id = %s"
    )


def encode_vector_literal(vector: Sequence[float]) -> str:
    """把浮点向量编码为 SQL 字面量字符串（紧凑 JSON 数组）。

    SeekDB 向量字面量是 JSON 数组**字符串**（事实卡：vector literal
    is a STRING）——经绑定参数传递（客户端转义，无注入面）。

    Args:
        vector: 浮点向量（维度须与索引表 VECTOR(dim) 一致——由存储层
            校验）。

    Returns:
        紧凑 JSON 数组字符串（如 ``"[0.1,0.2]"``）。
    """
    return json.dumps(list(vector), separators=(",", ":"))


def _require_mode_vector(query: MemorySearchQueryDTO, query_vector: Sequence[float] | None) -> None:
    """校验检索模式与查询向量的匹配契约（模式违规 fail loud）。

    契约矩阵（文档化决策——spec 语义澄清后锁定）：

        - dense / hybrid + 向量 None  → ``VALIDATION_ERROR``（向量缺失，
          无法执行 ANN 排序）；
        - keyword + 向量非 None       → ``VALIDATION_ERROR``（关键词模式
          不接收向量——调用方链路错误的编程错误面）。

    Args:
        query: 检索查询 DTO（mode 已归一）。
        query_vector: 查询向量（keyword 模式应为 None）。

    Raises:
        BusinessException: 模式与向量参数不匹配（``VALIDATION_ERROR``）。
    """
    if query.mode in (MemorySearchMode.DENSE, MemorySearchMode.HYBRID):
        if query_vector is None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="向量检索模式需要查询向量",
                details={"mode": query.mode.value, "query_vector": None},
            )
        return
    if query_vector is not None:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="关键词模式不接收查询向量",
            details={"mode": query.mode.value},
        )


def build_search_sql(
    query: MemorySearchQueryDTO, query_vector: Sequence[float] | None
) -> tuple[str, list[Any]]:
    """构建三模式混合检索语句（Form A——单条 SQL，谓词下推 ANN 扫描）。

    模式 → SQL 形态（事实卡逐条对齐）：

        - **dense**：SELECT 含 ``cosine_distance(embedding, %s) AS score``；
          WHERE 无 MATCH；``ORDER BY score APPROXIMATE LIMIT %s``；
        - **keyword**：SELECT 含 ``MATCH(content) AGAINST(%s) AS score``；
          WHERE 含 MATCH 谓词；``ORDER BY MATCH(content) AGAINST(%s) DESC
          LIMIT %s``（**无 APPROXIMATE**——无 ANN 面）；
        - **hybrid**：SELECT 含 cosine_distance score；WHERE 含 MATCH
          谓词（关键词预过滤）；``ORDER BY score APPROXIMATE LIMIT %s``。

    谓词组装顺序（参数与 SQL 出现序一致——左到右，逐行）：

        SELECT score 位（向量 JSON / 关键词）→ persona_id → kind IN
        → provenance → ts_ms >= → ts_ms <= → WHERE MATCH（keyword /
        hybrid）→ ORDER BY 关键词位（keyword）→ LIMIT。

    LIMIT 钳位：由查询 DTO 边界强制（1-50，``MemorySearchQueryDTO.limit``
    的 ge/le 校验——超界请求在 DTO 构造即 422，SQL 层无需二次钳位）。

    Args:
        query: 检索查询 DTO。
        query_vector: 查询向量（dense / hybrid 必填；keyword 须 None——
            违规即 ``VALIDATION_ERROR``，见 :func:`_require_mode_vector`）。

    Returns:
        ``(sql, params)`` 二元组（params 为按序绑定参数列表）。

    Raises:
        BusinessException: 模式与向量不匹配（``VALIDATION_ERROR``）。
    """
    _require_mode_vector(query, query_vector)

    select_columns = "id, ref_type, ref_id, kind, provenance, ts_ms, content"
    lines: list[str] = []
    params: list[Any] = []

    # SELECT 列 + score 表达式
    lines.append(f"SELECT {select_columns},")
    if query.mode == MemorySearchMode.KEYWORD:
        lines.append("       MATCH(content) AGAINST(%s) AS score")
        params.append(query.query)
    else:
        lines.append("       cosine_distance(embedding, %s) AS score")
        params.append(encode_vector_literal(query_vector if query_vector is not None else []))

    # FROM + WHERE 谓词（persona_id 恒首——隔离面）
    lines.append("FROM ff_memory_index")
    lines.append("WHERE persona_id = %s")
    params.append(query.persona_id)
    if query.kinds:
        placeholders = ", ".join(["%s"] * len(query.kinds))
        lines.append(f"  AND kind IN ({placeholders})")
        params.extend(query.kinds)
    if query.provenance is not None:
        lines.append("  AND provenance = %s")
        params.append(query.provenance.value)
    if query.start_ms is not None:
        lines.append("  AND ts_ms >= %s")
        params.append(query.start_ms)
    if query.end_ms is not None:
        lines.append("  AND ts_ms <= %s")
        params.append(query.end_ms)
    if query.mode in (MemorySearchMode.KEYWORD, MemorySearchMode.HYBRID):
        lines.append("  AND MATCH(content) AGAINST(%s)")
        params.append(query.query)

    # ORDER BY + LIMIT（模式分派：keyword 无 APPROXIMATE）
    if query.mode == MemorySearchMode.KEYWORD:
        lines.append("ORDER BY MATCH(content) AGAINST(%s) DESC LIMIT %s")
        params.append(query.query)
    else:
        lines.append("ORDER BY score APPROXIMATE LIMIT %s")
    params.append(query.limit)

    return "\n".join(lines), params
