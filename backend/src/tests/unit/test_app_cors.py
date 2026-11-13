"""app/main CORS 装配 + 新管理路由注册测试（P2 chunk 1）。

测试策略：``create_app(settings=隔离 Settings)`` 注入自定义
``ServerConfig.cors_origins``（零真实配置 / 零真实网络——ASGITransport）；
预检（OPTIONS + Origin + Access-Control-Request-Method）与简单请求
双面验证 CORSMiddleware 行为；空 origins = 不挂中间件（CORS 关闭形态）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from packages.harness.config.models import ServerConfig
from packages.harness.config.settings import build_isolated_settings

#: Vite dev server 来源（P2 admin 前端默认形态）。
_ALLOWED_ORIGIN = "http://localhost:5173"


@pytest.fixture
def cors_app() -> FastAPI:
    """隔离装配的 app（显式单来源 CORS）。

    Returns:
        经 ``create_app(settings=...)`` 构造的 :class:`FastAPI`（origins
        仅含 _ALLOWED_ORIGIN——允许 / 拦截两面共用同一 app）。
    """
    return create_app(
        settings=build_isolated_settings(
            server=ServerConfig(cors_origins=[_ALLOWED_ORIGIN])
        )
    )


@pytest.fixture
async def client(cors_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        cors_app: 隔离装配的 FastAPI app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=cors_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def test_preflight_from_allowed_origin_passes(client: AsyncClient) -> None:
    """预检放行：allowed Origin 的 OPTIONS → 200 + ACAO 回显 + 凭据头。"""
    response = await client.options(
        "/api/v1/personas",
        headers={"Origin": _ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "GET" in response.headers["access-control-allow-methods"]


async def test_simple_get_from_allowed_origin_annotated(client: AsyncClient) -> None:
    """简单请求放行：allowed Origin 的 GET → 200 + ACAO 头携带。"""
    response = await client.get("/api/v1/health", headers={"Origin": _ALLOWED_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN


async def test_preflight_from_disallowed_origin_blocked(client: AsyncClient) -> None:
    """预检拦截：未知 Origin 的 OPTIONS → 400 + 无 ACAO 头。"""
    response = await client.options(
        "/api/v1/personas",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_cors_disabled_when_origins_empty() -> None:
    """空 origins：不挂 CORSMiddleware（OPTIONS 落到路由层 405；GET 无 ACAO）。"""
    app = create_app(
        settings=build_isolated_settings(server=ServerConfig(cors_origins=[]))
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        preflight = await client.options(
            "/api/v1/health",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.status_code == 405  # 无 CORS 中间件——路由层直接 405
        simple = await client.get("/api/v1/health")
        assert simple.status_code == 200
        assert "access-control-allow-origin" not in simple.headers


async def test_create_app_default_ships_dev_origins() -> None:
    """默认装配：全局 settings（env 面未覆写）→ Vite dev 两形态放行。"""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = await client.options(
                "/api/v1/health",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            )
            assert response.status_code == 200, origin
            assert response.headers["access-control-allow-origin"] == origin


async def test_admin_routers_registered_in_openapi() -> None:
    """路由注册面：usage / models 两组管理端点进入 OpenAPI 路径真源。"""
    app = create_app()
    paths = set(app.openapi()["paths"].keys())
    for path in (
        "/api/v1/usage/records",
        "/api/v1/usage/aggregates",
        "/api/v1/usage/summary",
        "/api/v1/models/entries",
        "/api/v1/models/entries/{name}",
        "/api/v1/models/roles",
    ):
        assert path in paths, path
