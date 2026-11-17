"""并发不变式真引擎验证（DB 级约束 + 行锁——服务层重试收敛的强制面）。

四个不变式（迁移 0007/0008 落地的 DB 级守卫 + InnoDB 行锁语义，
全部经 raw SQL 直击真引擎——SQLite 单测面无法验证的并发拒绝行为）：

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
      证明确为行锁持有而非超时巧合）。

引擎方言事实（live 实测 v1.4.0，锁定进断言）：唯一键冲突 =
pymysql ``IntegrityError``（1062，消息携带约束名）；行锁等待上限的
生效旋钮是 OceanBase 原生 ``ob_trx_lock_timeout``（微秒）——
``innodb_lock_wait_timeout`` 在本引擎为 mock 变量（读回恒 50、
``SET SESSION`` 无效），两者都设以保留语义声明与实际生效。
"""

from __future__ import annotations

import aiomysql
import pytest

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


async def test_chat_message_seq_unique(migrated_scratch: SeekDbTestTarget) -> None:
    """ff_chat_messages 会话内序号守卫：撞序被 uq_ff_chat_messages_seq 拒绝。

    Args:
        migrated_scratch: 已升级 head 的 scratch 库目标。
    """
    conn = await support.connect_scratch(migrated_scratch)
    try:
        async with conn.cursor() as cur:
            # 会话头 + 首条消息（列形来自迁移 0004）。
            await cur.execute(
                "INSERT INTO ff_chat_sessions"
                " (persona_id, title, message_count, last_active_at, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                ("900001", "seq-guard-session", 2, 1, 1, 1),
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
            await cur.execute(message_insert, (session_id, 1, "user", "m1", 1, 1, 1))

            # 同 (session_id, seq)：并发 append 的 DB 级兜底拒绝。
            with pytest.raises(aiomysql.IntegrityError) as exc_info:
                await cur.execute(message_insert, (session_id, 1, "user", "m2", 1, 1, 1))
            assert exc_info.value.args[0] == 1062
            assert "uq_ff_chat_messages_seq" in str(exc_info.value)

            # 异序共存的正控（seq=2 合法插入）。
            await cur.execute(message_insert, (session_id, 2, "user", "m3", 1, 1, 1))
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
