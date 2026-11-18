"""chat 技能注入测试——compose 块装配 / 链位置 / 命中条件 / 服务层接线。

覆盖面：``render_skills_block``（空跳过 / 标题与正文渲染 / 字符帽截断 /
双技能分隔）/ compose 链位置（人物事实块之后、输出格式协议之前）/
ChatService 持久化会话按**本轮用户文本**匹配技能并注入（无命中不注入 /
读取面未接线 / 纯内存形态零 I/O）/ 读取失败不阻断回复。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.skills_orm as skills_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.prompts import render_skills_block
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills import SkillsService
from packages.harness.persona.skills_models import (
    SkillCreateModel,
    SkillRecordDTO,
    SkillSource,
    SkillStatus,
)


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


def _skill(
    name: str = "周报流程",
    *,
    content: str = "1. 先列三件事\n2. 再写进展",
    triggers: list[str] | None = None,
) -> SkillRecordDTO:
    """构造最小技能记录（渲染测试铺底）。"""
    return SkillRecordDTO(
        id=1,
        name=name,
        slug="zhou-bao",
        description="主人说「写周报」时使用",
        triggers=triggers or ["写周报"],
        content=content,
        status=SkillStatus.ACTIVE,
        source=SkillSource.MANUAL,
        confidence=0.8,
        use_count=0,
        last_used_ms=None,
        version=1,
        created_at=0,
        updated_at=0,
    )


async def _create_persona(db_session: AsyncSession, name: str = "skill-chat") -> PersonaRecordDTO:
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


async def test_render_skills_block_empty_skips() -> None:
    """空列表：返回空串（组装链跳过空块）。"""
    assert render_skills_block([]) == ""


async def test_render_skills_block_renders_headers_and_content() -> None:
    """非空列表：标题 + markdown 正文；双技能以空行分隔。"""
    text = render_skills_block(
        [_skill(), _skill(name="日报流程", content="1. 写完成\n2. 写计划")]
    )
    assert text.startswith("【相关技能】")
    assert "### 周报流程\n1. 先列三件事" in text
    assert "### 日报流程\n1. 写完成" in text
    assert "\n\n### 日报流程" in text


async def test_render_skills_block_truncates_to_budget() -> None:
    """字符帽：总文本不超过预算；截断后不再追加后续技能。"""
    text = render_skills_block(
        [
            _skill(name="技能甲", content="甲" * 80),
            _skill(name="技能乙", content="乙" * 80),
        ],
        max_chars=40,
    )
    assert len(text) <= 40 + len("【相关技能】\n")
    assert "### 技能甲" in text
    assert "技能乙" not in text
    assert text.endswith("甲")


async def test_compose_inserts_skills_between_facts_and_protocol() -> None:
    """compose 链：相关技能块位于人物事实块之后、输出格式协议之前。"""
    result = compose_messages(
        _state(),
        directives=["以后都用简体中文"],
        facts=["【偏好】主人喜欢无糖乌龙茶"],
        skills=[_skill()],
    )
    prompt = result.system_prompt
    facts_index = prompt.index("【关于对方】")
    skills_index = prompt.index("【相关技能】")
    protocol_index = prompt.index("【回复格式协议】")
    assert facts_index < skills_index < protocol_index
    assert "### 周报流程" in prompt


async def test_compose_without_skills_unchanged() -> None:
    """未提供技能：system 提示不含相关技能块（既有形态零变更）。"""
    result = compose_messages(_state())
    assert "【相关技能】" not in result.system_prompt


# ---------------------------------------------------------------------------
# 服务层接线（持久化会话）
# ---------------------------------------------------------------------------


async def test_reply_injects_matched_skill(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """命中本轮用户文本：技能进入本轮 system 提示（标题 + 正文）。"""
    gateway, session_service, persona, db_session = chat_env
    skills_service = SkillsService(usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(persona.id):
        await skills_service.create(
            db_session,
            SkillCreateModel(
                name="周报流程",
                description="主人说「写周报」时使用",
                triggers=["写周报"],
                content="1. 先列三件事\n2. 再写进展",
            ),
        )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        skills_reader=skills_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "帮我写周报")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【相关技能】" in system_prompt
    assert "### 周报流程" in system_prompt
    assert "1. 先列三件事" in system_prompt


async def test_reply_no_match_skips_block(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """技能存在但本轮文本未命中：不注入相关技能块（内容只随当前输入）。"""
    gateway, session_service, persona, db_session = chat_env
    skills_service = SkillsService(usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(persona.id):
        await skills_service.create(
            db_session,
            SkillCreateModel(
                name="周报流程",
                description="主人说「写周报」时使用",
                triggers=["写周报"],
                content="1. 先列三件事",
            ),
        )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        skills_reader=skills_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "今天天气不错")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【相关技能】" not in system_prompt


async def test_reply_without_skills_reader_skips_block(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """未接线读取面：system 提示不含相关技能块（既有行为零变更）。"""
    gateway, session_service, persona, db_session = chat_env
    service = ChatService(gateway=gateway, session_service=session_service)
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "帮我写周报")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【相关技能】" not in system_prompt


async def test_reply_memory_only_skips_skill_read() -> None:
    """纯内存形态（无持久化会话）：读取面接线也不触库 / 不注入。"""
    gateway = FakeGateway()

    class ExplodingSkills(SkillsService):
        """读取即炸的技能服务（证明纯内存形态零调用）。"""

        async def match_for_compose(
            self, session: AsyncSession, user_text: str
        ) -> list[SkillRecordDTO]:
            raise AssertionError("纯内存形态不应读取技能")

    service = ChatService(gateway=gateway, skills_reader=ExplodingSkills())
    state = _state()

    await service.reply(state, "帮我写周报")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【相关技能】" not in system_prompt


async def test_reply_skill_read_failure_degrades(
    chat_env: tuple[FakeGateway, ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取异常：本轮不注入相关技能块，回复照常（增强面不阻断链路）。"""
    gateway, session_service, persona, db_session = chat_env

    class BoomSkills(SkillsService):
        """读取恒抛的技能服务（失败退化路径）。"""

        async def match_for_compose(
            self, session: AsyncSession, user_text: str
        ) -> list[SkillRecordDTO]:
            raise RuntimeError("读取失败（测试注入）")

    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        skills_reader=BoomSkills(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "帮我写周报")

    assert content == "假回复"
    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【相关技能】" not in system_prompt
