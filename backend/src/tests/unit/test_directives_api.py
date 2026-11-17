"""app/api/directives 路由测试——常备指令操作面（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（directives router + 共享异常处理器）；
DB 经共享会话级 SQLite fixture 外部安装到 engine holder。覆盖面：创建 /
列表 / 更新（supersede 链）/ 删除 / 边界拒绝 / 人格解析（缺省主人格 /
无主人格 404）/ 跨人格隔离 404。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.directives import router as directives_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService


async def _create_persona(
    db_session: AsyncSession, name: str, *, active: bool = False
) -> int:
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
async def directives_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(directives_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(directives_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=directives_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "dir-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


async def _create(client: AsyncClient, **payload: Any) -> dict[str, Any]:
    """经 API 创建长期指令并返回 data 段（铺底辅助）。"""
    response = await client.post("/api/v1/directives", json=payload)
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_create_and_list(client: AsyncClient, primary_persona_id: int) -> None:
    """创建 → 列表：ResultDTO 形态 + 命名空间 topic + 正文裁剪。"""
    created = await _create(client, content="  以后都用简体中文  ")
    assert created["topic"].startswith("directive:")
    assert created["content"] == "以后都用简体中文"
    assert created["version"] == 1

    listing = await client.get("/api/v1/directives")
    assert listing.status_code == 200
    items = listing.json()["data"]
    assert [item["id"] for item in items] == [created["id"]]


async def test_update_supersedes_chain(client: AsyncClient, primary_persona_id: int) -> None:
    """更新：同 topic supersede（新主键 + version 递增）——列表只剩现行版本。"""
    created = await _create(client, content="不要熬夜")
    updated = await client.put(
        f"/api/v1/directives/{created['id']}", json={"content": "不要超过十二点睡"}
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["topic"] == created["topic"]
    assert data["version"] == 2
    assert data["id"] != created["id"]

    listing = await client.get("/api/v1/directives")
    assert [item["content"] for item in listing.json()["data"]] == ["不要超过十二点睡"]


async def test_delete_removes_directive(client: AsyncClient, primary_persona_id: int) -> None:
    """删除：列表清空；重复删除 404。"""
    created = await _create(client, content="以后都用简体中文")
    deleted = await client.delete(f"/api/v1/directives/{created['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/v1/directives")).json()["data"] == []

    again = await client.delete(f"/api/v1/directives/{created['id']}")
    assert again.status_code == 404


async def test_create_blank_rejected(client: AsyncClient, primary_persona_id: int) -> None:
    """创建：全空白正文服务层 422；空串在字段边界 400。"""
    blank = await client.post("/api/v1/directives", json={"content": "   "})
    assert blank.status_code == 422
    empty = await client.post("/api/v1/directives", json={"content": ""})
    assert empty.status_code == 400


async def test_no_primary_persona_not_found(client: AsyncClient) -> None:
    """无主人格：列表 404（fail loud——携可操作指引）。"""
    listing = await client.get("/api/v1/directives")
    assert listing.status_code == 404
    assert "主人格" in listing.json()["message"]


async def test_explicit_persona_isolation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """显式 persona_id：他人格指令不可见 / 不可删（隔离语义 404）。"""
    primary = await _create_persona(db_session, "dir-primary-2", active=True)
    await PersonaService().set_primary(db_session, primary)
    other = await _create_persona(db_session, "dir-other", active=True)

    created = await _create(client, content="以后都用简体中文")
    other_list = await client.get("/api/v1/directives", params={"persona_id": other})
    assert other_list.json()["data"] == []

    other_delete = await client.delete(
        f"/api/v1/directives/{created['id']}", params={"persona_id": other}
    )
    assert other_delete.status_code == 404

    missing = await client.get("/api/v1/directives", params={"persona_id": 999_999})
    assert missing.status_code == 404
