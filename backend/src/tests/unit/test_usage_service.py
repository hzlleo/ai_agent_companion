"""harness/usage service 测试——创建 / 批量 / 游标分页 / 多维聚合。

引擎经共享会话级 SQLite fixture（StaticPool 单连接）；服务经
``session_scope`` 消费——engine holder 槽位外部安装（owner_token=None，
生命周期归共享 fixture，见 database.engine 治理约定）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from packages.harness.core.dtos import PageResponse
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.usage.dtos import (
    UsageAggregateQueryDTO,
    UsageCreateDTO,
    UsageQueryDTO,
    UsageRecordDTO,
)
from packages.harness.usage.models import (
    UsageGroupBy,
    UsagePurpose,
    UsageStatus,
    UsageTimeBucket,
)
from packages.harness.usage.orm import UsageRecordRow
from packages.harness.usage.service import UsageService


@pytest.fixture
async def usage_service(shared_db_engine: AsyncEngine) -> AsyncIterator[UsageService]:
    """用量服务（engine holder 外部安装到共享引擎；用后复位槽位）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        绑定共享引擎的 :class:`UsageService` 实例。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None  # 外部安装——close 不 dispose 共享引擎
    yield UsageService()
    db_engine_holder.reset()


def _dto(
    *,
    ts_ms: int = 1_000_000,
    model_entry: str = "main-chat",
    purpose: UsagePurpose = UsagePurpose.CHAT,
    persona_id: str | None = "p-1",
    input_tokens: int = 10,
    output_tokens: int = 20,
    latency_ms: int = 100,
    status: UsageStatus = UsageStatus.SUCCESS,
    error_code: str | None = None,
) -> UsageCreateDTO:
    """构造创建 DTO（total 缺省——归一链路一并验证）。"""
    return UsageCreateDTO(
        ts_ms=ts_ms,
        persona_id=persona_id,
        model_entry=model_entry,
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
        request_id="req-fixture" if status == UsageStatus.SUCCESS else None,
    )


# ---------------------------------------------------------------------------
# 创建（单条 / 批量）
# ---------------------------------------------------------------------------


async def test_create_returns_record_with_id(usage_service: UsageService) -> None:
    """单条创建：主键分配 + 字段回读 + total 归一。"""
    record = await usage_service.create(_dto())
    assert record.id >= 1
    assert record.persona_id == "p-1"
    assert record.purpose == UsagePurpose.CHAT
    assert record.status == UsageStatus.SUCCESS
    assert record.total_tokens == 30
    assert record.provider == "openai_compatible"
    assert record.created_at > 0


async def test_create_system_call_without_persona(usage_service: UsageService) -> None:
    """系统调用（persona_id=None）：可空契约 + 跨人格查询可见。"""
    record = await usage_service.create(_dto(persona_id=None, purpose=UsagePurpose.DUTY))
    assert record.persona_id is None
    page = await usage_service.query(UsageQueryDTO(purpose=UsagePurpose.DUTY))
    assert [item.persona_id for item in page.items] == [None]


async def test_create_batch_preserves_order(usage_service: UsageService) -> None:
    """批量创建：同序返回 + 主键递增 + 空批量为空列表。"""
    dtos = [
        _dto(ts_ms=1_000, model_entry="main-chat"),
        _dto(ts_ms=2_000, model_entry="dream-writer", purpose=UsagePurpose.DREAM),
        _dto(ts_ms=3_000, model_entry="local-embed", purpose=UsagePurpose.EMBED),
    ]
    records = await usage_service.create_batch(dtos)
    assert [r.model_entry for r in records] == ["main-chat", "dream-writer", "local-embed"]
    assert records[0].id < records[1].id < records[2].id

    assert await usage_service.create_batch([]) == []


async def test_create_error_record(usage_service: UsageService) -> None:
    """失败记录：error_code 落库 + status 过滤可命中。"""
    await usage_service.create(
        _dto(
            status=UsageStatus.ERROR,
            error_code="LLM_CALL_FAILED",
            input_tokens=0,
            output_tokens=0,
            latency_ms=37,
        )
    )
    page = await usage_service.query(UsageQueryDTO(status=UsageStatus.ERROR))
    assert len(page.items) == 1
    assert page.items[0].error_code == "LLM_CALL_FAILED"
    assert page.items[0].total_tokens == 0


# ---------------------------------------------------------------------------
# 游标分页查询
# ---------------------------------------------------------------------------


async def test_query_pagination_newest_first(usage_service: UsageService) -> None:
    """分页遍历：id 降序（新记录在前）+ 游标接续 + 末页无游标。"""
    for index in range(5):
        await usage_service.create(_dto(ts_ms=1_000 + index))

    page1 = await usage_service.query(UsageQueryDTO(limit=2))
    assert isinstance(page1, PageResponse)
    assert len(page1.items) == 2
    assert page1.has_more is True
    assert page1.next_cursor is not None
    ids_page1 = [item.id for item in page1.items]
    assert ids_page1[0] > ids_page1[1]

    page2 = await usage_service.query(UsageQueryDTO(limit=2, cursor=page1.next_cursor))
    ids_page2 = [item.id for item in page2.items]
    assert ids_page1[1] > ids_page2[0]
    assert len(page2.items) == 2

    page3 = await usage_service.query(UsageQueryDTO(limit=2, cursor=page2.next_cursor))
    assert len(page3.items) == 1
    assert page3.has_more is False
    assert page3.next_cursor is None


