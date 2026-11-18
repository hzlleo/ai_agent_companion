"""迁移 0013 冒烟测试（scratch SQLite——人物事实表升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性（两方言同一 Alembic 句子渲染）。降级语义：删表
（事实与证据链整体丢弃——文档化）。
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

#: 0013 迁移版本戳（人物事实表）。
_TARGET_REVISION = "000000000013"
#: 0013 的前一版本（降级目标）。
_PREV_REVISION = "000000000012"

#: 事实表列集（迁移形态断言面）。
_FACT_COLUMNS = {
    "id",
    "persona_id",
    "category",
    "content",
    "normalized_hash",
    "confidence",
    "status",
    "provenance",
    "source",
    "evidence",
    "superseded_by_id",
    "version",
    "active_marker",
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
    """读 SQLite 建表 SQL（sqlite_master.sql——命名约束断言面）。"""
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


async def test_migration_0013_persona_facts_round_trip(tmp_path: Path) -> None:
    """0013：升级出现事实表（列 / 唯一键 / 索引齐备）→ 降级消失 → 复升回归。"""
    db_path = tmp_path / "migration-persona-facts.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        assert "ff_persona_facts" in _sqlite_tables(db_path)
        assert _sqlite_columns(db_path, "ff_persona_facts") >= _FACT_COLUMNS
        assert "ix_ff_persona_facts_normalized_hash" in _sqlite_indexes(
            db_path, "ff_persona_facts"
        )
        table_sql = _sqlite_table_sql(db_path, "ff_persona_facts")
        assert "uq_ff_persona_facts_active" in table_sql
        assert "fk_ff_persona_facts_superseded_by_id" in table_sql

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        assert "ff_persona_facts" not in _sqlite_tables(db_path)

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert _sqlite_columns(db_path, "ff_persona_facts") >= _FACT_COLUMNS
    finally:
        database.type, database.name = backups
