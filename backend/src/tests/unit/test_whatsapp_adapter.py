"""WhatsApp 适配器测试——Graph API 文本出站 / 服务窗闸 / 错误映射（零真实网络）。

全部经 ``httpx.MockTransport`` 注入：请求 URL / 头 / 体可断言，响应与
传输异常可编程；时钟为伪时钟（服务窗边界断言不需要真实等待）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.whatsapp.whatsapp_adapter import WhatsAppAdapter
from packages.harness.channels.whatsapp.whatsapp_client import WhatsAppCloudClient
from packages.harness.channels.whatsapp.whatsapp_models import (
    WHATSAPP_SERVICE_WINDOW_MS,
    WhatsAppServiceWindow,
)
from packages.harness.channels.whatsapp.whatsapp_runner import WhatsAppWebhookRunner
from packages.harness.core.errors import BusinessException, ErrorCode

_BASE_MS = 1_731_000_000_000
_WA = "15551234567"


class _Clock:
    """可推进的测试时钟（epoch 毫秒）。"""

    def __init__(self, now_ms: int = _BASE_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


class _RecordingRouter:
    """fake 路由器（服务窗联动用——记录入站）。"""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def handle_inbound(self, message: object) -> object:
        self.received.append(str(getattr(message, "message_id", "")))
        return None


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    access_token: str = "test-access-token",
    phone_number_id: str = "123456",
) -> httpx.AsyncClient:
    """构造注入 MockTransport 的共享形态客户端。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _adapter(
    http_client: httpx.AsyncClient,
    *,
    service_window: WhatsAppServiceWindow,
    clock: _Clock,
    access_token: str = "test-access-token",
    phone_number_id: str = "123456",
) -> WhatsAppAdapter:
    """构造被测适配器（指向 mock 客户端）。"""
    return WhatsAppAdapter(
        client=WhatsAppCloudClient(
            http_client,
            api_base="https://graph.test",
            api_version="v21.0",
            phone_number_id=phone_number_id,
            access_token=access_token,
        ),
        service_window=service_window,
        clock=clock,
    )


async def test_send_success_returns_provider_message_id_and_wire_shape() -> None:
    """成功路径：POST 路径 / Bearer / 文本体 / 返回 message_id。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": _WA, "profile": {"name": "Alice"}}],
                "messages": [{"id": "wamid.OUT-1"}],
            },
        )

    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)
    async with _client(handler) as http_client:
        adapter = _adapter(
            http_client,
            service_window=window,
            clock=_Clock(_BASE_MS + 1000),
        )
        result = await adapter.send_message(
            OutboundMessageModel(chat_id=f"wa:{_WA}", text="人格回复")
        )
    assert result.channel is ChannelType.WHATSAPP
    assert result.chat_id == f"wa:{_WA}"
    assert result.message_ids == ["wamid.OUT-1"]
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "https://graph.test/v21.0/123456/messages"
    assert request.headers["authorization"] == "Bearer test-access-token"
    body = json.loads(request.content)
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == _WA
    assert body["type"] == "text"
    assert body["text"] == {"body": "人格回复"}


async def test_send_response_without_message_id_returns_empty_ids() -> None:
    """响应未回传消息凭据：送达成功但 message_ids 空。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messaging_product": "whatsapp"})

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        result = await adapter.send_message(
            OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复")
        )
    assert result.message_ids == []


async def test_send_401_maps_to_channel_token_missing() -> None:
    """HTTP 401：令牌无效（CHANNEL_TOKEN_MISSING）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid token")

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING


async def test_send_non_2xx_maps_to_channel_api_error() -> None:
    """其余非 2xx：CHANNEL_API_ERROR（携带状态码与响应片段）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
    assert exc_info.value.details["status_code"] == 500


async def test_send_timeout_maps_to_channel_api_error() -> None:
    """传输超时：CHANNEL_API_ERROR（逐消息隔离面）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


async def test_send_invalid_json_response_maps_to_channel_api_error() -> None:
    """响应不是合法 JSON：CHANNEL_API_ERROR。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


