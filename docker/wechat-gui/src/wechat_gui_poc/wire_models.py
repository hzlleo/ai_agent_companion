"""GUI 微信沙盒的数据模型与 wire 契约（本树自持，与逻辑代码分离）。

本模块集中四类载荷：

- **控制面协议**：Unix socket 上的单请求单响应 JSON 行——``ping`` /
  ``key`` / ``move`` / ``click`` / ``shutdown`` 各自的请求模型与响应模型
  （含健康探测的附加字段）；
- **agent HTTP 契约**：仅回环监听的 ``GET /health`` 与 ``POST /invoke``
  载荷（与后端 ``channels/wechat_gui/wechat_gui_wire_models.py`` 文本
  镜像——恒 200 携带成败终态，401 / 422 属传输层失败）；
- **设备制品**：uinputd 落盘的 ``uinput-devices.json`` 模型；
- **门禁载荷**：外部命令输出、X 属性解析结果与门禁报告的模型。

模型只描述字段与序列化出口（``to_wire()``）；协议解码、设备注入与步骤
编排在 ``uinputd`` / ``uinput_devices`` / ``poc_gate`` / ``agent`` 中，二者
分离。容器内控制面仅本机可见：socket 权限 0666，不承载机密，只承载注入
指令；agent HTTP 面同样只监听回环并强制 Bearer 鉴权。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ClickToolArgs",
    "OpenChatToolArgs",
    "PressKeyToolArgs",
    "ScreenshotToolArgs",
    "TypeTextToolArgs",
    "InvokeRequestModel",
    "InvokeResponseModel",
    "HealthResponseModel",
    "ScreenshotResultModel",
    "ClickResultModel",
    "TypeTextResultModel",
    "PressKeyResultModel",
    "OpenChatResultModel",
    "DEFAULT_AGENT_HOST",
    "DEFAULT_AGENT_PORT",
    "MAX_AGENT_REQUEST_BYTES",
    "TITLE_REGION_HEIGHT",
    "WECHAT_GUI_HEALTH_PATH",
    "WECHAT_GUI_INVOKE_PATH",
    "WECHAT_GUI_TOOL_SCREENSHOT",
    "WECHAT_GUI_TOOL_CLICK",
    "WECHAT_GUI_TOOL_TYPE_TEXT",
    "WECHAT_GUI_TOOL_PRESS_KEY",
    "WECHAT_GUI_TOOL_OPEN_CHAT",
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


# ─────────────────────────────────────────────────────────────────────────────
# agent HTTP 契约：部署常量与五工具参数模型
# ─────────────────────────────────────────────────────────────────────────────

#: agent 监听地址（仅回环——后端与沙盒同机部署时经本机回环访问）。
DEFAULT_AGENT_HOST = "127.0.0.1"
#: agent 监听端口（compose 仅映射到宿主回环）。
DEFAULT_AGENT_PORT = 8090
#: 单次 agent 请求体上限（字节；协议消息很小，超限即视为客户端异常）。
MAX_AGENT_REQUEST_BYTES = 1_048_576
#: 标题栏截图区域高度（像素；1280x800 布局顶部标题条）。
TITLE_REGION_HEIGHT = 140

#: agent health 端点路径（后端客户端探测面——两树部署契约常量）。
WECHAT_GUI_HEALTH_PATH: Final[str] = "/health"
#: agent invoke 端点路径（后端客户端调用面——两树部署契约常量）。
WECHAT_GUI_INVOKE_PATH: Final[str] = "/invoke"

#: screenshot 工具名（agent 运行时唯一标识——两树契约常量）。
WECHAT_GUI_TOOL_SCREENSHOT: Final[str] = "screenshot"
#: click 工具名（仅沙盒侧暴露——后端保留常量作契约镜像）。
WECHAT_GUI_TOOL_CLICK: Final[str] = "click"
#: type_text 工具名。
WECHAT_GUI_TOOL_TYPE_TEXT: Final[str] = "type_text"
#: press_key 工具名。
WECHAT_GUI_TOOL_PRESS_KEY: Final[str] = "press_key"
#: open_chat 工具名。
WECHAT_GUI_TOOL_OPEN_CHAT: Final[str] = "open_chat"


def _require_int_arg(arguments: Mapping[str, object], key: str) -> int:
    """读取必填非负整数字段（拒绝 bool）。

    Args:
        arguments: 工具调用参数对象。
        key: 字段名。

    Returns:
        字段值。

    Raises:
        ValueError: 字段缺失、类型不符或为负数。
    """
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"参数 {key} 必须是非负整数")
    return value


def _require_text_arg(arguments: Mapping[str, object], key: str) -> str:
    """读取必填非空字符串字段。

    Args:
        arguments: 工具调用参数对象。
        key: 字段名。

    Returns:
        字段原文（调用方按需裁剪空白）。

    Raises:
        ValueError: 字段缺失、类型不符或为空白。
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参数 {key} 必须是非空字符串")
    return value


