"""persona 域语音绑定测试——voice_profile 列全链路 round-trip。

覆盖矩阵：

    - 服务面：创建携带绑定 / 缺省 None / 更新替换 / 显式 null 清除 /
      不提供保留原样 / 复制继承；
    - DTO 面：ORM 行 dict → VoiceProfileModel 归一（字段校验）；
    - API 面：personas 路由 create / get / update 的 voice_profile
      JSON 往返（未破坏既有契约——响应含既有字段）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.personas import router as personas_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import (
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.service import PersonaService
from packages.harness.voice.voice_models import VoiceProfileModel


def _create_dto(name: str, profile: dict[str, object] | None = None) -> PersonaCreateDTO:
    """构造创建 DTO（可选语音绑定）。

    Args:
        name: 注册名。
        profile: 语音绑定 JSON（None = 不绑定）。

    Returns:
        :class:`PersonaCreateDTO` 实例。
    """
    payload: dict[str, object] = {
        "name": name,
        "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
    }
    if profile is not None:
        payload["voice_profile"] = profile
    return PersonaCreateDTO.model_validate(payload)


async def _create_persona(
    service: PersonaService, session: AsyncSession, name: str, profile: dict | None = None
) -> int:
    """经服务创建人格并返回主键（铺底辅助）。

    Args:
        service: 人格服务。
        session: 注入的异步 Session。
        name: 注册名。
        profile: 语音绑定 JSON（None = 不绑定）。

    Returns:
        新人格主键。
    """
    created = await service.create(session, _create_dto(name, profile))
    return created.id


class TestServiceRoundTrip:
    async def test_create_with_profile_roundtrip(self, db_session: AsyncSession) -> None:
        """创建携带绑定 → 读取归一为 VoiceProfileModel（全字段）。"""
        service = PersonaService()
        persona_id = await _create_persona(
            service,
            db_session,
            "voice-bound",
            {"speaker": "克隆音色A", "instruct_text": "温柔地说", "speed": 1.5},
        )
        record = await service.get(db_session, persona_id)
        assert record.voice_profile is not None
        assert record.voice_profile.speaker == "克隆音色A"
        assert record.voice_profile.instruct_text == "温柔地说"
        assert record.voice_profile.speed == 1.5

    async def test_create_without_profile_is_none(self, db_session: AsyncSession) -> None:
        """不携带绑定：voice_profile 为 None（既有形态零破坏）。"""
        service = PersonaService()
        persona_id = await _create_persona(service, db_session, "voice-none")
        record = await service.get(db_session, persona_id)
        assert record.voice_profile is None

    async def test_update_replaces_profile(self, db_session: AsyncSession) -> None:
        """更新提供绑定：整体替换（出现即替换语义）。"""
        service = PersonaService()
        persona_id = await _create_persona(
            service, db_session, "voice-replace", {"speaker": "旧音色", "speed": 1.0}
        )
        updated = await service.update(
            db_session,
            persona_id,
            PersonaUpdateDTO.model_validate(
                {"voice_profile": {"speaker": "新音色", "speed": 0.75}}
            ),
        )
        assert updated.voice_profile is not None
        assert updated.voice_profile.speaker == "新音色"
        assert updated.voice_profile.speed == 0.75

    async def test_update_explicit_null_clears_profile(self, db_session: AsyncSession) -> None:
        """更新显式 null：清除绑定（model_fields_set 语义——非「不提供」）。"""
        service = PersonaService()
        persona_id = await _create_persona(
            service, db_session, "voice-clear", {"speaker": "旧音色"}
        )
        updated = await service.update(
            db_session,
            persona_id,
            PersonaUpdateDTO.model_validate({"voice_profile": None}),
        )
        assert updated.voice_profile is None

    async def test_update_without_profile_keeps_existing(
        self, db_session: AsyncSession
    ) -> None:
        """更新不含 voice_profile 字段：绑定原样保留（部分更新语义）。"""
        service = PersonaService()
        persona_id = await _create_persona(
            service, db_session, "voice-keep", {"speaker": "保留音色"}
        )
        updated = await service.update(
            db_session,
            persona_id,
            PersonaUpdateDTO.model_validate({"display_name": "改名不影响绑定"}),
        )
        assert updated.display_name == "改名不影响绑定"
        assert updated.voice_profile is not None
        assert updated.voice_profile.speaker == "保留音色"

    async def test_copy_inherits_profile(self, db_session: AsyncSession) -> None:
        """复制人格：语音绑定随卡片一并继承。"""
        service = PersonaService()
        persona_id = await _create_persona(
            service, db_session, "voice-copy-src", {"speaker": "遗传音色", "speed": 1.5}
        )
        copied = await service.copy(
            db_session, persona_id, PersonaCopyDTO(new_name="voice-copy-dst")
        )
        assert copied.voice_profile is not None
        assert copied.voice_profile.speaker == "遗传音色"
        assert copied.voice_profile.speed == 1.5

    async def test_profile_model_validates_bounds(self) -> None:
        """绑定模型边界：speaker 非空 / 语速 0.5-2.0（DTO 边界拒绝）。"""
        with pytest.raises(ValueError):
            VoiceProfileModel(speaker="", speed=1.0)
        with pytest.raises(ValueError):
            VoiceProfileModel(speaker="x", speed=3.0)


class TestPersonaApiVoiceProfile:
    """personas 路由的 voice_profile JSON 往返（HTTP 面零破坏实证）。"""

    @pytest.fixture
    async def persona_app(self, shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
        """本地装配的最小 FastAPI app（personas 路由 + 异常处理器）。"""
        db_engine_holder.async_engine = shared_db_engine
        db_engine_holder.session_factory = async_sessionmaker(
            bind=shared_db_engine, expire_on_commit=False
        )
        db_engine_holder.owner_token = None
        app = FastAPI(docs_url=None, redoc_url=None)
        app.include_router(personas_router)
        register_exception_handlers(app)
        yield app
        db_engine_holder.reset()

    @pytest.fixture
    async def client(self, persona_app: FastAPI) -> AsyncIterator[AsyncClient]:
        """ASGITransport 客户端（零真实网络）。"""
        transport = ASGITransport(app=persona_app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client

    async def test_api_create_get_roundtrip(self, client: AsyncClient) -> None:
        """API 往返：create 携带绑定 → get 归一返回（含既有字段）。"""
        create_response = await client.post(
            "/api/v1/personas",
            json={
                "name": "api-voice",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
                "voice_profile": {"speaker": "API音色", "instruct_text": "慢一点", "speed": 1.25},
            },
        )
        assert create_response.status_code == 200
        data = create_response.json()["data"]
        assert data["voice_profile"] == {
            "speaker": "API音色",
            "instruct_text": "慢一点",
            "speed": 1.25,
        }
        # 既有字段零破坏
        assert data["name"] == "api-voice"
        assert data["card"]["name"] == "大肥鱼"

        get_response = await client.get(f"/api/v1/personas/{data['id']}")
        assert get_response.status_code == 200
        assert get_response.json()["data"]["voice_profile"]["speaker"] == "API音色"

    async def test_api_create_without_profile_null(self, client: AsyncClient) -> None:
        """API 创建不携带：响应 voice_profile 为 null。"""
        response = await client.post(
            "/api/v1/personas",
            json={
                "name": "api-voice-none",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["voice_profile"] is None

    async def test_api_update_set_and_clear(self, client: AsyncClient) -> None:
        """API 更新：设置绑定 → 清除绑定（显式 null）。"""
        created = (
            await client.post(
                "/api/v1/personas",
                json={
                    "name": "api-voice-upd",
                    "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
                },
            )
        ).json()["data"]
        persona_id = created["id"]

        set_response = await client.put(
            f"/api/v1/personas/{persona_id}",
            json={"voice_profile": {"speaker": "更新音色"}},
        )
        assert set_response.status_code == 200
        assert set_response.json()["data"]["voice_profile"]["speaker"] == "更新音色"

        clear_response = await client.put(
            f"/api/v1/personas/{persona_id}", json={"voice_profile": None}
        )
        assert clear_response.status_code == 200
        assert clear_response.json()["data"]["voice_profile"] is None

    async def test_api_invalid_profile_rejected_400(self, client: AsyncClient) -> None:
        """API 非法绑定（语速越界 / speaker 空）：请求校验 400。"""
        response = await client.post(
            "/api/v1/personas",
            json={
                "name": "api-voice-bad",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
                "voice_profile": {"speaker": "x", "speed": 9.9},
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"
