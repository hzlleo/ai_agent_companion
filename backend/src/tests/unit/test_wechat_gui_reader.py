"""GUI 微信截图解析测试——严格 JSON / 低置信度 / 回退链失败（fail-closed）。

零真实网络：LLM 网关经注入 fake（按队列返回文本或抛业务异常）；解析器本身
只消费注入的条目解析面。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

import pytest

from packages.harness.channels.wechat_gui.wechat_gui_reader import (
    WechatGuiReader,
    extract_json_object,
)
from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatRequestParams,
    ChatResponseModel,
    ToolDefinitionModel,
)

_ENTRY = LlmModelEntry(
    name="vision-local",
    base_url="http://127.0.0.1:9000/v1",
    model="qwen-vl",
    multimodal=True,
)
_PNG_BASE64 = base64.b64encode(b"png-bytes").decode("ascii")


class FakeResolver:
    """按角色返回固定条目的解析器替身。"""

    def __init__(self, entry: LlmModelEntry = _ENTRY) -> None:
        self._entry = entry

    def resolve(self, role: object) -> LlmModelEntry:
        """返回固定条目。"""
        return self._entry


class FakeGateway:
    """按队列返回文本 / 抛出异常的视觉网关替身。"""

    def __init__(self, queue: list[Any] | None = None) -> None:
        self.queue = list(queue or [])
        self.calls: list[tuple[str, str]] = []
        self.messages: list[Sequence[ChatMessageModel]] = []

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
        tools: Sequence[ToolDefinitionModel] | None = None,
        tool_choice: str | None = None,
    ) -> ChatResponseModel:
        """记录调用并返回脚本化响应。"""
        self.calls.append((entry.name, purpose))
        self.messages.append(messages)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResponseModel(content=str(item))


def _reader(gateway: FakeGateway, **kwargs: Any) -> WechatGuiReader:
    """构造注入 fake 的解析器。"""
    return WechatGuiReader(gateway=gateway, resolver=FakeResolver(), **kwargs)


def _json_content(
    *,
    title: str = "小林",
    messages: list[dict[str, Any]] | None = None,
    confidence: float = 0.9,
) -> str:
    """构造模型 JSON 响应文本。"""
    return json.dumps(
        {
            "title": title,
            "messages": list(messages or []),
            "confidence": confidence,
        },
        ensure_ascii=False,
    )


async def test_extract_json_object_tolerates_fences_and_noise() -> None:
    """JSON 提取：代码围栏 / 前后说明 / 非法输入。"""
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('说明文字 {"a": 2} 结束') == {"a": 2}
    with pytest.raises(ValueError, match="未找到"):
        extract_json_object("no json")
    with pytest.raises(ValueError, match="非法"):
        extract_json_object("{bad}")


async def test_reader_success_and_purpose() -> None:
    """解析成功：标题 / 消息 / 置信度 / 原始 JSON + purpose=look_at。"""
    gateway = FakeGateway(
        [
            "```json\n"
            + _json_content(
                messages=[
                    {"sender": "小林", "text": "你好", "is_self": False, "ts_ms": 1000},
                    {"sender": "我", "text": "在的", "is_self": True, "ts_ms": 2000},
                ]
            )
            + "\n```"
        ]
    )
    reader = _reader(gateway)
    result = await reader.read(_PNG_BASE64, request_id="req-1")
    assert result.title == "小林"
    assert [message.text for message in result.messages] == ["你好", "在的"]
    assert result.confidence == 0.9
    assert result.raw_json["title"] == "小林"
    assert gateway.calls == [("vision-local", "look_at")]
    user_message = gateway.messages[0][1]
    assert isinstance(user_message.content, list)
    assert len(user_message.content) == 2


async def test_reader_caps_messages_to_limit() -> None:
    """解析成功：消息按时间序末尾裁剪到上限。"""
    messages = [{"sender": "小林", "text": f"消息{i}"} for i in range(5)]
    gateway = FakeGateway([_json_content(messages=messages)])
    reader = _reader(gateway, max_messages=2)
    result = await reader.read(_PNG_BASE64)
    assert [message.text for message in result.messages] == ["消息3", "消息4"]


async def test_reader_rejects_non_json_and_low_confidence() -> None:
    """非 JSON / 低置信度 / 缺字段 → LLM_CALL_FAILED（fail-closed）。"""
    for content in (
        "这不是 JSON",
        _json_content(confidence=0.1),
        json.dumps({"title": "", "messages": [], "confidence": 0.9}),
        json.dumps({"title": "小林", "messages": "oops", "confidence": 0.9}),
    ):
        gateway = FakeGateway([content])
        reader = _reader(gateway)
        with pytest.raises(BusinessException) as error:
            await reader.read(_PNG_BASE64)
        assert error.value.code is ErrorCode.LLM_CALL_FAILED


async def test_reader_propagates_single_entry_gateway_error() -> None:
    """单条目链的网关业务异常原样上抛（错误码不被包裹）。"""
    failure = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="上游熔断")
    gateway = FakeGateway([failure])
    reader = _reader(gateway)
    with pytest.raises(BusinessException) as error:
        await reader.read(_PNG_BASE64)
    assert error.value.code is ErrorCode.LLM_CALL_FAILED
    assert error.value.message == "上游熔断"


async def test_reader_vlm_entry_filter() -> None:
    """vlm_entry：命中条目正常解析；不在链中即 CONFIG_INVALID。"""
    gateway = FakeGateway([_json_content()])
    matching = _reader(gateway, vlm_entry="vision-local")
    result = await matching.read(_PNG_BASE64)
    assert result.title == "小林"

    with pytest.raises(BusinessException) as error:
        await _reader(FakeGateway([_json_content()]), vlm_entry="other").read(_PNG_BASE64)
    assert error.value.code is ErrorCode.CONFIG_INVALID


async def test_reader_rejects_invalid_base64() -> None:
    """截图载荷非法（非 base64 / 空）→ CHANNEL_API_ERROR。"""
    with pytest.raises(BusinessException) as error:
        await _reader(FakeGateway([])).read("不是 base64")
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    with pytest.raises(BusinessException) as empty:
        await _reader(FakeGateway([])).read("")
    assert empty.value.code is ErrorCode.CHANNEL_API_ERROR


async def test_reader_constructor_bounds() -> None:
    """构造不变式：置信度阈值与消息上限。"""
    with pytest.raises(ValueError):
        _reader(FakeGateway([]), confidence_threshold=1.5)
    with pytest.raises(ValueError):
        _reader(FakeGateway([]), max_messages=0)
