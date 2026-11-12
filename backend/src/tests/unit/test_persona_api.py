"""app/api/personas 路由测试——全管理面端点（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（router + 共享异常处理器注册——无全局
app 导入、无真实服务器）；DB 经共享会话级 SQLite fixture 外部安装到
engine holder（get_async_db 依赖自然生效）。

覆盖面：happy path 全端点 + 错误映射（404 / 409 / 400 ResultDTO 形态）
+ 批量删除部分成功 + 游标分页接续；另设**处理器直调契约面**（handler
函数作为普通 async 可调用直接调用）——逐端点锁定 ResultDTO 包装契约
（success / code / message / data 形态）。直调面与 HTTP 面互补：HTTP
面验证路由装配与错误映射，直调面验证处理器函数体与响应包装契约。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.personas import (
    activate_persona,
    batch_delete_personas,
    copy_persona,
    create_persona,
    deactivate_persona,
    delete_persona,
    get_persona,
    get_persona_card,
    list_persona_states,
    list_personas,
    replace_persona_card,
    update_persona,
    upsert_persona_state,
)
from app.api.personas import (
    router as personas_router,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaBatchDeleteRequest,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.models import PersonaStateLayer


@pytest.fixture
async def persona_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
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
    app.include_router(personas_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(persona_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        persona_app: 本地装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=persona_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _create_payload(name: str = "feiyu", **overrides: object) -> dict[str, object]:
    """构造最小创建请求体（name + 最小卡片）。"""
    payload: dict[str, object] = {
        "name": name,
        "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
    }
    payload.update(overrides)
    return payload


async def _create_persona(
    client: AsyncClient, name: str = "feiyu", **overrides: object
) -> dict[str, object]:
    """经 API 创建人格并返回 data 段（测试铺底辅助）。"""
    response = await client.post("/api/v1/personas", json=_create_payload(name, **overrides))
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    data = body["data"]
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# 创建 / 读取 / 列表
# ---------------------------------------------------------------------------


async def test_create_and_get_roundtrip(client: AsyncClient) -> None:
    """创建 → 读取：ResultDTO 形态 + 卡片回落 + 缺省 disabled。"""
    created = await _create_persona(client)
    assert created["name"] == "feiyu"
    assert created["display_name"] == "大肥鱼"
    assert created["status"] == "disabled"
    assert created["is_active"] is False
    assert created["card"]["first_mes"] == "你回来啦！"
    persona_id = created["id"]
    assert isinstance(persona_id, int)

    response = await client.get(f"/api/v1/personas/{persona_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "200"
    assert body["data"]["id"] == persona_id


async def test_create_duplicate_name_409(client: AsyncClient) -> None:
    """唯一名冲突：409 + ResultDTO error 形态（code=CONFLICT）。"""
    await _create_persona(client, name="dup")
    response = await client.post("/api/v1/personas", json=_create_payload("dup"))
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "CONFLICT"
    assert body["data"]["name"] == "dup"


async def test_get_missing_404(client: AsyncClient) -> None:
    """缺失读取：404 + ResultDTO error 形态（code=NOT_FOUND）。"""
    response = await client.get("/api/v1/personas/999")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "NOT_FOUND"
    assert body["data"]["persona_id"] == 999


async def test_list_status_filter_and_cursor(client: AsyncClient) -> None:
    """列表：status 过滤 + 游标分页接续（id 降序）。"""
    for index in range(3):
        await _create_persona(client, name=f"p{index}", status="active")
    for index in range(2):
        await _create_persona(client, name=f"d{index}")

    active = await client.get("/api/v1/personas", params={"status": "active"})
    active_data = active.json()["data"]
    assert len(active_data["items"]) == 3
    assert active_data["page_size"] == 20

    page1 = await client.get("/api/v1/personas", params={"limit": 2})
    page1_data = page1.json()["data"]
    assert len(page1_data["items"]) == 2
    assert page1_data["has_more"] is True
    cursor: str = page1_data["next_cursor"]

    page2 = await client.get("/api/v1/personas", params={"limit": 2, "cursor": cursor})
    page2_data = page2.json()["data"]
    assert len(page2_data["items"]) == 2
    assert page2_data["items"][0]["id"] < page1_data["items"][1]["id"]

    page3 = await client.get(
        "/api/v1/personas", params={"limit": 2, "cursor": page2_data["next_cursor"]}
    )
    page3_data = page3.json()["data"]
    assert len(page3_data["items"]) == 1
    assert page3_data["has_more"] is False
    assert page3_data["next_cursor"] is None


# ---------------------------------------------------------------------------
# 更新 / 删除 / 复制
# ---------------------------------------------------------------------------


async def test_update_partial_fields(client: AsyncClient) -> None:
    """部分更新：display_name + 卡片补丁（仅出现字段生效）。"""
    created = await _create_persona(client)
    persona_id = created["id"]
    response = await client.put(
        f"/api/v1/personas/{persona_id}",
        json={
            "display_name": "小鱼干",
            "card": {"personality": "元气满满"},
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["display_name"] == "小鱼干"
    assert data["card"]["personality"] == "元气满满"
    assert data["card"]["first_mes"] == "你回来啦！"  # 未提供 → 保留


async def test_update_rename_conflict_409(client: AsyncClient) -> None:
    """重命名占用：409。"""
    await _create_persona(client, name="a")
    b = await _create_persona(client, name="b")
    response = await client.put(f"/api/v1/personas/{b['id']}", json={"name": "a"})
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


async def test_delete_flow_and_guard(client: AsyncClient) -> None:
    """删除：非激活可删（再读 404）；激活人格删除被拒 409。"""
    created = await _create_persona(client, name="del-target")
    response = await client.delete(f"/api/v1/personas/{created['id']}")
    assert response.status_code == 200
    assert response.json()["success"] is True

    missing = await client.get(f"/api/v1/personas/{created['id']}")
    assert missing.status_code == 404

    active_persona = await _create_persona(client, name="active-one", status="active")
    await client.post(f"/api/v1/personas/{active_persona['id']}/activate")
    guarded = await client.delete(f"/api/v1/personas/{active_persona['id']}")
    assert guarded.status_code == 409
    assert guarded.json()["code"] == "CONFLICT"


async def test_copy_endpoint(client: AsyncClient) -> None:
    """复制端点：新注册名 + 恒 disabled + include_states 深拷贝。"""
    source = await _create_persona(client, name="src")
    await client.put(
        f"/api/v1/personas/{source['id']}/states/M",
        json={"state": {"mood": "期待"}},
    )
    plain = await client.post(
        f"/api/v1/personas/{source['id']}/copy", json={"new_name": "copy-plain"}
    )
    assert plain.status_code == 200
    plain_data = plain.json()["data"]
    assert plain_data["status"] == "disabled"
    states = await client.get(f"/api/v1/personas/{plain_data['id']}/states")
    assert states.json()["data"] == []

    with_states = await client.post(
        f"/api/v1/personas/{source['id']}/copy",
        json={"new_name": "copy-full", "include_states": True},
    )
    assert with_states.status_code == 200
    copied_states = await client.get(f"/api/v1/personas/{with_states.json()['data']['id']}/states")
    assert copied_states.json()["data"][0]["state"] == {"mood": "期待"}


# ---------------------------------------------------------------------------
# 激活 / 停用
# ---------------------------------------------------------------------------


async def test_activate_deactivate_flow(client: AsyncClient) -> None:
    """激活流：停用人格激活 409 → 启用后激活成功 → 停用归零。"""
    created = await _create_persona(client, name="switcher")

    rejected = await client.post(f"/api/v1/personas/{created['id']}/activate")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "CONFLICT"

    await client.put(f"/api/v1/personas/{created['id']}", json={"status": "active"})
    activated = await client.post(f"/api/v1/personas/{created['id']}/activate")
    assert activated.status_code == 200
    assert activated.json()["data"]["is_active"] is True

    deactivated = await client.post(f"/api/v1/personas/{created['id']}/deactivate")
    assert deactivated.status_code == 200
    assert deactivated.json()["data"]["is_active"] is False


async def test_activate_switches_single_active(client: AsyncClient) -> None:
    """激活切换不变式：激活 B 后 A 去激活（经 API 面）。"""
    a = await _create_persona(client, name="sw-a", status="active")
    b = await _create_persona(client, name="sw-b", status="active")
    await client.post(f"/api/v1/personas/{a['id']}/activate")
    await client.post(f"/api/v1/personas/{b['id']}/activate")

    a_after = await client.get(f"/api/v1/personas/{a['id']}")
    b_after = await client.get(f"/api/v1/personas/{b['id']}")
    assert a_after.json()["data"]["is_active"] is False
    assert b_after.json()["data"]["is_active"] is True


# ---------------------------------------------------------------------------
# 卡片 / 状态端点
# ---------------------------------------------------------------------------


async def test_card_get_and_replace(client: AsyncClient) -> None:
    """卡片端点：GET 读取 + PUT 整体替换。"""
    created = await _create_persona(client)
    persona_id = created["id"]
    card = await client.get(f"/api/v1/personas/{persona_id}/card")
    assert card.status_code == 200
    card_data = card.json()["data"]
    assert card_data["persona_id"] == persona_id
    assert card_data["card"]["name"] == "大肥鱼"

    replaced = await client.put(
        f"/api/v1/personas/{persona_id}/card",
        json={"name": "小肥鱼", "first_mes": "哼。"},
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"]["card"]["first_mes"] == "哼。"
    reread = await client.get(f"/api/v1/personas/{persona_id}/card")
    assert reread.json()["data"]["card"]["name"] == "小肥鱼"


async def test_states_upsert_list_and_layer_validation(
    client: AsyncClient,
) -> None:
    """状态端点：M/S upsert + 列表 + 非法 layer 400。"""
    created = await _create_persona(client)
    persona_id = created["id"]

    upsert_m = await client.put(
        f"/api/v1/personas/{persona_id}/states/M", json={"state": {"mood": "安心"}}
    )
    assert upsert_m.status_code == 200
    assert upsert_m.json()["data"]["layer"] == "M"
    upsert_s = await client.put(
        f"/api/v1/personas/{persona_id}/states/S", json={"state": {"topic": "晚饭"}}
    )
    assert upsert_s.status_code == 200

    states = await client.get(f"/api/v1/personas/{persona_id}/states")
    states_data = states.json()["data"]
    assert [item["layer"] for item in states_data] == ["M", "S"]

    # 非法 layer（L 不入状态表 / 任意串）→ 路径参数校验失败 → 400
    bad_layer = await client.put(
        f"/api/v1/personas/{persona_id}/states/L", json={"state": {"x": 1}}
    )
    assert bad_layer.status_code == 400
    bad_body = bad_layer.json()
    assert bad_body["success"] is False
    assert bad_body["code"] == "VALIDATION_ERROR"
    assert bad_body["data"]["errors"][0]["loc"] == ["path", "layer"]


# ---------------------------------------------------------------------------
# 请求校验错误 → 400 ResultDTO 形态
# ---------------------------------------------------------------------------


async def test_create_invalid_body_400(client: AsyncClient) -> None:
    """非法请求体（缺卡片 / 空 name）→ 400 + VALIDATION_ERROR 形态。"""
    response = await client.post("/api/v1/personas", json={"name": ""})
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["data"]["errors"], list)
    assert body["data"]["errors"]


async def test_invalid_path_param_400(client: AsyncClient) -> None:
    """非法路径参数（persona_id 非整型）→ 400。"""
    response = await client.get("/api/v1/personas/not-a-number")
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_invalid_query_param_400(client: AsyncClient) -> None:
    """非法查询参数（limit 越界 / status 非法值）→ 400。"""
    response = await client.get("/api/v1/personas", params={"limit": 0})
    assert response.status_code == 400
    response = await client.get("/api/v1/personas", params={"status": "bogus"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 批量删除（部分成功形态）
# ---------------------------------------------------------------------------


async def test_batch_delete_partial_success(client: AsyncClient) -> None:
    """批量删除：成功 + 激活守卫失败 + 缺失失败并存（HTTP 200 包裹部分成功）。"""
    ok_persona = await _create_persona(client, name="batch-ok")
    active_persona = await _create_persona(client, name="batch-active", status="active")
    await client.post(f"/api/v1/personas/{active_persona['id']}/activate")

    response = await client.post(
        "/api/v1/personas/batch-delete",
        json={"ids": [ok_persona["id"], active_persona["id"], 999]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["total"] == 3
    assert data["succeeded"] == 1
    assert data["failed"] == 2
    by_id = {item["id"]: item for item in data["items"]}
    assert by_id[ok_persona["id"]]["success"] is True
    assert by_id[active_persona["id"]]["code"] == "CONFLICT"
    assert by_id[999]["code"] == "NOT_FOUND"

    missing = await client.get(f"/api/v1/personas/{ok_persona['id']}")
    assert missing.status_code == 404
    kept = await client.get(f"/api/v1/personas/{active_persona['id']}")
    assert kept.json()["data"]["is_active"] is True


async def test_batch_delete_empty_ids_400(client: AsyncClient) -> None:
    """批量删除空 ids：请求校验失败 400（min_length=1）。"""
    response = await client.post("/api/v1/personas/batch-delete", json={"ids": []})
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 处理器直调契约面——handler 函数作为普通 async 可调用直接调用，逐端点
# 锁定 ResultDTO 包装契约（success / code / message / data 形态）。
# 与 HTTP 面互补：HTTP 面验证路由装配与错误映射（经 ASGITransport），
# 直调面验证处理器函数体全路径与响应包装（不经 FastAPI 派发）。
# ---------------------------------------------------------------------------


def _create_dto(name: str = "direct-feiyu") -> PersonaCreateDTO:
    """直调面创建 DTO（与 HTTP 面 _create_payload 同构）。"""
    return PersonaCreateDTO.model_validate(_create_payload(name))


async def test_direct_handlers_registry_contract(db_session: AsyncSession) -> None:
    """注册面直调：list / create / get / update / delete 的 ResultDTO 契约。"""
    created = await create_persona(_create_dto(), db_session)
    assert created.success is True
    assert created.code == "200"
    assert created.message == "人格已创建"
    assert created.data is not None
    assert created.data.name == "direct-feiyu"

    listed = await list_personas(status=None, cursor=None, limit=20, db=db_session)
    assert listed.success is True
    assert listed.data is not None
    assert len(listed.data.items) == 1
    assert listed.data.page_size == 20

    fetched = await get_persona(created.data.id, db_session)
    assert fetched.success is True
    assert fetched.data is not None
    assert fetched.data.display_name == "大肥鱼"

    updated = await update_persona(
        created.data.id,
        PersonaUpdateDTO(display_name="直调更新", card=CharaCardPatchDTO(personality="元气")),
        db_session,
    )
    assert updated.success is True
    assert updated.data is not None
    assert updated.data.display_name == "直调更新"
    assert updated.data.card.personality == "元气"

    deleted = await delete_persona(created.data.id, db_session)
    assert deleted.success is True
    assert deleted.message == "人格已删除"
    assert deleted.data is None


async def test_direct_handlers_switching_contract(db_session: AsyncSession) -> None:
    """激活切换面直调：activate / deactivate 的 ResultDTO 契约。"""
    record = await create_persona(
        PersonaCreateDTO.model_validate(_create_payload("direct-switch", status="active")),
        db_session,
    )
    assert record.data is not None

    activated = await activate_persona(record.data.id, db_session)
    assert activated.success is True
    assert activated.message == "人格已激活（单激活切换）"
    assert activated.data is not None
    assert activated.data.is_active is True

    deactivated = await deactivate_persona(record.data.id, db_session)
    assert deactivated.success is True
    assert deactivated.data is not None
    assert deactivated.data.is_active is False


async def test_direct_handlers_card_states_batch_contract(
    db_session: AsyncSession,
) -> None:
    """卡片 / 状态 / 复制 / 批量面直调：ResultDTO 契约。"""
    created = await create_persona(_create_dto("direct-card"), db_session)
    persona_id = created.data.id if created.data is not None else 0
    assert persona_id > 0

    card = await get_persona_card(persona_id, db_session)
    assert card.success is True
    assert card.data is not None
    assert card.data.card.name == "大肥鱼"

    replaced = await replace_persona_card(persona_id, CharaCardModel(name="直调卡"), db_session)
    assert replaced.success is True
    assert replaced.message == "人格卡片已替换"
    assert replaced.data is not None
    assert replaced.data.card.name == "直调卡"

    upserted = await upsert_persona_state(
        persona_id, PersonaStateLayer.M, PersonaStateUpsertDTO(state={"mood": "安心"}), db_session
    )
    assert upserted.success is True
    assert upserted.message == "人格状态已更新"
    assert upserted.data is not None
    assert upserted.data.state == {"mood": "安心"}

    states = await list_persona_states(persona_id, db_session)
    assert states.success is True
    assert states.data is not None
    assert [state.layer for state in states.data] == [PersonaStateLayer.M]

    copied = await copy_persona(persona_id, PersonaCopyDTO(new_name="direct-copy"), db_session)
    assert copied.success is True
    assert copied.data is not None
    assert copied.data.name == "direct-copy"

    batch = await batch_delete_personas(
        PersonaBatchDeleteRequest(ids=[copied.data.id if copied.data is not None else 0]),
        db_session,
    )
    assert batch.success is True
    assert batch.data is not None
    assert batch.data.succeeded == 1
