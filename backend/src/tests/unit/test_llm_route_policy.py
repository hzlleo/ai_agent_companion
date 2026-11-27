"""条件路由策略测试——条件矩阵 / 优先级 / 回落语义 / 内置亲密策略等价性。

测试策略：求值为纯函数直测（注入 ``now_ms``，零真实等待）；网关层用真实
``PoolManager``（隔离 Settings，共享 client 替换为 ``httpx.MockTransport``）
与按 host 分派的双端点 handler，对「内置亲密策略」与「等价通用策略」逐场景
对比请求字节 / 凭据 / 条目 / purpose / 用量归属，证明通用策略路径承接既有
亲密通道语义。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from loguru import logger
from pydantic import SecretStr, ValidationError

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.config.models import PoolsConfig
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.intimate_models import (
    INTIMATE_CHANNEL_ENTRY_NAME,
    IntimateChannelConfig,
)
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatRequestParams,
    LlmUsageEvent,
)
from packages.harness.llm.route_policy import evaluate_route, time_window_matches
from packages.harness.llm.route_policy_models import (
    INTIMATE_BUILTIN_POLICY_NAME,
    INTIMATE_BUILTIN_PRIORITY,
    INTIMATE_TARGET_ROLE,
    RouteConditionModel,
    RouteDecisionReason,
    RoutePolicyModel,
    TimeWindowModel,
)
from packages.harness.pools.manager import PoolManager

#: MockTransport handler 类型。
Handler = Callable[[httpx.Request], httpx.Response]

#: 默认端点基础 URL（注册表条目）。
_DEFAULT_BASE_URL: str = "http://llm.test/v1"

#: 路由目标端点基础 URL（与默认端点 host 不同——分派与断言锚点）。
_TARGET_BASE_URL: str = "http://intimate.test/v1"

#: 路由目标凭据（通用策略条目经环境变量解析；与内置通道配置值一致）。
_TARGET_API_KEY: str = "sk-intimate"

#: 通用策略目标条目的密钥环境变量名（测试经 monkeypatch 注入）。
_TARGET_API_KEY_ENV: str = "ROUTE_TARGET_API_KEY"


def _ok_response(content: str) -> httpx.Response:
    """合法非流式 200 响应。

    Args:
        content: 回复正文。

    Returns:
        OpenAI 兼容响应。
    """
    return httpx.Response(
        200,
        json={
            "id": "resp-1",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


def _sse_response() -> httpx.Response:
    """合法 SSE 流（两块内容 + 末块 usage + [DONE]）。"""
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"亲"},"finish_reason":null}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"近"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":6,"completion_tokens":4,"total_tokens":10}}',
        "data: [DONE]",
    ]
    return httpx.Response(
        200,
        content="\n".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
    )


class DualEndpointHandler:
    """按 host 分派并记录请求（默认 / 路由目标双端点）。"""

    def __init__(self, *, target_status: int = 200) -> None:
        """初始化记录容器与目标端点状态码。

        Args:
            target_status: 目标端点返回的状态码（非 200 用于失败语义测试）。
        """
        self.default_requests: list[httpx.Request] = []
        self.target_requests: list[httpx.Request] = []
        self._target_status = target_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """分派请求（目标 host → 目标记录；其余 → 默认记录）。"""
        is_stream = bool(json.loads(request.content).get("stream"))
        if str(request.url).startswith(_TARGET_BASE_URL):
            self.target_requests.append(request)
            if self._target_status != 200:
                return httpx.Response(self._target_status, json={"error": "boom"})
            return _sse_response() if is_stream else _ok_response("亲密回复")
        self.default_requests.append(request)
        return _sse_response() if is_stream else _ok_response("默认回复")


class RecordingUsageRecorder:
    """假用量记录器（记录事件）。"""

    def __init__(self) -> None:
        """初始化记录列表。"""
        self.events: list[LlmUsageEvent] = []

    async def __call__(self, event: LlmUsageEvent) -> None:
        """记录事件。"""
        self.events.append(event)


def _messages() -> list[ChatMessageModel]:
    """单条用户消息（请求字节对比的固定输入）。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="今天想抱抱")]


