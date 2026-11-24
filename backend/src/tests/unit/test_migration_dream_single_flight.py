"""迁移 0019 冒烟测试（scratch SQLite——梦境单飞标记列 / 唯一键升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试锁定
SQLite 方言下的可逆性与唯一键行为：运行中标记 ``'1'`` 的同人格第二行被
拒绝，NULL 标记（终态 / 历史代际）多行共存；降级删列后标记约束消失，
复升恢复。两方言同一 Alembic 句子渲染。
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

#: 0019 迁移版本戳（梦境运行单飞守卫）。
_TARGET_REVISION = "000000000019"
#: 0019 的前一版本（降级目标）。
_PREV_REVISION = "000000000018"

#: 0019 新增的单飞标记列。
_SINGLE_FLIGHT_COLUMN = "running_marker"

#: 运行日志行插入语句（只列 NOT NULL 与标记面——created/updated 取常量）。
_INSERT_RUN_SQL = (
    "INSERT INTO ff_dream_runs"
    " (persona_id, status, started_ms, stats, decisions, created_at, updated_at,"
    " running_marker)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _sqlite_columns(db_path: Path, table: str) -> set[str]:
    """读 SQLite 表列名集合（PRAGMA table_info——只读断言面）。

    Args:
        db_path: scratch 数据库文件路径。
        table: 表名。

    Returns:
        列名集合。
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _alembic_config() -> AlembicConfig:
    """构造指向本仓库迁移脚本的 alembic 配置（script_location 绝对化）。

    Returns:
        已绝对化 script_location 的 Alembic 配置。
    """
    config = AlembicConfig(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


def _insert_run(db_path: Path, *, persona_id: str, marker: str | None) -> None:
    """插入一行运行日志（调用方负责事务提交 / 异常断言）。

    Args:
        db_path: scratch 数据库文件路径。
        persona_id: 人格作用域。
        marker: 运行标记（'1' 或 None）。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            _INSERT_RUN_SQL,
            (persona_id, "running", 1, "{}", "[]", 1, 1, marker),
        )
        conn.commit()
    finally:
        conn.close()


async def test_migration_0019_single_flight_marker_round_trip(tmp_path: Path) -> None:
    """0019：标记列与唯一键生效 → 降级删列 → 复升恢复。

    Args:
        tmp_path: pytest 临时目录（scratch 数据库落盘位置）。
    """
    db_path = tmp_path / "migration-dream-single-flight.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        assert _SINGLE_FLIGHT_COLUMN in _sqlite_columns(db_path, "ff_dream_runs")
        _insert_run(db_path, persona_id="p-1", marker="1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(db_path, persona_id="p-1", marker="1")
        _insert_run(db_path, persona_id="p-1", marker=None)
        _insert_run(db_path, persona_id="p-1", marker=None)
        _insert_run(db_path, persona_id="p-2", marker="1")

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        assert _SINGLE_FLIGHT_COLUMN not in _sqlite_columns(db_path, "ff_dream_runs")

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        assert _SINGLE_FLIGHT_COLUMN in _sqlite_columns(db_path, "ff_dream_runs")
        # 复升回填：每个人格的最新生运行行重新持标记——同人格再插入即撞键。
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(db_path, persona_id="p-1", marker="1")
        _insert_run(db_path, persona_id="p-3", marker="1")
    finally:
        database.type, database.name = backups
