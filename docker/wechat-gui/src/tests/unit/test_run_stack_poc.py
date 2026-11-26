"""宿主运行器会话守卫与安全态判定的 fail-closed 自证。

用临时目录充当 X11 / Wayland 套接字根、sysfs 与 /dev/input，脚本支持
参数化根路径并可在被 source 时只定义函数。覆盖：套接字存在即视为活动
会话（不依赖 ``xdpyinfo`` 可连接）、全部 X 会话遍历、同名多行必须全部
floating 且连续两轮、xinput 列举失败不等于 ABSENT、udev 正证据与无
udevadm 回落、起容器前残留设备拒绝。
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[3] / "poc" / "run_stack_poc.sh"

KEYBOARD = "wechat-gui virtual keyboard"
POINTER = "wechat-gui virtual pointer"


async def _run_sourced(
    body: str, *, env_extra: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """source 运行器后执行给定脚本。

    Args:
        body: 在 source 之后执行的 bash 片段。
        env_extra: 追加 / 覆盖的环境变量。

    Returns:
        (退出码, stdout, stderr)。
    """
    env = dict(os.environ)
    if env_extra is not None:
        env.update(env_extra)
    script = 'source "$1" || exit 99; ' + body
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
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


async def _detect(
    *,
    x11_dir: Path,
    wayland_dir: Path | None,
    xdg_dir: Path | None = None,
    scan_root: Path | None = None,
) -> tuple[str, str, str]:
    """在临时 socket 根下调用运行器的会话探测函数。

    Args:
        x11_dir: X11 套接字根。
        wayland_dir: 显式 Wayland 套接字根（None 时走 XDG / 扫描回落）。
        xdg_dir: 显式 XDG_RUNTIME_DIR（None 时沿用环境）。
        scan_root: /run/user 的替代扫描根（None 时沿用环境）。

    Returns:
        (HOST_SESSION_ACTIVE, HOST_SESSION_WAYLAND, HOST_DISPLAYS 逗号串)。
    """
    env_extra = {"WECHAT_GUI_POC_X11_SOCKET_DIR": str(x11_dir)}
    if wayland_dir is not None:
        env_extra["WECHAT_GUI_POC_WAYLAND_SOCKET_DIR"] = str(wayland_dir)
    if xdg_dir is not None:
        env_extra["XDG_RUNTIME_DIR"] = str(xdg_dir)
    if scan_root is not None:
        env_extra["WECHAT_GUI_POC_WAYLAND_SCAN_ROOT"] = str(scan_root)
    body = (
        "detect_host_session || true; "
        'printf "%s\\t%s\\t%s\\n" "$HOST_SESSION_ACTIVE" "$HOST_SESSION_WAYLAND" '
        '"$(IFS=,; printf "%s" "${HOST_DISPLAYS[*]:-}")"'
    )
    rc, stdout, stderr = await _run_sourced(body, env_extra=env_extra)
    assert rc == 0, stderr
    active, wayland, displays = stdout.rstrip("\n").split("\t", 2)
    return active, wayland, displays


def _write_executable(path: Path, body: str) -> None:
    """写入可执行脚本。

    Args:
        path: 目标路径。
        body: 脚本正文（不含 shebang）。
    """
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_xinput(
    bin_dir: Path,
    *,
    name_only: list[str] | None,
    list_lines: list[str] | None,
) -> None:
    """写入假 xinput：``--name-only`` 与 ``list`` 可按需成功或失败。

    Args:
        bin_dir: 存放假命令的目录。
        name_only: ``list --name-only`` 的输出行；None 表示该调用失败。
        list_lines: ``list`` 的输出行；None 表示该调用失败。
    """

    def emit(lines: list[str] | None) -> str:
        if lines is None:
            return "  exit 1"
        return "  printf '%s\\n' " + " ".join(shlex.quote(line) for line in lines)

    script = (
        'if [[ "$1" == "list" && "${2:-}" == "--name-only" ]]; then\n'
        f"{emit(name_only)}\n  exit 0\nfi\n"
        'if [[ "$1" == "list" ]]; then\n'
        f"{emit(list_lines)}\n  exit 0\nfi\n"
        'if [[ "$1" == "float" ]]; then\n  exit 0\nfi\n'
        "exit 1\n"
    )
    _write_executable(bin_dir / "xinput", script)


def _make_sysfs(tmp_path: Path, names: list[str]) -> tuple[Path, Path]:
    """构造同名设备的 sysfs 树与 /dev/input 节点。

    节点权限置 000：宿主上事件节点是 ``root:input 0660``，运行用户通常
    不在 ``input`` 组而不可读；udev 正证据只依赖 ``udevadm info``，不得把
    节点可读性当作前提。

    Args:
        tmp_path: 临时根。
        names: 每台设备的内核名。

    Returns:
        (sysfs 根, /dev/input 根)。
    """
    sysfs = tmp_path / "sys-class-input"
    devinput = tmp_path / "dev-input"
    sysfs.mkdir()
    devinput.mkdir()
    for index, name in enumerate(names, start=42):
        node = f"event{index}"
        device_dir = sysfs / node / "device"
        device_dir.mkdir(parents=True)
        (device_dir / "name").write_text(name + "\n", encoding="utf-8")
        node_path = devinput / node
        node_path.touch()
        node_path.chmod(0o000)
    return sysfs, devinput


async def test_detect_no_sockets_is_inactive(tmp_path: Path) -> None:
    """无 X11 / Wayland 套接字时判定无活动会话。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    assert await _detect(
        x11_dir=x11_dir, wayland_dir=wayland_dir, xdg_dir=tmp_path / "xdg"
    ) == ("0", "0", "")


