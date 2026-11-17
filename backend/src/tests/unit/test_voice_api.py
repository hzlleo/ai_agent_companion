"""app/api/voice 路由测试——TTS / ASR 端点（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（voice 路由 + 共享异常处理器；共享引擎
外部安装——get_async_db 依赖自然生效）；服务层经 monkeypatch 替换模块级
``voice_service``（fake 记录调用 / 真实服务承载 disabled 门控面）。

覆盖面：TTS 字节响应契约（audio/wav + X-Latency-Ms 头）+ 人格解析三分支
（显式 / 激活缺省 / 未绑定）+ 错误映射（404 / 409 / 400 / 503 ResultDTO
形态）；ASR multipart 解析（file / language 字段 + 字节载荷）+ 形态错误
400 + 上传上限字节精确边界（恰等于上限通过 / 超一字节 422——经
``app.state.settings`` 注入小上限隔离驱动）+ disabled 409；另设处理器
直调契约面（handler 作为普通 async 可调用直接调用——Response /
ResultDTO 包装契约）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import Request

import app.api.voice as voice_api
from app.api.voice import router as voice_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from packages.harness.voice.voice_models import (
    AsrResultModel,
    TtsResultModel,
    VoiceTtsRequestDTO,
)
from packages.harness.voice.voice_service import VoiceService

#: 假合成产物（字节面——断言响应体原样透传）。
_FAKE_WAV: bytes = b"RIFF-fake-wav"


class FakeVoiceService:
    """假语音服务（记录调用；可注入合成 / 转写故障）。"""

    def __init__(
        self,
        *,
        tts_error: BusinessException | None = None,
        asr_error: BusinessException | None = None,
    ) -> None:
        """初始化假服务。

        Args:
            tts_error: 合成故障注入。
            asr_error: 转写故障注入。
        """
        self.tts_error = tts_error
        self.asr_error = asr_error
        self.tts_calls: list[dict[str, Any]] = []
        self.asr_calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        *,
        persona: Any = None,
        speaker: str | None = None,
        speed: float | None = None,
        request_id: str | None = None,
    ) -> TtsResultModel:
        """假合成（记录绑定人格与参数；返回固定产物 / 注入故障）。"""
        self.tts_calls.append(
            {
                "text": text,
                "persona": persona,
                "speaker": speaker,
                "speed": speed,
            }
        )
        if self.tts_error is not None:
            raise self.tts_error
        return TtsResultModel(audio=_FAKE_WAV, latency_ms=12)

    async def transcribe(
        self,
        audio: bytes,
        filename: str,
        *,
        persona: Any = None,
        language: str | None = None,
        request_id: str | None = None,
    ) -> AsrResultModel:
        """假转写（记录载荷与参数；返回固定产物 / 注入故障）。"""
        self.asr_calls.append(
            {"audio": audio, "filename": filename, "persona": persona, "language": language}
        )
        if self.asr_error is not None:
            raise self.asr_error
        return AsrResultModel(text="你好世界", duration_ms=3400, language="zh")


def _disabled_service() -> VoiceService:
    """双关闭形态的真实服务（disabled 门控直测——零池依赖）。"""
    return VoiceService(
        settings=build_isolated_settings(
            voice=VoiceSettings(tts=TtsSettings(enabled=False), asr=AsrSettings(enabled=False))
        )
    )


async def _seed_persona(
    session: AsyncSession, name: str, *, set_primary: bool = False
) -> int:
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


def _multipart_request(
    files: dict[str, Any], data: dict[str, Any] | None = None
) -> Request:
    """构造携带 multipart 请求体的 starlette Request（处理器直调面）。

    Args:
        files: httpx files 形态（字段名 → (文件名, 字节[, 内容类型])）。
        data: 附加表单字段。

    Returns:
        装载了完整 multipart 请求体的 :class:`Request`。
    """
    built = httpx.Request("POST", "http://test/asr", files=files, data=data)
    raw = built.read()
    content_type = built.headers["content-type"]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/asr",
        "query_string": b"",
        "headers": [(b"content-type", content_type.encode("latin-1"))],
    }

    async def receive() -> dict[str, Any]:
        """一次性下发完整请求体。"""
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive=receive)


@pytest.fixture
async def voice_app(shared_db_engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """本地装配的最小 FastAPI app（voice 路由 + 异常处理器；共享引擎外部安装）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(voice_router)
    register_exception_handlers(app)
    yield app
    db_engine_holder.reset()


