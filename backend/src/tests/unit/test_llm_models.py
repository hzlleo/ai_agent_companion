"""harness/llm llm_models 测试——请求体构建 / 用量归一 / DTO 校验。

纯 Model 层测试（零 HTTP / 零数据库）；wire 请求体构建覆盖参数映射、
extra 透传与流式声明；usage 域创建 DTO 的 total 归一一并锁定（网关
事件 → 落库链的输入形态）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatRequestParams,
    LlmUsageEvent,
    TokenUsageModel,
    build_chat_request_body,
)
from packages.harness.usage.dtos import UsageCreateDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus


def _messages() -> list[ChatMessageModel]:
    """构造两轮消息序列（system 设定 + 用户输入）。"""
    return [
        ChatMessageModel(role=ChatMessageRole.SYSTEM, content="你是陪伴智能体"),
        ChatMessageModel(role=ChatMessageRole.USER, content="你好"),
    ]


# ---------------------------------------------------------------------------
# build_chat_request_body——非流式
# ---------------------------------------------------------------------------


async def test_build_body_non_stream_minimal() -> None:
    """非流式最小请求体：model + messages，无采样参数、无 stream 键。"""
    body = build_chat_request_body("m-1", _messages(), None, stream=False)
    assert body["model"] == "m-1"
    assert body["messages"] == [
        {"role": "system", "content": "你是陪伴智能体"},
        {"role": "user", "content": "你好"},
    ]
    assert "stream" not in body
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "top_p" not in body


async def test_build_body_maps_params() -> None:
    """采样参数映射：已设置字段进入请求体。"""
    params = ChatRequestParams(temperature=0.7, max_tokens=256, top_p=0.9)
    body = build_chat_request_body("m-1", _messages(), params, stream=False)
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 256
    assert body["top_p"] == 0.9


async def test_build_body_unset_params_absent() -> None:
    """未设置字段不进入请求体（端点默认值生效）。"""
    params = ChatRequestParams(temperature=0.5)
    body = build_chat_request_body("m-1", _messages(), params, stream=False)
    assert body["temperature"] == 0.5
    assert "max_tokens" not in body
    assert "top_p" not in body


async def test_build_body_extra_passthrough() -> None:
    """extra 透传：provider 特有参数原样并入顶层。"""
    params = ChatRequestParams(extra={"presence_penalty": 0.1, "seed": 42})
    body = build_chat_request_body("m-1", _messages(), params, stream=False)
    assert body["presence_penalty"] == 0.1
    assert body["seed"] == 42


async def test_build_body_stream_declares_include_usage() -> None:
    """流式请求体：stream=True + stream_options.include_usage（末块回传用量）。"""
    body = build_chat_request_body("m-1", _messages(), None, stream=True)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


async def test_chat_params_reject_out_of_range() -> None:
    """采样参数越界拒绝：temperature / top_p / max_tokens。"""
    with pytest.raises(ValidationError):
        ChatRequestParams(temperature=2.5)
    with pytest.raises(ValidationError):
        ChatRequestParams(top_p=1.5)
    with pytest.raises(ValidationError):
        ChatRequestParams(max_tokens=0)


# ---------------------------------------------------------------------------
# 用量归一
# ---------------------------------------------------------------------------


async def test_token_usage_fills_total_when_missing() -> None:
    """total 未提供（0）且分项非零时按 input + output 补齐。"""
    usage = TokenUsageModel(input_tokens=3, output_tokens=4)
    assert usage.total_tokens == 7


async def test_token_usage_keeps_explicit_total() -> None:
    """显式 total 原样保留（不重算）。"""
    usage = TokenUsageModel(input_tokens=3, output_tokens=4, total_tokens=10)
    assert usage.total_tokens == 10


async def test_usage_create_dto_fills_total() -> None:
    """usage 创建 DTO 的 total 归一（网关事件 → 落库链输入形态）。"""
    dto = UsageCreateDTO(
        ts_ms=1_000,
        model_entry="main-chat",
        purpose=UsagePurpose.CHAT,
        status=UsageStatus.SUCCESS,
        input_tokens=6,
        output_tokens=8,
    )
    assert dto.total_tokens == 14


# ---------------------------------------------------------------------------
# 网关用量事件
# ---------------------------------------------------------------------------


async def test_llm_usage_event_defaults() -> None:
    """网关用量事件：默认 provider / token 零值 / 可空上下文轴。"""
    event = LlmUsageEvent(
        ts_ms=1_234,
        purpose="chat",
        model_entry="main-chat",
        status="success",
    )
    assert event.provider == "openai_compatible"
    assert event.persona_id is None
    assert event.request_id is None
    assert event.error_code is None
    assert event.input_tokens == 0
    assert event.latency_ms == 0


async def test_llm_usage_event_rejects_blank_entry() -> None:
    """model_entry 非空契约。"""
    with pytest.raises(ValidationError):
        LlmUsageEvent(ts_ms=1, purpose="chat", model_entry="", status="success")
