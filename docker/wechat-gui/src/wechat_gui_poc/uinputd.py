"""uinput 控制面守护进程（容器启动期创建并持有虚拟输入设备）。

职责：
- 创建键盘 + 绝对指针内核设备（生命周期与进程一致）；
- 按 sysfs 实际事件节点生成 Xorg 静态配置并落盘；
- 在 Unix socket 上提供 JSON 行协议（注入按键 / 移动 / 点击 / 健康探测）——
  门脚本与后续通道实现都经此控制面注入，避免多进程重复创建设备；
- 把事件节点 chown 给非 root 运行用户（Xorg 以该用户运行）。

协议为单请求单响应 JSON 行，请求与响应模型见 ``wire_models``；控制面仅
容器内可见（socket 权限 0666，不含机密，只承载注入指令）。注入失败以
``ok=false`` 返回，不中断守护。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pwd
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .uinput_devices import (
    BTN_LEFT,
    UinputError,
    create,
)
from .wire_models import (
    DEFAULT_DEVICES_JSON,
    DEFAULT_HEIGHT,
    DEFAULT_SOCKET_PATH,
    DEFAULT_WIDTH,
    MAX_CONTROL_REQUEST_BYTES,
    ClickRequest,
    CommandOutcome,
    ControlCommand,
    ControlRequest,
    DeviceArtifacts,
    DevicePairInfo,
    KeyRequest,
    MoveRequest,
    PingDetails,
    PingRequest,
    ShutdownRequest,
)
from .xorg import build_xorg_config, write_xorg_config

#: Xorg 配置落盘默认路径（Xorg 默认加载 conf.d）。
DEFAULT_XORG_CONF = Path("/etc/X11/xorg.conf.d/99-wechat-gui.conf")

#: 模块级 logger（stdlib logging——本树最小依赖约定）。
logger = logging.getLogger(__name__)


class CommandError(ValueError):
    """控制命令载荷非法。"""


class InputEmitter(Protocol):
    """可注入事件的虚拟设备协议（生产实现与测试替身共同满足）。"""

    @property
    def pair_info(self) -> DevicePairInfo:
        """设备身份与节点信息。

        Returns:
            设备信息模型。
        """
        ...

    def emit_key(self, code: int, *, pressed: bool) -> None:
        """注入按键按下 / 抬起。

        Args:
            code: Linux 按键码。
            pressed: True 表示按下，False 表示抬起。

        Raises:
            UinputError: 设备已销毁或写入失败。
        """
        ...

    def emit_move(self, x: int, y: int) -> None:
        """注入绝对指针移动。

        Args:
            x: 目标 X 坐标（像素）。
            y: 目标 Y 坐标（像素）。

        Raises:
            UinputError: 设备已销毁或写入失败。
        """
        ...

    def emit_click(self, button: int = BTN_LEFT) -> None:
        """注入鼠标点击。

        Args:
            button: Linux 鼠标按键码。
        """
        ...

    def destroy(self) -> None:
        """销毁内核设备。"""
        ...


def artifacts_from_pair(pair: DevicePairInfo) -> DeviceArtifacts:
    """从设备信息构造落盘模型。

    Args:
        pair: 设备身份与节点信息。

    Returns:
        落盘模型。
    """
    return DeviceArtifacts(
        keyboard_name=pair.keyboard.name,
        keyboard_event=pair.keyboard.event_path,
        pointer_name=pair.pointer.name,
        pointer_event=pair.pointer.event_path,
        width=pair.width,
        height=pair.height,
    )


def load_artifacts(path: Path) -> DeviceArtifacts:
    """读取并校验设备信息文件。

    Args:
        path: 设备信息 JSON 路径。

    Returns:
        设备信息模型。

    Raises:
        ValueError: 文件缺失 / JSON 非法 / 字段缺失或类型不符。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取设备信息 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"设备信息不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("设备信息根节点必须是对象")
    keyboard = raw.get("keyboard")
    pointer = raw.get("pointer")
    if not isinstance(keyboard, dict) or not isinstance(pointer, dict):
        raise ValueError("设备信息缺少 keyboard / pointer 对象")
    try:
        artifacts = DeviceArtifacts(
            keyboard_name=_require_str(keyboard, "name"),
            keyboard_event=_require_str(keyboard, "event_path"),
            pointer_name=_require_str(pointer, "name"),
            pointer_event=_require_str(pointer, "event_path"),
            width=_require_int(raw, "width"),
            height=_require_int(raw, "height"),
        )
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    if artifacts.width <= 0 or artifacts.height <= 0:
        raise ValueError("设备信息中的屏幕尺寸必须为正数")
    return artifacts


