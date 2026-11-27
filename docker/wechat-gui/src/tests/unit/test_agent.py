"""agent 异步 HTTP 工具面与 wire 模型的 hermetic 单元测试。

真实 socket 仅落在 127.0.0.1 的临时端口（无外网、无真实 X / uinput）；截图 /
剪贴板 / 键鼠注入全部经注入替身记录，断言按记录值与脚本化返回值进行，不做
墙钟等待（有界等待用虚拟时钟推演）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from wechat_gui_poc import agent, wire_models
from wechat_gui_poc.agent import (
    AgentRuntime,
    AgentToolError,
    ImagemagickScreenshot,
    ParsedHttpRequest,
    ProcessOutput,
    XclipClipboard,
    parse_http_request,
    parse_key_expression,
    resolve_agent_token,
    run_process,
)
from wechat_gui_poc.uinput_devices import (
    KEY_ENTER,
    KEY_LEFTCTRL,
    KEY_LEFTSHIFT,
    KEY_SPACE,
)
from wechat_gui_poc.wire_models import (
    ClickRequest,
    ClickResultModel,
    ClickToolArgs,
    CommandOutcome,
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
    KeyRequest,
    MoveRequest,
    OpenChatResultModel,
    OpenChatToolArgs,
    PressKeyResultModel,
    PressKeyToolArgs,
    ScreenshotResultModel,
    ScreenshotToolArgs,
    TypeTextResultModel,
    TypeTextToolArgs,
)


class FakeControl:
    """记录式异步控制面（可注入失败与未预期异常）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_types: set[type[object]] = set()
        self.error: Exception | None = None

    async def send(self, request: Any) -> CommandOutcome:
        """记录请求并按注入类型返回失败或抛异常。"""
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if type(request) in self.fail_types:
            return CommandOutcome(command=request.command.value, ok=False, error="注入失败")
        return CommandOutcome(command=request.command.value, ok=True)


class FakeScreenshots:
    """脚本化异步截图替身（可注入异常与等待闸门）。"""

    def __init__(
        self, payloads: list[bytes] | None = None, gate: asyncio.Event | None = None
    ) -> None:
        self.payloads = list(payloads or [b"png-default"])
        self.crops: list[tuple[int, int] | None] = []
        self.error: Exception | None = None
        self.gate = gate
        self.started = asyncio.Event()

    async def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
        """记录裁剪参数并返回脚本化字节。"""
        self.crops.append(crop)
        if self.gate is not None:
            self.started.set()
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]


class FakeClipboard:
    """记录式异步剪贴板替身。"""

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def write(self, text: str) -> None:
        """记录写入文本。"""
        self.writes.append(text)


class FakeClock:
    """可控单调时钟与记录式异步休眠（有界等待按虚拟时间推演）。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        """返回虚拟单调时间。"""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """记录并推进虚拟时间。"""
        self.delays.append(seconds)
        self.now += seconds


def _runtime(
    control: FakeControl,
    screenshots: FakeScreenshots,
    clipboard: FakeClipboard,
    clock: FakeClock | None = None,
) -> AgentRuntime:
    """构造注入替身的运行时（显式参数——不读全局配置）。"""
    fake_clock = clock or FakeClock()
    return AgentRuntime(
        control=control,
        screenshots=screenshots,
        clipboard=clipboard,
        width=1280,
        height=800,
        title_height=140,
        open_chat_timeout_seconds=1.0,
        settle_seconds=0.1,
        sleep=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        now_ms=lambda: 1731000000000,
    )


@asynccontextmanager
async def _serve(runtime: AgentRuntime, token: str = "secret") -> AsyncIterator[int]:
    """在临时回环端口启动 agent 并在退出时关闭。"""
    server = await agent.start_server(runtime, token, port=0)
    sockets = server.sockets or ()
    port = sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def _raw_request(port: int, raw: bytes) -> tuple[int, dict[str, Any], dict[str, str]]:
    """发送原始报文并解析响应（连接由服务端关闭）。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 5.0)
    writer.close()
    await writer.wait_closed()
    head, separator, body = response.partition(b"\r\n\r\n")
    assert separator
    lines = head.decode("utf-8", errors="replace").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {
        key.strip().lower(): value.strip()
        for line in lines[1:]
        if ":" in line
        for key, _, value in [line.partition(":")]
    }
    payload = json.loads(body.decode("utf-8")) if body else {}
    return status, payload, headers


