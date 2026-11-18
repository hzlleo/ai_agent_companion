"""chat 亲密通道路由输入测试——分支一致性与摘要缺失回退。

测试策略：脚本化事实读面产出关系摘要（高置信 / 近期事实 → 亲密等级），
脚本化网关记录每次调用收到的路由输入；真实持久化会话（共享 SQLite）只
用于满足「关系摘要读取面已落库」的运行判定——零真实网络。

覆盖：``reply`` / ``stream_reply`` / 工具循环（含收尾调用）/ 合规强化重试
全部携带同一 ``intimacy_level``；读取失败降级为空摘要时为 0；读取面未
接线时为 None（默认通道输入面）。
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
from packages.harness.chat.service import ChatService
from packages.harness.core.dtos import PageResponse
from packages.harness.core.time_utils import now_ms
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
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.facts_models import (
    FactQueryDTO,
    FactRecordDTO,
    PersonaFactCategory,
    PersonaFactSource,
    PersonaFactStatus,
)
from packages.harness.persona.service import PersonaService
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)

#: 典型助手腔回复（合规守卫命中）。
_VIOLATING_TEXT: str = "作为一个AI，我建议你早点休息。"

#: 合规角色台词。
_COMPLIANT_TEXT: str = "哼，那你还不快去睡，别在这儿磨蹭。"


class ScriptedFactsReader:
    """脚本化关系事实读面（返回固定分页或抛错）。"""

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
        """人物事实注入行（本测试不关心——空列表）。"""
        return []

    async def list_facts(
        self, session: AsyncSession, query: FactQueryDTO
    ) -> PageResponse[FactRecordDTO]:
        """记录查询并返回脚本分页（配置了异常时抛出）。"""
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return PageResponse(items=list(self._items), total=-1, page_size=query.limit)


class ScriptedGateway:
    """脚本化假网关——按调用序返回预设回复并记录路由输入。"""

    def __init__(
        self,
        responses: list[tuple[str, list[ToolCallModel] | None]] | None = None,
        *,
        stream_text: str = "亲密回复",
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
        self.chat_calls.append(
            {"role": role, "messages": list(messages), "params": params, **kwargs}
        )
        if self._responses:
            content, tool_calls = self._responses.pop(0)
            return ChatResponseModel(content=content, tool_calls=tool_calls or [])
        return ChatResponseModel(content="哼，知道啦。")

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


def _fact(
    fact_id: int = 1,
    *,
    content: str = "主人开始叫我小鱼",
    confidence: float = 1.0,
    updated_at: int | None = None,
) -> FactRecordDTO:
    """构造现行关系类事实记录（派生输入铺底）。"""
    ts = now_ms() if updated_at is None else updated_at
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


def _intimate_facts() -> list[FactRecordDTO]:
    """三条高置信近期关系事实（得分 30 → 亲密等级 3）。"""
    return [
        _fact(1, content="主人说喜欢我这样靠着他"),
        _fact(2, content="我们约定难过的时候要先抱一下"),
        _fact(3, content="主人开始叫我小鱼"),
    ]


def _tool_specs() -> list[ToolSpecModel]:
    """单工具面（工具循环分支输入）。"""
    return [
        ToolSpecModel(
            name="web_search",
            description="联网搜索",
            args_schema=ToolParametersSchema.model_validate(
                {"type": "object", "properties": {}, "required": []}
            ),
        )
    ]


async def _create_persona(
    db_session: AsyncSession, name: str = "intimate-chat"
) -> PersonaRecordDTO:
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


async def _service(
    gateway: Any,
    session_service: ChatSessionService,
    persona: PersonaRecordDTO,
    db_session: AsyncSession,
    *,
    facts_reader: Any = None,
    compliance_settings: ComplianceGuardSettings | None = None,
) -> tuple[ChatService, ChatSessionStateModel]:
    """构造接线后的服务与会话状态（关系摘要读取面已落库）。"""
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=facts_reader,
        compliance_settings=compliance_settings,
    )
    state = await service.create_session(persona=persona, db_session=db_session)
    return service, state


# ---------------------------------------------------------------------------
# 分支一致性
# ---------------------------------------------------------------------------


async def test_reply_passes_relationship_intimacy_level(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """非流式回复：路由输入为本轮关系摘要等级（3）。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway()
    service, state = await _service(
        gateway,
        session_service,
        persona,
        db_session,
        facts_reader=ScriptedFactsReader(_intimate_facts()),
    )

    reply = await service.reply(state, "今天好累")

    assert reply == "哼，知道啦。"
    assert len(gateway.chat_calls) == 1
    assert gateway.chat_calls[0]["intimacy_level"] == 3
    assert gateway.chat_calls[0]["purpose"] == "chat"


