"""亲密通道（roles:intimate）测试——配置面 / 路由矩阵 / 双网关端点选择 / 零行为变更。

测试策略：配置与裁决为纯函数直测（零 I/O）；网关层用真实 ``PoolManager``
（隔离 Settings，共享 client 替换为 ``httpx.MockTransport``——零真实出站）
与按 host 分派的双端点 handler，断言命中通道的 base_url / model / 凭据与
消息内容，同时对比未启用形态的请求字节。覆盖：

    - 配置面：``LLM_INTIMATE_*`` env 解析 / 必填项判定 / 缺项清单 / 越界校验；
    - 路由矩阵：开关 × 配置齐全 × 等级 0-3 / 摘要缺失 / 阈值覆盖；
    - 未启用零行为变更：默认通道请求体与从未提供亲密配置的网关逐字节一致；
    - 启用命中：端点 / 模型 / 凭据切换，消息与工具面不受影响，用量条目名
      为通道条目；
    - 流式命中、半配置单次 warning、调用失败不静默换道、温度合并优先级。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

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
from packages.harness.llm.intimate_router import build_intimate_entry
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatRequestParams,
    LlmUsageEvent,
    ToolDefinitionModel,
    ToolParametersSchema,
)
from packages.harness.llm.route_policy import evaluate_route
from packages.harness.llm.route_policy_models import (
    INTIMATE_BUILTIN_POLICY_NAME,
    INTIMATE_TARGET_ROLE,
    RouteDecisionModel,
    RouteDecisionReason,
)
from packages.harness.pools.manager import PoolManager

#: MockTransport handler 类型。
Handler = Callable[[httpx.Request], httpx.Response]

#: 亲密端点基础 URL（与默认端点 host 不同——分派与断言锚点）。
_INTIMATE_BASE_URL: str = "http://intimate.test/v1"

#: 默认端点基础 URL（注册表条目）。
_DEFAULT_BASE_URL: str = "http://llm.test/v1"

#: 亲密端点凭据（断言 Authorization 头与默认端点隔离）。
_INTIMATE_API_KEY: str = "sk-intimate"


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


def _entry() -> Any:
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


def _messages() -> list[ChatMessageModel]:
    """单条用户消息（组装面字节对比的固定输入）。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="今天想抱抱")]


def _tool_definition() -> ToolDefinitionModel:
    """单工具定义（工具面透传对照）。"""
    return ToolDefinitionModel(
        name="get_time",
        description="获取当前时间",
        parameters=ToolParametersSchema(type="object"),
    )


def _complete_config(**overrides: Any) -> IntimateChannelConfig:
    """构造配置齐全的亲密通道（默认开启——调用方按需覆盖）。

    Args:
        **overrides: 字段覆盖值。

    Returns:
        完整配置实例。
    """
    payload: dict[str, Any] = {
        "enabled": True,
        "base_url": _INTIMATE_BASE_URL,
        "api_key": SecretStr(_INTIMATE_API_KEY),
        "model": "intimate-model",
    }
    payload.update(overrides)
    return IntimateChannelConfig(**payload)


def _incomplete_config(*, enabled: bool = False) -> IntimateChannelConfig:
    """构造三项必填全缺的亲密通道（显式空值——不依赖进程环境）。

    Args:
        enabled: 总闸取值。

    Returns:
        必填项全缺、未配置的实例。
    """
    return IntimateChannelConfig(
        enabled=enabled,
        base_url="",
        api_key=SecretStr(""),
        model="",
        temperature=None,
    )


class RecordingUsageRecorder:
    """假用量记录器（记录事件）。"""

    def __init__(self) -> None:
        """初始化记录列表。"""
        self.events: list[LlmUsageEvent] = []

    async def __call__(self, event: LlmUsageEvent) -> None:
        """记录事件。"""
        self.events.append(event)