def _require_str(payload: Mapping[str, object], key: str) -> str:
    """读取必填字符串字段。

    Args:
        payload: 载荷对象。
        key: 字段名。

    Returns:
        字段值。

    Raises:
        TypeError: 字段缺失或类型不符。
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"字段 {key} 必须是非空字符串")
    return value


def _require_int(payload: Mapping[str, object], key: str) -> int:
    """读取必填整数字段（拒绝 bool）。

    Args:
        payload: 载荷对象。
        key: 字段名。

    Returns:
        字段值。

    Raises:
        TypeError: 字段缺失或类型不符。
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"字段 {key} 必须是整数")
    return value


def decode_control_request(raw: bytes) -> ControlRequest:
    """把单行 JSON 请求解码为具体请求模型。

    Args:
        raw: 原始字节行（含换行符）。

    Returns:
        ``ping`` / ``key`` / ``move`` / ``click`` / ``shutdown`` 之一。

    Raises:
        CommandError: 超长 / 空行 / JSON 非法 / 根节点非对象 / 字段缺失或
            类型不符 / 未知命令。
    """
    if len(raw) > MAX_CONTROL_REQUEST_BYTES:
        raise CommandError("请求超过大小上限")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"请求不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise CommandError("请求根节点必须是对象")
    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise CommandError("缺少 command 字段")
    if command == ControlCommand.PING:
        return PingRequest()
    if command == ControlCommand.KEY:
        return KeyRequest(
            code=_int_value(payload, "code"),
            pressed=_bool_value(payload, "pressed"),
        )
    if command == ControlCommand.MOVE:
        return MoveRequest(x=_int_value(payload, "x"), y=_int_value(payload, "y"))
    if command == ControlCommand.CLICK:
        return ClickRequest(button=_int_value(payload, "button", default=BTN_LEFT))
    if command == ControlCommand.SHUTDOWN:
        return ShutdownRequest()
    raise CommandError(f"未知命令：{command}")


