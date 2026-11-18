"""douyin 通道客户端测试——浏览器底座调用面 / 页面脚本参数 / 失败归一。

覆盖矩阵：

    - 会话页打开：配置了 URL 经 navigate；未配置零调用；导航失败归一
      CHANNEL_API_ERROR；
    - 采集：脚本携带会话标识与上限 + evaluate 参数（上限 / 超时）；成功解析
      快照；工具失败 / 返回值非对象 / 快照结构非法均归一 CHANNEL_API_ERROR；
    - 发送：脚本携带文本；成功零返回；脚本报告 ok=false / 工具失败归一
      CHANNEL_API_ERROR。
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.harness.channels.douyin.douyin_client import (
    DOUYIN_EVALUATE_TIMEOUT_MS,
    DOUYIN_MAX_CHARS,
    DouyinWebClient,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.tools.tool_models import ToolResultModel
from tests.douyin_fakes import FakeDouyinBrowserInvoker, make_message_payload, make_snapshot_payload


async def test_open_chat_navigates_when_configured() -> None:
    """会话页打开：配置 URL → navigate 调用（无配置零调用）。"""
    invoker = FakeDouyinBrowserInvoker()
    client = DouyinWebClient(invoker=invoker, chat_url="https://www.douyin.com/im")
    await client.open_chat()
    assert invoker.invocations == [("navigate", {"url": "https://www.douyin.com/im"})]

    quiet_invoker = FakeDouyinBrowserInvoker()
    quiet_client = DouyinWebClient(invoker=quiet_invoker)
    await quiet_client.open_chat()
    assert quiet_invoker.invocations == []


async def test_open_chat_failure_maps_to_channel_api_error() -> None:
    """导航失败（允许清单拒绝 / 私网拒绝 / sidecar 不可达）→ CHANNEL_API_ERROR。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.navigate_ok = False
    client = DouyinWebClient(invoker=invoker, chat_url="https://www.douyin.com/im")
    with pytest.raises(BusinessException) as error:
        await client.open_chat()
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert invoker.navigate_error in error.value.message


async def test_fetch_snapshot_passes_script_and_limits() -> None:
    """采集：脚本携带会话标识 / 上限；evaluate 携带规模与超时旋钮。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append(
        make_snapshot_payload(messages=[make_message_payload(message_id="m-1")])
    )
    client = DouyinWebClient(invoker=invoker)
    snapshot = await client.fetch_snapshot(conversation_id="c-9", limit=8)
    assert snapshot.conversation_id == "c-1"
    assert [message.message_id for message in snapshot.messages] == ["m-1"]
    assert len(invoker.evaluate_calls) == 1
    call = invoker.evaluate_calls[0]
    assert call["max_chars"] == DOUYIN_MAX_CHARS
    assert call["timeout_ms"] == DOUYIN_EVALUATE_TIMEOUT_MS
    assert '"conversation_id": "c-9"' in call["script"]
    assert '"limit": 8' in call["script"]


async def test_fetch_snapshot_tool_failure_maps_to_channel_api_error() -> None:
    """采集工具失败（sidecar 不可达 / 求值超时）→ CHANNEL_API_ERROR。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_ok = False
    client = DouyinWebClient(invoker=invoker)
    with pytest.raises(BusinessException) as error:
        await client.fetch_snapshot(conversation_id="", limit=5)
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert invoker.evaluate_error in error.value.message


async def test_fetch_snapshot_rejects_non_object_result() -> None:
    """采集返回值不是 JSON 对象（脚本被改坏）→ CHANNEL_API_ERROR。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append({"messages": []})
    client = DouyinWebClient(invoker=invoker)
    await client.fetch_snapshot(conversation_id="", limit=5)

    class _ScalarInvoker(FakeDouyinBrowserInvoker):
        """返回标量脚本结果的 fake（结构破坏面）。"""

        async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
            """返回 ``result`` 为字符串的工具结果（非对象载荷）。"""
            return ToolResultModel(tool=tool, ok=True, result={"result": "text"})

    scalar_client = DouyinWebClient(invoker=_ScalarInvoker())
    with pytest.raises(BusinessException) as error:
        await scalar_client.fetch_snapshot(conversation_id="", limit=5)
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "期望 JSON 对象" in error.value.message


async def test_fetch_snapshot_rejects_broken_wire_shape() -> None:
    """快照结构非法（messages 非数组）→ CHANNEL_API_ERROR（可重试面）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append({"conversation_id": "c-1", "messages": "oops"})
    client = DouyinWebClient(invoker=invoker)
    with pytest.raises(BusinessException) as error:
        await client.fetch_snapshot(conversation_id="", limit=5)
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "结构非法" in error.value.message


async def test_send_text_success_and_failure() -> None:
    """发送：成功零返回；脚本报告失败 → CHANNEL_API_ERROR（携带原因）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append({"ok": True, "text": "你好"})
    client = DouyinWebClient(invoker=invoker)
    await client.send_text(text="你好")
    assert '"text": "\\u4f60\\u597d"' in invoker.evaluate_calls[0]["script"]

    failing_invoker = FakeDouyinBrowserInvoker()
    failing_invoker.evaluate_queue.append({"ok": False, "error": "未找到输入框"})
    failing_client = DouyinWebClient(invoker=failing_invoker)
    with pytest.raises(BusinessException) as error:
        await failing_client.send_text(text="你好")
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "未找到输入框" in error.value.message