async def test_detect_x11_socket_is_active_without_xdpyinfo(
    tmp_path: Path,
) -> None:
    """仅存在 X11 套接字即判活动（fail-closed，不要求可连接）。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    (x11_dir / "X0").touch()
    assert await _detect(
        x11_dir=x11_dir, wayland_dir=wayland_dir, xdg_dir=tmp_path / "xdg"
    ) == ("1", "0", ":0")


async def test_detect_enumerates_every_x11_socket(tmp_path: Path) -> None:
    """同机多个 X 会话必须全部收集，不能只取第一个。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    (x11_dir / "X0").touch()
    (x11_dir / "X1").touch()
    assert await _detect(
        x11_dir=x11_dir, wayland_dir=wayland_dir, xdg_dir=tmp_path / "xdg"
    ) == ("1", "0", ":0,:1")


async def test_detect_wayland_socket_marks_wayland(tmp_path: Path) -> None:
    """存在 Wayland 套接字时标记 Wayland 会话（float 不可行）。"""
    x11_dir = tmp_path / "x11"
    wayland_dir = tmp_path / "wayland"
    x11_dir.mkdir()
    wayland_dir.mkdir()
    (wayland_dir / "wayland-0").touch()
    assert await _detect(
        x11_dir=x11_dir, wayland_dir=wayland_dir, xdg_dir=tmp_path / "xdg"
    ) == ("1", "1", "")


async def test_detect_wayland_falls_back_to_run_user_scan(tmp_path: Path) -> None:
    """未显式指定 Wayland 目录时扫描 /run/user/<uid>/wayland-*。"""
    x11_dir = tmp_path / "x11"
    xdg_dir = tmp_path / "xdg"
    scan_root = tmp_path / "run-user"
    x11_dir.mkdir()
    xdg_dir.mkdir()
    (scan_root / "1000").mkdir(parents=True)
    (scan_root / "1000" / "wayland-0").touch()
    assert await _detect(
        x11_dir=x11_dir,
        wayland_dir=None,
        xdg_dir=xdg_dir,
        scan_root=scan_root,
    ) == ("1", "1", "")


async def _float_driver(
    body: str,
    *,
    running: bool = True,
    marker: Path | None = None,
) -> str:
    """在 source 后替换探测 / 浮动 / 容器函数，驱动安全态判定。

    Args:
        body: 覆盖 ``float_host_devices`` 等函数的定义。
        running: 容器是否被视为仍在运行（false 时循环提前结束）。
        marker: 非空时，仅在 ``float_until_settled`` 成功时 touch 该文件。

    Returns:
        ``rc=<n> floated=<n> ignored=<n> basis=<...>`` 形式的输出。
    """
    container_rc = 0 if running else 1
    if marker is None:
        settle = "if float_until_settled 3; then rc=0; else rc=$?; fi; "
    else:
        settle = (
            f'if float_until_settled 3; then touch "{marker}"; '
            "rc=0; else rc=$?; fi; "
        )
    body = (
        "host_device_present() { return 0; }; "
        "host_devices_present() { return 0; }; "
        "host_device_ignored_evidence() { return 0; }; "
        f"container_running() {{ return {container_rc}; }}; "
        'HOST_DISPLAYS=(":0"); '
        f"{body}; "
        f"{settle}"
        'printf "rc=%s floated=%s ignored=%s basis=%s\\n" "$rc" '
        '"${#FLOATED_DEVICES[@]}" "${#IGNORED_DEVICES[@]}" "${IGNORED_BASIS}"'
    )
    rc, stdout, stderr = await _run_sourced(body)
    assert rc == 0, stderr
    return stdout.strip()


