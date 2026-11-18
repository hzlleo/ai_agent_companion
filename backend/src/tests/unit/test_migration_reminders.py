"""迁移 0011 冒烟测试（scratch SQLite——ff_reminders 升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性与列 / 索引形态（两方言同一 Alembic 句子渲染）。
降级语义：删表（提醒数据丢弃——文档化）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from packages.harness.config.models import settings

#: backend 项目根（alembic.ini / alembic/ 锚点）。
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

#: 0011 迁移版本戳（持久化提醒表）。
_TARGET_REVISION = "000000000011"
#: 0011 的前一版本（降级目标）。
_PREV_REVISION = "000000000010"


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_indexes(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表索引名集合（PRAGMA index_list——索引形态断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_revision(db_path: Path) -> str | None:
    """读 alembic 版本戳（降级 / 复升的迁移链状态断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _sqlite_tables(db_path: Path) -> set[str]:
    """读 SQLite 全部表名集合（降级判定的存在性断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。"""
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


async def test_migration_0011_reminders_table_round_trip(tmp_path: Path) -> None:
    """0011：升级至 0011 后 ff_reminders 列 / 索引存在 → 降级 0010 消失 → 复升回归。"""
    db_path = tmp_path / "migration-reminders.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert "ff_reminders" in _sqlite_tables(db_path)
        columns = _sqlite_columns(db_path, "ff_reminders")
        assert {
            "id",
            "persona_id",
            "title",
            "due_at_ms",
            "status",
            "delivery_attempts",
            "last_error",
            "delivered_at_ms",
            "source",
            "meta",
            "created_at",
            "updated_at",
        } <= columns
        indexes = _sqlite_indexes(db_path, "ff_reminders")
        assert "ix_ff_reminders_due_at_ms" in indexes
        assert "ix_ff_reminders_persona_status_due" in indexes
        assert "ix_ff_reminders_persona_id" in indexes

        # 约束语义（SQLite 方言）：NOT NULL 列缺席被拒（status 为扫描判定轴）。
        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ff_reminders"
                    " (persona_id, title, due_at_ms, source, created_at, updated_at)"
                    " VALUES ('1', '喝水', 1, 'manual', 1, 1)"
                )
                conn.commit()
            conn.rollback()
            conn.execute(
                "INSERT INTO ff_reminders"
                " (persona_id, title, due_at_ms, status, source, created_at, updated_at)"
                " VALUES ('1', '喝水', 1, 'pending', 'manual', 1, 1)"
            )
            conn.commit()
        finally:
            conn.close()

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        assert "ff_reminders" not in _sqlite_tables(db_path)
        assert _sqlite_revision(db_path) == _PREV_REVISION

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert "ff_reminders" in _sqlite_tables(db_path)
        assert _sqlite_revision(db_path) == _TARGET_REVISION
    finally:
        database.type, database.name = backups
