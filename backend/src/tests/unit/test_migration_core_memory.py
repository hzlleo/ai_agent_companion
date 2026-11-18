"""迁移 0015 冒烟测试（scratch SQLite——核心记忆块 / 修订表升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性（两方言同一 Alembic 句子渲染）。降级语义：先删修订
表再删块表（核心记忆数据整体丢弃——文档化）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from packages.harness.config.models import settings

#: backend 项目根（alembic.ini / alembic/ 锚点）。
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: 0015 迁移版本戳（核心记忆块 / 修订表）。
_TARGET_REVISION = "000000000015"
#: 0015 的前一版本（降级目标）。
_PREV_REVISION = "000000000014"

#: 块表列集（迁移形态断言面）。
_BLOCK_COLUMNS = {
    "id",
    "persona_id",
    "block_key",
    "content",
    "locked",
    "version",
    "updated_by",
    "last_edited_session_id",
    "created_at",
    "updated_at",
}

#: 修订表列集（迁移形态断言面）。
_REVISION_COLUMNS = {
    "id",
    "block_id",
    "version",
    "content",
    "provenance",
    "source",
    "session_id",
    "ts_ms",
    "created_at",
    "updated_at",
}


def _sqlite_tables(db_path: Path) -> set[str]:
    """读 SQLite 表名集合（sqlite_master——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_indexes(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表索引名集合（PRAGMA index_list——普通索引断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_table_sql(db_path: Path, table: str) -> str:
    """读 SQLite 建表 SQL（sqlite_master.sql——命名约束 / 外键断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row is not None else ""


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。"""
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


async def test_migration_0015_core_memory_round_trip(tmp_path: Path) -> None:
    """0015：升级出现两表（列 / 唯一键 / 级联外键 / 索引）→ 降级消失 → 复升回归。"""
    db_path = tmp_path / "migration-core-memory.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        assert "ff_core_memory_blocks" in _sqlite_tables(db_path)
        assert "ff_core_memory_revisions" in _sqlite_tables(db_path)
        assert _sqlite_columns(db_path, "ff_core_memory_blocks") >= _BLOCK_COLUMNS
        assert _sqlite_columns(db_path, "ff_core_memory_revisions") >= _REVISION_COLUMNS
        assert "uq_ff_core_memory_blocks_key" in _sqlite_table_sql(db_path, "ff_core_memory_blocks")
        revision_sql = _sqlite_table_sql(db_path, "ff_core_memory_revisions")
        assert "REFERENCES ff_core_memory_blocks" in revision_sql
        assert "ON DELETE CASCADE" in revision_sql
        assert "ix_ff_core_memory_blocks_persona_id" in _sqlite_indexes(
            db_path, "ff_core_memory_blocks"
        )
        assert "ix_ff_core_memory_revisions_block_id" in _sqlite_indexes(
            db_path, "ff_core_memory_revisions"
        )
        assert "ix_ff_core_memory_revisions_ts_ms" in _sqlite_indexes(
            db_path, "ff_core_memory_revisions"
        )

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        assert "ff_core_memory_revisions" not in _sqlite_tables(db_path)
        assert "ff_core_memory_blocks" not in _sqlite_tables(db_path)

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert _sqlite_columns(db_path, "ff_core_memory_blocks") >= _BLOCK_COLUMNS
        assert _sqlite_columns(db_path, "ff_core_memory_revisions") >= _REVISION_COLUMNS
    finally:
        database.type, database.name = backups