@pytest.fixture
async def client(voice_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=voice_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# TTS 端点（HTTP 面）
# ---------------------------------------------------------------------------


class TestTtsEndpoint:
    async def test_success_bytes_response_contract(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功契约：200 + audio/wav 字节体 + X-Latency-Ms 头（非 JSON 包裹）。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post("/api/v1/voice/tts", json={"text": "你好"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["x-latency-ms"] == "12"
        assert response.content == _FAKE_WAV

    async def test_persona_resolution_active_default(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """人格解析缺省分支：persona_id 未提供 → 当前激活人格绑定。"""
        persona_id = await _seed_persona(db_session, "voice-active", set_primary=True)
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post("/api/v1/voice/tts", json={"text": "你好"})
        assert response.status_code == 200
        assert len(fake.tts_calls) == 1
        bound = fake.tts_calls[0]["persona"]
        assert bound is not None
        assert bound.id == persona_id

    async def test_persona_resolution_explicit_id(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """人格解析显式分支：persona_id 给定 → 该人格（未激活也可绑定）。"""
        persona_id = await _seed_persona(db_session, "voice-explicit")
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post(
            "/api/v1/voice/tts", json={"text": "你好", "persona_id": persona_id}
        )
        assert response.status_code == 200
        bound = fake.tts_calls[0]["persona"]
        assert bound is not None
        assert bound.id == persona_id

    async def test_persona_resolution_unbound_when_none(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """人格解析空态分支：无人格（无激活 + 未提供）→ 未绑定合成。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post("/api/v1/voice/tts", json={"text": "你好"})
        assert response.status_code == 200
        assert fake.tts_calls[0]["persona"] is None

    async def test_missing_persona_404(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 persona_id 不存在：404 NOT_FOUND（ResultDTO 形态）。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post(
            "/api/v1/voice/tts", json={"text": "你好", "persona_id": 999}
        )
        assert response.status_code == 404
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "NOT_FOUND"
        assert fake.tts_calls == []

    async def test_disabled_tts_409(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TTS 未启用：409 CONFLICT（真实服务门控 + 统一异常处理器）。"""
        monkeypatch.setattr(voice_api, "voice_service", _disabled_service())
        response = await client.post("/api/v1/voice/tts", json={"text": "你好"})
        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "CONFLICT"

    async def test_validation_400_text_over_limit(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """请求校验：文本超 500 字符 → 400（VALIDATION_ERROR ResultDTO）。"""
        monkeypatch.setattr(voice_api, "voice_service", FakeVoiceService())
        response = await client.post("/api/v1/voice/tts", json={"text": "长" * 501})
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    async def test_validation_400_speed_out_of_range(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """请求校验：语速越界 → 400。"""
        monkeypatch.setattr(voice_api, "voice_service", FakeVoiceService())
        response = await client.post(
            "/api/v1/voice/tts", json={"text": "你好", "speed": 3.0}
        )
        assert response.status_code == 400

    async def test_service_call_failed_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """合成服务失败：503 LLM_CALL_FAILED（ResultDTO error 形态）。"""
        failure = BusinessException(
            code=ErrorCode.LLM_CALL_FAILED, message="语音合成失败: 服务不可达"
        )
        monkeypatch.setattr(
            voice_api, "voice_service", FakeVoiceService(tts_error=failure)
        )
        response = await client.post("/api/v1/voice/tts", json={"text": "你好"})
        assert response.status_code == 503
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "LLM_CALL_FAILED"


# ---------------------------------------------------------------------------
# ASR 端点（HTTP 面——multipart）
# ---------------------------------------------------------------------------


class TestAsrEndpoint:
    async def test_success_multipart_roundtrip(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功契约：multipart file 上传 → ResultDTO {text, duration_ms}。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        audio = b"RIFF....upload-bytes"
        response = await client.post(
            "/api/v1/voice/asr",
            files={"file": ("memo.wav", audio, "audio/wav")},
            data={"language": "zh"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["text"] == "你好世界"
        assert body["data"]["duration_ms"] == 3400
        assert body["data"]["language"] == "zh"
        assert fake.asr_calls == [
            {
                "audio": audio,
                "filename": "memo.wav",
                "persona": None,
                "language": "zh",
            }
        ]

    async def test_multipart_without_language_defaults_none(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """language 字段缺省：服务收到 None（settings 缺省由服务层解析）。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        await client.post(
            "/api/v1/voice/asr", files={"file": ("memo.wav", b"audio", "audio/wav")}
        )
        assert fake.asr_calls[0]["language"] is None

    async def test_multipart_empty_language_normalized_none(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """language 空串字段：归一 None（不作为空提示透传）。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        await client.post(
            "/api/v1/voice/asr",
            files={"file": ("memo.wav", b"audio", "audio/wav")},
            data={"language": "  "},
        )
        assert fake.asr_calls[0]["language"] is None

    async def test_missing_file_field_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """multipart 缺 file 字段（仅其他字段）：BAD_REQUEST 400。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await client.post(
            "/api/v1/voice/asr", files={"notfile": ("x.txt", b"hi", "text/plain")}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert fake.asr_calls == []

    async def test_non_multipart_body_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 multipart 请求体（JSON）：BAD_REQUEST 400。"""
        monkeypatch.setattr(voice_api, "voice_service", FakeVoiceService())
        response = await client.post("/api/v1/voice/asr", json={"file": "x"})
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    async def test_empty_body_400(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """multipart 声明 + 空请求体：BAD_REQUEST 400。"""
        monkeypatch.setattr(voice_api, "voice_service", FakeVoiceService())
        response = await client.post(
            "/api/v1/voice/asr",
            content=b"",
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert response.status_code == 400

    async def test_disabled_asr_409(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASR 未启用：409 CONFLICT（真实服务门控）。"""
        monkeypatch.setattr(voice_api, "voice_service", _disabled_service())
        response = await client.post(
            "/api/v1/voice/asr", files={"file": ("memo.wav", b"audio", "audio/wav")}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "CONFLICT"

    async def test_upload_size_limit_byte_exact(
        self, voice_app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上传上限字节精确边界：请求体恰等于上限通过，超一字节 422。

        上限经 ``app.state.settings`` 注入小值隔离驱动（不依赖全局
        env 面）；请求体为手工拼装的 multipart envelope——总字节数
        可精确控制（httpx files= 形态无法保证 envelope 字节数）。
        """
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        limit = 1024
        voice_app.state.settings = build_isolated_settings(
            voice=VoiceSettings(asr=AsrSettings(max_upload_bytes=limit))
        )
        boundary = "byte-exact-boundary"
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="memo.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
        audio_ok = b"a" * (limit - len(prefix) - len(suffix))
        assert len(prefix) + len(audio_ok) + len(suffix) == limit
        response = await client.post(
            "/api/v1/voice/asr", content=prefix + audio_ok + suffix, headers=headers
        )
        assert response.status_code == 200
        assert fake.asr_calls[0]["audio"] == audio_ok
        audio_over = b"a" * (limit - len(prefix) - len(suffix) + 1)
        response = await client.post(
            "/api/v1/voice/asr", content=prefix + audio_over + suffix, headers=headers
        )
        assert response.status_code == 422
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "VALIDATION_ERROR"
        assert body["data"]["max_upload_bytes"] == limit
        assert body["data"]["size_bytes"] == limit + 1
        assert len(fake.asr_calls) == 1


# ---------------------------------------------------------------------------
# 处理器直调契约面——handler 函数作为普通 async 可调用直接调用
# ---------------------------------------------------------------------------


class TestDirectHandlerContract:
    async def test_tts_handler_response_contract(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TTS 直调：Response 字节契约（media_type / 头 / 体）。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        response = await voice_api.synthesize_speech(
            VoiceTtsRequestDTO(text="直调", persona_id=None, speaker=None, speed=None),
            db_session,
        )
        assert response.status_code == 200
        assert response.media_type == "audio/wav"
        assert response.headers["x-latency-ms"] == "12"
        assert response.body == _FAKE_WAV

    async def test_asr_handler_result_contract(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASR 直调：multipart Request → ResultDTO 包装契约。"""
        fake = FakeVoiceService()
        monkeypatch.setattr(voice_api, "voice_service", fake)
        audio = b"RIFF....direct-bytes"
        request = _multipart_request(
            {"file": ("direct.wav", audio, "audio/wav")}, {"language": "en"}
        )
        result = await voice_api.transcribe_audio(request, db_session)
        assert result.success is True
        assert result.code == "200"
        assert result.message == "语音识别完成"
        assert result.data is not None
        assert result.data.text == "你好世界"
        assert fake.asr_calls[0]["audio"] == audio
        assert fake.asr_calls[0]["filename"] == "direct.wav"
        assert fake.asr_calls[0]["language"] == "en"

    async def test_asr_direct_malformed_raises_bad_request(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASR 直调：非 multipart Request → BAD_REQUEST 业务异常。"""
        monkeypatch.setattr(voice_api, "voice_service", FakeVoiceService())
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/asr",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }

        async def receive() -> dict[str, Any]:
            """下发 JSON 体。"""
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with pytest.raises(BusinessException) as exc_info:
            await voice_api.transcribe_audio(Request(scope, receive=receive), db_session)
        assert exc_info.value.code == ErrorCode.BAD_REQUEST
