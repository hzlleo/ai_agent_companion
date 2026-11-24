"""app/api/channels_whatsapp 路由测试——hub 校验 / 签名 / 体校验（零真实网络）。

测试策略：本地装配 FastAPI app（WhatsApp webhook 路由 + 共享异常处理器）；
通道路由器 / runner 经 ``app.state`` 注入 fake；凭据来自隔离 Settings——
鉴权矩阵不依赖进程 env。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

import app.api.channels_whatsapp as whatsapp_api
from app.api.channels_whatsapp import WHATSAPP_MAX_BODY_BYTES
from app.api.channels_whatsapp import router as whatsapp_channels_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.whatsapp.whatsapp_models import (
    WhatsAppServiceWindow,
    reset_whatsapp_state,
)
from packages.harness.channels.whatsapp.whatsapp_runner import WhatsAppWebhookRunner
from packages.harness.config.models import (
    ChannelsSettings,
    Settings,
    WhatsAppChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode

_VERIFY_TOKEN = "test-only-verify-token"
_APP_SECRET = "test-only-app-secret"
_URL = "/api/v1/channels/whatsapp"


class FakeChannelRouter:
    """fake ChannelRouter——记录入站消息（路由目标）。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.WHATSAPP,
            chat_id=message.chat_id,
            message_ids=[],
        )


class _BlockingRouter:
    """fake 路由器——入站处理被闸门占住（验证 ack 不等待路由完成）。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        self.started.set()
        await self.release.wait()
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.WHATSAPP,
            chat_id=message.chat_id,
            message_ids=[],
        )


def _settings(
    *,
    verify_token: str = _VERIFY_TOKEN,
    app_secret: str = _APP_SECRET,
    access_token: str = "test-only-access-token",
    phone_number_id: str = "123456",
    enabled: bool = True,
    owner_numbers: str = "",
) -> Settings:
    """构造隔离 Settings（WhatsApp 配置显式值）。"""
    return build_isolated_settings(
        channels=ChannelsSettings(
            whatsapp=WhatsAppChannelSettings(
                enabled=enabled,
                verify_token=verify_token,
                app_secret=app_secret,
                access_token=access_token,
                phone_number_id=phone_number_id,
                owner_numbers=owner_numbers,
            )
        )
    )


def _app(
    settings: Settings,
    *,
    router: Any = None,
    runner: WhatsAppWebhookRunner | None = None,
) -> FastAPI:
    """装配最小 FastAPI app（路由 + 异常处理器 + 注入面）。"""
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(whatsapp_channels_router)
    register_exception_handlers(app)
    app.state.settings = settings
    if router is not None:
        app.state.channels_router = router
    if runner is not None:
        app.state.channels_whatsapp_runner = runner
    return app


@pytest.fixture(autouse=True)
def _reset_direct_dedup() -> None:
    """每测试复位直调路径的进程内去重窗口。"""
    reset_whatsapp_state()


def _payload(
    *,
    wamid: str = "wamid.TEST-1",
    text: str = "你好",
    sender: str = "15551234567",
) -> dict[str, Any]:
    """构造单消息 webhook 载荷（测试直供形态）。"""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "biz-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wamid,
                                    "timestamp": "1731000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _sign(body: bytes, secret: str = _APP_SECRET) -> str:
    """构造 ``X-Hub-Signature-256`` 头值（与 Meta 同形态）。"""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def _post(
    client: AsyncClient,
    payload: dict[str, Any],
    *,
    secret: str = _APP_SECRET,
    include_signature: bool = True,
) -> tuple[int, Any]:
    """发送带合法签名的 JSON 推送（返回状态码与 JSON 体）。"""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if include_signature:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    response = await client.post(_URL, content=body, headers=headers)
    return response.status_code, response.json()


class TestHubVerification:
    async def test_valid_handshake_echoes_challenge_plaintext(self) -> None:
        """合法 hub 握手：200 纯文本回显 challenge。"""
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": _VERIFY_TOKEN,
                    "hub.challenge": "987654321",
                },
            )
        assert response.status_code == 200
        assert response.text == "987654321"
        assert response.headers["content-type"].startswith("text/plain")

    async def test_wrong_mode_403(self) -> None:
        """hub.mode 非 subscribe：403。"""
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={
                    "hub.mode": "unsubscribe",
                    "hub.verify_token": _VERIFY_TOKEN,
                    "hub.challenge": "123",
                },
            )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    async def test_missing_challenge_403(self) -> None:
        """缺 hub.challenge：403（无法回显确认）。"""
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={"hub.mode": "subscribe", "hub.verify_token": _VERIFY_TOKEN},
            )
        assert response.status_code == 403

    async def test_wrong_token_403(self) -> None:
        """hub.verify_token 不匹配：403。"""
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "123",
                },
            )
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

    async def test_missing_token_403(self) -> None:
        """期望 token 非空而请求未携带：403。"""
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL, params={"hub.mode": "subscribe", "hub.challenge": "123"}
            )
        assert response.status_code == 403

    async def test_empty_configured_token_disables_comparison(self) -> None:
        """配置 token 为空：校验关闭（开发 / 测试形态——任意 token 通过）。"""
        app = _app(_settings(verify_token=""))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "anything",
                    "hub.challenge": "42",
                },
            )
        assert response.status_code == 200
        assert response.text == "42"

    async def test_token_comparison_uses_constant_time_primitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正确 token 走恒时比较原语（``secrets.compare_digest`` 实参记录）。"""
        calls: list[tuple[bytes, bytes]] = []
        real_compare_digest = secrets.compare_digest

        def _record(presented: bytes, expected: bytes) -> bool:
            """录制比较实参并委托真实实现。"""
            calls.append((presented, expected))
            return real_compare_digest(presented, expected)

        monkeypatch.setattr(whatsapp_api.secrets, "compare_digest", _record)
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                _URL,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": _VERIFY_TOKEN,
                    "hub.challenge": "1",
                },
            )
        assert response.status_code == 200
        token = _VERIFY_TOKEN.encode("utf-8")
        assert calls == [(token, token)]