def _chat_entry() -> Any:
    """构造默认 chat 角色条目（注册表形态）。"""
    return parse_model_entry(
        {
            "name": "main-chat",
            "base_url": _DEFAULT_BASE_URL,
            "api_key_env": "",
            "model": "test-model",
            "roles": ["chat"],
        }
    )


def _target_entry(*, default_params: ChatRequestParams | None = None) -> Any:
    """构造与内置通道等价的通用策略目标条目（同名 / 同端点 / 同模型）。

    Args:
        default_params: 条目级缺省采样参数（与内置通道 temperature 等价）。

    Returns:
        路由目标条目（密钥经环境变量间接）。
    """
    return parse_model_entry(
        {
            "name": INTIMATE_CHANNEL_ENTRY_NAME,
            "base_url": _TARGET_BASE_URL,
            "api_key_env": _TARGET_API_KEY_ENV,
            "model": "intimate-model",
            "roles": [],
            "default_params": (
                default_params.model_dump(exclude_none=True) if default_params else None
            ),
        }
    )


def _policy(
    *,
    condition: RouteConditionModel,
    priority: int = 0,
    fallback_to_source: bool = True,
    target_role: str = INTIMATE_TARGET_ROLE,
) -> RoutePolicyModel:
    """构造源角色 chat 的通用策略（默认目标为等价的 intimate 别名）。

    Args:
        condition: 命中条件。
        priority: 优先级。
        fallback_to_source: 目标未绑定的回落语义。
        target_role: 目标角色名。

    Returns:
        条件路由策略。
    """
    return RoutePolicyModel(
        name="equivalence-policy",
        source_role="chat",
        target_role=target_role,
        priority=priority,
        condition=condition,
        fallback_to_source=fallback_to_source,
    )


def _complete_intimate_config(**overrides: Any) -> IntimateChannelConfig:
    """构造配置齐全的亲密通道（默认开启——调用方按需覆盖）。

    Args:
        **overrides: 字段覆盖值。

    Returns:
        完整配置实例。
    """
    payload: dict[str, Any] = {
        "enabled": True,
        "base_url": _TARGET_BASE_URL,
        "api_key": SecretStr(_TARGET_API_KEY),
        "model": "intimate-model",
        "temperature": None,
    }
    payload.update(overrides)
    return IntimateChannelConfig(**payload)


async def _make_pool_manager(handler: Handler) -> PoolManager:
    """构造已启动的 PoolManager（共享 client 替换为 MockTransport）。

    Args:
        handler: MockTransport handler。

    Returns:
        已启动的池管理器（调用方负责 ``await manager.stop()``）。
    """
    settings = build_isolated_settings(pools=PoolsConfig(cb_fail_max=2))
    manager = PoolManager(settings)
    await manager.start()
    original = manager.http_client
    manager.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if original is not None:
        await original.aclose()
    return manager


def _gateway(
    manager: PoolManager,
    *,
    resolver: ModelResolver,
    route_policies: list[RoutePolicyModel] | None = None,
    intimate_config: IntimateChannelConfig | None = None,
    usage_recorder: Any = None,
    clock: Callable[[], int] | None = None,
) -> LlmGateway:
    """构造注入网关（重试一次——失败语义测试不放大请求数）。

    Args:
        manager: 已启动池管理器。
        resolver: 模型解析器。
        route_policies: 用户策略。
        intimate_config: 亲密通道配置。
        usage_recorder: 用量记录器。
        clock: 时钟注入。

    Returns:
        网关实例。
    """
    kwargs: dict[str, Any] = {
        "resolver": resolver,
        "pool_manager": manager,
        "settings": build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        "usage_recorder": usage_recorder,
        "retry_attempts": 1,
        "retry_wait_seconds": 0.0,
        "route_policies": route_policies,
    }
    if intimate_config is not None:
        kwargs["intimate_config"] = intimate_config
    if clock is not None:
        kwargs["clock"] = clock
    return LlmGateway(**kwargs)


