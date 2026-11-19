"""app/api/memories 消息变体端点测试（ASGITransport——swipes 面）。

覆盖：POST /sessions/{id}/messages/{mid}/variants（数量校验 / 落库 / 详情
嵌套读回）/ POST .../select-variant（正典替换 + 选中注记）/ GET 详情的
include_variants 语义 / 缺失会话 / 非 assistant 父回合 / 数量越界。
零真实网络（ASGITransport + 共享 SQLite + 脚本化假网关）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api.memories import router as memories_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatMessageRole, ChatResponseModel
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context


class ScriptedGateway:
    """脚本化假网关（顺序回放回复原文——swipes 生成面）。"""

    def __init__(self, contents: Sequence[str]) -> None:
        """初始化假网关。

        Args:
            contents: 逐次调用返回的原文序列（耗尽后重复最后一条）。
        """
        self._contents = list(contents)
        self.chat_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录组装消息；按序返回脚本原文）。"""
        index = min(len(self.chat_calls), len(self._contents) - 1)
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        return ChatResponseModel(content=self._contents[index])


@dataclass
class SwipeApiEnv:
    """变体端点测试环境（app / 客户端 / 会话脊柱铺设结果）。"""

    client: AsyncClient
    app: FastAPI
    db_session: AsyncSession
    persona_id: int
    session_id: int
    user_message_id: int
    parent_message_id: int
    gateway: ScriptedGateway = field(default_factory=lambda: ScriptedGateway(["候选一"]))


