"""chat 核心记忆测试——渲染面 / compose 链位置 / 读取面接线 / 工具写入门。

覆盖面：``render_core_memory_block``（空跳过 / 标题与锁定标识 / 字符帽
截断）/ compose 链位置（人物事实之后、相关技能之前）/ ChatService 持久化
会话注入四块（全空跳过 / 读取面未接线 / 纯内存形态零 I/O / 读取失败
降级）/ 工具列表按发送者来源分级（owner 全量、peer 剔除
``edit_core_memory``）与执行面按可执行名集拦截（面外调用不进执行器）/
工具执行期的会话主键绑定与循环后复位。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.core_memory_orm as core_memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.prompts import render_core_memory_block
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
    ToolCallModel,
)
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.core_memory import (
    CoreMemoryService,
    core_memory_session_id_var,
)
from packages.harness.persona.core_memory_models import (
    CORE_MEMORY_TOOL_NAME,
    CoreBlockKey,
    CoreMemoryActor,
    CoreMemoryBlockDTO,
    OwnerEditModel,
)
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
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


class FakeGateway:
    """假 LLM 网关——记录组装消息与工具面（可脚本化工具调用响应）。"""

    def __init__(self, responses: list[tuple[str, list[ToolCallModel] | None]] | None = None) -> None:
        """初始化假网关（空脚本 = 恒 content-only 单次响应）。"""
        self.chat_calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])

    async def chat_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> ChatResponseModel:
        """非流式假调用（记录参数；脚本非空时按序消费）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        if self._responses:
            content, tool_calls = self._responses.pop(0)
            return ChatResponseModel(content=content, tool_calls=tool_calls or [])
        return ChatResponseModel(content="假回复")

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """流式假调用（单块产出——既有测试面同形态）。"""
        self.chat_calls.append({"role": role, "messages": list(messages), **kwargs})
        yield ChatStreamChunkModel(content="假回复")


