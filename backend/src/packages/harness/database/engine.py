"""数据库引擎工厂与生命周期（SQLAlchemy 2.0 async）。

方言分派（锁定决策）：

    - **MySQL 8 权威存储**（``mysql+aiomysql``）——生产路径；连接池参数
      全部来自 :class:`DatabaseConfig`（pool_size / max_overflow /
      pool_recycle 防 wait_timeout 断连 / pool_pre_ping 取连接前健康检查）。
    - **SQLite 测试**（``sqlite+aiosqlite``）——``DatabaseConfig.type
      = "sqlite"``；``name=":memory:"`` 时启用 ``StaticPool``（全部 session
      共享同一底层连接——SQLite 内存库 per-connection 语义的必然选择），
      文件库用默认 ``QueuePool``。

懒初始化 + 热重建：引擎与会话工厂在 ``init_database()`` 前为 None
（:class:`EngineHolder` 持有器槽位）；``rebuild_database_engine``
支持配置变更后整体交换引擎（槽位原子替换，读者调用时取新值）。

所有权治理：``init_database`` / ``rebuild`` 创建的引擎携带 owner_token
（处置凭证）；外部安装（测试 bootstrap 直接写槽位）的引擎
``owner_token=None``——生产路径不 dispose（生命周期归安装方）。

SQLite create_all fallback：init 时 ``Base.metadata.create_all``
（checkfirst=True 幂等）——开发环境 / 首次启动零迁移可用；生产 schema
演进以 Alembic 迁移为准（可追溯）。
"""

from __future__ import annotations

import uuid

from loguru import logger
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from packages.harness.config.models import DatabaseConfig, Settings, settings
from packages.harness.database.base import Base
from packages.harness.database.units.engine_holder import EngineHolder

__all__ = [
    "db_engine_holder",
    "create_database_engine",
    "init_database",
    "close_database",
    "rebuild_database_engine",
]

#: 引擎/会话工厂持有器单例（全工程唯一真源；对象身份恒定，槽位可变）。
db_engine_holder = EngineHolder()


def create_database_engine(config: DatabaseConfig) -> AsyncEngine:
    """按配置创建异步引擎（方言分派：MySQL 生产 / SQLite 测试）。

    Args:
        config: 数据库配置（``type`` 分派方言；MySQL 侧连接池参数全量
            生效，SQLite 侧忽略池参数——单连接方言）。

    Returns:
        未连接的 :class:`AsyncEngine`（连接惰性建立）。
    """
    if config.type == "sqlite":
        is_memory = config.name == ":memory:"
        return create_async_engine(
            config.async_sqlalchemy_url,
            # 内存库必须 StaticPool：所有 session 共享同一底层连接，
            # 否则每个连接是独立的空库；文件库用默认 QueuePool。
            poolclass=pool.StaticPool if is_memory else None,
            connect_args={"check_same_thread": False} if is_memory else {},
            echo=False,
        )

    return create_async_engine(
        config.async_sqlalchemy_url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_recycle=config.pool_recycle,
        pool_pre_ping=config.pool_pre_ping,
        echo=False,
    )


async def _dispose_owned_engine_if_any() -> None:
    """按所有权令牌 dispose 既有主库引擎（外部安装的引擎不动，仅告警）。

    ``owner_token is None`` 表示当前引擎为外部安装（测试 bootstrap /
    手动注入）——生命周期归安装方，生产 init/rebuild 越权 dispose 会
    误杀共享测试引擎。
    """
    holder = db_engine_holder
    engine = holder.async_engine
    if engine is None:
        return
    if holder.owner_token is None:
        logger.warning(
            "检测到外部安装的主库引擎（owner_token=None），跳过 dispose——生命周期归安装方管理"
        )
        return
    try:
        await engine.dispose()
    except Exception:
        logger.debug("旧异步引擎 dispose 失败（best-effort，已忽略）", exc_info=True)


async def init_database(config: DatabaseConfig | None = None) -> EngineHolder:
    """初始化异步数据库引擎与会话工厂（写经持有器槽位）。

    在 app lifespan（P0-6 装配）startup 中调用。重复调用安全：按所有权
    令牌先 dispose 本生命周期先前创建的引擎再创建新引擎（外部安装的
    引擎原样保留）。

    Args:
        config: 数据库配置；``None`` 时用全局 ``settings.database``。

    Returns:
        写入完成后的 :class:`EngineHolder`（与 ``db_engine_holder`` 同一对象）。
    """
    resolved = config if config is not None else settings.database

    await _dispose_owned_engine_if_any()

    holder = db_engine_holder
    holder.async_engine = create_database_engine(resolved)
    holder.session_factory = async_sessionmaker(bind=holder.async_engine, expire_on_commit=False)
    holder.owner_token = uuid.uuid4().hex
    logger.info(
        "异步数据库引擎已初始化: type={}, url_host_part={}",
        resolved.type,
        resolved.host if resolved.type == "mysql" else resolved.name,
    )

    # 开发环境 fallback：Alembic 迁移未执行时按 metadata 建缺失表
    # （checkfirst=True 幂等；生产 schema 演进以 Alembic 为准）。
    try:
        async with holder.async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # best-effort: 建表失败不阻断启动（DB 不可达 / 权限不足）
        logger.warning("create_all fallback 失败（Alembic 管理 schema 或 DB 不可达）: {}", exc)

    return holder


async def close_database() -> None:
    """关闭异步数据库引擎（app 停机期调用；恒 None 哨兵复位）。

    复位恒写 None 哨兵——从不恢复「关闭前捕获的值」；主库引擎仅在生产
    所有权（owner_token 非 None）时 dispose，外部安装的引擎由安装方管理。
    """
    holder = db_engine_holder
    if holder.async_engine is not None and holder.owner_token is not None:
        await holder.async_engine.dispose()
        logger.info("异步数据库引擎已关闭")
    elif holder.async_engine is not None:
        logger.warning("外部安装的主库引擎未被 close dispose（owner_token=None，归安装方管理）")
    holder.reset()


async def rebuild_database_engine(config: DatabaseConfig | None = None) -> EngineHolder:
    """热重建异步引擎（配置变更后调用；持有器槽位整体交换）。

    Args:
        config: 新数据库配置；``None`` 时重读全局 ``settings.database``。

    Returns:
        重建后的 :class:`EngineHolder`（与 ``db_engine_holder`` 同一对象）。
    """
    resolved = config if config is not None else settings.database

    await _dispose_owned_engine_if_any()

    holder = db_engine_holder
    holder.async_engine = create_database_engine(resolved)
    holder.session_factory = async_sessionmaker(bind=holder.async_engine, expire_on_commit=False)
    holder.owner_token = uuid.uuid4().hex
    logger.info("异步数据库引擎已重建: type={}", resolved.type)
    return holder


def current_settings_database(settings_instance: Settings | None = None) -> DatabaseConfig:
    """读取当前生效的数据库配置（测试 / 可观测性辅助）。

    Args:
        settings_instance: 显式 Settings（None = 全局单例）。

    Returns:
        对应的 :class:`DatabaseConfig`。
    """
    return settings_instance.database if settings_instance is not None else settings.database
