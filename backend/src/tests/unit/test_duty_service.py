"""harness/duty service 测试——值班工具循环全链 + fail-closed + 报告路由。

覆盖面：happy path（脚本化工具调用序列 → registry 隔离执行 → 最终报告
+ duty 事件落库形态 + 消息序列形状：assistant 工具调用轮 / tool 结果轮）；
步数上限强制收尾（末次调用不携带工具面 + 收尾指令在场）；网关风暴 →
status=error + error 事件落库 + 恒不抛；空最终报告 → failed；无激活人格
→ error 零副作用；属主投递矩阵（sent / failed / no-channel（路由器缺失
与适配器未注册两形态）/ no-owner / disabled）；落库失败不阻断返回。

全部经假网关 / 假 registry 执行器 / 假记忆服务 / fake 通道路由器
（零真实网络 / 零真实配置）。
"""

from __future__ import annotations

from typing import Any

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.duty.duty_models import (
    DutyOwnerDelivery,
    DutyRunConfigModel,
    DutyStatus,
)
from packages.harness.duty.duty_service import DUTY_PURPOSE, DUTY_SOURCE, DutyService
from packages.harness.llm.llm_models import ChatMessageRole, ChatResponseModel, ToolCallModel
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.models import DutyReportPayloadModel, EpisodeKind
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolSpecModel

#: 注入时钟（epoch 毫秒）。
NOW = 1_760_000_000_000


class FakePersonaRecord:
    """fake 激活人格记录（id / display_name 消费面）。"""

    def __init__(self, persona_id: int = 701, display_name: str = "大肥鱼") -> None:
        """初始化。"""
        self.id = persona_id
        self.display_name = display_name


#: 缺省激活人格（模块级单例——B008 参数默认值约束）。
_DEFAULT_ACTIVE_PERSONA = FakePersonaRecord()


class FakePersonaService:
    """fake 人格服务（激活人格可配置——None = 无激活）。"""

    def __init__(self, record: FakePersonaRecord | None = _DEFAULT_ACTIVE_PERSONA) -> None:
        """初始化（缺省 = 模块级单例激活人格；显式 None = 无激活）。"""
        self.record = record

    async def get_active_persona(self, session: Any) -> FakePersonaRecord | None:
        """返回配置的激活人格。"""
        return self.record


class ScriptedGateway:
    """脚本化假网关（逐次调用捕获 + 排队响应——含工具调用形态）。

    Attributes:
        calls: 逐次调用捕获（role / messages / tools / purpose / persona_id）。
    """

    def __init__(self, responses: list[ChatResponseModel]) -> None:
        """初始化（响应按序弹出；队列耗尽即测试装配错误）。"""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self,
        role: Any,
        messages: Any,
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
        tools: Any = None,
        tool_choice: Any = None,
    ) -> ChatResponseModel:
        """按角色调用（捕获参数；弹出下一个排队响应）。"""
        self.calls.append(
            {
                "role": role,
                "messages": list(messages),
                "persona_id": persona_id,
                "purpose": purpose,
                "tools": list(tools) if tools is not None else None,
            }
        )
        if not self._responses:
            raise AssertionError("假网关响应队列耗尽（调用次数超出装配）")
        return self._responses.pop(0)


class FailingGateway(ScriptedGateway):
    """恒抛错的假网关（风暴路径）。"""

    def __init__(self) -> None:
        """初始化（空响应队列——永不返回）。"""
        super().__init__([])

    async def chat_by_role(self, *args: Any, **kwargs: Any) -> ChatResponseModel:
        """捕获后抛业务异常（网关风暴注入）。"""
        self.calls.append({"args": args, "kwargs": kwargs})
        raise RuntimeError("网关风暴（测试注入）")


class FakeMemoryService:
    """fake 记忆服务（append_episode 调用捕获；可注入落库失败）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化。"""
        self.appended: list[EpisodeCreateDTO] = []
        self.fail = fail

    async def append_episode(
        self, session: Any, dto: EpisodeCreateDTO, *, dedup: bool = False
    ) -> Any:
        """记录创建 DTO（fail 模式抛异常——落库失败路径）。"""
        if self.fail:
            raise RuntimeError("落库失败（测试注入）")
        self.appended.append(dto)
        return dto


class FakeAdapter:
    """fake Telegram 适配器发送面（记录出站消息；可注入投递失败）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化。"""
        self.sent: list[OutboundMessageModel] = []
        self.fail = fail

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """记录出站消息（fail 模式抛异常）。"""
        if self.fail:
            raise RuntimeError("投递失败（测试注入）")
        self.sent.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.TELEGRAM, chat_id=message.chat_id, message_ids=["1"]
        )


