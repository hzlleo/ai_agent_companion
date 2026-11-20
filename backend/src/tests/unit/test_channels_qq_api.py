"""app/api/channels_qq 路由测试——QQ webhook 端点（ASGITransport 零真实网络）。

测试策略：本地装配 FastAPI app（QQ webhook 路由 + 共享异常处理器）；
通道路由器经 ``app.state.channels_router`` 注入 fake（真实 webhook 处理器
承载展平 / 去重 / 隔离面）；token 矩阵经 conftest 硬覆盖的全局 settings
（webhook_token_env → QQ_TEST_WEBHOOK_TOKEN）+ monkeypatch env 驱动。

覆盖面：恒 200 确认形状 ``{"status": "ok"}``；鉴权矩阵（token 配置时
缺失 / scheme 不符 / 不匹配 → 401；正确 → 200；env 为空 → 鉴权关闭）；
请求体校验（非 JSON / 非 JSON 对象 → 400）；路由器未装配（通道关闭
形态）确认丢弃不 5xx；事件经真实处理器路由到 fake 路由器。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.channels_qq import router as qq_channels_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)

#: conftest 硬覆盖的测试专用 webhook token 变量（webhook_token_env 指向形态）。
_TEST_WEBHOOK_TOKEN_ENV = "QQ_TEST_WEBHOOK_TOKEN"


class FakeChannelRouter:
    """fake ChannelRouter——记录入站消息（webhook 处理器路由目标）。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 路由（记录入站——恒成功投递形态）。"""
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.QQ, chat_id=message.chat_id, message_ids=["1"]
        )


def _message_event(message_id: int = 1001) -> dict[str, Any]:
    """构造 message 事件上报 wire dict（测试直供形态）。

    Args:
        message_id: 消息 ID（逐测试错开——webhook 处理器进程内去重）。

    Returns:
        事件 wire dict。
    """
    return {
        "post_type": "message",
        "time": 1731000000,
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": 10086,
        "message": [{"type": "text", "data": {"text": "你好"}}],
    }


def _auth_headers() -> dict[str, str]:
    """构造正确的鉴权头（conftest 硬覆盖的测试专用 token）。"""
    return {"Authorization": f"Bearer {os.environ.get(_TEST_WEBHOOK_TOKEN_ENV, '')}"}


@pytest.fixture
async def qq_app() -> AsyncIterator[tuple[FastAPI, FakeChannelRouter]]:
    """本地装配的最小 FastAPI app（QQ webhook 路由 + 异常处理器 + fake 路由器）。"""
    fake_router = FakeChannelRouter()
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(qq_channels_router)
    register_exception_handlers(app)
    app.state.channels_router = fake_router
    yield app, fake_router


@pytest.fixture
async def client(qq_app: tuple[FastAPI, FakeChannelRouter]) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。"""
    transport = ASGITransport(app=qq_app[0])
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture(autouse=True)
def _reset_webhook_dedup() -> None:
    """每测试复位 webhook 处理器进程内去重窗口（隔离铁律）。"""
    from packages.harness.channels.qq.qq_webhook import reset_qq_webhook_state

    reset_qq_webhook_state()


