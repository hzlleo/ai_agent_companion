"""GUI 微信出站适配器测试——标题校验 / 发送 / 失败隔离 / 附件边界。"""

from __future__ import annotations

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_adapter import (
    WECHAT_GUI_ATTACHMENT_UNSUPPORTED_ERROR,
    WechatGuiChannelAdapter,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.wechat_gui_fakes import (
    FakeWechatGuiClient,
    FakeWechatGuiReader,
    make_read_result,
)


def _adapter(
    *,
    client: FakeWechatGuiClient | None = None,
    reader: FakeWechatGuiReader | None = None,
) -> WechatGuiChannelAdapter:
    """构造注入 fake 的适配器。"""
    return WechatGuiChannelAdapter(
        client=client or FakeWechatGuiClient(),
        reader=reader or FakeWechatGuiReader([make_read_result(title="小林")]),
    )


async def test_send_after_title_verification() -> None:
    """标题校验通过：输入文本并回车；附件逐条标记不支持。"""
    client = FakeWechatGuiClient()
    adapter = _adapter(client=client)
    message = OutboundMessageModel(
        chat_id="wechat_gui:小林",
        text="晚安",
        attachments=[
            ChannelMessageAttachment(
                kind=ChannelAttachmentKind.IMAGE, url="https://example.com/a.jpg"
            )
        ],
        silent=True,
    )
    result = await adapter.send_message(message)
    assert client.open_calls == ["小林"]
    assert client.type_calls == ["晚安"]
    assert client.key_calls == ["enter"]
    assert result.channel is ChannelType.WECHAT_GUI
    assert result.chat_id == "wechat_gui:小林"
    assert result.message_ids == []
    assert [(item.index, item.ok, item.error) for item in result.attachment_results] == [
        (0, False, WECHAT_GUI_ATTACHMENT_UNSUPPORTED_ERROR)
    ]


async def test_send_rejected_on_title_mismatch() -> None:
    """标题不一致：拒绝发送（不输入 / 不回显）。"""
    client = FakeWechatGuiClient()
    adapter = _adapter(
        client=client,
        reader=FakeWechatGuiReader([make_read_result(title="别人")]),
    )
    with pytest.raises(BusinessException) as error:
        await adapter.send_message(
            OutboundMessageModel(chat_id="wechat_gui:小林", text="晚安")
        )
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert client.open_calls == ["小林"]
    assert client.type_calls == []
    assert client.key_calls == []


async def test_reader_failure_propagates_without_send() -> None:
    """视觉解析失败（fail-closed）：不输入、不回车。"""
    client = FakeWechatGuiClient()
    reader = FakeWechatGuiReader([])
    reader.error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="视觉解析失败"
    )
    adapter = _adapter(client=client, reader=reader)
    with pytest.raises(BusinessException) as error:
        await adapter.send_message(
            OutboundMessageModel(chat_id="wechat_gui:小林", text="晚安")
        )
    assert error.value.code is ErrorCode.LLM_CALL_FAILED
    assert client.type_calls == []


async def test_bad_chat_id_rejected_before_open_chat() -> None:
    """chat_id grammar 非法：VALIDATION_ERROR（调用方 bug 面）。"""
    client = FakeWechatGuiClient()
    adapter = _adapter(client=client)
    with pytest.raises(BusinessException) as error:
        await adapter.send_message(OutboundMessageModel(chat_id="im:小林", text="晚安"))
    assert error.value.code is ErrorCode.VALIDATION_ERROR
    assert client.open_calls == []


async def test_empty_text_skips_input() -> None:
    """空文本：仍打开并校验，但不输入 / 不回车（附件-only 形态）。"""
    client = FakeWechatGuiClient()
    adapter = _adapter(client=client)
    result = await adapter.send_message(
        OutboundMessageModel(chat_id="wechat_gui:小林", text="   ")
    )
    assert client.type_calls == []
    assert result.message_ids == []


async def test_type_text_failure_propagates() -> None:
    """输入失败：上抛（路由器兜底隔离）。"""
    client = FakeWechatGuiClient()
    client.fail_type_text = BusinessException(
        code=ErrorCode.CHANNEL_API_ERROR, message="剪贴板失败"
    )
    adapter = _adapter(client=client)
    with pytest.raises(BusinessException) as error:
        await adapter.send_message(
            OutboundMessageModel(chat_id="wechat_gui:小林", text="晚安")
        )
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert client.key_calls == []
