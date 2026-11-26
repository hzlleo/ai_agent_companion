"""chat 关系进展块测试——渲染面 / compose 链位置 / 服务层读取与分支一致性。

测试策略：正常注入走真实事实服务 + 共享 SQLite（关系类事实经写入口径
铺底）；读失败 / 流式 / 工具循环 / 合规重试分支走脚本化事实读面（
``list_facts`` 返回固定分页或抛错）与脚本化网关——零真实网络。覆盖面：

    - 渲染：空摘要仅阶段行 / 阶段中文称谓 / 最近变化行 / 边界行 / 1200
      字符硬帽与确定性截断；
    - compose 链：关系进展块位于【自我认知】之后、【相关技能】之前；
    - 服务层：关系类事实正常注入 / 事实读取失败降级为空摘要（初识）且
      回复照常 / 读取面未接线整块跳过 / 纯内存形态零 I/O；
    - 分支一致性：stream_reply / 工具循环 / 合规重试请求全部携带同一
      关系进展块；
    - 构造注入：``relationship_thresholds`` 替换阈值表后阶段随表变化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.facts_orm as facts_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.compliance_models import ComplianceGuardSettings
from packages.harness.chat.prompts import (
    RELATIONSHIP_BLOCK_MAX_CHARS,
    render_relationship_block,
)
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.core.dtos import PageResponse
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import (
    ChatResponseModel,
    ChatStreamChunkModel,
    ToolCallModel,
    ToolParametersSchema,
)
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.core_memory_models import (
    CoreBlockKey,
    CoreMemoryActor,
    CoreMemoryBlockDTO,
)
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.facts import FactsService
from packages.harness.persona.facts_models import (
    FactCreateDTO,
    FactQueryDTO,
    FactRecordDTO,
    PersonaFactCategory,
    PersonaFactSource,
    PersonaFactStatus,
)
from packages.harness.persona.relationship import build_relationship_summary
from packages.harness.persona.relationship_models import (
    RelationshipThresholdsModel,
)
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills_models import (
    SkillRecordDTO,
    SkillSource,
    SkillStatus,
)
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)

#: 固定派生时刻（渲染 / 派生的时间锚——不读系统时钟）。
_NOW_MS: int = 1_700_000_000_000

#: 一天的毫秒数。
_DAY_MS: int = 86_400_000

#: 典型助手腔回复（合规守卫命中）。
_VIOLATING_TEXT: str = "作为一个AI，我建议你早点休息。"

#: 合规角色台词。
_COMPLIANT_TEXT: str = "哼，那你还不快去睡，别在这儿磨蹭。"


class ScriptedFactsReader:
    """脚本化事实读面——返回固定分页或抛错（服务分支测试注入面）。"""

    def __init__(
        self,
        items: list[FactRecordDTO] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """初始化脚本与查询记录。"""
        self._items = list(items or [])
        self._error = error
        self.queries: list[FactQueryDTO] = []

    async def top_facts_for_compose(self, session: AsyncSession) -> list[str]:
        """人物事实注入行（关系测试不关心——空列表）。"""
        return []

    async def list_facts(
        self, session: AsyncSession, query: FactQueryDTO
    ) -> PageResponse[FactRecordDTO]:
        """记录查询并返回脚本分页（配置了异常时抛出）。"""
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return PageResponse(items=list(self._items), total=-1, page_size=query.limit)


class ExplodingFactsReader:
    """读取即炸的事实读面（证明纯内存形态零调用）。"""

    async def top_facts_for_compose(self, session: AsyncSession) -> list[str]:
        """不应被调用。"""
        raise AssertionError("纯内存形态不应读取人物事实")

    async def list_facts(
        self, session: AsyncSession, query: FactQueryDTO
    ) -> PageResponse[FactRecordDTO]:
        """不应被调用。"""
        raise AssertionError("纯内存形态不应读取关系事实")


class ScriptedGateway:
    """脚本化假网关——按调用序返回预设回复并记录请求（含流式调用）。"""

    def __init__(
        self,
        responses: list[tuple[str, list[ToolCallModel] | None]] | None = None,
        *,
        stream_text: str = "假回复",
    ) -> None:
        """初始化脚本队列与请求记录。"""
        self._responses = list(responses or [])
        self._stream_text = stream_text
        self.chat_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """记录非流式请求并弹出下一条脚本回复。"""
        self.chat_calls.append({"role": role, "messages": list(messages), "params": params, **kwargs})
        if self._responses:
            content, tool_calls = self._responses.pop(0)
            return ChatResponseModel(content=content, tool_calls=tool_calls or [])
        return ChatResponseModel(content="假回复")

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """记录流式请求并产出预设文本。"""
        self.stream_calls.append({"role": role, "messages": list(messages), **kwargs})
        yield ChatStreamChunkModel(content=self._stream_text)


class FakeToolExecutor:
    """假工具执行面（返回成功载荷）。"""

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """执行单次调用（成功形态）。"""
        return ToolResultModel(tool=call.name, ok=True, result={"items": []})


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


def _fact(
    fact_id: int = 1,
    *,
    content: str = "主人开始叫我小鱼",
    confidence: float = 0.8,
    updated_at: int | None = None,
) -> FactRecordDTO:
    """构造现行关系类事实记录（派生输入铺底）。"""
    ts = _NOW_MS if updated_at is None else updated_at
    return FactRecordDTO(
        id=fact_id,
        category=PersonaFactCategory.RELATIONSHIP,
        content=content,
        confidence=confidence,
        status=PersonaFactStatus.ACTIVE,
        provenance=MemoryProvenance.OWNER,
        source=PersonaFactSource.MANUAL,
        evidence=[],
        superseded_by_id=None,
        version=1,
        created_at=ts,
        updated_at=ts,
    )


def _core_block() -> CoreMemoryBlockDTO:
    """最小核心记忆块（链位置对照——前一名的块）。"""
    return CoreMemoryBlockDTO(
        id=1,
        block=CoreBlockKey.RELATIONSHIP_STORY,
        content="我们一起养过一条金鱼",
        locked=False,
        version=1,
        updated_by=CoreMemoryActor.OWNER,
        last_edited_session_id=None,
        created_at=0,
        updated_at=0,
    )


def _skill() -> SkillRecordDTO:
    """最小技能记录（链位置对照——后一名的块）。"""
    return SkillRecordDTO(
        id=1,
        name="先给结论",
        slug="xian-gei-jie-lun",
        description="回答先给结论",
        triggers=["先给结论"],
        content="先给结论，再解释。",
        status=SkillStatus.ACTIVE,
        source=SkillSource.MANUAL,
        confidence=0.8,
        use_count=0,
        last_used_ms=None,
        version=1,
        created_at=0,
        updated_at=0,
    )


def _tool_specs() -> list[ToolSpecModel]:
    """单工具面（工具循环分支对照）。"""
    return [
        ToolSpecModel(
            name="web_search",
            description="联网搜索",
            args_schema=ToolParametersSchema.model_validate(
                {"type": "object", "properties": {}, "required": []}
            ),
        )
    ]


async def _create_persona(db_session: AsyncSession, name: str = "rel-chat") -> PersonaRecordDTO:
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
) -> AsyncIterator[tuple[ChatSessionService, PersonaRecordDTO, AsyncSession]]:
    """持久化注入环境（共享引擎外部安装到 holder；主人格铺底）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    async with async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)() as db_session:
        persona = await _create_persona(db_session)
        yield ChatSessionService(), persona, db_session
    db_engine_holder.reset()


