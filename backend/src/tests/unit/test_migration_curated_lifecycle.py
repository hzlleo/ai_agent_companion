"""迁移 0012 冒烟测试（scratch SQLite——生命周期列 / archived 值域升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试只锁定
SQLite 方言的可逆性与默认值语义（两方言同一 Alembic 句子渲染）。降级
语义：archived 存量行回退为 superseded 后删四列（生命周期状态丢弃——
文档化）。
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

#: 0012 迁移版本戳（策展记忆生命周期列）。
_TARGET_REVISION = "000000000012"
#: 0012 的前一版本（降级目标）。
_PREV_REVISION = "000000000011"

#: 0012 新增的生命周期列。
_LIFECYCLE_COLUMNS = {"pinned", "frozen", "access_count", "last_accessed_ms"}


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_defaults(db_path: Path, table: str) -> dict[str, str | None]:
    """读 SQLite 列默认值（PRAGMA table_info dflt_value——默认值断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]): row[4] for row in rows}


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。"""
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


async def test_migration_0012_lifecycle_columns_round_trip(tmp_path: Path) -> None:
    """0012：升级出现四列（默认值就位）→ 降级消失 → 复升回归。"""
    db_path = tmp_path / "migration-lifecycle.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        columns = _sqlite_columns(db_path, "ff_curated_memory")
        assert columns >= _LIFECYCLE_COLUMNS
        defaults = _sqlite_defaults(db_path, "ff_curated_memory")
        assert defaults["pinned"] is not None
        assert defaults["frozen"] is not None
        assert str(defaults["access_count"]).strip("'") == "0"
        assert defaults["last_accessed_ms"] is None

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        downgraded = _sqlite_columns(db_path, "ff_curated_memory")
        assert _LIFECYCLE_COLUMNS.isdisjoint(downgraded)

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        reupgraded = _sqlite_columns(db_path, "ff_curated_memory")
        assert reupgraded >= _LIFECYCLE_COLUMNS
    finally:
        database.type, database.name = backups
