"""chat 域工具循环测试——ChatService 可选工具面（fake 网关 + fake 执行面）。

覆盖矩阵：

    - 装配错误：tools / tool_executor 未成对注入 → INTERNAL_ERROR；
    - 循环机械（镜像 duty 循环）：tool_calls 序列 → assistant 轮回放 +
      executor 执行 + tool 轮反馈（tool_call_id 配对）→ 最终 content；
    - 反馈正文：web_search 预格式化 text 直取 / 其余工具紧凑 JSON；
    - 步数上限：超限（5 步仍 tool_calls）→ 收尾指令 + 无工具面最终
      调用（final-report forcing——末次请求不携带 tools）；
    - 持久化不变：绑定持久化（真实 ChatSessionService + 共享 SQLite）时
      仅用户 + 最终 assistant 两行落库（工具步不落 ff_chat_messages）；
    - 网关参数：persona_id / purpose=chat / tools 透传；
    - tools=None 零变更回归：reply 单次调用 + 无 tools 参数。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.service import CHAT_TOOL_STEP_CAP, ChatService
from packages.harness.config.model_entry import ModelRole
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ToolCallModel,
)
from packages.harness.memory.orm import ChatMessageRow
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)


class FakeGateway:
    """fake LLM 网关——脚本化响应序列 + 请求记录。"""

    def __init__(self, responses: list[ChatResponseModel]) -> None:
        """初始化（响应队列——chat_by_role 逐调用弹出一个）。"""
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def chat_by_role(
        self,
        role: Any,
        messages: Any,
        params: Any = None,
        **kwargs: Any,
    ) -> ChatResponseModel:
        """记录请求并弹出下一响应。"""
        self.requests.append(
            {"role": role, "messages": list(messages), "kwargs": kwargs}
        )
        return self._responses.pop(0)


class FakeToolExecutor:
    """fake 工具执行面（ToolExecutorFace 结构满足）——记录调用。"""

    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        """初始化（工具名 → 结果载荷；未知工具 → ok=False）。"""
        self.results = results or {}
        self.calls: list[ToolCallRequestModel] = []

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """记录调用并返回脚本结果（未知工具失败形态）。"""
        self.calls.append(call)
        if call.name in self.results:
            return ToolResultModel(tool=call.name, ok=True, result=self.results[call.name])
        return ToolResultModel(tool=call.name, ok=False, error=f"未注册工具: {call.name}")


def _tool_call(call_id: str, name: str, arguments: dict[str, Any] | None = None) -> ToolCallModel:
    """构造工具调用声明。"""
    return ToolCallModel(id=call_id, name=name, arguments=arguments or {})


def _tool_response(
    calls: list[ToolCallModel], content: str = ""
) -> ChatResponseModel:
    """构造携带 tool_calls 的响应。"""
    return ChatResponseModel(content=content, tool_calls=calls)


def _text_response(content: str) -> ChatResponseModel:
    """构造纯文本响应。"""
    return ChatResponseModel(content=content)


def _state() -> ChatSessionStateModel:
    """构造最小会话状态（正典卡片 + 固定人格）。"""
    return ChatSessionStateModel(
        persona_id=7,
        persona_name="test-persona",
        display_name="测试人格",
        card=CharaCardModel(name="test-persona"),
    )


def _web_search_spec() -> ToolSpecModel:
    """构造 web_search 规格形态（ToolSpecModel——ChatService 入参面）。"""
    from packages.harness.llm.llm_models import ToolParametersSchema

    return ToolSpecModel(
        name="web_search",
        description="联网搜索",
        args_schema=ToolParametersSchema.model_validate(
            {"type": "object", "properties": {}, "required": []}
        ),
    )


class TestConstruction:
    async def test_tools_without_executor_rejected(self) -> None:
        """装配错误：tools 无 executor → INTERNAL_ERROR。"""
        with pytest.raises(BusinessException) as exc_info:
            ChatService(gateway=FakeGateway([]), tools=[_web_search_spec()])
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR

    async def test_executor_without_tools_rejected(self) -> None:
        """装配错误：executor 无 tools → INTERNAL_ERROR。"""
        with pytest.raises(BusinessException) as exc_info:
            ChatService(gateway=FakeGateway([]), tool_executor=FakeToolExecutor())
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR

    async def test_empty_tools_with_executor_rejected(self) -> None:
        """空列表 + executor：装配错误 fail loud（动态空工具面不应静默
        关闭已注入的执行面——成对契约严格判定）。"""
        with pytest.raises(BusinessException) as exc_info:
            ChatService(
                gateway=FakeGateway([]),
                tools=[],
                tool_executor=FakeToolExecutor(),
            )
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR


class TestToolLoopMechanics:
    async def test_tool_calls_then_final_content(self) -> None:
        """循环机械：tool_calls → 执行 + tool 轮反馈（id 配对）→ 最终文本。"""
        gateway = FakeGateway(
            [
                _tool_response([_tool_call("call-1", "web_search", {"query": "天气"})], "先查一下"),
                _text_response("今天晴。"),
            ]
        )
        executor = FakeToolExecutor(
            results={"web_search": {"count": 1, "text": "【1】天气\n链接：https://w.test"}}
        )
        service = ChatService(
            gateway=gateway, tools=[_web_search_spec()], tool_executor=executor
        )
        content = await service.reply(_state(), "今天天气如何")
        assert content == "今天晴。"

        assert len(gateway.requests) == 2
        second_messages = gateway.requests[1]["messages"]
        # 第二次请求含 assistant 工具调用轮回放 + tool 反馈轮（id 配对）
        assistant_replay = second_messages[-2]
        assert assistant_replay.role == ChatMessageRole.ASSISTANT
        assert assistant_replay.tool_calls is not None
        assert assistant_replay.tool_calls[0].id == "call-1"
        tool_feedback = second_messages[-1]
        assert tool_feedback.role == ChatMessageRole.TOOL
        assert tool_feedback.tool_call_id == "call-1"
        assert tool_feedback.content == "【1】天气\n链接：https://w.test"
        # 执行器收到配对调用
        assert len(executor.calls) == 1
        assert executor.calls[0].name == "web_search"
        assert executor.calls[0].arguments == {"query": "天气"}
        # 首次请求至少含 system + user 两消息（组装面未受工具面污染）
        assert len(gateway.requests[0]["messages"]) >= 2

    async def test_gateway_params_passthrough(self) -> None:
        """网关参数：persona_id / purpose=chat / tools 定义透传。"""
        gateway = FakeGateway(
            [
                _tool_response([_tool_call("c1", "web_search", {"query": "q"})]),
                _text_response("答"),
            ]
        )
        service = ChatService(
            gateway=gateway, tools=[_web_search_spec()], tool_executor=FakeToolExecutor(results={"web_search": {}})
        )
        await service.reply(_state(), "问")
        first = gateway.requests[0]
        assert first["role"] == ModelRole.CHAT
        assert first["kwargs"]["persona_id"] == "7"
        assert first["kwargs"]["purpose"] == "chat"
        assert [tool.name for tool in first["kwargs"]["tools"]] == ["web_search"]
        final = gateway.requests[1]
        assert final["kwargs"]["purpose"] == "chat"

    async def test_persistence_only_final_turns(self, shared_db_engine: AsyncEngine) -> None:
        """持久化不变：真实 ChatSessionService + 共享 SQLite——仅用户 +
        最终 assistant 两行落库（工具步不落 ff_chat_messages）。"""
        db_engine_holder.async_engine = shared_db_engine
        db_engine_holder.session_factory = async_sessionmaker(
            bind=shared_db_engine, expire_on_commit=False
        )
        db_engine_holder.owner_token = None
        try:
            gateway = FakeGateway(
                [
                    _tool_response([_tool_call("c1", "web_search", {"query": "q"})]),
                    _text_response("最终答案"),
                ]
            )
            service = ChatService(
                gateway=gateway,
                session_service=ChatSessionService(),
                tools=[_web_search_spec()],
                tool_executor=FakeToolExecutor(results={"web_search": {}}),
            )
            async with async_sessionmaker(
                bind=shared_db_engine, expire_on_commit=False
            )() as db_session:
                persona = await PersonaService().create(
                    db_session,
                    PersonaCreateDTO.model_validate(
                        {
                            "name": "tool-loop-persona",
                            "status": "active",
                            "card": {"name": "工具循环人格", "first_mes": "在"},
                        }
                    ),
                )
                state = await service.create_session(
                    persona=persona, db_session=db_session
                )
                content = await service.reply(state, "问题")
                assert content == "最终答案"
                count = int(
                    (
                        await db_session.execute(
                            select(func.count()).select_from(ChatMessageRow),
                            execution_options={"skip_persona_filter": True},
                        )
                    ).scalar_one()
                )
                assert count == 2
            assert [turn.role for turn in state.turns] == [
                ChatMessageRole.USER,
                ChatMessageRole.ASSISTANT,
            ]
        finally:
            db_engine_holder.reset()

    async def test_state_turns_excludes_tool_steps(self) -> None:
        """回合面：工具中间态不进入 state.turns（后续组装不可见）。"""
        gateway = FakeGateway(
            [
                _tool_response([_tool_call("c1", "web_search", {"query": "q"})]),
                _tool_response([_tool_call("c2", "web_search", {"query": "q2"})]),
                _text_response("好"),
            ]
        )
        service = ChatService(
            gateway=gateway, tools=[_web_search_spec()], tool_executor=FakeToolExecutor(results={"web_search": {}})
        )
        state = _state()
        await service.reply(state, "问")
        assert [turn.role for turn in state.turns] == [
            ChatMessageRole.USER,
            ChatMessageRole.ASSISTANT,
        ]
        # 第三次请求回放了两轮工具调用（消息面累积，回合面不累积）
        third_messages = gateway.requests[2]["messages"]
        tool_roles = [m.role for m in third_messages if m.role == ChatMessageRole.TOOL]
        assert len(tool_roles) == 2

    async def test_json_result_rendered_compact(self) -> None:
        """反馈正文：无预格式化 text 的工具结果整体紧凑 JSON。"""
        import json

        gateway = FakeGateway(
            [
                _tool_response([_tool_call("c1", "other_tool", {"x": 1})]),
                _text_response("答"),
            ]
        )
        executor = FakeToolExecutor(results={"other_tool": {"count": 3}})
        service = ChatService(
            gateway=gateway, tools=[_web_search_spec()], tool_executor=executor
        )
        await service.reply(_state(), "问")
        tool_feedback = gateway.requests[1]["messages"][-1]
        assert tool_feedback.role == ChatMessageRole.TOOL
        payload = json.loads(tool_feedback.content)
        assert payload["ok"] is True
        assert payload["result"] == {"count": 3}


class TestStepCap:
    async def test_step_cap_forces_final_report(self) -> None:
        """步数上限：连续 tool_calls 超限 → 收尾指令 + 无工具面最终调用。"""
        storm = [
            _tool_response([_tool_call(f"c{i}", "web_search", {"query": f"q{i}"})])
            for i in range(CHAT_TOOL_STEP_CAP)
        ]
        gateway = FakeGateway([*storm, _text_response("收尾回答")])
        executor = FakeToolExecutor(results={"web_search": {}})
        service = ChatService(
            gateway=gateway, tools=[_web_search_spec()], tool_executor=executor
        )
        content = await service.reply(_state(), "问")
        assert content == "收尾回答"
        # 恰好 CAP 步工具循环调用 + 1 次收尾调用
        assert len(gateway.requests) == CHAT_TOOL_STEP_CAP + 1
        final_request = gateway.requests[-1]
        assert "tools" not in final_request["kwargs"] or not final_request["kwargs"]["tools"]
        # 收尾指令以 system 追加块进入最终请求
        final_messages = final_request["messages"]
        assert final_messages[-1].role == ChatMessageRole.SYSTEM
        assert "收尾指令" in final_messages[-1].content
        # 循环步全部执行了工具
        assert len(executor.calls) == CHAT_TOOL_STEP_CAP

    async def test_single_tool_call_within_cap(self) -> None:
        """单步工具调用：不触发收尾（第二请求即纯文本收束）。"""
        gateway = FakeGateway(
            [
                _tool_response([_tool_call("c1", "web_search", {"query": "q"})]),
                _text_response("答案"),
            ]
        )
        service = ChatService(
            gateway=gateway,
            tools=[_web_search_spec()],
            tool_executor=FakeToolExecutor(results={"web_search": {}}),
        )
        content = await service.reply(_state(), "问")
        assert content == "答案"
        assert len(gateway.requests) == 2


class TestZeroToolRegression:
    async def test_tools_none_single_call_no_tools_param(self) -> None:
        """tools=None 零变更回归：单次调用 + kwargs 无 tools 键。"""
        gateway = FakeGateway([_text_response("直答")])
        service = ChatService(gateway=gateway)
        content = await service.reply(_state(), "问")
        assert content == "直答"
        assert len(gateway.requests) == 1
        assert "tools" not in gateway.requests[0]["kwargs"]

    async def test_empty_final_content_raises(self) -> None:
        """空最终回复：LLM_CALL_FAILED（与零工具形态同失败语义）。"""
        gateway = FakeGateway(
            [
                _tool_response([_tool_call("c1", "web_search", {"query": "q"})]),
                _text_response("   "),
            ]
        )
        service = ChatService(
            gateway=gateway,
            tools=[_web_search_spec()],
            tool_executor=FakeToolExecutor(results={"web_search": {}}),
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.reply(_state(), "问")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
