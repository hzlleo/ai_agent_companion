"""GUI 微信沙盒的数据模型与 wire 契约（本树自持，与逻辑代码分离）。

本模块集中三类载荷：

- **控制面协议**：Unix socket 上的单请求单响应 JSON 行——``ping`` /
  ``key`` / ``move`` / ``click`` / ``shutdown`` 各自的请求模型与响应模型
  （含健康探测的附加字段）；
- **设备制品**：uinputd 落盘的 ``uinput-devices.json`` 模型；
- **门禁载荷**：外部命令输出、X 属性解析结果与门禁报告的模型。

模型只描述字段与序列化出口（``to_wire()``）；协议解码、设备注入与步骤
编排在 ``uinputd`` / ``uinput_devices`` / ``poc_gate`` 中，二者分离。
容器内控制面仅本机可见：socket 权限 0666，不承载机密，只承载注入指令。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

__all__ = [
    "ClickRequest",
    "CommandOutcome",
    "CommandOutput",
    "ControlCommand",
    "ControlRequest",
    "DeviceArtifacts",
    "DevicePairInfo",
    "GateContext",
    "GateReport",
    "KeyRequest",
    "MoveRequest",
    "PingDetails",
    "PingRequest",
    "ScreenInfo",
    "ScreenshotStats",
    "ShutdownRequest",
    "StepRecord",
    "StepSpec",
    "StepStatus",
    "VirtualDeviceInfo",
    "XInputDevice",
    "XevCounts",
    "DEFAULT_DEVICES_JSON",
    "DEFAULT_DISPLAY",
    "DEFAULT_EVENT_TIMEOUT",
    "DEFAULT_HEIGHT",
    "DEFAULT_NOVNC_PORT",
    "DEFAULT_OPERATION_TIMEOUT",
    "DEFAULT_READY_TIMEOUT",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_VNC_PORT",
    "DEFAULT_WIDTH",
    "EXPECTED_DEPTH",
    "MAX_CONTROL_REQUEST_BYTES",
]

# ─────────────────────────────────────────────────────────────────────────────
# 容器内固定路径与门禁缺省参数
# ─────────────────────────────────────────────────────────────────────────────

#: 控制面默认 socket 路径（容器内运行目录）。
DEFAULT_SOCKET_PATH = Path("/run/wechat-gui/control.sock")
#: 设备信息落盘默认路径。
DEFAULT_DEVICES_JSON = Path("/run/wechat-gui/uinput-devices.json")
#: 容器的固定显示号。
DEFAULT_DISPLAY = ":99"
#: 屏幕宽度（与 Xorg 配置一致）。
DEFAULT_WIDTH = 1280
#: 屏幕高度（与 Xorg 配置一致）。
DEFAULT_HEIGHT = 800
#: 期望色深。
EXPECTED_DEPTH = 24
#: x11vnc 端口。
DEFAULT_VNC_PORT = 5900
#: noVNC / websockify 端口。
DEFAULT_NOVNC_PORT = 6080
#: 单次外部命令超时（秒）。
DEFAULT_OPERATION_TIMEOUT = 30.0
#: 事件回环整体超时（秒）。
DEFAULT_EVENT_TIMEOUT = 10.0
#: 服务就绪探测超时（秒）。
DEFAULT_READY_TIMEOUT = 10.0

#: 单条控制请求读取上限（字节；协议消息很小，超限即视为客户端异常）。
MAX_CONTROL_REQUEST_BYTES = 8192


# ─────────────────────────────────────────────────────────────────────────────
# 控制面协议：请求
# ─────────────────────────────────────────────────────────────────────────────


class ControlCommand(StrEnum):
    """控制面命令名（JSON 行的 ``command`` 字段取值）。"""

    PING = "ping"
    KEY = "key"
    MOVE = "move"
    CLICK = "click"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class PingRequest:
    """健康探测请求（返回设备摘要与屏幕尺寸）。

    Attributes:
        command: 协议命令名（固定，不参与构造）。
    """

    command: ControlCommand = field(default=ControlCommand.PING, init=False)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            请求对象。
        """
        return {"command": self.command.value}


@dataclass(frozen=True)
class KeyRequest:
    """按键注入请求。

    Attributes:
        command: 协议命令名（固定，不参与构造）。
        code: Linux 按键码。
        pressed: True 表示按下，False 表示抬起。
    """

    code: int
    pressed: bool
    command: ControlCommand = field(default=ControlCommand.KEY, init=False)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            请求对象。
        """
        return {"command": self.command.value, "code": self.code, "pressed": self.pressed}


@dataclass(frozen=True)
class MoveRequest:
    """绝对指针移动请求。

    Attributes:
        command: 协议命令名（固定，不参与构造）。
        x: X 坐标（像素）。
        y: Y 坐标（像素）。
    """

    x: int
    y: int
    command: ControlCommand = field(default=ControlCommand.MOVE, init=False)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            请求对象。
        """
        return {"command": self.command.value, "x": self.x, "y": self.y}