def _fake_router(adapter: FakeAdapter | None = None) -> ChannelRouter:
    """构造 fake 通道路由器（真实 ChannelRouter + fake ChatService 工厂）。"""

    class _NullChatService:
        """占位 ChatService（出站链路不消费——仅路由器构造需要）。"""

    router = ChannelRouter(chat_service_factory=lambda: _NullChatService())  # type: ignore[arg-type]
    if adapter is not None:
        router.register_adapter(ChannelType.TELEGRAM, adapter)
    return router


def _registry_with_recorder() -> tuple[ToolRegistry, list[dict[str, Any]]]:
    """构造带记录执行器的 registry（echo 工具——参数原样回显）。"""
    executed: list[dict[str, Any]] = []

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """记录调用并回显。"""
        executed.append(args)
        return {"echo": args}

    registry = ToolRegistry()
    registry.register(
        ToolSpecModel(name="search_memory", description="检索记忆"),
        _execute,
    )
    return registry, executed


def _tool_call_response(
    name: str = "search_memory", arguments: dict[str, Any] | None = None
) -> ChatResponseModel:
    """构造携带单条工具调用的响应。"""
    return ChatResponseModel(
        content="",
        tool_calls=[
            ToolCallModel(id="call-1", name=name, arguments=arguments or {"query": "近况"})
        ],
        finish_reason="tool_calls",
    )


def _final_response(text: str = "今天主人心情不错，我陪他聊了几句。") -> ChatResponseModel:
    """构造纯文本最终响应。"""
    return ChatResponseModel(content=text, finish_reason="stop")


def _service(
    *,
    gateway: ScriptedGateway,
    registry: ToolRegistry,
    memory: FakeMemoryService | None = None,
    router: ChannelRouter | None = None,
    owner_chat_id: str | None = "10086",
    report_to_owner: bool = True,
) -> DutyService:
    """构造被测服务（依赖全注入）。"""
    return DutyService(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        memory_service=memory or FakeMemoryService(),  # type: ignore[arg-type]
        persona_service=FakePersonaService(),  # type: ignore[arg-type]
        channel_router=router,
        owner_chat_id=owner_chat_id,
        report_to_owner=report_to_owner,
    )


class _Session:
    """占位异步 Session（fake 服务不触达真实 DB）。"""


# ---------------------------------------------------------------------------
# happy path：工具循环 + 落库 + 投递
# ---------------------------------------------------------------------------


