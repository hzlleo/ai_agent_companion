"""uinput 注入器的 hermetic 单元测试。

ioctl 编码 / 结构体布局 / sysfs 发现 / mknod / 事件字节全部经 fake
``os.open``、``fcntl.ioctl``、``os.write`` 与临时 sysfs 树驱动，不触碰
真实 ``/dev/uinput``。
"""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

import pytest

from wechat_gui_poc import uinput_devices as ud


def _make_sysfs(root: Path, entries: dict[str, tuple[str, str]]) -> None:
    """在临时目录构造 sysfs 输入类树。

    Args:
        root: sysfs 输入类根目录。
        entries: ``eventN`` → (设备名, major:minor)。
    """
    for entry_name, (device_name, device_numbers) in entries.items():
        device_dir = root / entry_name / "device"
        device_dir.mkdir(parents=True)
        (device_dir / "name").write_text(device_name, encoding="utf-8")
        (root / entry_name / "dev").write_text(device_numbers, encoding="utf-8")


def _patch_io(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[int, int, int]], list[tuple[int, bytes]], list[int], list[tuple[str, int, int]]]:
    """安装 fake 文件系统调用并返回记录容器。

    Args:
        monkeypatch: pytest 补丁器。

    Returns:
        (ioctl 调用, write 调用, close 调用, mknod 调用) 记录列表。
    """
    ioctl_calls: list[tuple[int, int, int]] = []
    write_calls: list[tuple[int, bytes]] = []
    close_calls: list[int] = []
    mknod_calls: list[tuple[str, int, int]] = []
    descriptors = iter([3, 4])

    def fake_open(path: object, flags: int) -> int:
        del path, flags
        return next(descriptors)

    def fake_ioctl(fd: int, request: int, arg: int = 0) -> int:
        ioctl_calls.append((fd, request, arg))
        return 0

    def fake_write(fd: int, payload: bytes) -> int:
        write_calls.append((fd, payload))
        return len(payload)

    def fake_close(fd: int) -> None:
        close_calls.append(fd)

    def fake_mknod(path: object, mode: int, device: int) -> None:
        mknod_calls.append((str(path), mode, device))

    monkeypatch.setattr(ud.os, "open", fake_open)
    monkeypatch.setattr(ud.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(ud.os, "write", fake_write)
    monkeypatch.setattr(ud.os, "close", fake_close)
    monkeypatch.setattr(ud.os, "mknod", fake_mknod)
    return ioctl_calls, write_calls, close_calls, mknod_calls


def _create(
    tmp_path: Path,
    *,
    entries: dict[str, tuple[str, str]] | None = None,
) -> ud.VirtualInputDevices:
    """在 fake 调用面下创建设备对。

    Args:
        tmp_path: 临时目录。
        entries: 可选 sysfs 条目。

    Returns:
        注入器对象。
    """
    sysfs_root = tmp_path / "sys"
    event_root = tmp_path / "dev-input"
    _make_sysfs(
        sysfs_root,
        entries
        if entries is not None
        else {
            "event5": (ud.KEYBOARD_DEVICE_NAME, "13:65"),
            "event9": (ud.POINTER_DEVICE_NAME, "13:69"),
        },
    )
    return ud.create(
        width=1280,
        height=800,
        device_path=tmp_path / "uinput",
        sysfs_root=sysfs_root,
        event_root=event_root,
        discovery_timeout_seconds=0,
    )


async def test_ioctl_request_numbers_are_stable() -> None:
    """ioctl 编号与 Linux uinput.h 布局一致。"""
    assert ud.UI_SET_EVBIT == 0x40045564
    assert ud.UI_SET_KEYBIT == 0x40045565
    assert ud.UI_SET_ABSBIT == 0x40045567
    assert ud.UI_SET_PROPBIT == 0x4004556E
    assert ud.UI_DEV_CREATE == 0x5501
    assert ud.UI_DEV_DESTROY == 0x5502


async def test_pack_user_dev_layout_and_abs_range() -> None:
    """uinput_user_dev 结构体长度与绝对轴上限落位正确。"""
    payload = ud._pack_user_dev(ud.POINTER_DEVICE_NAME, (1279, 799))
    assert len(payload) == 1116
    assert payload[: len(ud.POINTER_DEVICE_NAME)] == ud.POINTER_DEVICE_NAME.encode()
    bus, vendor, product, version, ff_effects = struct.unpack_from("<HHHHI", payload, 80)
    assert (bus, vendor, product, version, ff_effects) == (0x03, 0x1234, 0x5678, 1, 0)
    assert struct.unpack_from("<i", payload, 92) == (1279,)
    assert struct.unpack_from("<i", payload, 96) == (799,)


async def test_create_registers_devices_and_creates_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建流程注册能力、写结构体、创建设备并按 sysfs 落节点。"""
    ioctl_calls, write_calls, _close_calls, mknod_calls = _patch_io(monkeypatch)
    devices = _create(tmp_path)

    assert devices.pair_info.keyboard.event_path.endswith("dev-input/event5")
    assert devices.pair_info.pointer.event_path.endswith("dev-input/event9")
    assert devices.pair_info.width == 1280
    assert devices.pair_info.height == 800

    requests = [request for _fd, request, _arg in ioctl_calls]
    assert requests.count(ud.UI_DEV_CREATE) == 2
    assert (3, ud.UI_SET_EVBIT, ud.EV_KEY) in ioctl_calls
    assert (4, ud.UI_SET_EVBIT, ud.EV_ABS) in ioctl_calls
    assert (4, ud.UI_SET_ABSBIT, ud.ABS_X) in ioctl_calls
    assert (4, ud.UI_SET_ABSBIT, ud.ABS_Y) in ioctl_calls
    assert (4, ud.UI_SET_PROPBIT, ud.INPUT_PROP_POINTER) in ioctl_calls
    assert any(fd == 3 and len(payload) == 1116 for fd, payload in write_calls)
    assert any(fd == 4 and len(payload) == 1116 for fd, payload in write_calls)

    assert len(mknod_calls) == 2
    node_path, mode, device = mknod_calls[0]
    assert node_path.endswith("event5")
    assert mode == stat.S_IFCHR | 0o600
    assert (os.major(device), os.minor(device)) == (13, 65)


async def test_emit_key_payload_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按键注入产生 EV_KEY + SYN 两条 24 字节事件。"""
    _ioctl_calls, write_calls, _close_calls, _mknod_calls = _patch_io(monkeypatch)
    devices = _create(tmp_path)
    write_calls.clear()

    devices.emit_key(ud.KEY_A, pressed=True)

    assert len(write_calls) == 2
    fd, payload = write_calls[0]
    assert fd == devices.keyboard_fd
    assert len(payload) == 24
    assert struct.unpack("<qqHHi", payload) == (0, 0, ud.EV_KEY, ud.KEY_A, 1)
    assert struct.unpack("<qqHHi", write_calls[1][1]) == (0, 0, ud.EV_SYN, ud.SYN_REPORT, 0)


async def test_emit_move_clamps_and_click_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绝对移动越界夹取；点击产生按键按下与抬起。"""
    _ioctl_calls, write_calls, _close_calls, _mknod_calls = _patch_io(monkeypatch)
    devices = _create(tmp_path)
    write_calls.clear()

    devices.emit_move(-20, 9000)
    packed = [struct.unpack("<qqHHi", payload) for _fd, payload in write_calls]
    assert packed[0][2:4] == (ud.EV_ABS, ud.ABS_X)
    assert packed[0][4] == 0
    assert packed[1][4] == 799

    write_calls.clear()
    devices.emit_click(ud.BTN_LEFT)
    packed = [struct.unpack("<qqHHi", payload) for _fd, payload in write_calls]
    assert packed[0][2:5] == (ud.EV_KEY, ud.BTN_LEFT, 1)
    assert packed[2][2:5] == (ud.EV_KEY, ud.BTN_LEFT, 0)


async def test_destroy_is_idempotent_and_blocks_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """销毁发出 DESTROY 并关闭 fd；重复销毁与后续注入行为正确。"""
    ioctl_calls, _write_calls, close_calls, _mknod_calls = _patch_io(monkeypatch)
    devices = _create(tmp_path)

    devices.destroy()
    devices.destroy()

    assert [request for _fd, request, _arg in ioctl_calls].count(ud.UI_DEV_DESTROY) == 2
    assert sorted(close_calls) == [3, 4]
    with pytest.raises(ud.UinputError):
        devices.emit_key(ud.KEY_A, pressed=True)


async def test_discovery_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sysfs 无匹配设备时在截止点立即失败。"""
    _patch_io(monkeypatch)
    sysfs_root = tmp_path / "sys"
    sysfs_root.mkdir()
    with pytest.raises(ud.UinputError, match="未在 sysfs 中发现设备"):
        ud._discover_event(sysfs_root, "missing", timeout_seconds=0)


async def test_ensure_event_node_existing_node_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存在的节点不重复 mknod。"""
    mknod_calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(ud.os, "mknod", lambda *args: mknod_calls.append(args))
    event_root = tmp_path / "dev-input"
    event_root.mkdir()
    (event_root / "event3").write_text("", encoding="utf-8")
    path = ud._ensure_event_node(tmp_path / "sys", event_root, "event3")
    assert path.endswith("event3")
    assert mknod_calls == []


async def test_ensure_event_node_missing_dev_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sysfs 缺少 dev 号时给出可读错误。"""
    monkeypatch.setattr(ud.os, "mknod", lambda *args: None)
    sysfs_root = tmp_path / "sys"
    (sysfs_root / "event4").mkdir(parents=True)
    with pytest.raises(ud.UinputError, match="无法读取 event4 的设备号"):
        ud._ensure_event_node(sysfs_root, tmp_path / "dev-input", "event4")


async def test_write_event_incomplete_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """短写被判为失败而非静默接受。"""
    monkeypatch.setattr(ud.os, "write", lambda _fd, _payload: 1)
    with pytest.raises(ud.UinputError, match="事件写入不完整"):
        ud._write_event(9, ud.EV_KEY, ud.KEY_A, 1)


async def test_create_rejects_non_positive_size() -> None:
    """非正屏幕尺寸在打开设备前拒绝。"""
    with pytest.raises(ValueError, match="屏幕尺寸必须为正数"):
        ud.create(width=0, height=800)


async def test_create_cleans_up_when_second_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次打开失败时释放已创建的键盘设备。"""
    ioctl_calls: list[tuple[int, int, int]] = []
    close_calls: list[int] = []
    monkeypatch.setattr(ud.fcntl, "ioctl", lambda fd, req, arg=0: ioctl_calls.append((fd, req, arg)) or 0)
    monkeypatch.setattr(ud.os, "write", lambda _fd, payload: len(payload))
    monkeypatch.setattr(ud.os, "close", lambda fd: close_calls.append(fd))
    attempts = iter([3])

    def fake_open(_path: object, _flags: int) -> int:
        try:
            return next(attempts)
        except StopIteration:
            raise OSError("second open fails") from None

    monkeypatch.setattr(ud.os, "open", fake_open)
    with pytest.raises(ud.UinputError, match="无法打开"):
        ud.create(device_path=tmp_path / "uinput")
    assert close_calls == [3]
    assert [request for _fd, request, _arg in ioctl_calls].count(ud.UI_DEV_DESTROY) == 1


async def test_event_index_parsing() -> None:
    """事件条目序号解析容忍非 event 形态。"""
    assert ud._event_index(Path("event12")) == 12
    assert ud._event_index(Path("mouse0")) == -1
