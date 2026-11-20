"""harness/llm 多模态内容分块测试——wire 形态 / 流式守卫 / 客户端往返。

覆盖面（内容分块契约）：

    - wire 形态：纯文本 content 原样字符串（向后兼容）；分块列表 →
      parts 数组（``image_url`` 对象 + detail 可选透传 / 缺省不进入
      wire）；data URL 原样透传；
    - 流式守卫：分块 + stream → ``VALIDATION_ERROR``（构建层 + client
      ``stream()`` 面）；tools + 分块 + stream 同样拒绝；
    - 组合面：tools + 分块并存 OK（视觉 + 工具调用形态——duty 轮
      工具循环消费）；
    - 客户端往返：``chat()`` 携带分块消息经 MockTransport 捕获请求体
      （parts 数组进入 HTTP JSON body）+ 响应归一。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from packages.harness.config.llm_wire import LlmClientConfigModel
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.client import LlmHttpClient
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ImageContentPart,
    ImageContentUrlModel,
    TextContentPart,
    ToolDefinitionModel,
    ToolParametersSchema,
    build_chat_request_body,
)

#: 测试用 data URL（载荷内容无关——透传契约面）。
_DATA_URL: str = "data:image/png;base64,iVBORw0KGgo="


def _parts_message(
    *, url: str = _DATA_URL, text: str = "这是什么", detail: str | None = None
) -> ChatMessageModel:
    """构造携带内容分块的 user 消息（[image, text]——look_at 形态）。"""
    return ChatMessageModel(
        role=ChatMessageRole.USER,
        content=[
            ImageContentPart(image_url=ImageContentUrlModel(url=url, detail=detail)),
            TextContentPart(text=text),
        ],
    )


# ---------------------------------------------------------------------------
# wire 形态
# ---------------------------------------------------------------------------


async def test_string_content_wire_unchanged() -> None:
    """纯文本 content：wire 二键形态原样字符串（向后兼容）。"""
    message = ChatMessageModel(role=ChatMessageRole.USER, content="你好")
    body = build_chat_request_body("m-1", [message], None, stream=False)
    assert body["messages"] == [{"role": "user", "content": "你好"}]


async def test_parts_content_serializes_array() -> None:
    """分块 content：wire 序列化为 parts 数组（image 在前 + text 在后）。"""
    body = build_chat_request_body("m-1", [_parts_message()], None, stream=False)
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _DATA_URL}},
                {"type": "text", "text": "这是什么"},
            ],
        }
    ]


async def test_parts_detail_passthrough_and_default_absent() -> None:
    """detail 显式设置 → 进入 wire；缺省 None → 不进入（端点默认生效）。"""
    message = _parts_message(url="https://example.com/a.png", detail="high")
    body = build_chat_request_body("m-1", [message], None, stream=False)
    image_part = body["messages"][0]["content"][0]
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/a.png", "detail": "high"},
    }
    # 缺省形态：detail 键不存在
    message_default = _parts_message(url="https://example.com/b.png")
    body_default = build_chat_request_body("m-1", [message_default], None, stream=False)
    assert "detail" not in body_default["messages"][0]["content"][0]["image_url"]


async def test_parts_message_parses_from_dict_forms() -> None:
    """分块消息经 dict 形态构造（判别联合分派）+ data URL 原样保留。"""
    message = ChatMessageModel.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": _DATA_URL}},
            ],
        }
    )
    assert isinstance(message.content, list)
    assert message.content[0] == TextContentPart(text="看图")
    assert message.content[1].image_url.url == _DATA_URL


async def test_parts_reject_unknown_type() -> None:
    """未知分块类型：判别联合校验拒绝。"""
    with pytest.raises(ValidationError):
        ChatMessageModel.model_validate(
            {
                "role": "user",
                "content": [{"type": "audio_url", "audio_url": {"url": "x"}}],
            }
        )


async def test_image_url_reject_blank() -> None:
    """图片分块 url 非空契约。"""
    with pytest.raises(ValidationError):
        ImageContentPart(image_url=ImageContentUrlModel(url=""))


# ---------------------------------------------------------------------------
# 流式守卫（流式传输面恒 content-only 纯文本）
# ---------------------------------------------------------------------------


async def test_parts_with_stream_rejected() -> None:
    """分块 + stream：构建层直接拒绝（VALIDATION_ERROR）。"""
    with pytest.raises(BusinessException) as exc_info:
        build_chat_request_body("m-1", [_parts_message()], None, stream=True)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert "多模态" in exc_info.value.message


async def test_string_content_with_stream_still_ok() -> None:
    """纯文本 + stream：不受分块守卫影响（stream 键照常声明）。"""
    message = ChatMessageModel(role=ChatMessageRole.USER, content="你好")
    body = build_chat_request_body("m-1", [message], None, stream=True)
    assert body["stream"] is True


async def test_parts_tools_stream_rejected() -> None:
    """分块 + tools + stream：拒绝（任一守卫命中即拒）。"""
    tools = [
        ToolDefinitionModel(
            name="look_at",
            description="看图",
            parameters=ToolParametersSchema(type="object"),
        )
    ]
    with pytest.raises(BusinessException) as exc_info:
        build_chat_request_body("m-1", [_parts_message()], None, stream=True, tools=tools)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 组合面：tools + 分块并存（视觉 + 工具调用形态）
# ---------------------------------------------------------------------------


async def test_tools_and_parts_combined_ok() -> None:
    """tools + 分块并存：请求体同时携带 tools 面与 parts 数组。"""
    tools = [
        ToolDefinitionModel(
            name="get_current_time",
            description="获取当前时间",
            parameters=ToolParametersSchema(type="object"),
        )
    ]
    body = build_chat_request_body("m-1", [_parts_message()], None, stream=False, tools=tools)
    assert body["tools"][0]["function"]["name"] == "get_current_time"
    assert body["tool_choice"] == "auto"
    assert body["messages"][0]["content"][0]["type"] == "image_url"


# ---------------------------------------------------------------------------
# 客户端往返（MockTransport 零真实出站）
# ---------------------------------------------------------------------------


def _wire_config() -> LlmClientConfigModel:
    """测试 wire 配置（免鉴权本地端点形态）。"""
    return LlmClientConfigModel(base_url="http://127.0.0.1:9888/v1", model="qwen3-vl")


def _ok_response() -> httpx.Response:
    """2xx 非流式响应（单 choice 文本回复）。"""
    return httpx.Response(
        200,
        json={
            "id": "chat-1",
            "model": "qwen3-vl",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "图中是一只橘猫"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
    )


async def test_client_chat_sends_parts_body() -> None:
    """client.chat() 携带分块：parts 数组进入 HTTP JSON body；响应归一。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获请求后返回 2xx。"""
        captured.append(request)
        return _ok_response()

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LlmHttpClient(_wire_config(), shared)
    try:
        response = await client.chat([_parts_message()])
        assert captured[0].url == "http://127.0.0.1:9888/v1/chat/completions"
        body: dict[str, Any] = json.loads(captured[0].read().decode("utf-8"))
        assert body["messages"][0]["content"] == [
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
            {"type": "text", "text": "这是什么"},
        ]
        assert response.content == "图中是一只橘猫"
        assert response.usage is not None
        assert response.usage.total_tokens == 120
    finally:
        await shared.aclose()


async def test_client_stream_rejects_parts_before_http() -> None:
    """client.stream() 携带分块：首个迭代步即 VALIDATION_ERROR（零出站）。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """不应被触达（守卫在请求构建层）。"""
        requests.append(request)
        return _ok_response()

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LlmHttpClient(_wire_config(), shared)
    try:
        stream = client.stream([_parts_message()])
        with pytest.raises(BusinessException) as exc_info:
            await stream.__anext__()
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert requests == []
    finally:
        await shared.aclose()