@pytest.fixture
async def swipe_api_env(shared_db_engine: AsyncEngine) -> AsyncIterator[SwipeApiEnv]:
    """铺底人格 / 会话脊柱 / ASGI 客户端（holder 用后复位）。

    Yields:
        :class:`SwipeApiEnv`（父回合为 assistant 正典行；用户回合用于
        非 assistant 父回合校验）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(memories_router)
    register_exception_handlers(app)
    gateway = ScriptedGateway(
        [
            "<心声>换种关心方式。</心声><情绪>affection:+0.3</情绪>候选一",
            "候选二",
            "候选三",
            "候选四",
        ]
    )
    app.state.gateway = gateway
    async with factory() as db_session:
        persona = await PersonaService().create(
            db_session,
            PersonaCreateDTO.model_validate(
                {
                    "name": "swipe-api-persona",
                    "status": "active",
                    "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
                }
            ),
        )
        await PersonaService().set_primary(db_session, persona.id)
        service = ChatSessionService()
        async with bind_persona_context(persona.id):
            summary = await service.create_session(db_session, title="API 变体会话")
            user_message = await service.append_message(
                db_session, summary.id, role=ChatMessageRole.USER, content="第一句"
            )
            parent = await service.append_message(
                db_session, summary.id, role=ChatMessageRole.ASSISTANT, content="父回合原文"
            )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield SwipeApiEnv(
                client=client,
                app=app,
                db_session=db_session,
                persona_id=persona.id,
                session_id=summary.id,
                user_message_id=user_message.id,
                parent_message_id=parent.id,
                gateway=gateway,
            )
    db_engine_holder.reset()


async def _create_variants(env: SwipeApiEnv, count: int = 2) -> list[dict[str, Any]]:
    """经端点生成变体并返回 data 段（铺底辅助）。"""
    response = await env.client.post(
        f"/api/v1/memories/sessions/{env.session_id}/messages/{env.parent_message_id}/variants",
        json={"count": count},
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    data: list[dict[str, Any]] = body["data"]
    return data


async def test_regenerate_and_detail_include_variants(swipe_api_env: SwipeApiEnv) -> None:
    """重生成端点：候选落库 + include_variants 详情嵌套读回 + 默认排除。"""
    created = await _create_variants(swipe_api_env, count=2)
    assert [item["content"] for item in created] == ["候选一", "候选二"]
    assert all(item["seq"] == 2 for item in created)
    assert created[0]["meta"]["variant_index"] == 1
    assert created[0]["meta"]["thought"] == "换种关心方式。"
    assert created[0]["meta"]["emotion_deltas"] == {"affection": 0.3}

    default_detail = await swipe_api_env.client.get(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}"
    )
    assert default_detail.status_code == 200
    default_messages = default_detail.json()["data"]["messages"]
    assert [message["content"] for message in default_messages] == ["第一句", "父回合原文"]
    assert all(message["variants"] == [] for message in default_messages)

    nested = await swipe_api_env.client.get(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}",
        params={"include_variants": "true"},
    )
    assert nested.status_code == 200
    messages = nested.json()["data"]["messages"]
    assert [variant["content"] for variant in messages[1]["variants"]] == ["候选一", "候选二"]
    assert messages[0]["variants"] == []

    # 网关调用面：正典脊柱组装（[system] + 末位 user 回合包裹），用途 / persona 直传。
    assert swipe_api_env.gateway.chat_calls[0]["purpose"] == "chat"
    assert swipe_api_env.gateway.chat_calls[0]["persona_id"] == str(swipe_api_env.persona_id)
    sent_messages = swipe_api_env.gateway.chat_calls[0]["messages"][1:]
    assert len(sent_messages) == 1
    assert sent_messages[0].content.startswith("[[[内容开始 ")
    assert "第一句" in sent_messages[0].content


async def test_select_variant_endpoint_promotes(swipe_api_env: SwipeApiEnv) -> None:
    """选定端点：正典内容替换 + 选中注记 + 变体仍可读回。"""
    created = await _create_variants(swipe_api_env, count=2)
    response = await swipe_api_env.client.post(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}"
        f"/messages/{swipe_api_env.parent_message_id}/select-variant",
        json={"variant_id": created[1]["id"]},
    )
    assert response.status_code == 200
    updated: dict[str, Any] = response.json()["data"]
    assert updated["content"] == "候选二"
    assert updated["meta"]["selected_variant_id"] == created[1]["id"]

    detail = await swipe_api_env.client.get(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}",
        params={"include_variants": "true"},
    )
    messages = detail.json()["data"]["messages"]
    assert messages[1]["content"] == "候选二"
    assert len(messages[1]["variants"]) == 2


async def test_regenerate_rejects_user_parent_and_bad_count(swipe_api_env: SwipeApiEnv) -> None:
    """父回合非 assistant → 422（业务校验）；数量越界 → 400（请求体校验）。"""
    bad_parent = await swipe_api_env.client.post(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}"
        f"/messages/{swipe_api_env.user_message_id}/variants",
        json={"count": 1},
    )
    assert bad_parent.status_code == 422
    assert bad_parent.json()["code"] == "VALIDATION_ERROR"

    bad_count = await swipe_api_env.client.post(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}"
        f"/messages/{swipe_api_env.parent_message_id}/variants",
        json={"count": 9},
    )
    assert bad_count.status_code == 400


async def test_variant_endpoints_missing_session(swipe_api_env: SwipeApiEnv) -> None:
    """缺失会话 / 父回合：404。"""
    missing_session = await swipe_api_env.client.post(
        "/api/v1/memories/sessions/999/messages/1/variants",
        json={"count": 1},
    )
    assert missing_session.status_code == 404
    assert missing_session.json()["code"] == "NOT_FOUND"

    missing_message = await swipe_api_env.client.post(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}/messages/999/variants",
        json={"count": 1},
    )
    assert missing_message.status_code == 404


async def test_select_variant_missing_variant(swipe_api_env: SwipeApiEnv) -> None:
    """选定缺失变体：404。"""
    response = await swipe_api_env.client.post(
        f"/api/v1/memories/sessions/{swipe_api_env.session_id}"
        f"/messages/{swipe_api_env.parent_message_id}/select-variant",
        json={"variant_id": 999},
    )
    assert response.status_code == 404
