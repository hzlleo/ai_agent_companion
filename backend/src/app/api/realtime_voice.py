"""实时语音 WebSocket 端点——只承担 API 层适配（协议语义全在 harness）。

端点契约：

    - 路径 ``/api/v1/realtime/voice/ws``（WS 升级；查询参数 ``persona_id``
      可选——缺省绑定当前激活人格；``token`` 可选——管理面 owner token
      的 WS 传递形态，浏览器 WebSocket 无法设置自定义头，因此走查询参数）；
    - **鉴权**：与 HTTP 管理面同源判定（``REALTIME`` 端点不挂 HTTP 依赖
      ——WS 路由不能复用 ``Request`` 形态依赖；本模块直接消费
      :func:`~app.middleware.admin_auth.verify_admin_token` 恒时比较）；
      失败下行 error(UNAUTHORIZED) 后以 1008 关闭；
    - **能力门控**：``REALTIME_VOICE_ENABLED=false`` 时下行
      error(CONFLICT) 后以 1013（稍后重试）关闭——客户端据此展示配置指引；
    - **并发帽**：``REALTIME_VOICE_MAX_SESSIONS``（默认 2）为进程内并发
      会话上限；超限下行 error(RATE_LIMITED) 后以 1013 拒绝（多个 worker
      部署时各进程独立计数——见 docs/backend/config.md 的 workers 语义）；
    - **人格**：显式 ``persona_id`` 缺失 → 当前激活人格；无主人格 →
      error(NOT_FOUND) + 关闭（会话需要人格正典做 compose 与 TTS 绑定）。

本层职责边界（硬约束：app 只做 API 层）：

    1. 接受 WS 升级 / 解析查询参数 / 鉴权 / 能力门控；
    2. 从 ``app.state`` 取网关与记忆管道装配件，构造连接级 ChatService
       与持久化聊天会话（实时语音回合与文字通道同表落库）；
    3. 把 Starlette WebSocket 适配为 :class:`RealtimeTransportFace`；
    4. 交给 :class:`RealtimeVoiceSession` 驱动，端点自身不含任何协议 /
       VAD / 打断逻辑。
"""

from __future__ import annotations

import contextlib
from typing import cast

from fastapi import APIRouter, WebSocket
from loguru import logger
from starlette.websockets import WebSocketState

from app.middleware.admin_auth import verify_admin_token
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import ChatService
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.database.session import session_scope
from packages.harness.llm.gateway import LlmGateway
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.core_memory import CoreMemoryService
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.facts import FactsService
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills import SkillsService
from packages.harness.voice.realtime_models import RealtimeErrorMessageModel
from packages.harness.voice.realtime_session import (
    ChatSessionStreamAdapter,
    RealtimeVoiceSession,
)
from packages.harness.voice.voice_service import (
    VoiceService,
    wire_default_voice_usage_counter,
    wire_default_voice_usage_recorder,
)

router = APIRouter(prefix="/api/v1/realtime/voice", tags=["realtime-voice"])

#: 鉴权失败关闭码（RFC 6455 policy violation）。
_WS_CLOSE_POLICY_VIOLATION: int = 1008
#: 能力未启用关闭码（RFC 6455 try again later——客户端可重试）。
_WS_CLOSE_TRY_AGAIN: int = 1013
#: 正常收尾关闭码。
_WS_CLOSE_NORMAL: int = 1000


