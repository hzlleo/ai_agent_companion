"""实时语音 WS 端点测试——路由注册 / 准入拒绝 / 鉴权 / 人格解析 / 端到端。

测试策略（零真实网络）：端点直调 + 假 WebSocket（``receive`` 返回 ASGI
消息字典）——协议语义由会话引擎承担（其端到端用例在 test_realtime_session）；
本文件聚焦 API 层职责：路由挂载、能力门控、token 校验、人格解析、
ChatService / 语音服务装配，以及真实数据库会话落库。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketState

from app.api import realtime_voice
from app.api.realtime_voice import (
    StarletteRealtimeTransport,
    realtime_voice_websocket,
)
from app.main import create_app
from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.dtos import ChatSessionQueryDTO
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.voice.realtime_models import RealtimeVoiceSettings
from tests.realtime_fakes import (
    FakeVoiceService,
    ScriptedChatStream,
    audio_message,
    loud_frame,
    persona_record,
    realtime_settings,
    silence_frame,
    start_message,
)

_WS_PATH = "/api/v1/realtime/voice/ws"
_WAIT_TIMEOUT: float = 5.0


class FakeWebSocket:
    """最小 WS 替身（ASGI 收帧字典 + 文本发送记录 + 关闭记录）。"""

    def __init__(
        self,
        *,
        app: Any,
        query_params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.query_params = query_params or {}
        self.headers = headers or {}
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict[str, Any]] = []
        self.close_action: tuple[int, str] | None = None
        self.accepted = False
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._changed = asyncio.Event()

    async def accept(self) -> None:
        """记录升级完成。"""
        self.accepted = True

    async def send_text(self, payload: str) -> None:
        """记录下行文本帧。"""
        self.sent.append(json.loads(payload))
        self._changed.set()

    async def receive(self) -> dict[str, Any]:
        """返回下一条 ASGI 消息。"""
        return await self._inbound.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """记录关闭动作并置断连态。"""
        if self.close_action is None:
            self.close_action = (code, reason)
        self.client_state = WebSocketState.DISCONNECTED

    async def push(self, message: dict[str, Any]) -> None:
        """投递上行 JSON 消息。"""
        await self._inbound.put({"type": "websocket.receive", "text": json.dumps(message)})

    async def wait_for(
        self, predicate: Any, *, timeout: float = _WAIT_TIMEOUT
    ) -> list[dict[str, Any]]:
        """等待下行记录满足谓词。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate(self.sent):
                return list(self.sent)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"等待下行消息超时: {[m.get('type') for m in self.sent]}")
            self._changed.clear()
            if predicate(self.sent):
                return list(self.sent)
            await asyncio.wait_for(self._changed.wait(), timeout=remaining)

    def types(self) -> list[str]:
        """下行消息类型序列。"""
        return [str(message.get("type")) for message in self.sent]


class _StreamingGateway:
    """最小流式网关替身（ChatService 消费面）。"""

    def __init__(self, text: str = "你好。") -> None:
        self._text = text

    async def stream_by_role(
        self, _role: Any, _messages: Any, _params: Any = None, **_kwargs: Any
    ) -> Any:
        yield _Chunk(content=self._text)

    async def chat_by_role(
        self, _role: Any, _messages: Any, _params: Any = None, **_kwargs: Any
    ) -> Any:
        return _Chunk(content=self._text)


class _Chunk:
    """流式块替身（只带 content 字段）。"""

    def __init__(self, *, content: str) -> None:
        self.content = content


def _settings(
    *,
    realtime: RealtimeVoiceSettings | None = None,
    admin_token_env: str = "ADMIN_AUTH_TOKEN",
) -> Any:
    """构造端点测试用隔离 Settings（默认实时语音启用 + ASR/TTS 开启）。"""
    voice = VoiceSettings(tts=TtsSettings(enabled=True), asr=AsrSettings(enabled=True))
    return build_isolated_settings(
        realtime_voice=realtime if realtime is not None else realtime_settings(),
        voice=voice,
    )


def _ws(
    *,
    settings: Any,
    gateway: Any = None,
    query_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    app: Any = None,
) -> FakeWebSocket:
    """构造带 app.state 的假 WS（app 注入用于共享进程级计数面）。"""
    state = SimpleNamespace(settings=settings, gateway=gateway)
    return FakeWebSocket(
        app=app if app is not None else SimpleNamespace(state=state),
        query_params=query_params,
        headers=headers,
    )


