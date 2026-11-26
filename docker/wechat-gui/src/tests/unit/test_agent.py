"""agent HTTP 工具面与同步控制面客户端的 hermetic 单元测试。

真实 socket 仅落在 pytest 临时目录或 127.0.0.1 的临时端口（无外网、无真实
X / uinput）；截图 / 剪贴板 / 键鼠注入全部经注入替身记录，断言按记录值与
脚本化返回值进行，不做墙钟等待。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from wechat_gui_poc import agent
from wechat_gui_poc.agent import (
    AgentRuntime,
    AgentToolError,
    ImagemagickScreenshot,
    XclipClipboard,
    build_server,
    parse_key_expression,
    resolve_agent_token,
)
from wechat_gui_poc.uinput_devices import (
    KEY_ENTER,
    KEY_LEFTCTRL,
    KEY_LEFTSHIFT,
    KEY_SPACE,
)
from wechat_gui_poc.uinputd import UinputError
from wechat_gui_poc.wire_models import (
    ClickRequest,
    ClickToolArgs,
    CommandOutcome,
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
    KeyRequest,
    MoveRequest,
    OpenChatToolArgs,
    PressKeyToolArgs,
    ScreenshotToolArgs,
    TypeTextToolArgs,
)


class FakeControl:
    """记录式控制面（可注入失败）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_types: set[type[object]] = set()

    def send(self, request: Any) -> CommandOutcome:
        """记录请求并按注入类型返回失败。"""
        self.calls.append(request)
        if type(request) in self.fail_types:
            return CommandOutcome(command=request.command.value, ok=False, error="注入失败")
        return CommandOutcome(command=request.command.value, ok=True)


class FakeScreenshots:
    """脚本化截图替身（按调用顺序返回 PNG；可注入异常）。"""

    def __init__(self, payloads: list[bytes] | None = None) -> None:
        self.payloads = list(payloads or [b"png-default"])
        self.crops: list[tuple[int, int] | None] = []
        self.error: Exception | None = None

    def capture(self, *, crop: tuple[int, int] | None = None) -> bytes:
        """记录裁剪参数并返回脚本化字节。"""
        self.crops.append(crop)
        if self.error is not None:
            raise self.error
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]


class FakeClipboard:
    """记录式剪贴板替身。"""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> None:
        """记录写入文本。"""
        self.writes.append(text)


