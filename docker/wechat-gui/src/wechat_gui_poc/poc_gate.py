"""Xorg + uinput 栈 PoC 门脚本（容器内运行）。

门禁步骤（顺序固定，失败即 FAIL 并跳过后续步骤）：

1. ``x_up``：``xdpyinfo`` 连接 ``:99``，断言 1280x800x24；
2. ``uinput_create``：设备信息落盘可读、两个事件节点可打开、控制面存活；
3. ``xinput_visible``：``xinput list`` 含两个已附加（非漂浮）虚拟设备；
4. ``event_roundtrip``：``xev -root`` 行缓冲采集，一次性注入 KEY_A 按下 /
   抬起、绝对指针移动与左键按下 / 抬起，按截止轮询解析输出，断言
   KeyPress / KeyRelease / ButtonPress / ButtonRelease / Motion 各 ≥1；
5. ``screenshot``：``import -window root png:-`` 取图，Pillow 校验尺寸与非
   全黑 / 非全同色；
6. ``clipboard``：``xclip`` 写入后读回一致；
7. ``vnc_up``：5900 可连接、``http://127.0.0.1:6080/vnc.html`` 返回 200。

输出单行 JSON 摘要（stdout 与 ``--out`` 同步落盘），退出码 0=PASS /
1=FAIL / 2=用法错误。每步耗时用 ``perf_counter`` 毫秒记录；等待一律带
截止（无固定长睡眠）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import platform
import re
import shutil
import socket
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from PIL import Image

from .uinput_devices import KEY_A, UinputError
from .uinputd import UinputControlClient, load_artifacts
from .wire_models import (
    DEFAULT_DEVICES_JSON,
    DEFAULT_DISPLAY,
    DEFAULT_EVENT_TIMEOUT,
    DEFAULT_HEIGHT,
    DEFAULT_NOVNC_PORT,
    DEFAULT_OPERATION_TIMEOUT,
    DEFAULT_READY_TIMEOUT,
    DEFAULT_SOCKET_PATH,
    DEFAULT_VNC_PORT,
    DEFAULT_WIDTH,
    EXPECTED_DEPTH,
    CommandOutput,
    GateContext,
    GateReport,
    ScreenInfo,
    ScreenshotStats,
    StepRecord,
    StepSpec,
    StepStatus,
    XevCounts,
    XInputDevice,
)

_READY_POLL_SECONDS = 0.05
_EVENT_READ_SLICE_SECONDS = 0.25


class StepFailure(RuntimeError):
    """门禁步骤失败（携带可读原因）。"""


def _display_env(display: str) -> dict[str, str]:
    """构造带 ``DISPLAY`` 的子进程环境。

    Args:
        display: X 显示号。

    Returns:
        环境变量映射（继承当前进程环境）。
    """
    env = dict(os.environ)
    env["DISPLAY"] = display
    return env


async def run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    capture_output: bool = True,
) -> CommandOutput:
    """执行外部命令并收集原始输出（带超时强杀）。

    ``capture_output=False`` 时标准输出 / 错误接到 ``/dev/null``——用于
    会 fork 出常驻子进程的命令（如 xclip 的 selection owner）：常驻子进程
    继承管道会让 ``communicate`` 直到其退出才返回，接 ``/dev/null`` 后父
    进程退出即返回。

    Args:
        argv: 命令与参数列表。
        timeout: 超时秒数。
        env: 子进程环境；None 表示继承。
        input_bytes: 标准输入字节；None 表示关闭标准输入。
        capture_output: 是否采集标准输出 / 错误。

    Returns:
        命令执行结果（未采集时 stdout / stderr 为空字节）。

    Raises:
        StepFailure: 启动失败或执行超时。
    """
    stdout_target = (
        asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    )
    stderr_target = (
        asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=stderr_target,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise StepFailure(f"无法启动命令 {argv[0]}：{exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_bytes), timeout=timeout
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise StepFailure(f"命令超时（{timeout:.1f}s）：{argv[0]}") from exc
    returncode = process.returncode if process.returncode is not None else -1
    return CommandOutput(
        returncode=returncode,
        stdout=stdout if stdout is not None else b"",
        stderr=stderr if stderr is not None else b"",
    )


def _require_success(output: CommandOutput, step: str) -> None:
    """断言命令退出码为 0。

    Args:
        output: 命令执行结果。
        step: 步骤名（用于失败文案）。

    Raises:
        StepFailure: 退出码非 0。
    """
    if output.returncode != 0:
        tail = output.stderr_text.strip()[-400:] or output.stdout_text.strip()[-400:]
        raise StepFailure(f"{step} 命令失败（退出码 {output.returncode}）：{tail}")


def parse_xdpyinfo(text: str) -> ScreenInfo:
    """解析 ``xdpyinfo`` 输出中的分辨率与根窗口色深。

    Args:
        text: ``xdpyinfo`` 标准输出。

    Returns:
        屏幕属性。

    Raises:
        ValueError: 关键字段缺失。
    """
    dimensions = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", text)
    depth = re.search(r"depth of root window:\s+(\d+) planes", text)
    if dimensions is None or depth is None:
        raise ValueError("xdpyinfo 输出缺少 dimensions / root depth 字段")
    return ScreenInfo(
        width=int(dimensions.group(1)),
        height=int(dimensions.group(2)),
        depth=int(depth.group(1)),
    )


def parse_xinput_list(text: str) -> tuple[XInputDevice, ...]:
    """解析 ``xinput list`` 输出。

    Args:
        text: ``xinput list`` 标准输出。

    Returns:
        设备列表（保持输出顺序）。
    """
    devices: list[XInputDevice] = []
    for line in text.splitlines():
        match = re.search(r"id=(\d+)\s+\[([^\]]+)\]", line)
        if match is None:
            continue
        prefix = line[: match.start()].strip()
        name = prefix.lstrip("⎡⎜⎣↳∼~┃|+-─ ").strip()
        if not name:
            continue
        classes = match.group(2).lower()
        devices.append(
            XInputDevice(
                name=name,
                device_id=int(match.group(1)),
                floating="floating" in classes,
            )
        )
    return tuple(devices)


def parse_xev_output(text: str) -> XevCounts:
    """统计 ``xev`` 输出中的关键事件数。

    Args:
        text: ``xev`` 输出。

    Returns:
        事件计数。
    """
    return XevCounts(
        key_press=len(re.findall(r"^KeyPress event", text, flags=re.MULTILINE)),
        key_release=len(re.findall(r"^KeyRelease event", text, flags=re.MULTILINE)),
        button_press=len(re.findall(r"^ButtonPress event", text, flags=re.MULTILINE)),
        button_release=len(re.findall(r"^ButtonRelease event", text, flags=re.MULTILINE)),
        motion=len(re.findall(r"^MotionNotify event", text, flags=re.MULTILINE)),
    )


def validate_screenshot(png_bytes: bytes, *, width: int, height: int) -> ScreenshotStats:
    """校验截图字节（PNG、尺寸、非全黑、非全同色）。

    Args:
        png_bytes: ``import`` 输出的 PNG 字节。
        width: 期望宽度。
        height: 期望高度。

    Returns:
        校验统计。

    Raises:
        ValueError: 非 PNG / 尺寸不符 / 全黑 / 全同色。
    """
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("截图输出不是 PNG")
    try:
        image = Image.open(io.BytesIO(png_bytes))
        image.load()
    except OSError as exc:
        raise ValueError(f"截图无法解码：{exc}") from exc
    if image.size != (width, height):
        raise ValueError(f"截图尺寸 {image.size} 与期望 {(width, height)} 不符")
    extrema = cast(tuple[tuple[int, int], ...], image.convert("RGB").getextrema())
    channel_min = min(low for low, _high in extrema)
    channel_max = max(high for _low, high in extrema)
    if channel_max == 0:
        raise ValueError("截图全黑")
    if all(low == high for low, high in extrema):
        raise ValueError("截图全同色")
    return ScreenshotStats(
        width=image.size[0],
        height=image.size[1],
        channel_min=channel_min,
        channel_max=channel_max,
    )


async def wait_for_tcp(host: str, port: int, *, timeout: float) -> None:
    """等待 TCP 端口可连接（带截止）。

    Args:
        host: 主机地址。
        port: 端口。
        timeout: 截止秒数。

    Raises:
        StepFailure: 截止内不可连接。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            if time.monotonic() >= deadline:
                raise StepFailure(f"TCP {host}:{port} 在 {timeout:.1f}s 内不可连接") from None
            await asyncio.sleep(_READY_POLL_SECONDS)
            continue
        writer.close()
        await writer.wait_closed()
        return