async def test_reject_residual_devices_exits_before_create() -> None:
    """起容器前同名设备存在即 exit 1；不存在则继续。"""
    present = (
        "host_device_present() { return 0; }; "
        "reject_residual_host_devices; "
        'printf "reached\\n"'
    )
    rc, stdout, _ = await _run_sourced(present)
    assert rc == 1
    assert stdout == ""

    absent = (
        "host_device_present() { return 1; }; "
        "reject_residual_host_devices; "
        'printf "reached\\n"'
    )
    rc, stdout, stderr = await _run_sourced(absent)
    assert rc == 0, stderr
    assert stdout == "reached\n"


async def test_float_probe_requires_all_rows_floating(tmp_path: Path) -> None:
    """同名残留浮动 + 新设备接入时不得判 SAFE，且不落 touch 标记。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_xinput(
        bin_dir,
        name_only=[KEYBOARD, POINTER],
        list_lines=[
            f"~ {KEYBOARD}\tid=11\t[floating slave]",
            f"  {KEYBOARD}\tid=12\t[slave  keyboard (3)]",
            f"~ {POINTER}\tid=21\t[floating slave]",
            f"  {POINTER}\tid=22\t[slave  pointer  (2)]",
        ],
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    probe = (
        'probe="$(host_float_probe_script)"; '
        'bash -c "$probe" runner "${DEVICE_NAMES[@]}"'
    )
    rc, stdout, _ = await _run_sourced(probe, env_extra=env)
    assert rc == 0
    assert "SAFE" not in stdout
    assert stdout.count("RETRY") == 2

    marker = tmp_path / "touched"
    body = (
        f'_fake_bin="{bin_dir}"; '
        'float_host_devices() { local probe; '
        'probe="$(host_float_probe_script)"; '
        'PATH="$_fake_bin:${PATH}" bash -c "$probe" runner "${DEVICE_NAMES[@]}"; }'
    )
    assert await _float_driver(body, marker=marker) == (
        "rc=1 floated=0 ignored=0 basis="
    )
    assert not marker.exists()


async def test_float_probe_accepts_all_rows_floating_two_rounds(
    tmp_path: Path,
) -> None:
    """同名两行均为 floating 时两轮确认后判 SAFE 并结算。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_xinput(
        bin_dir,
        name_only=[KEYBOARD, POINTER],
        list_lines=[
            f"~ {KEYBOARD}\tid=11\t[floating slave]",
            f"~ {KEYBOARD}\tid=12\t[floating slave]",
            f"~ {POINTER}\tid=21\t[floating slave]",
            f"~ {POINTER}\tid=22\t[floating slave]",
        ],
    )
    body = (
        f'_fake_bin="{bin_dir}"; '
        'float_host_devices() { local probe; '
        'probe="$(host_float_probe_script)"; '
        'PATH="$_fake_bin:${PATH}" bash -c "$probe" runner "${DEVICE_NAMES[@]}"; }'
    )
    assert await _float_driver(body) == "rc=0 floated=2 ignored=0 basis="


