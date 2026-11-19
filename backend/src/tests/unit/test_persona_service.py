"""harness/persona service 测试——注册 CRUD / 事务性主人格切换 / 复制 /
状态层 / 删除守卫 / 批量删除 / 游标分页 / 上下文绑定 / 可寻址集 /
主人格并发收敛。

引擎经共享会话级 SQLite fixture（StaticPool 单连接）；Session 由
``db_session`` fixture 注入（服务层事务边界——显式 commit / 异常
rollback），engine holder 槽位外部安装（owner_token=None，生命周期归
共享 fixture）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from packages.harness.core.context import current_context, persona_id_var
from packages.harness.core.dtos import PageResponse
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.base import PERSONA_FILTER_SKIP_OPTION, Base
from packages.harness.database.engine import db_engine_holder
from packages.harness.memory.dreaming_orm import DreamRunRow
from packages.harness.memory.models import ChatTurnPayloadModel, MemoryProvenance
from packages.harness.memory.orm import (
    ChatMessageRow,
    ChatSessionRow,
    CuratedMemoryRow,
    MemoryEpisodeRow,
)
from packages.harness.memory.retrieval import MemoryIndexer
from packages.harness.memory.retrieval_models import MemoryRefType
from packages.harness.memory.sqlite_store import MemoryIndexLiteRow
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.core_memory_orm import (
    CoreMemoryBlockRow,
    CoreMemoryRevisionRow,
)
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.facts_orm import PersonaFactRow
from packages.harness.persona.models import PersonaStateLayer, PersonaStatus
from packages.harness.persona.orm import PERSONA_PRIMARY_MARKER, PersonaRow, PersonaStateRow
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills_orm import PersonaSkillRow


@pytest.fixture
async def persona_service(shared_db_engine: AsyncEngine) -> AsyncIterator[PersonaService]:
    """人格服务（engine holder 外部安装到共享引擎；用后复位槽位）。

    Args:
        shared_db_engine: 会话级共享 SQLite 引擎（conftest 提供）。

    Yields:
        :class:`PersonaService` 实例（Session 逐调用注入，服务本身无状态）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None  # 外部安装——close 不 dispose 共享引擎
    yield PersonaService()
    db_engine_holder.reset()


def _create_dto(
    *,
    name: str = "feiyu",
    status: PersonaStatus = PersonaStatus.DISABLED,
) -> PersonaCreateDTO:
    """构造创建 DTO（最小卡片 + 可选状态）。"""
    return PersonaCreateDTO(
        name=name,
        status=status,
        card=CharaCardModel(
            name="大肥鱼",
            personality="温柔爱操心",
            first_mes="你回来啦！",
        ),
    )


# ---------------------------------------------------------------------------
# 创建 / 读取
# ---------------------------------------------------------------------------