def _builtin_gateway(
    manager: PoolManager,
    *,
    recorder: Any = None,
    config: IntimateChannelConfig | None = None,
    clock: Callable[[], int] | None = None,
) -> LlmGateway:
    """构造仅含内置亲密策略的网关（解析器仅 chat 条目）。"""
    return _gateway(
        manager,
        resolver=ModelResolver([_chat_entry()]),
        intimate_config=config if config is not None else _complete_intimate_config(),
        usage_recorder=recorder,
        clock=clock,
    )


def _equivalent_gateway(
    manager: PoolManager,
    *,
    condition: RouteConditionModel,
    recorder: Any = None,
    fallback_to_source: bool = True,
    clock: Callable[[], int] | None = None,
    target_entry: Any | None = None,
) -> LlmGateway:
    """构造通用策略网关（目标条目与内置通道等价——同名 / 同端点 / 同模型）。"""
    resolver = ModelResolver([_chat_entry()])
    entry = target_entry if target_entry is not None else _target_entry()
    resolver.register_route_target(INTIMATE_TARGET_ROLE, entry)
    return _gateway(
        manager,
        resolver=resolver,
        route_policies=[
            _policy(condition=condition, fallback_to_source=fallback_to_source)
        ],
        usage_recorder=recorder,
        clock=clock,
    )


def _json_body(request: httpx.Request) -> dict[str, Any]:
    """解析请求体 JSON（字节断言辅助）。"""
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def _usage_signature(events: list[LlmUsageEvent]) -> list[tuple[str, str, str, int, int, int]]:
    """用量事件可比签名（剔除时间戳 / 时延等非确定性字段）。"""
    return [
        (
            event.model_entry,
            event.purpose,
            event.status,
            event.input_tokens,
            event.output_tokens,
            event.total_tokens,
        )
        for event in events
    ]


