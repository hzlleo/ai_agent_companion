"""app 装配——create_app 工厂 + lifespan 生命周期（P0-6 最终装配面）。

装配契约：

    - :func:`create_app` —— FastAPI 工厂（共享异常处理器 + personas /
      health 路由 + lifespan）；
    - :func:`startup_app_state` / :func:`shutdown_app_state` —— **可独立
      驱动的纯异步函数对**（不藏在 lifespan 闭包内——测试无需 ASGI
      lifespan runner 即可直调，本工程不引入 asgi-lifespan 依赖）；
    - ``lifespan`` —— 上述函数对的 ``asynccontextmanager`` 薄封装。

启动序列（LIFO 之源——停机严格逆序）::

    setup_logging_from_settings(settings)     日志先行（后续步骤可观测）
        ↓ load_all_config(config_dir)          config.yaml / 工具清单
        ↓ init_database(settings.database)     引擎 + 会话工厂（槽位）
        ↓ init_pool_manager + start            公共池（线程 / HTTP / LLM 条目）
        ↓ app.state.gateway = LlmGateway(...)  网关上挂（默认用量接线）

停机序列（启动逆序）::

    pool_manager.stop()                       HTTP / LLM 条目池 + 共享线程池
        ↓ close_database()                    引擎 dispose + 槽位复位
        ↓ shutdown_shared_thread_pool()       幂等兜底（pool stop 已关）

``app.state.gateway``：为后续 API 消费方（会话路由 / 通道层）预装配的
chat 角色网关；P0 的对话入口为控制台 CLI（自建等价栈，见
harness.chat.cli）。

ORM 注册：persona / usage 两域 ORM 在本模块导入期注册到
``Base.metadata``——保证 ``init_database`` 的 create_all 兜底建表覆盖
全部域表（fresh SQLite 首启即可用；生产 schema 以 Alembic 为准）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

import packages.harness.persona.orm as persona_orm
import packages.harness.usage.orm as usage_orm
from app.api.health import APP_VERSION
from app.api.health import router as health_router
from app.api.personas import router as personas_router
from app.middleware.error_handler import register_exception_handlers
from packages.harness.config.loader import load_all_config, setup_logging_from_settings
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.thread_pool import shutdown_shared_thread_pool
from packages.harness.database.engine import close_database, init_database
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder
from packages.harness.pools.manager import get_pool_manager, init_pool_manager

# ORM 模型注册（导入副作用——见模块 docstring「ORM 注册」一节）。
_ORM_REGISTRY = (persona_orm, usage_orm)

__all__ = [
    "APP_VERSION",
    "create_app",
    "lifespan",
    "startup_app_state",
    "shutdown_app_state",
]


async def startup_app_state(
    app: FastAPI, settings: Settings, *, config_dir: Path | None = None
) -> None:
    """启动全部进程级组件并装配 ``app.state``（可独立驱动的纯异步函数）。

    Args:
        app: 待装配的 FastAPI 应用（gateway 上挂到 ``app.state``）。
        settings: Settings（生产传全局单例；测试传隔离实例）。
        config_dir: 结构化配置目录（None = CWD 发现；测试注入 tmp_path）。

    Raises:
        BusinessException: 配置解析失败（``CONFIG_INVALID``——fail loud，
            半生效配置不可审计）。
    """
    setup_logging_from_settings(settings)
    load_all_config(config_dir=config_dir)
    await init_database(settings.database)
    manager = init_pool_manager(settings)
    await manager.start()
    app.state.gateway = LlmGateway(
        settings=settings,
        usage_recorder=wire_default_usage_recorder(),
    )


async def shutdown_app_state(app: FastAPI) -> None:
    """停机全部进程级组件（启动严格逆序；幂等——组件未启动时 no-op）。

    Args:
        app: 待收尾的 FastAPI 应用（``app.state.gateway`` 置 None）。
    """
    manager = get_pool_manager()
    if manager is not None and manager.started:
        await manager.stop()
    await close_database()
    # 共享线程池幂等兜底（pool stop 已关闭；防御独立初始化路径）。
    shutdown_shared_thread_pool(wait=True)
    app.state.gateway = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期（startup / shutdown 函数对的薄封装）。

    Args:
        app: FastAPI 应用实例（生产全局 settings 驱动；隔离测试直调
            函数对或经 ``app.router.lifespan_context``）。

    Yields:
        无载荷（服务就绪窗口）。
    """
    await startup_app_state(app, global_settings)
    try:
        yield
    finally:
        await shutdown_app_state(app)


def create_app() -> FastAPI:
    """构造 FastAPI 应用（路由 + 异常处理器 + lifespan 一次性装配）。

    Returns:
        已完成静态装配的 :class:`FastAPI`（进程级组件在 lifespan 启动
        期装配——见 :func:`startup_app_state`）。
    """
    app = FastAPI(
        title="ai_agent_companion",
        version=APP_VERSION,
        description="自建「大肥鱼」式 AI 陪伴智能体 - 后端服务",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(personas_router)
    app.include_router(health_router)
    return app