class TestSignatureVerification:
    async def test_valid_signature_routes_message(self) -> None:
        """合法签名：200 确认 + 入站归一字段（会话键 / peer）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, body = await _post(client, _payload())
        assert status_code == 200
        assert body == {"status": "ok"}
        assert len(fake_router.handled) == 1
        message = fake_router.handled[0]
        assert message.channel is ChannelType.WHATSAPP
        assert message.chat_id == "wa:15551234567"
        assert message.synthetic is False
        assert message.is_owner is False

    async def test_country_code_owner_marked_on_direct_path(self) -> None:
        """直调路径：属主清单带国家码命中 → 会话键保留国家码且 owner 落档。"""
        fake_router = FakeChannelRouter()
        app = _app(
            _settings(owner_numbers="+86 138 0013 8000,妈妈"), router=fake_router
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, _ = await _post(
                client, _payload(wamid="wamid.CN-OWNER", sender="8613800138000")
            )
        assert status_code == 200
        assert len(fake_router.handled) == 1
        message = fake_router.handled[0]
        assert message.chat_id == "wa:8613800138000"
        assert message.is_owner is True

    async def test_tampered_body_403_and_router_untouched(self) -> None:
        """签名后篡改体：403 + 路由器零调用。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        original = json.dumps(_payload()).encode("utf-8")
        signature = _sign(original)
        tampered = original.replace(b"wamid.TEST-1", b"wamid.TAMPERED")
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=tampered, headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"
        assert fake_router.handled == []

    async def test_missing_signature_403(self) -> None:
        """缺签名头：403 + 路由器零调用。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, _ = await _post(
                client, _payload(), include_signature=False
            )
        assert status_code == 403
        assert fake_router.handled == []

    async def test_wrong_secret_403(self) -> None:
        """用错密钥签名：403。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, _ = await _post(client, _payload(), secret="wrong-secret")
        assert status_code == 403

    async def test_empty_configured_secret_disables_signature_check(self) -> None:
        """配置密钥为空：签名校验关闭（开发 / 测试形态）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(app_secret=""), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, _ = await _post(
                client, _payload(), include_signature=False
            )
        assert status_code == 200
        assert len(fake_router.handled) == 1

    async def test_rejected_signature_does_not_consume_dedup(self) -> None:
        """签名拒绝先于去重：同 wamid 的后续合法请求照常路由。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            rejected, _ = await _post(
                client, _payload(wamid="wamid.DEDUP-1"), secret="wrong-secret"
            )
            accepted, _ = await _post(client, _payload(wamid="wamid.DEDUP-1"))
        assert rejected == 403
        assert accepted == 200
        assert len(fake_router.handled) == 1

    async def test_duplicate_wamid_routed_once(self) -> None:
        """合法签名的同 wamid 重复推送：直调路径去重（只路由一次）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first, _ = await _post(client, _payload(wamid="wamid.DUP"))
            second, _ = await _post(client, _payload(wamid="wamid.DUP"))
        assert (first, second) == (200, 200)
        assert len(fake_router.handled) == 1


class TestBodyValidation:
    @pytest.mark.parametrize("media_type", ["text/plain", "application/x-www-form-urlencoded", ""])
    async def test_non_json_content_type_400(self, media_type: str) -> None:
        """非 application/json（含缺失）：400 BAD_REQUEST。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        body = json.dumps(_payload()).encode("utf-8")
        headers = {
            "Content-Type": media_type,
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert fake_router.handled == []

    async def test_malformed_json_400(self) -> None:
        """合法签名但请求体不是 JSON：400。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        body = b"not-json"
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    @pytest.mark.parametrize("body", [b"[]", b'"hello"', b"42"])
    async def test_non_object_json_400(self, body: bytes) -> None:
        """JSON 非对象：400（Cloud API 推送须为对象）+ 路由器零调用。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=body, headers=headers)
        assert response.status_code == 400
        assert fake_router.handled == []

    async def test_empty_body_400(self) -> None:
        """空请求体：400。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(b""),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=b"", headers=headers)
        assert response.status_code == 400

    async def test_declared_oversize_rejected_before_read(self) -> None:
        """声明的 Content-Length 超限：400（先于实读）。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        body = b'{"x":"' + b"a" * (WHATSAPP_MAX_BODY_BYTES + 16) + b'"}'
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_URL, content=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    async def test_actual_oversize_without_content_length_rejected(self) -> None:
        """Content-Length 缺失时按实读字节数拒绝超限体。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        body = b'{"x":"' + b"a" * (WHATSAPP_MAX_BODY_BYTES + 16) + b'"}'
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            request = client.build_request("POST", _URL, content=body, headers=headers)
            request.headers.pop("content-length", None)
            response = await client.send(request)
        assert response.status_code == 400

    async def test_missing_content_length_with_valid_signature_accepted(self) -> None:
        """Content-Length 缺失但签名合法：正常接受（200 + 路由一次）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        body = json.dumps(_payload(wamid="wamid.NO-CL")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            request = client.build_request("POST", _URL, content=body, headers=headers)
            request.headers.pop("content-length", None)
            response = await client.send(request)
        assert response.status_code == 200
        assert len(fake_router.handled) == 1


class _ReceiveRecorder:
    """ASGI receive 记录器（逐条投放预置消息并计数）。"""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        message = self._messages[self.calls]
        self.calls += 1
        return message


def _raw_request(
    *,
    receive: Any,
    headers: list[tuple[bytes, bytes]],
    app: FastAPI | None = None,
) -> Request:
    """手工构造 ASGI 请求（body 消费面可记录——不经过传输层）。"""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _URL,
        "raw_path": _URL.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("test", 12345),
        "server": ("test", 80),
    }
    if app is not None:
        scope["app"] = app
    return Request(scope, receive)