async def test_round_with_tool_calls_then_final_report() -> None:
    """脚本化序列：工具调用轮 → tool 结果反馈 → 最终报告 + 事件落库 + 投递。"""
    gateway = ScriptedGateway([_tool_call_response(), _final_response()])
    registry, executed = _registry_with_recorder()
    memory = FakeMemoryService()
    adapter = FakeAdapter()
    service = _service(
        gateway=gateway, registry=registry, memory=memory, router=_fake_router(adapter)
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.SUCCESS
    assert report.final_report == "今天主人心情不错，我陪他聊了几句。"
    assert report.persona == "701"
    assert report.persona_name == "大肥鱼"
    assert report.owner_delivery is DutyOwnerDelivery.SENT
    assert len(report.steps) == 1
    assert report.steps[0].call.name == "search_memory"
    assert report.steps[0].result.ok is True
    assert report.steps[0].result.result == {"echo": {"query": "近况"}}
    # 执行器收到 LLM 声明的参数
    assert executed == [{"query": "近况"}]
    # 消息序列形状：system 首块 → assistant 工具调用轮 → tool 结果轮
    second_call = gateway.calls[1]
    messages = second_call["messages"]
    assert messages[0].role is ChatMessageRole.SYSTEM
    assert messages[1].role is ChatMessageRole.ASSISTANT
    assert messages[1].tool_calls is not None and messages[1].tool_calls[0].name == "search_memory"
    assert messages[2].role is ChatMessageRole.TOOL
    assert messages[2].tool_call_id == "call-1"
    assert "【工具结果 · search_memory】" in messages[2].content
    # 工具面透传（首轮带工具，末轮纯文本响应）
    assert gateway.calls[0]["tools"] is not None and len(gateway.calls[0]["tools"]) == 1
    assert gateway.calls[0]["purpose"] == DUTY_PURPOSE
    assert gateway.calls[0]["persona_id"] == "701"
    # 事件落库形态：kind=duty / source / provenance=agent / 载荷投影
    assert len(memory.appended) == 1
    dto = memory.appended[0]
    assert dto.kind is EpisodeKind.DUTY
    assert dto.source == DUTY_SOURCE
    assert dto.provenance.value == "agent"
    assert dto.ts_ms == report.finished_ts_ms
    payload = dto.content
    assert isinstance(payload, DutyReportPayloadModel)
    assert payload.summary == report.final_report
    assert payload.title == "值班日报 · 大肥鱼"
    assert payload.details["status"] == "success"
    assert payload.details["tool_step_count"] == 1
    assert len(payload.details["steps"]) == 1
    # 属主投递形状
    assert [m.text for m in adapter.sent] == [report.final_report]
    assert adapter.sent[0].chat_id == "10086"


async def test_round_charter_override_and_name_substitution() -> None:
    """任务书覆写：cfg.charter_text 非空 → system 首块整体替换正典。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=None, owner_chat_id=None)
    cfg = DutyRunConfigModel(charter_text="自定义任务书（覆写）")
    await service.run_duty_round(_Session(), now_ms=NOW, cfg=cfg)
    system_prompt = gateway.calls[0]["messages"][0].content
    assert system_prompt.startswith("自定义任务书（覆写）")
    assert "【值班任务书】" not in system_prompt
    # 正典路径（无覆写）：人格名进入任务书
    gateway2 = ScriptedGateway([_final_response()])
    service2 = _service(gateway=gateway2, registry=registry, router=None, owner_chat_id=None)
    await service2.run_duty_round(_Session(), now_ms=NOW)
    assert "大肥鱼" in gateway2.calls[0]["messages"][0].content
    assert "【值班任务书】" in gateway2.calls[0]["messages"][0].content


# ---------------------------------------------------------------------------
# 步数上限：强制收尾
# ---------------------------------------------------------------------------


async def test_round_step_bound_forces_final_without_tools() -> None:
    """步数上限：max_tool_steps 轮工具调用后 → 收尾指令 + 无工具面最终调用。"""
    cfg = DutyRunConfigModel(max_tool_steps=2)
    storm_calls = [_tool_call_response(arguments={"query": f"q{i}"}) for i in range(2)]
    gateway = ScriptedGateway([*storm_calls, _final_response("收尾报告")])
    registry, executed = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=None, owner_chat_id=None)
    report = await service.run_duty_round(_Session(), now_ms=NOW, cfg=cfg)
    assert report.status is DutyStatus.SUCCESS
    assert report.final_report == "收尾报告"
    assert len(report.steps) == 2
    assert len(executed) == 2
    # 末次调用：无工具面 + 收尾指令在场
    final_call = gateway.calls[-1]
    assert final_call["tools"] is None
    final_messages = final_call["messages"]
    assert final_messages[-1].role is ChatMessageRole.SYSTEM
    assert "【收尾指令】" in final_messages[-1].content
    # 前两轮均携带工具面
    assert all(call["tools"] is not None for call in gateway.calls[:-1])


async def test_round_step_bound_with_still_tool_calls_counts_as_steps() -> None:
    """上限轮内每轮多条调用逐一执行（步数按轮计——steps 逐条记录）。"""
    multi_call = ChatResponseModel(
        content="",
        tool_calls=[
            ToolCallModel(id="c1", name="search_memory", arguments={"query": "a"}),
            ToolCallModel(id="c2", name="search_memory", arguments={"query": "b"}),
        ],
        finish_reason="tool_calls",
    )
    gateway = ScriptedGateway([multi_call, _final_response()])
    registry, executed = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=None, owner_chat_id=None)
    report = await service.run_duty_round(
        _Session(), now_ms=NOW, cfg=DutyRunConfigModel(max_tool_steps=3)
    )
    assert report.status is DutyStatus.SUCCESS
    assert len(report.steps) == 2
    assert executed == [{"query": "a"}, {"query": "b"}]


# ---------------------------------------------------------------------------
# fail-closed：风暴 / 空报告 / 无激活人格
# ---------------------------------------------------------------------------


async def test_round_gateway_storm_error_episode_and_never_raises() -> None:
    """网关风暴：status=error + error 事件落库 + 恒不抛（调度器零异常）。"""
    gateway = FailingGateway()
    registry, _ = _registry_with_recorder()
    memory = FakeMemoryService()
    adapter = FakeAdapter()
    service = _service(
        gateway=gateway, registry=registry, memory=memory, router=_fake_router(adapter)
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.ERROR
    assert report.error is not None and "值班轮异常中止" in report.error
    assert report.final_report  # 中止文案仍投递/落库（报告非空）
    assert report.steps == []
    assert len(memory.appended) == 1
    payload = memory.appended[0].content
    assert isinstance(payload, DutyReportPayloadModel)
    assert payload.details["status"] == "error"
    # 风暴路径仍投递中止文案（属主可知情）
    assert report.owner_delivery is DutyOwnerDelivery.SENT
    assert adapter.sent[0].text == report.final_report


async def test_round_empty_final_report_marks_failed() -> None:
    """空最终报告：status=failed + failed 事件落库（业务失败非异常）。"""
    gateway = ScriptedGateway([ChatResponseModel(content="   ", finish_reason="stop")])
    registry, _ = _registry_with_recorder()
    memory = FakeMemoryService()
    service = _service(gateway=gateway, registry=registry, memory=memory)
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.FAILED
    assert report.error == "最终报告为空"
    assert memory.appended[0].content.details["status"] == "failed"


async def test_round_no_active_persona_error_without_side_effects() -> None:
    """无激活人格：error 报告返回——零落库 / 零投递 / 恒不抛。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    memory = FakeMemoryService()
    adapter = FakeAdapter()
    service = DutyService(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        memory_service=memory,  # type: ignore[arg-type]
        persona_service=FakePersonaService(record=None),  # type: ignore[arg-type]
        channel_router=_fake_router(adapter),
        owner_chat_id="10086",
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.ERROR
    assert "无激活人格" in (report.error or "")
    assert report.persona == ""
    assert memory.appended == []
    assert adapter.sent == []
    assert gateway.calls == []


async def test_round_unknown_tool_recorded_as_failed_step() -> None:
    """模型调用未注册工具：registry 隔离 → failed step + tool 反馈轮照常。"""
    gateway = ScriptedGateway([_tool_call_response(name="nonexistent_tool"), _final_response()])
    registry, _ = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=None, owner_chat_id=None)
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.SUCCESS
    assert len(report.steps) == 1
    assert report.steps[0].result.ok is False
    assert "未注册工具" in (report.steps[0].result.error or "")
    # 失败结果仍作为 tool 反馈轮进入下一轮消息序列
    feedback = gateway.calls[1]["messages"][2]
    assert feedback.role is ChatMessageRole.TOOL
    assert '"ok":false' in feedback.content


# ---------------------------------------------------------------------------
# 属主投递矩阵
# ---------------------------------------------------------------------------


async def test_delivery_no_router_skipped_no_channel() -> None:
    """无路由器：skipped_no_channel（报告仅落记忆）。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=None)
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.owner_delivery is DutyOwnerDelivery.SKIPPED_NO_CHANNEL


async def test_delivery_router_without_adapter_skipped_no_channel() -> None:
    """路由器无 Telegram 适配器：skipped_no_channel（no-channel 路径）。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    service = _service(gateway=gateway, registry=registry, router=_fake_router(adapter=None))
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.owner_delivery is DutyOwnerDelivery.SKIPPED_NO_CHANNEL


async def test_delivery_no_owner_chat_id_skipped_no_owner() -> None:
    """属主会话未配置：skipped_no_owner。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    adapter = FakeAdapter()
    service = _service(
        gateway=gateway,
        registry=registry,
        router=_fake_router(adapter),
        owner_chat_id=None,
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.owner_delivery is DutyOwnerDelivery.SKIPPED_NO_OWNER
    assert adapter.sent == []


async def test_delivery_disabled_by_flag() -> None:
    """report_to_owner=False：disabled（通道与属主齐备也不投递）。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    adapter = FakeAdapter()
    service = _service(
        gateway=gateway,
        registry=registry,
        router=_fake_router(adapter),
        report_to_owner=False,
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.owner_delivery is DutyOwnerDelivery.DISABLED
    assert adapter.sent == []


async def test_delivery_adapter_failure_best_effort() -> None:
    """投递失败：owner_delivery=failed——轮次状态不受影响（success 保留）。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    adapter = FakeAdapter(fail=True)
    service = _service(gateway=gateway, registry=registry, router=_fake_router(adapter))
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.SUCCESS
    assert report.owner_delivery is DutyOwnerDelivery.FAILED


async def test_persist_failure_does_not_block_return() -> None:
    """落库失败：仅日志——轮次结果仍返回（fail-closed 不上抛）。"""
    gateway = ScriptedGateway([_final_response()])
    registry, _ = _registry_with_recorder()
    memory = FakeMemoryService(fail=True)
    adapter = FakeAdapter()
    service = _service(
        gateway=gateway, registry=registry, memory=memory, router=_fake_router(adapter)
    )
    report = await service.run_duty_round(_Session(), now_ms=NOW)
    assert report.status is DutyStatus.SUCCESS
    assert report.owner_delivery is DutyOwnerDelivery.SENT