@pytest.fixture
async def bound_db(shared_db_engine: AsyncEngine) -> Any:
    """把共享引擎安装进 holder（会话作用域可用——用后复位）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    yield shared_db_engine
    db_engine_holder.reset()


async def _create_primary_persona(db_session: AsyncSession) -> int:
    """创建并设为主人格（端点人格解析面铺底）。"""
    service = PersonaService()
    created = await service.create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": "realtime-primary",
                "status": "active",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )
    await service.set_primary(db_session, created.id)
    return created.id


def _walk_route_paths(routes: Any) -> list[tuple[str, str]]:
    """递归收集路由路径（框架把 include_router 包装为嵌套 _IncludedRouter）。"""
    collected: list[tuple[str, str]] = []
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            collected.append((type(route).__name__, path))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            collected.extend(_walk_route_paths(inner.routes))
        elif isinstance(getattr(route, "routes", None), list):
            collected.extend(_walk_route_paths(route.routes))
    return collected


async def test_ws_route_is_registered_on_app() -> None:
    """create_app 挂载实时语音 WS 路由（路径与豁免面一致）。"""
    app: FastAPI = create_app(settings=_settings())
    paths = _walk_route_paths(app.routes)
    ws_paths = [path for name, path in paths if name.endswith("WebSocketRoute")]
    http_paths = [path for name, path in paths if name == "APIRoute"]
    assert _WS_PATH in ws_paths
    assert _WS_PATH not in http_paths


async def test_disabled_realtime_rejects_with_conflict() -> None:
    """总闸关闭 → error(CONFLICT) + 1013 关闭（客户端可重试）。"""
    settings = _settings(realtime=realtime_settings(enabled=False))
    websocket = _ws(settings=settings)
    await realtime_voice_websocket(websocket)
    assert websocket.accepted is True
    assert websocket.sent[0]["type"] == "error"
    assert websocket.sent[0]["code"] == "CONFLICT"
    assert websocket.close_action == (1013, "realtime voice disabled")


async def test_unauthorized_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch, bound_db: AsyncEngine
) -> None:
    """owner token 配置时：缺参数 / 错参数 → error(UNAUTHORIZED) + 1008。"""
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "secret-token")
    settings = _settings()
    missing = _ws(settings=settings)
    await realtime_voice_websocket(missing)
    assert missing.sent[0]["code"] == "UNAUTHORIZED"
    assert missing.close_action == (1008, "unauthorized")

    wrong = _ws(settings=settings, query_params={"token": "wrong"})
    await realtime_voice_websocket(wrong)
    assert wrong.sent[0]["code"] == "UNAUTHORIZED"
    assert wrong.close_action == (1008, "unauthorized")


async def test_authorized_token_via_query_reaches_persona_resolution(
    monkeypatch: pytest.MonkeyPatch, bound_db: AsyncEngine
) -> None:
    """查询参数 token 通过鉴权 → 进入人格解析；无主人格 → error(NOT_FOUND)。"""
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "secret-token")
    websocket = _ws(settings=_settings(), query_params={"token": "secret-token"})
    await realtime_voice_websocket(websocket)
    assert websocket.sent[0]["code"] == "NOT_FOUND"
    assert websocket.close_action == (1008, "NOT_FOUND")


async def test_invalid_persona_id_is_rejected(bound_db: AsyncEngine) -> None:
    """显式 persona_id 非整数 → VALIDATION_ERROR；不存在 → NOT_FOUND。"""
    bad = _ws(settings=_settings(), query_params={"persona_id": "abc"})
    await realtime_voice_websocket(bad)
    assert bad.sent[0]["code"] == "VALIDATION_ERROR"
    missing = _ws(settings=_settings(), query_params={"persona_id": "404"})
    await realtime_voice_websocket(missing)
    assert missing.sent[0]["code"] == "NOT_FOUND"


async def test_missing_gateway_is_internal_error(
    bound_db: AsyncEngine, db_session: AsyncSession
) -> None:
    """lifespan 顺序破坏（网关未装配）→ INTERNAL_ERROR + 关闭。"""
    persona_id = await _create_primary_persona(db_session)
    websocket = _ws(settings=_settings(), query_params={"persona_id": str(persona_id)})
    await realtime_voice_websocket(websocket)
    assert websocket.sent[0]["code"] == "INTERNAL_ERROR"
    assert websocket.close_action == (1008, "INTERNAL_ERROR")


async def test_full_endpoint_session_streams_transcript_and_audio(
    bound_db: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：真实人格 / 会话落库 + 假网关 / 假语音 → 转写 / 回复 / 音频 / 关闭。"""
    persona_id = await _create_primary_persona(db_session)
    fake_voice = FakeVoiceService()
    monkeypatch.setattr(realtime_voice, "VoiceService", lambda **_kwargs: fake_voice)
    websocket = _ws(
        settings=_settings(),
        gateway=_StreamingGateway("你好呀，我在。"),
        query_params={"persona_id": str(persona_id)},
    )
    endpoint = asyncio.create_task(realtime_voice_websocket(websocket))
    await websocket.push(start_message(persona_id=persona_id))
    await websocket.wait_for(lambda sent: any(m.get("type") == "ready" for m in sent))
    for index, frame in enumerate([loud_frame(), loud_frame(), silence_frame()]):
        await websocket.push(audio_message(index, frame))
    await websocket.wait_for(
        lambda sent: any(m.get("type") == "audio_chunk" for m in sent)
    )
    await websocket.push({"type": "end", "ts_ms": 0})
    await asyncio.wait_for(endpoint, timeout=_WAIT_TIMEOUT)
    types = websocket.types()
    assert "final_transcript" in types
    assert "assistant_text" in types
    assert "audio_chunk" in types
    assert types[-1] == "closed"
    assert websocket.close_action == (1000, "client_end")
    assert websocket.sent[-1]["reason"] == "client_end"
    # 真实持久化：连接级聊天会话与回合并入库（与文字通道同表）。
    async with bind_persona_context(persona_id):
        sessions = await ChatSessionService().list_sessions(
            db_session, ChatSessionQueryDTO(limit=10)
        )
    assert sessions.items, "实时语音回合应落库为聊天会话"