async def http_status(host: str, port: int, path: str, *, timeout: float) -> int:
    """发起一次最小 HTTP GET 并返回状态码。

    Args:
        host: 主机地址。
        port: 端口。
        path: 请求路径。
        timeout: 超时秒数。

    Returns:
        HTTP 状态码。

    Raises:
        StepFailure: 连接 / 响应失败或状态行非法。
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise StepFailure(f"HTTP {host}:{port}{path} 连接失败：{exc}") from exc
    try:
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
        )
        writer.write(request.encode("utf-8"))
        await writer.drain()
        try:
            status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except TimeoutError as exc:
            raise StepFailure(f"HTTP {host}:{port}{path} 响应超时") from exc
    finally:
        writer.close()
        await writer.wait_closed()
    match = re.match(rb"HTTP/\d\.\d (\d{3})", status_line)
    if match is None:
        raise StepFailure(f"HTTP 状态行非法：{status_line[:120]!r}")
    return int(match.group(1))


def _xev_connected(pid: int, *, proc_root: Path = Path("/proc")) -> bool:
    """判断 ``xev`` 是否已连上 X 服务器（或已退出）。

    注入早于 ``xev`` 完成 SelectInput 时事件会全部丢失；``xev -root`` 不
    创建窗口，故 ``xlsclients`` 看不到它。连接 X 服务器后进程会持有一个
    unix socket 文件描述符，据此判定客户端已就绪；进程退出（fd 表为空或
    不可读）同样视为可继续——后续读取路径会给出真实失败原因。

    Args:
        pid: ``xev`` 进程号。
        proc_root: ``proc`` 文件系统根（测试可指向临时目录）。

    Returns:
        True 表示可以注入。
    """
    fd_dir = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return True
    if not entries:
        return True
    for entry in entries:
        try:
            if os.readlink(entry).startswith("socket:"):
                return True
        except OSError:
            continue
    return False


async def _wait_for_xev_connected(pid: int, *, timeout: float) -> None:
    """在截止内等待 ``xev`` 连上 X 服务器。

    Args:
        pid: ``xev`` 进程号。
        timeout: 截止秒数。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _xev_connected(pid):
            return
        await asyncio.sleep(_READY_POLL_SECONDS)


