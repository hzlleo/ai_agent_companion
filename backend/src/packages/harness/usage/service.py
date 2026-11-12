"""usage 域服务——用量记录创建（单条 / 批量）/ 游标分页查询 / 多维聚合。

全部经 ``database.session.session_scope``（成功提交 / 异常回滚 / 恒
关闭）；本表非 PersonaMixin 人格作用域（见 orm 模块 docstring 设计
决策）——人格过滤由 ``UsageFilterFields.persona_id`` 显式承担。

聚合口径：

    - 分组维度：model_entry / purpose / time_bucket（minute / hour /
      day 三档桶宽，桶起点 = ``floor(ts_ms / 桶宽) * 桶宽``——跨方言
      经 ``floor`` 归一整数除法语义）；
    - 指标：input / output / total 求和、调用计数、平均时延（四舍
      五入取整）、失败计数；
    - 排序：总 token 降序为主序（成本视角），分组键升序为次序
      （确定性输出）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import session_scope
from packages.harness.usage.dtos import (
    UsageAggregateDTO,
    UsageAggregateQueryDTO,
    UsageCreateDTO,
    UsageFilterFields,
    UsageQueryDTO,
    UsageRecordDTO,
)
from packages.harness.usage.models import TIME_BUCKET_MS, UsageGroupBy, UsageStatus
from packages.harness.usage.orm import UsageRecordRow

__all__ = ["UsageService"]


def _to_row(dto: UsageCreateDTO) -> UsageRecordRow:
    """创建 DTO → ORM 行（枚举落字符串值）。

    Args:
        dto: 创建 DTO。

    Returns:
        未持久化的 :class:`UsageRecordRow` 实例。
    """
    return UsageRecordRow(
        persona_id=dto.persona_id,
        ts_ms=dto.ts_ms,
        model_entry=dto.model_entry,
        provider=dto.provider,
        purpose=dto.purpose.value,
        input_tokens=dto.input_tokens,
        output_tokens=dto.output_tokens,
        total_tokens=dto.total_tokens,
        latency_ms=dto.latency_ms,
        status=dto.status.value,
        request_id=dto.request_id,
        error_code=dto.error_code,
    )


def _filter_conditions(query: UsageFilterFields) -> list[ColumnElement[bool]]:
    """公共过滤面 → WHERE 条件列表（时间左闭右开）。

    Args:
        query: 过滤字段（记录查询 / 聚合查询共用形态）。

    Returns:
        条件列表（空列表 = 无过滤）。
    """
    conditions: list[ColumnElement[bool]] = []
    if query.start_ms is not None:
        conditions.append(UsageRecordRow.ts_ms >= query.start_ms)
    if query.end_ms is not None:
        conditions.append(UsageRecordRow.ts_ms < query.end_ms)
    if query.persona_id is not None:
        conditions.append(UsageRecordRow.persona_id == query.persona_id)
    if query.model_entry is not None:
        conditions.append(UsageRecordRow.model_entry == query.model_entry)
    if query.purpose is not None:
        conditions.append(UsageRecordRow.purpose == query.purpose.value)
    if query.status is not None:
        conditions.append(UsageRecordRow.status == query.status.value)
    return conditions


class UsageService:
    """token 用量域服务（无状态——Session 经 session_scope 每调用获取）。"""

    async def create(self, dto: UsageCreateDTO) -> UsageRecordDTO:
        """单条创建（llm 网关默认记录器消费面）。

        Args:
            dto: 创建 DTO。

        Returns:
            落库后的记录 DTO（含主键）。
        """
        row = _to_row(dto)
        async with session_scope() as session:
            session.add(row)
            await session.flush()
            record = UsageRecordDTO.model_validate(row)
        return record

    async def create_batch(self, dtos: Sequence[UsageCreateDTO]) -> list[UsageRecordDTO]:
        """批量创建（单事务批量 INSERT——回填 / 批量记账消费面）。

        Args:
            dtos: 创建 DTO 序列（空序列直接返回空列表）。

        Returns:
            落库后的记录 DTO 列表（与入参同序）。
        """
        if not dtos:
            return []
        rows = [_to_row(dto) for dto in dtos]
        async with session_scope() as session:
            session.add_all(rows)
            await session.flush()
            records = [UsageRecordDTO.model_validate(row) for row in rows]
        logger.info("用量记录批量落库: count={}", len(records))
        return records

    async def query(self, query: UsageQueryDTO) -> PageResponse[UsageRecordDTO]:
        """游标分页查询（id 降序——新记录在前；首页到尾页确定性遍历）。

        Args:
            query: 查询 DTO（过滤面 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1
            ——计数查询不在分页路径上执行，管理面按需另行聚合）。
        """
        stmt = select(UsageRecordRow).where(*_filter_conditions(query))
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(UsageRecordRow.id < cursor_id)
        stmt = stmt.order_by(desc(UsageRecordRow.id)).limit(query.limit + 1)
        async with session_scope() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[UsageRecordDTO.model_validate(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def aggregate(self, query: UsageAggregateQueryDTO) -> list[UsageAggregateDTO]:
        """多维聚合查询（管理面总览；分组行按总 token 降序）。

        Args:
            query: 聚合查询 DTO（过滤面 + 分组维度）。

        Returns:
            聚合结果行列表（时间维度携带 ``bucket_ms`` 数值面）。

        Raises:
            BusinessException: ``group_by=time_bucket`` 未声明桶宽
                （``VALIDATION_ERROR``）。
        """
        # 分组表达式联合标注：列属性（InstrumentedAttribute[str]）与计算列
        # （func.floor 乘法，ColumnElement[Any]——SQLAlchemy 计算列的公开
        # 类型面）两形态；窄化任一侧都会引入赋值不兼容。
        group_expr: InstrumentedAttribute[str] | ColumnElement[Any]
        if query.group_by == UsageGroupBy.MODEL_ENTRY:
            group_expr = UsageRecordRow.model_entry
            is_time_dimension = False
        elif query.group_by == UsageGroupBy.PURPOSE:
            group_expr = UsageRecordRow.purpose
            is_time_dimension = False
        else:
            if query.time_bucket is None:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    details={
                        "reason": "time_bucket 分组维度必须声明桶宽",
                        "group_by": query.group_by.value,
                        "valid_buckets": [member.value for member in TIME_BUCKET_MS],
                    },
                )
            bucket_ms = TIME_BUCKET_MS[query.time_bucket]
            # 跨方言整数除法归一：MySQL int/int 为 DECIMAL 除法（须 floor）；
            # SQLite int/int 已是整数除法（floor 为恒等）——统一 floor 保证
            # 桶起点语义一致（SQLite floor 返回 REAL，结果侧 int() 归一）。
            group_expr = func.floor(UsageRecordRow.ts_ms / bucket_ms) * bucket_ms
            is_time_dimension = True

        sum_input = func.sum(UsageRecordRow.input_tokens).label("sum_input")
        sum_output = func.sum(UsageRecordRow.output_tokens).label("sum_output")
        sum_total = func.sum(UsageRecordRow.total_tokens).label("sum_total")
        call_count = func.count(UsageRecordRow.id).label("call_count")
        avg_latency = func.avg(UsageRecordRow.latency_ms).label("avg_latency")
        error_count = func.sum(
            case((UsageRecordRow.status == UsageStatus.ERROR.value, 1), else_=0)
        ).label("error_count")

        stmt = (
            select(
                group_expr.label("group_key"),
                sum_input,
                sum_output,
                sum_total,
                call_count,
                avg_latency,
                error_count,
            )
            .where(*_filter_conditions(query))
            .group_by(group_expr)
            .order_by(sum_total.desc(), group_expr.asc())
        )
        async with session_scope() as session:
            rows = list((await session.execute(stmt)).all())

        results: list[UsageAggregateDTO] = []
        for row in rows:
            group_raw = row.group_key
            if is_time_dimension:
                bucket_start = int(group_raw)
                group_value: str = str(bucket_start)
            else:
                bucket_start = None
                group_value = str(group_raw)
            results.append(
                UsageAggregateDTO(
                    group=group_value,
                    bucket_ms=bucket_start,
                    input_tokens=int(row.sum_input or 0),
                    output_tokens=int(row.sum_output or 0),
                    total_tokens=int(row.sum_total or 0),
                    call_count=int(row.call_count or 0),
                    avg_latency_ms=int(round(float(row.avg_latency or 0.0))),
                    error_count=int(row.error_count or 0),
                )
            )
        return results