def _shanghai_epoch_ms(*, hour: int, minute: int = 0) -> int:
    """构造 Asia/Shanghai 指定时刻的 epoch 毫秒（固定日期——hermetic）。"""
    moment = datetime(2026, 1, 15, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(moment.timestamp() * 1000)


def _resolver_for_targets(*names: str) -> ModelResolver:
    """构造绑定多个目标角色的解析器（含 chat 条目）。"""
    resolver = ModelResolver([_chat_entry()])
    for index, name in enumerate(names):
        resolver.register_route_target(name, parse_model_entry(
            {
                "name": f"target-{index}",
                "base_url": _TARGET_BASE_URL,
                "api_key_env": "",
                "model": f"target-model-{index}",
                "roles": [],
            }
        ))
    return resolver


# ---------------------------------------------------------------------------
# 模型校验
# ---------------------------------------------------------------------------


async def test_condition_rejects_invalid_ranges_and_timezone() -> None:
    """条件模型校验：区间倒置 / 非法时区 / 越界小时均拒绝。"""
    with pytest.raises(ValidationError):
        RouteConditionModel(min_intimacy_level=3, max_intimacy_level=1)
    with pytest.raises(ValidationError):
        RouteConditionModel(min_intimacy_level=4)
    with pytest.raises(ValidationError):
        TimeWindowModel(start_hour=22, end_hour=6, timezone="Not/AZone")
    with pytest.raises(ValidationError):
        TimeWindowModel(start_hour=24, end_hour=6)


async def test_policy_rejects_same_role_and_empty_name() -> None:
    """策略模型校验：目标角色不得等于原角色；关键字段非空。"""
    with pytest.raises(ValidationError):
        RoutePolicyModel(
            name="x",
            source_role="chat",
            target_role="chat",
            condition=RouteConditionModel(),
        )
    with pytest.raises(ValidationError):
        RoutePolicyModel(
            name="  ",
            source_role="chat",
            target_role="night",
            condition=RouteConditionModel(),
        )


# ---------------------------------------------------------------------------
# 条件矩阵
# ---------------------------------------------------------------------------


def _intimacy_policy(**condition_kwargs: Any) -> RoutePolicyModel:
    """构造仅亲密度条件的策略。"""
    return _policy(condition=RouteConditionModel(**condition_kwargs))


def _evaluate(
    policies: list[RoutePolicyModel],
    *,
    level: int | None,
    now_ms: int = 0,
    has_summary: bool | None = None,
    purpose: str = "chat",
    resolver: ModelResolver | None = None,
):
    """求值辅助（默认摘要可用性与等级输入一致）。"""
    return evaluate_route(
        "chat",
        policies,
        intimacy_level=level,
        now_ms=now_ms,
        has_summary=(level is not None) if has_summary is None else has_summary,
        purpose=purpose,
        resolver=resolver or _resolver_for_targets(INTIMATE_TARGET_ROLE),
    )


async def test_intimacy_bounds_hit_and_miss() -> None:
    """亲密度上下沿：区间内命中；低于下沿 / 高于上沿不命中。"""
    policy = _intimacy_policy(min_intimacy_level=2, max_intimacy_level=3)
    assert _evaluate([policy], level=1).reason is RouteDecisionReason.DIRECT
    assert _evaluate([policy], level=2).reason is RouteDecisionReason.ROUTED
    assert _evaluate([policy], level=3).reason is RouteDecisionReason.ROUTED

    capped = _intimacy_policy(max_intimacy_level=1)
    assert _evaluate([capped], level=1).reason is RouteDecisionReason.ROUTED
    assert _evaluate([capped], level=2).reason is RouteDecisionReason.DIRECT


async def test_requires_summary_three_states() -> None:
    """摘要可用性三态：仅可用 / 仅不可用 / 不约束。"""
    require = _intimacy_policy(requires_summary=True, min_intimacy_level=0)
    assert (
        _evaluate([require], level=None, has_summary=False).reason
        is RouteDecisionReason.DIRECT
    )
    assert (
        _evaluate([require], level=2, has_summary=True).reason
        is RouteDecisionReason.ROUTED
    )

    forbid = _intimacy_policy(requires_summary=False)
    assert _evaluate([forbid], level=2, has_summary=True).reason is RouteDecisionReason.DIRECT
    assert _evaluate([forbid], level=None, has_summary=False).reason is RouteDecisionReason.ROUTED

    either = _intimacy_policy()
    assert _evaluate([either], level=2, has_summary=True).reason is RouteDecisionReason.ROUTED
    assert _evaluate([either], level=None, has_summary=False).reason is RouteDecisionReason.ROUTED


async def test_purposes_hit_and_miss() -> None:
    """对话类型白名单：命中用途路由；非白名单用途不命中。"""
    policy = _intimacy_policy(purposes=("duty", "dream"))
    assert _evaluate([policy], level=2, purpose="duty").reason is RouteDecisionReason.ROUTED
    assert _evaluate([policy], level=2, purpose="chat").reason is RouteDecisionReason.DIRECT


async def test_time_window_same_day_and_cross_midnight() -> None:
    """时段：同日窗口边界与跨午夜窗口（22→6）判定。"""
    same_day = TimeWindowModel(start_hour=9, end_hour=18, timezone="Asia/Shanghai")
    assert time_window_matches(same_day, now_ms=_shanghai_epoch_ms(hour=9)) is True
    assert time_window_matches(same_day, now_ms=_shanghai_epoch_ms(hour=17, minute=59)) is True
    assert time_window_matches(same_day, now_ms=_shanghai_epoch_ms(hour=18)) is False
    assert time_window_matches(same_day, now_ms=_shanghai_epoch_ms(hour=8)) is False

    cross = TimeWindowModel(start_hour=22, end_hour=6, timezone="Asia/Shanghai")
    assert time_window_matches(cross, now_ms=_shanghai_epoch_ms(hour=23)) is True
    assert time_window_matches(cross, now_ms=_shanghai_epoch_ms(hour=5, minute=59)) is True
    assert time_window_matches(cross, now_ms=_shanghai_epoch_ms(hour=6)) is False
    assert time_window_matches(cross, now_ms=_shanghai_epoch_ms(hour=12)) is False


async def test_time_window_timezone_changes_resolution() -> None:
    """时区：同一 epoch 在不同时区落到不同小时（UTC vs Asia/Shanghai）。"""
    epoch = _shanghai_epoch_ms(hour=23)
    shanghai = TimeWindowModel(start_hour=22, end_hour=6, timezone="Asia/Shanghai")
    utc = TimeWindowModel(start_hour=22, end_hour=6, timezone="UTC")
    assert time_window_matches(shanghai, now_ms=epoch) is True
    assert time_window_matches(utc, now_ms=epoch) is False


async def test_and_combination_requires_all_dimensions() -> None:
    """AND 组合：任一维度不满足即不命中。"""
    policy = _policy(
        condition=RouteConditionModel(
            min_intimacy_level=2,
            requires_summary=True,
            purposes=("chat",),
            time_window=TimeWindowModel(
                start_hour=22, end_hour=6, timezone="Asia/Shanghai"
            ),
        )
    )
    inside = _shanghai_epoch_ms(hour=23)
    assert (
        _evaluate([policy], level=2, now_ms=inside, purpose="chat").reason
        is RouteDecisionReason.ROUTED
    )
    assert (
        _evaluate([policy], level=1, now_ms=inside, purpose="chat").reason
        is RouteDecisionReason.DIRECT
    )
    assert (
        _evaluate([policy], level=2, now_ms=_shanghai_epoch_ms(hour=12), purpose="chat").reason
        is RouteDecisionReason.DIRECT
    )
    assert (
        _evaluate([policy], level=2, now_ms=inside, purpose="duty").reason
        is RouteDecisionReason.DIRECT
    )


async def test_no_policy_and_source_mismatch_are_direct() -> None:
    """无策略 / 策略源角色不匹配：直连原角色。"""
    assert _evaluate([], level=3).reason is RouteDecisionReason.DIRECT
    other = _intimacy_policy(min_intimacy_level=0)
    other = other.model_copy(update={"source_role": "duty", "target_role": INTIMATE_TARGET_ROLE})
    decision = _evaluate([other], level=3)
    assert decision.reason is RouteDecisionReason.DIRECT
    assert decision.effective_role == "chat"


async def test_priority_and_declaration_order() -> None:
    """优先级降序；同优先级按声明序（先声明者胜出）。"""
    low = RoutePolicyModel(
        name="low",
        source_role="chat",
        target_role="target-a",
        priority=1,
        condition=RouteConditionModel(),
    )
    high = RoutePolicyModel(
        name="high",
        source_role="chat",
        target_role="target-b",
        priority=10,
        condition=RouteConditionModel(),
    )
    resolver = _resolver_for_targets("target-a", "target-b")
    decision = _evaluate([low, high], level=2, resolver=resolver)
    assert decision.policy_name == "high"
    assert decision.effective_role == "target-b"

    first = RoutePolicyModel(
        name="first",
        source_role="chat",
        target_role="target-a",
        priority=5,
        condition=RouteConditionModel(),
    )
    second = RoutePolicyModel(
        name="second",
        source_role="chat",
        target_role="target-b",
        priority=5,
        condition=RouteConditionModel(),
    )
    tied = _evaluate([first, second], level=2, resolver=resolver)
    assert tied.policy_name == "first"


async def test_fallback_unbound_and_strict_unbound() -> None:
    """回落两态：允许回落 → 原角色直连；禁止回落 → MODEL_NOT_FOUND。"""
    resolver = ModelResolver([_chat_entry()])
    lenient = _policy(condition=RouteConditionModel(), fallback_to_source=True)
    decision = _evaluate([lenient], level=2, resolver=resolver)
    assert decision.reason is RouteDecisionReason.FALLBACK_UNBOUND
    assert decision.effective_role == "chat"
    assert decision.policy_name == "equivalence-policy"

    strict = _policy(condition=RouteConditionModel(), fallback_to_source=False)
    with pytest.raises(BusinessException) as exc_info:
        _evaluate([strict], level=2, resolver=resolver)
    assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
    assert exc_info.value.details is not None
    assert exc_info.value.details["policy"] == "equivalence-policy"


async def test_evaluate_rejects_out_of_range_level() -> None:
    """等级越出 0-3 即调用方契约破坏（显式拒绝，不静默夹取）。"""
    with pytest.raises(ValueError):
        _evaluate([_intimacy_policy()], level=4)


# ---------------------------------------------------------------------------
# 内置策略转译与等价性（与既有亲密通道场景逐项对照）
# ---------------------------------------------------------------------------


async def test_builtin_translation_attributes() -> None:
    """内置转译：策略名 / 源 / 目标 / 最低优先级 / 摘要要求 / 回落。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        gateway = _builtin_gateway(manager, config=_complete_intimate_config(min_intimacy_level=3))
    finally:
        await manager.stop()
    builtin = [
        policy for policy in gateway.route_policies if policy.name == INTIMATE_BUILTIN_POLICY_NAME
    ]
    assert len(builtin) == 1
    assert builtin[0].source_role == "chat"
    assert builtin[0].target_role == INTIMATE_TARGET_ROLE
    assert builtin[0].priority == INTIMATE_BUILTIN_PRIORITY
    assert builtin[0].condition.min_intimacy_level == 3
    assert builtin[0].condition.requires_summary is True
    assert builtin[0].fallback_to_source is True


@pytest.mark.parametrize("level", [None, 0, 1, 2, 3])
async def test_equivalence_builtin_vs_generic_route_decision(
    level: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """等价性：同一等级输入下内置策略与通用策略的通道选择 / 请求 / 用量一致。"""
    monkeypatch.setenv(_TARGET_API_KEY_ENV, _TARGET_API_KEY)
    condition = RouteConditionModel(min_intimacy_level=2, requires_summary=True)
    builtin_handler = DualEndpointHandler()
    generic_handler = DualEndpointHandler()
    builtin_manager = await _make_pool_manager(builtin_handler)
    generic_manager = await _make_pool_manager(generic_handler)
    builtin_recorder = RecordingUsageRecorder()
    generic_recorder = RecordingUsageRecorder()
    try:
        builtin = _builtin_gateway(builtin_manager, recorder=builtin_recorder)
        generic = _equivalent_gateway(
            generic_manager, condition=condition, recorder=generic_recorder
        )
        builtin_response = await builtin.chat_by_role("chat", _messages(), intimacy_level=level)
        generic_response = await generic.chat_by_role("chat", _messages(), intimacy_level=level)
    finally:
        await builtin_manager.stop()
        await generic_manager.stop()

    assert builtin_response.content == generic_response.content
    assert len(builtin_handler.default_requests) == len(generic_handler.default_requests)
    assert len(builtin_handler.target_requests) == len(generic_handler.target_requests)
    assert _usage_signature(builtin_recorder.events) == _usage_signature(generic_recorder.events)

    for left, right in zip(builtin_handler.default_requests, generic_handler.default_requests, strict=True):
        assert left.content == right.content
        assert left.headers == right.headers
    for left, right in zip(builtin_handler.target_requests, generic_handler.target_requests, strict=True):
        assert left.content == right.content
        assert left.headers["Authorization"] == right.headers["Authorization"]
        assert str(left.url) == str(right.url)


async def test_equivalence_disabled_and_incomplete_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等价性：开关关闭 / 半配置均直连（半配置仍记一条 warning）。"""
    monkeypatch.setenv(_TARGET_API_KEY_ENV, _TARGET_API_KEY)
    disabled_handler = DualEndpointHandler()
    generic_handler = DualEndpointHandler()
    disabled_manager = await _make_pool_manager(disabled_handler)
    generic_manager = await _make_pool_manager(generic_handler)
    try:
        disabled = _builtin_gateway(
            disabled_manager, config=_complete_intimate_config(enabled=False)
        )
        generic = _gateway(
            generic_manager,
            resolver=ModelResolver([_chat_entry()]),
        )
        await disabled.chat_by_role("chat", _messages(), intimacy_level=3)
        await generic.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await disabled_manager.stop()
        await generic_manager.stop()
    assert disabled_handler.target_requests == []
    assert disabled_handler.default_requests[0].content == generic_handler.default_requests[0].content

    incomplete_handler = DualEndpointHandler()
    incomplete_manager = await _make_pool_manager(incomplete_handler)
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        incomplete = _builtin_gateway(
            incomplete_manager,
            config=IntimateChannelConfig(
                enabled=True,
                base_url=_TARGET_BASE_URL,
                api_key=SecretStr("k"),
                model="",
                temperature=None,
            ),
        )
        await incomplete.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        logger.remove(handler_id)
        await incomplete_manager.stop()
    assert incomplete_handler.target_requests == []
    warnings = [message for message in messages if "亲密通道" in message]
    assert len(warnings) == 1
    assert "LLM_INTIMATE_MODEL" in warnings[0]


async def test_equivalence_temperature_and_independent_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等价性：通道缺省温度只补空缺；目标凭据独立于默认端点。"""
    monkeypatch.setenv(_TARGET_API_KEY_ENV, _TARGET_API_KEY)
    builtin_handler = DualEndpointHandler()
    generic_handler = DualEndpointHandler()
    builtin_manager = await _make_pool_manager(builtin_handler)
    generic_manager = await _make_pool_manager(generic_handler)
    condition = RouteConditionModel(min_intimacy_level=2, requires_summary=True)
    try:
        builtin = _builtin_gateway(
            builtin_manager, config=_complete_intimate_config(temperature=1.1)
        )
        generic = _equivalent_gateway(
            generic_manager,
            condition=condition,
            target_entry=_target_entry(default_params=ChatRequestParams(temperature=1.1)),
        )
        await builtin.chat_by_role("chat", _messages(), intimacy_level=3)
        await generic.chat_by_role("chat", _messages(), intimacy_level=3)
        await builtin.chat_by_role(
            "chat", _messages(), ChatRequestParams(temperature=0.4), intimacy_level=3
        )
        await generic.chat_by_role(
            "chat", _messages(), ChatRequestParams(temperature=0.4), intimacy_level=3
        )
    finally:
        await builtin_manager.stop()
        await generic_manager.stop()

    builtin_bodies = [_json_body(request) for request in builtin_handler.target_requests]
    generic_bodies = [_json_body(request) for request in generic_handler.target_requests]
    assert builtin_bodies == generic_bodies
    assert builtin_bodies[0]["temperature"] == 1.1
    assert builtin_bodies[1]["temperature"] == 0.4
    for request in builtin_handler.target_requests + generic_handler.target_requests:
        assert request.headers["Authorization"] == f"Bearer {_TARGET_API_KEY}"


async def test_equivalence_failure_does_not_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等价性：命中通道失败按既有错误上抛，两侧均不改走默认通道。"""
    monkeypatch.setenv(_TARGET_API_KEY_ENV, _TARGET_API_KEY)
    condition = RouteConditionModel(min_intimacy_level=2, requires_summary=True)
    builtin_handler = DualEndpointHandler(target_status=500)
    generic_handler = DualEndpointHandler(target_status=500)
    builtin_manager = await _make_pool_manager(builtin_handler)
    generic_manager = await _make_pool_manager(generic_handler)
    try:
        builtin = _builtin_gateway(builtin_manager)
        generic = _equivalent_gateway(generic_manager, condition=condition)
        with pytest.raises(BusinessException) as builtin_exc:
            await builtin.chat_by_role("chat", _messages(), intimacy_level=3)
        with pytest.raises(BusinessException) as generic_exc:
            await generic.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await builtin_manager.stop()
        await generic_manager.stop()
    assert builtin_exc.value.code == generic_exc.value.code == ErrorCode.LLM_CALL_FAILED
    assert len(builtin_handler.target_requests) == len(generic_handler.target_requests) == 1
    assert builtin_handler.default_requests == []
    assert generic_handler.default_requests == []


async def test_equivalence_stream_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """等价性：流式命中目标端点，增量块与请求字节一致。"""
    monkeypatch.setenv(_TARGET_API_KEY_ENV, _TARGET_API_KEY)
    condition = RouteConditionModel(min_intimacy_level=2, requires_summary=True)
    builtin_handler = DualEndpointHandler()
    generic_handler = DualEndpointHandler()
    builtin_manager = await _make_pool_manager(builtin_handler)
    generic_manager = await _make_pool_manager(generic_handler)
    try:
        builtin = _builtin_gateway(builtin_manager)
        generic = _equivalent_gateway(generic_manager, condition=condition)
        builtin_chunks = [
            chunk.content
            async for chunk in builtin.stream_by_role("chat", _messages(), intimacy_level=3)
        ]
        generic_chunks = [
            chunk.content
            async for chunk in generic.stream_by_role("chat", _messages(), intimacy_level=3)
        ]
    finally:
        await builtin_manager.stop()
        await generic_manager.stop()
    assert builtin_chunks == generic_chunks == ["亲", "近", ""]
    assert builtin_handler.default_requests == generic_handler.default_requests == []
    assert (
        builtin_handler.target_requests[0].content
        == generic_handler.target_requests[0].content
    )


# ---------------------------------------------------------------------------
# 网关接入：用户策略（时段 / 用途）/ 优先级覆盖 / 热重建补登记
# ---------------------------------------------------------------------------


async def test_gateway_time_window_uses_injected_clock() -> None:
    """网关时段路由：注入 now_ms 落在窗口内即改道，窗外直连（零真实等待）。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    policy = _policy(
        condition=RouteConditionModel(
            time_window=TimeWindowModel(
                start_hour=22, end_hour=6, timezone="Asia/Shanghai"
            )
        )
    )
    try:
        inside = _gateway(
            manager,
            resolver=_resolver_for_targets(INTIMATE_TARGET_ROLE),
            route_policies=[policy],
            clock=lambda: _shanghai_epoch_ms(hour=23),
        )
        outside = _gateway(
            manager,
            resolver=_resolver_for_targets(INTIMATE_TARGET_ROLE),
            route_policies=[policy],
            clock=lambda: _shanghai_epoch_ms(hour=12),
        )
        await inside.chat_by_role("chat", _messages())
        await outside.chat_by_role("chat", _messages())
    finally:
        await manager.stop()
    assert len(handler.target_requests) == 1
    assert len(handler.default_requests) == 1


async def test_gateway_user_policy_overrides_builtin_priority() -> None:
    """用户策略优先级高于内置策略时可覆盖（目标角色不同）。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    user_policy = RoutePolicyModel(
        name="user-override",
        source_role="chat",
        target_role="chat_night",
        priority=100,
        condition=RouteConditionModel(min_intimacy_level=0, requires_summary=True),
    )
    resolver = ModelResolver([_chat_entry()])
    resolver.register_route_target(
        "chat_night",
        parse_model_entry(
            {
                "name": "chat-night",
                "base_url": _DEFAULT_BASE_URL,
                "api_key_env": "",
                "model": "night-model",
                "roles": [],
            }
        ),
    )
    try:
        gateway = _gateway(
            manager,
            resolver=resolver,
            route_policies=[user_policy],
            intimate_config=_complete_intimate_config(),
        )
        await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()
    assert len(handler.target_requests) == 0
    assert _json_body(handler.default_requests[0])["model"] == "night-model"


async def test_gateway_purpose_condition_uses_resolved_purpose() -> None:
    """网关用途条件：按 resolved_purpose 判定（用途维度即对话类型配置面）。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    policy = _policy(condition=RouteConditionModel(purposes=("duty",)))
    try:
        gateway = _gateway(
            manager,
            resolver=_resolver_for_targets(INTIMATE_TARGET_ROLE),
            route_policies=[policy],
        )
        await gateway.chat_by_role("chat", _messages())
        await gateway.chat_by_role("chat", _messages(), purpose="duty")
    finally:
        await manager.stop()
    assert len(handler.default_requests) == 1
    assert len(handler.target_requests) == 1


async def test_gateway_registers_builtin_target_after_resolver_rebuild() -> None:
    """热重建后解析器丢失路由别名时，网关在下次调用前自动补登记。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    resolver = ModelResolver([_chat_entry()])
    try:
        gateway = _gateway(
            manager,
            resolver=resolver,
            intimate_config=_complete_intimate_config(),
        )
        resolver.rebuild([_chat_entry()])
        assert resolver.resolve_optional(INTIMATE_TARGET_ROLE) is None
        await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()
    assert len(handler.target_requests) == 1
