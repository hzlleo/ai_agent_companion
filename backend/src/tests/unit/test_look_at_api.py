"""app/api/look_at 路由测试——multipart 上传 / MIME 解析序 / 校验面（ASGITransport
零真实网络）。

测试策略：与 test_voice_api.py 同构——本地装配 FastAPI app（look_at 路由 +
共享异常处理器；共享引擎外部安装）；服务层经 monkeypatch 替换模块级
``LookAtService`` 构造面注入 fake（记录调用）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.api.look_at as look_at_api
from app.api.look_at import router as look_at_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.multimodal.mm_models import (
    LOOK_AT_IMAGE_MAX_BYTES,
    LookAtResultModel,
)
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService


class FakeLookAtService:
    """假 look_at 服务（describe 调用捕获；可注入故障）。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化。

        Args:
            error: describe 故障注入（None = 正常返回固定结果）。
        """
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def describe(
        self,
        image: bytes,
        mime_type: str,
        question: str | None = None,
        *,
        persona_id: str | None = None,
        request_id: str | None = None,
    ) -> LookAtResultModel:
        """假理解（记录载荷与参数；返回固定结果 / 注入故障）。"""
        self.calls.append(
            {
                "image": image,
                "mime_type": mime_type,
                "question": question,
                "persona_id": persona_id,
            }
        )
        if self.error is not None:
            raise self.error
        return LookAtResultModel(
            description="图中是一只橘猫", model_entry="local-vision", latency_ms=30
        )


def _fake_service_factory(fake: FakeLookAtService) -> Callable[..., FakeLookAtService]:
    """构造假服务工厂（替换模块级 LookAtService 构造面——返回同一 fake）。"""

    def _factory(*args: Any, **kwargs: Any) -> FakeLookAtService:
        """忽略构造参数，恒返回注入的 fake 实例。"""
        return fake

    return _factory


async def _seed_persona(session: AsyncSession, name: str, *, set_primary: bool = False) -> int:
    """铺底人格（经服务创建；可选设为主人格）。

    Args:
        session: 注入的异步 Session。
        name: 注册名。
        set_primary: 是否设为主人格（主人格标记位）。

    Returns:
        新人格主键。
    """
    service = PersonaService()
    payload: dict[str, Any] = {
        "name": name,
        "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
    }
    if set_primary:
        payload["status"] = "active"
    created = await service.create(session, PersonaCreateDTO.model_validate(payload))
    if set_primary:
        await service.set_primary(session, created.id)
    return created.id


@pytest.fixture
async def look_at_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（look_at 路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(look_at_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(look_at_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=look_at_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# 上传往返（HTTP 面——multipart）
# ---------------------------------------------------------------------------


class TestLookAtEndpoint:
    async def test_success_multipart_roundtrip(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功契约：multipart image + question → ResultDTO {description, ...}。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        image = b"\x89PNG\r\n\x1a\n upload-bytes"
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("cat.png", image, "image/png")},
            data={"question": "图里有几只猫"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["description"] == "图中是一只橘猫"
        assert body["data"]["model_entry"] == "local-vision"
        assert body["data"]["latency_ms"] == 30
        assert fake.calls == [
            {
                "image": image,
                "mime_type": "image/png",
                "question": "图里有几只猫",
                "persona_id": None,
            }
        ]

    async def test_mime_from_extension_when_declared_generic(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MIME 解析序：声明 octet-stream + 文件名 .jpg → 扩展名兜底 jpeg。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        await client.post(
            "/api/v1/look_at",
            files={"image": ("photo.jpg", b"jpeg-bytes", "application/octet-stream")},
        )
        assert fake.calls[0]["mime_type"] == "image/jpeg"

    async def test_mime_declared_in_allowlist_wins(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MIME 解析序：允许清单内的声明值优先于扩展名。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        await client.post(
            "/api/v1/look_at",
            files={"image": ("photo.png", b"bytes", "image/webp")},
        )
        assert fake.calls[0]["mime_type"] == "image/webp"

    async def test_unknown_mime_rejected_422(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知 MIME（声明与扩展名均未命中）：VALIDATION_ERROR 422。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("memo.txt", b"text-bytes", "text/plain")},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert fake.calls == []

    async def test_question_over_limit_422(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """聚焦问题超 200 字符：VALIDATION_ERROR 422。"""
        monkeypatch.setattr(
            look_at_api, "LookAtService", _fake_service_factory(FakeLookAtService())
        )
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("cat.png", b"bytes", "image/png")},
            data={"question": "问" * 201},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_oversized_image_422(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图像超 10MB：VALIDATION_ERROR 422（系统边界先行拒绝）。"""
        monkeypatch.setattr(
            look_at_api, "LookAtService", _fake_service_factory(FakeLookAtService())
        )
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("big.png", b"x" * (LOOK_AT_IMAGE_MAX_BYTES + 1), "image/png")},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_missing_image_field_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """multipart 缺 image 字段（仅其他字段）：BAD_REQUEST 400。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        response = await client.post(
            "/api/v1/look_at", files={"picture": ("x.png", b"hi", "image/png")}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert fake.calls == []

    async def test_non_multipart_body_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 multipart 请求体（JSON）：BAD_REQUEST 400。"""
        monkeypatch.setattr(
            look_at_api, "LookAtService", _fake_service_factory(FakeLookAtService())
        )
        response = await client.post("/api/v1/look_at", json={"image": "x"})
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    async def test_empty_body_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """multipart 声明 + 空请求体：BAD_REQUEST 400。"""
        monkeypatch.setattr(
            look_at_api, "LookAtService", _fake_service_factory(FakeLookAtService())
        )
        response = await client.post(
            "/api/v1/look_at",
            content=b"",
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert response.status_code == 400

    async def test_service_failure_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务链路失败：503 LLM_CALL_FAILED（ResultDTO error 形态）。"""
        failure = BusinessException(
            code=ErrorCode.LLM_CALL_FAILED, message="look_at 回退链全部条目失败"
        )
        monkeypatch.setattr(
            look_at_api,
            "LookAtService",
            _fake_service_factory(FakeLookAtService(error=failure)),
        )
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("cat.png", b"bytes", "image/png")},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "LLM_CALL_FAILED"

    async def test_active_persona_bound_for_usage(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """人格绑定：当前激活人格的 ID 作为 persona_id 传入（用量归属）。"""
        persona_id = await _seed_persona(db_session, "look-at-active", set_primary=True)
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        response = await client.post(
            "/api/v1/look_at",
            files={"image": ("cat.png", b"bytes", "image/png")},
        )
        assert response.status_code == 200
        assert fake.calls[0]["persona_id"] == str(persona_id)

    async def test_question_field_blank_normalized_none(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """question 空白字段：归一 None（不作为空聚焦透传）。"""
        fake = FakeLookAtService()
        monkeypatch.setattr(look_at_api, "LookAtService", _fake_service_factory(fake))
        await client.post(
            "/api/v1/look_at",
            files={"image": ("cat.png", b"bytes", "image/png")},
            data={"question": "   "},
        )
        assert fake.calls[0]["question"] is None
