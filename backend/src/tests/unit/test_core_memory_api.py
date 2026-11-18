"""app/api/core_memory 路由测试——自编辑核心记忆操作面（ASGITransport 零网络）。

测试策略：本地装配 FastAPI app（core-memory router + 共享异常处理器）；
DB 经共享会话级 SQLite fixture 外部安装到 engine holder。覆盖面：四块
读取（懒建）/ 主人编辑（版本 / 锁定）/ 修订历史（最新在前）/ 正文上限
400 / 非法块键 400 / 人格解析（无主人格 404）/ 跨人格隔离（他人格块
不可见、编辑互不影响）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.core_memory_orm as core_memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.core_memory import router as core_memory_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService

#: 四块键的固定顺序（接口契约断言面）。
_BLOCK_ORDER = ["self_identity", "owner_profile", "relationship_story", "open_notes"]


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
async def core_memory_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(core_memory_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(core_memory_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=core_memory_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "core-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


async def _blocks(client: AsyncClient) -> list[dict[str, Any]]:
    """读取四块（成功断言 + data 段返回）。"""
    response = await client.get("/api/v1/core-memory/blocks")
    assert response.status_code == 200
    data: list[dict[str, Any]] = response.json()["data"]
    return data


async def test_blocks_are_seeded_in_fixed_order(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """读取：四块按固定序返回，空正文 / 未锁定 / 首版。"""
    blocks = await _blocks(client)
    assert [block["block"] for block in blocks] == _BLOCK_ORDER
    assert all(block["content"] == "" for block in blocks)
    assert all(block["locked"] is False for block in blocks)
    assert all(block["version"] == 1 for block in blocks)
    assert all(block["last_edited_session_id"] is None for block in blocks)


async def test_update_block_and_list_revisions(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """主人编辑：版本递增 + 修订可见（owner/admin + 最新在前）。"""
    first = await client.put(
        "/api/v1/core-memory/blocks/self_identity", json={"content": "我是一条会写字的鱼"}
    )
    assert first.status_code == 200
    assert first.json()["data"]["version"] == 2

    second = await client.put(
        "/api/v1/core-memory/blocks/self_identity", json={"content": "我更正了自我认知"}
    )
    assert second.json()["data"]["version"] == 3

    revisions = await client.get("/api/v1/core-memory/blocks/self_identity/revisions")
    assert revisions.status_code == 200
    items = revisions.json()["data"]
    assert [item["content"] for item in items] == ["我更正了自我认知", "我是一条会写字的鱼"]
    assert [item["version"] for item in items] == [3, 2]
    assert all(item["provenance"] == "owner" for item in items)
    assert all(item["source"] == "admin" for item in items)
    assert all(item["session_id"] is None for item in items)


async def test_update_lock_toggle(client: AsyncClient, primary_persona_id: int) -> None:
    """锁定开关：locked 落库；解锁后回到可编辑（无修订增量）。"""
    locked = await client.put(
        "/api/v1/core-memory/blocks/open_notes",
        json={"content": "备忘正文", "locked": True},
    )
    assert locked.json()["data"]["locked"] is True
    unlocked = await client.put(
        "/api/v1/core-memory/blocks/open_notes",
        json={"content": "备忘正文", "locked": False},
    )
    assert unlocked.json()["data"]["locked"] is False
    revisions = await client.get("/api/v1/core-memory/blocks/open_notes/revisions")
    assert len(revisions.json()["data"]) == 1


async def test_update_over_cap_rejected(client: AsyncClient, primary_persona_id: int) -> None:
    """正文超 2000 字符：400（DTO 边界拒绝）。"""
    response = await client.put(
        "/api/v1/core-memory/blocks/self_identity", json={"content": "x" * 2001}
    )
    assert response.status_code == 400


async def test_unknown_block_key_rejected(client: AsyncClient, primary_persona_id: int) -> None:
    """非法块键（路径枚举越界）：400。"""
    response = await client.put(
        "/api/v1/core-memory/blocks/fifth_block", json={"content": "内容"}
    )
    assert response.status_code == 400


async def test_no_primary_persona_not_found(client: AsyncClient) -> None:
    """无主人格：404（fail loud——携可操作指引）。"""
    listing = await client.get("/api/v1/core-memory/blocks")
    assert listing.status_code == 404
    assert "主人格" in listing.json()["message"]


async def test_explicit_persona_isolation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """跨人格隔离：他人格块自空、主人格写入不影响其内容。"""
    first_id = await _create_persona(db_session, "core-a", active=True)
    second_id = await _create_persona(db_session, "core-b")
    await PersonaService().set_primary(db_session, first_id)
    await client.put("/api/v1/core-memory/blocks/self_identity", json={"content": "甲的自我认知"})

    other = await client.get(
        "/api/v1/core-memory/blocks", params={"persona_id": second_id}
    )
    assert all(block["content"] == "" for block in other.json()["data"])
    same = await client.get("/api/v1/core-memory/blocks", params={"persona_id": first_id})
    target = next(block for block in same.json()["data"] if block["block"] == "self_identity")
    assert target["content"] == "甲的自我认知"
    other_revisions = await client.get(
        "/api/v1/core-memory/blocks/self_identity/revisions", params={"persona_id": second_id}
    )
    assert other_revisions.json()["data"] == []
