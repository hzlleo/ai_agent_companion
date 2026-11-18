"""迁移 0016 冒烟测试（scratch SQLite——声纹 / 同意 / 表情包三表升 / 降 / 复升）。

真引擎（SeekDB / MySQL 方言）强制面见
``src/tests/integration_seekdb/test_migration_chain.py``；本测试锁定 SQLite
方言的可逆性与命名约束（两方言同一 Alembic 句子渲染）。降级语义：按建表
逆序删三表（数据整体丢弃——文档化）。
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

#: 0016 迁移版本戳（声纹 / 同意 / 表情包三表）。
_TARGET_REVISION = "000000000016"
#: 0016 的前一版本（降级目标）。
_PREV_REVISION = "000000000015"

_SAMPLE_COLUMNS = {
    "id",
    "persona_id",
    "sender_id",
    "sample_path",
    "sample_sha256",
    "duration_ms",
    "sample_rate_hz",
    "embedding",
    "embedding_dim",
    "created_at",
    "updated_at",
}

_CONSENT_COLUMNS = {
    "id",
    "persona_id",
    "sender_id",
    "consent_text_version",
    "granted_at_ms",
    "revoked_at_ms",
    "created_at",
    "updated_at",
}

_STICKER_COLUMNS = {
    "id",
    "persona_id",
    "sha256",
    "content_type",
    "storage_path",
    "size_bytes",
    "tags",
    "description",
    "source_note",
    "annotation_status",
    "annotation_error",
    "annotation_model_entry",
    "annotated_at_ms",
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
    """读 SQLite 表索引名集合（PRAGMA index_list——索引断言面）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()
    return {str(row[1]) for row in rows}


def _sqlite_table_sql(db_path: Path, table: str) -> str:
    """读 SQLite 建表 SQL（命名唯一键断言面）。"""
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


async def test_migration_0016_voiceprint_sticker_round_trip(tmp_path: Path) -> None:
    """0016：升级出现三表与约束 → 降级回 0015 消失 → 复升回归。"""
    db_path = tmp_path / "migration-voiceprint-sticker.db"
    database = settings.database
    backups = (database.type, database.name)
    database.type = "sqlite"
    database.name = str(db_path)
    config = _alembic_config()
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        tables = _sqlite_tables(db_path)
        assert {
            "ff_voiceprint_samples",
            "ff_voiceprint_consents",
            "ff_stickers",
        } <= tables
        assert _sqlite_columns(db_path, "ff_voiceprint_samples") >= _SAMPLE_COLUMNS
        assert _sqlite_columns(db_path, "ff_voiceprint_consents") >= _CONSENT_COLUMNS
        assert _sqlite_columns(db_path, "ff_stickers") >= _STICKER_COLUMNS
        sample_sql = _sqlite_table_sql(db_path, "ff_voiceprint_samples")
        assert "uq_ff_voiceprint_samples_persona_sender_sha" in sample_sql
        sticker_sql = _sqlite_table_sql(db_path, "ff_stickers")
        assert "uq_ff_stickers_persona_sha" in sticker_sql
        assert "ix_ff_voiceprint_samples_persona_sender" in _sqlite_indexes(
            db_path, "ff_voiceprint_samples"
        )
        assert "ix_ff_voiceprint_consents_persona_sender" in _sqlite_indexes(
            db_path, "ff_voiceprint_consents"
        )
        assert "ix_ff_stickers_annotation_status" in _sqlite_indexes(
            db_path, "ff_stickers"
        )
        assert "ix_ff_stickers_persona_id" in _sqlite_indexes(db_path, "ff_stickers")

        await asyncio.to_thread(command.downgrade, config, _PREV_REVISION)
        downgraded = _sqlite_tables(db_path)
        assert "ff_voiceprint_samples" not in downgraded
        assert "ff_voiceprint_consents" not in downgraded
        assert "ff_stickers" not in downgraded

        await asyncio.to_thread(command.upgrade, config, _TARGET_REVISION)
        reupgraded = _sqlite_tables(db_path)
        assert "ff_voiceprint_samples" in reupgraded
        assert "ff_voiceprint_consents" in reupgraded
        assert "ff_stickers" in reupgraded
    finally:
        database.type, database.name = backups