async def test_create_returns_record_with_defaults(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """创建：主键分配 + status 缺省 disabled + 展示名回落 card.name。"""
    record = await persona_service.create(db_session, _create_dto())
    assert record.id >= 1
    assert record.name == "feiyu"
    assert record.display_name == "大肥鱼"  # 缺省回落卡片名
    assert record.status == PersonaStatus.DISABLED
    assert record.is_primary is False
    assert record.card.first_mes == "你回来啦！"
    assert record.created_at > 0


async def test_create_persists_card_as_json(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """落库物理校验：chara_card 列为 JSON 文档（dict 形态）。"""
    record = await persona_service.create(db_session, _create_dto())
    row = await db_session.get(PersonaRow, record.id)
    assert row is not None
    assert isinstance(row.chara_card, dict)
    assert row.chara_card["name"] == "大肥鱼"
    assert row.status == "disabled"


async def test_create_duplicate_name_conflict(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """唯一名冲突：第二次同名创建 CONFLICT（details 携带占用方主键）。"""
    await persona_service.create(db_session, _create_dto())
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.create(db_session, _create_dto())
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.details is not None
    assert exc_info.value.details["name"] == "feiyu"


async def test_create_integrity_error_maps_conflict(
    persona_service: PersonaService, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同名竞态落 IntegrityError：归一为 CONFLICT（409 语义）。"""
    from unittest.mock import AsyncMock

    integrity = IntegrityError(
        "INSERT INTO ff_personas ...", {}, Exception("UNIQUE constraint failed: ff_personas.name")
    )
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=integrity))
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.create(db_session, _create_dto(name="race-feiyu"))
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.__cause__ is integrity
    assert exc_info.value.details is not None
    assert exc_info.value.details["name"] == "race-feiyu"


async def test_update_rename_integrity_error_maps_conflict(
    persona_service: PersonaService, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发重命名竞态落 IntegrityError：归一为 CONFLICT（409 语义）。"""
    from unittest.mock import AsyncMock

    record = await persona_service.create(db_session, _create_dto(name="rename-race"))
    integrity = IntegrityError(
        "UPDATE ff_personas ...", {}, Exception("UNIQUE constraint failed: ff_personas.name")
    )
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=integrity))
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.update(
            db_session, record.id, PersonaUpdateDTO(name="rename-target")
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.details is not None
    assert exc_info.value.details["name"] == "rename-target"


async def test_get_missing_raises_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """缺失读取：NOT_FOUND fail loud。"""
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.get(db_session, 999)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 更新（重命名 / 状态 / 卡片补丁）
# ---------------------------------------------------------------------------


async def test_update_rename_uniqueness(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """重命名：合法改名生效；改成他人占用名 CONFLICT；同名 no-op 通过。"""
    a = await persona_service.create(db_session, _create_dto(name="a"))
    b = await persona_service.create(db_session, _create_dto(name="b"))

    renamed = await persona_service.update(db_session, a.id, PersonaUpdateDTO(name="a2"))
    assert renamed.name == "a2"

    with pytest.raises(BusinessException) as exc_info:
        await persona_service.update(db_session, b.id, PersonaUpdateDTO(name="a2"))
    assert exc_info.value.code == ErrorCode.CONFLICT

    # 改回自身当前名：no-op（排除自身的主键语义）
    self_rename = await persona_service.update(db_session, a.id, PersonaUpdateDTO(name="a2"))
    assert self_rename.name == "a2"
    assert b.name == "b"


async def test_update_display_name_and_status(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """部分更新：仅出现字段生效（display_name / status）。"""
    record = await persona_service.create(db_session, _create_dto())
    updated = await persona_service.update(
        db_session,
        record.id,
        PersonaUpdateDTO(display_name="小鱼干", status=PersonaStatus.ACTIVE),
    )
    assert updated.display_name == "小鱼干"
    assert updated.status == PersonaStatus.ACTIVE
    assert updated.card.name == "大肥鱼"  # 未提供卡片 → 原样


async def test_update_card_patch_merge(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """卡片补丁：仅出现字段覆写，其余保留；世界书整体替换。"""
    record = await persona_service.create(db_session, _create_dto())
    patched = await persona_service.update(
        db_session,
        record.id,
        PersonaUpdateDTO(
            card=CharaCardPatchDTO(
                personality="元气满满",
                character_book={
                    "name": "world",
                    "entries": [{"keys": ["鱼缸"], "content": "书房鱼缸"}],
                },
            )
        ),
    )
    assert patched.card.personality == "元气满满"
    assert patched.card.first_mes == "你回来啦！"  # 未提供 → 保留
    assert patched.card.character_book is not None
    assert patched.card.character_book.entries[0].keys == ["鱼缸"]

    # 显式 null 清除世界书
    cleared = await persona_service.update(
        db_session,
        record.id,
        PersonaUpdateDTO(card=CharaCardPatchDTO.model_validate({"character_book": None})),
    )
    assert cleared.card.character_book is None


async def test_update_card_patch_invalid_merged_card(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """合并结果校验失败：VALIDATION_ERROR（防御存储态损坏——合并后整体重校验）。

    补丁本身经 DTO 边界校验合法；但行内既有卡片若被直写损坏（如缺
    name 键），合并后的整体重校验兜底拒绝（VALIDATION_ERROR fail loud）。
    """
    record = await persona_service.create(db_session, _create_dto())
    # 直写损坏存储态（绕过服务校验——模拟上游数据损坏）
    row = await db_session.get(PersonaRow, record.id)
    assert row is not None
    row.chara_card = {"description": "损坏卡片（缺 name）"}
    await db_session.commit()

    with pytest.raises(BusinessException) as exc_info:
        await persona_service.update(
            db_session,
            record.id,
            PersonaUpdateDTO(card=CharaCardPatchDTO(personality="试图修补")),
        )
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 事务性主人格切换 + 可寻址集 + 主人格读取
# ---------------------------------------------------------------------------


async def test_set_primary_switches_single_primary(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """事务性切换：设 B 为主人格必然解除 A（至多一个主人格不变式）。"""
    a = await persona_service.create(db_session, _create_dto(name="a", status=PersonaStatus.ACTIVE))
    b = await persona_service.create(db_session, _create_dto(name="b", status=PersonaStatus.ACTIVE))

    await persona_service.set_primary(db_session, a.id)
    primary = await persona_service.get_primary_persona(db_session)
    assert primary.id == a.id

    await persona_service.set_primary(db_session, b.id)
    primary_after = await persona_service.get_primary_persona(db_session)
    assert primary_after.id == b.id
    record_a = await persona_service.get(db_session, a.id)
    assert record_a.is_primary is False
    record_b = await persona_service.get(db_session, b.id)
    assert record_b.is_primary is True


async def test_set_primary_disabled_persona_conflict(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用状态人格不可设为主人格：CONFLICT。"""
    record = await persona_service.create(db_session, _create_dto())  # 默认 disabled
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.set_primary(db_session, record.id)
    assert exc_info.value.code == ErrorCode.CONFLICT


async def test_set_primary_idempotent(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """重复设置当前主人格：幂等 no-op（仍为唯一主人格）。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.set_primary(db_session, record.id)
    await persona_service.set_primary(db_session, record.id)
    primary = await persona_service.get_primary_persona(db_session)
    assert primary.id == record.id


async def test_disable_persona_lifecycle_only(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用是纯生命周期操作：status 切 disabled，不触碰主人格标记位持有者。"""
    primary = await persona_service.create(
        db_session, _create_dto(name="primary", status=PersonaStatus.ACTIVE)
    )
    other = await persona_service.create(
        db_session, _create_dto(name="other", status=PersonaStatus.ACTIVE)
    )
    await persona_service.set_primary(db_session, primary.id)

    disabled = await persona_service.disable(db_session, other.id)
    assert disabled.status == PersonaStatus.DISABLED
    assert disabled.is_primary is False
    # 主人格原样在位（停用他人不动主人格）
    primary_after = await persona_service.get_primary_persona(db_session)
    assert primary_after.id == primary.id

    # 幂等：已停用人格重复停用为 no-op
    again = await persona_service.disable(db_session, other.id)
    assert again.status == PersonaStatus.DISABLED


async def test_disable_primary_rejected(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用守卫：停用当前主人格被拒（CONFLICT，指引先另设主人格）。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.set_primary(db_session, record.id)
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.disable(db_session, record.id)
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.details is not None
    assert "set-primary" in str(exc_info.value.details.get("fix", ""))
    # 两步后可停用：另设主人格 → 停用原主人格
    other = await persona_service.create(
        db_session, _create_dto(name="other", status=PersonaStatus.ACTIVE)
    )
    await persona_service.set_primary(db_session, other.id)
    disabled = await persona_service.disable(db_session, record.id)
    assert disabled.status == PersonaStatus.DISABLED
    assert disabled.is_primary is False


async def test_update_status_disabled_on_primary_rejected(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """更新面停用守卫：update(status=disabled) 命中当前主人格同样拒绝。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.set_primary(db_session, record.id)
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.update(
            db_session, record.id, PersonaUpdateDTO(status=PersonaStatus.DISABLED)
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    # 主人格原样在位（拒绝即零副作用）
    primary = await persona_service.get_primary_persona(db_session)
    assert primary.id == record.id


async def test_get_primary_no_primary_raises_clear_error(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """无主人格（全新库）：NOT_FOUND fail loud（清晰中文错误 + fix 指引）。"""
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.get_primary_persona(db_session)
    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert "主人格" in exc_info.value.message
    assert exc_info.value.details is not None
    assert "set-primary" in str(exc_info.value.details.get("fix", ""))


async def test_list_addressable_returns_enabled_only(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """可寻址集：仅 enabled 人格（id 升序）；disabled 排除；主人格含于其中。"""
    first = await persona_service.create(
        db_session, _create_dto(name="first", status=PersonaStatus.ACTIVE)
    )
    await persona_service.create(db_session, _create_dto(name="off", status=PersonaStatus.DISABLED))
    second = await persona_service.create(
        db_session, _create_dto(name="second", status=PersonaStatus.ACTIVE)
    )
    await persona_service.set_primary(db_session, second.id)

    addressable = await persona_service.list_addressable(db_session)
    assert [record.id for record in addressable] == [first.id, second.id]
    assert all(record.status == PersonaStatus.ACTIVE for record in addressable)
    # 主人格也在可寻址集中（主人格是缺省目标，非唯一可用者）
    assert {record.id for record in addressable if record.is_primary} == {second.id}


# ---------------------------------------------------------------------------
# 主人格并发不变式（DB 唯一键 + 冲突重试收敛——镜像策展记忆回归模式）
# ---------------------------------------------------------------------------


async def test_primary_marker_unique_db_constraint(db_session: AsyncSession) -> None:
    """DB 级主人格唯一键：双 ``'primary'`` 标记行撞键；NULL 标记行共存。"""
    db_session.add(
        PersonaRow(
            name="marker-a",
            display_name="A",
            status="active",
            is_primary_marker=PERSONA_PRIMARY_MARKER,
            chara_card={"name": "A", "first_mes": "hi"},
        )
    )
    await db_session.commit()
    db_session.add(
        PersonaRow(
            name="marker-b",
            display_name="B",
            status="active",
            is_primary_marker=PERSONA_PRIMARY_MARKER,
            chara_card={"name": "B", "first_mes": "hi"},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # NULL 标记行数量不限：多行共存不撞唯一键（NULL 被忽略）
    for name in ("plain-1", "plain-2", "plain-3"):
        db_session.add(
            PersonaRow(
                name=name,
                display_name=name,
                status="active",
                is_primary_marker=None,
                chara_card={"name": name, "first_mes": "hi"},
            )
        )
    await db_session.commit()
    marker_rows = list(
        (
            await db_session.execute(
                select(PersonaRow).where(PersonaRow.is_primary_marker.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    assert len(marker_rows) == 1  # 首行标记原样；撞键行未落库
    assert len(list((await db_session.execute(select(PersonaRow))).scalars().all())) == 4


async def test_set_primary_conflict_retry_converges(
    persona_service: PersonaService, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发冲突重试：首次尝试撞主人格唯一键 → 回滚后重试收敛（镜像策展 upsert 模式）。"""
    a = await persona_service.create(db_session, _create_dto(name="a", status=PersonaStatus.ACTIVE))
    b = await persona_service.create(db_session, _create_dto(name="b", status=PersonaStatus.ACTIVE))
    await persona_service.set_primary(db_session, a.id)

    calls: list[int] = []
    original_once = PersonaService._set_primary_once

    async def conflicting_then_real(
        inner_self: PersonaService, session: AsyncSession, persona_id: int
    ) -> PersonaRecordDTO:
        """首次尝试：真实完成切换后模拟并发赢家抢先占位抛唯一键冲突。"""
        if not calls:
            calls.append(1)
            await original_once(inner_self, session, persona_id)
            raise IntegrityError(
                "UPDATE ff_personas ...",
                {},
                Exception("UNIQUE constraint failed: uq_ff_personas_primary"),
            )
        return await original_once(inner_self, session, persona_id)

    monkeypatch.setattr(PersonaService, "_set_primary_once", conflicting_then_real)
    record = await persona_service.set_primary(db_session, b.id)
    assert calls == [1]  # 首次尝试确实撞了冲突
    assert record.id == b.id
    assert record.is_primary is True

    # 重试收敛后全表恰一个主人格（a 被解除，b 在位）
    primary = await persona_service.get_primary_persona(db_session)
    assert primary.id == b.id
    record_a = await persona_service.get(db_session, a.id)
    assert record_a.is_primary is False
    marker_count = len(
        list(
            (
                await db_session.execute(
                    select(PersonaRow).where(
                        PersonaRow.is_primary_marker == PERSONA_PRIMARY_MARKER
                    )
                )
            )
            .scalars()
            .all()
        )
    )
    assert marker_count == 1


async def test_set_primary_retry_exhaustion_maps_internal_error(
    persona_service: PersonaService, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重试后仍冲突：归一 INTERNAL_ERROR（清晰中文消息——与策展同口径）。"""
    a = await persona_service.create(db_session, _create_dto(name="a", status=PersonaStatus.ACTIVE))

    async def always_conflict(
        inner_self: PersonaService, session: AsyncSession, persona_id: int
    ) -> PersonaRecordDTO:
        raise IntegrityError(
            "UPDATE ff_personas ...",
            {},
            Exception("UNIQUE constraint failed: uq_ff_personas_primary"),
        )

    monkeypatch.setattr(PersonaService, "_set_primary_once", always_conflict)
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.set_primary(db_session, a.id)
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert exc_info.value.__cause__ is not None


async def test_set_primary_concurrent_exactly_one_primary(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """并发切换收敛：两个人格并发 set_primary → 收束后全表恰一个主人格。

    私有引擎隔离（StaticPool 单连接 + 每任务独立 Session——AsyncSession
    非并发安全，单连接使写入串行化）：共享会话级引擎的连接携前序测试
    事件循环的内部锁（跨 loop 不可重入），并发面须在测试自有连接上
    运行；唯一键 + 服务层重试保证任意交错后不变式收敛。
    """
    a = await persona_service.create(db_session, _create_dto(name="a", status=PersonaStatus.ACTIVE))
    b = await persona_service.create(db_session, _create_dto(name="b", status=PersonaStatus.ACTIVE))

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)

        async def _seed(record: PersonaRecordDTO) -> None:
            async with factory() as session:
                session.add(
                    PersonaRow(
                        name=record.name,
                        display_name=record.display_name,
                        status=record.status.value,
                        is_primary_marker=None,
                        chara_card=record.card.model_dump(),
                    )
                )
                await session.commit()

        await asyncio.gather(_seed(a), _seed(b))

        async def _set_primary(persona_id: int) -> None:
            async with factory() as session:
                await persona_service.set_primary(session, persona_id)

        await asyncio.gather(_set_primary(a.id), _set_primary(b.id))

        async with factory() as session:
            marker_rows = list(
                (
                    await session.execute(
                        select(PersonaRow).where(
                            PersonaRow.is_primary_marker == PERSONA_PRIMARY_MARKER
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(marker_rows) == 1
            primary = await persona_service.get_primary_persona(session)
            assert primary.id in {a.id, b.id}
            assert marker_rows[0].id == primary.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 复制
# ---------------------------------------------------------------------------


async def test_copy_without_states(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """复制（不含状态）：新注册名 + 卡片深拷贝 + 恒 disabled/不激活 + 零状态。"""
    source = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.upsert_state(
        db_session,
        source.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "开心"}),
    )
    copied = await persona_service.copy(
        db_session, source.id, PersonaCopyDTO(new_name="feiyu-copy")
    )
    assert copied.id != source.id
    assert copied.name == "feiyu-copy"
    assert copied.status == PersonaStatus.DISABLED
    assert copied.is_primary is False
    assert copied.card == source.card
    assert await persona_service.get_states(db_session, copied.id) == []


async def test_copy_with_states_deep_copies(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """复制（含状态）：M/S 状态深拷贝到新人格（源状态保留）。"""
    source = await persona_service.create(db_session, _create_dto())
    await persona_service.upsert_state(
        db_session,
        source.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "开心", "relationship": 80}),
    )
    await persona_service.upsert_state(
        db_session,
        source.id,
        PersonaStateLayer.S,
        PersonaStateUpsertDTO(state={"topic": "晚饭", "energy": 0.6}),
    )
    copied = await persona_service.copy(
        db_session, source.id, PersonaCopyDTO(new_name="feiyu-copy", include_states=True)
    )
    states = await persona_service.get_states(db_session, copied.id)
    by_layer = {state.layer: state for state in states}
    assert set(by_layer) == {PersonaStateLayer.M, PersonaStateLayer.S}
    assert by_layer[PersonaStateLayer.M].state == {"mood": "开心", "relationship": 80}
    assert by_layer[PersonaStateLayer.S].state == {"topic": "晚饭", "energy": 0.6}
    # 源状态保留（深拷贝非移动）
    assert len(await persona_service.get_states(db_session, source.id)) == 2


async def test_copy_with_states_deep_copies_nested_structures(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """状态深拷贝嵌套结构：副本嵌套对象与源不共享——突变副本不影响源行。"""
    source = await persona_service.create(db_session, _create_dto())
    await persona_service.upsert_state(
        db_session,
        source.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(
            state={"mood": "开心", "meta": {"tags": ["温柔"], "scores": {"亲密": 80}}}
        ),
    )
    source_state = (
        (
            await db_session.execute(
                select(PersonaStateRow).where(
                    PersonaStateRow.persona_id == source.id,
                    PersonaStateRow.layer == PersonaStateLayer.M.value,
                )
            )
        )
        .scalars()
        .one()
    )
    source_nested = source_state.state["meta"]

    copied = await persona_service.copy(
        db_session, source.id, PersonaCopyDTO(new_name="feiyu-deep-copy", include_states=True)
    )
    copied_state = (
        (
            await db_session.execute(
                select(PersonaStateRow).where(
                    PersonaStateRow.persona_id == copied.id,
                    PersonaStateRow.layer == PersonaStateLayer.M.value,
                )
            )
        )
        .scalars()
        .one()
    )
    copied_nested = copied_state.state["meta"]
    assert copied_nested is not source_nested
    copied_nested["tags"].append("突变")
    copied_nested["scores"]["亲密"] = 1
    assert source_nested["tags"] == ["温柔"]
    assert source_nested["scores"]["亲密"] == 80


async def test_copy_duplicate_name_conflict(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """复制目标名占用：CONFLICT。"""
    await persona_service.create(db_session, _create_dto(name="a"))
    b = await persona_service.create(db_session, _create_dto(name="b"))
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.copy(db_session, b.id, PersonaCopyDTO(new_name="a"))
    assert exc_info.value.code == ErrorCode.CONFLICT


async def test_copy_missing_source_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """复制缺失源：NOT_FOUND。"""
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.copy(db_session, 999, PersonaCopyDTO(new_name="x"))
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 删除守卫 + 状态级联清理
# ---------------------------------------------------------------------------


async def _seed_persona_domain(db_session: AsyncSession, persona_id: int) -> tuple[int, int]:
    """为指定人格造齐记忆域七表 + 会话消息 + 回退索引行（级联测试种子）。

    Args:
        db_session: 注入的异步 Session。
        persona_id: 目标人格主键。

    Returns:
        ``(episode_id, curated_id)``——供索引清理分发断言引用。
    """
    episode = MemoryEpisodeRow(
        persona_id=str(persona_id),
        kind="chat",
        source="chat",
        content=ChatTurnPayloadModel(
            user_content="级联测试事件", assistant_content="", session_id=1
        ).model_dump(),
        importance=5,
        provenance=MemoryProvenance.OWNER.value,
        content_hash="cascade-hash",
        ts_ms=1,
    )
    curated = CuratedMemoryRow(
        persona_id=str(persona_id),
        kind="memory",
        topic="级联测试主题",
        content="级联测试策展记忆",
        importance=5,
        provenance=MemoryProvenance.OWNER.value,
        status="active",
        superseded_by_id=None,
        version=1,
        active_marker="active",
        pinned=False,
        frozen=False,
        access_count=0,
        last_accessed_ms=None,
    )
    dream_run = DreamRunRow(
        persona_id=str(persona_id),
        status="success",
        started_ms=1,
        finished_ms=2,
        stats={},
        decisions=[],
        diary_episode_id=None,
        error=None,
    )
    fact = PersonaFactRow(
        persona_id=str(persona_id),
        category="preference",
        content="级联测试事实",
        normalized_hash="cascade-fact-hash",
        confidence=0.8,
        status="active",
        provenance="owner",
        source="manual",
        evidence=[],
        superseded_by_id=None,
        version=1,
        active_marker="active",
    )
    skill = PersonaSkillRow(
        persona_id=str(persona_id),
        name="级联测试技能",
        slug="cascade-skill",
        description="级联测试技能描述",
        triggers=["测试"],
        content="1. 执行测试",
        status="active",
        source="manual",
        confidence=0.8,
        use_count=0,
        last_used_ms=None,
        version=1,
    )
    block = CoreMemoryBlockRow(
        persona_id=str(persona_id),
        block_key="self_identity",
        content="级联测试自我认知",
        locked=False,
        version=1,
        updated_by="owner",
        last_edited_session_id=None,
    )
    chat_session = ChatSessionRow(
        persona_id=str(persona_id),
        title="级联测试会话",
        message_count=1,
        last_active_at=1,
    )
    db_session.add_all(
        [episode, curated, dream_run, fact, skill, block, chat_session]
    )
    await db_session.flush()
    db_session.add(
        CoreMemoryRevisionRow(
            block_id=block.id,
            version=1,
            content="级联测试修订",
            provenance="owner",
            source="admin",
            session_id=None,
            ts_ms=1,
        )
    )
    db_session.add(
        ChatMessageRow(
            session_id=chat_session.id,
            seq=1,
            role="user",
            content="级联测试消息",
            ts_ms=1,
            meta=None,
            variant_of_id=None,
            variant_index=None,
        )
    )
    db_session.add(
        MemoryIndexLiteRow(
            persona_id=persona_id,
            ref_type=MemoryRefType.EPISODE.value,
            ref_id=episode.id,
            kind="chat",
            provenance=MemoryProvenance.OWNER.value,
            ts_ms=1,
            content="级联测试索引行",
        )
    )
    await db_session.commit()
    return episode.id, curated.id


class _RecordingIndexer:
    """记录索引删除分发的假管道（生产 SeekDB 清理面的观测替身）。"""

    def __init__(self) -> None:
        """初始化空分发记录。"""
        self.deleted: list[tuple[MemoryRefType, int, int]] = []

    async def dispatch_delete(
        self, ref_type: MemoryRefType, ref_id: int, persona_id: int
    ) -> None:
        """记录一次删除分发。"""
        self.deleted.append((ref_type, ref_id, persona_id))


async def _count_persona_domain(
    db_session: AsyncSession, persona_id: int
) -> tuple[int, int, int, int, int, int, int, int, int]:
    """统计单人格的记忆域行数（固定顺序便于级联断言）。

    Args:
        db_session: 注入的异步 Session。
        persona_id: 目标人格主键。

    Returns:
        九元组：episodes / curated / dream_runs / facts / skills /
        core_blocks / core_revisions / chat_sessions / chat_messages。
    """
    skip_filter = {PERSONA_FILTER_SKIP_OPTION: True}
    tables = (
        MemoryEpisodeRow,
        CuratedMemoryRow,
        DreamRunRow,
        PersonaFactRow,
        PersonaSkillRow,
        CoreMemoryBlockRow,
    )
    counts: list[int] = []
    for model in tables:
        value = (
            await db_session.execute(
                select(func.count())
                .select_from(model)
                .where(model.persona_id == str(persona_id)),
                execution_options=skip_filter,
            )
        ).scalar_one()
        counts.append(int(value))
    block_ids = select(CoreMemoryBlockRow.id).where(
        CoreMemoryBlockRow.persona_id == str(persona_id)
    )
    revision_count = (
        await db_session.execute(
            select(func.count())
            .select_from(CoreMemoryRevisionRow)
            .where(CoreMemoryRevisionRow.block_id.in_(block_ids)),
            execution_options=skip_filter,
        )
    ).scalar_one()
    session_ids = select(ChatSessionRow.id).where(
        ChatSessionRow.persona_id == str(persona_id)
    )
    chat_session_count = (
        await db_session.execute(
            select(func.count())
            .select_from(ChatSessionRow)
            .where(ChatSessionRow.persona_id == str(persona_id)),
            execution_options=skip_filter,
        )
    ).scalar_one()
    message_count = (
        await db_session.execute(
            select(func.count())
            .select_from(ChatMessageRow)
            .where(ChatMessageRow.session_id.in_(session_ids)),
            execution_options=skip_filter,
        )
    ).scalar_one()
    counts.extend([int(revision_count), int(chat_session_count), int(message_count)])
    return (
        counts[0],
        counts[1],
        counts[2],
        counts[3],
        counts[4],
        counts[5],
        counts[6],
        counts[7],
        counts[8],
    )


async def test_delete_cascades_entire_memory_domain(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """删除人格级联：A 的记忆域七表 / 会话消息 / 索引全空，B 原样保留。

    七表 = episodes / curated / dream_runs / persona_facts /
    persona_skills / core_memory_blocks / core_memory_revisions；另含
    chat_sessions / chat_messages 与回退检索索引行。二次删除走既有
    NOT_FOUND 语义（幂等失败面）。
    """
    persona_a = await persona_service.create(db_session, _create_dto(name="cascade-a"))
    persona_b = await persona_service.create(db_session, _create_dto(name="cascade-b"))
    await _seed_persona_domain(db_session, persona_a.id)
    await _seed_persona_domain(db_session, persona_b.id)

    await persona_service.delete(db_session, persona_a.id)

    assert await _count_persona_domain(db_session, persona_a.id) == (0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert await _count_persona_domain(db_session, persona_b.id) == (1, 1, 1, 1, 1, 1, 1, 1, 1)
    index_a = list(
        (
            await db_session.execute(
                select(MemoryIndexLiteRow).where(MemoryIndexLiteRow.persona_id == persona_a.id)
            )
        )
        .scalars()
        .all()
    )
    index_b = list(
        (
            await db_session.execute(
                select(MemoryIndexLiteRow).where(MemoryIndexLiteRow.persona_id == persona_b.id)
            )
        )
        .scalars()
        .all()
    )
    assert index_a == []
    assert len(index_b) == 1

    with pytest.raises(BusinessException) as exc_info:
        await persona_service.delete(db_session, persona_a.id)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_delete_dispatches_remote_index_purge(
    db_session: AsyncSession,
) -> None:
    """删除提交后按 ref 集合分发生产索引清理（episode + curated 两类）。"""
    recorder = _RecordingIndexer()
    service = PersonaService(indexer=cast("MemoryIndexer", recorder))
    persona = await service.create(db_session, _create_dto(name="index-purge"))
    episode_id, curated_id = await _seed_persona_domain(db_session, persona.id)

    await service.delete(db_session, persona.id)

    assert set(recorder.deleted) == {
        (MemoryRefType.EPISODE, episode_id, persona.id),
        (MemoryRefType.CURATED, curated_id, persona.id),
    }


async def test_delete_primary_persona_forbidden(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """删除守卫（本工程锁定语义）：主人格删除 CONFLICT，先另设主人格后可删。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.set_primary(db_session, record.id)
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.delete(db_session, record.id)
    assert exc_info.value.code == ErrorCode.CONFLICT

    other = await persona_service.create(
        db_session, _create_dto(name="other", status=PersonaStatus.ACTIVE)
    )
    await persona_service.set_primary(db_session, other.id)
    await persona_service.delete(db_session, record.id)  # 两步后可删
    with pytest.raises(BusinessException):
        await persona_service.get(db_session, record.id)


async def test_delete_cleans_states_same_transaction(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """删除非激活人格：M/S 状态行同事务清理（跨方言一致——不依赖 FK 强制）。"""
    record = await persona_service.create(db_session, _create_dto())
    await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "平静"}),
    )
    await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.S,
        PersonaStateUpsertDTO(state={"topic": "无"}),
    )
    await persona_service.delete(db_session, record.id)
    remaining_states = list(
        (
            await db_session.execute(
                select(PersonaStateRow).where(PersonaStateRow.persona_id == record.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining_states == []


async def test_delete_missing_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """删除缺失人格：NOT_FOUND。"""
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.delete(db_session, 999)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 状态层（M/S upsert / 读取）
# ---------------------------------------------------------------------------


async def test_state_upsert_insert_then_replace(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """状态 upsert：首写插入 → 再写整体替换（每 (persona, layer) 一行）。"""
    record = await persona_service.create(db_session, _create_dto())
    first = await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "好奇", "relationship": 50}),
    )
    assert first.state == {"mood": "好奇", "relationship": 50}
    assert first.layer == PersonaStateLayer.M

    second = await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "安心"}),
    )
    assert second.state == {"mood": "安心"}
    rows = list(
        (
            await db_session.execute(
                select(PersonaStateRow).where(PersonaStateRow.persona_id == record.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # 同层唯一


async def test_state_layers_independent(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """M / S 两层独立 upsert + get_states layer 升序 + get_state 未写返回 None。"""
    record = await persona_service.create(db_session, _create_dto())
    assert await persona_service.get_state(db_session, record.id, PersonaStateLayer.M) is None

    await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.S,
        PersonaStateUpsertDTO(state={"topic": "周末计划"}),
    )
    await persona_service.upsert_state(
        db_session,
        record.id,
        PersonaStateLayer.M,
        PersonaStateUpsertDTO(state={"mood": "期待"}),
    )
    states = await persona_service.get_states(db_session, record.id)
    assert [state.layer for state in states] == [PersonaStateLayer.M, PersonaStateLayer.S]

    m_state = await persona_service.get_state(db_session, record.id, PersonaStateLayer.M)
    assert m_state is not None
    assert m_state.state == {"mood": "期待"}


async def test_state_upsert_missing_persona_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """状态不能挂不存在的人格：NOT_FOUND。"""
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.upsert_state(
            db_session,
            999,
            PersonaStateLayer.M,
            PersonaStateUpsertDTO(state={"mood": "x"}),
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_states_missing_persona_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """get_states 缺失人格：NOT_FOUND（读取面同样守卫）。"""
    with pytest.raises(BusinessException):
        await persona_service.get_states(db_session, 999)


# ---------------------------------------------------------------------------
# 游标分页列表
# ---------------------------------------------------------------------------


async def test_query_pagination_and_status_filter(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """分页遍历（id 降序 + 游标接续 + 末页无游标）+ status 过滤。"""
    for index in range(5):
        status = PersonaStatus.ACTIVE if index < 2 else PersonaStatus.DISABLED
        await persona_service.create(db_session, _create_dto(name=f"p{index}", status=status))

    active_only = await persona_service.query(
        db_session, PersonaQueryDTO(status=PersonaStatus.ACTIVE)
    )
    assert isinstance(active_only, PageResponse)
    assert len(active_only.items) == 2
    assert all(item.status == PersonaStatus.ACTIVE for item in active_only.items)

    page1 = await persona_service.query(db_session, PersonaQueryDTO(limit=2))
    assert page1.has_more is True
    assert page1.next_cursor is not None
    ids_page1 = [item.id for item in page1.items]
    assert ids_page1[0] > ids_page1[1]

    page2 = await persona_service.query(
        db_session, PersonaQueryDTO(limit=2, cursor=page1.next_cursor)
    )
    assert ids_page1[1] > page2.items[0].id

    last = await persona_service.query(
        db_session, PersonaQueryDTO(limit=4, cursor=page2.next_cursor)
    )
    assert len(last.items) == 1
    assert last.has_more is False
    assert last.next_cursor is None


async def test_query_malformed_cursor_first_page(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """非法游标：按无游标处理（首页语义——decode 契约）。"""
    await persona_service.create(db_session, _create_dto())
    page = await persona_service.query(
        db_session, PersonaQueryDTO(cursor="!!!not-base64!!!", limit=10)
    )
    assert len(page.items) == 1


# ---------------------------------------------------------------------------
# 批量删除（部分成功）
# ---------------------------------------------------------------------------


async def test_batch_delete_partial_success(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """批量删除部分成功：成功项 / NOT_FOUND 项 / 主人格守卫 CONFLICT 项并存。"""
    ok_persona = await persona_service.create(db_session, _create_dto(name="ok"))
    primary_persona = await persona_service.create(
        db_session, _create_dto(name="primary", status=PersonaStatus.ACTIVE)
    )
    await persona_service.set_primary(db_session, primary_persona.id)

    result = await persona_service.batch_delete(db_session, [ok_persona.id, primary_persona.id, 999])
    assert result.total == 3
    assert result.succeeded == 1
    assert result.failed == 2
    by_id = {item.id: item for item in result.items}
    assert by_id[ok_persona.id].success is True
    assert by_id[primary_persona.id].success is False
    assert by_id[primary_persona.id].code == ErrorCode.CONFLICT.value
    assert by_id[999].success is False
    assert by_id[999].code == ErrorCode.NOT_FOUND.value

    # 成功项真实删除；失败项（主人格）原样保留且在位
    with pytest.raises(BusinessException):
        await persona_service.get(db_session, ok_persona.id)
    kept = await persona_service.get(db_session, primary_persona.id)
    assert kept.is_primary is True


async def test_batch_delete_unknown_exception_partial_success(
    persona_service: PersonaService,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单项未知异常：归一 INTERNAL_ERROR 失败项，其余 id 照常删除。"""
    ok_persona = await persona_service.create(db_session, _create_dto(name="batch-ok"))
    broken_persona = await persona_service.create(db_session, _create_dto(name="batch-broken"))
    original_delete = persona_service.delete

    async def delete_with_injected_failure(session: AsyncSession, persona_id: int) -> None:
        if persona_id == broken_persona.id:
            raise RuntimeError("模拟单项未知故障（非 BusinessException）")
        await original_delete(session, persona_id)

    monkeypatch.setattr(persona_service, "delete", delete_with_injected_failure)
    result = await persona_service.batch_delete(
        db_session, [ok_persona.id, broken_persona.id]
    )
    assert result.total == 2
    assert result.succeeded == 1
    assert result.failed == 1
    by_id = {item.id: item for item in result.items}
    assert by_id[ok_persona.id].success is True
    assert by_id[broken_persona.id].success is False
    assert by_id[broken_persona.id].code == ErrorCode.INTERNAL_ERROR.value
    assert by_id[broken_persona.id].message == "删除人格时发生内部错误"

    # 未知故障项原样保留；成功项真实删除
    kept = await persona_service.get(db_session, broken_persona.id)
    assert kept.name == "batch-broken"
    with pytest.raises(BusinessException):
        await persona_service.get(db_session, ok_persona.id)


# ---------------------------------------------------------------------------
# 卡片面（整体替换）
# ---------------------------------------------------------------------------


async def test_card_get_and_replace(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """卡片读取 + 整体替换（PUT 语义——非补丁合并）。"""
    record = await persona_service.create(db_session, _create_dto())
    card = await persona_service.get_card(db_session, record.id)
    assert card.persona_id == record.id
    assert card.card.name == "大肥鱼"
    assert card.updated_at >= record.created_at

    new_card = CharaCardModel(name="小肥鱼", first_mes="哼，才不是想你呢。")
    replaced = await persona_service.replace_card(db_session, record.id, new_card)
    assert replaced.card.first_mes == "哼，才不是想你呢。"
    assert replaced.card.personality == ""  # 整体替换——旧值不保留
    assert replaced.updated_at >= card.updated_at

    reread = await persona_service.get_card(db_session, record.id)
    assert reread.card.name == "小肥鱼"


async def test_card_missing_persona_not_found(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """卡片面缺失人格：NOT_FOUND。"""
    with pytest.raises(BusinessException):
        await persona_service.get_card(db_session, 999)
    with pytest.raises(BusinessException):
        await persona_service.replace_card(db_session, 999, CharaCardModel(name="x"))


# ---------------------------------------------------------------------------
# 事务边界（commit 失败 → 回滚 → 原样上抛）
# ---------------------------------------------------------------------------


async def test_commit_failure_rolls_back_and_reraises(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """提交失败路径：_commit 回滚（回滚自身失败亦吞掉）后原样上抛。

    激活 / 删除等事务性操作的原子性依赖该出口：commit 失败必须回滚
    并把异常交给上层异常处理器接面——回滚自身的失败（连接已坏）由
    会话关闭兜底，不得掩盖原始异常。
    """
    from unittest.mock import AsyncMock

    original_error = RuntimeError("模拟 commit 失败（连接中断）")
    db_session.commit = AsyncMock(side_effect=original_error)
    db_session.rollback = AsyncMock(side_effect=RuntimeError("模拟回滚失败"))

    with pytest.raises(RuntimeError, match="模拟 commit 失败"):
        await persona_service.create(db_session, _create_dto(name="commit-fail"))

    db_session.commit.assert_awaited()
    db_session.rollback.assert_awaited()


# ---------------------------------------------------------------------------
# 上下文绑定（运行期切换面）
# ---------------------------------------------------------------------------


async def test_bind_persona_context_scopes_and_restores(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """bind_persona_context：int 主键归一为字符串轴绑定 + 退出恢复。"""
    record: PersonaRecordDTO = await persona_service.create(db_session, _create_dto())
    assert current_context().persona_id is None
    async with bind_persona_context(record.id) as ctx:
        assert ctx.persona_id == str(record.id)
        assert persona_id_var.get() == str(record.id)
    assert current_context().persona_id is None  # 退出恢复

    # 字符串形态同样接受（幂等归一）
    async with bind_persona_context(str(record.id)):
        assert persona_id_var.get() == str(record.id)
    assert persona_id_var.get() is None
