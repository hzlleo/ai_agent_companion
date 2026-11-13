"""用量观测 admin 路由——/api/v1/usage（P2 chunk 1 交付）。

    - GET /api/v1/usage/records     —— 用量记录游标分页（过滤面）
    - GET /api/v1/usage/aggregates  —— 多维聚合（model_entry / purpose / time_bucket）
    - GET /api/v1/usage/summary     —— 总览（总量 + 均时延 + per-purpose 明细）

**跨人格观测语义（文档化决策）**：用量是跨人格 observability 数据——
本表 persona_id 为普通可空索引列（非 PersonaMixin，见 harness.usage.orm
设计决策），全部端点**无人格上下文要求**（不解析目标人格、不绑定
persona contextvar）；人格过滤经 ``persona_id`` 查询参数显式表达
（None = 全人格——管理面口径）。

Session 语义（文档化决策）：:class:`UsageService` 自持
``session_scope``（每调用获取 / 成功提交 / 异常回滚——服务层会话边界，
见该模块 docstring），因此本路由**无 DB 依赖注入**（不挂
``Depends(get_async_db)``——多余 Session 徒增开销）；引擎经
engine holder 槽位由 lifespan 启动期装配。

聚合口径对齐：``group_by`` 暴露服务层既有维度（model_entry / purpose /
time_bucket——后者的桶宽经 ``time_bucket`` 参数声明 minute / hour / day
三档）；不新增分组维度。总览端点经 ``group_by=purpose`` 聚合行派生：
总量 / 计数 / 失败数为精确求和；整体平均时延按 per-purpose 均值（已
取整）的调用次数加权——与逐记录加权均值的偏差 < 1ms（观测面可接受，
文档化于此）。全部响应经 ``ResultDTO`` 封装。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.usage.dtos import (
    UsageAggregateDTO,
    UsageAggregateQueryDTO,
    UsageQueryDTO,
    UsageRecordDTO,
)
from packages.harness.usage.models import (
    UsageGroupBy,
    UsagePurpose,
    UsageStatus,
    UsageTimeBucket,
)
from packages.harness.usage.service import UsageService

__all__ = ["router", "usage_service", "UsageSummaryDTO"]

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])

#: 用量服务实例（无状态——Session 经 session_scope 每调用获取，可安全共享）。
usage_service = UsageService()


class UsageSummaryDTO(BaseModel):
    """用量总览 DTO（单请求聚合面——时间范围 + 过滤的整体口径）。

    整体平均时延按 per-purpose 聚合行均值（已取整）的调用次数加权
    （派生口径，偏差 < 1ms——见模块 docstring）。

    Attributes:
        call_count: 调用总次数。
        input_tokens: 输入 token 总量。
        output_tokens: 输出 token 总量。
        total_tokens: 总 token 总量。
        avg_latency_ms: 整体平均时延（毫秒，四舍五入取整）。
        error_count: 失败总次数（status=error）。
        purposes: per-purpose 聚合明细（总量降序——同聚合口径）。
    """

    call_count: int = Field(default=0, ge=0, description="调用总次数")
    input_tokens: int = Field(default=0, ge=0, description="输入 token 总量")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 总量")
    total_tokens: int = Field(default=0, ge=0, description="总 token 总量")
    avg_latency_ms: int = Field(default=0, ge=0, description="整体平均时延（毫秒）")
    error_count: int = Field(default=0, ge=0, description="失败总次数")
    purposes: list[UsageAggregateDTO] = Field(
        default_factory=list, description="per-purpose 聚合明细（总量降序）"
    )


def _summarize(purpose_rows: list[UsageAggregateDTO]) -> UsageSummaryDTO:
    """per-purpose 聚合行 → 总览 DTO（求和精确；均时延按次数加权）。

    Args:
        purpose_rows: ``group_by=purpose`` 聚合结果行（总量降序）。

    Returns:
        总览 DTO（空结果返回全零 + 空明细）。
    """
    call_count = sum(row.call_count for row in purpose_rows)
    weighted_latency = sum(row.avg_latency_ms * row.call_count for row in purpose_rows)
    return UsageSummaryDTO(
        call_count=call_count,
        input_tokens=sum(row.input_tokens for row in purpose_rows),
        output_tokens=sum(row.output_tokens for row in purpose_rows),
        total_tokens=sum(row.total_tokens for row in purpose_rows),
        avg_latency_ms=round(weighted_latency / call_count) if call_count else 0,
        error_count=sum(row.error_count for row in purpose_rows),
        purposes=purpose_rows,
    )


@router.get("/records", response_model=ResultDTO[PageResponse[UsageRecordDTO]])
async def list_usage_records(
    persona_id: str | None = Query(None, description="人格 ID 过滤（None = 全人格）"),
    model_entry: str | None = Query(None, description="模型条目名过滤"),
    purpose: UsagePurpose | None = Query(None, description="调用用途过滤"),
    status: UsageStatus | None = Query(None, description="调用结果过滤"),
    start_ms: int | None = Query(None, ge=0, description="范围起点（epoch 毫秒，含）"),
    end_ms: int | None = Query(None, ge=0, description="范围终点（epoch 毫秒，不含）"),
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
) -> ResultDTO[PageResponse[UsageRecordDTO]]:
    """用量记录游标分页（id 降序——新记录在前；过滤面全可选）。

    时间范围为左闭右开（``start_ms <= ts_ms < end_ms``）——与聚合 /
    总览端点同口径。跨人格观测：无人格上下文要求，``persona_id``
    缺省即全人格。

    Args:
        persona_id: 人格 ID 过滤（None = 全人格）。
        model_entry: 模型条目名过滤（None = 全条目）。
        purpose: 调用用途过滤（None = 全用途）。
        status: 调用结果过滤（None = 全状态）。
        start_ms: 范围起点（epoch 毫秒，含）。
        end_ms: 范围终点（epoch 毫秒，不含）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200）。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
    """
    page = await usage_service.query(
        UsageQueryDTO(
            persona_id=persona_id,
            model_entry=model_entry,
            purpose=purpose,
            status=status,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor=cursor,
            limit=limit,
        )
    )
    return ResultDTO.ok(data=page)


@router.get("/aggregates", response_model=ResultDTO[list[UsageAggregateDTO]])
async def aggregate_usage(
    group_by: UsageGroupBy = Query(description="分组维度（model_entry / purpose / time_bucket）"),
    time_bucket: UsageTimeBucket | None = Query(
        None, description="时间桶宽（group_by=time_bucket 时必填；其余维度忽略）"
    ),
    persona_id: str | None = Query(None, description="人格 ID 过滤（None = 全人格）"),
    model_entry: str | None = Query(None, description="模型条目名过滤"),
    purpose: UsagePurpose | None = Query(None, description="调用用途过滤"),
    status: UsageStatus | None = Query(None, description="调用结果过滤"),
    start_ms: int | None = Query(None, ge=0, description="范围起点（epoch 毫秒，含）"),
    end_ms: int | None = Query(None, ge=0, description="范围终点（epoch 毫秒，不含）"),
) -> ResultDTO[list[UsageAggregateDTO]]:
    """用量多维聚合（分组行按总 token 降序——成本视角）。

    聚合口径与服务层一致（见 harness.usage.service）：input / output /
    total 求和、调用计数、平均时延（取整）、失败计数；时间维度携带
    ``bucket_ms``（桶起点 epoch 毫秒）。

    Args:
        group_by: 分组维度（model_entry / purpose / time_bucket）。
        time_bucket: 时间桶宽（minute / hour / day；time_bucket 维度
            必填）。
        persona_id: 人格 ID 过滤（None = 全人格）。
        model_entry: 模型条目名过滤。
        purpose: 调用用途过滤。
        status: 调用结果过滤。
        start_ms: 范围起点（epoch 毫秒，含）。
        end_ms: 范围终点（epoch 毫秒，不含）。

    Returns:
        聚合结果行列表（空结果为空列表）。

    Raises:
        BusinessException: ``group_by=time_bucket`` 未声明桶宽
            （``VALIDATION_ERROR`` → 422）。
    """
    rows = await usage_service.aggregate(
        UsageAggregateQueryDTO(
            group_by=group_by,
            time_bucket=time_bucket,
            persona_id=persona_id,
            model_entry=model_entry,
            purpose=purpose,
            status=status,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    )
    return ResultDTO.ok(data=rows)


@router.get("/summary", response_model=ResultDTO[UsageSummaryDTO])
async def usage_summary(
    persona_id: str | None = Query(None, description="人格 ID 过滤（None = 全人格）"),
    model_entry: str | None = Query(None, description="模型条目名过滤"),
    purpose: UsagePurpose | None = Query(None, description="调用用途过滤"),
    status: UsageStatus | None = Query(None, description="调用结果过滤"),
    start_ms: int | None = Query(None, ge=0, description="范围起点（epoch 毫秒，含）"),
    end_ms: int | None = Query(None, ge=0, description="范围终点（epoch 毫秒，不含）"),
) -> ResultDTO[UsageSummaryDTO]:
    """用量总览（单 payload：总量 + 均时延 + per-purpose 明细）。

    经 ``group_by=purpose`` 聚合派生（口径见模块 docstring）：总量 /
    计数 / 失败数精确求和；整体平均时延按调用次数加权。

    Args:
        persona_id: 人格 ID 过滤（None = 全人格）。
        model_entry: 模型条目名过滤。
        purpose: 调用用途过滤。
        status: 调用结果过滤。
        start_ms: 范围起点（epoch 毫秒，含）。
        end_ms: 范围终点（epoch 毫秒，不含）。

    Returns:
        总览 DTO（空范围返回全零 + 空明细）。
    """
    purpose_rows = await usage_service.aggregate(
        UsageAggregateQueryDTO(
            group_by=UsageGroupBy.PURPOSE,
            persona_id=persona_id,
            model_entry=model_entry,
            purpose=purpose,
            status=status,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    )
    return ResultDTO.ok(data=_summarize(purpose_rows))
