"""app/api/voiceprints 路由测试——告知 / 同意 / 注册 / 校验 / 删除（ASGI 零网络）。

测试策略：本地装配 FastAPI app（voiceprints router + 共享异常处理器）；
DB 经共享会话级 SQLite fixture 外部安装到 engine holder；服务实例经
monkeypatch 替换为注配置 / 注 fake 提供方 / tmp 存储的实现。覆盖面：
告知读取 / 无同意 409 / 授予同意 / 批量注册 / 档案读取与列表 / 校验命中 /
撤回清样本 / 删除幂等 / 人格解析 404 / 请求体校验 422。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.orm as persona_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.voice.voiceprint_orm as voiceprint_orm  # noqa: F401  模型注册副作用导入（共享建表）
from app.api import voiceprints as voiceprints_api
from app.api.voiceprints import router as voiceprints_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from packages.harness.voice.voiceprint_models import (
    VOICEPRINT_AUDIO_BASE64_MAX_LENGTH,
    VoiceprintSettings,
)
from packages.harness.voice.voiceprint_service import VoiceprintService


class FakeEmbedding:
    """测试用说话人向量提供方（固定返回 [1, 0]）。"""

    async def embed(self, audio: bytes, filename: str, content_type: str) -> list[float]:
        """返回缺省向量。"""
        return [1.0, 0.0]


def _sample(audio: bytes, *, duration_ms: int = 2000) -> dict[str, Any]:
    """构造注册 / 校验样本 JSON 载荷。"""
    return {
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "content_type": "audio/wav",
        "duration_ms": duration_ms,
        "sample_rate_hz": 16000,
    }


async def _create_persona(db_session: AsyncSession, name: str, *, active: bool = False) -> int:
    """经真实 PersonaService 创建人格（可选启用）。"""
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
async def voiceprint_app(
    shared_db_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    """装配最小 FastAPI app（本路由 + 异常处理器 + 注配置服务实例）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    service = VoiceprintService(
        settings=VoiceprintSettings.model_validate(
            {
                "enabled": True,
                "embedding_base_url": "http://voiceprint.test",
                "min_samples": 2,
                "max_samples": 10,
                "max_batch_samples": 10,
            }
        ),
        embedding=FakeEmbedding(),
        storage_root=tmp_path,
    )
    monkeypatch.setattr(voiceprints_api, "voiceprint_service", service)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(voiceprints_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(voiceprint_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络）。"""
    transport = ASGITransport(app=voiceprint_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def primary_persona_id(
    client: AsyncClient, db_session: AsyncSession
) -> AsyncIterator[int]:
    """铺底一个主人格（返回主键）。"""
    persona_id = await _create_persona(db_session, "vp-primary", active=True)
    await PersonaService().set_primary(db_session, persona_id)
    yield persona_id


async def _data(response: Any) -> Any:
    """断言成功并返回 data 段。"""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    return body["data"]


async def test_notice_endpoint(client: AsyncClient) -> None:
    """告知端点返回 PIPL 义务文本（无需人格上下文）。"""
    data = await _data(await client.get("/api/v1/voiceprints/notice"))
    assert data["consent_text_version"] == "v1"
    assert "撤回权" in data["text"]


async def test_enroll_requires_consent_then_full_flow(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """完整链路：无同意 409 → 授予 → 注册 → 档案 → 列表 → 校验 → 撤回 → 删除。"""
    denied = await client.post(
        "/api/v1/voiceprints/user-1/enroll",
        json={"samples": [_sample(b"a1"), _sample(b"a2")]},
    )
    assert denied.status_code == 409
    assert denied.json()["data"]["reason"] == "consent_required"

    consent = await _data(
        await client.post("/api/v1/voiceprints/user-1/consent", json={})
    )
    assert consent["revoked_at_ms"] is None

    enrolled = await _data(
        await client.post(
            "/api/v1/voiceprints/user-1/enroll",
            json={"samples": [_sample(b"a1"), _sample(b"a2")]},
        )
    )
    assert enrolled["accepted"] == 2
    assert enrolled["profile"]["sample_count"] == 2

    profile = await _data(await client.get("/api/v1/voiceprints/user-1"))
    assert profile["total_duration_ms"] == 4000
    assert profile["consent"]["revoked_at_ms"] is None

    listed = await _data(await client.get("/api/v1/voiceprints"))
    assert [item["sender_id"] for item in listed] == ["user-1"]

    verified = await _data(
        await client.post(
            "/api/v1/voiceprints/user-1/verify", json={"sample": _sample(b"query")}
        )
    )
    assert verified["matched"] is True

    withdraw = await _data(
        await client.post("/api/v1/voiceprints/user-1/consent/withdraw")
    )
    assert withdraw["deleted_samples"] == 2
    assert withdraw["consent"]["revoked_at_ms"] is not None

    deleted = await _data(await client.delete("/api/v1/voiceprints/user-1"))
    assert deleted["deleted_samples"] == 0


async def test_explicit_persona_missing_404(client: AsyncClient) -> None:
    """显式 persona_id 不存在 → 404。"""
    response = await client.get("/api/v1/voiceprints/user-1", params={"persona_id": 9999})
    assert response.status_code == 404


async def test_no_primary_persona_404(client: AsyncClient) -> None:
    """无主人格且未显式指定 → 404。"""
    response = await client.get("/api/v1/voiceprints")
    assert response.status_code == 404


async def test_request_body_validation(
    client: AsyncClient, primary_persona_id: int
) -> None:
    """空样本列表 / 非法版本 → 400 级校验响应。"""
    await _data(await client.post("/api/v1/voiceprints/user-1/consent", json={}))
    empty = await client.post("/api/v1/voiceprints/user-1/enroll", json={"samples": []})
    assert empty.status_code in (400, 422)
    bad_version = await client.post(
        "/api/v1/voiceprints/user-1/consent", json={"consent_text_version": "v0"}
    )
    assert bad_version.status_code == 422


async def test_audio_base64_over_dto_cap_422(client: AsyncClient) -> None:
    """声纹 base64 超 DTO 长度帽：请求体校验层拒绝（共享处理器归一 400，不进服务层）。"""
    payload = {
        "sample": {
            "audio_base64": "A" * (VOICEPRINT_AUDIO_BASE64_MAX_LENGTH + 1),
            "content_type": "audio/wav",
            "duration_ms": 0,
            "sample_rate_hz": 0,
        }
    }
    response = await client.post("/api/v1/voiceprints/user-1/verify", json=payload)
    assert response.status_code == 400