@dataclass(frozen=True)
class ScreenshotToolArgs:
    """screenshot 工具参数（无参数——空对象校验锚点）。"""

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> ScreenshotToolArgs:
        """从 wire 参数对象构造（拒绝多余字段）。

        Args:
            arguments: 工具调用参数对象。

        Returns:
            参数模型。

        Raises:
            ValueError: 携带了未声明的字段。
        """
        if arguments:
            raise ValueError(f"screenshot 不接受参数：{sorted(arguments)}")
        return cls()


@dataclass(frozen=True)
class ClickToolArgs:
    """click 工具参数（绝对坐标点击）。

    Attributes:
        x: 目标 X 坐标（像素，非负）。
        y: 目标 Y 坐标（像素，非负）。
    """

    x: int
    y: int

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> ClickToolArgs:
        """从 wire 参数对象构造（字段类型与取值范围校验）。

        Args:
            arguments: 工具调用参数对象。

        Returns:
            参数模型。

        Raises:
            ValueError: 字段缺失或非法。
        """
        return cls(
            x=_require_int_arg(arguments, "x"),
            y=_require_int_arg(arguments, "y"),
        )


@dataclass(frozen=True)
class TypeTextToolArgs:
    """type_text 工具参数（文本注入——经剪贴板粘贴以覆盖非 ASCII 文本）。

    Attributes:
        text: 待输入文本（非空白）。
    """

    text: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> TypeTextToolArgs:
        """从 wire 参数对象构造（文本非空白校验）。

        Args:
            arguments: 工具调用参数对象。

        Returns:
            参数模型。

        Raises:
            ValueError: 文本缺失或为空白。
        """
        return cls(text=_require_text_arg(arguments, "text"))


@dataclass(frozen=True)
class PressKeyToolArgs:
    """press_key 工具参数（按键 / 组合键）。

    Attributes:
        key: 按键表达式（如 ``enter`` / ``esc`` / ``ctrl+f``）。
    """

    key: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> PressKeyToolArgs:
        """从 wire 参数对象构造。

        Args:
            arguments: 工具调用参数对象。

        Returns:
            参数模型。

        Raises:
            ValueError: 按键表达式缺失或为空白。
        """
        return cls(key=_require_text_arg(arguments, "key").strip())


@dataclass(frozen=True)
class OpenChatToolArgs:
    """open_chat 工具参数（按联系人备注名搜索并打开会话）。

    Attributes:
        remark_name: 联系人备注名（非空白——搜索框输入原文）。
    """

    remark_name: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> OpenChatToolArgs:
        """从 wire 参数对象构造。

        Args:
            arguments: 工具调用参数对象。

        Returns:
            参数模型。

        Raises:
            ValueError: 备注名缺失或为空白。
        """
        return cls(remark_name=_require_text_arg(arguments, "remark_name").strip())