async def test_float_probe_listing_failure_is_not_absence(tmp_path: Path) -> None:
    """列举失败 exit 3，绝不冒充 ABSENT，也不判 ignored。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_xinput(
        bin_dir,
        name_only=[KEYBOARD, POINTER],
        list_lines=None,
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    probe = (
        'probe="$(host_float_probe_script)"; '
        'bash -c "$probe" runner "${DEVICE_NAMES[@]}"'
    )
    rc, stdout, _ = await _run_sourced(probe, env_extra=env)
    assert rc == 3
    assert stdout == ""

    body = (
        f'_fake_bin="{bin_dir}"; '
        'float_host_devices() { local probe; '
        'probe="$(host_float_probe_script)"; '
        'PATH="$_fake_bin:${PATH}" bash -c "$probe" runner "${DEVICE_NAMES[@]}"; }'
    )
    assert await _float_driver(body) == "rc=1 floated=0 ignored=0 basis="


async def _ignored_evidence_rc(tmp_path: Path, *, id_input: bool) -> str:
    """以假 udevadm 调用正证据判定。

    Args:
        tmp_path: 临时根。
        id_input: 假 udevadm 是否额外输出 ID_INPUT 行。

    Returns:
        ``rc=<n>``。
    """
    sysfs, devinput = _make_sysfs(tmp_path, [KEYBOARD])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    extra = '  printf "%s\\n" "ID_INPUT=1"' if id_input else ""
    _write_executable(
        bin_dir / "udevadm",
        'printf "%s\\n" "DEVPATH=/devices/virtual/input/input42/event42" '
        '"LIBINPUT_IGNORE_DEVICE=1"\n' + extra + '\nexit 0\n',
    )
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "WECHAT_GUI_POC_SYSFS_INPUT_DIR": str(sysfs),
        "WECHAT_GUI_POC_HOST_INPUT_DIR": str(devinput),
    }
    body = (
        f'if host_device_ignored_evidence "{KEYBOARD}"; then rc=0; else rc=$?; fi; '
        'printf "rc=%s\\n" "$rc"'
    )
    rc, stdout, stderr = await _run_sourced(body, env_extra=env)
    assert rc == 0, stderr
    return stdout.strip()


async def test_udev_positive_evidence_marks_ignored(tmp_path: Path) -> None:
    """LIBINPUT_IGNORE_DEVICE=1 且无 ID_INPUT* 时正证据成立。"""
    assert await _ignored_evidence_rc(tmp_path, id_input=False) == "rc=0"


async def test_udev_evidence_rejects_residual_id_input(tmp_path: Path) -> None:
    """仍有 ID_INPUT* 行时正证据不成立。"""
    assert await _ignored_evidence_rc(tmp_path, id_input=True) == "rc=1"


async def test_float_until_settled_accepts_udev_ignored_devices() -> None:
    """sysfs 有设备但 xinput 看不到（udev 正证据）连续两轮即安全。"""
    body = (
        "float_host_devices() { "
        'printf "ABSENT %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == (
        "rc=0 floated=0 ignored=2 basis=udev_positive"
    )


async def test_float_until_settled_falls_back_without_udevadm() -> None:
    """无 udevadm 时按旧启发式判定，并标记 basis=heuristic。"""
    body = (
        "host_device_ignored_evidence() { return 2; }; "
        "float_host_devices() { "
        'printf "ABSENT %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == (
        "rc=0 floated=0 ignored=2 basis=heuristic"
    )


async def test_float_until_settled_counts_floated_devices() -> None:
    """xinput 可见且全部行浮动成功的设备计入 floated。"""
    body = (
        "float_host_devices() { "
        'printf "SAFE %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == "rc=0 floated=2 ignored=0 basis="


async def test_float_until_settled_fails_with_partial_safe_and_retry() -> None:
    """一台 SAFE、一台恒 RETRY 时不得结算（floated 不得提前计数）。"""
    body = (
        "float_host_devices() { "
        'printf "SAFE %s\\n" "${DEVICE_NAMES[0]}"; '
        'printf "RETRY %s\\n" "${DEVICE_NAMES[1]}"; return 0; }'
    )
    assert await _float_driver(body) == "rc=1 floated=0 ignored=0 basis="


async def test_float_until_settled_requires_two_absent_rounds(
    tmp_path: Path,
) -> None:
    """首轮 ABSENT、次轮起 RETRY 时必须失败（一轮 ABSENT 不足以结算）。"""
    counter = tmp_path / "round"
    body = (
        f'_counter="{counter}"; '
        "float_host_devices() { n=0; "
        'if [[ -f "$_counter" ]]; then n="$(cat "$_counter")"; fi; '
        'n=$((n + 1)); printf "%s" "$n" > "$_counter"; '
        'if [[ "$n" -eq 1 ]]; then '
        'printf "ABSENT %s\\n" "${DEVICE_NAMES[@]}"; '
        'else printf "RETRY %s\\n" "${DEVICE_NAMES[@]}"; fi; return 0; }'
    )
    assert await _float_driver(body) == "rc=1 floated=0 ignored=0 basis="


async def test_float_until_settled_fails_when_neither_safe_nor_absent() -> None:
    """xinput 可见但浮动失败（RETRY / 空输出）时判定失败。"""
    body = "float_host_devices() { return 0; }"
    assert await _float_driver(body, running=False) == (
        "rc=1 floated=0 ignored=0 basis="
    )


async def test_float_until_settled_probes_every_display(tmp_path: Path) -> None:
    """安全态判定必须遍历全部宿主 X 会话，而不是只探测第一个。"""
    log = tmp_path / "displays"
    body = (
        f'_log="{log}"; '
        'HOST_DISPLAYS=(":0" ":1"); '
        "float_host_devices() { "
        'printf "%s\\n" "$1" >> "$_log"; '
        'printf "SAFE %s\\n" "${DEVICE_NAMES[@]}"; return 0; }'
    )
    assert await _float_driver(body) == "rc=0 floated=2 ignored=0 basis="
    probed = set(log.read_text(encoding="utf-8").split())
    assert probed == {":0", ":1"}
