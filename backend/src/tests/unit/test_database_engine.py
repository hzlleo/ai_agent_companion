"""harness/database engine 测试——引擎工厂 / 生命周期 / 持有器所有权。

MySQL 路径只验证引擎构造参数（不真实连接）；SQLite 路径经 tmp 文件库
真连真查（init → create_all → 查询 → close 全链）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool, StaticPool

from packages.harness.config.models import DatabaseConfig
from packages.harness.database.engine import (
    close_database,
    create_database_engine,
    db_engine_holder,
    init_database,
    rebuild_database_engine,
)
from packages.harness.database.units.engine_holder import EngineHolder


async def test_create_engine_mysql_pool_params() -> None:
    """MySQL 引擎：方言 + 连接池参数全量生效（不真实连接）。"""
    config = DatabaseConfig(
        type="mysql",
        host="h",
        port=3306,
        name="db",
        user="u",
        password="p",
        pool_size=7,
        max_overflow=3,
        pool_recycle=120,
        pool_pre_ping=True,
    )
    engine = create_database_engine(config)
    try:
        assert engine.dialect.name == "mysql"
        assert engine.pool.size() == 7  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == 3  # type: ignore[attr-defined]
        assert engine.pool._recycle == 120  # type: ignore[attr-defined]
        assert engine.pool._pre_ping is True  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


async def test_create_engine_sqlite_memory_uses_static_pool() -> None:
    """SQLite 内存库：StaticPool（单连接共享语义）。"""
    engine = create_database_engine(DatabaseConfig(type="sqlite", name=":memory:"))
    try:
        assert engine.dialect.name == "sqlite"
        assert isinstance(engine.pool, StaticPool)
    finally:
        await engine.dispose()


async def test_create_engine_sqlite_file_uses_queue_pool(tmp_path: Path) -> None:
    """SQLite 文件库：默认池（非 StaticPool）。"""
    db_file = tmp_path / "file.db"
    engine = create_database_engine(DatabaseConfig(type="sqlite", name=str(db_file)))
    try:
        assert engine.dialect.name == "sqlite"
        assert not isinstance(engine.pool, StaticPool)
        assert not isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()


async def test_init_database_full_lifecycle_sqlite(tmp_path: Path) -> None:
    """init → create_all → 查询 → close 全链（SQLite 文件库真连）。"""
    config = DatabaseConfig(type="sqlite", name=str(tmp_path / "lifecycle.db"))
    holder = await init_database(config)
    assert holder is db_engine_holder
    assert holder.async_engine is not None
    assert holder.session_factory is not None
    assert holder.owner_token is not None

    async with holder.async_engine.connect() as conn:
        # create_all fallback：测试模型表已建
        rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        table_names = {row[0] for row in rows}
        assert "test_persona_note" in table_names
        assert "alembic_version" not in table_names  # 本链不经 alembic

    await close_database()
    assert holder.async_engine is None
    assert holder.session_factory is None
    assert holder.owner_token is None


async def test_init_database_replaces_owned_engine(tmp_path: Path) -> None:
    """重复 init：先 dispose 旧引擎再建新引擎（所有权令牌刷新）。"""
    first = await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "a.db")))
    token_first = first.owner_token
    second = await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "b.db")))
    assert second is first
    assert second.owner_token is not None
    assert second.owner_token != token_first
    await close_database()


async def test_init_database_preserves_externally_installed_engine(tmp_path: Path) -> None:
    """外部安装引擎（owner_token=None）：init 不 dispose（生命周期归安装方）。"""
    external = create_database_engine(DatabaseConfig(type="sqlite", name=":memory:"))
    db_engine_holder.async_engine = external
    db_engine_holder.session_factory = None
    db_engine_holder.owner_token = None
    try:
        await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "prod.db")))
        # init 建立了自己的引擎（外部引擎被槽位替换，但未被 dispose）
        assert db_engine_holder.async_engine is not external
        assert db_engine_holder.owner_token is not None
        # 外部引擎仍可连接（未被动过）
        async with external.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await external.dispose()
        await close_database()


async def test_close_database_external_engine_not_disposed() -> None:
    """close：外部安装引擎不 dispose（仅清槽位哨兵复位）。"""
    external = create_database_engine(DatabaseConfig(type="sqlite", name=":memory:"))
    db_engine_holder.async_engine = external
    db_engine_holder.session_factory = None
    db_engine_holder.owner_token = None
    try:
        await close_database()
        assert db_engine_holder.async_engine is None
        # 外部引擎仍可用（close 未 dispose 它）
        async with external.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await external.dispose()


async def test_rebuild_database_engine_swaps_slots(tmp_path: Path) -> None:
    """热重建：槽位整体交换 + 所有权令牌刷新。"""
    await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "old.db")))
    old_engine = db_engine_holder.async_engine
    await rebuild_database_engine(DatabaseConfig(type="sqlite", name=str(tmp_path / "new.db")))
    assert db_engine_holder.async_engine is not old_engine
    assert db_engine_holder.owner_token is not None
    await close_database()


async def test_engine_holder_setattr_observability() -> None:
    """持有器全写可观测：槽位写入记 debug 日志（loguru sink 捕获）。"""
    from loguru import logger

    holder = EngineHolder()
    captured: list[str] = []
    handler_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        holder.owner_token = "tok-1"
    finally:
        logger.remove(handler_id)
    assert any("owner_token" in message for message in captured)


async def test_engine_holder_unknown_slot_rejected() -> None:
    """__slots__ 语义：未知槽位名即刻 AttributeError（拼写错误无法静默）。"""
    holder = EngineHolder()
    with pytest.raises(AttributeError):
        holder.typo_slot = 1  # type: ignore[attr-defined]


async def test_engine_holder_reset_writes_none_sentinels(tmp_path: Path) -> None:
    """reset：恒 None 哨兵复位（不清 dispose——处置归生命周期函数）。"""
    await init_database(DatabaseConfig(type="sqlite", name=str(tmp_path / "r.db")))
    db_engine_holder.reset()
    assert db_engine_holder.async_engine is None
    assert db_engine_holder.session_factory is None
    assert db_engine_holder.owner_token is None
    # 旧引擎未 dispose（reset 不负责处置）——手动清理防泄漏
    await close_database()