class DualEndpointHandler:
    """按 host 分派并记录请求（默认 / 亲密双端点）。"""

    def __init__(self, *, intimate_status: int = 200) -> None:
        """初始化记录容器与亲密端点状态码。

        Args:
            intimate_status: 亲密端点返回的状态码（非 200 用于失败语义测试）。
        """
        self.default_requests: list[httpx.Request] = []
        self.intimate_requests: list[httpx.Request] = []
        self._intimate_status = intimate_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """分派请求（亲密 host → 亲密记录；其余 → 默认记录）。"""
        is_stream = bool(_json_body(request).get("stream"))
        if str(request.url).startswith(_INTIMATE_BASE_URL):
            self.intimate_requests.append(request)
            if self._intimate_status != 200:
                return httpx.Response(self._intimate_status, json={"error": "boom"})
            return _sse_response() if is_stream else _ok_response("亲密回复")
        self.default_requests.append(request)
        return _sse_response() if is_stream else _ok_response("默认回复")


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
    intimate_config: IntimateChannelConfig,
    usage_recorder: Any = None,
) -> LlmGateway:
    """构造注入网关（重试一次——失败语义测试不放大请求数）。"""
    return LlmGateway(
        resolver=ModelResolver([_entry()]),
        pool_manager=manager,
        settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
        usage_recorder=usage_recorder,
        retry_attempts=1,
        retry_wait_seconds=0.0,
        intimate_config=intimate_config,
    )


def _json_body(request: httpx.Request) -> dict[str, Any]:
    """解析请求体 JSON（字节断言辅助）。"""
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def _builtin_policy_decision(
    config: IntimateChannelConfig,
    *,
    intimacy_level: int | None,
    purpose: str = "chat",
) -> RouteDecisionModel:
    """经通用求值面裁决内置亲密策略（装配期快照 + 解析器路由别名）。

    Args:
        config: 亲密通道配置（网关构造期转译为内置策略或保持无策略）。
        intimacy_level: 本轮关系摘要亲密等级（None = 摘要不可用）。
        purpose: 本轮调用用途（内置策略不约束用途）。

    Returns:
        通用路由裁决（生效角色 / 原因 / 求值轨迹）。
    """
    manager = PoolManager(build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)))
    gateway = _gateway(manager, intimate_config=config)
    resolver = gateway._resolver
    assert resolver is not None
    return evaluate_route(
        "chat",
        gateway._policies,
        intimacy_level=intimacy_level,
        now_ms=0,
        has_summary=intimacy_level is not None,
        purpose=purpose,
        resolver=resolver,
    )


# ---------------------------------------------------------------------------
# 配置面
# ---------------------------------------------------------------------------


async def test_config_defaults_disabled_and_incomplete() -> None:
    """缺省形态：关闭 + 三项必填全缺（不依赖环境——显式空值构造）。"""
    config = IntimateChannelConfig(
        enabled=False, base_url="", api_key=SecretStr(""), model="", temperature=None
    )
    assert config.enabled is False
    assert config.is_configured is False
    assert config.missing_env_keys == (
        "LLM_INTIMATE_BASE_URL",
        "LLM_INTIMATE_API_KEY",
        "LLM_INTIMATE_MODEL",
    )
    assert config.min_intimacy_level == 2


async def test_config_partial_missing_keys_matrix() -> None:
    """半配置矩阵：任一项缺失即未配置，缺项清单只列真实缺失项。"""
    assert IntimateChannelConfig(
        base_url="", api_key=SecretStr("k"), model="m"
    ).missing_env_keys == ("LLM_INTIMATE_BASE_URL",)
    assert IntimateChannelConfig(
        base_url=_INTIMATE_BASE_URL, api_key=SecretStr("  "), model="m"
    ).missing_env_keys == ("LLM_INTIMATE_API_KEY",)
    assert IntimateChannelConfig(
        base_url=_INTIMATE_BASE_URL, api_key=SecretStr("k"), model=""
    ).missing_env_keys == ("LLM_INTIMATE_MODEL",)
    assert _complete_config().is_configured is True


async def test_config_strips_whitespace_and_rejects_out_of_range() -> None:
    """首尾空白归一（含密钥）；温度 / 阈值越界由模型校验拒绝。"""
    config = _complete_config(
        base_url="  http://intimate.test/v1  ",
        api_key=SecretStr("  key-trimmed  "),
        model=" m ",
    )
    assert config.base_url == _INTIMATE_BASE_URL
    assert config.api_key.get_secret_value() == "key-trimmed"
    assert config.model == "m"

    blank_key = _complete_config(api_key=SecretStr("   "))
    assert blank_key.is_configured is False
    assert blank_key.missing_env_keys == ("LLM_INTIMATE_API_KEY",)

    with pytest.raises(ValidationError):
        IntimateChannelConfig(temperature=2.5)
    with pytest.raises(ValidationError):
        IntimateChannelConfig(min_intimacy_level=4)


