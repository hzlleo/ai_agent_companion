"""共享 HTTP 池——全工程复用的 ``httpx.AsyncClient`` 构建器。

单实例共享 ``httpx.AsyncClient``（连接池内建于 SDK）：全部出站 HTTP
（LLM 网关调用 / 外部 API）复用同一池，避免逐请求 TCP 握手 / TLS 协商
开销。连接上限与超时全部来自 :class:`PoolsConfig`（env 面
``POOLS_HTTP_*``）。

LLM 端点专用连接隔离由 P0-4 网关按模型条目自建 client 承担（带
per-entry 超时），本模块只管「通用出站」池。
"""

from __future__ import annotations

import httpx

from packages.harness.config.models import PoolsConfig

__all__ = ["build_shared_http_client"]


def build_shared_http_client(pools: PoolsConfig) -> httpx.AsyncClient:
    """按池配置构建共享 ``httpx.AsyncClient``。

    Args:
        pools: 池配置（``http_max_connections`` / ``http_max_keepalive_connections``
            / ``http_connect_timeout_seconds`` / ``http_timeout_seconds``）。

    Returns:
        未连接的 :class:`httpx.AsyncClient`（连接惰性建立；生命周期由
        :class:`packages.harness.pools.manager.PoolManager` 统一管理——
        ``stop()`` 期 ``aclose()``）。
    """
    limits = httpx.Limits(
        max_connections=pools.http_max_connections,
        max_keepalive_connections=pools.http_max_keepalive_connections,
    )
    timeout = httpx.Timeout(
        pools.http_timeout_seconds,
        connect=pools.http_connect_timeout_seconds,
    )
    return httpx.AsyncClient(limits=limits, timeout=timeout)
