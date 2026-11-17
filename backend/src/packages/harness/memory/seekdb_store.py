"""SeekDB 生产检索存储——MemoryStore 的 aiomysql raw SQL 实现。

架构位置（检索层，文档化于 docs/backend/memory.md）：

    - 索引表 ``ff_memory_index``：向量（VECTOR(dim) + hnsw）+ 全文
      （ik）+ 标量（persona_id / kind / provenance / ts_ms）混合检索
      ——**raw SQL only**（SeekDB 无 SQLAlchemy 方言；DDL 与查询形态
      见 memory.store 构建器，与事实卡逐字对齐）；hybrid 模式经官方
      融合引擎 ``DBMS_HYBRID_SEARCH.SEARCH``（Form B——单行单列 JSON
      载荷，见下）；
    - 连接：自有 aiomysql 池（构造注入优先——PoolManager 共享池；
      未注入时惰性自建，``close`` 归本存储）——**不共享** SQLAlchemy
      主库引擎（检索索引是独立数据面）；
    - 全部语句经 ``memory.store`` 构建器产出（SQL 形态单一真源）；
    - 行映射：Form A（dense / keyword）——驱动游标 tuple 行 +
      description 列名 → dict → 结果 DTO；Form B（hybrid）——SEARCH
      单行单列 JSON 载荷 → json.loads → 行 dict（全表列 + ``_score``
      融合相关度，越大越相关）→ 结果 DTO（畸形行警告 + 跳过——
      永不逐行抛出；空集 = SQL NULL 单元格 → 空列表，live 实测形态）；
    - 错误映射：驱动异常（pymysql.err.Error 族）与 hybrid 载荷形态
      异常 → ``INTERNAL_ERROR`` BusinessException（携带 operation /
      reason——write / fetch / create_pool / hybrid_parse；错误码纪律：
      不新增 SEEKB_* 成员，复用既有通用码）。

人格隔离：索引表无 PersonaMixin 概念——``persona_id`` 是显式 BigInt
列 + 每条查询的必带谓词（构建器保证——见 memory.store 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from loguru import logger

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
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


def _hybrid_result_from_row(row: object) -> MemorySearchResultDTO | None:
    """DBMS_HYBRID_SEARCH 命中行 → 检索结果 DTO（畸形行警告 + 跳过）。

    Form B 行 dict 携带全表列 + ``_score``（融合相关度，越大越相关
    ——方向与 Form A dense 的 cosine 距离相反）；provenance 取存储
    字符串直映（值域枚举不合法即畸形）。畸形行（非 JSON 对象 / 缺
    ``_score`` / 引用与信任列缺失或非法）**警告 + 跳过，永不逐行
    抛出**；展示列（kind / ts_ms / content）缺失取空 / 0 缺省。

    Args:
        row: json.loads 产出的命中行（形态不可信——非 dict / 缺列容忍）。

    Returns:
        归一后的结果 DTO；畸形行返回 None（调用方跳过）。
    """
    if not isinstance(row, dict):
        logger.warning("SeekDB hybrid 检索畸形行已跳过（非 JSON 对象）: row={!r}", row)
        return None
    try:
        return MemorySearchResultDTO(
            ref_type=MemoryRefType(str(row["ref_type"])),
            ref_id=int(row["ref_id"]),
            kind=str(row.get("kind", "")),
            provenance=MemoryProvenance(str(row["provenance"])),
            ts_ms=int(row.get("ts_ms", 0)),
            content=str(row.get("content", "")),
            score=float(row["_score"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "SeekDB hybrid 检索畸形行已跳过: reason={!r}, row_keys={!r}",
            f"{type(exc).__name__}: {exc}",
            sorted(row.keys()),
        )
        return None


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
        # 池创建专用锁（与 schema 锁分立——ensure_schema 持 schema 锁
        # 执行 DDL 期间会经 _run_write 触发池创建，同一把锁会自锁死锁）
        self._pool_lock = asyncio.Lock()

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
        """按模式混合检索（hybrid = Form B 官方融合引擎；其余 = Form A）。

        SQL 构建 / 模式校验归 memory.store 构建器；hybrid 分派到
        :meth:`_search_hybrid`（单行单列 JSON 载荷解析 + 逐行映射）。

        Args:
            query: 检索查询 DTO（persona_id 必带——隔离谓词）。
            query_vector: 查询向量（dense / hybrid 必填；keyword 须 None
                ——违规由构建器 fail loud）。

        Returns:
            结果 DTO 列表（score 方向随模式：dense = cosine 距离越小越
            相关；keyword = MATCH 相关度、hybrid = 融合 ``_score`` 均越大
            越相关——hybrid 与 dense 方向相反，调用方按 mode 解释）。

        Raises:
            BusinessException: 模式违规（``VALIDATION_ERROR``——构建器
                抛出）；驱动执行失败 / hybrid 载荷形态异常
                （``INTERNAL_ERROR``）。
        """
        sql, params = build_search_sql(query, query_vector)
        if query.mode == MemorySearchMode.HYBRID:
            return await self._search_hybrid(sql, params)
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
        """取连接池（未注入时惰性自建——双检锁防并发冷启动双建）。

        自建经 ``_pool_lock`` 串行化（与 schema 锁分立——schema 锁在
        ``ensure_schema`` 持锁执行 DDL 期间会经本方法嵌套获取，同锁
        自锁死锁）；首个检查无锁快速路径，锁内二次确认后按配置 +
        密码 env 解析创建。

        Returns:
            就绪的连接池。

        Raises:
            BusinessException: 池创建失败（``INTERNAL_ERROR``——含
                SeekDB 不可达等驱动错误映射）。
        """
        if self._pool is None:
            async with self._pool_lock:
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

    async def _search_hybrid(self, sql: str, params: Sequence[Any]) -> list[MemorySearchResultDTO]:
        """执行 Form B 检索并解析单行单列 JSON 载荷（hybrid 专用路径）。

        结果契约（live 实测，v1.4.0）：SEARCH 恒返回单行单列——命中集
        为 JSON 数组字符串（行 dict 含全表列 + ``_score``，引擎已按
        融合分降序排列）；**空集为 SQL NULL 单元格**（非 ``"[]"``）。
        载荷形态偏离该契约（行数 / 列数 / 单元格类型 / JSON 形态）→
        ``INTERNAL_ERROR`` fail loud（协议面异常不该静默吞掉）；行级
        畸形由 :func:`_hybrid_result_from_row` 警告 + 跳过。

        Args:
            sql: SEARCH 语句（构建器产出）。
            params: 绑定参数（单元素——JSON DSL 字符串）。

        Returns:
            结果 DTO 列表（引擎融合分降序保持）。

        Raises:
            BusinessException: 载荷形态异常（``INTERNAL_ERROR``——含
                行数 / 列数偏离单行单列）；驱动执行失败
                （``INTERNAL_ERROR``——_run_fetch 映射）。
        """
        rows = await self._run_fetch(sql, params)
        if len(rows) != 1:
            raise self._hybrid_payload_error(f"SEARCH 语句应返回单行单列，实际行数 {len(rows)}")
        first = rows[0]
        if not first:
            raise self._hybrid_payload_error("SEARCH 语句应返回单行单列，实际首行零列")
        if len(first) != 1:
            raise self._hybrid_payload_error(
                f"SEARCH 语句应返回单行单列，实际列数 {len(first)}"
            )
        cell = next(iter(first.values()))
        if cell is None:
            return []
        if not isinstance(cell, str):
            raise self._hybrid_payload_error(
                f"SEARCH 结果单元格非 JSON 字符串: {type(cell).__name__}"
            )
        try:
            raw_rows: Any = json.loads(cell)
        except json.JSONDecodeError as exc:
            raise self._hybrid_payload_error(f"SEARCH 载荷 JSON 解析失败: {exc}") from exc
        if not isinstance(raw_rows, list):
            raise self._hybrid_payload_error(
                f"SEARCH 载荷不是 JSON 数组: {type(raw_rows).__name__}"
            )
        results: list[MemorySearchResultDTO] = []
        for raw_row in raw_rows:
            mapped = _hybrid_result_from_row(raw_row)
            if mapped is not None:
                results.append(mapped)
        return results

    def _hybrid_payload_error(self, reason: str) -> BusinessException:
        """构造 hybrid 载荷形态异常（与 _map_error 同形——operation 面）。

        Args:
            reason: 形态异常原因（入 details 供排障）。

        Returns:
            携带 ``hybrid_parse`` 操作面的 :class:`BusinessException`。
        """
        return BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="SeekDB 检索存储访问失败",
            details={"operation": "hybrid_parse", "reason": reason},
        )

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