class RecordingExecutor:
    """假工具执行器——记录调用与执行时刻的会话主键绑定。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.calls: list[ToolCallRequestModel] = []
        self.session_ids: list[int | None] = []

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """执行（记录工具名 + 会话主键；返回成功载荷）。"""
        self.calls.append(call)
        self.session_ids.append(core_memory_session_id_var.get())
        return ToolResultModel(
            tool=call.name,
            ok=True,
            result={"ok": True, "message": "已更新", "length": 3},
            latency_ms=0,
        )


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


def _block(
    block: CoreBlockKey,
    content: str,
    *,
    locked: bool = False,
) -> CoreMemoryBlockDTO:
    """构造最小核心记忆块记录（渲染测试铺底）。"""
    return CoreMemoryBlockDTO(
        id=1,
        block=block,
        content=content,
        locked=locked,
        version=1,
        updated_by=CoreMemoryActor.OWNER,
        last_edited_session_id=None,
        created_at=0,
        updated_at=0,
    )


def _tool_specs() -> list[ToolSpecModel]:
    """构造双工具面（核心记忆自编辑 + 无关工具——裁剪断言对照）。"""
    return [
        ToolSpecModel(name=CORE_MEMORY_TOOL_NAME, description="核心记忆自编辑"),
        ToolSpecModel(name="get_current_time", description="当前时间"),
    ]


async def _create_persona(db_session: AsyncSession, name: str = "core-chat") -> PersonaRecordDTO:
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


async def test_render_core_memory_block_empty_skips() -> None:
    """空列表 / 全空正文：返回空串（组装链跳过空块）。"""
    assert render_core_memory_block([]) == ""
    assert render_core_memory_block([_block(CoreBlockKey.SELF_IDENTITY, "   ")]) == ""


async def test_render_core_memory_block_headers_and_locked_marker() -> None:
    """非空块：名称标题 + 正文；锁定块标题带锁定标识；块间空行分隔。"""
    text = render_core_memory_block(
        [
            _block(CoreBlockKey.SELF_IDENTITY, "我是会写字的鱼"),
            _block(CoreBlockKey.OWNER_PROFILE, "主人喜欢乌龙茶", locked=True),
        ]
    )
    assert text.startswith("【自我认知】")
    assert "### 自我认知\n我是会写字的鱼" in text
    assert "### 关于对方（锁定）\n主人喜欢乌龙茶" in text
    assert "\n\n### 关于对方（锁定）" in text


async def test_render_core_memory_block_truncates_to_budget() -> None:
    """字符帽：总正文不超过预算；截断后不再追加后续块。"""
    text = render_core_memory_block(
        [
            _block(CoreBlockKey.SELF_IDENTITY, "甲" * 80),
            _block(CoreBlockKey.OPEN_NOTES, "乙" * 80),
        ],
        max_chars=40,
    )
    assert len(text) <= 40 + len("【自我认知】\n")
    assert "### 自我认知" in text
    assert "备忘" not in text
    assert text.endswith("甲")


async def test_compose_inserts_core_memory_between_facts_and_skills() -> None:
    """compose 链：核心记忆块位于人物事实块之后、相关技能块之前。"""
    skill = SkillRecordDTO(
        id=1,
        name="周报流程",
        slug="zhou-bao",
        description="写周报时使用",
        triggers=["写周报"],
        content="1. 先列三件事",
        status=SkillStatus.ACTIVE,
        source=SkillSource.MANUAL,
        confidence=0.8,
        use_count=0,
        last_used_ms=None,
        version=1,
        created_at=0,
        updated_at=0,
    )
    result = compose_messages(
        _state(),
        facts=["【偏好】主人喜欢无糖乌龙茶"],
        core_memory=[_block(CoreBlockKey.SELF_IDENTITY, "我是会写字的鱼")],
        skills=[skill], wrap_current_message=False
    )
    prompt = result.system_prompt
    facts_index = prompt.index("【关于对方】")
    core_index = prompt.index("【自我认知】")
    skills_index = prompt.index("【相关技能】")
    protocol_index = prompt.index("【回复格式协议】")
    assert facts_index < core_index < skills_index < protocol_index
    assert "### 自我认知\n我是会写字的鱼" in prompt


async def test_compose_without_core_memory_unchanged() -> None:
    """未提供核心记忆：system 提示不含该块（既有形态零变更）。"""
    result = compose_messages(_state(), wrap_current_message=False)
    assert "【自我认知】" not in result.system_prompt


# ---------------------------------------------------------------------------
# 服务层接线（持久化会话）
# ---------------------------------------------------------------------------


async def test_reply_injects_core_memory_blocks(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """持久化会话：四块非空内容进入本轮 system 提示。"""
    session_service, persona, db_session = chat_env
    core_service = CoreMemoryService()
    async with bind_persona_context(persona.id):
        await core_service.owner_update(
            db_session, CoreBlockKey.SELF_IDENTITY, OwnerEditModel(content="我是会写字的鱼")
        )
    gateway = FakeGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        core_memory_reader=core_service,
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "今天想聊点什么")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【自我认知】" in system_prompt
    assert "### 自我认知\n我是会写字的鱼" in system_prompt


async def test_reply_empty_blocks_skip_injection(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """四块全空：不注入核心记忆块（懒建后仍是空块）。"""
    session_service, persona, db_session = chat_env
    gateway = FakeGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        core_memory_reader=CoreMemoryService(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "今天想聊点什么")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【自我认知】" not in system_prompt


async def test_reply_without_reader_skips_block(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """未接线读取面：system 提示不含核心记忆块（既有行为零变更）。"""
    session_service, persona, db_session = chat_env
    gateway = FakeGateway()
    service = ChatService(gateway=gateway, session_service=session_service)
    state = await service.create_session(persona=persona, db_session=db_session)

    await service.reply(state, "今天想聊点什么")

    system_prompt = gateway.chat_calls[0]["messages"][0].content
    assert "【自我认知】" not in system_prompt


async def test_reply_memory_only_skips_core_memory_read() -> None:
    """纯内存形态（无持久化会话）：读取面接线也不触库 / 不注入。"""
    gateway = FakeGateway()

    class ExplodingCoreMemory(CoreMemoryService):
        """读取即炸的核心记忆服务（证明纯内存形态零调用）。"""

        async def list_blocks(self, session: AsyncSession) -> list[CoreMemoryBlockDTO]:
            raise AssertionError("纯内存形态不应读取核心记忆")

    service = ChatService(gateway=gateway, core_memory_reader=ExplodingCoreMemory())
    await service.reply(_state(session_id=None), "今天想聊点什么")
    assert "【自我认知】" not in gateway.chat_calls[0]["messages"][0].content


async def test_reply_core_memory_read_failure_degrades(
    chat_env: tuple[ChatSessionService, PersonaRecordDTO, AsyncSession],
) -> None:
    """读取异常：本轮不注入核心记忆块，回复照常（增强面不阻断链路）。"""
    session_service, persona, db_session = chat_env

    class BoomCoreMemory(CoreMemoryService):
        """读取恒抛的核心记忆服务（失败退化路径）。"""

        async def list_blocks(self, session: AsyncSession) -> list[CoreMemoryBlockDTO]:
            raise RuntimeError("读取失败（测试注入）")

    gateway = FakeGateway()
    service = ChatService(
        gateway=gateway,
        session_service=session_service,
        core_memory_reader=BoomCoreMemory(),
    )
    state = await service.create_session(persona=persona, db_session=db_session)

    content = await service.reply(state, "今天想聊点什么")

    assert content == "假回复"
    assert "【自我认知】" not in gateway.chat_calls[0]["messages"][0].content


# ---------------------------------------------------------------------------
# 工具写入门（发送者来源分级）
# ---------------------------------------------------------------------------


async def test_owner_turn_carries_edit_core_memory_tool() -> None:
    """主人回合：完整工具面进入网关请求。"""
    gateway = FakeGateway()
    executor = RecordingExecutor()
    service = ChatService(gateway=gateway, tools=_tool_specs(), tool_executor=executor)

    await service.reply(_state(session_id=42), "记住我喜欢乌龙茶")

    tool_names = [tool.name for tool in gateway.chat_calls[0]["tools"]]
    assert tool_names == [CORE_MEMORY_TOOL_NAME, "get_current_time"]


async def test_peer_turn_drops_edit_core_memory_tool() -> None:
    """群聊 peer 回合：工具列表结构性剔除自编辑工具（模型无从调用）。"""
    gateway = FakeGateway()
    executor = RecordingExecutor()
    service = ChatService(gateway=gateway, tools=_tool_specs(), tool_executor=executor)

    await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    tool_names = [tool.name for tool in gateway.chat_calls[0]["tools"]]
    assert tool_names == ["get_current_time"]


async def test_peer_turn_with_only_core_memory_tool_omits_tool_face() -> None:
    """仅核心记忆工具 + peer 回合：剔除后网关请求不携带工具面（None）。"""
    gateway = FakeGateway()
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=[ToolSpecModel(name=CORE_MEMORY_TOOL_NAME, description="核心记忆自编辑")],
        tool_executor=executor,
    )

    await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    assert gateway.chat_calls[0]["tools"] is None


async def test_empty_executable_face_still_blocks_out_of_face_call() -> None:
    """仅核心记忆工具 + peer 回合：可执行名集为空仍拦截面外回传，不进执行器。"""
    gateway = FakeGateway(
        responses=[
            ("", [ToolCallModel(id="call-1", name=CORE_MEMORY_TOOL_NAME, arguments={})]),
            ("好的", None),
        ]
    )
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=[ToolSpecModel(name=CORE_MEMORY_TOOL_NAME, description="核心记忆自编辑")],
        tool_executor=executor,
    )

    content = await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    assert content == "好的"
    assert gateway.chat_calls[0]["tools"] is None
    assert executor.calls == []
    tool_rounds = [
        message
        for message in gateway.chat_calls[1]["messages"]
        if message.role == ChatMessageRole.TOOL
    ]
    feedback = tool_rounds[-1].content
    assert ("未注册工具: " + CORE_MEMORY_TOOL_NAME) in feedback
    assert "可用" not in feedback


async def test_tool_loop_binds_and_resets_session_id() -> None:
    """工具执行期绑定会话主键（修订归属面），循环结束即复位。"""
    gateway = FakeGateway(
        responses=[
            (
                "",
                [ToolCallModel(id="call-1", name=CORE_MEMORY_TOOL_NAME, arguments={})],
            ),
            ("已记住", None),
        ]
    )
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=[ToolSpecModel(name=CORE_MEMORY_TOOL_NAME, description="核心记忆自编辑")],
        tool_executor=executor,
    )

    content = await service.reply(_state(session_id=42), "记住我喜欢乌龙茶")

    assert content == "已记住"
    assert executor.calls[0].name == CORE_MEMORY_TOOL_NAME
    assert executor.session_ids == [42]
    assert core_memory_session_id_var.get() is None


def _owner_only_specs() -> list[ToolSpecModel]:
    """三工具面（两个 owner-only + 一个 peer 可见——执行面拦截对照）。"""
    return [
        ToolSpecModel(name="calendar_query", description="查询日历"),
        ToolSpecModel(name="secret_tool", description="仅主人可用"),
        ToolSpecModel(name="get_current_time", description="当前时间"),
    ]


async def test_peer_turn_out_of_face_tool_call_blocked_before_executor() -> None:
    """peer 回合回传已剔除工具名：执行器零调用，反馈同「未注册」且不含清单。"""
    gateway = FakeGateway(
        responses=[
            ("", [ToolCallModel(id="call-1", name="calendar_query", arguments={})]),
            ("我看看", None),
        ]
    )
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=_owner_only_specs(),
        tool_executor=executor,
        owner_only_tools=frozenset({"calendar_query", "secret_tool"}),
    )

    content = await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    assert content == "我看看"
    assert executor.calls == []
    tool_rounds = [
        message
        for message in gateway.chat_calls[1]["messages"]
        if message.role == ChatMessageRole.TOOL
    ]
    feedback = tool_rounds[-1].content
    assert ("未注册工具: " + "calendar_query") in feedback
    assert "可用" not in feedback
    assert "secret_tool" not in feedback
    follow_up = gateway.chat_calls[1]
    assert [tool.name for tool in follow_up["tools"]] == ["get_current_time"]
    assert "secret_tool" not in "".join(
        message.content for message in follow_up["messages"]
    )


async def test_peer_turn_unknown_tool_feedback_has_no_available_list() -> None:
    """peer 回合回传未注册名：同一受控失败路径，反馈不带可用工具清单。"""
    gateway = FakeGateway(
        responses=[
            ("", [ToolCallModel(id="call-1", name="no_such_tool", arguments={})]),
            ("收到", None),
        ]
    )
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=_owner_only_specs(),
        tool_executor=executor,
        owner_only_tools=frozenset({"calendar_query", "secret_tool"}),
    )

    await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    assert executor.calls == []
    tool_rounds = [
        message
        for message in gateway.chat_calls[1]["messages"]
        if message.role == ChatMessageRole.TOOL
    ]
    feedback = tool_rounds[-1].content
    assert ("未注册工具: " + "no_such_tool") in feedback
    assert "可用" not in feedback
    assert "calendar_query" not in feedback
    assert "secret_tool" not in feedback


async def test_owner_turn_executes_owner_only_tool() -> None:
    """owner 回合不设可执行名集：owner-only 工具照常进入执行器。"""
    gateway = FakeGateway(
        responses=[
            ("", [ToolCallModel(id="call-1", name="calendar_query", arguments={})]),
            ("查到两条", None),
        ]
    )
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=_owner_only_specs(),
        tool_executor=executor,
        owner_only_tools=frozenset({"calendar_query", "secret_tool"}),
    )

    content = await service.reply(_state(session_id=42), "看看我的日程")

    assert content == "查到两条"
    assert [call.name for call in executor.calls] == ["calendar_query"]


async def test_peer_turn_step_cap_keeps_out_of_face_calls_out_of_executor() -> None:
    """收尾强制路径：步数上限后的无工具面最终调用不重放面外名到执行器。"""
    responses = [
        ("", [ToolCallModel(id=f"call-{index}", name="calendar_query", arguments={})])
        for index in range(5)
    ]
    responses.append(("没有更多了", None))
    gateway = FakeGateway(responses=responses)
    executor = RecordingExecutor()
    service = ChatService(
        gateway=gateway,
        tools=_owner_only_specs(),
        tool_executor=executor,
        owner_only_tools=frozenset({"calendar_query", "secret_tool"}),
    )

    content = await service.reply(
        _state(session_id=42), "群友路过", sender_provenance=MemoryProvenance.PEER
    )

    assert content == "没有更多了"
    assert executor.calls == []
    assert "tools" not in gateway.chat_calls[-1]
