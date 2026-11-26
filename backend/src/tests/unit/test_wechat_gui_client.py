"""GUI 微信沙盒客户端测试——MockTransport 覆盖成功与全部失败面。

零真实网络：请求经 ``httpx.MockTransport`` 在进程内应答；客户端只消费注入
的 ``httpx.AsyncClient``（共享池形态）。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from packages.harness.channels.wechat_gui.wechat_gui_client import (
    WechatGuiSandboxClient,
)
from packages.harness.core.errors import BusinessException, ErrorCode

_PNG = base64.b64encode(b"png-bytes").decode("ascii")


def _client(
    handler: Any,
    *,
    token: str = "tok",
) -> WechatGuiSandboxClient:
    """构造注入 MockTransport 的客户端。"""
    transport = httpx.MockTransport(handler)
    return WechatGuiSandboxClient(
        httpx.AsyncClient(transport=transport),
        base_url="http://127.0.0.1:8090/",
        token=token,
    )


async def test_screenshot_success_wire_shape() -> None:
    """screenshot：路径 / Bearer / 请求体 / 结果字段校验。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "image_base64": _PNG,
                    "sha256": "abc",
                    "width": 1280,
                    "height": 800,
                },
                "latency_ms": 5,
            },
        )

    client = _client(handler)
    shot = await client.screenshot()
    assert shot.sha256 == "abc" and shot.width == 1280 and shot.height == 800
    assert seen[0].url.path == "/invoke"
    assert seen[0].headers["Authorization"].split() == ["Bearer", "tok"]
    body = json.loads(seen[0].content)
    assert body == {"tool": "screenshot", "arguments": {}}


async def test_tool_methods_argument_mapping() -> None:
    """click / type_text / press_key / open_chat 的参数与结果映射。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        tool = body["tool"]
        if tool == "open_chat":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "remark_name": "小林",
                        "title_image_base64": _PNG,
                        "title_sha256": "t",
                        "full_image_base64": _PNG,
                        "full_sha256": "f",
                    },
                    "latency_ms": 1,
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}, "latency_ms": 1})

    client = _client(handler)
    await client.click(3, 4)
    await client.type_text("晚安")
    await client.press_key("enter")
    opened = await client.open_chat("小林")
    assert opened.remark_name == "小林" and opened.title_sha256 == "t"
    assert bodies[0] == {"tool": "click", "arguments": {"x": 3, "y": 4}}
    assert bodies[1] == {"tool": "type_text", "arguments": {"text": "晚安"}}
    assert bodies[2] == {"tool": "press_key", "arguments": {"key": "enter"}}
    assert bodies[3] == {"tool": "open_chat", "arguments": {"remark_name": "小林"}}


async def test_invoke_ok_false_maps_to_channel_api_error() -> None:
    """恒 200 + ok=False：受控业务错误（携带 agent 报告原因）。"""
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"ok": False, "result": {}, "error": "输入框未找到", "latency_ms": 2},
        )
    )
    with pytest.raises(BusinessException) as error:
        await client.screenshot()
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "输入框未找到" in error.value.message


async def test_invoke_non_200_maps_to_channel_api_error() -> None:
    """401 / 500 等非 200 属传输层失败。"""
    client = _client(lambda request: httpx.Response(401, json={"detail": "unauthorized"}))
    with pytest.raises(BusinessException) as error:
        await client.screenshot()
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "401" in error.value.message


async def test_invoke_transport_error_maps_to_channel_api_error() -> None:
    """传输异常（超时 / 连接失败）→ 受控业务错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)
    with pytest.raises(BusinessException) as error:
        await client.press_key("enter")
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "ReadTimeout" in error.value.message


async def test_invoke_broken_wire_maps_to_channel_api_error() -> None:
    """wire 破坏（result 非对象 / ok 非布尔）→ 受控业务错误。"""
    for payload in (
        {"ok": True, "result": [], "latency_ms": 1},
        {"ok": "yes", "result": {}, "latency_ms": 1},
        {"ok": True, "result": {}, "latency_ms": -1},
        {"ok": True, "result": {}, "error": 3},
    ):
        client = _client(lambda request, payload=payload: httpx.Response(200, json=payload))
        with pytest.raises(BusinessException) as error:
            await client.screenshot()
        assert error.value.code is ErrorCode.CHANNEL_API_ERROR
        assert "wire 契约破坏" in error.value.message


async def test_missing_token_fails_without_http_call() -> None:
    """token 缺失：invoke 短路失败（不发起请求）。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = _client(handler, token="   ")
    assert client.has_token is False
    with pytest.raises(BusinessException) as error:
        await client.screenshot()
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert calls == []


async def test_screenshot_result_missing_field_is_controlled_error() -> None:
    """结果缺字段（width 非整数）→ 受控业务错误。"""
    client = _client(
        lambda request: httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"image_base64": _PNG, "sha256": "a", "width": "1280", "height": 800},
                "latency_ms": 1,
            },
        )
    )
    with pytest.raises(BusinessException) as error:
        await client.screenshot()
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR
    assert "width" in error.value.message


async def test_open_chat_result_missing_field_is_controlled_error() -> None:
    """open_chat 结果缺字段 → 受控业务错误。"""
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"ok": True, "result": {"remark_name": "小林"}, "latency_ms": 1},
        )
    )
    with pytest.raises(BusinessException) as error:
        await client.open_chat("小林")
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR


async def test_probe_health_matrix() -> None:
    """健康探测：成功 / 非 200 / 传输异常 / 非 JSON / status 非 ok。"""
    ok_client = _client(
        lambda request: httpx.Response(200, json={"status": "ok"})
        if request.method == "GET"
        else httpx.Response(200)
    )
    assert await ok_client.probe_health() is True

    bad_status = _client(lambda request: httpx.Response(503))
    assert await bad_status.probe_health() is False

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert await _client(boom).probe_health() is False
    assert (
        await _client(lambda request: httpx.Response(200, content=b"<html>")).probe_health()
        is False
    )
    assert (
        await _client(lambda request: httpx.Response(200, json={"status": "degraded"})).probe_health()
        is False
    )
