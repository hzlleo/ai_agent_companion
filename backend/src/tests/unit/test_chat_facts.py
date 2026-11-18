"""chat 人物事实注入测试——compose 块装配 / 链位置 / 服务层读取接线。

覆盖面：``render_facts_block``（空跳过 / 列表渲染）/ compose 链位置
（长期指令块之后、输出格式协议之前）/ ChatService 持久化会话读取现行
高置信事实并注入（读取面未接线 / 纯内存形态零 I/O）/ 读取失败不阻断
回复（空块退化）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.facts_orm as facts_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.prompts import render_facts_block
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.facts import FactsService
from packages.harness.persona.facts_models import FactCreateDTO, PersonaFactCategory
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


async def _create_persona(db_session: AsyncSession, name: str = "fact-chat") -> PersonaRecordDTO:
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


async def test_render_facts_block_empty_skips() -> None:
    """空列表 / 全空白条目：返回空串（组装链跳过空块）。"""
    assert render_facts_block([]) == ""
    assert render_facts_block(["   ", ""]) == ""


async def test_render_facts_block_renders_lines() -> None:
    """非空列表：标题 + 逐条「- 【类别标签】正文」行。"""
    text = render_facts_block(["【偏好】主人喜欢无糖乌龙茶", "【背景】主人在杭州工作"])
    assert text.startswith("【关于对方】")
    assert "- 【偏好】主人喜欢无糖乌龙茶" in text
    assert "- 【背景】主人在杭州工作" in text


async def test_compose_inserts_facts_between_directives_and_protocol() -> None:
    """compose 链：人物事实块位于长期指令块之后、输出格式协议之前。"""
    result = compose_messages(
        _state(),
        directives=["以后都用简体中文"],
        facts=["【偏好】主人喜欢无糖乌龙茶"],
    )
    prompt = result.system_prompt
    directives_index = prompt.index("【长期指令】")
    facts_index = prompt.index("【关于对方】")
    protocol_index = prompt.index("【回复格式协议】")
    assert directives_index < facts_index < protocol_index
    assert "- 【偏好】主人喜欢无糖乌龙茶" in prompt


async def test_compose_without_facts_unchanged() -> None:
    """未提供事实：system 提示不含人物事实块（既有形态零变更）。"""
    result = compose_messages(_state())
    assert "【关于对方】" not in result.system_prompt


async def test_reply_injects_active_facts(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """持久化会话 + 事实读取面接线：现行事实进入本轮 system 提示。"""
    gateway, session_service, persona, db_session = chat_env
    facts_service = FactsService()
    async with bind_persona_context(persona.id):
        await facts_service.upsert_fact(
            db_session,
            FactCreateDTO(
                category=PersonaFactCategory.PREFERENCE,
                content="主人喜欢无糖乌龙茶",
                confidence=0.9,
            ),
        )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=facts_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关于对方】" in system_prompt
    assert "- 【偏好】主人喜欢无糖乌龙茶" in system_prompt


async def test_reply_without_facts_reader_skips_block(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """未接线读取面：system 提示不含人物事实块（既有行为零变更）。"""
    gateway, session_service, persona, db_session = chat_env
    service = ChatService(gateway=gateway, session_service=session_service)
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关于对方】" not in system_prompt


async def test_reply_memory_only_skips_fact_read() -> None:
    """纯内存形态（无持久化会话）：读取面接线也不触库 / 不注入。"""
    gateway = FakeGateway()

    class ExplodingFacts(FactsService):
        """读取即炸的事实服务（证明纯内存形态零调用）。"""

        async def top_facts_for_compose(
            self, session: AsyncSession, *, limit: int = 12, max_chars: int = 1500
        ) -> Sequence[str]:
            raise AssertionError("纯内存形态不应读取人物事实")

    service = ChatService(gateway=gateway, facts_reader=ExplodingFacts())
    state = _state()

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关于对方】" not in system_prompt


async def test_reply_fact_read_failure_degrades(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取异常：本轮不注入人物事实块，回复照常（增强面不阻断链路）。"""
    gateway, session_service, persona, db_session = chat_env

    class BoomFacts(FactsService):
        """读取恒抛的事实服务（失败退化路径）。"""

        async def top_facts_for_compose(
            self, session: AsyncSession, *, limit: int = 12, max_chars: int = 1500
        ) -> Sequence[str]:
            raise RuntimeError("读取失败（测试注入）")

    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=BoomFacts(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "早呀")

    assert content == "假回复"
    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关于对方】" not in system_prompt
