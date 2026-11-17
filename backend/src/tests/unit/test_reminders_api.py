"""app/api/reminders 路由测试——全操作面（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（reminders router + 共享异常处理器——无
全局 app 导入、无真实服务器）；DB 经共享会话级 SQLite fixture 外部安装
到 engine holder。覆盖面：创建 / 列表（status 过滤 + 游标）/ 读取 /
取消（状态守卫）/ 批量取消部分成功 / 人格解析（缺省主人格 / 显式
persona_id / 无主人格 404）/ 跨人格隔离 404 / 标题边界 422。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.reminders.orm as reminders_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.reminders import router as reminders_router
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
async def reminders_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
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
    app.include_router(reminders_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(reminders_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=reminders_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "rem-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


def _payload(**overrides: Any) -> dict[str, Any]:
    """构造最小创建请求体。"""
    payload: dict[str, Any] = {"title": "喝水", "due_at_ms": 2_000}
    payload.update(overrides)
    return payload


async def _create(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    """经 API 创建提醒并返回 data 段（铺底辅助）。"""
    response = await client.post("/api/v1/reminders", json=_payload(**overrides))
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_create_and_list(client: AsyncClient, primary_persona_id: int) -> None:
    """创建 → 列表：ResultDTO 形态 + status 过滤 + 人格归属。"""
    created = await _create(client, title=" 喝水 ", source="chat")
    assert created["persona_id"] == str(primary_persona_id)
    assert created["title"] == "喝水"
    assert created["status"] == "pending"
    assert created["source"] == "chat"

    listing = await client.get("/api/v1/reminders", params={"status": "pending"})
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert [item["id"] for item in items] == [created["id"]]

    empty = await client.get("/api/v1/reminders", params={"status": "delivered"})
    assert empty.json()["data"]["items"] == []


async def test_get_and_cancel(client: AsyncClient, primary_persona_id: int) -> None:
    """读取 + 取消：状态迁移 + 重复取消 422（VALIDATION_ERROR）。"""
    created = await _create(client)
    fetched = await client.get(f"/api/v1/reminders/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["status"] == "pending"

    cancelled = await client.post(f"/api/v1/reminders/{created['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"

    again = await client.post(f"/api/v1/reminders/{created['id']}/cancel")
    assert again.status_code == 422


async def test_batch_cancel_partial(client: AsyncClient, primary_persona_id: int) -> None:
    """批量取消：部分成功（成功项 + 不存在项）。"""
    first = await _create(client, title="一")
    second = await _create(client, title="二")
    await client.post(f"/api/v1/reminders/{second['id']}/cancel")

    response = await client.post(
        "/api/v1/reminders/batch-cancel", json={"ids": [first["id"], 999_999]}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    assert data["items"][1]["code"] == "NOT_FOUND"


async def test_batch_cancel_duplicate_ids_rejected(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """批量请求：重复 id 在请求校验面拒绝（400 + VALIDATION_ERROR）。"""
    response = await client.post("/api/v1/reminders/batch-cancel", json={"ids": [1, 1]})
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_rejects_blank_title(client: AsyncClient, primary_persona_id: int) -> None:
    """创建：全空白标题在服务层拒绝（422）；空串在字段边界即拒绝（400）。"""
    blank = await client.post("/api/v1/reminders", json=_payload(title="   "))
    assert blank.status_code == 422
    empty = await client.post("/api/v1/reminders", json=_payload(title=""))
    assert empty.status_code == 400
    assert empty.json()["code"] == "VALIDATION_ERROR"


async def test_no_primary_persona_not_found(client: AsyncClient, db_session: AsyncSession) -> None:
    """无人格铺底：列表 / 创建均 404（无主人格 fail loud——携 fix 指引）。"""
    listing = await client.get("/api/v1/reminders")
    assert listing.status_code == 404
    assert "主人格" in listing.json()["message"]


async def test_explicit_persona_scope_isolation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """显式 persona_id：他人格提醒不可见（隔离语义——跨作用域 404）。"""
    primary = await _create_persona(db_session, "rem-primary-2", active=True)
    await PersonaService().set_primary(db_session, primary)
    other = await _create_persona(db_session, "rem-other", active=True)

    created = await _create(client)
    other_list = await client.get("/api/v1/reminders", params={"persona_id": other})
    assert other_list.status_code == 200
    assert other_list.json()["data"]["items"] == []

    other_get = await client.get(
        f"/api/v1/reminders/{created['id']}", params={"persona_id": other}
    )
    assert other_get.status_code == 404

    missing = await client.get("/api/v1/reminders", params={"persona_id": 999_999})
    assert missing.status_code == 404