async def test_config_reads_intimate_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 面解析：LLM_INTIMATE_* 六键映射到配置字段（全量显式设置）。"""
    monkeypatch.setenv("LLM_INTIMATE_ENABLED", "true")
    monkeypatch.setenv("LLM_INTIMATE_BASE_URL", _INTIMATE_BASE_URL)
    monkeypatch.setenv("LLM_INTIMATE_API_KEY", _INTIMATE_API_KEY)
    monkeypatch.setenv("LLM_INTIMATE_MODEL", "intimate-model")
    monkeypatch.setenv("LLM_INTIMATE_TEMPERATURE", "1.1")
    monkeypatch.setenv("LLM_INTIMATE_MIN_INTIMACY_LEVEL", "3")

    config = IntimateChannelConfig()
    assert config.enabled is True
    assert config.is_configured is True
    assert config.api_key.get_secret_value() == _INTIMATE_API_KEY
    assert config.temperature == 1.1
    assert config.min_intimacy_level == 3


# ---------------------------------------------------------------------------
# 路由矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "complete", "level", "expected_intimate"),
    [
        (False, True, 3, False),
        (False, False, 3, False),
        (True, False, 3, False),
        (True, False, None, False),
        (True, True, None, False),
        (True, True, 0, False),
        (True, True, 1, False),
        (True, True, 2, True),
        (True, True, 3, True),
    ],
)
async def test_route_matrix(
    enabled: bool,
    complete: bool,
    level: int | None,
    expected_intimate: bool,
) -> None:
    """路由矩阵：开关 × 配置齐全 × 等级 0-3（含摘要缺失）逐格裁决。"""
    config = _complete_config(enabled=enabled) if complete else _incomplete_config(enabled=enabled)
    decision = _builtin_policy_decision(config, intimacy_level=level)
    if expected_intimate:
        assert decision.reason is RouteDecisionReason.ROUTED
        assert decision.effective_role == INTIMATE_TARGET_ROLE
        assert decision.policy_name == INTIMATE_BUILTIN_POLICY_NAME
    else:
        assert decision.reason is RouteDecisionReason.DIRECT
        assert decision.effective_role == "chat"
        assert decision.policy_name is None


async def test_route_threshold_override_matrix() -> None:
    """阈值配置化：min=3 时等级 2 回退、等级 3 命中；min=0 时等级 0 命中。"""
    strict = _complete_config(min_intimacy_level=3)
    assert _builtin_policy_decision(strict, intimacy_level=2).reason is RouteDecisionReason.DIRECT
    routed = _builtin_policy_decision(strict, intimacy_level=3)
    assert routed.reason is RouteDecisionReason.ROUTED
    assert routed.effective_role == INTIMATE_TARGET_ROLE

    loose = _complete_config(min_intimacy_level=0)
    assert _builtin_policy_decision(loose, intimacy_level=0).reason is RouteDecisionReason.ROUTED


async def test_route_rejects_out_of_range_level() -> None:
    """等级越出 0-3 即调用方契约破坏（显式拒绝，不静默夹取）。"""
    with pytest.raises(ValueError):
        _builtin_policy_decision(_complete_config(), intimacy_level=4)


async def test_build_intimate_entry_requires_complete_config() -> None:
    """条目构建：完整配置产出固定条目键；半配置拒绝（INTERNAL_ERROR）。"""
    entry = build_intimate_entry(_complete_config())
    assert entry.name == INTIMATE_CHANNEL_ENTRY_NAME
    assert entry.base_url == _INTIMATE_BASE_URL
    assert entry.model == "intimate-model"
    assert entry.api_key_env == ""

    with pytest.raises(BusinessException) as exc_info:
        build_intimate_entry(_incomplete_config(enabled=True))
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR


async def test_intimate_request_params_merge_priority() -> None:
    """温度合并：通道缺省值只补调用方未设置的温度，显式参数优先。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        warm = _gateway(manager, intimate_config=_complete_config(temperature=1.1))
        await warm.chat_by_role("chat", _messages(), intimacy_level=3)
        await warm.chat_by_role(
            "chat", _messages(), ChatRequestParams(max_tokens=32), intimacy_level=3
        )
        await warm.chat_by_role(
            "chat", _messages(), ChatRequestParams(temperature=0.4), intimacy_level=3
        )
        plain = _gateway(manager, intimate_config=_complete_config(temperature=None))
        await plain.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()

    bodies = [_json_body(request) for request in handler.intimate_requests]
    assert bodies[0]["temperature"] == 1.1
    assert bodies[1]["temperature"] == 1.1
    assert bodies[1]["max_tokens"] == 32
    assert bodies[2]["temperature"] == 0.4
    assert "temperature" not in bodies[3]


