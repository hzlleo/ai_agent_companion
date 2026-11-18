"""core 文件工具——原子写入与幂等删除（同步实现，供线程池下沉）。

媒体文件（声纹样本 / 表情包）的落盘与清理共用本模块：写入采用「临时文件
+ flush + fsync + ``os.replace`` 原子换名」，崩溃窗口内目标路径至多旧内容
或新内容（无半写态）；删除对「文件不存在」幂等（重复清理不报错）。

调用方必须经 :func:`packages.harness.core.thread_pool.run_blocking_ops` 下沉
线程池后再调用这些同步函数（硬约束：阻塞文件 I/O 不占事件循环）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["unlink_missing_ok", "write_bytes_atomic"]


def write_bytes_atomic(directory: Path, target: Path, payload: bytes) -> Path:
    """原子写入字节文件（目录不存在则创建）。

    Args:
        directory: 目标目录（自动 ``mkdir -p``）。
        target: 目标文件路径。
        payload: 文件字节。

    Returns:
        写入后的目标路径。

    Raises:
        OSError: 目录创建 / 写入 / fsync / 换名失败（临时文件由调用方或
            操作系统回收——目标路径保持旧内容）。
    """
    directory.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def unlink_missing_ok(path: Path) -> None:
    """删除文件（不存在视为成功——重复清理幂等）。

    Args:
        path: 目标文件路径。

    Raises:
        OSError: 删除失败（权限 / 目录占用等——调用方按告警处理）。
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
