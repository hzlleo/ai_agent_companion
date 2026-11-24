"""并发不变式真引擎验证（DB 级约束 + 行锁——服务层重试收敛的强制面）。

十一个不变式（迁移 0007/0008/0013/0014/0015/0016/0019 落地的 DB 级守卫 +
InnoDB 行锁语义，全部经 raw SQL / 服务层共用插入函数直击真引擎——SQLite
单测面无法验证的并发拒绝行为）：

    - (a) ``ff_personas``：``is_primary_marker`` 唯一键
      ``uq_ff_personas_primary``——NULL 标记行多行共存（唯一键忽略
      NULL），第二个 ``'primary'`` 写入被 1062 拒绝且错误消息点名约束；
    - (b) ``ff_curated_memory``：复合唯一键 ``uq_ff_curated_active``
      (persona_id, kind, topic, active_marker)——同主题第二条 active
      行被 1062 拒绝；superseded（NULL 标记）多代共存；
    - (c) ``ff_chat_messages``：会话内序号唯一键
      ``uq_ff_chat_messages_seq`` (session_id, seq)——并发 append 的
      DB 级兜底，撞序被 1062 拒绝；
    - (d) 跨连接 ``SELECT ... FOR UPDATE``：连接 A 持行锁，连接 B 等
      锁超时（MySQL 错误 1205）；A 回滚释放后 B 即取锁成功（正控——
      证明确为行锁持有而非超时巧合）；
    - (e) ``ff_persona_facts``：复合唯一键 ``uq_ff_persona_facts_active``
      (persona_id, category, normalized_hash, active_marker)——更正链
      「NULL 标记插入 → 旧行 superseded → 新行取标记」的并发腾位写序，
      后提交者被 1062 拒绝，最终仅一条 active 行；
    - (f) ``ff_persona_skills``：唯一键 ``uq_ff_persona_skills_slug``
      (persona_id, slug)——同人格同 slug 第二条写入被 1062 拒绝；
    - (g) ``ff_core_memory_blocks``：唯一键 ``uq_ff_core_memory_blocks_key``
      (persona_id, block_key)——同人格同块第二条写入被 1062 拒绝；
    - (h) ``ff_stickers``：唯一键 ``uq_ff_stickers_persona_sha``
      (persona_id, sha256)——同人格同文件哈希第二条写入被 1062 拒绝，
      不同人格同哈希可共存（人格隔离轴）；
    - (i) ``ff_voiceprint_samples``：唯一键
      ``uq_ff_voiceprint_samples_persona_sender_sha``
      (persona_id, sender_id, sample_sha256)——同人格同发送者同样本哈希
      第二条写入被 1062 拒绝；
    - (j) ``ff_dream_runs``：唯一键 ``uq_ff_dream_runs_persona_running``
      (persona_id, running_marker)——同人格第二条运行中行（标记 ``'1'``）
      被 1062 拒绝；终态清标记（NULL）后下一次运行可插入；
    - (k) 表情落败写入的跨连接收敛：连接 B 在连接 A 提交前建立可重复读
      快照，随后同哈希插入撞 ``uq_ff_stickers_persona_sha``——服务层共用
      插入函数经 ``SELECT ... FOR UPDATE`` 当前读解析胜者行（快照读会落
      入旧读视图看不到胜者），单行收敛且胜者文件保留、败者文件清理。

引擎方言事实（live 实测 v1.4.0，锁定进断言）：唯一键冲突 =
pymysql ``IntegrityError``（1062，消息携带约束名）；行锁等待上限的
生效旋钮是 OceanBase 原生 ``ob_trx_lock_timeout``（微秒）——
``innodb_lock_wait_timeout`` 在本引擎为 mock 变量（读回恒 50、
``SET SESSION`` 无效），两者都设以保留语义声明与实际生效。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import aiomysql
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from packages.harness.config.models import DatabaseConfig
from packages.harness.multimodal.sticker_models import StickerSettings
from packages.harness.multimodal.sticker_orm import StickerRow
from packages.harness.multimodal.sticker_service import StickerService
from packages.harness.persona.service import bind_persona_context
from tests.integration_seekdb import support
from tests.integration_seekdb.support import SeekDbTestTarget

#: 套件选择 marker（pyproject markers 注册；显式路径定向运行为主入口）。
pytestmark = [pytest.mark.seekdb]

#: 行锁等待上限（秒）——ob_trx_lock_timeout 微秒换算基数。
_LOCK_WAIT_SECONDS = 2


async def _insert_persona(cur: aiomysql.Cursor, name: str, marker: str | None) -> None:
    """插入一行 ff_personas 种子（列形来自迁移 0003/0006/0008）。

    Args:
        cur: 游标（autocommit 连接——语句即事务落库）。
        name: 注册名（全局唯一——uq_ff_personas_name）。
        marker: 主人格标记（'primary' / NULL——NULL 不入唯一键）。
    """
    await cur.execute(
        "INSERT INTO ff_personas"
        " (name, display_name, status, chara_card, created_at, updated_at, is_primary_marker)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (name, name, "active", "{}", 1, 1, marker),
    )


async def _insert_curated(
    cur: aiomysql.Cursor,
    topic: str,
    marker: str | None,
    *,
    version: int,
) -> None:
    """插入一行 ff_curated_memory 种子（列形来自迁移 0004/0007）。

    Args:
        cur: 游标（autocommit 连接）。
        topic: 主题键（与 persona/kind 组成单 active 守卫的复合维度）。
        marker: 单 active 守卫标记（'active' / NULL——superseded 多代）。
        version: 链内版本号（supersede 语义的展示列）。
    """
    await cur.execute(
        "INSERT INTO ff_curated_memory"
        " (persona_id, kind, topic, content, importance, provenance, status,"
        " version, created_at, updated_at, active_marker)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            "900001",
            "memory",
            topic,
            f"content-{topic}-v{version}",
            5,
            "owner",
            "active" if marker else "superseded",
            version,
            1,
            1,
            marker,
        ),
    )


async def test_persona_primary_marker_unique(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """ff_personas 单主人格守卫：NULL 多行共存 + 第二 'primary' 被拒。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            # 种子：主人格 + 两个 NULL 标记人格（NULL 不入唯一键——共存）。
            await _insert_persona(cur, "p-primary", "primary")
            await _insert_persona(cur, "p-null-1", None)
            await _insert_persona(cur, "p-null-2", None)
            await cur.execute("SELECT COUNT(*) FROM ff_personas WHERE is_primary_marker IS NULL")
            null_count_row = await cur.fetchone()
            assert null_count_row is not None
            assert int(null_count_row[0]) == 2

            # 第二 'primary'：DB 级拒绝（1062 + 约束点名——并发 set_primary
            # 双写的强制面，服务层重试收敛的前置）。
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(
                    "UPDATE ff_personas SET is_primary_marker = %s WHERE name = %s",
                    ("primary", "p-null-1"),
                )
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_personas_primary" in str(exc_info.value)
    finally:
        conn.close()


