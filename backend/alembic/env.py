"""Alembic 迁移环境（异步 SQLAlchemy 2.0）。

关键设计：

    - **异步引擎**：``create_async_engine`` + ``connection.run_sync`` 模式
      运行迁移（aiomysql / aiosqlite 均为 async-only 驱动）；
    - **配置注入**：``sqlalchemy.url`` 在 alembic.ini 留空，由本文件从
      ``settings.database.async_sqlalchemy_url`` 动态构造（经
      :func:`packages.harness.database.engine.create_database_engine` 同源
      的 DatabaseConfig），避免硬编码连接串；
    - **模型发现**：显式 import ORM 基类（``Base``）确保 metadata 可用；
      本 chunk（P0-3）无域表——首个迁移为空基线，后续域 chunk 落地模型
      时在此处补充 import；
    - **路径修复**：``sys.path`` 注入 ``backend/src``，使从 backend/ 目录
      调用 ``alembic ...`` 时能找到 ``packages.*``。

使用方式::

    cd backend
    pixi run alembic revision --autogenerate -m "..."   # 生成新迁移
    pixi run alembic upgrade head                        # 应用迁移
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# 路径修复：把 backend/src 加入 sys.path（alembic 从 backend/ 目录执行）
# ---------------------------------------------------------------------------
_BACKEND_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from packages.harness.config.models import settings  # noqa: E402  sys.path 操纵后导入（位置即契约）
from packages.harness.database.base import Base  # noqa: E402  sys.path 操纵后导入

# ---------------------------------------------------------------------------
# Alembic 配置
# ---------------------------------------------------------------------------
config = context.config

# 日志配置（如果通过 -c 指定了 alembic.ini）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata —— autogenerate 对比基准
# （后续域 chunk 的 ORM 模型在此补充 import 后自动进入 metadata）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。

    通过 ``alembic upgrade head --sql`` 触发。
    """
    url = settings.database.async_sqlalchemy_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """在同步 connection 上下文中执行迁移（经 ``run_sync`` 调用）。

    Args:
        connection: 同步 Connection（由异步连接经 run_sync 桥接）。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式（异步）：创建临时引擎并执行迁移。

    使用 ``NullPool`` 避免与运行时连接池冲突；迁移完成后立即 dispose。
    """
    connectable = create_async_engine(
        settings.database.async_sqlalchemy_url, poolclass=pool.NullPool
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口：在事件循环中运行异步迁移。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
