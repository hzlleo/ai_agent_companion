"""qq 通道客户端测试——NapCatOneBotClient（MockTransport 零真实网络）。

覆盖矩阵：

    - 构造期 token 解析（env 间接；缺失不 fail fast——NapCat 未开启
      鉴权的合法形态）；
    - 动作请求形状（单一对象包裹 POST 基础 URL / send_private_msg /
      send_group_msg / get_login_info 参数面 / 超时注入 / Bearer 头
      有无）；
    - 成功判定（status=ok / status=async 均成功）；
    - 错误映射表：401/403 → CHANNEL_TOKEN_MISSING；非 200 / retcode
      非 0 / status=failed / 传输异常 / JSON 非法 / data 缺失 →
      CHANNEL_API_ERROR；空文本 → VALIDATION_ERROR。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.harness.channels.qq.qq_client import NapCatOneBotClient
from packages.harness.channels.qq.qq_models import (
    OB11_SUCCESS_STATUSES,
    Ob11MessageSegmentModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

#: conftest 硬覆盖的测试专用 access token 变量（access_token_env 缺省指向形态）。
_TEST_ACCESS_TOKEN_ENV = "QQ_TEST_ACCESS_TOKEN"

#: 测试专用基础 URL（MockTransport 不触达真实网络）。
_TEST_BASE_URL = "http://127.0.0.1:3000"


def _ok(data: Any) -> httpx.Response:
    """构造 status=ok / retcode=0 的动作响应。

    Args:
        data: data 载荷（None = 无载荷形态）。

    Returns:
        200 JSON 响应。
    """
    return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})


def _client(
    handler: Any, *, base_url: str = _TEST_BASE_URL, request_timeout_seconds: float = 15.0
) -> NapCatOneBotClient:
    """构造测试客户端（MockTransport + 测试 token env）。

    Args:
        handler: MockTransport 处理器。
        base_url: NapCat HTTP API 基础 URL。
        request_timeout_seconds: 单次动作请求超时秒数。

    Returns:
        NapCatOneBotClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NapCatOneBotClient(
        http,
        base_url=base_url,
        access_token_env=_TEST_ACCESS_TOKEN_ENV,
        request_timeout_seconds=request_timeout_seconds,
    )


class TestConstruction:
    async def test_token_present_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 环境变量已配置：has_token=True（Bearer 头携带面）。"""
        monkeypatch.setenv(_TEST_ACCESS_TOKEN_ENV, "  test-only-qq-access-token  ")
        client = _client(lambda request: _ok({}))
        assert client.has_token is True

    async def test_token_missing_is_not_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 环境变量缺失：构造不 fail fast（NapCat 未开启鉴权合法形态）。"""
        monkeypatch.delenv(_TEST_ACCESS_TOKEN_ENV, raising=False)
        client = _client(lambda request: _ok({}))
        assert client.has_token is False

    async def test_base_url_trailing_slash_tolerated(self) -> None:
        """基础 URL 尾斜杠容错（rstrip 后 POST 根路径）。"""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return _ok({})

        client = _client(handler, base_url=f"{_TEST_BASE_URL}/")
        await client.send_private_msg(10086, "你好")
        assert captured["url"] == _TEST_BASE_URL


class TestActionEnvelope:
    async def test_send_private_msg_envelope(self) -> None:
        """私聊发送形状：单一对象包裹（action / params.message 纯文本）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": -100200300})

        client = _client(handler)
        response = await client.send_private_msg(10086, "你好")
        assert bodies == [
            {"action": "send_private_msg", "params": {"user_id": 10086, "message": "你好"}}
        ]
        assert response.is_success is True
        assert response.data == {"message_id": -100200300}

    async def test_send_group_msg_envelope(self) -> None:
        """群聊发送形状：单一对象包裹（group_id / message）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": -400500600})

        client = _client(handler)
        await client.send_group_msg(67890, "收到")
        assert bodies == [
            {"action": "send_group_msg", "params": {"group_id": 67890, "message": "收到"}}
        ]

    async def test_get_login_info_happy_path(self) -> None:
        """get_login_info：data 载荷 → 登录账号模型（启动校验消费面）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"user_id": 123456789, "nickname": "小鱼机器人"})

        client = _client(handler)
        login = await client.get_login_info()
        assert bodies == [{"action": "get_login_info", "params": {}}]
        assert login.user_id == 123456789
        assert login.nickname == "小鱼机器人"

    async def test_bearer_header_present_when_token_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 配置时：请求携带 Authorization: Bearer 头。"""
        monkeypatch.setenv(_TEST_ACCESS_TOKEN_ENV, "test-only-qq-access-token")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return _ok({})

        client = _client(handler)
        await client.send_private_msg(1, "hi")
        assert captured["authorization"] == "Bearer test-only-qq-access-token"

    async def test_no_bearer_header_when_token_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 缺失时：请求不携带 Authorization 头（未开启鉴权形态）。"""
        monkeypatch.delenv(_TEST_ACCESS_TOKEN_ENV, raising=False)
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return _ok({})

        client = _client(handler)
        await client.send_private_msg(1, "hi")
        assert captured["authorization"] == ""

    async def test_request_timeout_applied(self) -> None:
        """单次动作请求超时注入（request_timeout_seconds 传导）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["timeout"] = request.extensions.get("timeout")
            return _ok({})

        client = _client(handler, request_timeout_seconds=15.0)
        await client.send_private_msg(1, "hi")
        assert captured["timeout"]["read"] == pytest.approx(15.0)


