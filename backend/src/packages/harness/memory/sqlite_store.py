"""SQLite 回退检索存储——keyword 模式专用（MemoryStore 协议实现）。

定位（文档化决策）：

    - **谁用**：``settings.seekdb.enabled=False``（默认——零配置开发 /
      无 SeekDB 部署）时的全工程检索存储；亦是测试面（共享 SQLite
      fixture——零真实网络 / 零向量依赖）；
    - **能做什么**：keyword 模式检索（LIKE 子串匹配 + 朴素相关度
      ——查询串在正文中大小写不敏感的出现次数）；dense / hybrid 模式
      **不支持**（清晰 ``VALIDATION_ERROR``：「回退存储不支持向量检索」
      ——向量检索须启用 SeekDB）；
    - **引擎**：运行时共享 SQLAlchemy 引擎（``db_engine_holder``——每
      次调用时读取，支持热重建 / 测试外部安装；**不捕获**引擎引用）；
    - **schema**：``ff_memory_index_lite`` 表经 Base metadata 声明（本文
      件内 ORM 行类）——``ensure_schema`` 以 ``create(checkfirst=True)``
      幂等落表（选择 Base metadata 而非裸 DDL 的原因：与主库 ORM 同
      一注册面，共享引擎 fixture 的 create_all / 自动清表 / init_database
      兜底建表全部自动覆盖）；向量列不存在（keyword-only——upsert 的
      vectors 参数收下即弃，协议统一性）。

人格隔离：回退表**不挂 PersonaMixin**（与 SeekDB 索引表同构的显式
谓词隔离）——persona_id 是显式 BigInteger 列，全部读写路径带
``persona_id`` 谓词。
"""

from __future__ import annotations

from typing import Any, cast

from loguru import logger
from sqlalchemy import BigInteger, Index, String, Table, Text, UniqueConstraint, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.base import Base, BigIntIdMixin
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)

__all__ = ["SQLiteFallbackStore", "MemoryIndexLiteRow"]


class MemoryIndexLiteRow(Base, BigIntIdMixin):
    """检索索引回退表行（ff_memory_index_lite——keyword 模式专用）。

    与 SeekDB 索引表（ff_memory_index）的字段对齐子集：引用键 + 标量
    列 + 正文；**无向量列**（keyword-only——向量检索归 SeekDB 路径）。
    非 PersonaMixin——人格隔离经显式 ``persona_id`` 谓词（与 SeekDB
    路径同构，见模块 docstring）。

    Attributes:
        persona_id: 人格主键（BigInt——显式谓词隔离轴）。
        ref_type: 引用类型（episode / curated）。
        ref_id: 引用行主键。
        kind: 记忆类型值（episode kind 或 curated kind）。
        provenance: 来源可信度。
        ts_ms: 事件时间（epoch 毫秒）。
        content: 索引正文（LIKE 匹配面）。
    """

    __tablename__ = "ff_memory_index_lite"
    __table_args__ = (
        UniqueConstraint("persona_id", "ref_type", "ref_id", name="uq_ff_mem_lite_ref"),
        Index("ix_ff_memory_index_lite_persona", "persona_id"),
    )

    persona_id: Mapped[int] = mapped_column(
        BigInteger, comment="人格主键（显式谓词隔离——检索索引无 PersonaMixin）"
    )
    ref_type: Mapped[str] = mapped_column(String(32), comment="引用类型（episode/curated）")
    ref_id: Mapped[int] = mapped_column(BigInteger, comment="引用行主键")
    kind: Mapped[str] = mapped_column(String(32), comment="记忆类型值（episode/curated kind）")
    provenance: Mapped[str] = mapped_column(String(16), comment="来源可信度")
    ts_ms: Mapped[int] = mapped_column(BigInteger, comment="事件时间（epoch 毫秒）")
    content: Mapped[str] = mapped_column(Text, comment="索引正文（keyword LIKE 匹配面）")