async def _request(
    port: int,
    *,
    method: str = "GET",
    path: str = "/health",
    body: bytes = b"",
    token: str | None = None,
    content_length: int | None = None,
    omit_content_length: bool = False,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """发送一次 JSON 请求并解析响应。"""
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
    if not omit_content_length:
        declared = len(body) if content_length is None else content_length
        lines.append(f"Content-Length: {declared}")
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    for key, value in (extra_headers or {}).items():
        lines.append(f"{key}: {value}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    return await _raw_request(port, raw)


async def _invoke(
    port: int, tool: str, arguments: Mapping[str, object], *, token: str = "secret"
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """发送一次 /invoke 工具调用。"""
    body = json.dumps({"tool": tool, "arguments": dict(arguments)}).encode("utf-8")
    return await _request(
        port, method="POST", path="/invoke", body=body, token=token
    )


def _parsed_invoke(
    tool: str, arguments: Mapping[str, object], *, token: str = "secret"
) -> ParsedHttpRequest:
    """构造一个已解析的 /invoke 请求（绕过网络——串行化用例直调）。"""
    body = json.dumps({"tool": tool, "arguments": dict(arguments)}).encode("utf-8")
    return ParsedHttpRequest(
        method="POST",
        target="/invoke",
        headers={"authorization": f"Bearer {token}"},
        body=body,
    )


async def test_wire_models_serialization_round_trip() -> None:
    """wire 模型序列化与解码形态（健康 / 请求 / 响应 / 五工具结果）。"""
    assert HealthResponseModel().to_wire() == {"status": "ok"}
    request = InvokeRequestModel(tool="screenshot", arguments={"a": 1})
    assert request.to_wire() == {"tool": "screenshot", "arguments": {"a": 1}}
    assert InvokeRequestModel.from_wire(request.to_wire()) == request
    success = InvokeResponseModel(ok=True, result={"x": 1}, latency_ms=3)
    assert success.to_wire() == {"ok": True, "result": {"x": 1}, "latency_ms": 3}
    assert InvokeResponseModel.from_wire(success.to_wire()) == success
    failure = InvokeResponseModel(ok=False, error="坏", latency_ms=4)
    assert failure.to_wire() == {
        "ok": False,
        "result": {},
        "error": "坏",
        "latency_ms": 4,
    }

    assert ScreenshotResultModel.from_wire(
        {"image_base64": "a", "sha256": "b", "width": 1, "height": 2}
    ) == ScreenshotResultModel(image_base64="a", sha256="b", width=1, height=2)
    assert ClickResultModel.from_wire({"x": 1, "y": 2}) == ClickResultModel(x=1, y=2)
    assert TypeTextResultModel.from_wire({"chars": 3}) == TypeTextResultModel(chars=3)
    assert PressKeyResultModel.from_wire({"key": "enter"}) == PressKeyResultModel(
        key="enter"
    )
    open_chat = OpenChatResultModel(
        remark_name="小林",
        title_image_base64="t",
        full_image_base64="f",
        title_sha256="ts",
        full_sha256="fs",
        title_changed=True,
    )
    assert OpenChatResultModel.from_wire(open_chat.to_wire()) == open_chat


async def test_wire_model_decoders_reject_invalid_shapes() -> None:
    """wire 解码矩阵：根节点非对象 / 字段缺失 / 类型不符。"""
    with pytest.raises(ValueError, match="根节点"):
        InvokeRequestModel.from_wire([])
    with pytest.raises(ValueError, match="未声明"):
        InvokeRequestModel.from_wire({"tool": "a", "extra": 1})
    with pytest.raises(ValueError, match="tool"):
        InvokeRequestModel.from_wire({"tool": " "})
    with pytest.raises(ValueError, match="arguments"):
        InvokeRequestModel.from_wire({"tool": "a", "arguments": []})
    with pytest.raises(ValueError, match="ok"):
        InvokeResponseModel.from_wire({"ok": "yes"})
    with pytest.raises(ValueError, match="latency_ms"):
        InvokeResponseModel.from_wire({"ok": True, "latency_ms": -1})
    with pytest.raises(ValueError, match="image_base64"):
        ScreenshotResultModel.from_wire({"sha256": "b", "width": 1, "height": 1})
    with pytest.raises(ValueError, match="width"):
        ScreenshotResultModel.from_wire(
            {"image_base64": "a", "sha256": "b", "width": 0, "height": 1}
        )
    with pytest.raises(ValueError, match="x"):
        ClickResultModel.from_wire({"x": True, "y": 1})
    with pytest.raises(ValueError, match="chars"):
        TypeTextResultModel.from_wire({"chars": -1})
    with pytest.raises(ValueError, match="key"):
        PressKeyResultModel.from_wire({"key": ""})
    with pytest.raises(ValueError, match="title_changed"):
        OpenChatResultModel.from_wire(
            {
                "remark_name": "a",
                "title_image_base64": "t",
                "full_image_base64": "f",
                "title_sha256": "ts",
                "full_sha256": "fs",
                "title_changed": "yes",
            }
        )


async def test_tool_argument_models_validate_shapes() -> None:
    """五工具参数模型：合法构造 + 非法字段拒绝。"""
    assert ScreenshotToolArgs.from_arguments({}) == ScreenshotToolArgs()
    assert ClickToolArgs.from_arguments({"x": 1, "y": 2}) == ClickToolArgs(1, 2)
    assert TypeTextToolArgs.from_arguments({"text": " 你好 "}).text == " 你好 "
    assert PressKeyToolArgs.from_arguments({"key": " ctrl+f "}).key == "ctrl+f"
    assert OpenChatToolArgs.from_arguments({"remark_name": " 小林 "}).remark_name == "小林"

    with pytest.raises(ValueError, match="不接受参数"):
        ScreenshotToolArgs.from_arguments({"full": True})
    with pytest.raises(ValueError, match="非负整数"):
        ClickToolArgs.from_arguments({"x": -1, "y": 2})
    with pytest.raises(ValueError, match="非空字符串"):
        TypeTextToolArgs.from_arguments({"text": "   "})
    with pytest.raises(ValueError, match="非空字符串"):
        PressKeyToolArgs.from_arguments({"key": ""})
    with pytest.raises(ValueError, match="非空字符串"):
        OpenChatToolArgs.from_arguments({"remark_name": "  "})


async def test_parse_key_expression_matrix() -> None:
    """按键表达式解析：具名键 / 字母 / 数字 / 组合键 / 非法形态。"""
    assert parse_key_expression("enter") == ([], KEY_ENTER)
    assert parse_key_expression("space") == ([], KEY_SPACE)
    assert parse_key_expression("ctrl+f") == ([KEY_LEFTCTRL], 33)
    assert parse_key_expression("ctrl+shift+a") == ([KEY_LEFTCTRL, KEY_LEFTSHIFT], 30)
    assert parse_key_expression("0") == ([], 11)
    assert parse_key_expression("7") == ([], 8)
    with pytest.raises(ValueError, match="不得为空"):
        parse_key_expression(" + ")
    with pytest.raises(ValueError, match="修饰键"):
        parse_key_expression("f+ctrl")
    with pytest.raises(ValueError, match="缺少主键"):
        parse_key_expression("ctrl+shift")
    with pytest.raises(ValueError, match="修饰键重复"):
        parse_key_expression("ctrl+control+f")
    with pytest.raises(ValueError, match="不支持的按键"):
        parse_key_expression("ctrl+f13")


async def test_resolve_agent_token_env_and_file(tmp_path: Path) -> None:
    """token 解析：明文优先 / 文件回落 / 缺失为空串。"""
    token_file = tmp_path / "token"
    token_file.write_text(" file-token \n", encoding="utf-8")
    assert resolve_agent_token(token_file=token_file, environ={}) == "file-token"
    assert (
        resolve_agent_token(
            token_file=token_file,
            environ={
                agent.AGENT_TOKEN_ENV: " env-token ",
                agent.AGENT_TOKEN_FILE_ENV: str(tmp_path / "missing"),
            },
        )
        == "env-token"
    )
    assert resolve_agent_token(token_file=tmp_path / "absent", environ={}) == ""


async def test_parse_http_request_matrix() -> None:
    """HTTP 解析纯函数：合法报文 + 413 / 422 边界。"""
    parsed = parse_http_request(
        b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert parsed == ParsedHttpRequest(
        method="GET", target="/health", headers={"host": "127.0.0.1"}, body=b""
    )
    parsed = parse_http_request(
        b"POST /invoke HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
    )
    assert parsed.method == "POST" and parsed.body == b"{}"
    cases = [
        b"GET /health HTTP/1.1\r\nHost: x",
        b"GET\r\n\r\n",
        b"GET /health HTTP/2\r\n\r\n",
        b"GET /health HTTP/1.1\r\nBroken\r\n\r\n",
        b"POST /invoke HTTP/1.1\r\n\r\n",
        b"POST /invoke HTTP/1.1\r\nContent-Length: x\r\n\r\n",
        b"POST /invoke HTTP/1.1\r\nContent-Length: \xc2\xb2\r\n\r\n",
        b"POST /invoke HTTP/1.1\r\nContent-Length: 3\r\n\r\n{}",
        b"POST /invoke HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /invoke HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
    ]
    for raw in cases:
        with pytest.raises(agent.HttpRequestError) as error:
            parse_http_request(raw)
        assert error.value.status == 422, raw
    oversized = (
        b"POST /invoke HTTP/1.1\r\nContent-Length: "
        + str(wire_models.MAX_AGENT_REQUEST_BYTES + 1).encode()
        + b"\r\n\r\n"
    )
    with pytest.raises(agent.HttpRequestError) as error:
        parse_http_request(oversized)
    assert error.value.status == 413
    huge_digits = (
        b"POST /invoke HTTP/1.1\r\nContent-Length: " + b"9" * 5_000 + b"\r\n\r\n"
    )
    with pytest.raises(agent.HttpRequestError) as error:
        parse_http_request(huge_digits)
    assert error.value.status == 413
    huge_head = b"GET /health HTTP/1.1\r\nX-Pad: " + b"a" * 70_000 + b"\r\n\r\n"
    with pytest.raises(agent.HttpRequestError) as error:
        parse_http_request(huge_head)
    assert error.value.status == 413


async def test_health_endpoint_is_open_and_unknown_path_is_404() -> None:
    """GET /health 无鉴权返回存活状态；未知路径 404。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        status, payload, headers = await _request(port, path="/health")
        assert status == 200 and payload == {"status": "ok"}
        assert headers["connection"] == "close"
        status, _, _ = await _request(port, path="/nope")
        assert status == 404
        status, _, _ = await _request(port, method="PUT", path="/health")
        assert status == 405


async def test_bearer_auth_matrix_including_non_ascii() -> None:
    """鉴权矩阵：缺失 / 错误 / 非 ASCII / 正确；未知路径先 404。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    async with _serve(runtime, token="秘密") as port:
        body = b'{"tool":"screenshot","arguments":{}}'
        missing, _, _ = await _request(
            port, method="POST", path="/invoke", body=body
        )
        wrong, _, _ = await _request(
            port, method="POST", path="/invoke", body=body, token="错误"
        )
        ok, payload, _ = await _request(
            port, method="POST", path="/invoke", body=body, token="秘密"
        )
        not_found, _, _ = await _request(
            port, method="POST", path="/nope", body=body, token="错误"
        )
        assert (missing, wrong, ok, not_found) == (401, 401, 200, 404)
        assert payload["ok"] is True


async def test_request_shape_failures_are_422() -> None:
    """请求形态非法（非对象 / 缺 tool / arguments 非对象 / JSON 损坏 / 多余字段）→ 422。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        cases = [
            b"[1, 2]",
            b'{"arguments": {}}',
            b'{"tool": "screenshot", "arguments": []}',
            b"not-json",
            b'{"tool": "screenshot", "extra": 1}',
        ]
        for body in cases:
            status, payload, _ = await _request(
                port, method="POST", path="/invoke", body=body, token="secret"
            )
            assert status == 422, body
            assert payload["detail"]


async def test_deeply_nested_json_is_422() -> None:
    """深层嵌套 JSON（RecursionError）→ 422，不断连。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    body = b"[" * 10_000 + b"0" + b"]" * 10_000
    async with _serve(runtime) as port:
        status, payload, _ = await _request(
            port, method="POST", path="/invoke", body=body, token="secret"
        )
        assert status == 422
        assert payload["detail"] == "请求体不是合法 JSON"


async def test_oversized_body_is_413_before_read() -> None:
    """Content-Length 超过上限 → 413（不读取请求体）。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        status, payload, _ = await _request(
            port,
            method="POST",
            path="/invoke",
            token="secret",
            content_length=wire_models.MAX_AGENT_REQUEST_BYTES + 1,
        )
        assert status == 413
        assert "上限" in payload["detail"]


async def test_non_ascii_content_length_is_422_not_500() -> None:
    """Content-Length 非 ASCII 数字（²）→ 422（不落 int() 的 ValueError → 500）。"""
    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        raw = (
            "POST /invoke HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Content-Length: ²\r\nConnection: close\r\n\r\n"
        ).encode()
        status, payload, _ = await _raw_request(port, raw)
        assert status == 422
        assert payload["detail"]


async def test_tool_failure_is_200_with_ok_false() -> None:
    """工具级失败恒 200 + ok=false（未知工具 / 参数非法 / 执行失败）。"""
    control = FakeControl()
    control.fail_types.add(MoveRequest)
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        for tool, arguments in (
            ("nope", {}),
            ("click", {"x": "bad", "y": 1}),
            ("click", {"x": 1, "y": 1}),
        ):
            status, payload, _ = await _invoke(port, tool, arguments)
            assert status == 200
            assert payload["ok"] is False
            assert payload["result"] == {}
            assert payload["error"]


async def test_unexpected_tool_exception_is_200_with_ok_false() -> None:
    """工具内未预期异常 → 恒 200 + ok=false（不冒到 500）。"""
    control = FakeControl()
    control.error = RuntimeError("控制面炸了")
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    async with _serve(runtime) as port:
        status, payload, _ = await _invoke(port, "click", {"x": 1, "y": 2})
        assert status == 200
        assert payload["ok"] is False
        assert payload["result"] == {}
        assert "控制面炸了" in payload["error"]


async def test_unexpected_handler_exception_is_controlled_500() -> None:
    """handler 顶层未预期异常 → 受控 500 JSON（不断连）。"""

    class BrokenClock:
        """仅让健康 / 调用路径之外的时钟抛错。"""

        def __call__(self) -> int:
            raise RuntimeError("时钟坏了")

    runtime = _runtime(FakeControl(), FakeScreenshots(), FakeClipboard())
    runtime.now_ms = BrokenClock()
    async with _serve(runtime) as port:
        body = b'{"tool":"screenshot","arguments":{}}'
        status, payload, _ = await _request(
            port, method="POST", path="/invoke", body=body, token="secret"
        )
        assert status == 500
        assert payload == {"detail": "agent 内部错误"}


async def test_invoke_tools_are_serialized_while_health_stays_open() -> None:
    """串行化：一个 /invoke 持锁执行时后续工具等待；/health 不占锁。"""
    gate = asyncio.Event()
    control = FakeControl()
    screenshots = FakeScreenshots([b"png"], gate=gate)
    runtime = _runtime(control, screenshots, FakeClipboard())
    first = asyncio.create_task(
        agent.dispatch_request(_parsed_invoke("screenshot", {}), runtime, "secret")
    )
    await asyncio.wait_for(screenshots.started.wait(), 1.0)
    health = await agent.dispatch_request(
        ParsedHttpRequest(method="GET", target="/health", headers={}, body=b""),
        runtime,
        "secret",
    )
    second = asyncio.create_task(
        agent.dispatch_request(
            _parsed_invoke("click", {"x": 1, "y": 2}), runtime, "secret"
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert health.status == 200
    assert control.calls == []
    gate.set()
    first_response = await asyncio.wait_for(first, 2.0)
    second_response = await asyncio.wait_for(second, 2.0)
    assert first_response.status == 200 and first_response.payload["ok"] is True
    assert second_response.status == 200 and second_response.payload["ok"] is True
    assert [type(call) for call in control.calls] == [MoveRequest, ClickRequest]


async def test_screenshot_tool_returns_base64_and_digest() -> None:
    """screenshot 工具：整屏抓取 + base64 + sha256 + 尺寸。"""
    runtime = _runtime(FakeControl(), FakeScreenshots([b"png-bytes"]), FakeClipboard())
    result = await runtime.execute("screenshot", {})
    assert isinstance(result, ScreenshotResultModel)
    assert result.width == 1280 and result.height == 800
    assert result.image_base64 == base64.b64encode(b"png-bytes").decode("ascii")
    assert result.sha256 == hashlib.sha256(b"png-bytes").hexdigest()


async def test_click_tool_maps_to_move_and_click() -> None:
    """click 工具：先绝对移动后左键点击；控制面失败即工具失败。"""
    control = FakeControl()
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    result = await runtime.execute("click", {"x": 10, "y": 20})
    assert result == ClickResultModel(x=10, y=20)
    assert [type(call) for call in control.calls] == [MoveRequest, ClickRequest]
    assert control.calls[0] == MoveRequest(x=10, y=20)

    failing = FakeControl()
    failing.fail_types.add(MoveRequest)
    failing_runtime = _runtime(failing, FakeScreenshots(), FakeClipboard())
    with pytest.raises(AgentToolError, match="指针移动失败"):
        await failing_runtime.execute("click", {"x": 1, "y": 2})


async def test_type_text_uses_clipboard_and_ctrl_v() -> None:
    """type_text：剪贴板写入 + Ctrl+V 序列（中文可输入；粘贴后不清空）。"""
    control = FakeControl()
    clipboard = FakeClipboard()
    clock = FakeClock()
    runtime = _runtime(control, FakeScreenshots(), clipboard, clock)
    result = await runtime.execute("type_text", {"text": "晚安，明天见"})
    assert result == TypeTextResultModel(chars=6)
    assert clipboard.writes == ["晚安，明天见"]
    assert control.calls == [
        KeyRequest(code=KEY_LEFTCTRL, pressed=True),
        KeyRequest(code=47, pressed=True),
        KeyRequest(code=47, pressed=False),
        KeyRequest(code=KEY_LEFTCTRL, pressed=False),
    ]
    assert clock.delays == [0.1]


async def test_press_key_sequence_and_failure_isolation() -> None:
    """press_key：组合键按下 / 抬起顺序；控制面失败即工具失败。"""
    control = FakeControl()
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    result = await runtime.execute("press_key", {"key": "ctrl+shift+a"})
    assert result == PressKeyResultModel(key="ctrl+shift+a")
    assert [call.pressed for call in control.calls] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]

    failing = FakeControl()
    failing.fail_types.add(KeyRequest)
    failing_runtime = _runtime(failing, FakeScreenshots(), FakeClipboard())
    with pytest.raises(AgentToolError, match="按键按下失败"):
        await failing_runtime.execute("press_key", {"key": "enter"})


async def test_open_chat_waits_for_title_change_and_returns_two_shots() -> None:
    """open_chat：Ctrl+F → 粘贴 → Enter → 有界等待标题变化 → 双截图。"""
    control = FakeControl()
    clipboard = FakeClipboard()
    clock = FakeClock()
    screenshots = FakeScreenshots([b"title-old", b"title-old", b"title-new", b"full"])
    runtime = _runtime(control, screenshots, clipboard, clock)
    result = await runtime.execute("open_chat", {"remark_name": "小林"})
    assert clipboard.writes == ["小林"]
    assert result.remark_name == "小林"
    assert result.title_changed is True
    assert result.title_sha256 != result.full_sha256
    assert screenshots.crops == [
        (1280, 140),
        (1280, 140),
        (1280, 140),
        None,
    ]
    assert len(control.calls) == 10
    assert clock.now > 0


async def test_open_chat_bounded_wait_reports_title_unchanged() -> None:
    """open_chat：标题始终不变时按截止收口，title_changed=False（不假装成功变化）。"""
    clock = FakeClock()
    runtime = _runtime(
        FakeControl(),
        FakeScreenshots([b"same", b"same", b"same", b"same", b"same"]),
        FakeClipboard(),
        clock,
    )
    result = await runtime.execute("open_chat", {"remark_name": "群聊"})
    assert isinstance(result, OpenChatResultModel)
    assert result.title_changed is False
    assert result.title_image_base64
    assert clock.now >= 1.0
    assert clock.delays


async def test_screenshot_tool_propagates_capture_failure() -> None:
    """截图失败（子进程异常）→ AgentToolError（HTTP 层归一 ok=false）。"""
    screenshots = FakeScreenshots()
    screenshots.error = AgentToolError("截图命令失败")
    runtime = _runtime(FakeControl(), screenshots, FakeClipboard())
    with pytest.raises(AgentToolError, match="截图命令失败"):
        await runtime.execute("screenshot", {})


async def test_imagemagick_screenshot_command_construction() -> None:
    """ImageMagick 实现：整屏 / 裁剪参数与成功 / 失败映射。"""
    recorded: list[dict[str, Any]] = []

    async def fake_runner(
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        recorded.append({"argv": list(argv), "env": env, "timeout": timeout})
        return ProcessOutput(returncode=0, stdout=b"png", stderr=b"")

    capturer = ImagemagickScreenshot(":99", timeout=1.0, runner=fake_runner)
    assert await capturer.capture() == b"png"
    assert await capturer.capture(crop=(1280, 140)) == b"png"
    assert recorded[0]["argv"][-1] == "png:-" and "-crop" not in recorded[0]["argv"]
    assert recorded[1]["argv"][recorded[1]["argv"].index("-crop") + 1] == "1280x140+0+0"
    assert recorded[0]["env"]["DISPLAY"] == ":99"

    async def failing_runner(
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        return ProcessOutput(returncode=1, stdout=b"", stderr="炸了".encode())

    with pytest.raises(AgentToolError, match="非零退出"):
        await ImagemagickScreenshot(":99", runner=failing_runner).capture()

    async def empty_runner(
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        return ProcessOutput(returncode=0, stdout=b"", stderr=b"")

    with pytest.raises(AgentToolError, match="输出为空"):
        await ImagemagickScreenshot(":99", runner=empty_runner).capture()


async def test_xclip_clipboard_command() -> None:
    """xclip 实现：argv / stdin 字节 / 非零退出映射。"""
    calls: list[dict[str, Any]] = []

    async def fake_runner(
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        calls.append(
            {
                "argv": list(argv),
                "input_bytes": input_bytes,
                "capture_output": capture_output,
                "env": env,
            }
        )
        return ProcessOutput(returncode=0, stdout=b"", stderr=b"")

    await XclipClipboard(":99", timeout=1.0, runner=fake_runner).write("你好")
    assert calls[0]["argv"] == ["xclip", "-selection", "clipboard", "-in"]
    assert calls[0]["input_bytes"] == "你好".encode()
    assert calls[0]["capture_output"] is False
    assert calls[0]["env"]["DISPLAY"] == ":99"

    async def failing_runner(
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        capture_output: bool = True,
    ) -> ProcessOutput:
        return ProcessOutput(returncode=1, stdout=b"", stderr=b"")

    with pytest.raises(AgentToolError, match="非零退出"):
        await XclipClipboard(":99", runner=failing_runner).write("x")


async def test_run_process_success_failure_and_timeout() -> None:
    """异步子进程执行面：成功 / 非零退出 / 启动失败 / 超时强杀。"""
    output = await run_process(
        [sys.executable, "-c", "print('ok')"], timeout=5.0
    )
    assert output.returncode == 0 and output.stdout.strip() == b"ok"

    output = await run_process([sys.executable, "-c", "raise SystemExit(3)"], timeout=5.0)
    assert output.returncode == 3

    with pytest.raises(AgentToolError, match="无法启动命令"):
        await run_process(["/nonexistent/agent-test-bin"], timeout=1.0)

    with pytest.raises(AgentToolError, match="命令超时"):
        await run_process(
            [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01
        )


async def test_dead_code_removed() -> None:
    """死代码已删：同步控制面客户端与 WECHAT_GUI_AGENT_TOOLS 不再存在。"""
    assert not hasattr(agent, "SyncUinputControlClient")
    assert not hasattr(wire_models, "WECHAT_GUI_AGENT_TOOLS")
    assert not hasattr(wire_models, "SCREENSHOT_TOOL")


async def test_main_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI：token 缺失即退出码 1（不启动服务）。

    同步入口内部调用 asyncio.run，须在独立线程执行，避免与测试事件循环冲突。
    """
    monkeypatch.setattr(agent, "resolve_agent_token", lambda **_: "")
    assert await asyncio.to_thread(agent.main, ["--port", "0"]) == 1


async def test_main_delegates_to_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI：入口经 asyncio.run 执行 _run 并回传退出码。"""

    async def fake_run(args: Any, *, stop_event: Any = None) -> int:
        return 7

    monkeypatch.setattr(agent, "_run", fake_run)
    assert await asyncio.to_thread(agent.main, ["--port", "0"]) == 7


async def test_run_serves_until_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run：停止事件置位后正常收口（真实临时端口，立即返回）。"""
    monkeypatch.setattr(agent, "resolve_agent_token", lambda **_: "token")
    args = agent.build_parser().parse_args(["--port", "0"])
    stop_event = asyncio.Event()
    stop_event.set()
    assert await asyncio.wait_for(agent._run(args, stop_event=stop_event), 5.0) == 0