class TestSuccessJudgement:
    async def test_status_async_is_success(self) -> None:
        """status=async + retcode=0：成功（已受理异步执行形态）。"""
        client = _client(
            lambda request: httpx.Response(
                200, json={"status": "async", "retcode": 0, "data": None}
            )
        )
        response = await client.send_private_msg(10086, "你好")
        assert response.is_success is True

    async def test_success_statuses_value_domain(self) -> None:
        """成功 status 值域 = {ok, async}（锁定决策对齐）。"""
        assert "ok" in OB11_SUCCESS_STATUSES
        assert "async" in OB11_SUCCESS_STATUSES
        assert "failed" not in OB11_SUCCESS_STATUSES
        assert len(OB11_SUCCESS_STATUSES) == 2


class TestErrorMapping:
    async def test_retcode_nonzero_maps_api_error(self) -> None:
        """retcode != 0：CHANNEL_API_ERROR（details 携带动作与 retcode）。"""
        client = _client(
            lambda request: httpx.Response(
                200, json={"status": "failed", "retcode": 1200, "data": None}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["action"] == "send_private_msg"
        assert exc_info.value.details["retcode"] == 1200

    async def test_status_failed_with_zero_retcode_maps_api_error(self) -> None:
        """status=failed + retcode=0：CHANNEL_API_ERROR（status 联合判定）。"""
        client = _client(
            lambda request: httpx.Response(200, json={"status": "failed", "retcode": 0})
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.send_group_msg(67890, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_missing_status_with_zero_retcode_maps_api_error(self) -> None:
        """status 缺失 + retcode=0：保守判失败（联合判定——status 必填面）。"""
        client = _client(lambda request: httpx.Response(200, json={"retcode": 0}))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(1, "hi")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_401_maps_token_missing(self) -> None:
        """401：CHANNEL_TOKEN_MISSING（NapCat 侧开启鉴权而本侧 token 无效）。"""
        client = _client(lambda request: httpx.Response(401, text="Unauthorized"))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_403_maps_token_missing(self) -> None:
        """403：CHANNEL_TOKEN_MISSING（NapCat 鉴权拒绝面）。"""
        client = _client(lambda request: httpx.Response(403, text="Forbidden"))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_login_info()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_non_200_maps_api_error(self) -> None:
        """非 200 状态：CHANNEL_API_ERROR（details 携带动作与状态码）。"""
        client = _client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["action"] == "send_private_msg"
        assert exc_info.value.details["status_code"] == 500

    async def test_transport_error_maps_api_error(self) -> None:
        """httpx 传输异常：CHANNEL_API_ERROR（连接 / 超时统一面）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["action"] == "send_private_msg"

    async def test_malformed_json_maps_api_error(self) -> None:
        """响应非合法 JSON：CHANNEL_API_ERROR。"""
        client = _client(lambda request: httpx.Response(200, text="not-json"))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "你好")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_login_info_data_missing_maps_api_error(self) -> None:
        """get_login_info 成功但 data 缺失：CHANNEL_API_ERROR（载荷不完整面）。"""
        client = _client(lambda request: httpx.Response(200, json={"status": "ok", "retcode": 0}))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_login_info()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_login_info_data_malformed_maps_api_error(self) -> None:
        """get_login_info data 载荷字段非法：CHANNEL_API_ERROR。"""
        client = _client(
            lambda request: httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"user_id": "not-int"}}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.get_login_info()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


class TestSendValidation:
    async def test_empty_text_rejected(self) -> None:
        """空 / 纯空白文本：VALIDATION_ERROR（调用方 bug 面）。"""
        client = _client(lambda request: _ok({}))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg(10086, "   ")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        with pytest.raises(BusinessException) as exc_info:
            await client.send_group_msg(67890, "")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


class TestSegmentSendsV2:
    async def test_send_private_msg_segments_envelope(self) -> None:
        """v2 私聊段数组发送：单一对象包裹 + params.message 段数组形态。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": -100200300})

        client = _client(handler)
        response = await client.send_private_msg_segments(
            10086,
            [
                Ob11MessageSegmentModel(type="text", data={"text": "看看"}),
                Ob11MessageSegmentModel(type="image", data={"file": "base64://aGVsbG8="}),
            ],
        )
        assert bodies == [
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": 10086,
                    "message": [
                        {"type": "text", "data": {"text": "看看"}},
                        {"type": "image", "data": {"file": "base64://aGVsbG8="}},
                    ],
                },
            }
        ]
        assert response.is_success is True

    async def test_send_group_msg_segments_envelope(self) -> None:
        """v2 群聊段数组发送：group_id + 段数组形态（同单一对象包裹）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": -400500600})

        client = _client(handler)
        await client.send_group_msg_segments(
            67890,
            [
                Ob11MessageSegmentModel(type="text", data={"text": "群里看"}),
                Ob11MessageSegmentModel(type="record", data={"file": "https://x/1.silk"}),
            ],
        )
        assert bodies == [
            {
                "action": "send_group_msg",
                "params": {
                    "group_id": 67890,
                    "message": [
                        {"type": "text", "data": {"text": "群里看"}},
                        {"type": "record", "data": {"file": "https://x/1.silk"}},
                    ],
                },
            }
        ]

    async def test_empty_segments_rejected(self) -> None:
        """空段列表：VALIDATION_ERROR（空消息平台必拒——入口先拒）。"""
        client = _client(lambda request: _ok({}))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_private_msg_segments(10086, [])
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        with pytest.raises(BusinessException) as exc_info:
            await client.send_group_msg_segments(67890, [])
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
