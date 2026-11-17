"""app/middleware/admin_auth 测试——管理面 owner-token 鉴权依赖 + 装配矩阵。

测试策略：

    - 依赖单测（TestDependencyMatrix）：本地最小 FastAPI app（health
      路由挂 ``require_admin_token`` 依赖作探针 + 共享异常处理器）+
      monkeypatch env 驱动矩阵——关闭 / 缺头 / scheme 不符 / 裸 token /
      不匹配 / 正确 / env 间接名解析 / details 不携带密钥 / 恒时比较
      路径（``secrets.compare_digest`` 调用面录制）；
    - ASGI 装配矩阵（TestAssembledAppMatrix / TestDisabledMode）：
      ``create_app`` 产物经 ASGITransport（零真实网络）——受保护路由
      401（ResultDTO error 形态）→ 正确 token 200（外部安装共享引擎）；
      豁免面（health 零头 200；qq webhook 自鉴权不受 admin token 影响，
      其自身 401 路径照常）；关闭形态（conftest 默认
      ``ADMIN_AUTH_TOKEN`` 空）全部放行；
    - 启动通告（TestStartupNotice）：helper 直调（loguru sink 捕获——
      恰一条 info + 中文 + R3 指针 + 永不落 token 本值）+
      ``startup_app_state`` 接线（recorder 恰好调用一次）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.main as app_main
import app.middleware.admin_auth as admin_auth_module
from app.api.health import router as health_router
from app.main import create_app, startup_app_state
from app.middleware.admin_auth import (
    log_admin_auth_startup_notice,
    require_admin_token,
)
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config.models import ServerConfig, Settings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.database.engine import db_engine_holder

#: 测试专用 admin token 本值（env 间接注入形态——永不进配置树）。
_TEST_ADMIN_TOKEN = "test-only-admin-token"

#: 测试专用 env 间接变量名（admin_token_env 指向形态）。
_TEST_ADMIN_TOKEN_ENV = "ADMIN_TEST_TOKEN"

#: conftest 硬覆盖的测试专用 qq webhook token 变量（豁免面矩阵消费）。
_QQ_WEBHOOK_TOKEN_ENV = "QQ_TEST_WEBHOOK_TOKEN"


def _build_probe_app(settings: Settings | None = None) -> FastAPI:
    """本地最小 app（health 探针路由挂鉴权依赖 + 共享异常处理器）。

    health 路由零依赖（不触 DB / 池），挂上鉴权依赖后即成为纯鉴权
    矩阵探针——请求结果只反映依赖的放行 / 拒绝语义。

    Args:
        settings: 显式 Settings（None = 不上挂 ``app.state.settings``，
            走全局单例回落——与 channels_qq 同回退次序）。

    Returns:
        已装配探针路由的 FastAPI 应用。
    """
    app = FastAPI(docs_url=None, redoc_url=None)
    app.include_router(health_router, dependencies=[Depends(require_admin_token)])
    register_exception_handlers(app)
    if settings is not None:
        app.state.settings = settings
    return app


def _install_external_engine(engine: AsyncEngine) -> None:
    """外部安装共享引擎（personas 200 路径的 DB 面零 lifespan 装配）。"""
    db_engine_holder.async_engine = engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None


def _record_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[bytes, bytes]]:
    """录制 ``secrets.compare_digest`` 调用面（恒时比较路径断言用）。

    Args:
        monkeypatch: pytest monkeypatch（secrets 模块属性替换——测试后
            自动还原）。

    Returns:
        调用记录列表（逐次 ``(presented, expected)`` utf-8 字节二元组）。
    """
    calls: list[tuple[bytes, bytes]] = []
    real_compare = admin_auth_module.secrets.compare_digest

    def _record(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return bool(real_compare(left, right))

    monkeypatch.setattr(admin_auth_module.secrets, "compare_digest", _record)
    return calls


@pytest.fixture
async def probe_client() -> AsyncIterator[AsyncClient]:
    """探针 app（全局 settings 回落形态）的 ASGITransport 客户端。"""
    transport = ASGITransport(app=_build_probe_app())
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


class TestDependencyMatrix:
    async def test_disabled_when_env_empty(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 环境变量为空：鉴权关闭（无头亦 200——放行不触比较面）。"""
        calls = _record_compare_digest(monkeypatch)
        monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
        response = await probe_client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert calls == []

    async def test_missing_header_401_result_shape(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 配置时缺失 Authorization 头：401 + ResultDTO error 形态。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get("/api/v1/health")
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "UNAUTHORIZED"
        assert body["data"] == {"admin_token_env": "ADMIN_AUTH_TOKEN"}

    async def test_wrong_scheme_401(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scheme 不符（非 Bearer）：401（头形态契约）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get(
            "/api/v1/health", headers={"Authorization": "Basic dXNlcjpwd2Q="}
        )
        assert response.status_code == 401

    async def test_bare_token_without_scheme_401(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """裸 token（无 Bearer scheme）：401。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get(
            "/api/v1/health", headers={"Authorization": _TEST_ADMIN_TOKEN}
        )
        assert response.status_code == 401

    async def test_token_mismatch_401(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bearer 值不匹配：401。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get(
            "/api/v1/health", headers={"Authorization": "Bearer wrong-token"}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"

    async def test_correct_token_200(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正确 Bearer token：放行（200 ResultDTO ok 形态）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get(
            "/api/v1/health", headers={"Authorization": f"Bearer {_TEST_ADMIN_TOKEN}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "ok"

    async def test_constant_time_compare_path(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恒时比较路径：匹配判定经 ``secrets.compare_digest``（utf-8 字节面）。"""
        calls = _record_compare_digest(monkeypatch)
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get(
            "/api/v1/health", headers={"Authorization": f"Bearer {_TEST_ADMIN_TOKEN}"}
        )
        assert response.status_code == 200
        assert calls == [
            (_TEST_ADMIN_TOKEN.encode("utf-8"), _TEST_ADMIN_TOKEN.encode("utf-8"))
        ]

    async def test_env_name_indirection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 间接名解析：``admin_token_env`` 指向自定义变量（app.state.settings 注入）。"""
        monkeypatch.setenv(_TEST_ADMIN_TOKEN_ENV, _TEST_ADMIN_TOKEN)
        settings = build_isolated_settings(
            server=ServerConfig(admin_token_env=_TEST_ADMIN_TOKEN_ENV)
        )
        transport = ASGITransport(app=_build_probe_app(settings))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
            assert response.status_code == 401
            response = await client.get(
                "/api/v1/health",
                headers={"Authorization": f"Bearer {_TEST_ADMIN_TOKEN}"},
            )
            assert response.status_code == 200

    async def test_failure_details_carry_no_secret(
        self, probe_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """401 响应不携带 token 本值（details 只有变量名——密钥不入响应面）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        response = await probe_client.get("/api/v1/health")
        assert response.status_code == 401
        assert _TEST_ADMIN_TOKEN not in response.text


class TestAssembledAppMatrix:
    """create_app 产物矩阵（受保护 / 豁免——装配契约锁定）。"""

    async def test_protected_route_401_without_token(
        self, shared_db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受保护路由（personas）：token 配置时缺头 → 401（ResultDTO 形态）。"""
        _install_external_engine(shared_db_engine)
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        app = create_app()
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/personas")
        finally:
            db_engine_holder.reset()
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "UNAUTHORIZED"

    async def test_protected_route_200_with_correct_token(
        self, shared_db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受保护路由（personas）：正确 token → 200（鉴权放行后业务面照常）。"""
        _install_external_engine(shared_db_engine)
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        app = create_app()
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/personas",
                    headers={"Authorization": f"Bearer {_TEST_ADMIN_TOKEN}"},
                )
        finally:
            db_engine_holder.reset()
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["items"] == []

    async def test_exempt_health_200_without_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """豁免面（health）：token 配置时零头仍 200（零依赖存活面契约保持）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["success"] is True

    async def test_exempt_qq_webhook_unaffected_by_admin_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """豁免面（qq webhook）：admin token 配置下自鉴权照常（正确 webhook
        token + 无 admin 头 → 200 确认——若误挂 admin 依赖此处必 401）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/qq/webhook",
                json={"post_type": "message", "message_id": 10101},
                headers={
                    "Authorization": f"Bearer {os.environ.get(_QQ_WEBHOOK_TOKEN_ENV, '')}"
                },
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_exempt_qq_webhook_own_401_path_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """豁免面（qq webhook）：自身 webhook token 校验照常（错 token → 401）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/qq/webhook",
                json={"post_type": "message", "message_id": 20202},
                headers={"Authorization": "Bearer wrong-qq-token"},
            )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "UNAUTHORIZED"


class TestDisabledMode:
    """关闭形态（conftest 默认 ADMIN_AUTH_TOKEN 空）：全部路由放行。"""

    async def test_protected_route_200_without_header(
        self, shared_db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受保护路由（personas）：token 空 → 无头 200（既有路由测试不受
        影响的同一形态——conftest 默认即关闭）。"""
        _install_external_engine(shared_db_engine)
        monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
        app = create_app()
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/personas")
        finally:
            db_engine_holder.reset()
        assert response.status_code == 200
        assert response.json()["success"] is True

    async def test_health_200_without_header(self) -> None:
        """health：关闭形态零头 200。"""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
        assert response.status_code == 200

    async def test_qq_webhook_200_with_own_token(self) -> None:
        """qq webhook：关闭形态照常确认（自身 token 校验不受影响）。"""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/channels/qq/webhook",
                json={"post_type": "message", "message_id": 30303},
                headers={
                    "Authorization": f"Bearer {os.environ.get(_QQ_WEBHOOK_TOKEN_ENV, '')}"
                },
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestStartupNotice:
    async def test_disabled_mode_logs_single_info_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """关闭形态：恰一条 info——中文 + 未启用语义 + R3 指针 + 变量名。"""
        monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
        messages: list[str] = []
        handler_id = logger.add(lambda message: messages.append(str(message)))
        try:
            log_admin_auth_startup_notice(Settings())
        finally:
            logger.remove(handler_id)
        assert len([m for m in messages if "管理面 owner-token 鉴权" in m]) == 1
        notice = messages[0]
        assert "未启用" in notice
        assert "R3" in notice
        assert "ADMIN_AUTH_TOKEN" in notice

    async def test_enabled_mode_logs_without_token_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """启用形态：info 不携带 token 本值（密钥不入日志）。"""
        monkeypatch.setenv("ADMIN_AUTH_TOKEN", _TEST_ADMIN_TOKEN)
        messages: list[str] = []
        handler_id = logger.add(lambda message: messages.append(str(message)))
        try:
            log_admin_auth_startup_notice(Settings())
        finally:
            logger.remove(handler_id)
        assert len(messages) == 1
        assert "已启用" in messages[0]
        assert _TEST_ADMIN_TOKEN not in messages[0]

    async def test_startup_app_state_calls_notice_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """接线契约：startup_app_state 恰好调用一次启动通告（隔离 Settings 透传）。"""
        isolated = build_isolated_settings()
        calls: list[Settings] = []
        monkeypatch.setattr(
            app_main, "log_admin_auth_startup_notice", lambda s: calls.append(s)
        )
        app = create_app()
        await startup_app_state(app, isolated, config_dir=tmp_path)
        try:
            assert calls == [isolated]
        finally:
            await app_main.shutdown_app_state(app)
