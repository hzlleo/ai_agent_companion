"""app/api/skills 路由测试——程序性技能操作面（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（skills router + 共享异常处理器）；DB 经
共享会话级 SQLite fixture 外部安装到 engine holder。覆盖面：手工创建 /
列表（状态过滤 + 游标）/ 原地编辑 / 启用与停用（幂等）/ 删除（重复 404）/
同名冲突 409 / 边界拒绝 / 人格解析（缺省主人格 / 无主人格 404）/ 跨人格
隔离 404。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.skills_orm as skills_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.skills import router as skills_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService


async def _create_persona(db_session: AsyncSession, name: str, *, active: bool = False) -> int:
    """经真实 PersonaService 创建人格（可选启用人格）。"""
    created = await PersonaService().create(
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
async def skills_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(skills_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(skills_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=skills_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "skills-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


async def _create(client: AsyncClient, **payload: Any) -> dict[str, Any]:
    """经 API 创建技能并返回 data 段（铺底辅助）。"""
    body: dict[str, Any] = {
        "name": "周报流程",
        "description": "主人说「写周报」时使用",
        "triggers": ["写周报"],
        "content": "1. 先列三件事\n2. 再写进展",
        "confidence": 0.6,
    }
    body.update(payload)
    response = await client.post("/api/v1/skills", json=body)
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_create_and_list(client: AsyncClient, primary_persona_id: int) -> None:
    """创建 → 列表：ResultDTO 形态 + slug 派生 + 初始状态字段齐备。"""
    created = await _create(client, name="Weekly Report")
    assert created["slug"] == "weekly-report"
    assert created["status"] == "active"
    assert created["source"] == "manual"
    assert created["version"] == 1
    assert created["use_count"] == 0
    assert created["last_used_ms"] is None
    assert created["triggers"] == ["写周报"]

    listing = await client.get("/api/v1/skills")
    assert listing.status_code == 200
    page = listing.json()["data"]
    assert [item["id"] for item in page["items"]] == [created["id"]]
    assert page["total"] == -1


async def test_create_duplicate_conflicts(client: AsyncClient, primary_persona_id: int) -> None:
    """同人格同名：409（中文冲突消息——不静默覆盖）。"""
    await _create(client)
    duplicate = await client.post(
        "/api/v1/skills",
        json={
            "name": "周报流程",
            "description": "另一条同名技能",
            "triggers": ["周报"],
            "content": "1. 步骤",
        },
    )
    assert duplicate.status_code == 409
    assert "slug 冲突" in duplicate.json()["message"]


async def test_update_in_place_increments_version(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """原地编辑：字段更新 + version 递增 + 列表仍是同一行。"""
    created = await _create(client)
    updated = await client.put(
        f"/api/v1/skills/{created['id']}",
        json={"content": "1. 新的步骤", "confidence": 0.9},
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["id"] == created["id"]
    assert data["version"] == 2
    assert data["confidence"] == 0.9
    assert data["content"] == "1. 新的步骤"
    listing = await client.get("/api/v1/skills")
    assert [item["version"] for item in listing.json()["data"]["items"]] == [2]


async def test_enable_disable_is_idempotent(client: AsyncClient, primary_persona_id: int) -> None:
    """停用 / 启用：状态切换 + 幂等；状态过滤可见。"""
    created = await _create(client)
    disabled = await client.post(f"/api/v1/skills/{created['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "disabled"
    again = await client.post(f"/api/v1/skills/{created['id']}/disable")
    assert again.status_code == 200
    active_page = await client.get("/api/v1/skills", params={"status": "active"})
    assert active_page.json()["data"]["items"] == []
    disabled_page = await client.get("/api/v1/skills", params={"status": "disabled"})
    assert [item["id"] for item in disabled_page.json()["data"]["items"]] == [created["id"]]
    enabled = await client.post(f"/api/v1/skills/{created['id']}/enable")
    assert enabled.json()["data"]["status"] == "active"


async def test_delete_removes_skill(client: AsyncClient, primary_persona_id: int) -> None:
    """删除：列表清空；重复删除 404。"""
    created = await _create(client)
    deleted = await client.delete(f"/api/v1/skills/{created['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/v1/skills")).json()["data"]["items"] == []
    again = await client.delete(f"/api/v1/skills/{created['id']}")
    assert again.status_code == 404


async def test_create_boundaries_rejected(client: AsyncClient, primary_persona_id: int) -> None:
    """创建边界：空触发短语 400 / 非法状态 400 / 越界置信度 400 / 超条数 400。"""
    empty_triggers = await client.post(
        "/api/v1/skills",
        json={
            "name": "边界技能",
            "description": "描述",
            "triggers": [],
            "content": "正文",
        },
    )
    assert empty_triggers.status_code == 400
    bad_status = await client.get("/api/v1/skills", params={"status": "梦境"})
    assert bad_status.status_code == 400
    bad_confidence = await client.post(
        "/api/v1/skills",
        json={
            "name": "置信度技能",
            "description": "描述",
            "triggers": ["触发"],
            "content": "正文",
            "confidence": 1.5,
        },
    )
    assert bad_confidence.status_code == 400
    too_many_triggers = await client.post(
        "/api/v1/skills",
        json={
            "name": "触发过载技能",
            "description": "描述",
            "triggers": [f"触发{index}" for index in range(9)],
            "content": "正文",
        },
    )
    assert too_many_triggers.status_code == 400


async def test_no_primary_persona_not_found(client: AsyncClient) -> None:
    """无主人格：列表 404（fail loud——携可操作指引）。"""
    listing = await client.get("/api/v1/skills")
    assert listing.status_code == 404
    assert "主人格" in listing.json()["message"]


async def test_explicit_persona_isolation(client: AsyncClient, db_session: AsyncSession) -> None:
    """跨人格隔离：他人格技能不可见 / 编辑 404 / 本作用域可见。"""
    first_id = await _create_persona(db_session, "skills-a", active=True)
    second_id = await _create_persona(db_session, "skills-b")
    await PersonaService().set_primary(db_session, first_id)
    created = await _create(client)
    listing = await client.get("/api/v1/skills", params={"persona_id": second_id})
    assert listing.json()["data"]["items"] == []
    updated = await client.put(
        f"/api/v1/skills/{created['id']}",
        json={"content": "他人格编辑"},
        params={"persona_id": second_id},
    )
    assert updated.status_code == 404
    same = await client.get("/api/v1/skills", params={"persona_id": first_id})
    assert [item["id"] for item in same.json()["data"]["items"]] == [created["id"]]


async def test_pagination_cursor(client: AsyncClient, primary_persona_id: int) -> None:
    """游标分页：limit=1 首页返回 next_cursor，续页取到其余条目。"""
    for index in range(3):
        await _create(client, name=f"技能{index}", triggers=[f"触发{index}"])
    first = await client.get("/api/v1/skills", params={"limit": 1})
    cursor = first.json()["data"]["next_cursor"]
    assert cursor
    second = await client.get("/api/v1/skills", params={"limit": 10, "cursor": cursor})
    ids = [item["id"] for item in first.json()["data"]["items"]] + [
        item["id"] for item in second.json()["data"]["items"]
    ]
    assert len(ids) == len(set(ids)) == 3
