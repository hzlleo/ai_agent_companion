"""app/api/usage 路由测试——记录分页 / 聚合 / 总览（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（router + 共享异常处理器注册——无全局
app 导入、无真实服务器）；UsageService 经 session_scope 消费共享会话级
SQLite 引擎（engine holder 槽位外部安装——路由无 DB 依赖注入，见路由
模块 docstring Session 语义一节）。铺底数据经 UsageService.create_batch
写入（与生产写入链同构）。

时间戳设计（桶对齐确定性）：基准 ``BASE_TS = 864_000_000``
（86_400_000 × 10——day 桶起点对齐）；A / B / C 相对基准分别偏移
0 / 1 分钟 / 1 小时（minute 桶互异、hour 桶 A+B 同桶）；D 为基准
+2 天。聚合期望值全部手算锁定（不复制实现公式）。

覆盖面：HTTP 面（分页接续 / 全过滤面 / 三维聚合正确性 / 总览派生 /
错误映射 422·400）+ 处理器直调契约面（ResultDTO 包装形态）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api.usage import (
    aggregate_usage,
    list_usage_records,
    usage_summary,
)
from app.api.usage import (
    router as usage_router,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.usage.dtos import UsageCreateDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus
from packages.harness.usage.service import UsageService

#: day 桶宽（86_400_000）× 10——day / hour / minute 全档桶起点对齐。
BASE_TS = 864_000_000
#: 铺底行时间戳（A 基准；B +1 分钟；C +1 小时；D +2 天）。
_TS_A = BASE_TS
_TS_B = BASE_TS + 60_000
_TS_C = BASE_TS + 3_600_000
_TS_D = BASE_TS + 2 * 86_400_000


@pytest.fixture
async def usage_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        已注册路由与异常处理器的 :class:`FastAPI` 实例（用后复位 holder）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None  # 外部安装——生命周期归共享 fixture
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(usage_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(usage_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        usage_app: 本地装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=usage_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def seeded_rows(usage_app: FastAPI) -> AsyncIterator[list[int]]:
    """铺底四行用量记录（经服务写入链——与生产同构）。

    行集（期望值手算真源）：

        - A：main-chat / chat / p-1 / 10+20 / 100ms / success
        - B：main-chat / chat / p-1 / 5+5 / 200ms / success
        - C：dream-writer / dream / p-2 / 1+2 / 300ms / error
        - D：local-embed / embed / None / 0+0 / 60ms / success

    Args:
        usage_app: 已装配 app（fixture 依赖保证引擎槽位已安装）。

    Yields:
        落库行主键列表（与 A-D 同序）。
    """
    service = UsageService()
    records = await service.create_batch(
        [
            UsageCreateDTO(
                ts_ms=_TS_A,
                persona_id="p-1",
                model_entry="main-chat",
                purpose=UsagePurpose.CHAT,
                input_tokens=10,
                output_tokens=20,
                latency_ms=100,
                status=UsageStatus.SUCCESS,
                request_id="req-a",
            ),
            UsageCreateDTO(
                ts_ms=_TS_B,
                persona_id="p-1",
                model_entry="main-chat",
                purpose=UsagePurpose.CHAT,
                input_tokens=5,
                output_tokens=5,
                latency_ms=200,
                status=UsageStatus.SUCCESS,
                request_id="req-b",
            ),
            UsageCreateDTO(
                ts_ms=_TS_C,
                persona_id="p-2",
                model_entry="dream-writer",
                purpose=UsagePurpose.DREAM,
                input_tokens=1,
                output_tokens=2,
                latency_ms=300,
                status=UsageStatus.ERROR,
                error_code="LLM_CALL_FAILED",
            ),
            UsageCreateDTO(
                ts_ms=_TS_D,
                persona_id=None,
                model_entry="local-embed",
                purpose=UsagePurpose.EMBED,
                input_tokens=0,
                output_tokens=0,
                latency_ms=60,
                status=UsageStatus.SUCCESS,
            ),
        ]
    )
    yield [record.id for record in records]


# ---------------------------------------------------------------------------
# 记录分页（HTTP 面）
# ---------------------------------------------------------------------------


async def test_records_pagination_desc_and_cursor_continuation(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """分页：id 降序（D→C→B→A）+ 游标接续 + total 恒 -1。"""
    response = await client.get("/api/v1/usage/records", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "200"
    data = body["data"]
    assert data["total"] == -1
    assert data["page_size"] == 2
    assert [item["id"] for item in data["items"]] == [seeded_rows[3], seeded_rows[2]]
    assert data["next_cursor"] is not None

    second = await client.get(
        "/api/v1/usage/records", params={"limit": 2, "cursor": data["next_cursor"]}
    )
    assert second.status_code == 200
    data2 = second.json()["data"]
    assert [item["id"] for item in data2["items"]] == [seeded_rows[1], seeded_rows[0]]
    assert data2["next_cursor"] is None
    assert data2["has_more"] is False


async def test_records_filters_full_surface(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """过滤面：persona_id / model_entry / purpose / status / 时间范围（左闭右开）。"""
    by_persona = await client.get("/api/v1/usage/records", params={"persona_id": "p-1"})
    assert [i["model_entry"] for i in by_persona.json()["data"]["items"]] == [
        "main-chat",
        "main-chat",
    ]

    by_entry = await client.get(
        "/api/v1/usage/records", params={"model_entry": "dream-writer"}
    )
    assert [i["id"] for i in by_entry.json()["data"]["items"]] == [seeded_rows[2]]

    by_purpose = await client.get("/api/v1/usage/records", params={"purpose": "embed"})
    assert [i["id"] for i in by_purpose.json()["data"]["items"]] == [seeded_rows[3]]

    by_status = await client.get("/api/v1/usage/records", params={"status": "error"})
    items = by_status.json()["data"]["items"]
    assert [i["id"] for i in items] == [seeded_rows[2]]
    assert items[0]["error_code"] == "LLM_CALL_FAILED"

    # 左闭右开：[A, C) 含 A/B、不含 C；起点含 A 本身。
    by_range = await client.get(
        "/api/v1/usage/records",
        params={"start_ms": _TS_A, "end_ms": _TS_C},
    )
    assert sorted(i["ts_ms"] for i in by_range.json()["data"]["items"]) == [_TS_A, _TS_B]

    combined = await client.get(
        "/api/v1/usage/records",
        params={"persona_id": "p-2", "status": "success"},
    )
    assert combined.json()["data"]["items"] == []


async def test_records_invalid_query_params_400(client: AsyncClient) -> None:
    """非法查询参数：limit=0 / 未知 purpose / 负 start_ms → 400 VALIDATION_ERROR。"""
    for params in (
        {"limit": 0},
        {"purpose": "codex"},
        {"start_ms": -1},
    ):
        response = await client.get("/api/v1/usage/records", params=params)
        assert response.status_code == 400, params
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 聚合（HTTP 面——期望值手算锁定）
# ---------------------------------------------------------------------------


async def test_aggregates_by_model_entry(client: AsyncClient, seeded_rows: list[int]) -> None:
    """model_entry 维度：求和 / 计数 / 均时延 / 失败计数 / 总量降序。"""
    response = await client.get("/api/v1/usage/aggregates", params={"group_by": "model_entry"})
    assert response.status_code == 200
    rows = response.json()["data"]
    # 总量降序：main-chat 40 > dream-writer 3 > local-embed 0
    assert [row["group"] for row in rows] == ["main-chat", "dream-writer", "local-embed"]
    assert rows[0] == {
        "group": "main-chat",
        "bucket_ms": None,
        "input_tokens": 15,
        "output_tokens": 25,
        "total_tokens": 40,
        "call_count": 2,
        "avg_latency_ms": 150,
        "error_count": 0,
    }
    assert rows[1]["call_count"] == 1
    assert rows[1]["avg_latency_ms"] == 300
    assert rows[1]["error_count"] == 1
    assert rows[2]["total_tokens"] == 0
    assert rows[2]["call_count"] == 1


async def test_aggregates_by_purpose_with_filters(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """purpose 维度 + 过滤面收敛（persona_id=p-1 → 仅 chat 行）。"""
    response = await client.get(
        "/api/v1/usage/aggregates",
        params={"group_by": "purpose", "persona_id": "p-1"},
    )
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["group"] == "chat"
    assert rows[0]["total_tokens"] == 40
    assert rows[0]["call_count"] == 2
    assert rows[0]["avg_latency_ms"] == 150


async def test_aggregates_time_bucket_minute_hour_day(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """time_bucket 三档桶宽：minute 互异桶 / hour A+B 同桶 / day 跨 2 天。"""
    minute = await client.get(
        "/api/v1/usage/aggregates",
        params={"group_by": "time_bucket", "time_bucket": "minute"},
    )
    assert minute.status_code == 200
    rows = minute.json()["data"]
    # minute：四行四个桶（A/B 相差 1 分钟、C +1 小时、D +2 天）。
    assert len(rows) == 4
    by_group = {row["group"]: row for row in rows}
    # 桶起点 = ts 对齐 minute 桶宽（A 基准本身对齐——桶起点即 ts）。
    assert set(by_group) == {str(_TS_A), str(_TS_B), str(_TS_C), str(_TS_D)}
    assert by_group[str(_TS_A)]["bucket_ms"] == _TS_A
    assert by_group[str(_TS_A)]["total_tokens"] == 30
    assert by_group[str(_TS_C)]["bucket_ms"] == _TS_C

    hour = await client.get(
        "/api/v1/usage/aggregates",
        params={"group_by": "time_bucket", "time_bucket": "hour"},
    )
    rows = hour.json()["data"]
    # hour：A/B 同桶（总量 40 合并）；C/D 各一桶。
    assert len(rows) == 3
    hour_by_group = {row["group"]: row for row in rows}
    assert str(_TS_A) in hour_by_group  # A 与 B 同 hour 桶（起点 = A）
    assert hour_by_group[str(_TS_A)]["call_count"] == 2
    assert hour_by_group[str(_TS_A)]["total_tokens"] == 40

    day = await client.get(
        "/api/v1/usage/aggregates",
        params={"group_by": "time_bucket", "time_bucket": "day"},
    )
    rows = day.json()["data"]
    # day：A/B/C 同桶（day 10，总量 43）；D 单独桶（day 12）。
    assert len(rows) == 2
    day_by_group = {row["group"]: row for row in rows}
    assert day_by_group[str(BASE_TS)]["call_count"] == 3
    assert day_by_group[str(BASE_TS)]["total_tokens"] == 43
    assert day_by_group[str(_TS_D)]["total_tokens"] == 0


async def test_aggregates_time_bucket_missing_width_422(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """time_bucket 维度缺桶宽：服务层 VALIDATION_ERROR → 422。"""
    response = await client.get("/api/v1/usage/aggregates", params={"group_by": "time_bucket"})
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    assert body["data"]["reason"] == "time_bucket 分组维度必须声明桶宽"


async def test_aggregates_invalid_params_400(client: AsyncClient) -> None:
    """非法聚合参数：未知 group_by / 未知 time_bucket → 400。"""
    for params in (
        {"group_by": "persona"},
        {"group_by": "time_bucket", "time_bucket": "week"},
    ):
        response = await client.get("/api/v1/usage/aggregates", params=params)
        assert response.status_code == 400, params
        assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 总览（HTTP 面——派生口径正确性）
# ---------------------------------------------------------------------------


async def test_summary_totals_and_purpose_breakdown(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """总览：全量口径（计数 / 三项 token 求和 / 加权均时延 / 失败数 / 明细）。"""
    response = await client.get("/api/v1/usage/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["call_count"] == 4
    assert data["input_tokens"] == 16
    assert data["output_tokens"] == 27
    assert data["total_tokens"] == 43
    # 加权均时延：(150×2 + 300×1 + 60×1) / 4 = 165。
    assert data["avg_latency_ms"] == 165
    assert data["error_count"] == 1
    # per-purpose 明细（总量降序）：chat 40 > dream 3 > embed 0。
    assert [row["group"] for row in data["purposes"]] == ["chat", "dream", "embed"]
    assert data["purposes"][0]["call_count"] == 2


async def test_summary_with_persona_filter(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """总览 + persona 过滤：收敛到 p-1（chat 单用途）。"""
    response = await client.get("/api/v1/usage/summary", params={"persona_id": "p-1"})
    data = response.json()["data"]
    assert data["call_count"] == 2
    assert data["total_tokens"] == 40
    assert data["avg_latency_ms"] == 150
    assert data["error_count"] == 0
    assert [row["group"] for row in data["purposes"]] == ["chat"]


async def test_summary_empty_range_returns_zeros(
    client: AsyncClient, seeded_rows: list[int]
) -> None:
    """空范围总览：全零 + 空明细（除零守卫）。"""
    response = await client.get(
        "/api/v1/usage/summary",
        params={"start_ms": _TS_D + 86_400_000, "end_ms": _TS_D + 2 * 86_400_000},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["call_count"] == 0
    assert data["total_tokens"] == 0
    assert data["avg_latency_ms"] == 0
    assert data["purposes"] == []


# ---------------------------------------------------------------------------
# 处理器直调契约面——handler 函数作为普通 async 可调用直接调用，逐端点
# 锁定 ResultDTO 包装契约（success / code / message / data 形态）。
# ---------------------------------------------------------------------------


async def test_direct_handlers_result_contract(seeded_rows: list[int]) -> None:
    """直调面：records / aggregates / summary 的 ResultDTO 契约。"""
    listed = await list_usage_records(
        persona_id="p-1",
        model_entry=None,
        purpose=None,
        status=None,
        start_ms=None,
        end_ms=None,
        cursor=None,
        limit=20,
    )
    assert listed.success is True
    assert listed.code == "200"
    assert listed.data is not None
    assert len(listed.data.items) == 2

    aggregated = await aggregate_usage(
        group_by="model_entry",
        time_bucket=None,
        persona_id=None,
        model_entry=None,
        purpose=None,
        status=None,
        start_ms=None,
        end_ms=None,
    )
    assert aggregated.success is True
    assert aggregated.data is not None
    assert [row.group for row in aggregated.data] == [
        "main-chat",
        "dream-writer",
        "local-embed",
    ]

    summarized = await usage_summary(
        persona_id=None,
        model_entry=None,
        purpose=None,
        status=None,
        start_ms=None,
        end_ms=None,
    )
    assert summarized.success is True
    assert summarized.data is not None
    assert summarized.data.call_count == 4
    assert summarized.data.total_tokens == 43
    assert summarized.data.avg_latency_ms == 165
