"""Xorg 配置生成的 hermetic 单元测试。"""

from __future__ import annotations

from pathlib import Path

from wechat_gui_poc import xorg


async def test_build_xorg_config_binds_static_evdev_devices() -> None:
    """配置包含静态 evdev 输入段、dummy 屏幕与期望分辨率。"""
    config = xorg.build_xorg_config(
        keyboard_event="/dev/input/event5",
        pointer_event="/dev/input/event9",
        width=1280,
        height=800,
    )
    assert 'Option "AutoAddDevices" "false"' in config
    assert 'Driver "evdev"' in config
    assert 'Option "Device" "/dev/input/event5"' in config
    assert 'Option "Device" "/dev/input/event9"' in config
    assert 'InputDevice "wechat-gui virtual keyboard" "CoreKeyboard"' in config
    assert 'InputDevice "wechat-gui virtual pointer" "CorePointer"' in config
    assert 'Driver "dummy"' in config
    assert 'DefaultDepth 24' in config
    assert 'Modes "1280x800"' in config
    assert '"1280x800" 83.46' in config


async def test_write_xorg_config_creates_parent_directories(tmp_path: Path) -> None:
    """落盘自动创建父目录并原样写入。"""
    target = tmp_path / "nested" / "xorg.conf"
    xorg.write_xorg_config(target, config_text="Section \"ServerFlags\"\nEndSection\n")
    assert target.read_text(encoding="utf-8") == 'Section "ServerFlags"\nEndSection\n'
