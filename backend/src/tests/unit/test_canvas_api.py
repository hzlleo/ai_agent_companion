"""app/api/canvas 路由测试——全操作面（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（canvas router + 共享异常处理器——无全局
app 导入、无真实服务器）；DB 经共享会话级 SQLite fixture 外部安装到
engine holder。覆盖面：创建 / 列表（类型过滤）/ 读取 / 修订（revision
递增）/ 删除（幂等）/ 导出（文件名 + Content-Type + 原始字节）/ 人格解析
（缺省主人格 / 显式 persona_id / 无主人格 404）/ 跨人格隔离 404 / 请求
边界 400（请求体校验统一由异常处理器映射为 400）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.canvas.canvas_orm as canvas_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.canvas import router as canvas_router
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
async def canvas_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
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
    app.include_router(canvas_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(canvas_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=canvas_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "canvas-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


def _payload(**overrides: Any) -> dict[str, Any]:
    """构造最小创建请求体。"""
    payload: dict[str, Any] = {
        "title": "今日心情卡",
        "kind": "html",
        "content": "<div>今天很好</div>",
    }
    payload.update(overrides)
    return payload


async def _create(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    """经 API 创建画布并返回 data 段（铺底辅助）。"""
    response = await client.post("/api/v1/canvas", json=_payload(**overrides))
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_create_list_get(client: AsyncClient, primary_persona_id: int) -> None:
    """创建 → 列表 → 读取：内容与修订号贯通。"""
    created = await _create(client)
    assert created["revision"] == 1
    assert created["kind"] == "html"
    listed = await client.get("/api/v1/canvas", params={"limit": 10})
    assert listed.status_code == 200
    records = listed.json()["data"]
    assert [record["id"] for record in records] == [created["id"]]
    fetched = await client.get(f"/api/v1/canvas/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["content"] == "<div>今天很好</div>"


async def test_list_kind_filter(client: AsyncClient, primary_persona_id: int) -> None:
    """列表类型过滤：svg 只返回 svg 产物。"""
    await _create(client, title="网页卡", kind="html")
    svg = await _create(client, title="图形卡", kind="svg", content="<svg/>")
    response = await client.get("/api/v1/canvas", params={"kind": "svg"})
    assert [record["id"] for record in response.json()["data"]] == [svg["id"]]


async def test_update_increments_revision(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """修订：revision 递增 + 字段按请求更新。"""
    created = await _create(client)
    response = await client.patch(
        f"/api/v1/canvas/{created['id']}", json={"content": "<div>二版</div>"}
    )
    assert response.status_code == 200
    updated = response.json()["data"]
    assert updated["revision"] == 2
    assert updated["content"] == "<div>二版</div>"
    assert updated["title"] == "今日心情卡"


async def test_delete_idempotent(client: AsyncClient, primary_persona_id: int) -> None:
    """删除：首次报告主键，再次删除空报告。"""
    created = await _create(client)
    first = await client.delete(f"/api/v1/canvas/{created['id']}")
    second = await client.delete(f"/api/v1/canvas/{created['id']}")
    assert first.json()["data"]["deleted_ids"] == [created["id"]]
    assert second.json()["data"]["deleted_ids"] == []


async def test_export_returns_attachment_bytes(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """导出：Content-Type 按类型 + 文件名带主键修订号 + 原始字节。"""
    created = await _create(client, title="Mood Card")
    response = await client.get(f"/api/v1/canvas/{created['id']}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="canvas-{created["id"]}-r1-mood-card.html"'
    )
    assert response.content == "<div>今天很好</div>".encode()


async def test_export_markdown_media_type(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """导出：Markdown 产物 MIME 为 text/markdown 且扩展名 .md。"""
    created = await _create(client, title="笔记", kind="markdown", content="# 标题")
    response = await client.get(
        f"/api/v1/canvas/{created['id']}/export", params={"persona_id": primary_persona_id}
    )
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].endswith('.md"')


async def test_cross_persona_isolation(
    client: AsyncClient, db_session: AsyncSession, primary_persona_id: int
) -> None:
    """跨人格隔离：他人格画布读取 / 修订 / 删除 / 导出均 404。"""
    created = await _create(client)
    other_id = await _create_persona(db_session, "canvas-other")
    params = {"persona_id": other_id}
    assert (await client.get(f"/api/v1/canvas/{created['id']}", params=params)).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/canvas/{created['id']}", json={"title": "改"}, params=params
        )
    ).status_code == 404
    assert (await client.delete(f"/api/v1/canvas/{created['id']}", params=params)).json()[
        "data"
    ]["deleted_ids"] == []
    assert (
        await client.get(f"/api/v1/canvas/{created['id']}/export", params=params)
    ).status_code == 404


async def test_missing_persona_and_validation(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """人格解析：显式 persona_id 不存在 → 404；请求边界 → 422。"""
    assert (
        await client.post("/api/v1/canvas", json=_payload(), params={"persona_id": 9999})
    ).status_code == 404
    assert (await client.post("/api/v1/canvas", json=_payload(title="   "))).status_code == 400
    assert (await client.post("/api/v1/canvas", json=_payload(kind="pdf"))).status_code == 400
    created = await _create(client)
    empty_patch = await client.patch(f"/api/v1/canvas/{created['id']}", json={})
    assert empty_patch.status_code == 400
    assert (await client.get("/api/v1/canvas/0")).status_code == 400