# ─────────────────────────────────────────────────────────────────────────────
# agent HTTP 契约：wire 模型（与后端 wechat_gui_wire_models.py 文本镜像）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthResponseModel:
    """GUI 微信 agent 健康探测响应（GET /health 载荷——无鉴权存活面）。

    Attributes:
        status: 存活状态（恒 ``"ok"``——后端启动期探测的判定面）。
    """

    status: str = "ok"

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            健康探测响应对象。
        """
        return {"status": self.status}


@dataclass(frozen=True)
class InvokeRequestModel:
    """GUI 微信 agent 工具调用请求（backend → 沙盒 wire 契约——POST /invoke 载荷）。

    Attributes:
        tool: 工具名（agent 运行时内唯一标识——五工具面见 agent.py）。
        arguments: 调用参数（JSON 对象——agent 侧校验后进入执行器）。
    """

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            工具调用请求对象。
        """
        return {"tool": self.tool, "arguments": self.arguments}

    @classmethod
    def from_wire(cls, payload: object) -> InvokeRequestModel:
        """从 wire 请求对象还原模型（严格字段校验）。

        Args:
            payload: 已解析的 JSON 载荷。

        Returns:
            工具调用请求模型。

        Raises:
            ValueError: 根节点非对象 / 携带未声明字段 / tool 非法 /
                arguments 非对象。
        """
        if not isinstance(payload, dict):
            raise ValueError("请求根节点必须是对象")
        extra = set(payload) - {"tool", "arguments"}
        if extra:
            raise ValueError(f"请求携带未声明的字段：{sorted(extra)}")
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("字段 tool 必须是非空字符串")
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("字段 arguments 必须是对象")
        return cls(tool=tool.strip(), arguments=dict(arguments))


@dataclass(frozen=True)
class InvokeResponseModel:
    """GUI 微信 agent 工具调用响应（沙盒 → backend wire 契约——恒携带成败终态）。

    Attributes:
        ok: 是否成功（未知工具 / 参数校验失败 / 执行失败 / 执行异常均归一
            为本形态，agent 端点恒 200 携带）。
        result: 结果 JSON（成功时为工具执行器返回 dict；失败恒空 dict）。
        error: 失败原因（成功为 None；人读中文字符串）。
        latency_ms: agent 侧单次执行时延（epoch 毫秒差；含失败路径耗时）。
    """

    ok: bool = True
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            协议响应对象（``ok`` / ``result`` / ``latency_ms`` / 可选 ``error``）。
        """
        payload: dict[str, object] = {
            "ok": self.ok,
            "result": self.result,
            "latency_ms": self.latency_ms,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_wire(cls, payload: object) -> InvokeResponseModel:
        """从 wire 响应对象还原模型（严格字段校验）。

        Args:
            payload: 已解析的 JSON 载荷。

        Returns:
            工具调用响应模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("响应根节点必须是对象")
        ok = payload.get("ok")
        result = payload.get("result", {})
        error = payload.get("error")
        latency_ms = payload.get("latency_ms", 0)
        if not isinstance(ok, bool):
            raise ValueError("字段 ok 必须是布尔值")
        if not isinstance(result, dict):
            raise ValueError("字段 result 必须是对象")
        if error is not None and not isinstance(error, str):
            raise ValueError("字段 error 必须是字符串或 null")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("字段 latency_ms 必须是非负整数")
        return cls(
            ok=ok,
            result=dict(result),
            error=error,
            latency_ms=latency_ms,
        )


# ─────────────────────────────────────────────────────────────────────────────
# agent HTTP 契约：五工具结果模型（与后端 wechat_gui_wire_models.py 文本镜像）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScreenshotResultModel:
    """screenshot 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        image_base64: PNG base64 载荷。
        sha256: 截图内容 sha256。
        width: 截图宽度（像素）。
        height: 截图高度（像素）。
    """

    image_base64: str
    sha256: str
    width: int
    height: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            截图结果对象。
        """
        return {
            "image_base64": self.image_base64,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_wire(cls, payload: object) -> ScreenshotResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            截图结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        image_base64 = payload.get("image_base64")
        sha256 = payload.get("sha256")
        width = payload.get("width")
        height = payload.get("height")
        if not isinstance(image_base64, str) or not image_base64:
            raise ValueError("字段 image_base64 必须是非空字符串")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("字段 sha256 必须是非空字符串")
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise ValueError("字段 width 必须是正整数")
        if isinstance(height, bool) or not isinstance(height, int) or height < 1:
            raise ValueError("字段 height 必须是正整数")
        return cls(image_base64=image_base64, sha256=sha256, width=width, height=height)


@dataclass(frozen=True)
class ClickResultModel:
    """click 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        x: 点击 X 坐标（像素）。
        y: 点击 Y 坐标（像素）。
    """

    x: int
    y: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            点击结果对象。
        """
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_wire(cls, payload: object) -> ClickResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            点击结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        x = payload.get("x")
        y = payload.get("y")
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            raise ValueError("字段 x 必须是非负整数")
        if isinstance(y, bool) or not isinstance(y, int) or y < 0:
            raise ValueError("字段 y 必须是非负整数")
        return cls(x=x, y=y)