# ---------------------------------------------------------------------------
# 渲染面
# ---------------------------------------------------------------------------


async def test_render_relationship_block_none_skips() -> None:
    """未接线读取面（None）：返回空串（组装链跳过整块）。"""
    assert render_relationship_block(None) == ""


async def test_render_relationship_block_empty_summary_keeps_stage_line() -> None:
    """空摘要：只渲染阶段行（恒注入面——不出现事件标题与边界行）。"""
    summary = build_relationship_summary([], now_ms=_NOW_MS)
    text = render_relationship_block(summary)
    assert text == "【关系进展】\n当前阶段：初识（亲密等级 0/3）"
    assert "最近的变化：" not in text
    assert "相处边界：" not in text


async def test_render_relationship_block_stage_events_and_boundaries() -> None:
    """非空摘要：中文阶段称谓 + 最近变化逐条 + 相处边界行。"""
    summary = build_relationship_summary(
        [
            _fact(1, content="说好不熬夜", updated_at=_NOW_MS),
            _fact(2, content="主人开始叫我小鱼", updated_at=_NOW_MS - _DAY_MS),
        ],
        now_ms=_NOW_MS,
    )
    text = render_relationship_block(summary)
    assert text.startswith("【关系进展】\n当前阶段：")
    assert "（亲密等级 2/3）" in text
    assert "最近的变化：" in text
    assert "- 说好不熬夜" in text
    assert "相处边界：说好不熬夜" in text
    assert text.index("最近的变化：") < text.index("相处边界：")


