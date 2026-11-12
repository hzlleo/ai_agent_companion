"""harness/persona service 测试——注册 CRUD / 事务性单激活切换 / 复制 /
状态层 / 删除守卫 / 批量删除 / 游标分页 / 上下文绑定。

引擎经共享会话级 SQLite fixture（StaticPool 单连接）；Session 由
``db_session`` fixture 注入（服务层事务边界——显式 commit / 异常
rollback），engine holder 槽位外部安装（owner_token=None，生命周期归
共享 fixture）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.harness.core.context import current_context, persona_id_var
from packages.harness.core.dtos import PageResponse
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.models import PersonaStateLayer, PersonaStatus
from packages.harness.persona.orm import PersonaRow, PersonaStateRow
from packages.harness.persona.service import PersonaService, bind_persona_context


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
    assert record.is_active is False
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
# 事务性单激活切换
# ---------------------------------------------------------------------------


async def test_activate_switches_single_active(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """激活事务性：激活 B 必然去激活 A（至多一个激活不变式）。"""
    a = await persona_service.create(db_session, _create_dto(name="a", status=PersonaStatus.ACTIVE))
    b = await persona_service.create(db_session, _create_dto(name="b", status=PersonaStatus.ACTIVE))

    await persona_service.activate(db_session, a.id)
    active = await persona_service.get_active_persona(db_session)
    assert active is not None
    assert active.id == a.id

    await persona_service.activate(db_session, b.id)
    active_after = await persona_service.get_active_persona(db_session)
    assert active_after is not None
    assert active_after.id == b.id
    record_a = await persona_service.get(db_session, a.id)
    assert record_a.is_active is False
    record_b = await persona_service.get(db_session, b.id)
    assert record_b.is_active is True


async def test_activate_disabled_persona_conflict(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用状态人格激活被拒：CONFLICT。"""
    record = await persona_service.create(db_session, _create_dto())  # 默认 disabled
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.activate(db_session, record.id)
    assert exc_info.value.code == ErrorCode.CONFLICT


async def test_activate_idempotent(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """重复激活当前激活人格：幂等 no-op（仍为唯一激活）。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.activate(db_session, record.id)
    await persona_service.activate(db_session, record.id)
    active = await persona_service.get_active_persona(db_session)
    assert active is not None
    assert active.id == record.id


async def test_deactivate_leaves_zero_active(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用激活：零激活合法（系统态）；未激活人格停用幂等。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.activate(db_session, record.id)
    deactivated = await persona_service.deactivate(db_session, record.id)
    assert deactivated.is_active is False
    assert await persona_service.get_active_persona(db_session) is None

    # 幂等：再次停用不激活人格
    again = await persona_service.deactivate(db_session, record.id)
    assert again.is_active is False


async def test_update_status_disabled_clears_active_flag(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """停用即下线：status 切 disabled 时自动清激活位。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.activate(db_session, record.id)
    updated = await persona_service.update(
        db_session, record.id, PersonaUpdateDTO(status=PersonaStatus.DISABLED)
    )
    assert updated.status == PersonaStatus.DISABLED
    assert updated.is_active is False
    assert await persona_service.get_active_persona(db_session) is None


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
    assert copied.is_active is False
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


async def test_delete_active_persona_forbidden(
    persona_service: PersonaService, db_session: AsyncSession
) -> None:
    """删除守卫（本工程锁定语义）：激活人格删除 CONFLICT，先 deactivate 后可删。"""
    record = await persona_service.create(db_session, _create_dto(status=PersonaStatus.ACTIVE))
    await persona_service.activate(db_session, record.id)
    with pytest.raises(BusinessException) as exc_info:
        await persona_service.delete(db_session, record.id)
    assert exc_info.value.code == ErrorCode.CONFLICT

    await persona_service.deactivate(db_session, record.id)
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
    """批量删除部分成功：成功项 / NOT_FOUND 项 / 激活守卫 CONFLICT 项并存。"""
    ok_persona = await persona_service.create(db_session, _create_dto(name="ok"))
    active_persona = await persona_service.create(
        db_session, _create_dto(name="active", status=PersonaStatus.ACTIVE)
    )
    await persona_service.activate(db_session, active_persona.id)

    result = await persona_service.batch_delete(db_session, [ok_persona.id, active_persona.id, 999])
    assert result.total == 3
    assert result.succeeded == 1
    assert result.failed == 2
    by_id = {item.id: item for item in result.items}
    assert by_id[ok_persona.id].success is True
    assert by_id[active_persona.id].success is False
    assert by_id[active_persona.id].code == ErrorCode.CONFLICT.value
    assert by_id[999].success is False
    assert by_id[999].code == ErrorCode.NOT_FOUND.value

    # 成功项真实删除；失败项（激活人格）原样保留
    with pytest.raises(BusinessException):
        await persona_service.get(db_session, ok_persona.id)
    kept = await persona_service.get(db_session, active_persona.id)
    assert kept.is_active is True


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
