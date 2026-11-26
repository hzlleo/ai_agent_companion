"""PoC 门脚本的 hermetic 单元测试。

外部命令经临时 ``PATH`` 假命令驱动；控制面 / TCP / HTTP 用 pytest 进程内
的 asyncio 服务；图像用内存 PNG。全程无真实 X、无真实 uinput。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image

from wechat_gui_poc import poc_gate
from wechat_gui_poc.wire_models import (
    CommandOutput,
    DeviceArtifacts,
    GateContext,
    StepRecord,
    StepSpec,
    StepStatus,
)

XDPYINFO_SAMPLE = """name of display:    :99
version number:    11.0
vendor string:    The X.Org Foundation
  dimensions:    1280x800 pixels (338x211 millimeters)
  resolution:    96x96 dots per inch
depth of root window:    24 planes
"""

XINPUT_SAMPLE = """⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]
⎜   ↳ Virtual core XTEST pointer               \tid=4\t[slave  pointer  (2)]
⎜   ↳ wechat-gui virtual pointer               \tid=7\t[slave  pointer  (2)]
⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]
    ↳ Virtual core XTEST keyboard              \tid=5\t[slave  keyboard (3)]
    ↳ wechat-gui virtual keyboard              \tid=6\t[slave  keyboard (3)]
"""

XINPUT_FLOATING_SAMPLE = """⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]
⎜   ↳ wechat-gui virtual pointer               \tid=7\t[slave  pointer  (2)]
⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]
    ↳ wechat-gui virtual keyboard              \tid=6\t[floating slave]
"""

KEY_PRESS_BLOCK = """KeyPress event, serial 18, synthetic NO, window 0x547,
    root 0x547, subw 0x0, time 1, (640,400), root:(640,400),
    keycode 38 (keysym 0x61, a), same_screen YES,

"""

KEY_RELEASE_BLOCK = """KeyRelease event, serial 18, synthetic NO, window 0x547,
    keycode 38 (keysym 0x61, a), same_screen YES,

"""

MOTION_BLOCK = """MotionNotify event, serial 18, synthetic NO, window 0x547,
    root 0x547, subw 0x0, time 1, (640,400), root:(640,400),

"""

BUTTON_PRESS_BLOCK = """ButtonPress event, serial 18, synthetic NO, window 0x547,
    root 0x547, subw 0x0, time 1, (640,400), root:(640,400),
    state 0x0, button 1, same_screen YES,

"""

BUTTON_RELEASE_BLOCK = """ButtonRelease event, serial 18, synthetic NO, window 0x547,
    root 0x547, subw 0x0, time 1, (640,400), root:(640,400),
    state 0x100, button 1, same_screen YES,

