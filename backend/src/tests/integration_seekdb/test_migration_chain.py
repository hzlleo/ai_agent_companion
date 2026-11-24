"""迁移链真引擎 round-trip（SeekDB MySQL 方言——0001→0019 全链升 / 降级）。

验证面（SQLite 方言永不暴露、只有真引擎能锁定的迁移序语义）：

    - 升级 head 后 ``SHOW TABLES`` 收敛到 18 表集（``alembic_version`` +
      17 张 ``ff_*`` 域表）且版本戳为 head（0019）；0009 的
      ``ff_chat_messages.meta`` 列、0010 的变体列
      （``variant_of_id`` / ``variant_index``）、0011 的
      ``ff_reminders`` 列、0012 的 ``ff_curated_memory`` 生命周期列
      （``pinned`` / ``frozen`` / ``access_count`` /
      ``last_accessed_ms``）、0013 的 ``ff_persona_facts`` 事实列
      （``category`` / ``normalized_hash`` / ``confidence`` /
      ``evidence`` / ``superseded_by_id`` / ``active_marker``）、0014 的
      ``ff_persona_skills`` 技能列（``slug`` / ``triggers`` /
      ``use_count`` / ``last_used_ms`` / ``version``）、0015 的核心记忆
      列（块表 ``block_key`` / ``locked`` / ``updated_by`` /
      ``last_edited_session_id``；修订表 ``block_id`` / ``provenance`` /
      ``source`` / ``session_id`` / ``ts_ms``）与 0016 的声纹 / 表情包列
      （样本 ``sender_id`` / ``sample_path`` / ``sample_sha256`` /
      ``duration_ms`` / ``sample_rate_hz`` / ``embedding`` /
      ``embedding_dim``；同意 ``consent_text_version`` / ``granted_at_ms`` /
      ``revoked_at_ms``；表情包 ``sha256`` / ``content_type`` /
      ``storage_path`` / ``source_note`` / ``annotation_status``）与 0017 的
      画布列（``title`` / ``kind`` / ``content`` / ``revision``）在真引擎
      上存在；0018 的 ``ff_persona_facts`` 复合索引
      （``ix_ff_persona_facts_persona_status_confidence``）在升级 / 复升
      后存在（降级后随表删除）；0019 的 ``ff_dream_runs.running_marker``
      列与唯一键 ``uq_ff_dream_runs_persona_running`` 在升级 / 复升后
      存在（降级后随列删除）；
    - **降级 base**：MySQL 系引擎（含 SeekDB）要求外键列必须保有索引
      ——降级须先删外键约束再删其索引，否则报错误 5320（「索引被外键
      占用」）；SQLite 方言无此约束，该序错误在单测面永不暴露。本
      round-trip 即该 dialect bug 的制度化回归；
    - 降级 base 后域表全删（``alembic_version`` 表保留——live 实测
      形态），版本戳清空；
    - **复升 head**：降级零残留的完整性证明（round-trip 闭环）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from tests.integration_seekdb import support
from tests.integration_seekdb.support import SeekDbTestTarget

#: 套件选择 marker（pyproject markers 注册；显式路径定向运行为主入口）。
pytestmark = [pytest.mark.seekdb]


async def test_migration_chain_round_trip(
    seekdb_scratch: SeekDbTestTarget,
    run_alembic: Callable[..., Awaitable[None]],
) -> None:
    """升级 head（11 张域表 + 版本戳）→ 降级 base（5320 方言面）→ 复升 head。

    Args:
        seekdb_scratch: scratch 库目标（空库起步——模块 fixture 已重建）。
        run_alembic: 进程内 alembic 命令运行器（conftest 提供——
            ``asyncio.to_thread`` 桥接的异步 callable）。
    """
    # 1. 升级 head：0001→0018 全链（env.py 异步引擎在线迁移）。
    await run_alembic("head")
    tables_upgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_upgraded == support.EXPECTED_HEAD_TABLES
    assert await support.fetch_alembic_version(seekdb_scratch) == support.HEAD_REVISION
    assert await support.fetch_column_names(seekdb_scratch, "ff_chat_messages") >= {
        "id",
        "session_id",
        "seq",
        "role",
        "content",
        "ts_ms",
        "meta",
        "variant_of_id",
        "variant_index",
    }
    assert await support.fetch_column_names(seekdb_scratch, "ff_curated_memory") >= {
        "id",
        "persona_id",
        "kind",
        "topic",
        "content",
        "importance",
        "provenance",
        "status",
        "superseded_by_id",
        "version",
        "active_marker",
        "pinned",
        "frozen",
        "access_count",
        "last_accessed_ms",
        "created_at",
        "updated_at",
    }
    assert await support.fetch_column_names(seekdb_scratch, "ff_dream_runs") >= {
        "id",
        "persona_id",
        "status",
        "started_ms",
        "finished_ms",
        "running_marker",
        "stats",
        "decisions",
        "diary_episode_id",
        "error",
        "created_at",
        "updated_at",
    }
    assert "uq_ff_dream_runs_persona_running" in await support.fetch_index_names(
        seekdb_scratch, "ff_dream_runs"
    )
    assert await support.fetch_column_names(seekdb_scratch, "ff_persona_facts") >= {
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
    assert "ix_ff_persona_facts_persona_status_confidence" in await support.fetch_index_names(
        seekdb_scratch, "ff_persona_facts"
    )
    assert await support.fetch_column_names(seekdb_scratch, "ff_persona_skills") >= {
        "id",
        "persona_id",
        "name",
        "slug",
        "description",
        "triggers",
        "content",
        "status",
        "source",
        "confidence",
        "use_count",
        "last_used_ms",
        "version",
        "created_at",
        "updated_at",
    }
    assert await support.fetch_column_names(seekdb_scratch, "ff_core_memory_blocks") >= {
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
    assert await support.fetch_column_names(seekdb_scratch, "ff_core_memory_revisions") >= {
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
    assert await support.fetch_column_names(seekdb_scratch, "ff_voiceprint_samples") >= {
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
    assert await support.fetch_column_names(seekdb_scratch, "ff_voiceprint_consents") >= {
        "id",
        "persona_id",
        "sender_id",
        "consent_text_version",
        "granted_at_ms",
        "revoked_at_ms",
        "created_at",
        "updated_at",
    }
    assert await support.fetch_column_names(seekdb_scratch, "ff_stickers") >= {
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
    assert await support.fetch_column_names(seekdb_scratch, "ff_canvas_documents") >= {
        "id",
        "persona_id",
        "title",
        "kind",
        "content",
        "revision",
        "created_at",
        "updated_at",
    }
    assert await support.fetch_column_names(seekdb_scratch, "ff_reminders") >= {
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
    }

    # 2. 降级 base：0019→0001 逆向链——错误 5320 的暴露面（FK 列须保有
    #    索引：先删约束再删索引；SQLite 永不复现，真引擎强制验证）。
    await run_alembic("base", downgrade=True)
    tables_downgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_downgraded == {"alembic_version"}  # 域表全删；版本表保留（live 实测）
    assert await support.fetch_alembic_version(seekdb_scratch) is None

    # 3. 复升 head：降级零残留的完整性证明（round-trip 闭环）。
    await run_alembic("head")
    tables_reupgraded = await support.fetch_table_names(seekdb_scratch)
    assert tables_reupgraded == support.EXPECTED_HEAD_TABLES
    assert await support.fetch_alembic_version(seekdb_scratch) == support.HEAD_REVISION
    assert "ix_ff_persona_facts_persona_status_confidence" in await support.fetch_index_names(
        seekdb_scratch, "ff_persona_facts"
    )

    # 4. 提醒扫描语句的方言面：SeekDB（MySQL 系）必须支持 FOR UPDATE SKIP
    #    LOCKED（并发扫描实例抢占不重叠的到期行——job 扫描语句的锁定
    #    子句；SQLite 方言忽略该子句，故该能力只能在真引擎锁定）。
    await support.assert_reminder_scan_lock(seekdb_scratch)