class FakeClock:
    """可控单调时钟与记录式休眠（有界等待按虚拟时间推演）。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        """返回虚拟单调时间。"""
        return self.now

    def sleep(self, seconds: float) -> None:
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


def _http_request(
    server: Any,
    *,
    method: str,
    path: str,
    body: bytes | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """对本地 agent 发起一次 HTTP 请求并解析 JSON 响应。"""
    port = server.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method=method
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


async def _start_server(
    runtime: AgentRuntime, token: str
) -> tuple[Any, threading.Thread]:
    """在临时端口启动 agent 服务（后台线程）。"""
    server = build_server(runtime, token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def _stop_server(server: Any, thread: threading.Thread) -> None:
    """停止 agent 服务并回收线程。"""
    await asyncio.to_thread(server.shutdown)
    await asyncio.to_thread(server.server_close)
    await asyncio.to_thread(thread.join, 2.0)


async def test_wire_models_serialization_round_trip() -> None:
    """wire 模型序列化形态（健康 / 请求 / 响应）。"""
    assert HealthResponseModel().to_wire() == {"status": "ok"}
    request = InvokeRequestModel(tool="screenshot", arguments={"a": 1})
    assert request.to_wire() == {"tool": "screenshot", "arguments": {"a": 1}}
    success = InvokeResponseModel(ok=True, result={"x": 1}, latency_ms=3)
    assert success.to_wire() == {"ok": True, "result": {"x": 1}, "latency_ms": 3}
    failure = InvokeResponseModel(ok=False, error="坏", latency_ms=4)
    assert failure.to_wire() == {
        "ok": False,
        "result": {},
        "error": "坏",
        "latency_ms": 4,
    }


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


async def test_health_endpoint_is_open() -> None:
    """GET /health 无鉴权返回存活状态；未知路径 404。"""
    server, thread = await _start_server(
        _runtime(FakeControl(), FakeScreenshots(), FakeClipboard()), "secret"
    )
    try:
        status, payload = await asyncio.to_thread(
            _http_request, server, method="GET", path="/health"
        )
        assert status == 200 and payload == {"status": "ok"}
        status, _ = await asyncio.to_thread(
            _http_request, server, method="GET", path="/nope"
        )
        assert status == 404
    finally:
        await _stop_server(server, thread)


async def test_bearer_auth_matrix() -> None:
    """鉴权矩阵：缺失 / 错误 / 正确。"""
    server, thread = await _start_server(
        _runtime(FakeControl(), FakeScreenshots(), FakeClipboard()), "secret"
    )
    body = json.dumps({"tool": "screenshot", "arguments": {}}).encode("utf-8")
    try:
        missing, _ = await asyncio.to_thread(
            _http_request, server, method="POST", path="/invoke", body=body
        )
        wrong, _ = await asyncio.to_thread(
            _http_request,
            server,
            method="POST",
            path="/invoke",
            body=body,
            token="nope",
        )
        ok, payload = await asyncio.to_thread(
            _http_request,
            server,
            method="POST",
            path="/invoke",
            body=body,
            token="secret",
        )
        assert (missing, wrong, ok) == (401, 401, 200)
        assert payload["ok"] is True
    finally:
        await _stop_server(server, thread)


async def test_request_shape_failures_are_422() -> None:
    """请求形态非法（非对象 / 缺 tool / arguments 非对象 / JSON 损坏）→ 422。"""
    server, thread = await _start_server(
        _runtime(FakeControl(), FakeScreenshots(), FakeClipboard()), "secret"
    )
    cases = [
        b"[1, 2]",
        b'{"arguments": {}}',
        b'{"tool": "screenshot", "arguments": []}',
        b"not-json",
        b'{"tool": "screenshot", "extra": 1}',
    ]
    try:
        for body in cases:
            status, _ = await asyncio.to_thread(
                _http_request,
                server,
                method="POST",
                path="/invoke",
                body=body,
                token="secret",
            )
            assert status == 422, body
    finally:
        await _stop_server(server, thread)


async def test_http_tool_failure_is_200_with_ok_false() -> None:
    """工具级失败恒 200 + ok=false（未知工具 / 参数非法 / 执行失败）。"""
    control = FakeControl()
    control.fail_types.add(MoveRequest)
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    server, thread = await _start_server(runtime, "secret")
    try:
        for tool, arguments in (
            ("nope", {}),
            ("click", {"x": "bad", "y": 1}),
            ("click", {"x": 1, "y": 1}),
        ):
            body = json.dumps({"tool": tool, "arguments": arguments}).encode("utf-8")
            status, payload = await asyncio.to_thread(
                _http_request,
                server,
                method="POST",
                path="/invoke",
                body=body,
                token="secret",
            )
            assert status == 200
            assert payload["ok"] is False
            assert payload["result"] == {}
            assert payload["error"]
    finally:
        await _stop_server(server, thread)


async def test_screenshot_tool_returns_base64_and_digest() -> None:
    """screenshot 工具：整屏抓取 + base64 + sha256 + 尺寸。"""
    runtime = _runtime(FakeControl(), FakeScreenshots([b"png-bytes"]), FakeClipboard())
    result = runtime.execute("screenshot", {})
    assert result["width"] == 1280 and result["height"] == 800
    import base64
    import hashlib

    assert result["image_base64"] == base64.b64encode(b"png-bytes").decode("ascii")
    assert result["sha256"] == hashlib.sha256(b"png-bytes").hexdigest()


async def test_click_tool_maps_to_move_and_click() -> None:
    """click 工具：先绝对移动后左键点击；控制面失败即工具失败。"""
    control = FakeControl()
    runtime = _runtime(control, FakeScreenshots(), FakeClipboard())
    assert runtime.execute("click", {"x": 10, "y": 20}) == {"x": 10, "y": 20}
    assert [type(call) for call in control.calls] == [MoveRequest, ClickRequest]
    assert control.calls[0] == MoveRequest(x=10, y=20)

    failing = FakeControl()
    failing.fail_types.add(MoveRequest)
    failing_runtime = _runtime(failing, FakeScreenshots(), FakeClipboard())
    with pytest.raises(AgentToolError, match="指针移动失败"):
        failing_runtime.execute("click", {"x": 1, "y": 2})


async def test_type_text_uses_clipboard_and_ctrl_v() -> None:
    """type_text：剪贴板写入 + Ctrl+V 序列（中文可输入）。"""
    control = FakeControl()
    clipboard = FakeClipboard()
    clock = FakeClock()
    runtime = _runtime(control, FakeScreenshots(), clipboard, clock)
    assert runtime.execute("type_text", {"text": "晚安，明天见"}) == {"chars": 6}
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
    assert runtime.execute("press_key", {"key": "ctrl+shift+a"}) == {"key": "ctrl+shift+a"}
    assert [call.pressed for call in control.calls] == [True, True, True, False, False, False]

    failing = FakeControl()
    failing.fail_types.add(KeyRequest)
    failing_runtime = _runtime(failing, FakeScreenshots(), FakeClipboard())
    with pytest.raises(AgentToolError, match="按键按下失败"):
        failing_runtime.execute("press_key", {"key": "enter"})


async def test_open_chat_waits_for_title_change_and_returns_two_shots() -> None:
    """open_chat：Ctrl+F → 粘贴 → Enter → 有界等待标题变化 → 双截图。"""
    control = FakeControl()
    clipboard = FakeClipboard()
    clock = FakeClock()
    screenshots = FakeScreenshots([b"title-old", b"title-old", b"title-new", b"full"])
    runtime = _runtime(control, screenshots, clipboard, clock)
    result = runtime.execute("open_chat", {"remark_name": "小林"})
    assert clipboard.writes == ["小林"]
    assert result["remark_name"] == "小林"
    assert result["title_sha256"] != result["full_sha256"]
    assert screenshots.crops == [
        (1280, 140),
        (1280, 140),
        (1280, 140),
        None,
    ]
    assert [type(call) for call in control.calls] == [
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
        KeyRequest,
    ]
    assert clock.now > 0


async def test_open_chat_bounded_wait_gives_up_at_deadline() -> None:
    """open_chat：标题始终不变时按截止收口（不无限等待）。"""
    clock = FakeClock()
    runtime = _runtime(
        FakeControl(),
        FakeScreenshots([b"same", b"same", b"same", b"same", b"same"]),
        FakeClipboard(),
        clock,
    )
    result = runtime.execute("open_chat", {"remark_name": "群聊"})
    assert result["title_image_base64"]
    assert clock.now >= 1.0
    assert clock.delays


async def test_screenshot_tool_propagates_capture_failure() -> None:
    """截图失败（子进程异常）→ AgentToolError（HTTP 层归一 ok=false）。"""
    screenshots = FakeScreenshots()
    screenshots.error = AgentToolError("截图命令失败")
    runtime = _runtime(FakeControl(), screenshots, FakeClipboard())
    with pytest.raises(AgentToolError, match="截图命令失败"):
        runtime.execute("screenshot", {})


async def test_imagemagick_screenshot_command_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImageMagick 实现：整屏 / 裁剪参数与成功 / 失败映射。"""
    recorded: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"png", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    capturer = ImagemagickScreenshot(":99", timeout=1.0)
    assert capturer.capture() == b"png"
    assert capturer.capture(crop=(1280, 140)) == b"png"
    assert recorded[0][-1] == "png:-" and "-crop" not in recorded[0]
    assert recorded[1][recorded[1].index("-crop") + 1] == "1280x140+0+0"

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr="炸了".encode())

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(AgentToolError, match="非零退出"):
        capturer.capture()

    def empty_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", empty_run)
    with pytest.raises(AgentToolError, match="输出为空"):
        capturer.capture()


