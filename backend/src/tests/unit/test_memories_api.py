"""app/api/memories 路由测试——全操作面（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（memories router + 共享异常处理器——无
全局 app 导入、无真实服务器）；DB 经共享会话级 SQLite fixture 外部安装
到 engine holder。覆盖面：episodes（注入 / 列表过滤 / 读取 / 删除 /
批量部分成功）/ curated（upsert supersede / history / untrusted 拒绝）/
sessions（列表 / 详情 / 删除）/ 人格解析（缺省激活人格 / 显式
persona_id / 无激活 404）/ 跨人格隔离 404；另设处理器直调契约面
（handler 函数作为普通 async 可调用直接调用——逐端点锁定 ResultDTO
包装契约）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.memories import (
    batch_delete_episodes,
    create_episode,
    delete_curated,
    delete_episode,
    delete_session,
    get_curated_history,
    get_episode,
    get_session,
    list_curated,
    list_episodes,
    list_sessions,
    upsert_curated,
)
from app.api.memories import router as memories_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.core.errors import BusinessException
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import (
    CuratedUpsertDTO,
    EpisodeBatchDeleteRequest,
    EpisodeCreateDTO,
)
from packages.harness.memory.models import CuratedStatus
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context


async def _create_persona(
    db_session: AsyncSession, name: str, *, active: bool = False
) -> int:
    """经真实 PersonaService 创建人格（可选激活）。"""
    service = PersonaService()
    created = await service.create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": name,
                "status": "active" if active else "disabled",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )
    return created.id


@pytest.fixture
async def memories_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。

    Yields:
        已注册路由与异常处理器的 :class:`FastAPI` 实例（用后复位 holder）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(memories_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(memories_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=memories_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def active_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个激活人格（经 personas API 语义面；返回主键）。"""
    persona_id = await _create_persona(db_session, "mem-active", active=True)
    await PersonaService().activate(db_session, persona_id)
    yield persona_id


def _episode_payload(**overrides: Any) -> dict[str, Any]:
    """构造最小事件注入请求体（chat 回合载荷）。"""
    payload: dict[str, Any] = {
        "kind": "chat",
        "source": "chat",
        "content": {"user_content": "今天吃了小鱼干", "assistant_content": "好棒"},
        "importance": 6,
    }
    payload.update(overrides)
    return payload


def _curated_payload(**overrides: Any) -> dict[str, Any]:
    """构造最小策展 upsert 请求体。"""
    payload: dict[str, Any] = {
        "kind": "memory",
        "topic": "口味偏好",
        "content": "主人喜欢小鱼干",
    }
    payload.update(overrides)
    return payload


async def _create_episode(
    client: AsyncClient, **overrides: Any
) -> dict[str, Any]:
    """经 API 注入事件并返回 data 段（铺底辅助）。"""
    response = await client.post("/api/v1/memories/episodes", json=_episode_payload(**overrides))
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


# ---------------------------------------------------------------------------
# 情景事件端点
# ---------------------------------------------------------------------------


async def test_episode_inject_and_get(
    client: AsyncClient, active_persona_id: int
) -> None:
    """注入（provenance 缺省 owner）→ 读取：ResultDTO 形态 + 载荷判别回读。"""
    created = await _create_episode(client)
    assert created["persona_id"] == str(active_persona_id)
    assert created["provenance"] == "owner"
    assert created["kind"] == "chat"
    assert created["content"]["user_content"] == "今天吃了小鱼干"

    response = await client.get(f"/api/v1/memories/episodes/{created['id']}")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == created["id"]


