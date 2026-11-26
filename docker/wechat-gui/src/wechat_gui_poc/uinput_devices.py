"""uinput 虚拟输入设备注入器（纯 stdlib）。

容器内没有 udev：``/dev/uinput`` 创建的内核设备只出现在 sysfs 的
``/sys/class/input/eventN`` 下，容器内的 ``/dev/input/eventN`` 设备节点
不会由 udev 自动生成。因此本模块在发现事件节点后按 sysfs 的
``dev``（major:minor）自行 ``mknod``（容器默认能力含 MKNOD；生产运行
只在 ``/dev/input`` 内建节点，不触碰宿主 devtmpfs）。

设备命名与 Xorg 静态 InputDevice 段（``evdev`` 驱动 + 显式
``Option "Device"``）配合：Xorg 在 PreInit 打开设备节点，因此设备必须
先于 Xorg 创建并由同一进程持有（uinput 生命周期 = 打开的文件描述符
生命周期）。门脚本经 Unix socket 控制面注入事件，见 ``uinputd``。

Google Style；注释只陈述事实，不携带批次编号等元数据。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import struct
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# ioctl 编号（Linux uinput.h；_IOW/_IO 手工计算，避免依赖第三方绑定）
# ─────────────────────────────────────────────────────────────────────────────

_UINPUT_IOCTL_BASE = ord("U")


def _ioc(direction: int, nr: int, size: int) -> int:
    """按 Linux asm-generic/ioctl.h 规则计算 ioctl 请求号。

    Args:
        direction: 方向位（1 = 写，0 = 无数据）。
        nr: 命令序号。
        size: 参数结构体字节数。

    Returns:
        32 位 ioctl 请求号。
    """
    return (direction << 30) | (size << 16) | (_UINPUT_IOCTL_BASE << 8) | nr


#: 注册支持的事件类型（``int``）。
UI_SET_EVBIT = _ioc(1, 100, 4)
#: 注册支持的按键码（``int``）。
UI_SET_KEYBIT = _ioc(1, 101, 4)
#: 注册支持的相对轴（``int``）。
UI_SET_RELBIT = _ioc(1, 102, 4)
#: 注册支持的绝对轴（``int``）。
UI_SET_ABSBIT = _ioc(1, 103, 4)
#: 注册设备属性位（``int``）。
UI_SET_PROPBIT = _ioc(1, 110, 4)
#: 创建设备（无参数）。
UI_DEV_CREATE = _ioc(0, 1, 0)
#: 销毁设备（无参数）。
UI_DEV_DESTROY = _ioc(0, 2, 0)

# ─────────────────────────────────────────────────────────────────────────────
# 事件类型 / 轴 / 按键码（Linux input-event-codes.h 子集）
# ─────────────────────────────────────────────────────────────────────────────

#: 同步事件（事件批次结束标记）。
EV_SYN = 0
#: 按键事件。
EV_KEY = 1
#: 绝对轴事件。
EV_ABS = 3
#: 同步报告码。
SYN_REPORT = 0

#: 绝对 X 轴。
ABS_X = 0
#: 绝对 Y 轴。
ABS_Y = 1

#: 输入设备属性：指针（帮助 Xorg 归类为指针而非触摸板）。
INPUT_PROP_POINTER = 0x00

#: 转义键。
KEY_ESC = 1
#: 数字 1。
KEY_1 = 2
#: 退格键。
KEY_BACKSPACE = 14
#: 制表键。
KEY_TAB = 15
#: 字母 Q。
KEY_Q = 16
#: 字母 A。
KEY_A = 30
#: 回车键。
KEY_ENTER = 28
#: 左 Ctrl。
KEY_LEFTCTRL = 29
#: 左 Shift。
KEY_LEFTSHIFT = 42
#: 空格键。
KEY_SPACE = 57
#: 左 Alt。
KEY_LEFTALT = 56
#: 删除键。
KEY_DELETE = 111
#: 方向键上。
KEY_UP = 103
#: 方向键左。
KEY_LEFT = 105
#: 方向键右。
KEY_RIGHT = 106
#: 方向键下。
KEY_DOWN = 108
#: 鼠标左键。
BTN_LEFT = 0x110
#: 鼠标右键。
BTN_RIGHT = 0x111
#: 鼠标中键。
BTN_MIDDLE = 0x112

#: 虚拟键盘注册的按键码（覆盖文本输入与常用编辑 / 导航键）。
_KEYBOARD_KEYS: tuple[int, ...] = tuple(
    sorted(
        {
            KEY_ESC,
            KEY_BACKSPACE,
            KEY_TAB,
            KEY_ENTER,
            KEY_LEFTCTRL,
            KEY_LEFTSHIFT,
            KEY_LEFTALT,
            KEY_SPACE,
            KEY_DELETE,
            KEY_UP,
            KEY_LEFT,
            KEY_RIGHT,
            KEY_DOWN,
            *range(2, 12),  # 数字 1-0
            *range(16, 26),  # Q-P
            *range(30, 39),  # A-L
            *range(44, 51),  # Z-M
        }
    )
)

#: 虚拟指针注册的按键码（三键鼠标）。
_POINTER_BUTTONS: tuple[int, ...] = (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE)

#: 键盘设备名（Xorg 静态段按名称与事件节点双重定位）。
KEYBOARD_DEVICE_NAME = "wechat-gui virtual keyboard"
#: 指针设备名。
POINTER_DEVICE_NAME = "wechat-gui virtual pointer"

#: 事件结构体格式（x86_64：timeval 16 字节 + type/code 各 2 + value 4）。
_EVENT_FORMAT = "<qqHHi"
#: 事件结构体字节数。
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)
#: 内核 ``uinput_user_dev`` 结构体格式（80 字节名 + input_id 8 字节 +
#: 4 字节 ff_effects_max + 4×64 个 32 位轴参数）。
_USER_DEV_FORMAT = "<80sHHHH I 256i"
#: ``uinput_user_dev`` 字节数。
_USER_DEV_SIZE = struct.calcsize(_USER_DEV_FORMAT)

#: 设备发现轮询间隔（秒；带截止的有界轮询，不做固定长睡眠）。
_DISCOVERY_POLL_SECONDS = 0.02


class UinputError(RuntimeError):
    """uinput 创建 / 注入 / 销毁失败。"""


class DeviceKind(StrEnum):
    """虚拟设备类别。"""

    KEYBOARD = "keyboard"
    POINTER = "pointer"


@dataclass(frozen=True)
class VirtualDeviceInfo:
    """单个虚拟设备的身份与节点信息。

    Attributes:
        kind: 设备类别。
        name: 内核设备名（sysfs ``name`` 字段）。
        event_path: 容器内事件设备节点路径。
    """

    kind: DeviceKind
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


@dataclass
class VirtualInputDevices:
    """持有内核 uinput 设备文件描述符的注入器。

    Attributes:
        keyboard_fd: 虚拟键盘的 ``/dev/uinput`` 文件描述符。
        pointer_fd: 虚拟指针的 ``/dev/uinput`` 文件描述符。
        pair_info: 设备身份与节点信息。
        _destroyed: 是否已销毁（避免重复 ioctl）。
    """

    keyboard_fd: int
    pointer_fd: int
    pair_info: DevicePairInfo
    _destroyed: bool = field(default=False, repr=False)

    def emit_key(self, code: int, *, pressed: bool) -> None:
        """注入一次按键按下或抬起。

        Args:
            code: Linux 按键码（如 ``KEY_A``）。
            pressed: True 表示按下，False 表示抬起。

        Raises:
            UinputError: 设备已销毁。
        """
        self._ensure_alive()
        _write_event(self.keyboard_fd, EV_KEY, code, 1 if pressed else 0)
        _write_event(self.keyboard_fd, EV_SYN, SYN_REPORT, 0)

    def emit_move(self, x: int, y: int) -> None:
        """把绝对指针移动到屏幕坐标（越界夹取到屏幕范围）。

        Args:
            x: 目标 X 坐标（像素）。
            y: 目标 Y 坐标（像素）。

        Raises:
            UinputError: 设备已销毁。
        """
        self._ensure_alive()
        clamped_x = min(max(x, 0), self.pair_info.width - 1)
        clamped_y = min(max(y, 0), self.pair_info.height - 1)
        _write_event(self.pointer_fd, EV_ABS, ABS_X, clamped_x)
        _write_event(self.pointer_fd, EV_ABS, ABS_Y, clamped_y)
        _write_event(self.pointer_fd, EV_SYN, SYN_REPORT, 0)

    def emit_click(self, button: int = BTN_LEFT) -> None:
        """注入一次完整的鼠标点击（按下 + 抬起）。

        Args:
            button: Linux 鼠标按键码（默认左键）。

        Raises:
            UinputError: 设备已销毁。
        """
        self._ensure_alive()
        self._emit_button(button, pressed=True)
        self._emit_button(button, pressed=False)

    def destroy(self) -> None:
        """销毁两个内核设备并关闭文件描述符（幂等）。"""
        if self._destroyed:
            return
        for fd in (self.keyboard_fd, self.pointer_fd):
            with contextlib.suppress(OSError):
                fcntl.ioctl(fd, UI_DEV_DESTROY)
            with contextlib.suppress(OSError):
                os.close(fd)
        self._destroyed = True

    def _emit_button(self, button: int, *, pressed: bool) -> None:
        """注入鼠标按键状态与同步报告。

        Args:
            button: Linux 鼠标按键码。
            pressed: True 表示按下，False 表示抬起。
        """
        _write_event(self.pointer_fd, EV_KEY, button, 1 if pressed else 0)
        _write_event(self.pointer_fd, EV_SYN, SYN_REPORT, 0)

    def _ensure_alive(self) -> None:
        """确认设备尚未销毁。

        Raises:
            UinputError: 设备已销毁。
        """
        if self._destroyed:
            raise UinputError("虚拟输入设备已销毁")


def _write_event(fd: int, event_type: int, code: int, value: int) -> None:
    """向 uinput 文件描述符写入一个 ``input_event``。

    Args:
        fd: ``/dev/uinput`` 文件描述符。
        event_type: 事件类型（如 ``EV_KEY``）。
        code: 事件码（按键码 / 轴码 / 同步码）。
        value: 事件值。

    Raises:
        UinputError: 写入失败。
    """
    payload = struct.pack(_EVENT_FORMAT, 0, 0, event_type, code, value)
    written = os.write(fd, payload)
    if written != _EVENT_SIZE:
        raise UinputError(f"事件写入不完整：{written}/{_EVENT_SIZE} 字节")


def _pack_user_dev(name: str, abs_max: tuple[int, int] | None) -> bytes:
    """打包内核 ``uinput_user_dev`` 结构体。

    Args:
        name: 设备名（最长 79 字节，超出截断）。
        abs_max: 绝对轴 (X, Y) 最大值；None 表示无绝对轴。

    Returns:
        1116 字节的结构体载荷。
    """
    absmax = [0] * 64
    absmin = [0] * 64
    absfuzz = [0] * 64
    absflat = [0] * 64
    if abs_max is not None:
        absmax[ABS_X], absmax[ABS_Y] = abs_max
    return struct.pack(
        _USER_DEV_FORMAT,
        name.encode("utf-8")[:79],
        0x03,  # BUS_USB
        0x1234,
        0x5678,
        1,
        0,
        *absmax,
        *absmin,
        *absfuzz,
        *absflat,
    )


def _read_device_name(sysfs_root: Path, entry_name: str) -> str | None:
    """读取 sysfs 中某输入设备的内核名称。

    Args:
        sysfs_root: sysfs 输入类根目录（生产为 ``/sys/class/input``）。
        entry_name: ``eventN`` 条目名。

    Returns:
        设备名；文件缺失或不可读时为 None。
    """
    name_path = sysfs_root / entry_name / "device" / "name"
    try:
        return name_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _event_index(entry: Path) -> int:
    """把 ``eventN`` 条目名转换为可排序的整数序号。

    Args:
        entry: 条目路径。

    Returns:
        数字后缀；非 ``eventN`` 形态返回 -1。
    """
    suffix = entry.name.removeprefix("event")
    return int(suffix) if suffix.isdigit() else -1


def _discover_event(
    sysfs_root: Path,
    name: str,
    *,
    timeout_seconds: float,
) -> str:
    """按内核名称在 sysfs 中定位事件节点条目。

    Args:
        sysfs_root: sysfs 输入类根目录。
        name: 目标设备内核名。
        timeout_seconds: 发现截止时间（秒；0 表示只扫描一次）。

    Returns:
        ``eventN`` 条目名。

    Raises:
        UinputError: 截止时间内未发现目标设备。
    """
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        entries = sorted(sysfs_root.glob("event*"), key=_event_index, reverse=True)
        for entry in entries:
            if _read_device_name(sysfs_root, entry.name) == name:
                return entry.name
        if time.monotonic() >= deadline:
            raise UinputError(f"未在 sysfs 中发现设备：{name}")
        time.sleep(_DISCOVERY_POLL_SECONDS)


def _ensure_event_node(sysfs_root: Path, event_root: Path, entry_name: str) -> str:
    """确保容器内事件设备节点存在（无 udev 时按 sysfs 的 dev 号 mknod）。

    Args:
        sysfs_root: sysfs 输入类根目录。
        event_root: 容器内事件节点目录（生产为 ``/dev/input``）。
        entry_name: ``eventN`` 条目名。

    Returns:
        事件设备节点路径。

    Raises:
        UinputError: sysfs ``dev`` 缺失或 mknod 失败。
    """
    event_path = event_root / entry_name
    if event_path.exists():
        return str(event_path)
    try:
        major_text, minor_text = (sysfs_root / entry_name / "dev").read_text(
            encoding="utf-8"
        ).strip().split(":")
    except (OSError, ValueError) as exc:
        raise UinputError(f"无法读取 {entry_name} 的设备号：{exc}") from exc
    try:
        os.mknod(event_path, stat.S_IFCHR | 0o600, os.makedev(int(major_text), int(minor_text)))
    except FileExistsError:
        pass
    except OSError as exc:
        raise UinputError(f"无法创建事件节点 {event_path}：{exc}") from exc
    return str(event_path)


def _open_uinput(device_path: Path) -> int:
    """打开 ``/dev/uinput``。

    Args:
        device_path: uinput 字符设备路径。

    Returns:
        文件描述符。

    Raises:
        UinputError: 打开失败（权限 / 设备不存在）。
    """
    try:
        return os.open(device_path, os.O_WRONLY)
    except OSError as exc:
        raise UinputError(f"无法打开 {device_path}：{exc}") from exc


def _configure_keyboard(fd: int) -> None:
    """在已打开的 uinput fd 上注册键盘能力并创建设备。

    Args:
        fd: ``/dev/uinput`` 文件描述符。

    Raises:
        OSError: ioctl 或写入失败。
    """
    for event_type in (EV_SYN, EV_KEY):
        fcntl.ioctl(fd, UI_SET_EVBIT, event_type)
    for code in _KEYBOARD_KEYS:
        fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    os.write(fd, _pack_user_dev(KEYBOARD_DEVICE_NAME, None))
    fcntl.ioctl(fd, UI_DEV_CREATE)


def _configure_pointer(fd: int, *, width: int, height: int) -> None:
    """在已打开的 uinput fd 上注册绝对指针能力并创建设备。

    Args:
        fd: ``/dev/uinput`` 文件描述符。
        width: 屏幕宽度（像素）。
        height: 屏幕高度（像素）。

    Raises:
        OSError: ioctl 或写入失败。
    """
    for event_type in (EV_SYN, EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, event_type)
    for button in _POINTER_BUTTONS:
        fcntl.ioctl(fd, UI_SET_KEYBIT, button)
    for axis in (ABS_X, ABS_Y):
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
    fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)
    os.write(fd, _pack_user_dev(POINTER_DEVICE_NAME, (width - 1, height - 1)))
    fcntl.ioctl(fd, UI_DEV_CREATE)


def create(
    *,
    width: int = 1280,
    height: int = 800,
    device_path: Path = Path("/dev/uinput"),
    sysfs_root: Path = Path("/sys/class/input"),
    event_root: Path = Path("/dev/input"),
    discovery_timeout_seconds: float = 5.0,
) -> VirtualInputDevices:
    """创建一对虚拟输入设备（键盘 + 绝对指针）。

    设备生命周期与返回对象持有的文件描述符绑定；进程退出或调用
    ``destroy()`` 时内核设备随之消失。

    Args:
        width: 屏幕宽度（像素），用于绝对指针轴上限。
        height: 屏幕高度（像素）。
        device_path: uinput 字符设备路径。
        sysfs_root: sysfs 输入类根目录。
        event_root: 事件设备节点目录。
        discovery_timeout_seconds: sysfs 设备发现截止时间（秒）。

    Returns:
        持有两个内核设备的注入器。

    Raises:
        UinputError: 创建、发现或节点落位失败。
        ValueError: 屏幕尺寸非正。
    """
    if width <= 0 or height <= 0:
        raise ValueError("屏幕尺寸必须为正数")
    keyboard_fd = _open_uinput(device_path)
    pointer_fd = -1
    try:
        _configure_keyboard(keyboard_fd)
        pointer_fd = _open_uinput(device_path)
        _configure_pointer(pointer_fd, width=width, height=height)
        keyboard_entry = _discover_event(
            sysfs_root, KEYBOARD_DEVICE_NAME, timeout_seconds=discovery_timeout_seconds
        )
        pointer_entry = _discover_event(
            sysfs_root, POINTER_DEVICE_NAME, timeout_seconds=discovery_timeout_seconds
        )
        keyboard = VirtualDeviceInfo(
            kind=DeviceKind.KEYBOARD,
            name=KEYBOARD_DEVICE_NAME,
            event_path=_ensure_event_node(sysfs_root, event_root, keyboard_entry),
        )
        pointer = VirtualDeviceInfo(
            kind=DeviceKind.POINTER,
            name=POINTER_DEVICE_NAME,
            event_path=_ensure_event_node(sysfs_root, event_root, pointer_entry),
        )
    except BaseException:
        for fd in (keyboard_fd, pointer_fd):
            if fd < 0:
                continue
            with contextlib.suppress(OSError):
                fcntl.ioctl(fd, UI_DEV_DESTROY)
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    return VirtualInputDevices(
        keyboard_fd=keyboard_fd,
        pointer_fd=pointer_fd,
        pair_info=DevicePairInfo(
            keyboard=keyboard,
            pointer=pointer,
            width=width,
            height=height,
        ),
    )
