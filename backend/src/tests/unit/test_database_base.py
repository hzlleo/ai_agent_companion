"""harness/database base 测试——Mixin 列契约 + PersonaMixin 自动注入/过滤/守卫。

使用共享 DB fixture（会话级 SQLite StaticPool 引擎 + 每测试自动清表，
见 tests/conftest.py）；人格上下文经 ``bind_context`` 绑定（contextvars
传播语义与入口层一致）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from packages.harness.core.context import bind_context, persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.database.base import (
    PERSONA_FILTER_SKIP_OPTION,
    Base,
    BigIntIdMixin,
    PersonaMixin,
    TimestampMixin,
)
from tests.db_models import PersonaNoteRow, PlainCounterRow


async def test_mixin_column_contract() -> None:
    """Mixin 列契约：BigInt 主键 / epoch-ms 时间戳 / persona_id 索引列。"""
    note_table = PersonaNoteRow.__table__
    assert note_table.name == "test_persona_note"
    assert "persona_id" in note_table.columns
    assert "created_at" in note_table.columns
    assert "updated_at" in note_table.columns
    assert "id" in note_table.columns
    # persona_id 带索引（隔离过滤高频列）
    persona_columns = [index.name for index in note_table.indexes]
    assert any("persona_id" in name for name in persona_columns)


async def test_mixin_isinstance_contract() -> None:
    """Mixin 组合关系：测试行同时是三个 Mixin 的实例。"""
    row = PersonaNoteRow(content="x", persona_id="p1")
    assert isinstance(row, (BigIntIdMixin, TimestampMixin, PersonaMixin))
    assert PlainCounterRow.__tablename__ == "test_plain_counter"


async def test_timestamp_defaults_epoch_ms(db_session) -> None:
    """时间戳默认值：epoch 毫秒整数（created/updated 自动填充）。"""
    before = now_ms()
    async with bind_context(persona_id="p-ts"):
        row = PersonaNoteRow(content="ts")
        db_session.add(row)
        await db_session.commit()
    after = now_ms()
    assert row.id is not None
    assert before <= row.created_at <= after
    assert before <= row.updated_at <= after


async def test_persona_id_auto_injected_on_flush(db_session) -> None:
    """写路径守卫：新实例缺 persona_id 时从上下文自动注入。"""
    async with bind_context(persona_id="p-auto"):
        row = PersonaNoteRow(content="auto")
        db_session.add(row)
        await db_session.commit()
        assert row.persona_id == "p-auto"


async def test_persona_id_explicit_value_preserved(db_session) -> None:
    """显式指定的 persona_id 原样保留（跨人格系统写入合法通道）。"""
    async with bind_context(persona_id="p-current"):
        row = PersonaNoteRow(content="explicit", persona_id="p-other")
        db_session.add(row)
        await db_session.commit()
        assert row.persona_id == "p-other"


async def test_persona_insert_without_context_fails_loud(db_session) -> None:
    """写路径守卫：无人格上下文的 INSERT → PERSONA_CONTEXT_REQUIRED。"""
    row = PersonaNoteRow(content="no-ctx")
    db_session.add(row)
    with pytest.raises(BusinessException) as exc_info:
        await db_session.commit()
    assert exc_info.value.code == ErrorCode.PERSONA_CONTEXT_REQUIRED
    await db_session.rollback()


async def test_persona_select_auto_filters_current_persona(db_session) -> None:
    """读路径守卫：SELECT 自动过滤为当前上下文的人格行。"""
    # 两个人格各写两行
    for persona in ("p-a", "p-b"):
        async with bind_context(persona_id=persona):
            for i in range(2):
                db_session.add(PersonaNoteRow(content=f"{persona}-{i}"))
            await db_session.commit()

    async with bind_context(persona_id="p-a"):
        rows = (await db_session.execute(select(PersonaNoteRow))).scalars().all()
        assert {row.persona_id for row in rows} == {"p-a"}
        assert len(rows) == 2

    async with bind_context(persona_id="p-b"):
        rows = (await db_session.execute(select(PersonaNoteRow))).scalars().all()
        assert {row.persona_id for row in rows} == {"p-b"}


async def test_persona_contextvars_propagation_isolates_concurrent_tasks(
    shared_db_engine,
) -> None:
    """contextvars 传播：并发任务各持各的人格上下文（ContextVar 复制语义）。

    每任务独立 Session（AsyncSession 非并发安全）；共享引擎的 StaticPool
    单连接使写入串行化，断言只看上下文隔离性。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=shared_db_engine, expire_on_commit=False)

    async def _write_as(persona: str) -> None:
        async with bind_context(persona_id=persona), factory() as session:
            session.add(PersonaNoteRow(content=persona))
            await session.commit()

    async with bind_context(persona_id="p-outer"):
        async with factory() as session:
            session.add(PersonaNoteRow(content="outer"))
            await session.commit()

        await asyncio.gather(_write_as("p-task1"), _write_as("p-task2"))

    # 外层上下文不受任务影响（作用域退出恢复）
    assert persona_id_var.get() is None

    async with bind_context(persona_id="p-outer"), factory() as session:
        rows = (await session.execute(select(PersonaNoteRow))).scalars().all()
        assert {row.content for row in rows} == {"outer"}

    async with bind_context(persona_id="p-task1"), factory() as session:
        rows = (await session.execute(select(PersonaNoteRow))).scalars().all()
        assert {row.content for row in rows} == {"p-task1"}