@dataclass(frozen=True)
class ClickRequest:
    """鼠标点击请求（一次完整按下 + 抬起）。

    Attributes:
        command: 协议命令名（固定，不参与构造）。
        button: Linux 鼠标按键码。
    """

    button: int
    command: ControlCommand = field(default=ControlCommand.CLICK, init=False)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            请求对象。
        """
        return {"command": self.command.value, "button": self.button}


@dataclass(frozen=True)
class ShutdownRequest:
    """守护进程收口请求。

    Attributes:
        command: 协议命令名（固定，不参与构造）。
    """

    command: ControlCommand = field(default=ControlCommand.SHUTDOWN, init=False)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            请求对象。
        """
        return {"command": self.command.value}


#: 控制面请求的封闭联合（解码产物恒为其中之一）。
ControlRequest = PingRequest | KeyRequest | MoveRequest | ClickRequest | ShutdownRequest


# ─────────────────────────────────────────────────────────────────────────────
# 控制面协议：响应
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PingDetails:
    """健康探测的附加字段（设备摘要与屏幕尺寸）。

    Attributes:
        keyboard: 虚拟键盘内核名。
        pointer: 虚拟指针内核名。
        width: 屏幕宽度（像素）。
        height: 屏幕高度（像素）。
    """

    keyboard: str
    pointer: str
    width: int
    height: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            附加字段对象。
        """
        return {
            "keyboard": self.keyboard,
            "pointer": self.pointer,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class CommandOutcome:
    """控制命令执行结果（在线协议与本地调用共用的模型）。

    Attributes:
        command: 命令名（请求无法解码时为空字符串）。
        ok: 是否成功。
        error: 失败原因（成功为 None）。
        details: 附加字段（仅健康探测携带）。
    """

    command: str
    ok: bool
    error: str | None = None
    details: PingDetails | None = None

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            协议响应对象（``ok`` / ``command`` / 可选 ``error`` 与附加字段）。
        """
        payload: dict[str, object] = {"ok": self.ok, "command": self.command}
        if self.error is not None:
            payload["error"] = self.error
        if self.details is not None:
            payload.update(self.details.to_wire())
        return payload


# ─────────────────────────────────────────────────────────────────────────────
# 设备制品
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VirtualDeviceInfo:
    """单个虚拟设备的身份与节点信息。

    Attributes:
        name: 内核设备名（sysfs ``name`` 字段）。
        event_path: 容器内事件设备节点路径。
    """

    name: str
    event_path: str


@dataclass(frozen=True)
class DevicePairInfo:
    """一对虚拟输入设备的完整信息。

    Attributes:
        keyboard: 虚拟键盘。
        pointer: 虚拟指针。
        width: 绝对指针的 X 轴最大值所对应的屏幕宽度（像素）。
        height: 绝对指针的 Y 轴最大值所对应的屏幕高度（像素）。
    """

    keyboard: VirtualDeviceInfo
    pointer: VirtualDeviceInfo
    width: int
    height: int