async def test_curated_active_marker_composite_unique(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """ff_curated_memory 单 active 守卫：复合撞键被拒 + NULL 多代共存。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await _insert_curated(cur, "topic-x", "active", version=1)

            # 同 (persona, kind, topic) 第二条 active：复合唯一键拒绝。
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await _insert_curated(cur, "topic-x", "active", version=2)
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_curated_active" in str(exc_info.value)

            # superseded（NULL 标记）多代共存——唯一键忽略 NULL。
            await _insert_curated(cur, "topic-x", None, version=3)
            await _insert_curated(cur, "topic-x", None, version=4)
            await cur.execute(
                "SELECT COUNT(*) FROM ff_curated_memory"
                " WHERE topic = %s AND active_marker IS NULL"
                " AND status = 'superseded'",
                ("topic-x",),
            )
            superseded_row = await cur.fetchone()
            assert superseded_row is not None
            assert int(superseded_row[0]) == 2
    finally:
        conn.close()


async def test_chat_message_variant_unique(migrated_scratch: SeekDbTestTarget) -> None:
    """ff_chat_messages 变体身份守卫：同 (会话, seq, 父回合, 序号) 被唯一键拒绝。

    迁移 0010 后复合唯一键 ``uq_ff_chat_messages_seq_variant`` 的守卫面
    是**变体身份**：正典行两列 NULL（唯一键忽略 NULL——正典 seq 唯一性
    由服务层会话行锁计数维持），变体行共享父回合 seq 但 (父回合, 序号)
    不同——同父同序号重复插入即 1062。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            # 会话头 + 正典父回合（列形来自迁移 0004 + 0010）。
            await cur.execute(
                "INSERT INTO ff_chat_sessions"
                " (persona_id, title, message_count, last_active_at, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                ("900001", "variant-guard-session", 2, 1, 1, 1),
            )
            await cur.execute("SELECT LAST_INSERT_ID()")
            session_row = await cur.fetchone()
            assert session_row is not None
            session_id = int(session_row[0])

            message_insert = (
                "INSERT INTO ff_chat_messages"
                " (session_id, seq, role, content, ts_ms, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            )
            await cur.execute(message_insert, (session_id, 2, "assistant", "父回合", 1, 1, 1))
            await cur.execute("SELECT LAST_INSERT_ID()")
            parent_row = await cur.fetchone()
            assert parent_row is not None
            parent_id = int(parent_row[0])

            variant_insert = (
                "INSERT INTO ff_chat_messages"
                " (session_id, seq, role, content, ts_ms, created_at, updated_at,"
                " variant_of_id, variant_index)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            await cur.execute(
                variant_insert, (session_id, 2, "assistant", "候选一", 1, 1, 1, parent_id, 1)
            )
            # 同 (session, seq, 父回合, 序号)：变体身份唯一键拒绝。
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(
                    variant_insert,
                    (session_id, 2, "assistant", "候选一重复", 1, 1, 1, parent_id, 1),
                )
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_chat_messages_seq_variant" in str(exc_info.value)

            # 正控：同父不同序号 / 同 seq 的正典行（两列 NULL）合法共存。
            await cur.execute(
                variant_insert, (session_id, 2, "assistant", "候选二", 1, 1, 1, parent_id, 2)
            )
            await cur.execute(message_insert, (session_id, 2, "assistant", "正典重复", 1, 1, 1))
    finally:
        conn.close()


async def test_persona_facts_supersede_takeover_unique(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """ff_persona_facts 更正腾位写序：并发双写仅一条 active（1062 收敛）。

    两个连接各自执行服务层的更正写序（NULL 守卫标记插入 → 旧行
    superseded / 标记清空 → 新行取 ``'active'`` 标记）。先提交者的
    active 行占位，后提交者的取标记语句撞
    ``uq_ff_persona_facts_active``——错误码 1062 且消息点名约束；
    终态只允许一条 active 行。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    persona_id = "900001"
    category = "preference"
    normalized_hash = "hash-facts-takeover"
    insert_sql = (
        "INSERT INTO ff_persona_facts"
        " (persona_id, category, content, normalized_hash, confidence, status,"
        " provenance, source, evidence, version, created_at, updated_at, active_marker)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    seed_conn = await support.connect_scratch(migrated_scratch)
    try:
        async with seed_conn.cursor() as cur:
            await cur.execute(
                insert_sql,
                (
                    persona_id,
                    category,
                    "旧事实",
                    normalized_hash,
                    0.5,
                    "active",
                    "owner",
                    "manual",
                    "[]",
                    1,
                    1,
                    1,
                    "active",
                ),
            )
            await cur.execute("SELECT LAST_INSERT_ID()")
            old_row = await cur.fetchone()
            assert old_row is not None
            old_id = int(old_row[0])
    finally:
        seed_conn.close()

    conn_a = await support.connect_scratch(migrated_scratch, autocommit=False)
    conn_b = await support.connect_scratch(migrated_scratch, autocommit=False)
    try:
        async with conn_a.cursor() as cur_a:
            await cur_a.execute("SET SESSION ob_trx_lock_timeout = 2000000")
            await cur_a.execute(
                insert_sql,
                (
                    persona_id,
                    category,
                    "新事实 A",
                    normalized_hash,
                    0.9,
                    "active",
                    "agent",
                    "correction",
                    "[]",
                    2,
                    1,
                    1,
                    None,
                ),
            )
            await cur_a.execute("SELECT LAST_INSERT_ID()")
            new_a_row = await cur_a.fetchone()
            assert new_a_row is not None
            new_a_id = int(new_a_row[0])

        async with conn_b.cursor() as cur_b:
            await cur_b.execute("SET SESSION ob_trx_lock_timeout = 2000000")
            await cur_b.execute(
                insert_sql,
                (
                    persona_id,
                    category,
                    "新事实 B",
                    normalized_hash,
                    0.8,
                    "active",
                    "agent",
                    "correction",
                    "[]",
                    2,
                    1,
                    1,
                    None,
                ),
            )
            await cur_b.execute("SELECT LAST_INSERT_ID()")
            new_b_row = await cur_b.fetchone()
            assert new_b_row is not None
            new_b_id = int(new_b_row[0])

        # A 完成腾位 + 取标记后提交；B 的事务在 A 提交前已插入 NULL 标记行
        # （唯一键忽略 NULL——两行共存），取标记时撞 A 的已提交 active 行。
        async with conn_a.cursor() as cur_a:
            await cur_a.execute(
                "UPDATE ff_persona_facts SET status = %s, active_marker = NULL WHERE id = %s",
                ("superseded", old_id),
            )
            await cur_a.execute(
                "UPDATE ff_persona_facts SET active_marker = %s WHERE id = %s",
                ("active", new_a_id),
            )
        await conn_a.commit()

        async with conn_b.cursor() as cur_b:
            await cur_b.execute(
                "UPDATE ff_persona_facts SET status = %s, active_marker = NULL WHERE id = %s",
                ("superseded", old_id),
            )
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur_b.execute(
                    "UPDATE ff_persona_facts SET active_marker = %s WHERE id = %s",
                    ("active", new_b_id),
                )
                await conn_b.commit()
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_persona_facts_active" in str(exc_info.value)
        await conn_b.rollback()
    finally:
        conn_a.close()
        conn_b.close()

    verify_conn = await support.connect_scratch(migrated_scratch)
    try:
        async with verify_conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM ff_persona_facts"
                " WHERE persona_id = %s AND normalized_hash = %s AND active_marker = %s",
                (persona_id, normalized_hash, "active"),
            )
            active_row = await cur.fetchone()
            assert active_row is not None
            assert int(active_row[0]) == 1
            await cur.execute(
                "SELECT id FROM ff_persona_facts"
                " WHERE persona_id = %s AND normalized_hash = %s AND active_marker = %s",
                (persona_id, normalized_hash, "active"),
            )
            active_id_row = await cur.fetchone()
            assert active_id_row is not None
            assert int(active_id_row[0]) == new_a_id
            await cur.execute(
                "SELECT status FROM ff_persona_facts WHERE id = %s", (old_id,)
            )
            old_status_row = await cur.fetchone()
            assert old_status_row is not None
            assert str(old_status_row[0]) == "superseded"
    finally:
        verify_conn.close()


async def test_persona_skill_slug_unique(migrated_scratch: SeekDbTestTarget) -> None:
    """ff_persona_skills 同人格同 slug 守卫：第二条写入被 1062 拒绝。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    insert_sql = (
        "INSERT INTO ff_persona_skills"
        " (persona_id, name, slug, description, triggers, content, status,"
        " source, confidence, use_count, last_used_ms, version, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                insert_sql,
                (
                    "900001",
                    "周报流程",
                    "weekly-report",
                    "写周报时使用",
                    "[]",
                    "1. 汇总本周进展",
                    "active",
                    "manual",
                    0.8,
                    0,
                    None,
                    1,
                    1,
                    1,
                ),
            )
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(
                    insert_sql,
                    (
                        "900001",
                        "周报流程重复",
                        "weekly-report",
                        "写周报时使用",
                        "[]",
                        "1. 汇总本周进展",
                        "active",
                        "manual",
                        0.7,
                        0,
                        None,
                        1,
                        1,
                        1,
                    ),
                )
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_persona_skills_slug" in str(exc_info.value)

            await cur.execute(
                insert_sql,
                (
                    "900002",
                    "周报流程",
                    "weekly-report",
                    "他人格同名技能",
                    "[]",
                    "1. 汇总本周进展",
                    "active",
                    "manual",
                    0.8,
                    0,
                    None,
                    1,
                    1,
                    1,
                ),
            )
            await cur.execute(
                "SELECT COUNT(*) FROM ff_persona_skills"
                " WHERE persona_id = %s AND slug = %s",
                ("900001", "weekly-report"),
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            assert int(count_row[0]) == 1
    finally:
        conn.close()


async def test_core_memory_block_key_unique(migrated_scratch: SeekDbTestTarget) -> None:
    """ff_core_memory_blocks 同人格同块守卫：第二条写入被 1062 拒绝。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    insert_sql = (
        "INSERT INTO ff_core_memory_blocks"
        " (persona_id, block_key, content, locked, version, updated_by,"
        " last_edited_session_id, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                insert_sql,
                ("900001", "self_identity", "我是大肥鱼", 0, 1, "owner", None, 1, 1),
            )
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(
                    insert_sql,
                    ("900001", "self_identity", "重复块", 0, 1, "owner", None, 1, 1),
                )
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_core_memory_blocks_key" in str(exc_info.value)

            await cur.execute(
                insert_sql,
                ("900002", "self_identity", "他人格自我认知", 0, 1, "owner", None, 1, 1),
            )
            await cur.execute(
                "SELECT COUNT(*) FROM ff_core_memory_blocks"
                " WHERE persona_id = %s AND block_key = %s",
                ("900001", "self_identity"),
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            assert int(count_row[0]) == 1
    finally:
        conn.close()


async def test_sticker_persona_sha_unique(migrated_scratch: SeekDbTestTarget) -> None:
    """ff_stickers 同人格同哈希守卫：第二条写入被 1062 拒绝且点名约束。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    insert_sql = (
        "INSERT INTO ff_stickers"
        " (persona_id, sha256, content_type, storage_path, size_bytes, tags,"
        " description, source_note, annotation_status, annotation_error,"
        " annotation_model_entry, annotated_at_ms, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = (
        "900001",
        "sha-sticker-guard",
        "image/png",
        "data/stickers/aa/sha-sticker-guard.png",
        7,
        "[]",
        "",
        "自绘素材",
        "pending",
        None,
        None,
        None,
        1,
        1,
    )
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await cur.execute(insert_sql, values)
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(insert_sql, values)
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_stickers_persona_sha" in str(exc_info.value)

            # 人格隔离正控：同哈希在另一人格下可共存。
            await cur.execute(insert_sql, ("900002", *values[1:]))
            await cur.execute(
                "SELECT COUNT(*) FROM ff_stickers"
                " WHERE persona_id = %s AND sha256 = %s",
                ("900001", "sha-sticker-guard"),
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            assert int(count_row[0]) == 1
    finally:
        conn.close()


async def test_voiceprint_sample_persona_sender_sha_unique(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """ff_voiceprint_samples 同人格同发送者同样本哈希守卫：1062 且点名约束。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    insert_sql = (
        "INSERT INTO ff_voiceprint_samples"
        " (persona_id, sender_id, sample_path, sample_sha256, duration_ms,"
        " sample_rate_hz, embedding, embedding_dim, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = (
        "900001",
        "user-1",
        "data/voiceprints/aa/sha-voiceprint-guard.wav",
        "sha-voiceprint-guard",
        2000,
        16000,
        "[1.0, 0.0]",
        2,
        1,
        1,
    )
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await cur.execute(insert_sql, values)
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(insert_sql, values)
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_voiceprint_samples_persona_sender_sha" in str(exc_info.value)

            # 发送者隔离正控：同哈希在另一发送者下可共存。
            await cur.execute(insert_sql, (values[0], "user-2", *values[2:]))
            await cur.execute(
                "SELECT COUNT(*) FROM ff_voiceprint_samples"
                " WHERE persona_id = %s AND sender_id = %s AND sample_sha256 = %s",
                ("900001", "user-1", "sha-voiceprint-guard"),
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            assert int(count_row[0]) == 1
    finally:
        conn.close()



#: 跨连接表情收敛用例的固定载荷（字节内容不参与图片解码——只作哈希原料）。
_STICKER_CONFLICT_PAYLOAD = b"\x89PNG\r\n\x1a\nrr-sticker-guard"


def _sticker_service_engine(target: SeekDbTestTarget) -> AsyncEngine:
    """按 scratch 目标构造真实 MySQL 异步引擎（服务层会话面）。

    Args:
        target: 已升级 head 的 scratch 库目标。

    Returns:
        未连接的异步引擎（可重复读隔离级别——显式锁定读用例的事务形态；
        连接惰性建立，关闭归调用方 dispose）。
    """
    config = DatabaseConfig(
        type="mysql",
        host=target.host,
        port=target.port,
        name=target.database,
        user=target.user,
        password=SecretStr(target.password),
    )
    return create_async_engine(
        config.async_sqlalchemy_url, isolation_level="REPEATABLE READ"
    )


async def test_sticker_conflict_across_connections_uses_current_read(
    migrated_scratch: SeekDbTestTarget, tmp_path: Path
) -> None:
    """跨连接同哈希：RR 旧快照下锁定读仍解析胜者（单行 + 文件归属收敛）。

    连接 B 在连接 A 提交前建立可重复读读视图；A 提交后 B 的同哈希插入被
    ``uq_ff_stickers_persona_sha`` 拒绝。服务层的胜者解析若为普通快照读，
    会落入 B 的旧读视图而返回 ``None``；``SELECT ... FOR UPDATE`` 当前读
    恒见 A 的已提交行——返回胜者记录、保留胜者文件、删除败者写入的文件。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
        tmp_path: 文件落盘临时根（胜者 / 败者路径按 MIME 扩展名区分）。
    """
    digest = hashlib.sha256(_STICKER_CONFLICT_PAYLOAD).hexdigest()
    engine = _sticker_service_engine(migrated_scratch)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = StickerService(settings=StickerSettings(enabled=True), storage_root=tmp_path)
    try:
        async with bind_persona_context("910001"):
            async with factory() as winner_session, factory() as loser_session:
                # B 的读视图建立于 A 提交之前（旧快照看不到稍后提交的胜者行）。
                await loser_session.execute(text("SELECT 1"))
                winner = await service._insert_sticker(
                    winner_session,
                    digest=digest,
                    content_type="image/png",
                    payload=_STICKER_CONFLICT_PAYLOAD,
                    tags=[],
                    description="并发胜者",
                    source_note="跨连接胜者",
                )
                assert winner.created is True
                assert winner.row is not None
                await winner_session.commit()
                loser = await service._insert_sticker(
                    loser_session,
                    digest=digest,
                    content_type="image/webp",
                    payload=_STICKER_CONFLICT_PAYLOAD,
                    tags=[],
                    description="并发落败",
                    source_note="跨连接落败",
                )
                assert loser.created is False
                assert loser.row is not None
                assert loser.row.id == winner.row.id
                assert winner.path.exists()
                assert not loser.path.exists()
                await loser_session.rollback()
            async with factory() as verify_session:
                count = (
                    await verify_session.execute(select(func.count()).select_from(StickerRow))
                ).scalar_one()
                rows = list(
                    (await verify_session.execute(select(StickerRow))).scalars().all()
                )
                assert int(count) == 1
                assert len(rows) == 1
                assert rows[0].storage_path == str(winner.path)
                assert rows[0].content_type == "image/png"
    finally:
        await engine.dispose()


async def test_dream_run_single_flight_marker_unique(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """ff_dream_runs 单飞守卫：同人格第二运行中行被拒，清标记后可再运行。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    insert_sql = (
        "INSERT INTO ff_dream_runs"
        " (persona_id, status, started_ms, finished_ms, stats, decisions,"
        " error, created_at, updated_at, running_marker)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = ("900001", "running", 1, None, "{}", "[]", None, 1, 1, "1")
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            await cur.execute(insert_sql, values)
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(insert_sql, values)
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_dream_runs_persona_running" in str(exc_info.value)
            await cur.execute(
                "SELECT COUNT(*) FROM ff_dream_runs"
                " WHERE persona_id = %s AND running_marker = %s",
                ("900001", "1"),
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            assert int(count_row[0]) == 1

            # 终态清标记（NULL 不入唯一键）后下一轮运行可插入。
            await cur.execute(
                "UPDATE ff_dream_runs SET status = %s, finished_ms = %s,"
                " running_marker = NULL WHERE persona_id = %s",
                ("success", 2, "900001"),
            )
            await cur.execute(insert_sql, values)
            await cur.execute(
                "SELECT COUNT(*) FROM ff_dream_runs WHERE persona_id = %s",
                ("900001",),
            )
            total_row = await cur.fetchone()
            assert total_row is not None
            assert int(total_row[0]) == 2
    finally:
        conn.close()


async def test_with_for_update_cross_connection_lock(
    migrated_scratch: SeekDbTestTarget,
) -> None:
    """跨连接 SELECT ... FOR UPDATE：A 持锁 → B 等锁超时（1205）→ 释放正控。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    lock_row_name = "p-lock-row"

    # 种子行（autocommit 连接——语句即事务落库，锁实验有行可锁）。
    seed_conn = await support.connect_scratch(migrated_scratch)
    try:
        async with seed_conn.cursor() as cur:
            await _insert_persona(cur, lock_row_name, None)
    finally:
        seed_conn.close()

    conn_a = await support.connect_scratch(migrated_scratch, autocommit=False)
    conn_b = await support.connect_scratch(migrated_scratch, autocommit=False)
    try:
        # 连接 A：显式事务内 SELECT FOR UPDATE——持有目标行锁。
        async with conn_a.cursor() as cur_a:
            await cur_a.execute(
                "SELECT id FROM ff_personas WHERE name = %s FOR UPDATE",
                (lock_row_name,),
            )
            locked_rows = await cur_a.fetchall()
            assert locked_rows  # 行存在且锁已持有

        # 连接 B：等锁超时（错误 1205）。方言事实（live 实测）：本引擎
        # innodb_lock_wait_timeout 为 mock 变量（设置无效），生效旋钮是
        # OceanBase 原生 ob_trx_lock_timeout（微秒）——两者都设。
        async with conn_b.cursor() as cur_b:
            await cur_b.execute("SET SESSION innodb_lock_wait_timeout = 2")
            await cur_b.execute(
                f"SET SESSION ob_trx_lock_timeout = {_LOCK_WAIT_SECONDS * 1_000_000}"
            )
            with pytest.raises(aiomysql.OperationalError) as exc_info:
                await cur_b.execute(
                    "SELECT id FROM ff_personas WHERE name = %s FOR UPDATE",
                    (lock_row_name,),
                )
            assert exc_info.value.args[0] == 1205

        # A 回滚即释放行锁（释放正控的前置）。
        await conn_a.rollback()
    finally:
        conn_a.close()
        conn_b.close()

    # 释放正控（独立连接——不依赖 B 超时后的事务状态）：新连接取锁成功
    # ——证明确为行锁持有而非超时巧合。
    reacquire_conn = await support.connect_scratch(migrated_scratch, autocommit=False)
    try:
        async with reacquire_conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM ff_personas WHERE name = %s FOR UPDATE",
                (lock_row_name,),
            )
            reacquired_rows = await cur.fetchall()
            assert reacquired_rows
        await reacquire_conn.rollback()
    finally:
        reacquire_conn.close()