async def test_render_relationship_block_is_deterministic() -> None:
    """确定性：同一摘要的两次渲染逐字节一致。"""
    summary = build_relationship_summary([_fact()], now_ms=_NOW_MS)
    assert render_relationship_block(summary) == render_relationship_block(summary)


async def test_render_relationship_block_enforces_char_cap() -> None:
    """硬字符帽：超长事件集在预算内截断且不追加后续内容。"""
    summary = build_relationship_summary(
        [
            _fact(index, content="甲" * 80, updated_at=_NOW_MS - index * _DAY_MS)
            for index in range(1, 6)
        ],
        now_ms=_NOW_MS,
    )
    text = render_relationship_block(summary, max_chars=120)
    assert len(text) == 120
    assert text.startswith("【关系进展】\n当前阶段：")
    assert text.endswith("- 甲甲甲")
    assert text.count("- ") == 2
    assert render_relationship_block(summary, max_chars=120) == text


async def test_render_relationship_block_truncates_stage_line_when_budget_tiny() -> None:
    """极小预算：阶段行按帽硬截断（总长恒不超预算）。"""
    summary = build_relationship_summary([_fact()], now_ms=_NOW_MS)
    text = render_relationship_block(summary, max_chars=10)
    assert text == "【关系进展】\n当前阶"


async def test_render_relationship_block_boundaries_without_events() -> None:
    """无事件但有边界：跳过事件段、照常渲染边界行。"""
    summary = build_relationship_summary(
        [_fact(content="不要翻旧账", updated_at=_NOW_MS)],
        now_ms=_NOW_MS,
    )
    summary = summary.model_copy(update={"recent_events": []})
    text = render_relationship_block(summary)
    assert "最近的变化：" not in text
    assert text.endswith("相处边界：不要翻旧账")
    assert RELATIONSHIP_BLOCK_MAX_CHARS == 1200


# ---------------------------------------------------------------------------
# compose 链位置
# ---------------------------------------------------------------------------


async def test_compose_inserts_relationship_between_core_memory_and_skills() -> None:
    """compose 链：关系进展块紧跟【自我认知】、位于【相关技能】之前。"""
    summary = build_relationship_summary([_fact()], now_ms=_NOW_MS)
    result = compose_messages(
        _state(),
        core_memory=[_core_block()],
        relationship=summary,
        skills=[_skill()], wrap_current_message=False
    )
    prompt = result.system_prompt
    core_index = prompt.index("【自我认知】")
    relationship_index = prompt.index("【关系进展】")
    skills_index = prompt.index("【相关技能】")
    assert core_index < relationship_index < skills_index
    assert relationship_index < prompt.index("【回复格式协议】")


async def test_compose_without_relationship_unchanged() -> None:
    """未提供摘要：system 提示不含关系进展块（既有形态零变更）。"""
    result = compose_messages(_state(), wrap_current_message=False)
    assert "【关系进展】" not in result.system_prompt


# ---------------------------------------------------------------------------
# 服务层——正常注入 / 降级 / 零 I/O
# ---------------------------------------------------------------------------