async def test_xclip_clipboard_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """xclip 实现：成功静默 / 非零退出映射 / 启动失败映射。"""
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", fake_run)
    XclipClipboard(":99", timeout=1.0).write("你好")
    assert calls[0]["argv"] == ["xclip", "-selection", "clipboard", "-in"]
    assert calls[0]["input"] == "你好".encode()

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(AgentToolError, match="非零退出"):
        XclipClipboard(":99").write("x")

    def broken(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("no xclip")

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(AgentToolError, match="剪贴板写入失败"):
        XclipClipboard(":99").write("x")


async def test_sync_control_client_round_trip(tmp_path: Path) -> None:
    """同步控制面客户端：Unix socket 往返 / 缺 socket / 非法响应。"""
    socket_path = tmp_path / "control.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        outcome = CommandOutcome(command=str(request.get("command", "")), ok=True)
        writer.write((json.dumps(outcome.to_wire(), ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = agent.SyncUinputControlClient(socket_path, timeout=2.0)
        outcome = await asyncio.to_thread(client.ping)
        assert outcome.ok is True and outcome.command == "ping"
    finally:
        server.close()
        await server.wait_closed()

    missing = agent.SyncUinputControlClient(tmp_path / "missing.sock", timeout=0.2)
    with pytest.raises(UinputError, match="无法连接控制面"):
        await asyncio.to_thread(missing.ping)

    bad_socket = tmp_path / "bad.sock"

    async def bad_handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"<html>\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    bad_server = await asyncio.start_unix_server(bad_handle, path=str(bad_socket))
    try:
        bad_client = agent.SyncUinputControlClient(bad_socket, timeout=1.0)
        with pytest.raises(UinputError, match="不是合法 JSON"):
            await asyncio.to_thread(bad_client.ping)
    finally:
        bad_server.close()
        await bad_server.wait_closed()


async def test_main_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI：token 缺失即退出码 1（不启动服务）。"""
    monkeypatch.setattr(agent, "resolve_agent_token", lambda **_: "")
    assert agent.main(["--port", "0"]) == 1


async def test_main_serves_until_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI：token 就绪后启动服务，中断后收口返回 0。"""
    monkeypatch.setattr(agent, "resolve_agent_token", lambda **_: "token")
    closed: list[bool] = []

    class FakeServer:
        """最小服务替身（serve_forever 抛中断，server_close 记录）。"""

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            closed.append(True)

        @property
        def server_address(self) -> tuple[str, int]:
            return ("127.0.0.1", 8090)

    monkeypatch.setattr(agent, "build_server", lambda *a, **k: FakeServer())
    assert agent.main(["--port", "0"]) == 0
    assert closed == [True]