async def test_episode_list_filters_and_cursor(
    client: AsyncClient, active_persona_id: int
) -> None:
    """列表过滤面：kind / provenance / 时间范围 + 游标接续。"""
    await _create_episode(client, ts_ms=100)
    await _create_episode(
        client, kind="duty", source="duty:daily",
        content={"title": "巡检", "summary": "正常"}, ts_ms=200,
    )
    await _create_episode(client, provenance="untrusted", ts_ms=300)

    response = await client.get(
        "/api/v1/memories/episodes",
        params={"kind": "chat", "provenance": "owner"},
    )
    data = response.json()["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["content"]["user_content"] == "今天吃了小鱼干"

    window = await client.get(
        "/api/v1/memories/episodes", params={"start_ms": 100, "end_ms": 300}
    )
    assert len(window.json()["data"]["items"]) == 2

    first = await client.get("/api/v1/memories/episodes", params={"limit": 2})
    first_data = first.json()["data"]
    assert first_data["has_more"] is True
    second = await client.get(
        "/api/v1/memories/episodes",
        params={"limit": 2, "cursor": first_data["next_cursor"]},
    )
    second_data = second.json()["data"]
    assert len(second_data["items"]) == 1
    assert second_data["has_more"] is False


async def test_episode_delete_and_batch_partial(
    client: AsyncClient, active_persona_id: int
) -> None:
    """删除 + 批量部分成功（缺失项 NOT_FAILED 形态）。"""
    keep = await _create_episode(client)
    drop = await _create_episode(client)

    response = await client.delete(f"/api/v1/memories/episodes/{drop['id']}")
    assert response.status_code == 200
    assert response.json()["message"] == "情景事件已删除"

    missing = await client.get(f"/api/v1/memories/episodes/{drop['id']}")
    assert missing.status_code == 404

    batch = await client.post(
        "/api/v1/memories/episodes/batch-delete",
        json={"ids": [999, keep["id"]]},
    )
    assert batch.status_code == 200
    data = batch.json()["data"]
    assert data["total"] == 2
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    by_id = {item["id"]: item for item in data["items"]}
    assert by_id[999]["code"] == "NOT_FOUND"
    assert by_id[keep["id"]]["success"] is True


async def test_episode_bad_payload_400(client: AsyncClient, active_persona_id: int) -> None:
    """载荷与 kind 不匹配 / 非法 kind：请求校验失败 400（VALIDATION_ERROR）。"""
    response = await client.post(
        "/api/v1/memories/episodes",
        json={"kind": "bogus", "source": "chat", "content": {"text": "x"}},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    mismatch = await client.post(
        "/api/v1/memories/episodes",
        json={"kind": "chat", "source": "chat", "content": "不是对象"},
    )
    assert mismatch.status_code == 400


# ---------------------------------------------------------------------------
# 策展端点
# ---------------------------------------------------------------------------


async def test_curated_upsert_supersede_and_history(
    client: AsyncClient, active_persona_id: int
) -> None:
    """upsert supersede：二次 upsert 单 active + 历史链端点（版本升序）。"""
    first = await client.post(
        "/api/v1/memories/curated/upsert", json=_curated_payload()
    )
    assert first.status_code == 200
    first_data: dict[str, Any] = first.json()["data"]
    assert first_data["version"] == 1
    assert first_data["status"] == "active"

    second = await client.post(
        "/api/v1/memories/curated/upsert",
        json=_curated_payload(content="主人现在更爱虾干"),
    )
    second_data: dict[str, Any] = second.json()["data"]
    assert second_data["version"] == 2
    assert second_data["status"] == "active"

    active_list = await client.get(
        "/api/v1/memories/curated", params={"status": "active", "topic": "口味偏好"}
    )
    items = active_list.json()["data"]["items"]
    assert [item["id"] for item in items] == [second_data["id"]]

    history = await client.get(
        f"/api/v1/memories/curated/{first_data['id']}/history"
    )
    assert history.status_code == 200
    chain = history.json()["data"]
    assert [row["version"] for row in chain] == [1, 2]
    assert chain[0]["status"] == "superseded"
    assert chain[0]["superseded_by_id"] == second_data["id"]

    deleted = await client.delete(f"/api/v1/memories/curated/{first_data['id']}")
    assert deleted.status_code == 200
    missing = await client.get(f"/api/v1/memories/curated/{first_data['id']}/history")
    assert missing.status_code == 404


async def test_curated_untrusted_rejected(
    client: AsyncClient, active_persona_id: int
) -> None:
    """untrusted upsert：VALIDATION_ERROR（策展层准入不变式）。"""
    response = await client.post(
        "/api/v1/memories/curated/upsert",
        json=_curated_payload(provenance="untrusted"),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 会话端点
# ---------------------------------------------------------------------------


async def test_sessions_endpoints(
    client: AsyncClient, active_persona_id: int, db_session: AsyncSession
) -> None:
    """会话列表 / 详情 / 删除（消息同事务清理）。"""
    session_service = ChatSessionService()
    async with bind_persona_context(active_persona_id):
        summary = await session_service.create_session(db_session, title="API 会话")
        await session_service.append_message(
            db_session,
            summary.id,
            role=ChatMessageRole.USER,
            content="在吗",
        )

    listed = await client.get("/api/v1/memories/sessions")
    items = listed.json()["data"]["items"]
    assert [item["id"] for item in items] == [summary.id]
    assert items[0]["title"] == "API 会话"

    detail = await client.get(f"/api/v1/memories/sessions/{summary.id}")
    detail_data = detail.json()["data"]
    assert detail_data["message_count"] == 1
    assert detail_data["messages"][0]["content"] == "在吗"
    assert detail_data["messages"][0]["seq"] == 1

    deleted = await client.delete(f"/api/v1/memories/sessions/{summary.id}")
    assert deleted.status_code == 200
    missing = await client.get(f"/api/v1/memories/sessions/{summary.id}")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 人格解析 / 跨人格隔离
# ---------------------------------------------------------------------------


async def test_persona_resolution_and_isolation(
    client: AsyncClient, active_persona_id: int, db_session: AsyncSession
) -> None:
    """缺省激活人格 + 显式 persona_id + 跨人格隔离（404）+ 无激活 404。"""
    other_id = await _create_persona(db_session, "mem-other")

    # 缺省：激活人格作用域
    mine = await _create_episode(client)
    assert mine["persona_id"] == str(active_persona_id)

    # 显式 persona_id：他人格作用域注入
    theirs = await client.post(
        "/api/v1/memories/episodes",
        json=_episode_payload(),
        params={"persona_id": other_id},
    )
    theirs_data = theirs.json()["data"]
    assert theirs_data["persona_id"] == str(other_id)

    # 跨人格隔离：他人格资源对本作用域不可见（404）
    cross_get = await client.get(f"/api/v1/memories/episodes/{theirs_data['id']}")
    assert cross_get.status_code == 404
    cross_delete = await client.delete(f"/api/v1/memories/episodes/{theirs_data['id']}")
    assert cross_delete.status_code == 404

    # 指定人格不存在
    bogus = await client.get(
        "/api/v1/memories/episodes", params={"persona_id": 999}
    )
    assert bogus.status_code == 404

    # 无激活人格 + 未显式指定：404 fail loud
    await PersonaService().deactivate(db_session, active_persona_id)
    no_active = await client.get("/api/v1/memories/episodes")
    assert no_active.status_code == 404
    assert no_active.json()["code"] == "NOT_FOUND"


async def test_invalid_query_params_400(client: AsyncClient, active_persona_id: int) -> None:
    """非法查询参数（limit 越界 / kind 非法值）→ 400。"""
    response = await client.get("/api/v1/memories/episodes", params={"limit": 0})
    assert response.status_code == 400
    response = await client.get("/api/v1/memories/episodes", params={"kind": "bogus"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 处理器直调契约面——handler 函数作为普通 async 可调用直接调用，逐端点
# 锁定 ResultDTO 包装契约（success / code / message / data 形态）。
# ---------------------------------------------------------------------------


async def test_direct_handlers_contract(
    db_session: AsyncSession, active_persona_id: int
) -> None:
    """直调面：episodes / curated / sessions 全端点 ResultDTO 契约。"""
    created = await create_episode(
        EpisodeCreateDTO.model_validate(_episode_payload()), None, db_session
    )
    assert created.success is True
    assert created.message == "情景事件已记录"
    assert created.data is not None
    assert created.data.persona_id == str(active_persona_id)

    listed = await list_episodes(
        kind=None, provenance=None, start_ms=None, end_ms=None,
        cursor=None, limit=20, persona_id=None, db=db_session,
    )
    assert listed.success is True
    assert listed.data is not None
    assert len(listed.data.items) == 1

    fetched = await get_episode(created.data.id, None, db_session)
    assert fetched.success is True
    assert fetched.data is not None

    upserted = await upsert_curated(
        CuratedUpsertDTO.model_validate(_curated_payload()), None, db_session
    )
    assert upserted.success is True
    assert upserted.message == "策展记忆已保存"
    assert upserted.data is not None
    assert upserted.data.version == 1

    curated_list = await list_curated(
        kind=None, topic=None, status=CuratedStatus.ACTIVE,
        cursor=None, limit=20, persona_id=None, db=db_session,
    )
    assert curated_list.success is True
    assert curated_list.data is not None
    assert len(curated_list.data.items) == 1

    history = await get_curated_history(upserted.data.id, None, db_session)
    assert history.success is True
    assert history.data is not None
    assert len(history.data) == 1

    session_service = ChatSessionService()
    async with bind_persona_context(active_persona_id):
        summary = await session_service.create_session(db_session, title="直调会话")
    sessions = await list_sessions(cursor=None, limit=20, persona_id=None, db=db_session)
    assert sessions.success is True
    assert sessions.data is not None
    assert [s.id for s in sessions.data.items] == [summary.id]

    detail = await get_session(summary.id, None, db_session)
    assert detail.success is True
    assert detail.data is not None
    assert detail.data.title == "直调会话"

    batch = await batch_delete_episodes(
        EpisodeBatchDeleteRequest(ids=[created.data.id, 999]), None, db_session
    )
    assert batch.success is True
    assert batch.data is not None
    assert batch.data.succeeded == 1

    deleted_session = await delete_session(summary.id, None, db_session)
    assert deleted_session.success is True
    assert deleted_session.message == "会话已删除"

    deleted_curated = await delete_curated(upserted.data.id, None, db_session)
    assert deleted_curated.success is True
    assert deleted_curated.message == "策展记忆已删除"

    # 直调失败路径：BusinessException 原样上抛（HTTP 面由异常处理器接面）
    with pytest.raises(BusinessException) as exc_info:
        await delete_episode(999, None, db_session)
    assert exc_info.value.code == "NOT_FOUND"