async def test_transport_adapter_maps_text_binary_and_disconnect() -> None:
    """传输适配：文本直传 / 二进制按 UTF-8 解码 / 断开返回 None / 关闭幂等。"""
    websocket = _ws(settings=_settings())
    transport = StarletteRealtimeTransport(websocket)
    await websocket._inbound.put({"type": "websocket.receive", "text": "{\"a\":1}"})
    assert await transport.receive_text() == '{"a":1}'
    await websocket._inbound.put(
        {"type": "websocket.receive", "bytes": b'{"b":2}'}
    )
    assert await transport.receive_text() == '{"b":2}'
    await websocket._inbound.put({"type": "websocket.disconnect"})
    assert await transport.receive_text() is None
    await transport.close(1000, "done")
    assert websocket.close_action == (1000, "done")
    # 已断连后再次关闭：静默返回（幂等）。
    await transport.close(1001, "again")
    assert websocket.close_action == (1000, "done")


async def test_transport_adapter_ignores_close_runtime_error() -> None:
    """底层已断开时 close 抛 RuntimeError → 适配层吞掉（只影响日志）。"""

    class _RaisingWebSocket(FakeWebSocket):
        async def close(self, code: int = 1000, reason: str = "") -> None:
            raise RuntimeError("closed")

    websocket = _RaisingWebSocket(app=SimpleNamespace(state=SimpleNamespace()))
    transport = StarletteRealtimeTransport(websocket)
    await transport.close(1000, "done")
    assert websocket.client_state is WebSocketState.CONNECTED


async def test_session_cap_rejects_third_and_releases_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发帽：两连通过、第三连 1013、结束释放后新连可用。

    人格 / 聊天装配经 monkeypatch 注入（本用例只验证 API 层准入计数与
    释放面，不触碰共享数据库连接）。
    """
    persona = persona_record()
    chat = ScriptedChatStream()

    async def _fake_resolve(_websocket: Any) -> Any:
        return persona

    async def _fake_build(_websocket: Any, _persona: Any) -> Any:
        return chat

    monkeypatch.setattr(realtime_voice, "_resolve_persona", _fake_resolve)
    monkeypatch.setattr(realtime_voice, "_build_chat", _fake_build)
    monkeypatch.setattr(realtime_voice, "VoiceService", lambda **_kwargs: FakeVoiceService())
    settings = _settings(realtime=realtime_settings(max_sessions=2))
    first = _ws(settings=settings)
    shared_app = first.app
    second = _ws(settings=settings, app=shared_app)
    first_task = asyncio.create_task(realtime_voice_websocket(first))
    second_task = asyncio.create_task(realtime_voice_websocket(second))
    await first.push(start_message(persona_id=persona.id))
    await first.wait_for(lambda sent: any(m.get("type") == "ready" for m in sent))
    await second.push(start_message(persona_id=persona.id))
    await second.wait_for(lambda sent: any(m.get("type") == "ready" for m in sent))
    assert shared_app.state.realtime_voice_active_sessions == 2

    third = _ws(settings=settings, app=shared_app)
    await realtime_voice_websocket(third)
    assert third.sent[-1]["type"] == "error"
    assert third.sent[-1]["code"] == "RATE_LIMITED"
    assert third.close_action == (1013, "realtime voice busy")
    assert shared_app.state.realtime_voice_active_sessions == 2

    await first.push({"type": "end", "ts_ms": 0})
    await asyncio.wait_for(first_task, timeout=_WAIT_TIMEOUT)
    await second.push({"type": "end", "ts_ms": 0})
    await asyncio.wait_for(second_task, timeout=_WAIT_TIMEOUT)
    assert shared_app.state.realtime_voice_active_sessions == 0

    fourth = _ws(settings=settings, app=shared_app)
    fourth_task = asyncio.create_task(realtime_voice_websocket(fourth))
    await fourth.push(start_message(persona_id=persona.id))
    await fourth.wait_for(lambda sent: any(m.get("type") == "ready" for m in sent))
    await fourth.push({"type": "end", "ts_ms": 0})
    await asyncio.wait_for(fourth_task, timeout=_WAIT_TIMEOUT)
    assert shared_app.state.realtime_voice_active_sessions == 0
