"""app/api/channels_sms_forwarder 路由测试——ASGITransport 零真实网络。

测试策略：本地装配 FastAPI app（SmsForwarder webhook 路由 + 共享异常处理器）；
通道路由器 / runner 经 ``app.state`` 注入 fake；token 来自隔离 Settings
（``SmsForwarderChannelSettings.token``）——鉴权矩阵不依赖进程 env。
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.api.channels_sms_forwarder as sms_forwarder_api
from app.api.channels_sms_forwarder import (
    SMS_FORWARDER_MAX_BODY_BYTES,
)
from app.api.channels_sms_forwarder import (
    router as sms_forwarder_channels_router,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.sms_forwarder.sms_models import reset_sms_forwarder_state
from packages.harness.channels.sms_forwarder.sms_runner import SmsForwarderWebhookRunner
from packages.harness.config.models import (
    ChannelsSettings,
    Settings,
    SmsForwarderChannelSettings,
)
from packages.harness.config.settings import build_isolated_settings
from tests.async_wait import wait_until

_TEST_TOKEN = "test-only-sms-forwarder-token"


class FakeChannelRouter:
    """fake ChannelRouter——记录入站消息（路由目标）。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.SMS_FORWARDER,
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
            channel=ChannelType.SMS_FORWARDER,
            chat_id=message.chat_id,
            message_ids=[],
        )


def _settings(
    *,
    token: str = _TEST_TOKEN,
    notifications_enabled: bool = False,
    owner_numbers: str = "",
    enabled: bool = True,
) -> Settings:
    """构造隔离 Settings（SmsForwarder 配置显式值）。"""
    return build_isolated_settings(
        channels=ChannelsSettings(
            sms_forwarder=SmsForwarderChannelSettings(
                enabled=enabled,
                token=token,
                notifications_enabled=notifications_enabled,
                owner_numbers=owner_numbers,
            )
        )
    )


def _app(
    settings: Settings,
    *,
    router: Any = None,
    runner: SmsForwarderWebhookRunner | None = None,
) -> FastAPI:
    """装配最小 FastAPI app（路由 + 异常处理器 + 注入面）。"""
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(sms_forwarder_channels_router)
    register_exception_handlers(app)
    app.state.settings = settings
    if router is not None:
        app.state.channels_router = router
    if runner is not None:
        app.state.channels_sms_forwarder_runner = runner
    return app


@pytest.fixture(autouse=True)
def _reset_direct_dedup() -> None:
    """每测试复位直调路径的进程内去重窗口。"""
    reset_sms_forwarder_state()


def _payload(
    *,
    msgid: str = "m-1",
    content: str = "验证码 123456",
    kind: str = "SMS",
    sender: str = "13800138000",
    **overrides: Any,
) -> dict[str, Any]:
    """构造转发载荷（测试直供形态）。"""
    payload: dict[str, Any] = {
        "from": sender,
        "content": content,
        "kind": kind,
        "msgid": msgid,
        "timestamp": 1_731_000_000_000,
    }
    payload.update(overrides)
    return payload


def _auth_headers(token: str = _TEST_TOKEN) -> dict[str, str]:
    """构造 Bearer 鉴权头。"""
    return {"Authorization": f"Bearer {token}"}


class TestHappyPath:
    async def test_accepted_payload_returns_ok_shape(self) -> None:
        """接受路径：200 + 恒确认形状 + 入站归一字段（channel / chat / peer）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert len(fake_router.handled) == 1
        message = fake_router.handled[0]
        assert message.channel is ChannelType.SMS_FORWARDER
        assert message.chat_id == "sms:13800138000"
        assert message.synthetic is False
        assert message.is_owner is False

    async def test_owner_numbers_mark_owner_provenance_source(self) -> None:
        """属主清单命中时入站消息 is_owner=True（来源分级原料）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(owner_numbers="13800138000"), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert fake_router.handled[0].is_owner is True

    async def test_custom_header_and_query_token_accepted(self) -> None:
        """手机端自定义头 / 查询参数两形态均可携带 token。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            header_response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(msgid="h-1"),
                headers={"X-SmsForwarder-Token": _TEST_TOKEN},
            )
            query_response = await client.post(
                f"/api/v1/channels/sms-forwarder?token={_TEST_TOKEN}",
                json=_payload(msgid="q-1"),
            )
        assert header_response.status_code == 200
        assert query_response.status_code == 200
        assert len(fake_router.handled) == 2

    async def test_notification_disabled_ack_without_routing(self) -> None:
        """通知关闭：确认 200 且不进入路由器（不消耗去重）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(kind="notification", msgid="same-id"),
                headers=_auth_headers(),
            )
            assert response.status_code == 200
            assert fake_router.handled == []
            second = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(kind="SMS", msgid="same-id"),
                headers=_auth_headers(),
            )
        assert second.status_code == 200
        assert len(fake_router.handled) == 1

    async def test_notification_enabled_routes(self) -> None:
        """通知开关开启：notification 正常进入路由器。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(notifications_enabled=True), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(kind="notification", msgid="n-1"),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert len(fake_router.handled) == 1

    async def test_invalid_field_isolated_ack(self) -> None:
        """JSON 对象但字段非法：200 隔离确认（处理器容错——不进 4xx）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json={"from": "1", "content": "hi", "kind": ["bad"]},
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert fake_router.handled == []


