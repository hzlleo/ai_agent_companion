"""chat 长期指令注入测试——compose 块装配 / 截断帽 / 服务层读取接线。

覆盖面：``render_directives_block``（空跳过 / 列表渲染 / 条数帽 /
字符帽 + 截断说明）/ compose 链位置（心情块之后、输出格式协议之前）/
ChatService 持久化会话读取现行指令并注入（注入服务未接线 / 纯内存形态
零 I/O）/ 读取失败不阻断回复（空块退化）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.prompts import (
    DIRECTIVES_MAX_CHARS,
    DIRECTIVES_MAX_ENTRIES,
    DIRECTIVES_TRUNCATION_NOTE,
    render_directives_block,
)
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.directives_models import DirectiveCreateDTO
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context


class FakeGateway:
    """假 LLM 网关——记录组装消息（零真实网络）。"""

    def __init__(self, *, response_content: str = "假回复") -> None:
        """初始化假网关。"""
        self.response_content = response_content
        self.chat_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录参数）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        return ChatResponseModel(content=self.response_content)

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """流式假调用（单块产出——现有测试面同形态）。"""
        from packages.harness.llm.llm_models import ChatStreamChunkModel

        yield ChatStreamChunkModel(content=self.response_content)


def _card() -> CharaCardModel:
    """最小人格卡片。"""
    return CharaCardModel.model_validate(
        {
            "name": "大肥鱼",
            "description": "圆滚滚的橘色胖鱼",
            "personality": "元气",
            "first_mes": "你回来啦！",
        }
    )


def _state(*, session_id: int | None = None, persona_id: int = 1) -> ChatSessionStateModel:
    """构造会话状态（可指定持久化会话主键）。"""
    return ChatSessionStateModel(
        persona_id=persona_id,
        persona_name="feiyu",
        display_name="大肥鱼",
        card=_card(),
        session_id=session_id,
    )


async def _create_persona(db_session: AsyncSession, name: str = "dir-chat") -> PersonaRecordDTO:
    """经真实 PersonaService 创建人格。"""
    return await PersonaService().create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": name,
                "status": "active",
                "card": {"name": "大肥鱼", "first_mes": "你回来啦！"},
            }
        ),
    )


@pytest.fixture
async def chat_env(
    shared_db_engine: AsyncEngine,
) -> AsyncIterator[tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession]]:
    """持久化注入环境（共享引擎外部安装到 holder；主人格铺底）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    gateway = FakeGateway()
    session_service = ChatSessionService()
    async with async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)() as db_session:
        persona = await _create_persona(db_session)
        yield gateway, session_service, persona, db_session
    db_engine_holder.reset()


# ---------------------------------------------------------------------------
# 渲染面
# ---------------------------------------------------------------------------


async def test_render_directives_block_empty_skips() -> None:
    """空列表 / 全空白条目：返回空串（组装链跳过空块）。"""
    assert render_directives_block([]) == ""
    assert render_directives_block(["   ", ""]) == ""


async def test_render_directives_block_renders_lines() -> None:
    """非空列表：标题 + 逐条「- 正文」行。"""
    text = render_directives_block(["以后都用简体中文", "不要熬夜"])
    assert text.startswith("【长期指令】")
    assert "- 以后都用简体中文" in text
    assert "- 不要熬夜" in text
    assert DIRECTIVES_TRUNCATION_NOTE not in text


async def test_render_directives_block_entry_cap() -> None:
    """条数帽：超出上限的条目省略并追加截断说明。"""
    directives = [f"指令 {index}" for index in range(DIRECTIVES_MAX_ENTRIES + 5)]
    text = render_directives_block(directives)
    assert text.count("- 指令 ") == DIRECTIVES_MAX_ENTRIES
    assert DIRECTIVES_TRUNCATION_NOTE in text


async def test_render_directives_block_char_cap() -> None:
    """字符帽：总字符超限即截断（保留已装入条目 + 说明行）。"""
    directives = ["字" * 480 for _ in range(6)]
    text = render_directives_block(directives)
    body_lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(body_lines) < 6
    assert DIRECTIVES_TRUNCATION_NOTE in text
    assert len(text) <= DIRECTIVES_MAX_CHARS + len(DIRECTIVES_TRUNCATION_NOTE) + 20


# ---------------------------------------------------------------------------
# compose 链位置
# ---------------------------------------------------------------------------


async def test_compose_inserts_directives_before_protocol() -> None:
    """compose 链：长期指令块位于心情块之后、输出格式协议之前。"""
    result = compose_messages(
        _state(),
        emotion_summary="当前情绪：愉悦↑",
        directives=["以后都用简体中文"],
    )
    prompt = result.system_prompt
    emotion_index = prompt.index("【心情状态】")
    directives_index = prompt.index("【长期指令】")
    protocol_index = prompt.index("【回复格式协议】")
    assert emotion_index < directives_index < protocol_index
    assert "- 以后都用简体中文" in prompt


async def test_compose_without_directives_unchanged() -> None:
    """未提供长期指令：system 提示不含长期指令块（既有形态零变更）。"""
    result = compose_messages(_state())
    assert "【长期指令】" not in result.system_prompt


# ---------------------------------------------------------------------------
# 服务层读取接线
# ---------------------------------------------------------------------------


async def test_reply_injects_active_directives(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """持久化会话 + 指令服务接线：现行指令进入本轮 system 提示。"""
    gateway, session_service, persona, db_session = chat_env
    directives_service = DirectivesService()
    async with bind_persona_context(persona.id):
        await directives_service.create(
            db_session, DirectiveCreateDTO(content="以后都用简体中文")
        )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        directive_service=directives_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【长期指令】" in system_prompt
    assert "- 以后都用简体中文" in system_prompt


async def test_reply_without_directive_service_skips_block(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """未接线指令服务：system 提示不含长期指令块（既有行为零变更）。"""
    gateway, session_service, persona, db_session = chat_env
    service = ChatService(gateway=gateway, session_service=session_service)
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【长期指令】" not in system_prompt


async def test_reply_memory_only_skips_directive_read() -> None:
    """纯内存形态（无持久化会话）：指令服务接线也不触库 / 不注入。"""
    gateway = FakeGateway()

    class ExplodingDirectives(DirectivesService):
        """读取即炸的指令服务（证明纯内存形态零调用）。"""

        async def list_active_texts(
            self, session: AsyncSession, *, limit: int = 20
        ) -> Sequence[str]:
            raise AssertionError("纯内存形态不应读取长期指令")

    service = ChatService(
        gateway=gateway,
        directive_service=ExplodingDirectives(),
    )
    state = _state()

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【长期指令】" not in system_prompt


async def test_reply_directive_read_failure_degrades(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取异常：本轮不注入长期指令块，回复照常（增强面不阻断链路）。"""
    gateway, session_service, persona, db_session = chat_env

    class BoomDirectives(DirectivesService):
        """读取恒抛的指令服务（失败退化路径）。"""

        async def list_active_texts(
            self, session: AsyncSession, *, limit: int = 20
        ) -> Sequence[str]:
            raise RuntimeError("读取失败（测试注入）")

    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        directive_service=BoomDirectives(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "早呀")

    assert content == "假回复"
    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【长期指令】" not in system_prompt