class TestHappyPath:
    async def test_accepted_event_returns_ok_shape(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """接受路径：200 + 恒确认形状 ``{"status": "ok"}`` + 事件已路由。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook", json=_message_event(), headers=_auth_headers()
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        fake_router = qq_app[1]
        assert len(fake_router.handled) == 1
        assert fake_router.handled[0].chat_id == "private:10086"

    async def test_ignored_event_still_returns_ok(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """忽略类事件（notice）：同样 200 确认（上报方重投防线）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            json=_message_event(message_id=2002) | {"post_type": "notice"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert qq_app[1].handled == []


class TestAuthMatrix:
    async def test_missing_authorization_header_401(self, client: AsyncClient) -> None:
        """token 配置时缺失 Authorization 头：401（UNAUTHORIZED ResultDTO）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook", json=_message_event(message_id=3003)
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "UNAUTHORIZED"

    async def test_wrong_scheme_401(self, client: AsyncClient) -> None:
        """scheme 不符（非 Bearer）：401。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            json=_message_event(message_id=4004),
            headers={"Authorization": "Basic dXNlcjpwd2Q="},
        )
        assert response.status_code == 401

    async def test_bare_token_without_scheme_401(self, client: AsyncClient) -> None:
        """裸 token（无 Bearer scheme）：401（头形态契约）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            json=_message_event(message_id=5005),
            headers={"Authorization": os.environ.get(_TEST_WEBHOOK_TOKEN_ENV, "")},
        )
        assert response.status_code == 401

    async def test_token_mismatch_401(self, client: AsyncClient) -> None:
        """Bearer 值不匹配：401（恒时比较面）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            json=_message_event(message_id=6006),
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    async def test_auth_disabled_when_env_empty(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 环境变量为空：鉴权关闭（无头亦 200——可信内网形态）。"""
        monkeypatch.delenv(_TEST_WEBHOOK_TOKEN_ENV, raising=False)
        response = await client.post(
            "/api/v1/channels/qq/webhook", json=_message_event(message_id=7007)
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_rejected_request_does_not_reach_router(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """鉴权拒绝在体解析之前：路由器零调用（鉴权先行顺序锁定）。"""
        await client.post("/api/v1/channels/qq/webhook", json=_message_event(message_id=8008))
        assert qq_app[1].handled == []


class TestBodyValidation:
    async def test_non_object_json_body_400(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """JSON 非对象（数组 / 字面量）：400（BAD_REQUEST）+ 路由器零调用。"""
        for payload in ([1, 2], "hello", 42):
            response = await client.post(
                "/api/v1/channels/qq/webhook",
                json=payload,
                headers=_auth_headers(),
            )
            assert response.status_code == 400
            body = response.json()
            assert body["success"] is False
            assert body["code"] == "BAD_REQUEST"
        assert qq_app[1].handled == []

    async def test_malformed_json_body_400(self, client: AsyncClient) -> None:
        """请求体非合法 JSON：400。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            content="not-json",
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    async def test_empty_body_400(self, client: AsyncClient) -> None:
        """空请求体：400（非法 JSON 形态）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            content="",
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    async def test_deeply_nested_json_body_400(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """极深嵌套 JSON（json.loads 递归上限）：400 归一（不 500——
        恒 200/4xx 契约的 RecursionError 破口回归锚）。"""
        deep_json = "[" * 200000 + "]" * 200000
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            content=deep_json,
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "BAD_REQUEST"
        assert qq_app[1].handled == []

    async def test_malformed_object_isolated_ack(
        self, client: AsyncClient, qq_app: tuple[FastAPI, FakeChannelRouter]
    ) -> None:
        """JSON 对象但字段非法：200 隔离确认（处理器容错——不进 4xx）。"""
        response = await client.post(
            "/api/v1/channels/qq/webhook",
            json={"post_type": "message", "message_id": "not-an-int"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert qq_app[1].handled == []


class TestRouterNotAssembled:
    async def test_router_missing_acks_and_drops(self) -> None:
        """通道路由器未装配（通道关闭形态）：确认丢弃（不 5xx——webhook
        端点不因配置缺失而失败）。"""
        app = FastAPI(docs_url=None, redoc_url=None)
        app.include_router(qq_channels_router)
        register_exception_handlers(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as bare_client:
            response = await bare_client.post(
                "/api/v1/channels/qq/webhook",
                json=_message_event(message_id=9009),
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


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
            channel=ChannelType.QQ, chat_id=message.chat_id, message_ids=["1"]
        )


async def _wait_until(predicate: Any) -> None:
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
        from packages.harness.channels.qq.qq_runner import QqWebhookRunner

        router = _BlockingRouter()
        runner = QqWebhookRunner(router=router)
        app = FastAPI(docs_url=None, redoc_url=None)
        app.include_router(qq_channels_router)
        register_exception_handlers(app)
        app.state.channels_qq_runner = runner
        task = asyncio.create_task(runner.run())
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as async_client:
                response = await async_client.post(
                    "/api/v1/channels/qq/webhook",
                    json=_message_event(message_id=7001),
                    headers=_auth_headers(),
                )
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert router.handled == []
            await asyncio.wait_for(router.started.wait(), timeout=5.0)
            router.release.set()
            await _wait_until(lambda: len(router.handled) == 1)
            assert router.handled[0].chat_id == "private:10086"
        finally:
            router.release.set()
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)
