"""app/api/memories 梦境端点测试——trigger / runs / run detail（P1 chunk 3）。

测试策略：本地装配 FastAPI app（memories router + 共享异常处理器——同
test_memories_api.py 模式；DB 经共享会话级 SQLite fixture 外部安装；网关
挂桩 app.state.gateway）。覆盖面：触发 happy path（合并 + 日记落库）/
cfg 参数覆写（窗口）/ 失败运行仍 200 返回记录（业务结果语义）/ 运行
日志分页 + 详情 / 人格解析（缺省激活 / 无激活 404 / 他人格隔离 404）/
处理器直调契约面。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.dreaming_orm as dreaming_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.memories import router as memories_router
from app.api.memories import trigger_dream
from app.middleware.error_handler import register_exception_handlers
from packages.harness.core.time_utils import now_ms
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.dtos import CuratedQueryDTO, EpisodeCreateDTO, EpisodeQueryDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    EpisodeKind,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context

memory_service = MemoryService()
persona_service = PersonaService()


class StubDreamGateway:
    """桩网关（chat_by_role 排队响应——dream 角色面，零真实网络）。

    Attributes:
        calls: 逐次调用捕获（messages / purpose）。
    """

    def __init__(self, responses: list[str]) -> None:
        """初始化（响应按序弹出）。"""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self,
        role: Any,
        messages: Any,
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """按角色调用（捕获参数；弹出下一个排队响应）。"""
        self.calls.append({"messages": list(messages), "purpose": purpose})
        if not self._responses:
            raise AssertionError("桩网关响应队列耗尽")
        return ChatResponseModel(content=self._responses.pop(0))


async def _create_persona(db_session: AsyncSession, name: str, *, active: bool = False) -> int:
    """经真实 PersonaService 创建人格（可选激活）。"""
    created = await persona_service.create(
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


async def _append_episode(
    db_session: AsyncSession, persona_id: int, user: str, *, ts_ms: int | None = None
) -> None:
    """在目标人格作用域内追加 chat 事件（触发铺底）。"""
    async with bind_persona_context(persona_id):
        await memory_service.append_episode(
            db_session,
            EpisodeCreateDTO(
                kind=EpisodeKind.CHAT,
                source="chat",
                content=ChatTurnPayloadModel(user_content=user, assistant_content="好呀"),
                importance=6,
                ts_ms=ts_ms,
            ),
        )


def _merge_response(proposal_refs: list[int]) -> str:
    """构造合并响应（fenced JSON 数组——引用给定事件 id）。"""
    proposals = [
        {
            "topic": "口味偏好",
            "kind": "memory",
            "content": "主人爱小鱼干和虾干",
            "importance": 6,
            "merged_from": proposal_refs,
        }
    ]
    return f"```json\n{json.dumps(proposals, ensure_ascii=False)}\n```"


@pytest.fixture
async def dream_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（本路由 + 异常处理器；共享引擎外部安装）。"""
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
async def client(dream_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=dream_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def active_persona_id(client: AsyncClient, db_session: AsyncSession) -> AsyncIterator[int]:
    """铺底一个激活人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "dream-active", active=True)
    await persona_service.activate(db_session, persona_id)
    yield persona_id


# ---------------------------------------------------------------------------
# ASGI 面——触发
# ---------------------------------------------------------------------------


async def test_dream_trigger_happy_path(
    dream_app: FastAPI, client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """触发 happy path：200 + success 记录 + 策展晋升 + 日记事件落库。"""
    async with bind_persona_context(active_persona_id):
        episode = await memory_service.append_episode(
            db_session,
            EpisodeCreateDTO(
                kind=EpisodeKind.CHAT,
                source="chat",
                content=ChatTurnPayloadModel(
                    user_content="我今天吃了虾干", assistant_content="好棒"
                ),
                importance=6,
            ),
        )
    dream_app.state.gateway = StubDreamGateway(
        [_merge_response([episode.id]), "今天主人吃到了虾干，很开心。"]
    )

    response = await client.post("/api/v1/memories/dream/trigger", json={})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["status"] == "success"
    assert data["stats"]["candidates"] == 1
    assert data["stats"]["promoted"] == 1
    assert data["diary_episode_id"] is not None
    assert "梦境整理完成" in body["message"]

    async with bind_persona_context(active_persona_id):
        page = await memory_service.query_curated(db_session, CuratedQueryDTO(topic="口味偏好"))
        assert len(page.items) == 1
        assert page.items[0].provenance == "agent"
        diary_page = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(kind=EpisodeKind.DREAM)
        )
        assert len(diary_page.items) == 1
        assert diary_page.items[0].source == "dream:diary"
        assert diary_page.items[0].id == data["diary_episode_id"]


async def test_dream_trigger_cfg_window_override(
    dream_app: FastAPI, client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """cfg 覆写：window_hours=1——窗口外旧事件不参与（零候选零调用）。"""
    current = now_ms()
    await _append_episode(
        db_session, active_persona_id, "两小时前的旧事", ts_ms=current - 2 * 3_600_000
    )
    dream_app.state.gateway = StubDreamGateway([])

    response = await client.post(
        "/api/v1/memories/dream/trigger", json={"cfg": {"window_hours": 1}}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "success"
    assert data["stats"]["candidates"] == 0
    assert data["diary_episode_id"] is None


async def test_dream_trigger_failed_run_returns_record(
    dream_app: FastAPI, client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """失败运行是业务结果：200 + status=failed + error 原因（fail-closed 面）。"""
    await _append_episode(db_session, active_persona_id, "今天发生的事")
    dream_app.state.gateway = StubDreamGateway(["这不是JSON"])

    response = await client.post("/api/v1/memories/dream/trigger", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "failed"
    assert "不是合法 JSON" in body["data"]["error"]
    assert "梦境整理失败" in body["message"]


async def test_dream_trigger_persona_resolution(
    client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """人格解析：缺省激活（404 前置）/ 无激活 404。"""
    await persona_service.deactivate(db_session, active_persona_id)
    no_active = await client.post("/api/v1/memories/dream/trigger", json={})
    assert no_active.status_code == 404
    assert no_active.json()["code"] == "NOT_FOUND"

    missing = await client.post("/api/v1/memories/dream/trigger", json={"persona_id": 9999})
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# ASGI 面——运行日志读面
# ---------------------------------------------------------------------------


async def test_dream_runs_list_and_detail(
    dream_app: FastAPI,
    client: AsyncClient,
    db_session: AsyncSession,
    active_persona_id: int,
) -> None:
    """运行日志：触发两次 → 游标分页 + 详情（决策明细）。"""
    async with bind_persona_context(active_persona_id):
        episode = await memory_service.append_episode(
            db_session,
            EpisodeCreateDTO(
                kind=EpisodeKind.CHAT,
                source="chat",
                content=ChatTurnPayloadModel(user_content="分页测试事件", assistant_content="好"),
                importance=6,
            ),
        )
    dream_app.state.gateway = StubDreamGateway([_merge_response([episode.id]), "第一天的日记。"])
    first = (await client.post("/api/v1/memories/dream/trigger", json={})).json()["data"]
    assert first["status"] == "success"
    # 第二次触发：chat 事件已在首次成功运行晋升 → 门控跳过；但首日的
    # 日记事件（kind=dream，新写入）作为普通候选进入合并（空提案）
    dream_app.state.gateway = StubDreamGateway(["[]", "第二天的日记。"])
    second = (await client.post("/api/v1/memories/dream/trigger", json={})).json()["data"]
    assert second["status"] == "success"
    assert second["stats"]["promoted"] == 0

    listed = await client.get("/api/v1/memories/dream/runs", params={"limit": 1})
    assert listed.status_code == 200
    page = listed.json()["data"]
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == second["id"]  # id 降序——新运行在前
    assert page["next_cursor"]

    next_page = await client.get(
        "/api/v1/memories/dream/runs",
        params={"limit": 10, "cursor": page["next_cursor"]},
    )
    items = next_page.json()["data"]["items"]
    assert [item["id"] for item in items] == [first["id"]]
    assert next_page.json()["data"]["next_cursor"] is None

    detail = await client.get(f"/api/v1/memories/dream/runs/{first['id']}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["id"] == first["id"]
    decisions = detail_data["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["episode_id"] == episode.id
    assert decisions[0]["action"] == "promoted"
    assert decisions[0]["topic"] == "口味偏好"


async def test_dream_run_detail_missing_and_isolation(
    client: AsyncClient, db_session: AsyncSession, active_persona_id: int
) -> None:
    """运行详情：缺失 404 / 他人格隔离 404。"""
    other_id = await _create_persona(db_session, "dream-other")
    missing = await client.get("/api/v1/memories/dream/runs/9999")
    assert missing.status_code == 404

    isolated = await client.get("/api/v1/memories/dream/runs/9999", params={"persona_id": other_id})
    assert isolated.status_code == 404


# ---------------------------------------------------------------------------
# 处理器直调契约面
# ---------------------------------------------------------------------------


async def test_dream_trigger_handler_direct_contract(
    dream_app: FastAPI,
    db_session: AsyncSession,
    active_persona_id: int,
) -> None:
    """处理器直调：ResultDTO 包装契约（触发即返回运行记录）。"""
    await _append_episode(db_session, active_persona_id, "直调契约事件")
    dream_app.state.gateway = StubDreamGateway(["[]", "今天的日记。"])
    request = Request(scope={"type": "http", "app": dream_app})

    from packages.harness.memory.dreaming_models import DreamTriggerRequestDTO

    req = DreamTriggerRequestDTO.model_validate({"persona_id": active_persona_id})
    result = await trigger_dream(req, request, db=db_session)

    assert result.success is True
    assert result.code == "200"
    assert result.data is not None
    assert result.data.status == "success"
    assert result.data.stats.candidates == 1