async def test_stream_reply_passes_relationship_intimacy_level(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """流式回复：同一路由输入进入 stream_by_role。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway(stream_text="抱抱")
    service, state = await _service(
        gateway,
        session_service,
        persona,
        db_session,
        facts_reader=ScriptedFactsReader(_intimate_facts()),
    )

    chunks = [chunk async for chunk in service.stream_reply(state, "想你了")]

    assert "".join(chunks) == "抱抱"
    assert len(gateway.stream_calls) == 1
    assert gateway.stream_calls[0]["intimacy_level"] == 3


async def test_tool_loop_passes_intimacy_level_on_every_step(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """工具循环：工具步与最终收尾调用携带同一路由输入。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway(
        responses=[
            (
                "",
                [ToolCallModel(id="call-1", name="web_search", arguments={"q": "天气"})],
            )
        ]
    )
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        facts_reader=ScriptedFactsReader(_intimate_facts()),
        tools=_tool_specs(),
        tool_executor=FakeToolExecutor(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    reply = await service.reply(state, "查一下今天天气")

    assert reply == "哼，知道啦。"
    assert len(gateway.chat_calls) == 2
    assert all(call["intimacy_level"] == 3 for call in gateway.chat_calls)
    assert all(call["purpose"] == "chat" for call in gateway.chat_calls)


async def test_compliance_retry_passes_intimacy_level(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """合规强化重试：重试请求沿用同一路由输入（不被换到另一条通道）。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway(responses=[(_VIOLATING_TEXT, None), (_COMPLIANT_TEXT, None)])
    service, state = await _service(
        gateway,
        session_service,
        persona,
        db_session,
        facts_reader=ScriptedFactsReader(_intimate_facts()),
        compliance_settings=ComplianceGuardSettings(
            enabled=True, threshold=2.0, temperature_bump=0.3, max_retries=1
        ),
    )

    reply = await service.reply(state, "在干嘛")

    assert reply == _COMPLIANT_TEXT
    assert len(gateway.chat_calls) == 2
    assert all(call["intimacy_level"] == 3 for call in gateway.chat_calls)


# ---------------------------------------------------------------------------
# 摘要缺失 / 降级
# ---------------------------------------------------------------------------


async def test_relationship_read_failure_degrades_to_level_zero(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取失败降级：空摘要等级 0（低于缺省阈值——网关判定默认通道）。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway()
    service, state = await _service(
        gateway,
        session_service,
        persona,
        db_session,
        facts_reader=ScriptedFactsReader(error=RuntimeError("facts down")),
    )

    await service.reply(state, "在吗")

    assert gateway.chat_calls[0]["intimacy_level"] == 0


async def test_without_facts_reader_passes_none(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取面未接线：路由输入为 None（默认通道输入面）。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway()
    service, state = await _service(gateway, session_service, persona, db_session)

    await service.reply(state, "在吗")

    assert gateway.chat_calls[0]["intimacy_level"] is None


async def test_memory_only_session_passes_none() -> None:
    """纯内存会话：零关系读取（路由输入 None——零附加 I/O）。"""
    gateway = ScriptedGateway()
    service = ChatService(gateway=gateway)
    state = ChatSessionStateModel(
        persona_id=1,
        persona_name="feiyu",
        display_name="大肥鱼",
        card=_card(),
    )

    await service.reply(state, "在吗")

    assert gateway.chat_calls[0]["intimacy_level"] is None


async def test_empty_facts_pass_level_zero(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """空关系事实：空摘要等级 0（照常注入关系块，路由输入为 0）。"""
    session_service, persona, db_session = chat_env
    gateway = ScriptedGateway()
    service, state = await _service(
        gateway, session_service, persona, db_session, facts_reader=ScriptedFactsReader()
    )

    await service.reply(state, "在吗")

    assert gateway.chat_calls[0]["intimacy_level"] == 0