async def test_query_malformed_cursor_treated_as_first_page(
    usage_service: UsageService,
) -> None:
    """非法游标：按无游标处理（首页语义——decode 契约）。"""
    await usage_service.create(_dto())
    page = await usage_service.query(UsageQueryDTO(cursor="!!!not-base64!!!", limit=10))
    assert len(page.items) == 1


async def test_query_filters(usage_service: UsageService) -> None:
    """过滤面：时间左闭右开 + persona / model_entry / purpose 各轴。"""
    await usage_service.create(
        _dto(ts_ms=100, persona_id="p-a", model_entry="main-chat", purpose=UsagePurpose.CHAT)
    )
    await usage_service.create(
        _dto(ts_ms=200, persona_id="p-b", model_entry="dream-writer", purpose=UsagePurpose.DREAM)
    )
    await usage_service.create(
        _dto(ts_ms=299, persona_id="p-a", model_entry="main-chat", purpose=UsagePurpose.CHAT)
    )
    await usage_service.create(_dto(ts_ms=300, persona_id="p-a", model_entry="main-chat"))

    time_range = await usage_service.query(UsageQueryDTO(start_ms=100, end_ms=300))
    assert sorted(item.ts_ms for item in time_range.items) == [100, 200, 299]

    start_inclusive = await usage_service.query(UsageQueryDTO(start_ms=200))
    assert sorted(item.ts_ms for item in start_inclusive.items) == [200, 299, 300]

    by_persona = await usage_service.query(UsageQueryDTO(persona_id="p-b"))
    assert [item.ts_ms for item in by_persona.items] == [200]

    by_entry = await usage_service.query(UsageQueryDTO(model_entry="dream-writer"))
    assert [item.ts_ms for item in by_entry.items] == [200]

    by_purpose = await usage_service.query(UsageQueryDTO(purpose=UsagePurpose.DREAM))
    assert [item.ts_ms for item in by_purpose.items] == [200]


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


async def test_aggregate_by_model_entry(usage_service: UsageService) -> None:
    """按条目聚合：求和 / 计数 / 均时延 / 失败计数 / 总量降序。"""
    await usage_service.create(
        _dto(model_entry="main-chat", input_tokens=10, output_tokens=20, latency_ms=100)
    )
    await usage_service.create(
        _dto(model_entry="main-chat", input_tokens=1, output_tokens=2, latency_ms=200)
    )
    await usage_service.create(
        _dto(
            model_entry="main-chat",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            status=UsageStatus.ERROR,
            error_code="LLM_CALL_FAILED",
        )
    )
    await usage_service.create(
        _dto(
            model_entry="local-embed",
            input_tokens=100,
            output_tokens=0,
            latency_ms=50,
            purpose=UsagePurpose.EMBED,
        )
    )

    results = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.MODEL_ENTRY)
    )
    assert [row.group for row in results] == ["local-embed", "main-chat"]  # 总量降序
    by_entry = {row.group: row for row in results}

    main = by_entry["main-chat"]
    assert main.bucket_ms is None
    assert main.input_tokens == 11
    assert main.output_tokens == 22
    assert main.total_tokens == 33
    assert main.call_count == 3
    assert main.avg_latency_ms == 100  # (100+200+0)/3
    assert main.error_count == 1

    embed = by_entry["local-embed"]
    assert embed.call_count == 1
    assert embed.total_tokens == 100
    assert embed.error_count == 0


async def test_aggregate_by_purpose(usage_service: UsageService) -> None:
    """按用途聚合：分组键为 purpose 值。"""
    await usage_service.create(_dto(purpose=UsagePurpose.CHAT, input_tokens=5, output_tokens=5))
    await usage_service.create(_dto(purpose=UsagePurpose.DREAM, input_tokens=3, output_tokens=1))
    await usage_service.create(_dto(purpose=UsagePurpose.DREAM, input_tokens=2, output_tokens=2))

    results = await usage_service.aggregate(UsageAggregateQueryDTO(group_by=UsageGroupBy.PURPOSE))
    by_purpose = {row.group: row for row in results}
    assert by_purpose["chat"].call_count == 1
    assert by_purpose["chat"].total_tokens == 10
    assert by_purpose["dream"].call_count == 2
    assert by_purpose["dream"].total_tokens == 8


