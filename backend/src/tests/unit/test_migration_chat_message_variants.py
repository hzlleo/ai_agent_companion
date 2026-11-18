"""迁移 0010 冒烟测试（scratch SQLite——变体列 / 复合唯一键升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性与约束语义（两方言同一 Alembic 句子渲染），不替代
真引擎套件。降级语义：变体行丢弃（正典 seq 唯一键重建前置——文档化）。
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

#: 0010 迁移版本戳（会话消息变体列 / 复合唯一键）。
_TARGET_REVISION = "000000000010"
#: 0010 的前一版本（降级目标）。
_PREV_REVISION = "000000000009"


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_table_sql(db_path: Path, table: str) -> str:
    """读 SQLite 建表语句（约束 / 外键文本断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row is not None else ""


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。"""
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


async def test_migration_0010_variant_columns_round_trip(tmp_path: Path) -> None:
    """0010：升级 head 后变量列 / 复合唯一键存在 → 降级 0009 消失 → 复升回归。"""
    db_path = tmp_path / "migration-variants.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        columns = _sqlite_columns(db_path, "ff_chat_messages")
        assert {"variant_of_id", "variant_index", "meta"} <= columns
        table_sql = _sqlite_table_sql(db_path, "ff_chat_messages")
        assert "uq_ff_chat_messages_seq_variant" in table_sql
        assert "uq_ff_chat_messages_seq" in table_sql  # 前缀包含（同名子串）
        assert "uq_ff_chat_messages_seq " not in table_sql
        assert "variant_of_id" in table_sql
        assert "ON DELETE CASCADE" in table_sql.upper()

        # 约束语义（SQLite 方言）：同父回合同序号变体被拒；正典行两列
        # NULL 时唯一键忽略（正典 seq 唯一性由服务层行锁计数维持）。
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO ff_chat_sessions"
                " (persona_id, title, message_count, last_active_at, created_at, updated_at)"
                " VALUES ('1', '变体约束', 2, 1, 1, 1)"
            )
            session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO ff_chat_messages"
                " (session_id, seq, role, content, ts_ms, created_at, updated_at)"
                " VALUES (?, 2, 'assistant', '父回合', 1, 1, 1)",
                (session_id,),
            )
            parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            variant_insert = (
                "INSERT INTO ff_chat_messages"
                " (session_id, seq, role, content, ts_ms, created_at, updated_at,"
                " variant_of_id, variant_index)"
                " VALUES (?, 2, 'assistant', ?, 1, 1, 1, ?, 1)"
            )
            conn.execute(variant_insert, (session_id, "候选一", parent_id))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(variant_insert, (session_id, "候选一重复", parent_id))
            # 正典重复 seq（两列 NULL）在 SQLite 唯一键下共存（文档化）；
            # 降级重建旧 (session_id, seq) 键前清除，避免 round-trip 假红。
            conn.execute(
                "INSERT INTO ff_chat_messages"
                " (session_id, seq, role, content, ts_ms, created_at, updated_at)"
                " VALUES (?, 2, 'assistant', '正典重复', 1, 1, 1)",
                (session_id,),
            )
            conn.commit()
            conn.execute("DELETE FROM ff_chat_messages WHERE content = '正典重复'")
            conn.commit()
        finally:
            conn.close()

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        downgraded = _sqlite_columns(db_path, "ff_chat_messages")
        assert "variant_of_id" not in downgraded
        assert "variant_index" not in downgraded
        assert "meta" in downgraded
        downgraded_sql = _sqlite_table_sql(db_path, "ff_chat_messages")
        assert "uq_ff_chat_messages_seq" in downgraded_sql
        conn = sqlite3.connect(db_path)
        try:
            # 降级已丢弃变体行（旧 seq 唯一键重建前置——只余正典父回合）。
            remaining = conn.execute(
                "SELECT content FROM ff_chat_messages ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
        assert [str(row[0]) for row in remaining] == ["父回合"]

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        reupgraded = _sqlite_columns(db_path, "ff_chat_messages")
        assert {"variant_of_id", "variant_index"} <= reupgraded
    finally:
        database.type, database.name = backups