@dataclass(frozen=True)
class DeviceArtifacts:
    """落盘的设备信息（门脚本读取后校验节点与控制面）。

    Attributes:
        keyboard_name: 虚拟键盘内核名。
        keyboard_event: 虚拟键盘事件节点路径。
        pointer_name: 虚拟指针内核名。
        pointer_event: 虚拟指针事件节点路径。
        width: 屏幕宽度。
        height: 屏幕高度。
    """

    keyboard_name: str
    keyboard_event: str
    pointer_name: str
    pointer_event: str
    width: int
    height: int

    def to_wire(self) -> dict[str, object]:
        """把设备信息编码为 JSON 载荷。

        Returns:
            可序列化对象。
        """
        return {
            "width": self.width,
            "height": self.height,
            "keyboard": {
                "name": self.keyboard_name,
                "event_path": self.keyboard_event,
            },
            "pointer": {
                "name": self.pointer_name,
                "event_path": self.pointer_event,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# 门禁载荷：外部命令与解析产物
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandOutput:
    """外部命令执行结果。

    Attributes:
        returncode: 退出码。
        stdout: 标准输出原始字节。
        stderr: 标准错误原始字节。
    """

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        """标准输出的文本形态（替换非法字节）。

        Returns:
            解码后的标准输出。
        """
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """标准错误的文本形态（替换非法字节）。

        Returns:
            解码后的标准错误。
        """
        return self.stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ScreenInfo:
    """``xdpyinfo`` 解析出的屏幕属性。

    Attributes:
        width: 宽度（像素）。
        height: 高度（像素）。
        depth: 根窗口色深。
    """

    width: int
    height: int
    depth: int


@dataclass(frozen=True)
class XInputDevice:
    """``xinput list`` 解析出的输入设备。

    Attributes:
        name: 设备名。
        device_id: XInput 设备 id。
        floating: 是否为漂浮设备。
    """

    name: str
    device_id: int
    floating: bool


@dataclass(frozen=True)
class XevCounts:
    """``xev`` 输出中的事件计数。

    Attributes:
        key_press: 按键按下事件数。
        key_release: 按键抬起事件数。
        button_press: 鼠标按下事件数。
        button_release: 鼠标抬起事件数。
        motion: 指针移动事件数。
    """

    key_press: int
    key_release: int
    button_press: int
    button_release: int
    motion: int


@dataclass(frozen=True)
class ScreenshotStats:
    """截图校验结果。

    Attributes:
        width: 图片宽度。
        height: 图片高度。
        channel_min: 全通道像素最小值。
        channel_max: 全通道像素最大值。
    """

    width: int
    height: int
    channel_min: int
    channel_max: int


# ─────────────────────────────────────────────────────────────────────────────
# 门禁载荷：步骤与报告
# ─────────────────────────────────────────────────────────────────────────────


class StepStatus(StrEnum):
    """门禁步骤状态。"""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StepRecord:
    """单步执行记录。

    Attributes:
        name: 步骤名。
        status: 状态。
        duration_perf_counter_ms: ``perf_counter`` 计时的毫秒耗时。
        detail: 结果摘要或失败原因。
    """

    name: str
    status: StepStatus
    duration_perf_counter_ms: int
    detail: str

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            步骤记录对象。
        """
        return {
            "name": self.name,
            "status": self.status.value,
            "duration_perf_counter_ms": self.duration_perf_counter_ms,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateReport:
    """门禁总报告。

    Attributes:
        verdict: ``PASS`` / ``FAIL``。
        steps: 步骤记录（顺序固定）。
        hostname: 容器主机名。
        kernel: 内核版本。
        docker: Docker 版本（不可得为 None）。
        started_at_ms: 启动时刻（Unix Epoch 毫秒）。
        duration_perf_counter_ms: 总耗时毫秒。
    """

    verdict: str
    steps: tuple[StepRecord, ...]
    hostname: str
    kernel: str
    docker: str | None
    started_at_ms: int
    duration_perf_counter_ms: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 载荷。

        Returns:
            可序列化报告。
        """
        return {
            "verdict": self.verdict,
            "steps": [step.to_wire() for step in self.steps],
            "hostname": self.hostname,
            "kernel": self.kernel,
            "docker": self.docker,
            "started_at_ms": self.started_at_ms,
            "duration_perf_counter_ms": self.duration_perf_counter_ms,
        }


@dataclass(frozen=True)
class GateContext:
    """门禁运行参数。

    Attributes:
        display: X 显示号。
        devices_json: 设备信息路径。
        control_socket: 控制面 socket 路径。
        width: 期望宽度。
        height: 期望高度。
        vnc_port: x11vnc 端口。
        novnc_port: noVNC 端口。
        operation_timeout: 单命令超时（秒）。
        event_timeout: 事件回环超时（秒）。
        ready_timeout: 服务就绪探测超时（秒）。
    """

    display: str = DEFAULT_DISPLAY
    devices_json: Path = DEFAULT_DEVICES_JSON
    control_socket: Path = DEFAULT_SOCKET_PATH
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    vnc_port: int = DEFAULT_VNC_PORT
    novnc_port: int = DEFAULT_NOVNC_PORT
    operation_timeout: float = DEFAULT_OPERATION_TIMEOUT
    event_timeout: float = DEFAULT_EVENT_TIMEOUT
    ready_timeout: float = DEFAULT_READY_TIMEOUT


@dataclass(frozen=True)
class StepSpec:
    """门禁步骤规格。

    Attributes:
        name: 步骤名。
        runner: 异步执行函数（成功返回摘要，失败抛步骤失败异常）。
    """

    name: str
    runner: Callable[[GateContext], Awaitable[str]]
