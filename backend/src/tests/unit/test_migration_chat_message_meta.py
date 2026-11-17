"""迁移 0009 冒烟测试（scratch SQLite——meta 列升 / 降 / 复升 round-trip）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性（两方言同一 Alembic 句子渲染），不替代真引擎
套件。测试经进程内 alembic command API 驱动（settings 单例字段补丁
——测试后完整还原，零跨测试泄漏）。
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

#: 0009 迁移版本戳（会话消息 meta 列）。
_TARGET_REVISION = "000000000009"
#: 0009 的前一版本（降级目标）。
_PREV_REVISION = "000000000008"


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。"""
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


async def test_migration_0009_meta_column_round_trip(tmp_path: Path) -> None:
    """0009：升级 head 后 meta 存在 → 降级 0008 后消失 → 复升后回归。"""
    db_path = tmp_path / "migration-meta.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        assert "meta" in _sqlite_columns(db_path, "ff_chat_messages")

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        columns = _sqlite_columns(db_path, "ff_chat_messages")
        assert "meta" not in columns
        assert {"id", "session_id", "seq", "role", "content", "ts_ms"} <= columns

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert "meta" in _sqlite_columns(db_path, "ff_chat_messages")
    finally:
        database.type, database.name = backups