@dataclass(frozen=True)
class TypeTextResultModel:
    """type_text 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        chars: 实际粘贴文本的字符数。
    """

    chars: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            文本输入结果对象。
        """
        return {"chars": self.chars}

    @classmethod
    def from_wire(cls, payload: object) -> TypeTextResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            文本输入结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        chars = payload.get("chars")
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
            raise ValueError("字段 chars 必须是非负整数")
        return cls(chars=chars)


@dataclass(frozen=True)
class PressKeyResultModel:
    """press_key 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        key: 已注入的按键表达式原文。
    """

    key: str

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            按键注入结果对象。
        """
        return {"key": self.key}

    @classmethod
    def from_wire(cls, payload: object) -> PressKeyResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            按键注入结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("字段 key 必须是非空字符串")
        return cls(key=key)


@dataclass(frozen=True)
class OpenChatResultModel:
    """open_chat 工具结果（沙盒 → backend wire 契约——标题与全屏截图）。

    Attributes:
        remark_name: 搜索并打开的目标备注名。
        title_image_base64: 标题区域 PNG base64。
        full_image_base64: 全屏 PNG base64。
        title_sha256: 标题区域截图 sha256。
        full_sha256: 全屏截图 sha256。
        title_changed: 标题区域是否在截止内变化（False = 超时未变化，
            后端仍以视觉标题校验为 fail-closed 落定前提）。
    """

    remark_name: str
    title_image_base64: str
    full_image_base64: str
    title_sha256: str
    full_sha256: str
    title_changed: bool

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            会话打开结果对象。
        """
        return {
            "remark_name": self.remark_name,
            "title_image_base64": self.title_image_base64,
            "full_image_base64": self.full_image_base64,
            "title_sha256": self.title_sha256,
            "full_sha256": self.full_sha256,
            "title_changed": self.title_changed,
        }

    @classmethod
    def from_wire(cls, payload: object) -> OpenChatResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            会话打开结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        remark_name = payload.get("remark_name")
        title_image_base64 = payload.get("title_image_base64")
        full_image_base64 = payload.get("full_image_base64")
        title_sha256 = payload.get("title_sha256")
        full_sha256 = payload.get("full_sha256")
        title_changed = payload.get("title_changed")
        if not isinstance(remark_name, str) or not remark_name:
            raise ValueError("字段 remark_name 必须是非空字符串")
        if not isinstance(title_image_base64, str) or not title_image_base64:
            raise ValueError("字段 title_image_base64 必须是非空字符串")
        if not isinstance(full_image_base64, str) or not full_image_base64:
            raise ValueError("字段 full_image_base64 必须是非空字符串")
        if not isinstance(title_sha256, str) or not title_sha256:
            raise ValueError("字段 title_sha256 必须是非空字符串")
        if not isinstance(full_sha256, str) or not full_sha256:
            raise ValueError("字段 full_sha256 必须是非空字符串")
        if not isinstance(title_changed, bool):
            raise ValueError("字段 title_changed 必须是布尔值")
        return cls(
            remark_name=remark_name,
            title_image_base64=title_image_base64,
            full_image_base64=full_image_base64,
            title_sha256=title_sha256,
            full_sha256=full_sha256,
            title_changed=title_changed,
        )
