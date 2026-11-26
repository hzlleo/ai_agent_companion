"""GUI 微信实验轨沙盒包。

存放 Xorg + uinput 栈的 PoC 门制品：虚拟输入设备注入器、uinput 控制面
守护进程、Xorg 配置生成与门脚本。本包不导入后端任何模块。
"""

from __future__ import annotations

__all__ = ["__version__"]

#: 本包版本（与 pyproject.toml 对齐）。
__version__ = "0.1.0"