def _int_value(payload: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    """读取可选 / 必填整数字段（拒绝 bool）。

    Args:
        payload: 请求对象。
        key: 字段名。
        default: 缺省值；None 表示必填。

    Returns:
        字段值。

    Raises:
        CommandError: 字段缺失或类型不符。
    """
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandError(f"字段 {key} 必须是整数")
    return value


def _bool_value(payload: Mapping[str, object], key: str) -> bool:
    """读取必填布尔字段。

    Args:
        payload: 请求对象。
        key: 字段名。

    Returns:
        字段值。

    Raises:
        CommandError: 字段缺失或类型不符。
    """
    value = payload.get(key)
    if not isinstance(value, bool):
        raise CommandError(f"字段 {key} 必须是布尔值")
    return value


def dispatch_command(devices: InputEmitter, request: ControlRequest) -> CommandOutcome:
    """执行一条已解码的控制命令（异常归一为失败结果）。

    Args:
        devices: 虚拟输入设备。
        request: 已解码的请求模型。

    Returns:
        执行结果；注入失败返回 ``ok=False``，不抛出。
    """
    command = request.command.value
    try:
        if isinstance(request, PingRequest):
            info = devices.pair_info
            return CommandOutcome(
                command=command,
                ok=True,
                details=PingDetails(
                    keyboard=info.keyboard.name,
                    pointer=info.pointer.name,
                    width=info.width,
                    height=info.height,
                ),
            )
        if isinstance(request, KeyRequest):
            devices.emit_key(request.code, pressed=request.pressed)
            return CommandOutcome(command=command, ok=True)
        if isinstance(request, MoveRequest):
            devices.emit_move(request.x, request.y)
            return CommandOutcome(command=command, ok=True)
        if isinstance(request, ClickRequest):
            devices.emit_click(request.button)
            return CommandOutcome(command=command, ok=True)
        return CommandOutcome(command=command, ok=True)
    except UinputError as exc:
        return CommandOutcome(command=command, ok=False, error=str(exc))


def chown_event_nodes(pair: DevicePairInfo, username: str) -> None:
    """把两个事件节点交给运行用户（Xorg 以非 root 运行）。

    Args:
        pair: 设备信息。
        username: 目标用户名。

    Raises:
        UinputError: 用户不存在或 chown 失败。
    """
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise UinputError(f"运行用户不存在：{username}") from exc
    # 属主保留为守护进程用户（容器内 root），组为运行用户、0600→0660：
    # root 与 Xorg（wechat 组）都可打开；生产最小能力集不含 DAC_OVERRIDE。
    for info in (pair.keyboard, pair.pointer):
        try:
            os.chown(info.event_path, os.getuid(), account.pw_gid)
            os.chmod(info.event_path, 0o660)
        except OSError as exc:
            raise UinputError(f"无法调整 {info.event_path} 属主：{exc}") from exc


def write_runtime_artifacts(
    devices: InputEmitter,
    *,
    devices_json_path: Path,
    xorg_conf_path: Path,
    username: str | None,
) -> DeviceArtifacts:
    """落盘设备信息与 Xorg 配置（必要时交付事件节点属主）。

    Args:
        devices: 虚拟输入设备。
        devices_json_path: 设备信息 JSON 路径。
        xorg_conf_path: Xorg 配置路径。
        username: 事件节点目标用户；None 表示保持 root。

    Returns:
        已落盘的设备信息。

    Raises:
        UinputError: 属主调整失败。
    """
    artifacts = artifacts_from_pair(devices.pair_info)
    devices_json_path.parent.mkdir(parents=True, exist_ok=True)
    devices_json_path.write_text(
        json.dumps(artifacts.to_wire(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_xorg_config(
        xorg_conf_path,
        config_text=build_xorg_config(
            keyboard_event=artifacts.keyboard_event,
            pointer_event=artifacts.pointer_event,
            width=artifacts.width,
            height=artifacts.height,
        ),
    )
    if username is not None:
        chown_event_nodes(devices.pair_info, username)
    return artifacts


async def serve(
    devices: InputEmitter,
    *,
    socket_path: Path,
    devices_json_path: Path,
    xorg_conf_path: Path,
    username: str | None,
    stop_event: asyncio.Event,
) -> None:
    """落盘运行期制品并在 Unix socket 上服务控制命令。

    Args:
        devices: 已创建并持有的虚拟输入设备。
        socket_path: 控制面 socket 路径。
        devices_json_path: 设备信息落盘路径。
        xorg_conf_path: Xorg 配置落盘路径。
        username: 事件节点目标用户；None 表示保持 root。
        stop_event: 置位后关闭服务并销毁设备。
    """
    write_runtime_artifacts(
        devices,
        devices_json_path=devices_json_path,
        xorg_conf_path=xorg_conf_path,
        username=username,
    )
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            try:
                request = decode_control_request(raw)
            except CommandError as exc:
                outcome = CommandOutcome(command="", ok=False, error=str(exc))
            else:
                outcome = dispatch_command(devices, request)
                if isinstance(request, ShutdownRequest) and outcome.ok:
                    stop_event.set()
            writer.write(
                (json.dumps(outcome.to_wire(), ensure_ascii=False) + "\n").encode("utf-8")
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    os.chmod(socket_path, 0o666)
    logger.info("控制面就绪：%s", socket_path)
    try:
        await stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        devices.destroy()


@dataclass(frozen=True)
class UinputControlClient:
    """控制面客户端（门脚本与后续通道共用）。

    Attributes:
        socket_path: 控制面 socket 路径。
        timeout: 单次请求超时（秒）。
    """

    socket_path: Path
    timeout: float = 5.0

    async def send(self, request: ControlRequest) -> CommandOutcome:
        """发送一条控制请求并解析响应。

        Args:
            request: 已构造的请求模型。

        Returns:
            控制面响应。

        Raises:
            UinputError: 连接 / 超时 / 响应非法。
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), self.timeout
            )
        except (OSError, TimeoutError) as exc:
            raise UinputError(f"无法连接控制面 {self.socket_path}：{exc}") from exc
        try:
            writer.write(
                (json.dumps(request.to_wire(), ensure_ascii=False) + "\n").encode("utf-8")
            )
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), self.timeout)
            except TimeoutError as exc:
                raise UinputError("控制面响应超时") from exc
        finally:
            writer.close()
            await writer.wait_closed()
        return decode_command_outcome(line)

    async def ping(self) -> CommandOutcome:
        """健康探测。

        Returns:
            控制面响应。
        """
        return await self.send(PingRequest())

    async def emit_key(self, code: int, *, pressed: bool) -> CommandOutcome:
        """注入按键按下 / 抬起。

        Args:
            code: Linux 按键码。
            pressed: True 表示按下。

        Returns:
            控制面响应。
        """
        return await self.send(KeyRequest(code=code, pressed=pressed))

    async def emit_move(self, x: int, y: int) -> CommandOutcome:
        """注入绝对指针移动。

        Args:
            x: X 坐标。
            y: Y 坐标。

        Returns:
            控制面响应。
        """
        return await self.send(MoveRequest(x=x, y=y))

    async def emit_click(self, button: int = BTN_LEFT) -> CommandOutcome:
        """注入鼠标点击。

        Args:
            button: Linux 鼠标按键码。

        Returns:
            控制面响应。
        """
        return await self.send(ClickRequest(button=button))

    async def shutdown(self) -> CommandOutcome:
        """请求守护进程收口。

        Returns:
            控制面响应。
        """
        return await self.send(ShutdownRequest())


def decode_command_outcome(line: bytes) -> CommandOutcome:
    """解码控制面单行响应（控制面客户端唯一解析面）。

    Args:
        line: 原始响应字节行（含换行符）。

    Returns:
        控制面响应模型。

    Raises:
        UinputError: 连接提前关闭 / JSON 非法 / 根节点非对象 / 字段缺失或
            类型不符。
    """
    if not line:
        raise UinputError("控制面连接提前关闭")
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UinputError(f"控制面响应不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise UinputError("控制面响应根节点必须是对象")
    ok = raw.get("ok")
    command = raw.get("command")
    error = raw.get("error")
    if not isinstance(ok, bool) or not isinstance(command, str):
        raise UinputError("控制面响应缺少 ok / command 字段")
    if error is not None and not isinstance(error, str):
        raise UinputError("控制面响应 error 字段类型非法")
    return CommandOutcome(
        command=command,
        ok=ok,
        error=error,
        details=_decode_ping_details(raw),
    )


def _decode_ping_details(payload: Mapping[str, object]) -> PingDetails | None:
    """从响应载荷中还原健康探测附加字段。

    Args:
        payload: 控制面响应对象。

    Returns:
        附加字段模型；响应不含任何探测字段时为 None。

    Raises:
        UinputError: 探测字段只出现一部分或类型不符。
    """
    keys = ("keyboard", "pointer", "width", "height")
    present = [key for key in keys if key in payload]
    if not present:
        return None
    if len(present) != len(keys):
        raise UinputError("控制面响应 ping 字段不完整")
    keyboard = payload["keyboard"]
    pointer = payload["pointer"]
    width = payload["width"]
    height = payload["height"]
    if not isinstance(keyboard, str) or not isinstance(pointer, str):
        raise UinputError("控制面响应 ping 设备名类型非法")
    if isinstance(width, bool) or not isinstance(width, int):
        raise UinputError("控制面响应 ping 尺寸类型非法")
    if isinstance(height, bool) or not isinstance(height, int):
        raise UinputError("控制面响应 ping 尺寸类型非法")
    return PingDetails(keyboard=keyboard, pointer=pointer, width=width, height=height)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        已配置的解析器。
    """
    parser = argparse.ArgumentParser(description="GUI 微信沙盒 uinput 控制面守护进程")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--devices-json", type=Path, default=DEFAULT_DEVICES_JSON)
    parser.add_argument("--xorg-conf", type=Path, default=DEFAULT_XORG_CONF)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--username", default=None, help="事件节点目标运行用户")
    parser.add_argument("--device", type=Path, default=Path("/dev/uinput"))
    parser.add_argument("--sysfs-root", type=Path, default=Path("/sys/class/input"))
    parser.add_argument("--event-root", type=Path, default=Path("/dev/input"))
    parser.add_argument("--discovery-timeout", type=float, default=5.0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    """创建设备并运行控制面直至收到停止信号。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码（0 正常收口）。
    """
    devices = create(
        width=args.width,
        height=args.height,
        device_path=args.device,
        sysfs_root=args.sysfs_root,
        event_root=args.event_root,
        discovery_timeout_seconds=args.discovery_timeout,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # 非主线程的事件循环无法注册信号处理器（测试注入场景）；
        # 生产入口在主线程运行，不触发该抑制。
        with contextlib.suppress(RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)
    await serve(
        devices,
        socket_path=args.socket,
        devices_json_path=args.devices_json,
        xorg_conf_path=args.xorg_conf,
        username=args.username,
        stop_event=stop_event,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表（None 时为进程参数）。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
