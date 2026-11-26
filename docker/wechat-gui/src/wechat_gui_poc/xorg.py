"""Xorg 静态配置生成（dummy 显卡 + evdev 显式输入设备段）。

驱动选择（文档化决策）：容器内不运行 udev，libinput 依赖 udev 枚举输入
设备；``evdev`` 驱动支持显式 ``Option "Device"`` 指向具体事件节点，不
依赖热插拔枚举。因此本栈固定使用 ``evdev`` 并在 ServerLayout 中以
CoreKeyboard / CorePointer 绑定——设备不会呈现为漂浮（floating）状态。

绝对指针在 evdev 下会被归类为 touchscreen 类型的 slave pointer（有
ABS_X/ABS_Y 且无 REL 轴）；对本栈的用途（坐标点击 / 拖拽）等价，且
``xinput`` 中仍是一个已附加的 slave pointer。若后续需要鼠标语义，
可改为相对轴并按累计位移移动，门脚本的断言面不变。
"""

from __future__ import annotations

from pathlib import Path

from .uinput_devices import KEYBOARD_DEVICE_NAME, POINTER_DEVICE_NAME

#: dummy 显卡显存（字节；1280x800x24 双缓冲有充裕余量）。
_DUMMY_VIDEO_RAM = 262144


def build_xorg_config(
    *,
    keyboard_event: str,
    pointer_event: str,
    width: int = 1280,
    height: int = 800,
) -> str:
    """生成 Xorg 配置文本（静态输入设备段 + dummy 屏幕）。

    Args:
        keyboard_event: 虚拟键盘事件节点路径（如 ``/dev/input/event7``）。
        pointer_event: 虚拟指针事件节点路径。
        width: 屏幕宽度（像素）。
        height: 屏幕高度（像素）。

    Returns:
        完整 xorg.conf 文本。
    """
    # 该 modeline 的像素时钟按 1280x800@60 的常见 CVT 参数取整；dummy 驱动
    # 不校验信号时序，仅用于固定模式名与分辨率。
    modeline = (
        f'"{width}x{height}" 83.46 {width} {width + 64} {width + 200} {width + 400} '
        f"{height} {height + 1} {height + 4} {height + 28}"
    )
    return f"""# GUI 微信实验轨沙盒——由 uinputd 在启动期按实际事件节点生成。
Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AllowEmptyInput" "false"
    Option "DontVTSwitch" "true"
    Option "AutoEnableDevices" "false"
EndSection

Section "InputDevice"
    Identifier "{KEYBOARD_DEVICE_NAME}"
    Driver "evdev"
    Option "Device" "{keyboard_event}"
EndSection

Section "InputDevice"
    Identifier "{POINTER_DEVICE_NAME}"
    Driver "evdev"
    Option "Device" "{pointer_event}"
EndSection

Section "ServerLayout"
    Identifier "wechat-gui-layout"
    Screen 0 "wechat-gui-screen"
    InputDevice "{KEYBOARD_DEVICE_NAME}" "CoreKeyboard"
    InputDevice "{POINTER_DEVICE_NAME}" "CorePointer"
EndSection

Section "Device"
    Identifier "wechat-gui-dummy"
    Driver "dummy"
    VideoRam {_DUMMY_VIDEO_RAM}
EndSection

Section "Monitor"
    Identifier "wechat-gui-monitor"
    HorizSync 5-1000
    VertRefresh 5-200
    Modeline {modeline}
EndSection

Section "Screen"
    Identifier "wechat-gui-screen"
    Device "wechat-gui-dummy"
    Monitor "wechat-gui-monitor"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
        Modes "{width}x{height}"
    EndSubSection
EndSection
"""


def write_xorg_config(path: Path, *, config_text: str) -> None:
    """把配置文本落盘（自动创建父目录）。

    Args:
        path: 目标配置文件路径。
        config_text: ``build_xorg_config`` 的产物。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text, encoding="utf-8")
