"""uinput 控制面守护进程与客户端的 hermetic 单元测试。

真实 Unix socket 仅落在 pytest 临时目录（无网络、无真实设备）；设备注入
由手写 fake 实现 ``InputEmitter`` 协议。
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import time
from pathlib import Path

import pytest

from wechat_gui_poc import uinputd
from wechat_gui_poc.uinput_devices import (
    BTN_LEFT,
    BTN_RIGHT,
    KEY_A,
    UinputError,
)
from wechat_gui_poc.wire_models import (
    DEFAULT_DEVICES_JSON,
    DEFAULT_SOCKET_PATH,
    MAX_CONTROL_REQUEST_BYTES,
    ClickRequest,
    CommandOutcome,
    ControlCommand,
    DevicePairInfo,
    KeyRequest,
    MoveRequest,
    PingDetails,
    PingRequest,
    ShutdownRequest,
    VirtualDeviceInfo,
)


def _pair(tmp_path: Path) -> DevicePairInfo:
    """构造指向临时事件节点的设备信息。

    Args:
        tmp_path: 临时目录。

    Returns:
        设备信息。
    """
    keyboard = tmp_path / "event5"
    pointer = tmp_path / "event9"
    keyboard.write_text("", encoding="utf-8")
    pointer.write_text("", encoding="utf-8")
    return DevicePairInfo(
        keyboard=VirtualDeviceInfo(
            name="wechat-gui virtual keyboard",
            event_path=str(keyboard),
        ),
        pointer=VirtualDeviceInfo(
            name="wechat-gui virtual pointer",
            event_path=str(pointer),
        ),
        width=1280,
        height=800,
    )


class FakeEmitter:
    """记录式虚拟设备（满足 ``InputEmitter`` 协议）。"""

    def __init__(self, pair_info: DevicePairInfo) -> None:
        self._pair = pair_info
        self.calls: list[tuple[str, object]] = []
        self.destroyed = False
        self.fail = False

    @property
    def pair_info(self) -> DevicePairInfo:
        """设备信息。"""
        return self._pair

    def emit_key(self, code: int, *, pressed: bool) -> None:
        """记录按键注入。"""
        if self.fail:
            raise UinputError("注入失败")
        self.calls.append(("key", (code, pressed)))

    def emit_move(self, x: int, y: int) -> None:
        """记录移动。"""
        self.calls.append(("move", (x, y)))

    def emit_click(self, button: int = BTN_LEFT) -> None:
        """记录点击。"""
        self.calls.append(("click", button))

    def destroy(self) -> None:
        """记录销毁。"""
        self.destroyed = True


async def test_artifacts_round_trip(tmp_path: Path) -> None:
    """设备信息编码 / 落盘 / 读取保持一致。"""
    pair = _pair(tmp_path)
    artifacts = uinputd.artifacts_from_pair(pair)
    wire = artifacts.to_wire()
    assert wire["width"] == 1280
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(wire), encoding="utf-8")
    loaded = uinputd.load_artifacts(path)
    assert loaded == artifacts


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[]",
        json.dumps({"width": 1280, "height": 800, "keyboard": {}}),
        json.dumps(
            {
                "width": 1280,
                "height": 800,
                "keyboard": {"name": "", "event_path": "/dev/input/event1"},
                "pointer": {"name": "p", "event_path": "/dev/input/event2"},
            }
        ),
        json.dumps(
            {
                "width": True,
                "height": 800,
                "keyboard": {"name": "k", "event_path": "/dev/input/event1"},
                "pointer": {"name": "p", "event_path": "/dev/input/event2"},
            }
        ),
        json.dumps(
            {
                "width": 0,
                "height": 800,
                "keyboard": {"name": "k", "event_path": "/dev/input/event1"},
                "pointer": {"name": "p", "event_path": "/dev/input/event2"},
            }
        ),
    ],
)
async def test_load_artifacts_rejects_invalid(tmp_path: Path, payload: str) -> None:
    """非法设备信息给出可读错误。"""
    path = tmp_path / "devices.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        uinputd.load_artifacts(path)


async def test_load_artifacts_missing_file(tmp_path: Path) -> None:
    """文件缺失归一为 ValueError。"""
    with pytest.raises(ValueError, match="无法读取设备信息"):
        uinputd.load_artifacts(tmp_path / "missing.json")


async def test_decode_control_request_matrix() -> None:
    """五条命令各自解码为具体请求模型，字段逐项落位。"""
    ping = uinputd.decode_control_request(b'{"command":"ping"}\n')
    assert isinstance(ping, PingRequest)
    assert ping.command is ControlCommand.PING

    key = uinputd.decode_control_request(
        json.dumps({"command": "key", "code": KEY_A, "pressed": True}).encode()
    )
    assert isinstance(key, KeyRequest)
    assert (key.code, key.pressed) == (KEY_A, True)

    move = uinputd.decode_control_request(b'{"command":"move","x":3,"y":4}')
    assert isinstance(move, MoveRequest)
    assert (move.x, move.y) == (3, 4)

    click = uinputd.decode_control_request(b'{"command":"click"}')
    assert isinstance(click, ClickRequest)
    assert click.button == BTN_LEFT

    right = uinputd.decode_control_request(
        json.dumps({"command": "click", "button": BTN_RIGHT}).encode()
    )
    assert isinstance(right, ClickRequest)
    assert right.button == BTN_RIGHT

    shutdown = uinputd.decode_control_request(b'{"command":"shutdown"}')
    assert isinstance(shutdown, ShutdownRequest)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{" + b" " * MAX_CONTROL_REQUEST_BYTES + b"}", "大小上限"),
        (b"not-json\n", "不是合法 JSON"),
        (b"[]\n", "根节点必须是对象"),
        (b"{}\n", "缺少 command 字段"),
        (b'{"command":""}\n', "缺少 command 字段"),
        (b'{"command":"jump"}\n', "未知命令"),
        (b'{"command":"key","code":true,"pressed":true}\n', "code 必须是整数"),
        (b'{"command":"key","code":30}\n', "pressed 必须是布尔值"),
        (b'{"command":"move","x":1}\n', "y 必须是整数"),
        (b'{"command":"click","button":"left"}\n', "button 必须是整数"),
    ],
)
async def test_decode_control_request_rejects_invalid(raw: bytes, message: str) -> None:
    """超长 / 非法 JSON / 字段缺失或类型不符 / 未知命令均拒绝。"""
    with pytest.raises(uinputd.CommandError, match=message):
        uinputd.decode_control_request(raw)


async def test_dispatch_commands_write_fake_device(tmp_path: Path) -> None:
    """ping / key / move / click / shutdown 写入 fake 设备。"""
    emitter = FakeEmitter(_pair(tmp_path))
    ping = uinputd.dispatch_command(emitter, PingRequest())
    assert ping.ok is True
    assert ping.details == PingDetails(
        keyboard="wechat-gui virtual keyboard",
        pointer="wechat-gui virtual pointer",
        width=1280,
        height=800,
    )
    assert uinputd.dispatch_command(emitter, KeyRequest(code=KEY_A, pressed=True)).ok
    assert uinputd.dispatch_command(emitter, MoveRequest(x=10, y=20)).ok
    assert uinputd.dispatch_command(emitter, ClickRequest(button=BTN_LEFT)).ok
    assert uinputd.dispatch_command(emitter, ClickRequest(button=BTN_RIGHT)).ok
    assert uinputd.dispatch_command(emitter, ShutdownRequest()).ok
    assert emitter.calls == [
        ("key", (KEY_A, True)),
        ("move", (10, 20)),
        ("click", BTN_LEFT),
        ("click", BTN_RIGHT),
    ]


async def test_dispatch_normalizes_emitter_failure(tmp_path: Path) -> None:
    """设备注入异常归一为 ok=False，不中断守护。"""
    emitter = FakeEmitter(_pair(tmp_path))
    emitter.fail = True
    outcome = uinputd.dispatch_command(emitter, KeyRequest(code=KEY_A, pressed=True))
    assert outcome.ok is False
    assert outcome.error == "注入失败"


async def test_command_outcome_wire_carries_ping_details() -> None:
    """响应序列化出口携带探测附加字段，失败响应携带 error。"""
    ok = CommandOutcome(
        command="ping",
        ok=True,
        details=PingDetails(keyboard="k", pointer="p", width=1280, height=800),
    ).to_wire()
    assert ok == {
        "ok": True,
        "command": "ping",
        "keyboard": "k",
        "pointer": "p",
        "width": 1280,
        "height": 800,
    }
    failed = CommandOutcome(command="key", ok=False, error="注入失败").to_wire()
    assert failed == {"ok": False, "command": "key", "error": "注入失败"}


async def test_write_runtime_artifacts_and_chown(tmp_path: Path) -> None:
    """设备信息 / Xorg 配置落盘并把节点交给当前用户。"""
    pair = _pair(tmp_path)
    emitter = FakeEmitter(pair)
    artifacts_path = tmp_path / "run" / "devices.json"
    xorg_path = tmp_path / "etc" / "99-wechat-gui.conf"
    username = pwd.getpwuid(os.getuid()).pw_name
    artifacts = uinputd.write_runtime_artifacts(
        emitter,
        devices_json_path=artifacts_path,
        xorg_conf_path=xorg_path,
        username=username,
    )
    assert artifacts.keyboard_name == "wechat-gui virtual keyboard"
    assert artifacts_path.exists()
    assert xorg_path.read_text(encoding="utf-8").find(pair.keyboard.event_path) >= 0
    stat_result = os.stat(pair.keyboard.event_path)
    assert stat_result.st_uid == os.getuid()
    assert stat_result.st_gid == os.getgid()
    assert stat_result.st_mode & 0o777 == 0o660


async def test_chown_unknown_user_raises(tmp_path: Path) -> None:
    """目标用户不存在时给出可读错误。"""
    with pytest.raises(UinputError, match="运行用户不存在"):
        uinputd.chown_event_nodes(_pair(tmp_path), "definitely-not-a-user")


async def test_build_parser_defaults() -> None:
    """命令行缺省值指向容器内运行路径。"""
    args = uinputd.build_parser().parse_args([])
    assert args.socket == DEFAULT_SOCKET_PATH
    assert args.devices_json == DEFAULT_DEVICES_JSON
    assert (args.width, args.height) == (1280, 800)


async def _wait_for_socket(path: Path, *, timeout: float = 2.0) -> None:
    """等待 socket 文件出现（带截止）。

    Args:
        path: socket 路径。
        timeout: 截止秒数。
    """
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, "socket 未在截止内出现"
        await asyncio.sleep(0.01)


async def _serve(
    tmp_path: Path,
) -> tuple[FakeEmitter, asyncio.Event, asyncio.Task[None], Path]:
    """在临时目录启动控制面服务。

    Args:
        tmp_path: 临时目录。

    Returns:
        (设备替身, 停止事件, 服务任务, socket 路径)。
    """
    emitter = FakeEmitter(_pair(tmp_path))
    socket_path = tmp_path / "run" / "control.sock"
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        uinputd.serve(
            emitter,
            socket_path=socket_path,
            devices_json_path=tmp_path / "run" / "devices.json",
            xorg_conf_path=tmp_path / "etc" / "xorg.conf",
            username=None,
            stop_event=stop_event,
        )
    )
    await _wait_for_socket(socket_path)
    return emitter, stop_event, task, socket_path


async def test_serve_and_client_full_roundtrip(tmp_path: Path) -> None:
    """守护进程服务控制命令、拒绝非法请求、shutdown 收口。"""
    emitter, _stop_event, task, socket_path = await _serve(tmp_path)
    client = uinputd.UinputControlClient(socket_path, timeout=2.0)
    ping = await client.ping()
    assert ping.ok is True
    assert ping.details == PingDetails(
        keyboard="wechat-gui virtual keyboard",
        pointer="wechat-gui virtual pointer",
        width=1280,
        height=800,
    )
    assert (await client.emit_key(KEY_A, pressed=True)).ok is True
    assert (await client.emit_move(5, 6)).ok is True
    assert (await client.emit_click()).ok is True

    invalid_reader, invalid_writer = await asyncio.open_unix_connection(str(socket_path))
    invalid_writer.write(b"not-json\n")
    await invalid_writer.drain()
    response = json.loads((await invalid_reader.readline()).decode("utf-8"))
    invalid_writer.close()
    await invalid_writer.wait_closed()
    assert response["ok"] is False
    assert "不是合法 JSON" in response["error"]

    assert (await client.shutdown()).ok is True
    await asyncio.wait_for(task, timeout=2.0)
    assert emitter.destroyed is True
    assert not socket_path.exists()
    assert (tmp_path / "run" / "devices.json").exists()


async def test_client_missing_socket_raises(tmp_path: Path) -> None:
    """控制面不存在时客户端给出可读错误。"""
    client = uinputd.UinputControlClient(tmp_path / "missing.sock", timeout=0.2)
    with pytest.raises(UinputError, match="无法连接控制面"):
        await client.ping()


async def test_client_rejects_malformed_response(tmp_path: Path) -> None:
    """控制面响应非 JSON 时客户端拒绝。"""
    socket_path = tmp_path / "bad.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"<html>\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = uinputd.UinputControlClient(socket_path, timeout=1.0)
        with pytest.raises(UinputError, match="响应不是合法 JSON"):
            await client.ping()
    finally:
        server.close()
        await server.wait_closed()


async def test_client_rejects_non_dict_response(tmp_path: Path) -> None:
    """控制面响应根节点非对象时客户端拒绝。"""
    socket_path = tmp_path / "list.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"[]\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = uinputd.UinputControlClient(socket_path, timeout=1.0)
        with pytest.raises(UinputError, match="根节点必须是对象"):
            await client.ping()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"ok": True, "command": "ping", "keyboard": "k"}, "ping 字段不完整"),
        (
            {
                "ok": True,
                "command": "ping",
                "keyboard": 1,
                "pointer": "p",
                "width": 1,
                "height": 1,
            },
            "设备名类型非法",
        ),
        (
            {
                "ok": True,
                "command": "ping",
                "keyboard": "k",
                "pointer": "p",
                "width": True,
                "height": 1,
            },
            "尺寸类型非法",
        ),
    ],
)
async def test_client_rejects_malformed_ping_details(
    tmp_path: Path, response: dict[str, object], message: str
) -> None:
    """探测字段只出现一部分或类型不符时客户端拒绝。"""
    socket_path = tmp_path / "ping.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = uinputd.UinputControlClient(socket_path, timeout=1.0)
        with pytest.raises(UinputError, match=message):
            await client.ping()
    finally:
        server.close()
        await server.wait_closed()


async def test_main_runs_with_injected_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 入口接线（create / serve 经 monkeypatch 替换）。"""
    emitter = FakeEmitter(_pair(tmp_path))
    monkeypatch.setattr(uinputd, "create", lambda **_kwargs: emitter)
    captured: dict[str, object] = {}

    async def fake_serve(devices: object, **kwargs: object) -> None:
        captured["devices"] = devices
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uinputd, "serve", fake_serve)
    code = await asyncio.to_thread(
        uinputd.main,
        [
            "--socket",
            str(tmp_path / "control.sock"),
            "--devices-json",
            str(tmp_path / "devices.json"),
            "--xorg-conf",
            str(tmp_path / "xorg.conf"),
        ],
    )
    assert code == 0
    assert captured["devices"] is emitter


async def test_main_usage_error_exits_two() -> None:
    """未知参数由 argparse 以退出码 2 收口。"""
    with pytest.raises(SystemExit) as excinfo:
        await asyncio.to_thread(uinputd.main, ["--unknown-flag"])
    assert excinfo.value.code == 2


async def test_dispatch_key_release_forwarded(tmp_path: Path) -> None:
    """按键释放（pressed=False）按原值下发，不被硬编码成按下。"""
    emitter = FakeEmitter(_pair(tmp_path))
    outcome = uinputd.dispatch_command(emitter, KeyRequest(code=KEY_A, pressed=False))
    assert outcome.ok is True
    assert emitter.calls == [("key", (KEY_A, False))]
