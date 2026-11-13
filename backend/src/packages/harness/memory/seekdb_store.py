"""SeekDB 生产检索存储——MemoryStore 的 aiomysql raw SQL 实现。

架构位置（P1 chunk 2 检索层，文档化于 docs/backend/memory.md）：

    - 索引表 ``ff_memory_index``：向量（VECTOR(dim) + hnsw）+ 全文
      （jieba）+ 标量（persona_id / kind / provenance / ts_ms）混合检索
      ——**raw SQL only**（SeekDB 无 SQLAlchemy 方言；DDL 与查询形态
      见 memory.store 构建器，与事实卡逐字对齐）；
    - 连接：自有 aiomysql 池（构造注入优先——PoolManager 共享池；
      未注入时惰性自建，``close`` 归本存储）——**不共享** SQLAlchemy
      主库引擎（检索索引是独立数据面）；
    - 全部语句经 ``memory.store`` 构建器产出（SQL 形态单一真源）；
    - 行映射：驱动游标 tuple 行 + description 列名 → dict → 结果 DTO；
    - 错误映射：驱动异常（pymysql.err.Error 族）→ ``INTERNAL_ERROR``
      BusinessException（携带 operation / reason——错误码纪律：本
      chunk 不新增 SEEKB_* 成员，复用既有通用码；偏差见交付报告）。

人格隔离：索引表无 PersonaMixin 概念——``persona_id`` 是显式 BigInt
列 + 每条查询的必带谓词（构建器保证——见 memory.store 模块 docstring）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from loguru import logger

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.store import (
    build_delete_sql,
    build_ensure_schema_sql,
    build_search_sql,
    build_upsert_sql,
    encode_vector_literal,
)
from packages.harness.pools.seekdb_compat import (
    SeekDbPoolProtocol,
    create_seekdb_pool,
    resolve_seekdb_password,
    seekdb_error_base,
)

__all__ = ["SeekDBStore"]

#: 驱动异常基类（模块加载期解析——纯 import 零连接副作用）。
_ERROR_BASE: type[Exception] = seekdb_error_base()


def _result_from_row(row: dict[str, Any]) -> MemorySearchResultDTO:
    """驱动行 dict → 检索结果 DTO（列名取自游标 description）。

    Args:
        row: 列名 → 列值的映射（SELECT 列序见 build_search_sql）。

    Returns:
        归一后的结果 DTO。
    """
    return MemorySearchResultDTO(
        ref_type=MemoryRefType(str(row["ref_type"])),
        ref_id=int(row["ref_id"]),
        kind=str(row["kind"]),
        provenance=MemoryProvenance(str(row["provenance"])),
        ts_ms=int(row["ts_ms"]),
        content=str(row["content"]),
        score=float(row["score"]),
    )


class SeekDBStore:
    """SeekDB 检索存储（生产路径——aiomysql raw SQL 混合检索）。

    池所有权（文档化决策）：

        - 构造注入池（PoolManager 共享池）→ 生命周期归注入方，本存储
          ``close`` 为 no-op（app 停机经 ``pool_manager.stop()`` 收口——
          app/main.py 无需感知存储）；
        - 未注入 → 惰性自建（首次访问时创建），``close`` 关闭自持池
          （独立场景 / 冒烟测试面）。
    """

    def __init__(
        self, config: SeekDBStoreConfig, *, pool: SeekDbPoolProtocol | None = None
    ) -> None:
        """初始化存储（池注入优先；schema 惰性确保）。

        Args:
            config: 存储装配配置（host / port / 密码变量名 / vector_dim /
                池边界）。
            pool: 已就绪的 aiomysql 池（None = 惰性自建——按配置创建，
                close 归本存储）。
        """
        self._config = config
        self._pool: SeekDbPoolProtocol | None = pool
        self._owns_pool = pool is None
        self._schema_ensured = False
        self._schema_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # MemoryStore 协议面
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """确保索引表 DDL 就绪（幂等——once 守卫 + 锁防并发双建）。

        Raises:
            BusinessException: DDL 执行失败（``INTERNAL_ERROR``）。
        """
        if self._is_schema_ensured():
            return
        async with self._schema_lock:
            if self._is_schema_ensured():
                return
            await self._run_write(build_ensure_schema_sql(self._config.vector_dim))
            self._schema_ensured = True
            logger.info(
                "SeekDB 索引表已就绪: table=ff_memory_index, vector_dim={}",
                self._config.vector_dim,
            )

    async def upsert(self, entries: list[MemoryIndexEntryDTO], vectors: list[list[float]]) -> None:
        """批量写入索引条目（同键覆盖——ON DUPLICATE KEY UPDATE）。

        写前自动确保 schema（惰性 once）；条目与向量同序同长，向量
        维度须与配置 ``vector_dim`` 一致（系统边界校验——embedder 输出
        与索引表 DDL 的契约面）。

        Args:
            entries: 索引条目列表（非空——空批量短路返回）。
            vectors: 与条目同序的向量列表（JSON 字符串化后绑定）。

        Raises:
            BusinessException: 条目 / 向量数量不一致或维度不符
                （``VALIDATION_ERROR``）；驱动执行失败（``INTERNAL_ERROR``）。
        """
        if len(entries) != len(vectors):
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="索引条目与向量数量不一致",
                details={"entries": len(entries), "vectors": len(vectors)},
            )
        if not entries:
            return
        for index, vector in enumerate(vectors):
            if len(vector) != self._config.vector_dim:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="向量维度与索引表配置不一致",
                    details={
                        "entry_index": index,
                        "got_dim": len(vector),
                        "expected_dim": self._config.vector_dim,
                    },
                )
        await self.ensure_schema()
        sql = build_upsert_sql(len(entries))
        params: list[Any] = []
        for entry, vector in zip(entries, vectors, strict=True):
            params.extend(
                [
                    entry.persona_id,
                    entry.ref_type.value,
                    entry.ref_id,
                    entry.kind,
                    entry.provenance.value,
                    entry.ts_ms,
                    entry.content,
                    encode_vector_literal(vector),
                ]
            )
        await self._run_write(sql, params)
        logger.debug("SeekDB 索引批量写入: count={}", len(entries))

    async def delete(self, ref_type: MemoryRefType, ref_id: int, persona_id: int) -> None:
        """按引用唯一键删除索引行（人格 + 类型 + 引用键三谓词）。

        Args:
            ref_type: 引用类型（episode / curated）。
            ref_id: 引用行主键。
            persona_id: 人格主键（隔离谓词）。

        Raises:
            BusinessException: 驱动执行失败（``INTERNAL_ERROR``）。
        """
        await self._run_write(build_delete_sql(), [persona_id, ref_type.value, ref_id])
        logger.debug(
            "SeekDB 索引删除: persona={}, ref_type={}, ref_id={}",
            persona_id,
            ref_type.value,
            ref_id,
        )

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[MemorySearchResultDTO]:
        """按模式混合检索（SQL 构建 / 模式校验归 memory.store 构建器）。

        Args:
            query: 检索查询 DTO（persona_id 必带——隔离谓词）。
            query_vector: 查询向量（dense / hybrid 必填；keyword 须 None
                ——违规由构建器 fail loud）。

        Returns:
            结果 DTO 列表（score 方向随模式——见 retrieval_models）。

        Raises:
            BusinessException: 模式违规（``VALIDATION_ERROR``——构建器
                抛出）；驱动执行失败（``INTERNAL_ERROR``）。
        """
        sql, params = build_search_sql(query, query_vector)
        rows = await self._run_fetch(sql, params)
        return [_result_from_row(row) for row in rows]

    async def close(self) -> None:
        """关闭存储（幂等；池所有权分派见类 docstring）。

        自持池 → close + wait_closed；注入池 → no-op（生命周期归
        注入方——PoolManager 停机收口）。
        """
        if self._owns_pool and self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            logger.info("SeekDBStore 自持连接池已关闭")

    # ------------------------------------------------------------------
    # 内部——池管理 / 语句执行 / 错误映射
    # ------------------------------------------------------------------

    def _is_schema_ensured(self) -> bool:
        """读 once 守卫标记（方法面读取——绕开静态窄化对双检锁的误报）。

        Returns:
            schema 是否已确保。
        """
        return self._schema_ensured

    async def _require_pool(self) -> SeekDbPoolProtocol:
        """取连接池（未注入时惰性自建——按配置 + 密码 env 解析）。

        Returns:
            就绪的连接池。

        Raises:
            BusinessException: 池创建失败（``INTERNAL_ERROR``——含
                SeekDB 不可达等驱动错误映射）。
        """
        if self._pool is None:
            password = resolve_seekdb_password(self._config.password_env)
            try:
                self._pool = await create_seekdb_pool(
                    host=self._config.host,
                    port=self._config.port,
                    user=self._config.user,
                    password=password,
                    database=self._config.database,
                    pool_min=self._config.pool_min,
                    pool_max=self._config.pool_max,
                )
            except _ERROR_BASE as exc:
                raise self._map_error("create_pool", exc) from exc
        return self._pool

    async def _run_write(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """执行写语句（DDL / INSERT / DELETE——autocommit 单语句自决）。

        Args:
            sql: 语句字符串（构建器产出）。
            params: 绑定参数（None = 无参语句）。

        Raises:
            BusinessException: 驱动执行失败（``INTERNAL_ERROR``）。
        """
        pool = await self._require_pool()
        try:
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
        except BusinessException:
            raise
        except _ERROR_BASE as exc:
            raise self._map_error("write", exc) from exc

    async def _run_fetch(self, sql: str, params: Sequence[Any] | None) -> list[dict[str, Any]]:
        """执行查询语句并取回全部行（tuple 行 → 列名 dict 归一）。

        Args:
            sql: 查询语句字符串（构建器产出）。
            params: 绑定参数。

        Returns:
            行 dict 列表（列名来自游标 description 首元素）。

        Raises:
            BusinessException: 驱动执行失败（``INTERNAL_ERROR``）。
        """
        pool = await self._require_pool()
        try:
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
                description = cur.description
                rows = await cur.fetchall()
        except BusinessException:
            raise
        except _ERROR_BASE as exc:
            raise self._map_error("fetch", exc) from exc
        if not description:
            return []
        columns = [str(column[0]) for column in description]
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def _map_error(self, operation: str, exc: Exception) -> BusinessException:
        """驱动异常 → 业务异常（错误码纪律：复用 INTERNAL_ERROR）。

        Args:
            operation: 失败操作面（write / fetch / create_pool——入
                details 供排障）。
            exc: 驱动原始异常。

        Returns:
            携带操作面与原因的 :class:`BusinessException`。
        """
        return BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="SeekDB 检索存储访问失败",
            details={
                "operation": operation,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
