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
    DeviceKind,
    DevicePairInfo,
    UinputError,
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
            kind=DeviceKind.KEYBOARD,
            name="wechat-gui virtual keyboard",
            event_path=str(keyboard),
        ),
        pointer=VirtualDeviceInfo(
            kind=DeviceKind.POINTER,
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
    wire = uinputd.artifacts_to_wire(artifacts)
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


async def test_dispatch_ping_and_injection(tmp_path: Path) -> None:
    """ping / key / move / click / shutdown 命令写入 fake 设备。"""
    emitter = FakeEmitter(_pair(tmp_path))
    assert uinputd.dispatch_command(emitter, {"command": "ping"}).ok is True
    assert uinputd.dispatch_command(emitter, {"command": "key", "code": KEY_A, "pressed": True}).ok
    assert uinputd.dispatch_command(emitter, {"command": "move", "x": 10, "y": 20}).ok
    assert uinputd.dispatch_command(emitter, {"command": "click"}).ok
    assert uinputd.dispatch_command(emitter, {"command": "click", "button": BTN_RIGHT}).ok
    assert uinputd.dispatch_command(emitter, {"command": "shutdown"}).ok
    assert emitter.calls == [
        ("key", (KEY_A, True)),
        ("move", (10, 20)),
        ("click", BTN_LEFT),
        ("click", BTN_RIGHT),
    ]


async def test_dispatch_rejects_invalid_payloads(tmp_path: Path) -> None:
    """缺命令 / 类型错误 / 未知命令均返回 ok=False。"""
    emitter = FakeEmitter(_pair(tmp_path))
    missing = uinputd.dispatch_command(emitter, {})
    assert missing.ok is False
    assert missing.error is not None and "command" in missing.error

    bad_code = uinputd.dispatch_command(emitter, {"command": "key", "code": True, "pressed": True})
    assert bad_code.ok is False
    assert bad_code.error is not None and "code" in bad_code.error

    bad_pressed = uinputd.dispatch_command(emitter, {"command": "key", "code": KEY_A})
    assert bad_pressed.ok is False

    unknown = uinputd.dispatch_command(emitter, {"command": "jump"})
    assert unknown.ok is False
    assert unknown.error is not None and "未知命令" in unknown.error


async def test_dispatch_normalizes_emitter_failure(tmp_path: Path) -> None:
    """设备注入异常归一为 ok=False，不中断守护。"""
    emitter = FakeEmitter(_pair(tmp_path))
    emitter.fail = True
    outcome = uinputd.dispatch_command(emitter, {"command": "key", "code": KEY_A, "pressed": True})
    assert outcome.ok is False
    assert outcome.error == "注入失败"


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


async def test_decode_request_rejects_oversize_and_non_object() -> None:
    """控制请求大小上限与根节点形态校验。"""
    with pytest.raises(ValueError, match="大小上限"):
        uinputd._decode_request(b"{" + b" " * uinputd._MAX_REQUEST_BYTES + b"}")
    with pytest.raises(ValueError, match="根节点必须是对象"):
        uinputd._decode_request(b"[]")


async def test_build_parser_defaults() -> None:
    """命令行缺省值指向容器内运行路径。"""
    args = uinputd.build_parser().parse_args([])
    assert args.socket == uinputd.DEFAULT_SOCKET_PATH
    assert args.devices_json == uinputd.DEFAULT_DEVICES_JSON
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


async def test_serve_and_client_full_roundtrip(tmp_path: Path) -> None:
    """守护进程服务控制命令、拒绝非法请求、shutdown 收口。"""
    emitter = FakeEmitter(_pair(tmp_path))
    socket_path = tmp_path / "run" / "control.sock"
    devices_json = tmp_path / "run" / "devices.json"
    xorg_conf = tmp_path / "etc" / "xorg.conf"
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        uinputd.serve(
            emitter,
            socket_path=socket_path,
            devices_json_path=devices_json,
            xorg_conf_path=xorg_conf,
            username=None,
            stop_event=stop_event,
        )
    )
    await _wait_for_socket(socket_path)
    client = uinputd.UinputControlClient(socket_path, timeout=2.0)
    assert (await client.ping()).ok is True
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

    assert (await client.shutdown()).ok is True
    await asyncio.wait_for(task, timeout=2.0)
    assert emitter.destroyed is True
    assert not socket_path.exists()
    assert devices_json.exists()


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
