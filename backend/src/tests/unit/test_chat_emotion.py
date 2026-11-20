"""chat 输出协议 + 轻情绪引擎集成测试（真实 SQLite / 假网关 / 注入时钟）。

覆盖面：reply 协议剥离与可见正文返回（心声 / 情绪标签不泄漏）/ 回合
meta 落库（thought / deltas / snapshot）与会话详情 DTO 读侧 / M 态情绪
文档推进（ayer=M 全维基线 + 增量）/ compose 心情块注入（非中性状态）/
无标签模型零状态变化 / 引擎停用（不读不写、meta 无快照）/ 流式边界
（起始标签前缀剥离 + 不落 meta）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.harness.chat.service import ChatService
from packages.harness.config.model_entry import ModelRole
from packages.harness.emotion.emotion_models import EMOTION_DOCUMENT_KEY, EmotionSettings
from packages.harness.emotion.service import EmotionService
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
)
from packages.harness.memory.dtos import ChatMessageMetaModel
from packages.harness.memory.orm import ChatMessageRow
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.orm import PersonaStateRow
from packages.harness.persona.service import PersonaService, bind_persona_context

#: 固定测试时钟（衰减 / 快照时间锚确定性）。
_FIXED_NOW_MS: int = 1_760_000_000_000


class ScriptedGateway:
    """脚本化假网关（非流式回复 / 流式增量可注入——零网络）。"""

    def __init__(
        self, *, response_content: str = "假回复", stream_parts: Sequence[str] = ("早", "安")
    ) -> None:
        """初始化假网关。

        Args:
            response_content: 非流式回复原文。
            stream_parts: 流式增量块序列。
        """
        self.response_content = response_content
        self.stream_parts = list(stream_parts)
        self.chat_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: ModelRole | str, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录组装消息）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        return ChatResponseModel(content=self.response_content)

    async def stream_by_role(
        self, role: ModelRole | str, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（按块回放）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        for part in self.stream_parts:
            yield ChatStreamChunkModel(content=part)


def _persona_payload(name: str) -> dict[str, Any]:
    """最小人格创建载荷（chara card 字段齐备）。"""
    return {
        "name": name,
        "status": "active",
        "card": {
            "name": "大肥鱼",
            "description": "圆滚滚的橘色胖鱼",
            "personality": "元气",
            "first_mes": "你回来啦！",
        },
    }


async def _create_persona(db_session: AsyncSession) -> PersonaRecordDTO:
    """经真实 PersonaService 创建人格记录。"""
    return await PersonaService().create(
        db_session, PersonaCreateDTO.model_validate(_persona_payload("emotion-feiyu"))
    )


def _emotion_service(*, enabled: bool = True) -> EmotionService:
    """注入式情绪服务（真实 M 态存储 + 固定时钟）。"""
    return EmotionService(
        settings=EmotionSettings(enabled=enabled), clock=lambda: _FIXED_NOW_MS
    )


@pytest.fixture
async def emotion_env(
    shared_db_engine: AsyncEngine,
) -> AsyncIterator[tuple[AsyncSession, PersonaRecordDTO, ChatSessionService]]:
    """共享 SQLite 上的会话 / 人格铺底（holder 安装供 M 态 session_scope）。

    Yields:
        ``(db_session, persona, session_service)`` 三元组。
    """
    from packages.harness.database.engine import db_engine_holder

    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    async with async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)() as db_session:
        persona = await _create_persona(db_session)
        yield db_session, persona, ChatSessionService()
    db_engine_holder.reset()


async def _m_state(db_session: AsyncSession, persona_id: int) -> dict[str, Any] | None:
    """读取人格 M 层状态文档（None = 未写过）。"""
    row = (
        (
            await db_session.execute(
                select(PersonaStateRow).where(
                    PersonaStateRow.persona_id == persona_id,
                    PersonaStateRow.layer == "M",
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    return dict(row.state) if row is not None else None


async def test_reply_strips_protocol_and_persists_meta(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """reply：返回正文不含标签；meta 落心声 / 增量 / 快照；M 态情绪推进。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(
        response_content=(
            "<心声>他终于来了，等了好久。</心声>"
            "<情绪>joy:+0.4, affection:0.2</情绪>你回来啦，快坐下。"
        )
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    content = await service.reply(state, "我回来了")

    assert content == "你回来啦，快坐下。"
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assistant = detail.messages[-1]
    assert assistant.content == "你回来啦，快坐下。"
    assert assistant.meta is not None
    assert assistant.meta.thought == "他终于来了，等了好久。"
    assert assistant.meta.emotion_deltas == {"joy": 0.4, "affection": 0.2}
    assert assistant.meta.emotion_snapshot is not None
    assert assistant.meta.emotion_snapshot["joy"] == pytest.approx(0.9)
    assert assistant.meta.emotion_snapshot["affection"] == pytest.approx(0.7)
    # 其他维度首轮初始化到中性基线。
    assert assistant.meta.emotion_snapshot["sadness"] == pytest.approx(0.5)

    document = await _m_state(db_session, persona.id)
    assert document is not None
    emotion_doc = document[EMOTION_DOCUMENT_KEY]
    assert emotion_doc["values"]["joy"] == pytest.approx(0.9)
    assert emotion_doc["updated_at_ms"] == _FIXED_NOW_MS


async def test_second_reply_injects_emotion_block(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """compose：非中性 M 态 → 心情块注入（persona 之后 / 输出协议之前）。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(
        response_content="<情绪>joy:+0.5</情绪>今天也开心。"
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    await service.reply(state, "第一轮")
    await service.reply(state, "第二轮")

    system_prompt = gateway.chat_calls[-1]["messages"][0].content
    assert "【心情状态】" in system_prompt
    assert "当前情绪：愉悦↑" in system_prompt
    assert system_prompt.index("【心情状态】") < system_prompt.index("【回复格式协议】")


async def test_reply_without_tags_keeps_zero_meta_and_zero_state(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """不合规模型（无标签）：原文可见、meta 为 null、M 态零写入。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(response_content="今天也辛苦啦。")
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    content = await service.reply(state, "嗯")

    assert content == "今天也辛苦啦。"
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assert detail.messages[-1].meta is None
    assert await _m_state(db_session, persona.id) is None


async def test_emotion_disabled_records_meta_without_snapshot(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """引擎停用：meta 仍留档心声 / 增量，快照为 null；M 态零写入、无心情块。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(
        response_content="<心声>今天有点安静。</心声><情绪>calm:0.2</情绪>我在呢。"
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(enabled=False),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    content = await service.reply(state, "在吗")

    assert content == "我在呢。"
    system_prompt = gateway.chat_calls[-1]["messages"][0].content
    assert "【心情状态】" not in system_prompt
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    meta = detail.messages[-1].meta
    assert meta is not None
    assert meta.thought == "今天有点安静。"
    assert meta.emotion_snapshot is None
    assert await _m_state(db_session, persona.id) is None


async def test_stream_reply_strips_leading_tags_and_skips_meta(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """流式边界：起始标签前缀被剥离、落库正文无标签、meta 恒 null（不推进 M 态）。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(
        stream_parts=["<心声>想", "他。</心声><情绪>joy:0.6</情绪>", "我在呢", "。"],
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    chunks: list[str] = []
    async for token in service.stream_reply(state, "在吗"):
        chunks.append(token)

    assert "".join(chunks) == "我在呢。"
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assistant = detail.messages[-1]
    assert assistant.content == "我在呢。"
    assert assistant.meta is None
    assert await _m_state(db_session, persona.id) is None


async def test_stream_reply_unclosed_tag_passes_through(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """流式未闭合标签：flush 交还标签内文本（best-effort——不吞字）。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(stream_parts=["<心声>还没说完"])
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    chunks = [token async for token in service.stream_reply(state, "嗯")]
    assert "".join(chunks) == "还没说完"


async def test_reply_meta_is_model_validated_and_byte_compatible(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """写侧 meta 经 ChatMessageMetaModel 构造：落库结构仍为旧三键且可回读校验。"""
    db_session, persona, session_service = emotion_env
    gateway = ScriptedGateway(
        response_content=(
            "<心声>模型化心声。</心声><情绪>joy:0.3</情绪>收到。"
        )
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        emotion_service=_emotion_service(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    assert await service.reply(state, "你好") == "收到。"
    row = (
        await db_session.execute(
            select(ChatMessageRow).where(ChatMessageRow.session_id == state.session_id)
            .where(ChatMessageRow.role == ChatMessageRole.ASSISTANT.value)
        )
    ).scalars().one()
    assert row.meta is not None
    assert set(row.meta) == {"thought", "emotion_deltas", "emotion_snapshot"}
    parsed = ChatMessageMetaModel.model_validate(row.meta)
    assert parsed.thought == "模型化心声。"
    assert parsed.emotion_deltas == {"joy": 0.3}
    assert parsed.emotion_snapshot is not None
    assert parsed.emotion_snapshot["joy"] == pytest.approx(0.8)
    async with bind_persona_context(persona.id):
        detail = await session_service.get_session(db_session, state.session_id or 0)
    assistant = detail.messages[-1]
    assert assistant.meta is not None
    assert assistant.meta.thought == "模型化心声。"
    assert assistant.meta.emotion_deltas == {"joy": 0.3}


async def test_append_message_meta_round_trip(
    emotion_env: tuple[AsyncSession, PersonaRecordDTO, ChatSessionService],
) -> None:
    """持久化面：append_message 可选 meta 原样落库并经详情 DTO 读回。"""
    db_session, persona, session_service = emotion_env
    async with bind_persona_context(persona.id):
        summary = await session_service.create_session(db_session, title="meta 直写")
        meta = {
            "thought": "测试心声",
            "emotion_deltas": {"joy": 0.1},
            "emotion_snapshot": {"joy": 0.6},
        }
        await session_service.append_message(
            db_session,
            summary.id,
            role=ChatMessageRole.ASSISTANT,
            content="可见正文",
            meta=meta,
        )
        detail = await session_service.get_session(db_session, summary.id)
    assert detail.messages[0].meta is not None
    assert detail.messages[0].meta.thought == "测试心声"
    assert detail.messages[0].meta.emotion_snapshot == {"joy": 0.6}
    row = (
        await db_session.execute(
            select(ChatMessageRow).where(ChatMessageRow.session_id == summary.id)
        )
    ).scalars().one()
    assert row.meta == meta