async def test_send_outside_window_rejected_without_http_call() -> None:
    """从未收到入站 / 超窗：受控业务错误且不发起 HTTP 调用。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    window = WhatsAppServiceWindow()
    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details["last_inbound_ms"] is None
        window.record_inbound(_WA, _BASE_MS)
        adapter_expired = _adapter(
            http_client,
            service_window=window,
            clock=_Clock(_BASE_MS + WHATSAPP_SERVICE_WINDOW_MS + 1),
        )
        with pytest.raises(BusinessException) as expired_info:
            await adapter_expired.send_message(
                OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复")
            )
    assert expired_info.value.code == ErrorCode.CHANNEL_API_ERROR
    assert expired_info.value.details["last_inbound_ms"] == _BASE_MS
    assert calls == []


async def test_send_at_window_boundary_allowed() -> None:
    """窗口边界（恰 24h）：放行。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "wamid.BORDER"}]})

    async with _client(handler) as http_client:
        adapter = _adapter(
            http_client,
            service_window=window,
            clock=_Clock(_BASE_MS + WHATSAPP_SERVICE_WINDOW_MS),
        )
        result = await adapter.send_message(
            OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复")
        )
    assert result.message_ids == ["wamid.BORDER"]


async def test_runner_inbound_updates_window_used_by_adapter() -> None:
    """runner 入站登记后适配器可放行（同一服务窗实例的联用语义）。"""
    window = WhatsAppServiceWindow()
    clock = _Clock(_BASE_MS)
    runner = WhatsAppWebhookRunner(
        router=_RecordingRouter(),
        service_window=window,
        owner_numbers=frozenset(),
        clock=clock,
    )
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "from": _WA,
                                    "id": "wamid.IN-1",
                                    "timestamp": str(_BASE_MS // 1000),
                                    "type": "text",
                                    "text": {"body": "在吗"},
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    }
    assert runner.submit(payload) is True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT-2"}]})

    async with _client(handler) as http_client:
        adapter = _adapter(
            http_client,
            service_window=window,
            clock=_Clock(_BASE_MS + 60_000),
        )
        result = await adapter.send_message(
            OutboundMessageModel(chat_id=f"wa:{_WA}", text="在的")
        )
    assert result.message_ids == ["wamid.OUT-2"]


@pytest.mark.parametrize("chat_id", ["sms:15551234567", "15551234567", "wa:", "wa:()"])
async def test_invalid_chat_id_grammar_rejected(chat_id: str) -> None:
    """非法 chat_id grammar：VALIDATION_ERROR（不发起 HTTP）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起 HTTP 调用")

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=chat_id, text="回复"))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_attachments_are_ignored_text_still_sent() -> None:
    """出站仅文本：附件被忽略，文本照常投递。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["type"] == "text"
        return httpx.Response(200, json={"messages": [{"id": "wamid.TEXT"}]})

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        result = await adapter.send_message(
            OutboundMessageModel(
                chat_id=f"wa:{_WA}",
                text="带表情的回复",
                attachments=[
                    ChannelMessageAttachment(
                        kind=ChannelAttachmentKind.STICKER, url="https://x/y.png"
                    )
                ],
            )
        )
    assert result.message_ids == ["wamid.TEXT"]


async def test_blank_text_maps_to_validation_error() -> None:
    """纯空白文本：VALIDATION_ERROR（调用方 bug 面）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起 HTTP 调用")

    async with _client(handler) as http_client:
        adapter = _adapter(http_client, service_window=window, clock=_Clock())
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="   "))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_missing_access_token_rejected_before_http() -> None:
    """未配置访问令牌：CHANNEL_TOKEN_MISSING（不发起 HTTP）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async with _client(handler) as http_client:
        adapter = _adapter(
            http_client, service_window=window, clock=_Clock(), access_token=""
        )
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
    assert calls == []


async def test_missing_phone_number_id_rejected_before_http() -> None:
    """未配置企业号码 ID：CONFIG_INVALID（不发起 HTTP）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound(_WA, _BASE_MS)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async with _client(handler) as http_client:
        adapter = _adapter(
            http_client, service_window=window, clock=_Clock(), phone_number_id=""
        )
        with pytest.raises(BusinessException) as exc_info:
            await adapter.send_message(OutboundMessageModel(chat_id=f"wa:{_WA}", text="回复"))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert calls == []
