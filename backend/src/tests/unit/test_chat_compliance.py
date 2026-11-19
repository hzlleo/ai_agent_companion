"""chat 域人格输出合规守卫接入测试——一次重试 / 开关 / 参数 / 组装位置。

测试策略：脚本化假网关（按调用序返回预设回复，记录组装消息与采样参数——
零真实网络）；直供人格记录建会话（不走 DB）。覆盖：

    - compose 链：角色契约紧跟宪章、先于记忆层与格式协议；空白人格名拒绝；
    - 重试：违规首次 + 合规重试 → 重试请求末尾携带角色提醒块、温度按基线
      提升、persona_id / purpose 直传不变，回复取重试文本；
    - 无循环：两次都违规 → 只发生一次重试，第二次结果无条件采用；
    - 开关：守卫停用 / 重试次数 0 / 合规首次回复 → 均只有一次网关调用；
    - 协议面：重试文本照常经输出协议解析（心声不入可见正文与回合）；
    - 工具循环分支：重试请求保留 assistant 工具调用轮与已包裹 tool 轮、
      不携带工具面（不重跑工具）；
    - 流式：stream_reply 不重试（单次流式调用，零非流式调用）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.compliance_models import ComplianceGuardSettings
from packages.harness.chat.prompts import (
    render_compliance_nudge,
    render_constitution,
    render_persona_contract,
)
from packages.harness.chat.service import ChatService, compose_messages
from packages.harness.llm.llm_models import (
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
    ToolCallModel,
    ToolParametersSchema,
)
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)

#: 典型失守回复（AI 自我声明——单条即达阈值）。
_VIOLATING_TEXT: str = "作为一个AI，我建议你早点休息。"

#: 合规回复（正常角色台词）。
_COMPLIANT_TEXT: str = "哼，那你还不快去睡，别在这儿磨蹭。"


class ScriptedGateway:
    """脚本化假网关——按调用序返回预设回复并记录请求。

    回复队列耗尽后重复最后一条（测试只关心前两次调用的形态）；工具面
    关键字参数原样记录（重试请求不携带工具面即记录为 None）。
    """

    def __init__(self, responses: Sequence[ChatResponseModel]) -> None:
        """初始化脚本队列与请求记录。

        Args:
            responses: 非流式回复序列（逐调用弹出）。
        """
        self._responses = list(responses)
        self.chat_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self,
        role: Any,
        messages: Sequence[Any],
        params: Any = None,
        **kwargs: Any,
    ) -> ChatResponseModel:
        """记录非流式请求并弹出下一条脚本回复。"""
        self.chat_calls.append(
            {
                "role": role,
                "messages": list(messages),
                "params": params,
                "kwargs": kwargs,
            }
        )
        index = min(len(self.chat_calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    async def stream_by_role(
        self,
        role: Any,
        messages: Sequence[Any],
        params: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """记录流式请求并逐块产出预设文本。"""
        self.stream_calls.append(
            {
                "role": role,
                "messages": list(messages),
                "params": params,
                "kwargs": kwargs,
            }
        )
        for part in ("作为", "一个AI", "…"):
            yield ChatStreamChunkModel(content=part)


class FakeToolExecutor:
    """假工具执行面（记录调用并返回成功载荷）。"""

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """记录调用并返回成功形态。"""
        return ToolResultModel(tool=call.name, ok=True, result={"items": []})


def _card(**overrides: Any) -> CharaCardModel:
    """组装测试人格卡片（字段可覆写）。"""
    fields: dict[str, Any] = {
        "name": "大肥鱼",
        "description": "一条圆滚滚的橘色胖鱼。",
        "personality": "元气、贪吃、爱撒娇。",
        "scenario": "周末午后的鱼缸边。",
        "first_mes": "你回来啦！",
    }
    fields.update(overrides)
    return CharaCardModel.model_validate(fields)


def _record() -> PersonaRecordDTO:
    """直供人格记录（不走 DB）。"""
    return PersonaRecordDTO(
        id=7,
        name="feiyu",
        display_name="大肥鱼",
        status="active",
        is_primary=True,
        card=_card(),
        created_at=1700000000000,
        updated_at=1700000000000,
    )


async def _make_state(
    gateway: ScriptedGateway,
    *,
    settings: ComplianceGuardSettings | None = None,
    tools: Sequence[ToolSpecModel] | None = None,
) -> tuple[ChatService, ChatSessionStateModel]:
    """按给定网关 / 配置构造服务与会话状态。"""
    service = ChatService(
        gateway=gateway,
        compliance_settings=settings
        if settings is not None
        else ComplianceGuardSettings(_env_file=None),
        tools=tools,
        tool_executor=FakeToolExecutor() if tools else None,
    )
    state = await service.create_session(persona=_record())
    return service, state


async def test_compose_injects_persona_contract_after_constitution() -> None:
    """组装位置：角色契约紧跟宪章，先于记忆层与输出格式协议。"""
    _, state = await _make_state(ScriptedGateway([ChatResponseModel(content="好")]))
    result = compose_messages(state, emotion_summary="心情不错")
    system = result.messages[0].content
    assert render_persona_contract("大肥鱼") in system
    assert system.index(render_constitution()) < system.index("【角色契约】")
    assert system.index("【角色契约】") < system.index("【心情状态】")
    assert system.index("【角色契约】") < system.index("【回复格式协议】")


async def test_render_persona_contract_rejects_blank_name() -> None:
    """空白人格名：装配错误（避免产出断句的契约文本）。"""
    with pytest.raises(ValueError):
        render_persona_contract("   ")
    with pytest.raises(ValueError):
        render_compliance_nudge("   ")


async def test_reply_retries_once_with_nudge_and_bumped_temperature() -> None:
    """违规首次回复：一次重试，末尾带角色提醒块、温度按基线提升。"""
    gateway = ScriptedGateway(
        [
            ChatResponseModel(content=_VIOLATING_TEXT),
            ChatResponseModel(content=_COMPLIANT_TEXT),
        ]
    )
    service, state = await _make_state(gateway)

    content = await service.reply(state, "今天好累呀")

    assert content == _COMPLIANT_TEXT
    assert len(gateway.chat_calls) == 2
    first, retry = gateway.chat_calls
    assert first["params"] is None
    retry_messages = retry["messages"]
    assert retry_messages[-1].role == ChatMessageRole.SYSTEM
    assert retry_messages[-1].content == render_compliance_nudge("大肥鱼")
    assert retry_messages[:-1] == first["messages"]
    assert retry["params"].temperature == pytest.approx(1.0)
    assert retry["kwargs"]["persona_id"] == "7"
    assert retry["kwargs"]["purpose"] == "chat"


async def test_reply_accepts_second_violation_without_loop() -> None:
    """两次都违规：只重试一次，第二次结果无条件采用（不循环）。"""
    gateway = ScriptedGateway(
        [
            ChatResponseModel(content=_VIOLATING_TEXT),
            ChatResponseModel(content="作为语言模型，我无法替你睡觉。"),
        ]
    )
    service, state = await _make_state(gateway)

    content = await service.reply(state, "今天好累呀")

    assert content == "作为语言模型，我无法替你睡觉。"
    assert len(gateway.chat_calls) == 2


async def test_reply_guard_disabled_keeps_single_call() -> None:
    """守卫停用：单次调用，违规回复原样返回。"""
    gateway = ScriptedGateway([ChatResponseModel(content=_VIOLATING_TEXT)])
    service, state = await _make_state(
        gateway, settings=ComplianceGuardSettings(_env_file=None, enabled=False)
    )

    content = await service.reply(state, "今天好累呀")

    assert content == _VIOLATING_TEXT
    assert len(gateway.chat_calls) == 1


async def test_reply_detection_without_retry_when_max_retries_zero() -> None:
    """重试次数 0：只记录检测、不重试（单次调用）。"""
    gateway = ScriptedGateway([ChatResponseModel(content=_VIOLATING_TEXT)])
    service, state = await _make_state(
        gateway, settings=ComplianceGuardSettings(_env_file=None, max_retries=0)
    )

    content = await service.reply(state, "今天好累呀")

    assert content == _VIOLATING_TEXT
    assert len(gateway.chat_calls) == 1


async def test_reply_compliant_first_response_skips_retry() -> None:
    """合规首次回复：不触发重试（单次调用，参数保持缺省）。"""
    gateway = ScriptedGateway([ChatResponseModel(content=_COMPLIANT_TEXT)])
    service, state = await _make_state(gateway)

    content = await service.reply(state, "今天好累呀")

    assert content == _COMPLIANT_TEXT
    assert len(gateway.chat_calls) == 1
    assert gateway.chat_calls[0]["params"] is None


async def test_reply_retry_runs_through_output_protocol() -> None:
    """重试文本照常走输出协议：心声不进可见正文 / 回合。"""
    gateway = ScriptedGateway(
        [
            ChatResponseModel(content=_VIOLATING_TEXT),
            ChatResponseModel(content="<心声>他终于肯去睡了。</心声>快去睡，晚安。"),
        ]
    )
    service, state = await _make_state(gateway)

    content = await service.reply(state, "今天好累呀")

    assert content == "快去睡，晚安。"
    assert state.turns[-1].content == "快去睡，晚安。"


async def test_reply_retry_uses_configured_bump() -> None:
    """温度增量按配置施加（基线 + 0.4 = 1.2）。"""
    gateway = ScriptedGateway(
        [
            ChatResponseModel(content=_VIOLATING_TEXT),
            ChatResponseModel(content=_COMPLIANT_TEXT),
        ]
    )
    service, state = await _make_state(
        gateway,
        settings=ComplianceGuardSettings(_env_file=None, temperature_bump=0.4),
    )

    await service.reply(state, "今天好累呀")

    assert gateway.chat_calls[1]["params"].temperature == pytest.approx(1.2)


async def test_tool_loop_retry_keeps_tool_evidence_without_tool_surface() -> None:
    """工具循环分支：重试保留工具轮证据与包裹，但不带工具面、不重跑工具。"""
    gateway = ScriptedGateway(
        [
            ChatResponseModel(
                content="",
                tool_calls=[ToolCallModel(id="c1", name="web_search", arguments={})],
            ),
            ChatResponseModel(content=_VIOLATING_TEXT),
            ChatResponseModel(content=_COMPLIANT_TEXT),
        ]
    )
    spec = ToolSpecModel(
        name="web_search",
        description="联网搜索",
        args_schema=ToolParametersSchema.model_validate(
            {"type": "object", "properties": {}, "required": []}
        ),
    )
    service, state = await _make_state(gateway, tools=[spec])

    content = await service.reply(state, "搜一下小鱼干")

    assert content == _COMPLIANT_TEXT
    assert len(gateway.chat_calls) == 3
    retry = gateway.chat_calls[2]
    retry_messages = retry["messages"]
    assert retry_messages[-1].content == render_compliance_nudge("大肥鱼")
    tool_rounds = [
        message for message in retry_messages if message.role == ChatMessageRole.TOOL
    ]
    assert len(tool_rounds) == 1
    assert tool_rounds[0].tool_call_id == "c1"
    assert tool_rounds[0].content.startswith("[[[内容开始 ")
    assistant_tool_calls = [
        message
        for message in retry_messages
        if message.role == ChatMessageRole.ASSISTANT and message.tool_calls
    ]
    assert len(assistant_tool_calls) == 1
    assistant_round = assistant_tool_calls[0]
    assert assistant_round.tool_calls is not None
    assert assistant_round.tool_calls[0].id == "c1"
    assert retry["kwargs"].get("tools") is None
    assert retry["params"].temperature == pytest.approx(1.0)


async def test_stream_reply_does_not_retry() -> None:
    """流式路径：不重试（单次流式调用，零非流式调用）。"""
    gateway = ScriptedGateway([ChatResponseModel(content=_VIOLATING_TEXT)])
    service, state = await _make_state(gateway)

    parts: list[str] = []
    async for chunk in service.stream_reply(state, "今天好累呀"):
        parts.append(chunk)

    assert "".join(parts) == "作为一个AI…"
    assert len(gateway.stream_calls) == 1
    assert gateway.chat_calls == []