"""

XEV_SAMPLE = (
    KEY_PRESS_BLOCK + KEY_RELEASE_BLOCK + MOTION_BLOCK + BUTTON_PRESS_BLOCK + BUTTON_RELEASE_BLOCK
)
XEV_WITHOUT_MOTION = (
    KEY_PRESS_BLOCK + KEY_RELEASE_BLOCK + BUTTON_PRESS_BLOCK + BUTTON_RELEASE_BLOCK
)
XEV_WITHOUT_BUTTON_RELEASE = (
    KEY_PRESS_BLOCK + KEY_RELEASE_BLOCK + MOTION_BLOCK + BUTTON_PRESS_BLOCK
)


def _write_script(path: Path, body: str) -> None:
    """写入可执行 shell 脚本。

    Args:
        path: 脚本路径。
        body: 脚本正文（含 shebang）。
    """
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _install_fake_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """安装门禁所需的外部命令替身并前置到 PATH。

    Args:
        tmp_path: 临时目录。
        monkeypatch: pytest 补丁器。

    Returns:
        替身命令目录。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_script(
        bin_dir / "xdpyinfo",
        "#!/bin/sh\ncat <<'EOF'\n" + XDPYINFO_SAMPLE + "EOF\n",
    )
    _write_script(
        bin_dir / "xinput",
        "#!/bin/sh\ncat <<'EOF'\n" + XINPUT_SAMPLE + "EOF\n",
    )
    _write_script(bin_dir / "import", "#!/bin/sh\nexec cat \"$FAKE_PNG\"\n")
    _write_script(
        bin_dir / "xclip",
        """#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = "-in" ]; then
    cat > "$FAKE_CLIP_FILE"
    exit 0
  fi
  if [ "$arg" = "-o" ]; then
    cat "$FAKE_CLIP_FILE"
    exit 0
  fi
done
exit 1
""",
    )
    _write_script(
        bin_dir / "xev",
        "#!/bin/sh\ncat <<'EOF'\n" + XEV_SAMPLE + "EOF\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return bin_dir


def _make_png(tmp_path: Path) -> Path:
    """生成尺寸正确且非全同色的 PNG。

    Args:
        tmp_path: 临时目录。

    Returns:
        PNG 路径。
    """
    image = Image.new("RGB", (1280, 800), (32, 64, 96))
    image.putpixel((10, 10), (255, 255, 255))
    path = tmp_path / "shot.png"
    image.save(path, format="PNG")
    return path


def _write_artifacts(path: Path) -> None:
    """写入门禁读取的设备信息。

    Args:
        path: JSON 路径。
    """
    artifacts = DeviceArtifacts(
        keyboard_name="wechat-gui virtual keyboard",
        keyboard_event=str(path.parent / "event5"),
        pointer_name="wechat-gui virtual pointer",
        pointer_event=str(path.parent / "event9"),
        width=1280,
        height=800,
    )
    (path.parent / "event5").write_text("", encoding="utf-8")
    (path.parent / "event9").write_text("", encoding="utf-8")
    path.write_text(json.dumps(artifacts.to_wire()), encoding="utf-8")


async def _ok_control_server(socket_path: Path) -> asyncio.AbstractServer:
    """启动只回 ``ok=true`` 的控制面替身服务。

    Args:
        socket_path: socket 路径。

    Returns:
        已启动的服务对象。
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        try:
            payload = json.loads(line.decode("utf-8"))
            command = payload.get("command", "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            command = ""
        writer.write(
            (json.dumps({"ok": True, "command": command}) + "\n").encode("utf-8")
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(socket_path))


async def _failing_control_server(socket_path: Path) -> asyncio.AbstractServer:
    """启动恒回 ``ok=false`` 的控制面替身服务（模拟注入失败）。

    Args:
        socket_path: socket 路径。

    Returns:
        已启动的服务对象。
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        try:
            payload = json.loads(line.decode("utf-8"))
            command = payload.get("command", "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            command = ""
        writer.write(
            (
                json.dumps({"ok": False, "command": command, "error": "注入失败"}) + "\n"
            ).encode("utf-8")
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(socket_path))


async def _close_server(server: asyncio.AbstractServer) -> None:
    """关闭 asyncio 服务。

    Args:
        server: 服务对象。
    """
    server.close()
    await server.wait_closed()


async def test_parse_xdpyinfo() -> None:
    """解析分辨率与根窗口色深。"""
    info = poc_gate.parse_xdpyinfo(XDPYINFO_SAMPLE)
    assert (info.width, info.height, info.depth) == (1280, 800, 24)


async def test_parse_xdpyinfo_missing_fields() -> None:
    """缺少关键字段时报错。"""
    with pytest.raises(ValueError, match="缺少"):
        poc_gate.parse_xdpyinfo("name of display: :99\n")


async def test_parse_xinput_list() -> None:
    """解析设备名 / id / 漂浮标记。"""
    devices = poc_gate.parse_xinput_list(XINPUT_SAMPLE)
    by_name = {device.name: device for device in devices}
    assert by_name["wechat-gui virtual keyboard"].floating is False
    assert by_name["wechat-gui virtual pointer"].device_id == 7
    floating = poc_gate.parse_xinput_list(XINPUT_FLOATING_SAMPLE)
    keyboard = next(device for device in floating if device.name == "wechat-gui virtual keyboard")
    assert keyboard.floating is True


async def test_parse_xev_output() -> None:
    """统计关键事件数。"""
    counts = poc_gate.parse_xev_output(XEV_SAMPLE)
    assert (counts.key_press, counts.key_release) == (1, 1)
    assert counts.button_press == 1
    assert counts.button_release == 1
    assert counts.motion == 1


async def test_validate_screenshot_accepts_non_uniform(tmp_path: Path) -> None:
    """尺寸正确且非全同色的 PNG 通过。"""
    stats = poc_gate.validate_screenshot(
        _make_png(tmp_path).read_bytes(), width=1280, height=800
    )
    assert stats.width == 1280
    assert stats.height == 800
    assert stats.channel_max > stats.channel_min


@pytest.mark.parametrize(
    ("mode", "color", "message"),
    [
        ("RGB", (0, 0, 0), "全黑"),
        ("RGB", (255, 255, 255), "全同色"),
    ],
)
async def test_validate_screenshot_rejects_uniform(
    tmp_path: Path, mode: str, color: tuple[int, int, int], message: str
) -> None:
    """全黑 / 全同色截图被拒。"""
    image = Image.new(mode, (1280, 800), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with pytest.raises(ValueError, match=message):
        poc_gate.validate_screenshot(buffer.getvalue(), width=1280, height=800)


async def test_validate_screenshot_rejects_wrong_size_and_non_png(tmp_path: Path) -> None:
    """尺寸不符与非 PNG 被拒。"""
    image = Image.new("RGB", (8, 8), (1, 2, 3))
    image.putpixel((0, 0), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    with pytest.raises(ValueError, match="尺寸"):
        poc_gate.validate_screenshot(buffer.getvalue(), width=1280, height=800)
    with pytest.raises(ValueError, match="不是 PNG"):
        poc_gate.validate_screenshot(b"GIF89a", width=8, height=8)


async def test_run_command_success_and_failure() -> None:
    """命令执行返回原始输出；非零退出码由调用方判定。"""
    success = await poc_gate.run_command(
        [sys.executable, "-c", "print('hello')"], timeout=5.0
    )
    assert success.returncode == 0
    assert success.stdout_text.strip() == "hello"
    failure = await poc_gate.run_command([sys.executable, "-c", "raise SystemExit(3)"], timeout=5.0)
    assert failure.returncode == 3
    with pytest.raises(poc_gate.StepFailure, match="命令失败"):
        poc_gate._require_success(failure, "demo")


async def test_run_command_without_capture() -> None:
    """关闭采集时输出接 /dev/null（为 fork 常驻子进程的命令准备）。"""
    output = await poc_gate.run_command(
        [sys.executable, "-c", "print('hidden')"], timeout=5.0, capture_output=False
    )
    assert output.returncode == 0
    assert output.stdout == b""
    assert output.stderr == b""


async def test_run_command_timeout_kills_process() -> None:
    """超时命令被强杀并归一为 StepFailure。"""
    with pytest.raises(poc_gate.StepFailure, match="命令超时"):
        await poc_gate.run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1
        )


async def test_run_command_missing_binary() -> None:
    """不存在的命令给出可读失败。"""
    with pytest.raises(poc_gate.StepFailure, match="无法启动命令"):
        await poc_gate.run_command(["definitely-missing-command"], timeout=1.0)


async def test_wait_for_tcp_and_http() -> None:
    """TCP / HTTP 探测成功路径。"""
    tcp_server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    assert tcp_server.sockets is not None
    port = tcp_server.sockets[0].getsockname()[1]

    async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    assert http_server.sockets is not None
    http_port = http_server.sockets[0].getsockname()[1]
    try:
        await poc_gate.wait_for_tcp("127.0.0.1", port, timeout=1.0)
        assert await poc_gate.http_status("127.0.0.1", http_port, "/vnc.html", timeout=1.0) == 200
    finally:
        await _close_server(tcp_server)
        await _close_server(http_server)


async def test_wait_for_tcp_timeout() -> None:
    """不可连接端口在截止点失败。"""
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    await _close_server(server)
    with pytest.raises(poc_gate.StepFailure, match="不可连接"):
        await poc_gate.wait_for_tcp("127.0.0.1", port, timeout=0.1)


async def test_run_steps_fail_fast_and_skip() -> None:
    """首个失败后后续步骤标记跳过，耗时字段存在。"""
    calls: list[str] = []

    async def ok(_ctx: GateContext) -> str:
        calls.append("ok")
        return "ok"

    async def bad(_ctx: GateContext) -> str:
        calls.append("bad")
        raise poc_gate.StepFailure("坏步骤")

    async def boom(_ctx: GateContext) -> str:
        calls.append("boom")
        raise RuntimeError("炸了")

    records = await poc_gate.run_steps(
        (
            StepSpec("one", ok),
            StepSpec("two", bad),
            StepSpec("three", ok),
        ),
        GateContext(),
    )
    assert [record.status for record in records] == [
        StepStatus.PASSED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    ]
    assert calls == ["ok", "bad"]
    assert records[1].detail == "坏步骤"
    assert all(record.duration_perf_counter_ms >= 0 for record in records)

    records = await poc_gate.run_steps((StepSpec("boom", boom),), GateContext())
    assert records[0].status is StepStatus.FAILED
    assert "未预期异常" in records[0].detail


async def test_build_report_verdict_and_wire() -> None:
    """报告在任一步未通过时为 FAIL，并可序列化为单行载体。"""
    passed = StepRecord("x_up", StepStatus.PASSED, 1, "ok")
    skipped = StepRecord("uinput_create", StepStatus.SKIPPED, 0, "跳过")
    report = poc_gate.build_report(
        [passed],
        hostname="h",
        kernel="k",
        docker=None,
        started_at_ms=1700000000000,
        duration_perf_counter_ms=5,
    )
    assert report.verdict == "PASS"
    wire = report.to_wire()
    assert wire["hostname"] == "h"
    assert wire["docker"] is None
    assert wire["started_at_ms"] == 1700000000000
    assert wire["steps"] == [
        {
            "name": "x_up",
            "status": "passed",
            "duration_perf_counter_ms": 1,
            "detail": "ok",
        }
    ]

    failing = poc_gate.build_report(
        [passed, skipped],
        hostname="h",
        kernel="k",
        docker="Docker version",
        started_at_ms=1700000000000,
        duration_perf_counter_ms=5,
    )
    assert failing.verdict == "FAIL"

    empty = poc_gate.build_report(
        [],
        hostname="h",
        kernel="k",
        docker=None,
        started_at_ms=0,
        duration_perf_counter_ms=0,
    )
    assert empty.verdict == "FAIL"


async def test_step_x_up_and_depth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x_up 通过正常样本；色深不符时失败。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    ctx = GateContext(devices_json=tmp_path / "devices.json")
    assert "1280x800x24" in await poc_gate.step_x_up(ctx)

    _write_script(
        bin_dir / "xdpyinfo",
        "#!/bin/sh\ncat <<'EOF'\n"
        "  dimensions:    1280x800 pixels\n"
        "depth of root window:    16 planes\n"
        "EOF\n",
    )
    with pytest.raises(poc_gate.StepFailure, match="色深"):
        await poc_gate.step_x_up(ctx)


async def test_step_x_up_xdpyinfo_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xdpyinfo 非零退出时失败。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    _write_script(bin_dir / "xdpyinfo", "#!/bin/sh\necho boom >&2\nexit 2\n")
    with pytest.raises(poc_gate.StepFailure, match="xdpyinfo 命令失败"):
        await poc_gate.step_x_up(GateContext())


async def test_step_uinput_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """设备信息 / 节点 / 控制面全部就绪时通过。"""
    _install_fake_commands(tmp_path, monkeypatch)
    devices_json = tmp_path / "devices.json"
    _write_artifacts(devices_json)
    socket_path = tmp_path / "control.sock"
    server = await _ok_control_server(socket_path)
    try:
        ctx = GateContext(devices_json=devices_json, control_socket=socket_path)
        detail = await poc_gate.step_uinput_create(ctx)
    finally:
        await _close_server(server)
    assert "wechat-gui virtual keyboard" in detail


async def test_step_uinput_create_missing_node(tmp_path: Path) -> None:
    """事件节点缺失时失败。"""
    devices_json = tmp_path / "devices.json"
    _write_artifacts(devices_json)
    (tmp_path / "event9").unlink()
    socket_path = tmp_path / "control.sock"
    server = await _ok_control_server(socket_path)
    try:
        with pytest.raises(poc_gate.StepFailure, match="事件节点不存在"):
            await poc_gate.step_uinput_create(
                GateContext(devices_json=devices_json, control_socket=socket_path)
            )
    finally:
        await _close_server(server)


async def test_step_uinput_create_control_failure(tmp_path: Path) -> None:
    """控制面不可达时失败。"""
    devices_json = tmp_path / "devices.json"
    _write_artifacts(devices_json)
    with pytest.raises(poc_gate.StepFailure, match="无法连接控制面"):
        await poc_gate.step_uinput_create(
            GateContext(
                devices_json=devices_json,
                control_socket=tmp_path / "missing.sock",
                operation_timeout=0.2,
            )
        )


async def test_step_xinput_visible_and_floating_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常样本通过；漂浮设备失败。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    devices_json = tmp_path / "devices.json"
    _write_artifacts(devices_json)
    ctx = GateContext(devices_json=devices_json)
    assert "attached" in await poc_gate.step_xinput_visible(ctx)

    _write_script(
        bin_dir / "xinput",
        "#!/bin/sh\ncat <<'EOF'\n" + XINPUT_FLOATING_SAMPLE + "EOF\n",
    )
    with pytest.raises(poc_gate.StepFailure, match="漂浮"):
        await poc_gate.step_xinput_visible(ctx)


async def test_step_event_roundtrip_uses_line_buffered_single_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xev 经 stdbuf 行缓冲启动，单轮注入后完整回环通过。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    stdbuf_log = tmp_path / "stdbuf.log"
    _write_script(
        bin_dir / "stdbuf",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_STDBUF_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-e) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
exec "$@"
""",
    )
    monkeypatch.setenv("FAKE_STDBUF_LOG", str(stdbuf_log))
    socket_path = tmp_path / "control.sock"
    server = await _ok_control_server(socket_path)
    try:
        detail = await poc_gate.step_event_roundtrip(
            GateContext(control_socket=socket_path, event_timeout=2.0)
        )
    finally:
        await _close_server(server)
    assert "KeyPress=1" in detail
    assert "ButtonPress=1" in detail
    assert "ButtonRelease=1" in detail
    assert "Motion=1" in detail
    assert "单轮注入" in detail
    assert stdbuf_log.read_text(encoding="utf-8").split()[0] == "-oL"


async def test_step_event_roundtrip_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xev 无输出时在截止点失败。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    _write_script(bin_dir / "xev", "#!/bin/sh\nexit 0\n")
    socket_path = tmp_path / "control.sock"
    server = await _ok_control_server(socket_path)
    try:
        with pytest.raises(poc_gate.StepFailure, match="事件回环不完整"):
            await poc_gate.step_event_roundtrip(
                GateContext(control_socket=socket_path, event_timeout=0.3)
            )
    finally:
        await _close_server(server)


@pytest.mark.parametrize(
    ("sample", "missing"),
    [
        (XEV_WITHOUT_MOTION, "Motion=0"),
        (XEV_WITHOUT_BUTTON_RELEASE, "ButtonRelease=0"),
    ],
)
async def test_step_event_roundtrip_rejects_incomplete_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample: str,
    missing: str,
) -> None:
    """缺少指针移动或左键抬起时失败（断言面覆盖 Motion / ButtonRelease）。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    _write_script(bin_dir / "xev", "#!/bin/sh\ncat <<'EOF'\n" + sample + "EOF\n")
    socket_path = tmp_path / "control.sock"
    server = await _ok_control_server(socket_path)
    try:
        with pytest.raises(poc_gate.StepFailure, match="事件回环不完整") as excinfo:
            await poc_gate.step_event_roundtrip(
                GateContext(control_socket=socket_path, event_timeout=0.3)
            )
    finally:
        await _close_server(server)
    assert missing in str(excinfo.value)


async def test_step_event_roundtrip_injection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """控制面注入失败时立即失败并携带原因。"""
    _install_fake_commands(tmp_path, monkeypatch)
    socket_path = tmp_path / "control.sock"
    server = await _failing_control_server(socket_path)
    try:
        with pytest.raises(poc_gate.StepFailure, match="控制面注入失败：注入失败"):
            await poc_gate.step_event_roundtrip(
                GateContext(control_socket=socket_path, event_timeout=1.0)
            )
    finally:
        await _close_server(server)


async def test_step_screenshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """截图校验成功路径。"""
    _install_fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_PNG", str(_make_png(tmp_path)))
    detail = await poc_gate.step_screenshot(GateContext())
    assert "1280x800" in detail


async def test_step_screenshot_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全黑截图被拒。"""
    _install_fake_commands(tmp_path, monkeypatch)
    black = Image.new("RGB", (1280, 800), (0, 0, 0))
    path = tmp_path / "black.png"
    black.save(path, format="PNG")
    monkeypatch.setenv("FAKE_PNG", str(path))
    with pytest.raises(poc_gate.StepFailure, match="全黑"):
        await poc_gate.step_screenshot(GateContext())


async def test_step_clipboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """剪贴板写入后读回一致。"""
    _install_fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CLIP_FILE", str(tmp_path / "clip"))
    detail = await poc_gate.step_clipboard(GateContext(ready_timeout=1.0))
    assert "回读一致" in detail


async def test_step_clipboard_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读回值与写入值不同即失败（Falsifiability：回读真的被比对）。"""
    bin_dir = _install_fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CLIP_FILE", str(tmp_path / "clip"))
    _write_script(
        bin_dir / "xclip",
        """#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = "-in" ]; then
    cat > "$FAKE_CLIP_FILE"
    exit 0
  fi
  if [ "$arg" = "-o" ]; then
    printf 'stale-clipboard-value'
    exit 0
  fi
done
exit 1
""",
    )
    with pytest.raises(poc_gate.StepFailure, match="剪贴板回读不一致") as excinfo:
        await poc_gate.step_clipboard(GateContext(ready_timeout=0.05))
    assert "stale-clipboard-value" in str(excinfo.value)


async def test_step_vnc_up(tmp_path: Path) -> None:
    """VNC 端口与 noVNC HTTP 均就绪时通过。"""
    tcp_server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    assert tcp_server.sockets is not None
    vnc_port = tcp_server.sockets[0].getsockname()[1]

    async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    assert http_server.sockets is not None
    novnc_port = http_server.sockets[0].getsockname()[1]
    try:
        detail = await poc_gate.step_vnc_up(
            GateContext(
                vnc_port=vnc_port,
                novnc_port=novnc_port,
                ready_timeout=1.0,
                operation_timeout=1.0,
            )
        )
    finally:
        await _close_server(tcp_server)
        await _close_server(http_server)
    assert "noVNC" in detail


async def test_step_vnc_up_rejects_non_200() -> None:
    """noVNC 返回非 200 时失败。"""
    tcp_server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    assert tcp_server.sockets is not None
    vnc_port = tcp_server.sockets[0].getsockname()[1]

    async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(
            b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    http_server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    assert http_server.sockets is not None
    novnc_port = http_server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(poc_gate.StepFailure, match="返回 503"):
            await poc_gate.step_vnc_up(
                GateContext(
                    vnc_port=vnc_port,
                    novnc_port=novnc_port,
                    ready_timeout=1.0,
                    operation_timeout=1.0,
                )
            )
    finally:
        await _close_server(tcp_server)
        await _close_server(http_server)


async def test_collect_environment_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker 不可得时返回 None。"""
    monkeypatch.setattr(poc_gate.shutil, "which", lambda _name: None)
    hostname, kernel, docker = await poc_gate.collect_environment()
    assert hostname
    assert kernel
    assert docker is None


async def test_collect_environment_with_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker 可得时经统一命令执行面收集版本字符串。"""
    monkeypatch.setattr(poc_gate.shutil, "which", lambda _name: "/usr/bin/docker")
    calls: list[tuple[str, ...]] = []

    async def fake_run(argv: Sequence[str], *, timeout: float) -> CommandOutput:
        calls.append(tuple(argv))
        assert timeout == 2.0
        return CommandOutput(
            returncode=0,
            stdout=b"Docker version 29.9.2, build deadbeef\n",
            stderr=b"",
        )

    monkeypatch.setattr(poc_gate, "run_command", fake_run)
    _hostname, _kernel, docker = await poc_gate.collect_environment()
    assert docker == "Docker version 29.9.2, build deadbeef"
    assert calls == [("docker", "--version")]


async def test_collect_environment_swallows_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker 版本探测超时按不可得处理。"""
    monkeypatch.setattr(poc_gate.shutil, "which", lambda _name: "/usr/bin/docker")

    async def fake_run(argv: Sequence[str], *, timeout: float) -> CommandOutput:
        del argv, timeout
        raise poc_gate.StepFailure("命令超时（2.0s）：docker")

    monkeypatch.setattr(poc_gate, "run_command", fake_run)
    _hostname, _kernel, docker = await poc_gate.collect_environment()
    assert docker is None


async def test_run_gate_composes_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_gate 按步骤表执行并汇总环境与耗时（时间戳为 Epoch 毫秒）。"""

    async def ok(_ctx: GateContext) -> str:
        return "ok"

    async def fake_environment() -> tuple[str, str, str | None]:
        return "host", "kernel", None

    monkeypatch.setattr(poc_gate, "build_steps", lambda: (StepSpec("one", ok),))
    monkeypatch.setattr(poc_gate, "collect_environment", fake_environment)
    report = await poc_gate.run_gate(GateContext())
    assert report.verdict == "PASS"
    assert report.hostname == "host"
    assert report.docker is None
    assert report.started_at_ms > 1_600_000_000_000
    assert isinstance(report.to_wire()["steps"], list)


async def test_main_writes_single_line_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 输出单行 JSON 并落盘，PASS 退出 0。"""
    record = StepRecord("x_up", StepStatus.PASSED, 1, "ok")

    async def fake_run_gate(_ctx: GateContext) -> poc_gate.GateReport:
        return poc_gate.build_report(
            [record],
            hostname="host",
            kernel="kernel",
            docker=None,
            started_at_ms=1700000000000,
            duration_perf_counter_ms=1,
        )

    monkeypatch.setattr(poc_gate, "run_gate", fake_run_gate)
    out = tmp_path / "out.json"
    code = await asyncio.to_thread(poc_gate.main, ["--out", str(out)])
    captured = capsys.readouterr()
    assert code == 0
    line = captured.out.strip()
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["verdict"] == "PASS"
    assert payload["started_at_ms"] == 1700000000000
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "PASS"


async def test_main_fail_returns_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL 退出 1。"""
    record = StepRecord("x_up", StepStatus.FAILED, 1, "bad")

    async def fake_run_gate(_ctx: GateContext) -> poc_gate.GateReport:
        return poc_gate.build_report(
            [record],
            hostname="host",
            kernel="kernel",
            docker=None,
            started_at_ms=1700000000000,
            duration_perf_counter_ms=1,
        )

    monkeypatch.setattr(poc_gate, "run_gate", fake_run_gate)
    assert await asyncio.to_thread(poc_gate.main, []) == 1
    capsys.readouterr()


async def test_main_usage_error_exits_two() -> None:
    """未知参数以退出码 2 收口。"""
    with pytest.raises(SystemExit) as excinfo:
        await asyncio.to_thread(poc_gate.main, ["--unknown-flag"])
    assert excinfo.value.code == 2


async def test_xev_connected_requires_socket_fd(tmp_path: Path) -> None:
    """就绪探测只认 socket fd：仅普通 fd 为未就绪，进程消失视为可继续。"""
    pid = 4242
    fd_dir = tmp_path / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "0").symlink_to("/dev/null")
    (fd_dir / "1").symlink_to("/dev/null")
    assert poc_gate._xev_connected(pid, proc_root=tmp_path) is False
    (fd_dir / "3").symlink_to("socket:[12345]")
    assert poc_gate._xev_connected(pid, proc_root=tmp_path) is True
    assert poc_gate._xev_connected(9999, proc_root=tmp_path) is True