def _create_lite_table(sync_conn: Any) -> None:
    """在同步连接上创建回退表（checkfirst 幂等——run_sync 适配面）。

    Args:
        sync_conn: run_sync 传入的同步 Connection。
    """
    # 声明类 __table__ 的静态面是 FromClause——cast 收口到 Table
    # （create_all 的 tables 形参契约；SQLAlchemy 2.0 已知类型缺口）。
    lite_table = cast("Table", MemoryIndexLiteRow.__table__)
    Base.metadata.create_all(bind=sync_conn, tables=[lite_table], checkfirst=True)


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符（``%`` / ``_`` / ``\\``——escape 子句配合）。

    Args:
        text: 原始查询文本。

    Returns:
        转义后的文本（配合 ``like(..., escape="\\\\")`` 使用）。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SQLiteFallbackStore:
    """SQLite 回退检索存储（keyword-only——共享引擎 + 显式谓词隔离）。

    引擎获取：每次操作时从 :data:`db_engine_holder` 读取当前引擎
    （None → fail loud）——不捕获构造期引擎，热重建 / 测试外部安装
    天然兼容。
    """

    def __init__(self) -> None:
        """初始化（无参数——schema 惰性确保；引擎调用期读取）。"""
        self._schema_ensured = False

    # ------------------------------------------------------------------
    # MemoryStore 协议面
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """确保回退表就绪（幂等——once 守卫 + checkfirst 建表）。

        Raises:
            BusinessException: 共享引擎未初始化（``INTERNAL_ERROR``）；
                建表失败（``INTERNAL_ERROR``）。
        """
        if self._schema_ensured:
            return
        engine = self._require_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(_create_lite_table)
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="检索回退表建表失败",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        self._schema_ensured = True
        logger.debug("检索回退表已就绪: table=ff_memory_index_lite")

    async def upsert(self, entries: list[MemoryIndexEntryDTO], vectors: list[list[float]]) -> None:
        """批量写入索引条目（同键覆盖——sqlite ON CONFLICT DO UPDATE）。

        vectors 参数**收下即弃**（keyword-only 回退——协议统一性；条目
        与向量数量仍做一致性校验——调用方契约面）。

        Args:
            entries: 索引条目列表（空批量短路返回）。
            vectors: 与条目同序的向量列表（回退路径不消费）。

        Raises:
            BusinessException: 条目 / 向量数量不一致（``VALIDATION_ERROR``）；
                引擎未初始化 / 写入失败（``INTERNAL_ERROR``）。
        """
        if len(entries) != len(vectors):
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="索引条目与向量数量不一致",
                details={"entries": len(entries), "vectors": len(vectors)},
            )
        if not entries:
            return
        await self.ensure_schema()
        engine = self._require_engine()
        values = [
            {
                "persona_id": entry.persona_id,
                "ref_type": entry.ref_type.value,
                "ref_id": entry.ref_id,
                "kind": entry.kind,
                "provenance": entry.provenance.value,
                "ts_ms": entry.ts_ms,
                "content": entry.content,
            }
            for entry in entries
        ]
        stmt = sqlite_insert(MemoryIndexLiteRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["persona_id", "ref_type", "ref_id"],
            set_={
                "kind": stmt.excluded.kind,
                "provenance": stmt.excluded.provenance,
                "ts_ms": stmt.excluded.ts_ms,
                "content": stmt.excluded.content,
            },
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(stmt)
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="检索回退存储写入失败",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        logger.debug("检索回退索引批量写入: count={}", len(entries))

    async def delete(self, ref_type: MemoryRefType, ref_id: int, persona_id: int) -> None:
        """按引用唯一键删除索引行（人格 + 类型 + 引用键三谓词）。

        Args:
            ref_type: 引用类型（episode / curated）。
            ref_id: 引用行主键。
            persona_id: 人格主键（隔离谓词）。

        Raises:
            BusinessException: 引擎未初始化 / 删除失败（``INTERNAL_ERROR``）。
        """
        engine = self._require_engine()
        stmt = delete(MemoryIndexLiteRow).where(
            MemoryIndexLiteRow.persona_id == persona_id,
            MemoryIndexLiteRow.ref_type == ref_type.value,
            MemoryIndexLiteRow.ref_id == ref_id,
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(stmt)
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="检索回退存储删除失败",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[MemorySearchResultDTO]:
        """keyword 模式检索（LIKE 子串 + 朴素相关度排序）。

        朴素相关度：查询串（大小写不敏感）在正文中出现次数（降序），
        同分按 ts_ms 降序、id 降序稳定排列——与 SeekDB keyword 模式的
        MATCH 相关度方向一致（越大越相关）。排序在 Python 侧完成
        （SQLite 无 MATCH 相关度函数——文档化近似）。

        Args:
            query: 检索查询 DTO（persona_id 必带——隔离谓词；时间范围
                闭区间语义与 SeekDB 路径一致）。
            query_vector: 查询向量（keyword 模式须 None——违规
                ``VALIDATION_ERROR``）。

        Returns:
            结果 DTO 列表（score = 出现次数）。

        Raises:
            BusinessException: dense / hybrid 模式（「回退存储不支持向量
                检索」——``VALIDATION_ERROR``）；keyword 模式携带向量
                （``VALIDATION_ERROR``）；引擎未初始化（``INTERNAL_ERROR``）。
        """
        if query.mode != MemorySearchMode.KEYWORD:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="回退存储不支持向量检索（仅 keyword 模式可用）",
                details={
                    "mode": query.mode.value,
                    "fix": "启用 SeekDB（SEEKDB_ENABLED=true）或改用 mode=keyword",
                },
            )
        if query_vector is not None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="关键词模式不接收查询向量",
                details={"mode": query.mode.value},
            )
        await self.ensure_schema()
        engine = self._require_engine()
        pattern = f"%{_escape_like(query.query)}%"
        conditions: list[Any] = [
            MemoryIndexLiteRow.persona_id == query.persona_id,
            MemoryIndexLiteRow.content.like(pattern, escape="\\"),
        ]
        if query.kinds:
            conditions.append(MemoryIndexLiteRow.kind.in_(query.kinds))
        if query.provenance is not None:
            conditions.append(MemoryIndexLiteRow.provenance == query.provenance.value)
        if query.start_ms is not None:
            conditions.append(MemoryIndexLiteRow.ts_ms >= query.start_ms)
        if query.end_ms is not None:
            conditions.append(MemoryIndexLiteRow.ts_ms <= query.end_ms)
        stmt = select(MemoryIndexLiteRow.__table__).where(*conditions)
        try:
            async with engine.connect() as conn:
                rows = list((await conn.execute(stmt)).all())
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="检索回退存储查询失败",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        needle = query.query.lower()
        scored = sorted(
            rows,
            key=lambda row: (
                -str(row.content).lower().count(needle),
                -int(row.ts_ms),
                -int(row.id),
            ),
        )[: query.limit]
        return [
            MemorySearchResultDTO(
                ref_type=MemoryRefType(str(row.ref_type)),
                ref_id=int(row.ref_id),
                kind=str(row.kind),
                provenance=MemoryProvenance(str(row.provenance)),
                ts_ms=int(row.ts_ms),
                content=str(row.content),
                score=float(str(row.content).lower().count(needle)),
            )
            for row in scored
        ]

    async def close(self) -> None:
        """关闭存储（no-op——引擎生命周期归 db_engine_holder / 安装方）。"""
        return

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_engine(self) -> AsyncEngine:
        """读取当前共享异步引擎（调用期读取——热重建 / 外部安装兼容）。

        Returns:
            当前安装的 :class:`AsyncEngine`。

        Raises:
            BusinessException: 引擎未初始化（``INTERNAL_ERROR``——
                lifespan 先 init_database / 测试先安装共享引擎）。
        """
        engine = db_engine_holder.async_engine
        if engine is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="数据库引擎未初始化（检索回退存储依赖共享引擎）",
            )
        return engine
