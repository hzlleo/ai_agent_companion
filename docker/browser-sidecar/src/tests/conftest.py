"""Pytest conftest — 浏览器 sidecar 树测试环境准备（全部测试共享）。

硬覆盖（非 setdefault——开发者 shell 残留的同名真实配置不得泄入测试）：

    - ``BROWSER_SIDECAR_TOKEN``     —— 测试专用 token（create_app fail-fast 面）；
    - ``BROWSER_SIDECAR_ALLOWLIST`` —— 默认空（navigate 拒绝一切——安全默认；
      允许清单用例在测试内按需 monkeypatch.setenv 覆写）。

用法约束：全部测试 ``async def test_``（asyncio_mode=auto——与后端 /
tool-sandbox 子项目同约定）；hermetic 红线——零真实浏览器 / 零网络
（浏览器运行时经注入 fake face 驱动；playwright 适配层经手写 fake
playwright 对象驱动；server 经手写 ASGI 调用，无 httpx 依赖）。
"""

from __future__ import annotations

import os

os.environ["BROWSER_SIDECAR_TOKEN"] = "test-only-browser-token"
os.environ["BROWSER_SIDECAR_ALLOWLIST"] = ""
