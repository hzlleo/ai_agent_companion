"""app/api/memories 检索端点测试——POST /api/v1/memories/search（P1 chunk 2）。

测试策略：本地装配 FastAPI app（memories router + 共享异常处理器——同
test_memories_api.py 模式；DB 经共享会话级 SQLite fixture 外部安装）。
覆盖面：keyword 模式往返（回退存储命中）/ 人格解析（缺省激活 / 显式
persona_id / 无激活 404 / 不存在 404）/ dense 模式在回退存储上 422
（清晰「回退存储不支持向量检索」——app.state.gateway 挂桩嵌入器）/
请求校验 422（空 query / limit 越界 / 非法 mode）/ 处理器直调契约面
（ResultDTO 包装 + 人格谓词透传）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.memories import router as memories_router
from app.api.memories import search_memories
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import EmbeddingsResponseModel
from packages.harness.memory.retrieval_models import MemoryIndexEntryDTO
from packages.harness.memory.sqlite_store import SQLiteFallbackStore
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService


class StubGateway:
    """桩网关（embed_by_role 可控——dense 模式嵌入面）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化（默认成功回送单向量）。"""
        self.calls: list[Sequence[str]] = []
        self._fail = fail

    async def embed_by_role(
        self,
        texts: Sequence[str],
        *,
        persona_id: str | None = None,
        request_id: str | None = None,
    ) -> EmbeddingsResponseModel:
        """批量向量化（捕获调用；可注入失败）。"""
        self.calls.append(texts)
        if self._fail:
            from packages.harness.core.errors import BusinessException, ErrorCode

            raise BusinessException(
                code=ErrorCode.MODEL_NOT_FOUND, message="embed 角色未绑定（桩）"
            )
        return EmbeddingsResponseModel(model="stub-embed", vectors=[[0.1, 0.2] for _ in texts])


async def _create_persona(db_session: AsyncSession, name: str, *, active: bool = False) -> int:
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
async def search_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(memories_router)
    register_exception_handlers(app)
    app.state.gateway = StubGateway()
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(search_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=search_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def active_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个激活人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "search-active", active=True)
    await PersonaService().activate(db_session, persona_id)
    yield persona_id


async def _index_entries(persona_id: int) -> None:
    """经回退存储直写索引行（检索铺底——共享引擎已由 fixture 安装）。"""
    store = SQLiteFallbackStore()
    await store.upsert(
        [
            MemoryIndexEntryDTO.model_validate(
                {
                    "persona_id": persona_id,
                    "ref_type": "episode",
                    "ref_id": 11,
                    "kind": "chat",
                    "provenance": "owner",
                    "ts_ms": 1000,
                    "content": "今天吃了很多小鱼干，小鱼干真好吃",
                }
            ),
            MemoryIndexEntryDTO.model_validate(
                {
                    "persona_id": persona_id,
                    "ref_type": "curated",
                    "ref_id": 21,
                    "kind": "memory",
                    "provenance": "agent",
                    "ts_ms": 2000,
                    "content": "主人喜欢小鱼干",
                }
            ),
        ],
        [[0.1], [0.2]],
    )


def _search_payload(**overrides: Any) -> dict[str, Any]:
    """构造最小检索请求体（keyword 模式；测试铺底辅助）。"""
    payload: dict[str, Any] = {"query": "小鱼干", "mode": "keyword", "limit": 10}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# ASGI 面——keyword 往返 / 人格解析
# ---------------------------------------------------------------------------


async def test_search_keyword_round_trip(client: AsyncClient, active_persona_id: int) -> None:
    """keyword 模式：命中回退存储索引行（ResultDTO 形态 + score 降序）。"""
    await _index_entries(active_persona_id)

    response = await client.post("/api/v1/memories/search", json=_search_payload())
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["success"] is True
    items = body["data"]
    assert len(items) == 2
    assert items[0]["ref_id"] == 11  # 出现两次——朴素相关度降序
    assert items[0]["score"] == 2.0
    assert items[1]["ref_id"] == 21
    assert items[1]["score"] == 1.0
    assert items[0]["ref_type"] == "episode"
    assert items[0]["provenance"] == "owner"


async def test_search_persona_resolution(
    client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """人格解析：缺省激活 / 显式 persona_id / 不存在 404 / 无激活 404。"""
    await _index_entries(active_persona_id)
    other_id = await _create_persona(db_session, "search-other")

    # 显式他人格：隔离（无命中——索引谓词过滤）
    explicit = await client.post(
        "/api/v1/memories/search", json=_search_payload(persona_id=other_id)
    )
    assert explicit.status_code == 200
    assert explicit.json()["data"] == []

    # 缺省 = 激活人格（命中）
    default = await client.post("/api/v1/memories/search", json=_search_payload())
    assert len(default.json()["data"]) == 2

    # 指定不存在人格：404
    missing = await client.post("/api/v1/memories/search", json=_search_payload(persona_id=9999))
    assert missing.status_code == 404

    # 停用激活人格后缺省检索：404（无激活 fail loud）
    await PersonaService().deactivate(db_session, active_persona_id)
    no_active = await client.post("/api/v1/memories/search", json=_search_payload())
    assert no_active.status_code == 404


async def test_search_filters_passthrough(client: AsyncClient, active_persona_id: int) -> None:
    """过滤透传：kinds / provenance / 时间范围（闭区间）作用于检索面。"""
    await _index_entries(active_persona_id)

    curated_only = await client.post(
        "/api/v1/memories/search", json=_search_payload(kinds=["memory"])
    )
    items = curated_only.json()["data"]
    assert [item["ref_id"] for item in items] == [21]

    agent_only = await client.post(
        "/api/v1/memories/search", json=_search_payload(provenance="agent")
    )
    assert [item["ref_id"] for item in agent_only.json()["data"]] == [21]

    window = await client.post(
        "/api/v1/memories/search",
        json=_search_payload(start_ms=1000, end_ms=1000),
    )
    assert [item["ref_id"] for item in window.json()["data"]] == [11]  # 闭区间含端点


# ---------------------------------------------------------------------------
# ASGI 面——模式 / 校验
# ---------------------------------------------------------------------------


async def test_search_dense_on_fallback_store_rejected(
    client: AsyncClient, active_persona_id: int
) -> None:
    """dense 模式 + 回退存储：422 VALIDATION_ERROR（嵌入先行、存储拒收）。"""
    await _index_entries(active_persona_id)

    response = await client.post("/api/v1/memories/search", json=_search_payload(mode="dense"))
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    assert "回退存储不支持向量检索" in body["message"]


async def test_search_hybrid_on_fallback_store_rejected(
    client: AsyncClient, active_persona_id: int
) -> None:
    """hybrid 模式 + 回退存储：同 422（向量模式族一致拒收）。"""
    await _index_entries(active_persona_id)
    response = await client.post("/api/v1/memories/search", json=_search_payload(mode="hybrid"))
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_search_request_validation_400(client: AsyncClient, active_persona_id: int) -> None:
    """请求校验：空 query / limit 越界 / 非法 mode 均 400（DTO 边界——处理器
    将 RequestValidationError 统一映射 BAD_REQUEST，与既有端点一致）。"""
    empty_query = await client.post("/api/v1/memories/search", json=_search_payload(query=""))
    assert empty_query.status_code == 400

    limit_over = await client.post("/api/v1/memories/search", json=_search_payload(limit=51))
    assert limit_over.status_code == 400

    limit_zero = await client.post("/api/v1/memories/search", json=_search_payload(limit=0))
    assert limit_zero.status_code == 400

    bad_mode = await client.post("/api/v1/memories/search", json=_search_payload(mode="semantic"))
    assert bad_mode.status_code == 400


async def test_search_embed_failure_maps_to_error(
    client: AsyncClient, search_app: FastAPI, active_persona_id: int
) -> None:
    """嵌入失败透传：embed 角色未绑定 → MODEL_NOT_FOUND（404 错误码面）。"""
    search_app.state.gateway = StubGateway(fail=True)
    response = await client.post("/api/v1/memories/search", json=_search_payload(mode="dense"))
    assert response.status_code == 404
    assert response.json()["code"] == "MODEL_NOT_FOUND"


# ---------------------------------------------------------------------------
# 处理器直调契约面
# ---------------------------------------------------------------------------


async def test_search_handler_direct_contract(
    search_app: FastAPI, db_session: AsyncSession, active_persona_id: int
) -> None:
    """处理器直调：ResultDTO 包装契约 + persona 谓词透传（keyword 路径）。"""
    await _index_entries(active_persona_id)
    request = Request(scope={"type": "http", "app": search_app})

    from packages.harness.memory.retrieval_models import MemorySearchRequestDTO

    req = MemorySearchRequestDTO.model_validate(
        {"query": "小鱼干", "mode": "keyword", "persona_id": active_persona_id}
    )
    result = await search_memories(req, request, db=db_session)

    assert result.success is True
    assert result.code == "200"
    assert len(result.data or []) == 2
    assert (result.data or [])[0].ref_id == 11
