"""harness/database session 测试——会话工厂 / session_scope / 依赖注入形态。

引擎经共享 fixture 或 init_database 装配（SQLite）；未初始化路径验证
业务异常语义。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.harness.config.models import DatabaseConfig
from packages.harness.core.context import bind_context
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import (
    close_database,
    db_engine_holder,
    init_database,
)
from packages.harness.database.session import get_async_db, get_session_factory, session_scope
from tests.db_models import PersonaNoteRow


async def test_get_session_factory_uninitialized_raises() -> None:
    """未初始化：get_session_factory → INTERNAL_ERROR（含初始化指引）。"""
    db_engine_holder.reset()
    with pytest.raises(BusinessException) as exc_info:
        get_session_factory()
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert "init_database" in exc_info.value.message


async def test_session_scope_commit_and_rollback(tmp_path) -> None:
    """session_scope：正常退出 commit / 异常 rollback + 恒关闭。"""
    await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "s.db")))
    try:
        async with bind_context(persona_id="p-scope"):
            async with session_scope() as session:
                session.add(PersonaNoteRow(content="committed"))
            # 正常退出已 commit——新作用域可见
            async with session_scope() as session:
                rows = (await session.execute(select(PersonaNoteRow))).scalars().all()
                assert [row.content for row in rows] == ["committed"]

            # 异常路径 rollback：写入不落库
            with pytest.raises(RuntimeError, match="boom"):
                async with session_scope() as session:
                    session.add(PersonaNoteRow(content="rolled-back"))
                    raise RuntimeError("boom")
            async with session_scope() as session:
                rows = (await session.execute(select(PersonaNoteRow))).scalars().all()
                assert [row.content for row in rows] == ["committed"]
    finally:
        await close_database()


async def test_get_async_db_yields_session_and_closes(tmp_path) -> None:
    """get_async_db：yield 会话 + 迭代结束自动关闭（依赖注入形态）。"""
    await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "d.db")))
    try:
        agen = get_async_db()
        session = await agen.__anext__()
        assert session is not None
        async with bind_context(persona_id="p-dep"):
            session.add(PersonaNoteRow(content="dep"))
            await session.commit()
        await agen.aclose()  # 依赖注入消费方结束（fastapi 等价路径）
        assert session.in_transaction() is False
    finally:
        await close_database()


async def test_session_factory_roundtrip_via_holder(tmp_path) -> None:
    """会话工厂直用：holder 槽位调用时读（init 后即生效）。"""
    await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "f.db")))
    try:
        factory = get_session_factory()
        assert isinstance(factory, async_sessionmaker)
        async with factory() as session, bind_context(persona_id="p-f"):
            session.add(PersonaNoteRow(content="factory"))
            await session.commit()
        assert db_engine_holder.session_factory is factory
    finally:
        await close_database()