def _roundtrip_complete(counts: XevCounts) -> bool:
    """判断注入的回环事件是否全部采集到。

    Args:
        counts: 已解析的事件计数。

    Returns:
        True 表示五个断言面均 ≥1。
    """
    return (
        counts.key_press >= 1
        and counts.key_release >= 1
        and counts.button_press >= 1
        and counts.button_release >= 1
        and counts.motion >= 1
    )


async def _collect_xev(
    *,
    ctx: GateContext,
    client: UinputControlClient,
) -> XevCounts:
    """启动 ``xev``、单轮注入事件并按截止解析输出。

    Args:
        ctx: 门禁参数。
        client: 控制面客户端。

    Returns:
        事件计数。

    Raises:
        StepFailure: ``xev`` 无法启动 / 注入失败 / 截止内事件不完整。
    """
    env = _display_env(ctx.display)
    try:
        # stdbuf -oL：xev 默认全缓冲，事件成行后必须立即可读。
        process = await asyncio.create_subprocess_exec(
            "stdbuf",
            "-oL",
            "xev",
            "-root",
            "-display",
            ctx.display,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise StepFailure(f"无法启动 xev：{exc}") from exc
    assert process.stdout is not None
    reader = process.stdout
    buffer: list[str] = []
    deadline = time.monotonic() + ctx.event_timeout
    try:
        await _wait_for_xev_connected(process.pid, timeout=ctx.event_timeout)
        outcomes = [
            await client.emit_key(KEY_A, pressed=True),
            await client.emit_key(KEY_A, pressed=False),
            await client.emit_move(ctx.width // 2, ctx.height // 2),
            await client.emit_click(),
        ]
        failed = [outcome for outcome in outcomes if not outcome.ok]
        if failed:
            raise StepFailure(f"控制面注入失败：{failed[0].error}")
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=min(_EVENT_READ_SLICE_SECONDS, max(deadline - time.monotonic(), 0.01)),
                )
            except TimeoutError:
                continue
            if not line:
                break
            buffer.append(line.decode("utf-8", errors="replace"))
            counts = parse_xev_output("".join(buffer))
            if _roundtrip_complete(counts):
                return counts
        counts = parse_xev_output("".join(buffer))
        raise StepFailure(
            "事件回环不完整："
            f"KeyPress={counts.key_press} KeyRelease={counts.key_release} "
            f"ButtonPress={counts.button_press} ButtonRelease={counts.button_release} "
            f"Motion={counts.motion}"
        )
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()


async def step_x_up(ctx: GateContext) -> str:
    """断言 X 服务在线且分辨率 / 色深符合配置。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 连接失败或属性不符。
    """
    output = await run_command(
        ["xdpyinfo", "-display", ctx.display],
        timeout=ctx.operation_timeout,
        env=_display_env(ctx.display),
    )
    _require_success(output, "xdpyinfo")
    try:
        info = parse_xdpyinfo(output.stdout_text)
    except ValueError as exc:
        raise StepFailure(str(exc)) from exc
    if (info.width, info.height) != (ctx.width, ctx.height):
        raise StepFailure(
            f"分辨率 {info.width}x{info.height} 与期望 {ctx.width}x{ctx.height} 不符"
        )
    if info.depth != EXPECTED_DEPTH:
        raise StepFailure(f"根窗口色深 {info.depth} 与期望 {EXPECTED_DEPTH} 不符")
    return f"{info.width}x{info.height}x{info.depth}"


async def step_uinput_create(ctx: GateContext) -> str:
    """断言虚拟设备已创建、事件节点可打开、控制面存活。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 设备信息 / 节点 / 控制面异常。
    """
    try:
        artifacts = load_artifacts(ctx.devices_json)
    except ValueError as exc:
        raise StepFailure(str(exc)) from exc
    for device in (artifacts.keyboard_event, artifacts.pointer_event):
        path = Path(device)
        if not path.exists():
            raise StepFailure(f"事件节点不存在：{path}")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(descriptor)
        except OSError as exc:
            raise StepFailure(f"事件节点不可打开 {path}：{exc}") from exc
    client = UinputControlClient(ctx.control_socket, timeout=ctx.operation_timeout)
    try:
        outcome = await client.ping()
    except UinputError as exc:
        raise StepFailure(str(exc)) from exc
    if not outcome.ok:
        raise StepFailure(f"控制面 ping 失败：{outcome.error}")
    return (
        f"{artifacts.keyboard_name}@{artifacts.keyboard_event}, "
        f"{artifacts.pointer_name}@{artifacts.pointer_event}"
    )


async def step_xinput_visible(ctx: GateContext) -> str:
    """断言两个虚拟设备出现在 XInput 且非漂浮。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 设备缺失或漂浮。
    """
    try:
        artifacts = load_artifacts(ctx.devices_json)
    except ValueError as exc:
        raise StepFailure(str(exc)) from exc
    output = await run_command(
        ["xinput", "list"], timeout=ctx.operation_timeout, env=_display_env(ctx.display)
    )
    _require_success(output, "xinput")
    devices = parse_xinput_list(output.stdout_text)
    for expected in (artifacts.keyboard_name, artifacts.pointer_name):
        matched = next((device for device in devices if device.name == expected), None)
        if matched is None:
            raise StepFailure(f"xinput 未列出设备：{expected}")
        if matched.floating:
            raise StepFailure(f"设备处于漂浮状态：{expected}")
    return f"keyboard+pointer attached（{len(devices)} 个输入设备）"


async def step_event_roundtrip(ctx: GateContext) -> str:
    """断言键盘 / 指针事件可经 uinput 注入并由 X 采集。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 事件回环失败。
    """
    client = UinputControlClient(ctx.control_socket, timeout=ctx.operation_timeout)
    counts = await _collect_xev(ctx=ctx, client=client)
    return (
        f"KeyPress={counts.key_press} KeyRelease={counts.key_release} "
        f"ButtonPress={counts.button_press} ButtonRelease={counts.button_release} "
        f"Motion={counts.motion}（单轮注入）"
    )


async def step_screenshot(ctx: GateContext) -> str:
    """抓取根窗口并校验图像真实性。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 抓图失败或图像校验失败。
    """
    output = await run_command(
        ["import", "-display", ctx.display, "-window", "root", "png:-"],
        timeout=ctx.operation_timeout,
        env=_display_env(ctx.display),
    )
    _require_success(output, "import")
    try:
        stats = validate_screenshot(output.stdout, width=ctx.width, height=ctx.height)
    except ValueError as exc:
        raise StepFailure(str(exc)) from exc
    return (
        f"{stats.width}x{stats.height} 非全黑非全同色"
        f"（通道范围 {stats.channel_min}-{stats.channel_max}）"
    )


async def step_clipboard(ctx: GateContext) -> str:
    """断言 X 剪贴板写入后可读回一致。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 写入 / 读回失败或内容不一致。
    """
    env = _display_env(ctx.display)
    token = "wechat-gui-poc-clipboard"
    write = await run_command(
        ["xclip", "-selection", "clipboard", "-in"],
        timeout=ctx.operation_timeout,
        env=env,
        input_bytes=token.encode("utf-8"),
        capture_output=False,
    )
    _require_success(write, "xclip -in")
    deadline = time.monotonic() + ctx.ready_timeout
    last_value = ""
    while time.monotonic() < deadline:
        read = await run_command(
            ["xclip", "-selection", "clipboard", "-o"],
            timeout=ctx.operation_timeout,
            env=env,
        )
        if read.returncode == 0:
            last_value = read.stdout_text
            if last_value == token:
                return f"剪贴板回读一致（{len(token)} 字符）"
        await asyncio.sleep(_READY_POLL_SECONDS)
    raise StepFailure(f"剪贴板回读不一致：{last_value!r}")


async def step_vnc_up(ctx: GateContext) -> str:
    """断言 x11vnc 与 noVNC 均就绪。

    Args:
        ctx: 门禁参数。

    Returns:
        成功摘要。

    Raises:
        StepFailure: 端口 / HTTP 探测失败。
    """
    await wait_for_tcp("127.0.0.1", ctx.vnc_port, timeout=ctx.ready_timeout)
    status = await http_status(
        "127.0.0.1", ctx.novnc_port, "/vnc.html", timeout=ctx.operation_timeout
    )
    if status != 200:
        raise StepFailure(f"noVNC /vnc.html 返回 {status}")
    return f"VNC {ctx.vnc_port} + noVNC {ctx.novnc_port} 就绪"


def build_steps() -> tuple[StepSpec, ...]:
    """构造固定顺序的门禁步骤。

    Returns:
        步骤规格元组。
    """
    return (
        StepSpec("x_up", step_x_up),
        StepSpec("uinput_create", step_uinput_create),
        StepSpec("xinput_visible", step_xinput_visible),
        StepSpec("event_roundtrip", step_event_roundtrip),
        StepSpec("screenshot", step_screenshot),
        StepSpec("clipboard", step_clipboard),
        StepSpec("vnc_up", step_vnc_up),
    )


async def run_steps(
    steps: Sequence[StepSpec],
    ctx: GateContext,
) -> tuple[StepRecord, ...]:
    """按顺序执行步骤（首个失败后其余标记跳过）。

    Args:
        steps: 步骤规格。
        ctx: 门禁参数。

    Returns:
        步骤记录元组。
    """
    records: list[StepRecord] = []
    failed = False
    for spec in steps:
        if failed:
            records.append(
                StepRecord(
                    name=spec.name,
                    status=StepStatus.SKIPPED,
                    duration_perf_counter_ms=0,
                    detail="前置步骤失败，跳过",
                )
            )
            continue
        started = time.perf_counter()
        try:
            detail = await spec.runner(ctx)
        except StepFailure as exc:
            failed = True
            records.append(
                StepRecord(
                    name=spec.name,
                    status=StepStatus.FAILED,
                    duration_perf_counter_ms=_elapsed_ms(started),
                    detail=str(exc),
                )
            )
        except Exception as exc:  # 门禁必须把任意异常归一为步骤失败
            failed = True
            records.append(
                StepRecord(
                    name=spec.name,
                    status=StepStatus.FAILED,
                    duration_perf_counter_ms=_elapsed_ms(started),
                    detail=f"未预期异常：{type(exc).__name__}: {exc}",
                )
            )
        else:
            records.append(
                StepRecord(
                    name=spec.name,
                    status=StepStatus.PASSED,
                    duration_perf_counter_ms=_elapsed_ms(started),
                    detail=detail,
                )
            )
    return tuple(records)


def _elapsed_ms(started: float) -> int:
    """计算自 ``started`` 起的毫秒耗时。

    Args:
        started: ``perf_counter`` 起点。

    Returns:
        毫秒数（非负整数）。
    """
    return max(int((time.perf_counter() - started) * 1000), 0)


async def collect_environment() -> tuple[str, str, str | None]:
    """收集主机名 / 内核 / Docker 版本（不可得则 None）。

    Returns:
        (hostname, kernel, docker)。
    """
    docker: str | None = None
    if shutil.which("docker") is not None:
        try:
            output = await run_command(["docker", "--version"], timeout=2.0)
        except StepFailure:
            docker = None
        else:
            if output.returncode == 0:
                docker = output.stdout_text.strip() or None
    return socket.gethostname(), platform.release(), docker


def build_report(
    records: Sequence[StepRecord],
    *,
    hostname: str,
    kernel: str,
    docker: str | None,
    started_at_ms: int,
    duration_perf_counter_ms: int,
) -> GateReport:
    """汇总步骤记录为门禁报告。

    Args:
        records: 步骤记录。
        hostname: 主机名。
        kernel: 内核版本。
        docker: Docker 版本。
        started_at_ms: 启动时刻（Unix Epoch 毫秒）。
        duration_perf_counter_ms: 总耗时毫秒。

    Returns:
        门禁报告（任一步非 passed 即 FAIL）。
    """
    verdict = (
        "PASS"
        if records and all(record.status is StepStatus.PASSED for record in records)
        else "FAIL"
    )
    return GateReport(
        verdict=verdict,
        steps=tuple(records),
        hostname=hostname,
        kernel=kernel,
        docker=docker,
        started_at_ms=started_at_ms,
        duration_perf_counter_ms=duration_perf_counter_ms,
    )


async def run_gate(ctx: GateContext) -> GateReport:
    """执行完整门禁并生成报告。

    Args:
        ctx: 门禁参数。

    Returns:
        门禁报告。
    """
    started_at_ms = time.time_ns() // 1_000_000
    started = time.perf_counter()
    records = await run_steps(build_steps(), ctx)
    hostname, kernel, docker = await collect_environment()
    return build_report(
        records,
        hostname=hostname,
        kernel=kernel,
        docker=docker,
        started_at_ms=started_at_ms,
        duration_perf_counter_ms=_elapsed_ms(started),
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        已配置解析器。
    """
    parser = argparse.ArgumentParser(description="GUI 微信沙盒 Xorg + uinput 栈 PoC 门")
    parser.add_argument("--out", type=Path, default=None, help="JSON 摘要落盘路径")
    parser.add_argument("--display", default=DEFAULT_DISPLAY)
    parser.add_argument("--devices-json", type=Path, default=DEFAULT_DEVICES_JSON)
    parser.add_argument("--control-socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--vnc-port", type=int, default=DEFAULT_VNC_PORT)
    parser.add_argument("--novnc-port", type=int, default=DEFAULT_NOVNC_PORT)
    parser.add_argument("--operation-timeout", type=float, default=DEFAULT_OPERATION_TIMEOUT)
    parser.add_argument("--event-timeout", type=float, default=DEFAULT_EVENT_TIMEOUT)
    parser.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表（None 时为进程参数）。

    Returns:
        退出码：0=PASS，1=FAIL，2=用法错误（argparse 自身行为）。
    """
    args = build_parser().parse_args(argv)
    ctx = GateContext(
        display=args.display,
        devices_json=args.devices_json,
        control_socket=args.control_socket,
        width=args.width,
        height=args.height,
        vnc_port=args.vnc_port,
        novnc_port=args.novnc_port,
        operation_timeout=args.operation_timeout,
        event_timeout=args.event_timeout,
        ready_timeout=args.ready_timeout,
    )
    report = asyncio.run(run_gate(ctx))
    line = json.dumps(report.to_wire(), ensure_ascii=False, separators=(",", ":"))
    print(line, flush=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(line + "\n", encoding="utf-8")
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