class TestBoundedBodyRead:
    """无 Content-Length 的 chunked 体按上限逐块读取（超限即停）。"""

    async def test_bounded_body_reader_stops_reading_after_limit(self) -> None:
        """逐块累计超限：立即受控 400，后续块不再向 ASGI 流索取。"""
        receive = _ReceiveRecorder(
            [
                {"type": "http.request", "body": b"a" * 6, "more_body": True},
                {"type": "http.request", "body": b"b" * 6, "more_body": True},
                {"type": "http.request", "body": b"c", "more_body": False},
            ]
        )
        request = _raw_request(
            receive=receive, headers=[(b"content-type", b"application/json")]
        )
        with pytest.raises(BusinessException) as exc_info:
            await whatsapp_api._read_request_body_bounded(request, 8)
        assert exc_info.value.code is ErrorCode.BAD_REQUEST
        assert receive.calls == 2

    async def test_declared_oversize_rejected_without_reading_stream(self) -> None:
        """声明 Content-Length 超限：端点直接 400，ASGI 流一次都不读。"""
        app = FastAPI(docs_url=None, redoc_url=None)
        app.state.settings = _settings()
        reads: list[str] = []

        async def receive() -> dict[str, Any]:
            reads.append("read")
            raise AssertionError("声明超限时不应读取请求体")

        request = _raw_request(
            receive=receive,
            headers=[
                (b"content-type", b"application/json"),
                (
                    b"content-length",
                    str(WHATSAPP_MAX_BODY_BYTES + 1).encode("ascii"),
                ),
            ],
            app=app,
        )
        with pytest.raises(BusinessException) as exc_info:
            await whatsapp_api.receive_whatsapp_webhook(request)
        assert exc_info.value.code is ErrorCode.BAD_REQUEST
        assert reads == []


class TestRouterNotAssembled:
    async def test_router_missing_acks_and_drops(self) -> None:
        """通道路由器未装配（通道关闭形态）：确认丢弃（不 5xx）。"""
        app = _app(_settings(enabled=False))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_code, body = await _post(client, _payload())
        assert status_code == 200
        assert body == {"status": "ok"}


async def _wait_until(predicate: Any) -> None:
    """事件门轮询（零固定睡眠）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not predicate():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("等待条件超时")
        await asyncio.wait_for(asyncio.sleep(0), timeout=remaining)


class TestQueuedAck:
    """runner 装配形态：端点只入队并立即确认（不等待 LLM 回合）。"""

    async def test_ack_returned_before_slow_route_completes(self) -> None:
        """慢路由器占住 worker 时 POST 仍立即 200，随后后台完成路由。"""
        router = _BlockingRouter()
        runner = WhatsAppWebhookRunner(
            router=router,
            service_window=WhatsAppServiceWindow(),
            owner_numbers=frozenset(),
        )
        app = _app(_settings(), runner=runner)
        task = asyncio.create_task(runner.run())
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                status_code, body = await _post(client, _payload(wamid="wamid.QUEUED"))
            assert status_code == 200
            assert body == {"status": "ok"}
            assert router.handled == []
            await asyncio.wait_for(router.started.wait(), timeout=5.0)
            router.release.set()
            await _wait_until(lambda: len(router.handled) == 1)
            assert router.handled[0].chat_id == "wa:15551234567"
        finally:
            router.release.set()
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

