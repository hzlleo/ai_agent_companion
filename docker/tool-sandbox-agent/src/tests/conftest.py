"""Pytest conftest — 沙盒树测试环境准备（全部测试共享）。

硬覆盖（非 setdefault——开发者 shell 残留的同名真实配置不得泄入测试）：

    - ``SANDBOX_TOKEN``        —— 测试专用 token（create_app fail-fast 面）；
    - ``SANDBOX_HTTP_ALLOWLIST`` —— 默认空（工具禁用语义——安全默认；
      允许清单用例在测试内按需 monkeypatch.setenv 覆写）。

用法约束：全部测试 ``async def test_``（asyncio_mode=auto——与后端子项目
同约定）；hermetic 红线——零真实网络（http_fetch 经注入 / monkeypatch
抓取函数；server 经手写 ASGI 调用，无 httpx 依赖）。
"""

from __future__ import annotations

import os

os.environ["SANDBOX_TOKEN"] = "test-only-sandbox-token"
os.environ["SANDBOX_HTTP_ALLOWLIST"] = ""
