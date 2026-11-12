"""异步数据库 Session 门面——会话获取 / 作用域封装 / 依赖注入形态。

从 :data:`db_engine_holder` 持有器槽位获取会话工厂（调用时读取——
``init_database`` 运行期写槽位后立即生效）：

    - :func:`session_scope`   —— ``async with`` 作用域封装（提交 / 回滚 /
      关闭一体；服务层主入口）
    - :func:`get_async_db`    —— FastAPI ``Depends`` 形态的 async generator
      （P0-6 app 层装配时直接 ``Depends(get_async_db)``；本模块不 import
      FastAPI——纯 async generator 契约）

Session 关闭异常防御：请求任务取消与中间件清理竞争时 ``close()`` 可能
抛 ``IllegalStateChangeError``——降级 ``invalidate()`` 丢弃底层连接
（连接池 ``pool_pre_ping`` / ``pool_recycle`` 兜底重建），不向调用方
注入新异常。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy.exc import IllegalStateChangeError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder

__all__ = ["get_session_factory", "session_scope", "get_async_db"]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取当前会话工厂（持有器槽位调用时读）。

    Returns:
        ``async_sessionmaker`` 工厂（可直接调用产出 :class:`AsyncSession`）。

    Raises:
        BusinessException: 数据库未初始化（``INTERNAL_ERROR``，含初始化
            指引——lifespan startup 调 ``init_database``）。
    """
    factory = db_engine_holder.session_factory
    if factory is None:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="数据库未初始化，请先在 lifespan startup 中调用 init_database()",
        )
    return factory


async def _close_session_defensively(session: AsyncSession) -> None:
    """防御性关闭 Session——清理竞争下不向调用方注入新异常。

    Args:
        session: 待关闭的 :class:`AsyncSession`。
    """
    try:
        await session.close()
    except IllegalStateChangeError:
        logger.warning(
            "数据库 Session close 与进行中的连接获取竞争（IllegalStateChangeError），"
            "降级为 invalidate 丢弃底层连接（连接池 pre_ping/recycle 兜底）"
        )
        try:
            await session.invalidate()
        except Exception:
            logger.opt(exception=True).debug(
                "Session invalidate 亦失败，底层连接交由连接池回收兜底"
            )


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """会话作用域封装——成功提交 / 异常回滚 / 恒关闭。

    服务层主入口::

        async with session_scope() as session:
            session.add(obj)
            # 作用域正常退出自动 commit；异常自动 rollback
            await session.flush()

    Yields:
        绑定到主库引擎的 :class:`AsyncSession`。

    Raises:
        BusinessException: 数据库未初始化（透传 :func:`get_session_factory`）。
    """
    session: AsyncSession = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await _close_session_defensively(session)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入形态：获取异步 Session（请求结束自动关闭）。

    P0-6 app 层装配::

        @app.get("/items")
        async def list_items(db: AsyncSession = Depends(get_async_db)):
            ...

    Session 在请求结束时自动关闭，不提交——由服务层经 ``session_scope``
    或显式 ``commit`` 管理。

    Yields:
        绑定到主库引擎的 :class:`AsyncSession`。

    Raises:
        BusinessException: 数据库未初始化。
    """
    session: AsyncSession = get_session_factory()()
    try:
        yield session
    finally:
        await _close_session_defensively(session)
