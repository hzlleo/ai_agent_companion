"""宿主运行器会话守卫的 fail-closed 自证。

用临时目录充当 X11 / Wayland 套接字根（脚本支持参数化根路径并可在被
source 时只定义函数），验证「套接字存在即视为活动会话」——不依赖
``xdpyinfo`` 可连接，连不上时也拒绝，避免误注入宿主。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[3] / "poc" / "run_stack_poc.sh"


async def _detect(*, x11_dir: Path, wayland_dir: Path) -> tuple[str, str, str]:
    """在临时 socket 根下调用运行器的会话探测函数。

    Args:
        x11_dir: X11 套接字根。
        wayland_dir: Wayland 套接字根。

    Returns:
        (HOST_SESSION_ACTIVE, HOST_SESSION_WAYLAND, HOST_DISPLAY)。
    """
    env = dict(os.environ)
    env["WECHAT_GUI_POC_X11_SOCKET_DIR"] = str(x11_dir)
    env["WECHAT_GUI_POC_WAYLAND_SOCKET_DIR"] = str(wayland_dir)
    script = (
        'source "$1" || exit 99; '
        "detect_host_session || true; "
        'printf "%s\\t%s\\t%s\\n" "$HOST_SESSION_ACTIVE" "$HOST_SESSION_WAYLAND" '
        '"${HOST_DISPLAY:-}"'
    )
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script,
        "runner",
        str(RUNNER),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    active, wayland, display = stdout.decode().rstrip("\n").split("\t", 2)
    return active, wayland, display


async def test_detect_no_sockets_is_inactive(tmp_path: Path) -> None:
    """无 X11 / Wayland 套接字时判定无活动会话。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    assert await _detect(x11_dir=x11_dir, wayland_dir=wayland_dir) == ("0", "0", "")


async def test_detect_x11_socket_is_active_without_xdpyinfo(
    tmp_path: Path,
) -> None:
    """仅存在 X11 套接字即判活动（fail-closed，不要求可连接）。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    (x11_dir / "X0").touch()
    assert await _detect(x11_dir=x11_dir, wayland_dir=wayland_dir) == ("1", "0", ":0")


async def test_detect_wayland_socket_marks_wayland(tmp_path: Path) -> None:
    """存在 Wayland 套接字时标记 Wayland 会话（float 不可行）。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    (wayland_dir / "wayland-0").touch()
    assert await _detect(x11_dir=x11_dir, wayland_dir=wayland_dir) == ("1", "1", "")


async def _float_driver(body: str, *, running: bool = True) -> str:
    """在 source 后替换探测 / 浮动 / 容器函数，驱动同步判定。

    Args:
        body: 覆盖 ``float_host_devices`` 的函数定义。
        running: 容器是否被视为仍在运行（false 时循环提前结束）。

    Returns:
        ``rc=<n> floated=<n> ignored=<n>`` 形式的输出。
    """
    container_rc = 0 if running else 1
    script = (
        'source "$1" || exit 99; '
        "host_device_present() { return 0; }; "
        "host_devices_present() { return 0; }; "
        f"container_running() {{ return {container_rc}; }}; "
        f"{body}; "
        "if float_until_settled 3; then rc=0; else rc=$?; fi; "
        'printf "rc=%s floated=%s ignored=%s\\n" "$rc" '
        '"${#FLOATED_DEVICES[@]}" "${#IGNORED_DEVICES[@]}"'
    )
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script,
        "runner",
        str(RUNNER),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    return stdout.decode().strip()


async def test_float_until_settled_accepts_udev_ignored_devices() -> None:
    """sysfs 有设备但 xinput 看不到（udev 忽略）连续两轮即安全。"""
    body = (
        "float_host_devices() { "
        'printf "ABSENT %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == "rc=0 floated=0 ignored=2"


async def test_float_until_settled_counts_floated_devices() -> None:
    """xinput 可见且浮动成功的设备计入 floated。"""
    body = (
        "float_host_devices() { "
        'printf "SAFE %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == "rc=0 floated=2 ignored=0"


async def test_float_until_settled_fails_when_neither_safe_nor_absent() -> None:
    """xinput 可见但浮动失败（RETRY / 空输出）时判定失败。"""
    body = "float_host_devices() { return 0; }"
    assert await _float_driver(body, running=False) == "rc=1 floated=0 ignored=0"