async def test_persona_select_without_context_fails_loud(db_session) -> None:
    """读路径守卫：无上下文的人格作用域 SELECT → PERSONA_CONTEXT_REQUIRED。"""
    async with bind_context(persona_id="p-x"):
        db_session.add(PersonaNoteRow(content="seed"))
        await db_session.commit()

    with pytest.raises(BusinessException) as exc_info:
        await db_session.execute(select(PersonaNoteRow))
    assert exc_info.value.code == ErrorCode.PERSONA_CONTEXT_REQUIRED


async def test_persona_filter_skip_option_bypasses(db_session) -> None:
    """绕过通道：skip_persona_filter execution option 跨人格全量查询。"""
    for persona in ("p-a", "p-b"):
        async with bind_context(persona_id=persona):
            db_session.add(PersonaNoteRow(content=persona))
            await db_session.commit()

    # 无上下文 + skip 选项 → 全量可见（管理面语义）
    rows = (
        (
            await db_session.execute(
                select(PersonaNoteRow).execution_options(**{PERSONA_FILTER_SKIP_OPTION: True})
            )
        )
        .scalars()
        .all()
    )
    assert {row.persona_id for row in rows} == {"p-a", "p-b"}


async def test_plain_table_unaffected_by_persona_guards(db_session) -> None:
    """非人格作用域表：无上下文读写零影响（守卫只作用于 PersonaMixin 实体）。"""
    # 无人格上下文写入非人格表 → 正常
    db_session.add(PlainCounterRow(label="x"))
    await db_session.commit()
    # 无人格上下文查询非人格表 → 正常（不触发守卫）
    count = (
        await db_session.execute(select(func.count()).select_from(PlainCounterRow))
    ).scalar_one()
    assert count == 1


async def test_count_aggregate_also_filtered(db_session) -> None:
    """聚合查询同样被自动过滤（with_loader_criteria 覆盖聚合面）。"""
    async with bind_context(persona_id="p-agg"):
        for i in range(3):
            db_session.add(PersonaNoteRow(content=f"n{i}"))
        await db_session.commit()
        total = (
            await db_session.execute(select(func.count()).select_from(PersonaNoteRow))
        ).scalar_one()
        assert total == 3

    async with bind_context(persona_id="p-empty"):
        total = (
            await db_session.execute(select(func.count()).select_from(PersonaNoteRow))
        ).scalar_one()
        assert total == 0


async def test_base_metadata_contains_test_tables() -> None:
    """Base.metadata 收集全部继承模型（Alembic autogenerate 基准面）。"""
    table_names = set(Base.metadata.tables)
    assert "test_persona_note" in table_names
    assert "test_plain_counter" in table_names