async def test_reply_injects_relationship_summary(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """持久化会话 + 事实读取面接线：关系类事实派生的摘要进入 system 提示。"""
    session_service, persona, db_session = chat_env
    facts_service = FactsService()
    async with bind_persona_context(persona.id):
        await facts_service.upsert_fact(
            db_session,
            FactCreateDTO(
                category=PersonaFactCategory.RELATIONSHIP,
                content="说好每天都要说晚安",
                confidence=0.9,
            ),
        )
    gateway = ScriptedGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=facts_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "晚安呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关系进展】" in system_prompt
    assert "当前阶段：熟悉（亲密等级 1/3）" in system_prompt
    assert "说好每天都要说晚安" in system_prompt
    assert "相处边界：" in system_prompt
    assert system_prompt.index("【关于对方】") < system_prompt.index("【关系进展】")


async def test_reply_relationship_threshold_injection(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """构造注入阈值表：熟悉下限调低后满置信事实进入熟悉段。"""
    session_service, persona, db_session = chat_env
    reader = ScriptedFactsReader([_fact(confidence=1.0)])
    gateway = ScriptedGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=reader,
        relationship_thresholds=RelationshipThresholdsModel(
            stage_familiar_min_score=0.5
        ),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "当前阶段：熟悉（亲密等级 1/3）" in system_prompt
    assert reader.queries[0].category == PersonaFactCategory.RELATIONSHIP
    assert reader.queries[0].status == PersonaFactStatus.ACTIVE


async def test_reply_relationship_read_failure_degrades_to_empty(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取异常：降级为空摘要（初识）且回复照常（增强面不阻断链路）。"""
    session_service, persona, db_session = chat_env
    reader = ScriptedFactsReader(error=RuntimeError("读取失败（测试注入）"))
    gateway = ScriptedGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=reader,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "早呀")

    assert content == "假回复"
    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关系进展】" in system_prompt
    assert "当前阶段：初识（亲密等级 0/3）" in system_prompt
    assert "最近的变化：" not in system_prompt


async def test_reply_without_facts_reader_skips_relationship_block() -> None:
    """读取面未接线：system 提示不含关系进展块（CLI / 直调形态零变更）。"""
    gateway = ScriptedGateway()
    service = ChatService(gateway=gateway)

    await service.reply(_state(), "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关系进展】" not in system_prompt


async def test_reply_memory_only_skips_relationship_read() -> None:
    """纯内存形态（无持久化会话）：读取面接线也不触库 / 不注入。"""
    gateway = ScriptedGateway()
    service = ChatService(gateway=gateway, facts_reader=ExplodingFactsReader())

    await service.reply(_state(), "早呀")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【关系进展】" not in system_prompt


# ---------------------------------------------------------------------------
# 分支一致性——流式 / 工具循环 / 合规重试
# ---------------------------------------------------------------------------


async def test_stream_reply_carries_relationship_block(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """流式路径：组装请求同样携带关系进展块。"""
    session_service, persona, db_session = chat_env
    reader = ScriptedFactsReader([_fact(content="主人开始叫我小鱼")])
    gateway = ScriptedGateway(stream_text="好呀")
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=reader,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    parts: list[str] = []
    async for chunk in service.stream_reply(state, "在吗"):
        parts.append(chunk)

    assert "".join(parts) == "好呀"
    system_prompt = gateway.stream_calls[0]["messages"][0].content
    assert "【关系进展】" in system_prompt
    assert "- 主人开始叫我小鱼" in system_prompt


async def test_tool_loop_carries_relationship_block(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """工具循环路径：循环内的网关请求基底同样携带关系进展块。"""
    session_service, persona, db_session = chat_env
    reader = ScriptedFactsReader([_fact(content="主人开始叫我小鱼")])
    gateway = ScriptedGateway(
        [
            ("", [ToolCallModel(id="c1", name="web_search", arguments={})]),
            ("搜到啦", None),
        ]
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=reader,
        tools=_tool_specs(),
        tool_executor=FakeToolExecutor(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "搜一下小鱼干")

    assert content == "搜到啦"
    assert len(gateway.chat_calls) == 2
    for call in gateway.chat_calls:
        assert "【关系进展】" in call["messages"][0].content
        assert "- 主人开始叫我小鱼" in call["messages"][0].content


async def test_compliance_retry_carries_relationship_block(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """合规重试路径：重试请求基底同样携带关系进展块。"""
    session_service, persona, db_session = chat_env
    reader = ScriptedFactsReader([_fact(content="主人开始叫我小鱼")])
    gateway = ScriptedGateway(
        [
            (_VIOLATING_TEXT, None),
            (_COMPLIANT_TEXT, None),
        ]
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=reader,
        compliance_settings=ComplianceGuardSettings(_env_file=None, enabled=True),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "今天好累呀")

    assert content == _COMPLIANT_TEXT
    assert len(gateway.chat_calls) == 2
    for call in gateway.chat_calls:
        assert "【关系进展】" in call["messages"][0].content