async def test_aggregate_by_time_bucket(usage_service: UsageService) -> None:
    """按时间桶聚合：桶起点对齐 + 跨记录归桶 + bucket_ms 数值面。"""
    minute = 60_000
    await usage_service.create(_dto(ts_ms=minute + 10, input_tokens=1, output_tokens=1))
    await usage_service.create(_dto(ts_ms=minute + 500, input_tokens=2, output_tokens=2))
    await usage_service.create(_dto(ts_ms=2 * minute - 1, input_tokens=4, output_tokens=4))
    await usage_service.create(_dto(ts_ms=2 * minute, input_tokens=8, output_tokens=8))

    results = await usage_service.aggregate(
        UsageAggregateQueryDTO(
            group_by=UsageGroupBy.TIME_BUCKET, time_bucket=UsageTimeBucket.MINUTE
        )
    )
    assert [row.group for row in results] == ["120000", "60000"]  # 总量降序
    buckets = {row.bucket_ms: row for row in results}
    assert set(buckets) == {60_000, 120_000}

    first = buckets[60_000]
    assert first.call_count == 3
    assert first.total_tokens == 2 + 4 + 8

    second = buckets[120_000]
    assert second.call_count == 1
    assert second.total_tokens == 16


async def test_aggregate_hour_and_day_buckets(usage_service: UsageService) -> None:
    """hour / day 桶宽：边界对齐（桶起点 = floor(ts / 桶宽) * 桶宽）。"""
    hour = 3_600_000
    day = 86_400_000
    await usage_service.create(_dto(ts_ms=hour + 1))
    await usage_service.create(_dto(ts_ms=2 * hour))
    await usage_service.create(_dto(ts_ms=day + hour))

    hour_results = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.TIME_BUCKET, time_bucket=UsageTimeBucket.HOUR)
    )
    # day + hour = 25 小时 → 独立小时桶 90_000_000（day 25 * hour）
    assert sorted(row.bucket_ms for row in hour_results) == [hour, 2 * hour, 25 * hour]

    day_results = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.TIME_BUCKET, time_bucket=UsageTimeBucket.DAY)
    )
    assert [row.bucket_ms for row in day_results] == [0, day]
    assert day_results[1].call_count == 1


async def test_aggregate_time_bucket_requires_bucket(usage_service: UsageService) -> None:
    """time_bucket 维度缺桶宽：VALIDATION_ERROR fail loud。"""
    with pytest.raises(BusinessException) as exc_info:
        await usage_service.aggregate(UsageAggregateQueryDTO(group_by=UsageGroupBy.TIME_BUCKET))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.details is not None
    assert exc_info.value.details["group_by"] == "time_bucket"


async def test_aggregate_respects_filters(usage_service: UsageService) -> None:
    """聚合过滤面：persona / 时间范围收敛分组行。"""
    await usage_service.create(
        _dto(ts_ms=100, persona_id="p-a", model_entry="main-chat", input_tokens=10, output_tokens=0)
    )
    await usage_service.create(
        _dto(ts_ms=200, persona_id="p-b", model_entry="main-chat", input_tokens=5, output_tokens=0)
    )
    await usage_service.create(
        _dto(ts_ms=300, persona_id="p-a", model_entry="main-chat", input_tokens=1, output_tokens=0)
    )

    by_persona = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.MODEL_ENTRY, persona_id="p-a")
    )
    assert by_persona[0].call_count == 2
    assert by_persona[0].total_tokens == 11

    in_range = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.MODEL_ENTRY, start_ms=150, end_ms=250)
    )
    assert in_range[0].call_count == 1
    assert in_range[0].total_tokens == 5


async def test_aggregate_empty_result(usage_service: UsageService) -> None:
    """空库聚合：空列表（零分组行）。"""
    results = await usage_service.aggregate(
        UsageAggregateQueryDTO(group_by=UsageGroupBy.MODEL_ENTRY)
    )
    assert results == []


# ---------------------------------------------------------------------------
# 行物理校验（共享库直查——落库形态而非仅 DTO 回读）
# ---------------------------------------------------------------------------


async def test_create_persists_row_in_shared_db(usage_service: UsageService) -> None:
    """落库物理校验：ff_usage_records 行存在且列形态正确（枚举字符串值）。"""
    await usage_service.create(_dto(error_code=None))
    factory = db_engine_holder.session_factory
    assert factory is not None
    async with factory() as session:
        rows = list((await session.execute(select(UsageRecordRow))).scalars().all())
    assert len(rows) == 1
    row = rows[0]
    assert row.purpose == "chat"
    assert row.status == "success"
    assert row.ts_ms == 1_000_000
    assert row.total_tokens == 30


async def test_record_dto_roundtrip_via_model_validate(
    usage_service: UsageService,
) -> None:
    """记录 DTO from_attributes 回读（ORM 行 → DTO 枚举 coercion）。"""
    await usage_service.create(_dto(purpose=UsagePurpose.LOOK_AT))
    page = await usage_service.query(UsageQueryDTO(purpose=UsagePurpose.LOOK_AT))
    record: UsageRecordDTO = page.items[0]
    assert record.purpose == UsagePurpose.LOOK_AT
    assert isinstance(record.id, int)