class StarletteRealtimeTransport:
    """Starlette WebSocket → 实时语音传输面适配（仅文本帧 + 关闭）。

    Args:
        websocket: 已 accept 的 WS 连接。
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_text(self, payload: str) -> None:
        """发送文本帧（对端已关闭时由调用方归一为收尾）。

        Args:
            payload: 下行 JSON 字符串。
        """
        await self._websocket.send_text(payload)

    async def receive_text(self) -> str | None:
        """接收文本帧；断开返回 None（二进制帧按 UTF-8 解码透传）。

        Returns:
            上行文本；连接断开返回 None。
        """
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        if text is not None:
            return cast("str", text)
        raw = message.get("bytes")
        if raw is None:
            return None
        return bytes(raw).decode("utf-8", errors="replace")

    async def close(self, code: int, reason: str) -> None:
        """关闭连接（幂等——已关闭时静默返回）。

        Args:
            code: 关闭码。
            reason: 关闭原因。
        """
        if self._websocket.client_state is WebSocketState.DISCONNECTED:
            return
        try:
            await self._websocket.close(code=code, reason=reason)
        except RuntimeError:
            logger.debug("实时语音 WS 关闭时连接已断开（忽略）: code={}", code)


def _resolve_settings(websocket: WebSocket) -> Settings:
    """解析生效 Settings（app.state 优先，缺省回落全局单例）。

    Args:
        websocket: 当前连接（``app.state.settings`` 读取面）。

    Returns:
        生效 Settings。
    """
    app_state = getattr(websocket.app, "state", None)
    injected = cast("Settings | None", getattr(app_state, "settings", None))
    return injected if injected is not None else global_settings


def _authorized(websocket: WebSocket, settings: Settings) -> bool:
    """校验 WS 鉴权（查询参数 token 或 Authorization 头）。

    Args:
        websocket: 当前连接。
        settings: 生效 Settings。

    Returns:
        True 表示通过。
    """
    query_token = websocket.query_params.get("token")
    if query_token is not None:
        return verify_admin_token(query_token.strip(), settings)
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    presented = token.strip() if scheme.lower() == "bearer" and token.strip() else None
    return verify_admin_token(presented, settings)


async def _send_bootstrap_error(websocket: WebSocket, *, code: str, message: str) -> None:
    """在会话引擎建立前下行一条 error（连接级拒绝的最小载荷）。

    Args:
        websocket: 当前连接。
        code: 错误码。
        message: 面向用户的中文提示。
    """
    payload = RealtimeErrorMessageModel(
        ts_ms=now_ms(), code=code, message=message, recoverable=False
    )
    with contextlib.suppress(Exception):
        await websocket.send_text(payload.model_dump_json())


async def _resolve_persona(websocket: WebSocket) -> PersonaRecordDTO:
    """解析连接绑定人格（显式 query 参数优先，缺省当前激活人格）。

    Args:
        websocket: 当前连接（查询参数读取面）。

    Returns:
        人格记录。

    Raises:
        BusinessException: 显式 persona_id 不存在（``NOT_FOUND``）；无当前
            激活人格（``NOT_FOUND``——实时语音需要人格正典）。
    """
    service = PersonaService()
    explicit = websocket.query_params.get("persona_id")
    async with session_scope() as db_session:
        if explicit is not None:
            try:
                persona_id = int(explicit)
            except ValueError as exc:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="persona_id 查询参数必须为整数",
                ) from exc
            return await service.get(db_session, persona_id)
        return await service.get_primary_persona(db_session)


async def _build_chat(websocket: WebSocket, persona: PersonaRecordDTO) -> ChatSessionStreamAdapter:
    """构造连接级 ChatService 与持久化聊天会话（与文字通道路径同构）。

    Args:
        websocket: 当前连接（``app.state`` 网关 / 记忆管道读取面）。
        persona: 绑定人格（会话正典快照来源）。

    Returns:
        回复生成面适配器。

    Raises:
        BusinessException: 网关未装配（lifespan 顺序破坏——
            ``INTERNAL_ERROR``）。
    """
    app_state = websocket.app.state
    gateway = cast("LlmGateway | None", getattr(app_state, "gateway", None))
    if gateway is None:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="实时语音装配前置失败：LLM 网关未就绪（lifespan 顺序破坏）",
        )
    chat_service = ChatService(
        gateway=gateway,
        session_service=ChatSessionService(),
        directive_service=DirectivesService(),
        facts_reader=FactsService(),
        core_memory_reader=CoreMemoryService(),
        skills_reader=SkillsService(),
        episode_sink=getattr(app_state, "episode_sink", None),
    )
    async with bind_persona_context(persona.id), session_scope() as db_session:
        state: ChatSessionStateModel = await chat_service.create_session(
            persona=persona,
            db_session=db_session,
            title="实时语音会话",
        )
    return ChatSessionStreamAdapter(chat_service, state)


@router.websocket("/ws")
async def realtime_voice_websocket(websocket: WebSocket) -> None:
    """实时语音全双工会话端点（协议 / 状态机 / 编排全部在 harness）。

    Args:
        websocket: FastAPI 注入的 WS 连接。
    """
    await websocket.accept()
    settings = _resolve_settings(websocket)
    if not _authorized(websocket, settings):
        await _send_bootstrap_error(
            websocket,
            code=str(ErrorCode.UNAUTHORIZED),
            message="管理面鉴权失败：实时语音 WS 查询参数 token 缺失或不匹配",
        )
        await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION, reason="unauthorized")
        return
    realtime_settings = settings.realtime_voice
    if not realtime_settings.enabled:
        await _send_bootstrap_error(
            websocket,
            code=str(ErrorCode.CONFLICT),
            message="实时语音未启用（REALTIME_VOICE_ENABLED=true 时可用）",
        )
        await websocket.close(code=_WS_CLOSE_TRY_AGAIN, reason="realtime voice disabled")
        return
    app_state = websocket.app.state
    active_sessions = cast("int", getattr(app_state, "realtime_voice_active_sessions", 0))
    if active_sessions >= realtime_settings.max_sessions:
        logger.warning(
            "实时语音并发会话已达上限（1013 拒绝）: active={}, limit={}",
            active_sessions,
            realtime_settings.max_sessions,
        )
        await _send_bootstrap_error(
            websocket,
            code=str(ErrorCode.RATE_LIMITED),
            message=(
                "实时语音并发会话已达上限"
                f"（REALTIME_VOICE_MAX_SESSIONS={realtime_settings.max_sessions}），请稍后重试"
            ),
        )
        await websocket.close(code=_WS_CLOSE_TRY_AGAIN, reason="realtime voice busy")
        return
    app_state.realtime_voice_active_sessions = active_sessions + 1
    try:
        try:
            persona = await _resolve_persona(websocket)
            chat = await _build_chat(websocket, persona)
        except BusinessException as exc:
            await _send_bootstrap_error(
                websocket, code=str(exc.code), message=exc.message
            )
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION, reason=str(exc.code))
            return
        logger.info(
            "实时语音连接已接入: persona_id={}, tts_enabled={}, asr_enabled={}",
            persona.id,
            settings.voice.tts.enabled,
            settings.voice.asr.enabled,
        )
        session = RealtimeVoiceSession(
            transport=StarletteRealtimeTransport(websocket),
            realtime_settings=realtime_settings,
            voice_settings=settings.voice,
            chat=chat,
            voice_service=VoiceService(
                usage_recorder=wire_default_voice_usage_recorder(),
                usage_counter=wire_default_voice_usage_counter(),
            ),
            persona=persona,
        )
        await session.run()
    finally:
        current = cast(
            "int", getattr(app_state, "realtime_voice_active_sessions", active_sessions + 1)
        )
        app_state.realtime_voice_active_sessions = max(0, current - 1)