# ---------------------------------------------------------------------------
# 网关：未启用零行为变更
# ---------------------------------------------------------------------------


async def test_disabled_channel_keeps_request_bytes_identical() -> None:
    """未启用：含完整配置但开关关闭的请求体与从未配置的网关逐字节一致。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        baseline = _gateway(manager, intimate_config=IntimateChannelConfig())
        disabled = _gateway(manager, intimate_config=_complete_config(enabled=False))
        await baseline.chat_by_role("chat", _messages(), intimacy_level=3)
        await disabled.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()

    assert handler.intimate_requests == []
    assert len(handler.default_requests) == 2
    first, second = handler.default_requests
    assert first.url == second.url
    assert first.content == second.content
    assert first.headers == second.headers


async def test_incomplete_config_falls_back_and_warns_once() -> None:
    """半配置：回退默认通道，装配期记一条 warning（不逐回合刷屏）。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        gateway = _gateway(
            manager,
            intimate_config=IntimateChannelConfig(
                enabled=True,
                base_url=_INTIMATE_BASE_URL,
                api_key=SecretStr("k"),
                model="",
                temperature=None,
            ),
        )
        await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
        await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        logger.remove(handler_id)
        await manager.stop()

    assert handler.intimate_requests == []
    assert len(handler.default_requests) == 2
    warnings = [message for message in messages if "亲密通道" in message]
    assert len(warnings) == 1
    assert "LLM_INTIMATE_MODEL" in warnings[0]


async def test_missing_summary_and_below_threshold_use_default_channel() -> None:
    """摘要缺失 / 等级不足：均走默认通道（端点与模型不变）。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(manager, intimate_config=_complete_config())
        await gateway.chat_by_role("chat", _messages(), intimacy_level=None)
        await gateway.chat_by_role("chat", _messages(), intimacy_level=1)
    finally:
        await manager.stop()

    assert handler.intimate_requests == []
    assert len(handler.default_requests) == 2
    for request in handler.default_requests:
        assert _json_body(request)["model"] == "test-model"


# ---------------------------------------------------------------------------
# 网关：启用命中
# ---------------------------------------------------------------------------


async def test_enabled_and_threshold_met_routes_to_intimate_endpoint() -> None:
    """命中：端点 / 模型 / 凭据切换到亲密通道，消息与工具面原样透传。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    recorder = RecordingUsageRecorder()
    tools = [_tool_definition()]
    try:
        default_gateway = _gateway(manager, intimate_config=IntimateChannelConfig())
        intimate_gateway = _gateway(
            manager, intimate_config=_complete_config(), usage_recorder=recorder
        )
        default_response = await default_gateway.chat_by_role(
            "chat", _messages(), tools=tools, intimacy_level=None
        )
        response = await intimate_gateway.chat_by_role(
            "chat", _messages(), tools=tools, intimacy_level=2
        )
    finally:
        await manager.stop()

    assert default_response.content == "默认回复"
    assert response.content == "亲密回复"
    assert len(handler.default_requests) == 1
    assert len(handler.intimate_requests) == 1

    intimate_request = handler.intimate_requests[0]
    assert str(intimate_request.url) == f"{_INTIMATE_BASE_URL}/chat/completions"
    assert intimate_request.headers["Authorization"] == f"Bearer {_INTIMATE_API_KEY}"
    intimate_body = _json_body(intimate_request)
    assert intimate_body["model"] == "intimate-model"
    assert intimate_body["messages"] == _json_body(handler.default_requests[0])["messages"]
    assert intimate_body["tools"] == _json_body(handler.default_requests[0])["tools"]

    assert len(recorder.events) == 1
    assert recorder.events[0].model_entry == INTIMATE_CHANNEL_ENTRY_NAME
    assert recorder.events[0].purpose == "chat"
    assert recorder.events[0].status == "success"