class TestAuthMatrix:
    async def test_missing_token_401(self) -> None:
        """token 配置时缺失鉴权：401（UNAUTHORIZED ResultDTO）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder", json=_payload()
            )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "UNAUTHORIZED"
        assert fake_router.handled == []

    async def test_wrong_token_403(self) -> None:
        """token 不匹配：403（FORBIDDEN）+ 路由器零调用。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(),
                headers=_auth_headers("wrong-token"),
            )
        assert response.status_code == 403
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "FORBIDDEN"
        assert fake_router.handled == []

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

        monkeypatch.setattr(sms_forwarder_api.secrets, "compare_digest", _record)
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        presented = _TEST_TOKEN.encode("utf-8")
        assert calls == [(presented, presented)]

    async def test_auth_disabled_when_token_empty(self) -> None:
        """token 为空：鉴权关闭（无头亦 200——同机 / 可信内网形态）。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(token=""), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder", json=_payload()
            )
        assert response.status_code == 200
        assert len(fake_router.handled) == 1

    async def test_rejected_request_does_not_consume_dedup(self) -> None:
        """鉴权拒绝在体解析 / 去重之前：同一 msgid 的后续正确请求照常路由。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            rejected = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(msgid="dedup-1"),
                headers=_auth_headers("wrong-token"),
            )
            accepted = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(msgid="dedup-1"),
                headers=_auth_headers(),
            )
        assert rejected.status_code == 403
        assert accepted.status_code == 200
        assert len(fake_router.handled) == 1


class TestBodyValidation:
    @pytest.mark.parametrize("media_type", ["text/plain", "application/x-www-form-urlencoded", ""])
    async def test_non_json_content_type_400(self, media_type: str) -> None:
        """非 application/json（含缺失）：400 BAD_REQUEST。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        headers = {**_auth_headers(), "Content-Type": media_type}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                content=b'{"from": "1", "content": "hi"}',
                headers=headers,
            )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert fake_router.handled == []

    async def test_malformed_json_400(self) -> None:
        """请求体非合法 JSON：400。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        headers = {**_auth_headers(), "Content-Type": "application/json"}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder", content="not-json", headers=headers
            )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    async def test_empty_body_400(self) -> None:
        """空请求体：400（非法 JSON 形态）。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        headers = {**_auth_headers(), "Content-Type": "application/json"}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder", content="", headers=headers
            )
        assert response.status_code == 400

    @pytest.mark.parametrize("payload", [[1, 2], "hello", 42])
    async def test_non_object_json_400(self, payload: Any) -> None:
        """JSON 非对象：400（转发模板须为对象）+ 路由器零调用。"""
        fake_router = FakeChannelRouter()
        app = _app(_settings(), router=fake_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=payload,
                headers=_auth_headers(),
            )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"
        assert fake_router.handled == []

    async def test_oversized_body_400(self) -> None:
        """请求体超过上限：400（短文本转发载荷——64 KiB 足够）。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        oversized = {"from": "1", "content": "x" * (SMS_FORWARDER_MAX_BODY_BYTES + 1024)}
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=oversized,
                headers=_auth_headers(),
            )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"

    async def test_deeply_nested_json_400(self) -> None:
        """极深嵌套 JSON（递归上限）：400 归一（不 500）。"""
        app = _app(_settings(), router=FakeChannelRouter())
        transport = ASGITransport(app=app)
        headers = {**_auth_headers(), "Content-Type": "application/json"}
        deep_json = "[" * 10000 + "]" * 10000
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder", content=deep_json, headers=headers
            )
        assert response.status_code == 400
        assert response.json()["code"] == "BAD_REQUEST"


class TestRouterNotAssembled:
    async def test_router_missing_acks_and_drops(self) -> None:
        """通道路由器未装配（通道关闭形态）：确认丢弃（不 5xx）。"""
        app = _app(_settings(enabled=False))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/sms-forwarder",
                json=_payload(),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestQueuedAck:
    """runner 装配形态：端点只入队并立即确认（不等待 LLM 回合）。"""

    async def test_ack_returned_before_slow_route_completes(self) -> None:
        """慢路由器占住 worker 时 POST 仍立即 200，随后后台完成路由。"""
        router = _BlockingRouter()
        runner = SmsForwarderWebhookRunner(
            router=router,
            notifications_enabled=False,
            owner_numbers=frozenset(),
        )
        app = _app(_settings(), runner=runner)
        task = asyncio.create_task(runner.run())
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/channels/sms-forwarder",
                    json=_payload(msgid="queued-1"),
                    headers=_auth_headers(),
                )
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert router.handled == []
            await asyncio.wait_for(router.started.wait(), timeout=5.0)
            router.release.set()
            await wait_until(lambda: len(router.handled) == 1)
            assert router.handled[0].chat_id == "sms:13800138000"
        finally:
            router.release.set()
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)