async def test_alias_conflict_keeps_yaml_entry_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """intimate 别名改绑到 yaml 条目：请求与凭据均按该条目，不用通道密钥。"""
    monkeypatch.setenv("YAML_INTIMATE_KEY", "sk-yaml-entry")
    yaml_entry = parse_model_entry(
        {
            "name": "yaml-intimate",
            "base_url": "http://yaml-intimate.test/v1",
            "api_key_env": "YAML_INTIMATE_KEY",
            "model": "yaml-model",
        }
    )
    resolver = ModelResolver([_entry(), yaml_entry])
    resolver.register_route_target(INTIMATE_TARGET_ROLE, yaml_entry)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录请求并返回固定回复。"""
        requests.append(request)
        return _ok_response("yaml 回复")

    manager = await _make_pool_manager(handler)
    try:
        gateway = LlmGateway(
            resolver=resolver,
            pool_manager=manager,
            settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
            retry_attempts=1,
            retry_wait_seconds=0.0,
            intimate_config=_complete_config(),
        )
        response = await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()

    assert response.content == "yaml 回复"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://yaml-intimate.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-yaml-entry"
    assert _json_body(request)["model"] == "yaml-model"


async def test_same_name_admin_entry_keeps_own_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admin 建同名条目绑 intimate 别名：凭据按条目 api_key_env，不套通道密钥。"""
    monkeypatch.setenv("SAME_NAME_KEY", "sk-admin-entry")
    admin_entry = parse_model_entry(
        {
            "name": INTIMATE_CHANNEL_ENTRY_NAME,
            "base_url": "http://same-name.test/v1",
            "api_key_env": "SAME_NAME_KEY",
            "model": "admin-model",
        }
    )
    resolver = ModelResolver([_entry(), admin_entry])
    resolver.register_route_target(INTIMATE_TARGET_ROLE, admin_entry)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录请求并返回固定回复。"""
        requests.append(request)
        return _ok_response("同名条目回复")

    manager = await _make_pool_manager(handler)
    try:
        gateway = LlmGateway(
            resolver=resolver,
            pool_manager=manager,
            settings=build_isolated_settings(pools=PoolsConfig(cb_fail_max=2)),
            retry_attempts=1,
            retry_wait_seconds=0.0,
            intimate_config=_complete_config(),
        )
        response = await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()

    assert response.content == "同名条目回复"
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["Authorization"] == "Bearer sk-admin-entry"


async def test_intimate_temperature_default_only_when_caller_silent() -> None:
    """温度：通道缺省温度在调用方未设置时进入请求体，显式参数优先。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(manager, intimate_config=_complete_config(temperature=1.1))
        await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
        await gateway.chat_by_role(
            "chat", _messages(), ChatRequestParams(temperature=0.4), intimacy_level=3
        )
    finally:
        await manager.stop()

    bodies = [_json_body(request) for request in handler.intimate_requests]
    assert bodies[0]["temperature"] == 1.1
    assert bodies[1]["temperature"] == 0.4


async def test_stream_routes_to_intimate_endpoint() -> None:
    """流式一致：命中时流式请求同样指向亲密通道，增量块形态不变。"""
    handler = DualEndpointHandler()
    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(manager, intimate_config=_complete_config())
        chunks = [
            chunk async for chunk in gateway.stream_by_role("chat", _messages(), intimacy_level=3)
        ]
    finally:
        await manager.stop()

    assert [chunk.content for chunk in chunks] == ["亲", "近", ""]
    assert handler.default_requests == []
    assert len(handler.intimate_requests) == 1
    request = handler.intimate_requests[0]
    assert str(request.url) == f"{_INTIMATE_BASE_URL}/chat/completions"
    assert _json_body(request)["model"] == "intimate-model"


async def test_intimate_failure_propagates_without_silent_fallback() -> None:
    """失败语义：已配置被选中的通道调用失败按既有错误上抛，不改走默认通道。"""
    handler = DualEndpointHandler(intimate_status=500)
    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(manager, intimate_config=_complete_config())
        with pytest.raises(BusinessException) as exc_info:
            await gateway.chat_by_role("chat", _messages(), intimacy_level=3)
    finally:
        await manager.stop()

    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert len(handler.intimate_requests) == 1
    assert handler.default_requests == []


async def test_stream_failure_propagates_without_silent_fallback() -> None:
    """流式失败同语义：亲密端点 500 → 上抛，默认端点零请求。"""
    handler = DualEndpointHandler(intimate_status=500)
    manager = await _make_pool_manager(handler)
    try:
        gateway = _gateway(manager, intimate_config=_complete_config())
        with pytest.raises(BusinessException) as exc_info:
            async for _chunk in gateway.stream_by_role("chat", _messages(), intimacy_level=3):
                pass
    finally:
        await manager.stop()

    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert handler.default_requests == []
