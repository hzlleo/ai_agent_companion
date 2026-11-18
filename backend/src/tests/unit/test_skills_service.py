"""harness/persona skills 服务测试——技能 CRUD / 触发匹配 / 写读强化 / 梦境合并。

覆盖面：slug 归一（NFKC / 大小写 / 标点折叠 / 空派生拒绝）/ 创建（首版
active + manual）/ 同名冲突（CONFLICT——中文消息）/ 触发短语归一与
去重 / 原地编辑（版本递增、改名换 slug、与他人格内技能冲突）/ 状态
切换（停用 / 启用幂等）/ 硬删除 / 列表（状态过滤 + 使用次数降序 +
游标分页）/ compose 匹配（触发短语字面命中 / 描述关键词弱命中 / 无命中
/ 条数帽与排序 / 使用计数 fire-and-forget 写入）/ 梦境写入（新建 mined /
同 slug 元数据合并：置信度最大值 + 触发短语并集上限、状态与版本不变）/
人格隔离。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import packages.harness.persona.skills_orm as skills_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.dtos import decode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.service import bind_persona_context
from packages.harness.persona.skills import (
    SkillsService,
    _normalize_triggers,
    normalize_skill_slug,
)
from packages.harness.persona.skills_models import (
    SKILL_COMPOSE_MAX_SKILLS,
    SKILL_TRIGGERS_MAX,
    SkillCreateModel,
    SkillQueryDTO,
    SkillSource,
    SkillStatus,
    SkillUpdateModel,
)
from packages.harness.persona.skills_orm import PersonaSkillRow

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "skill-alpha"
PERSONA_B = "skill-beta"

#: 注入时钟（epoch 毫秒——命中计数时间戳的参考轴）。
NOW = 1_760_000_000_000


def _create(
    name: str = "周报流程",
    *,
    description: str = "主人说「写周报」时使用：先列三件事再写进展",
    triggers: list[str] | None = None,
    content: str = "1. 先列三件事\n2. 再写进展",
    confidence: float = 0.6,
) -> SkillCreateModel:
    """构造技能创建入参（测试铺底）。"""
    return SkillCreateModel(
        name=name,
        description=description,
        triggers=triggers or ["写周报"],
        content=content,
        confidence=confidence,
    )


@pytest.fixture
async def installed_engine(shared_db_engine: AsyncEngine) -> AsyncIterator[None]:
    """把共享 SQLite 引擎装进全局持有器（服务内部 session_scope 消费面）。

    使用计数写入走独立短事务（``session_scope``）——测试需让该入口指向
    共享引擎，否则强化写入会因「数据库未初始化」被吞没（断言不到）。
    """
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    try:
        yield
    finally:
        db_engine_holder.reset()


# ---------------------------------------------------------------------------
# slug 归一与触发短语归一
# ---------------------------------------------------------------------------


async def test_normalize_skill_slug_kebab_and_unicode() -> None:
    """slug 归一：ASCII 折叠 kebab；CJK 保留；标点折叠；长度截断。"""
    assert normalize_skill_slug("Weekly Report") == "weekly-report"
    assert normalize_skill_slug("周报流程（简版）") == "周报流程-简版"
    assert normalize_skill_slug("  A  B---C  ") == "a-b-c"
    assert len(normalize_skill_slug("a" * 200)) == 80
    assert normalize_skill_slug("!!!") == ""


async def test_create_rejects_underivable_name(db_session: AsyncSession) -> None:
    """名称无可派生字符：VALIDATION_ERROR（不能静默落空 slug）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await service.create(db_session, _create(name="！！！"))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 创建 / 冲突 / 触发短语归一
# ---------------------------------------------------------------------------


async def test_create_skill_first_version(db_session: AsyncSession) -> None:
    """创建：active/manual/version=1/use_count=0；触发短语去重后落库。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        record = await service.create(
            db_session, _create(triggers=["写周报", "写周报", "本周总结"])
        )
    assert record.slug == "周报流程"
    assert record.status == SkillStatus.ACTIVE
    assert record.source == SkillSource.MANUAL
    assert record.version == 1
    assert record.use_count == 0
    assert record.last_used_ms is None
    assert record.triggers == ["写周报", "本周总结"]


async def test_create_duplicate_slug_conflicts(db_session: AsyncSession) -> None:
    """同人格同名：CONFLICT（中文消息）——不静默覆盖。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        await service.create(db_session, _create())
        with pytest.raises(BusinessException) as exc_info:
            await service.create(db_session, _create(description="另一条同名技能"))
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert "slug 冲突" in exc_info.value.message


async def test_create_allows_same_name_across_personas(db_session: AsyncSession) -> None:
    """跨人格同名不冲突（唯一键含 persona_id）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        first = await service.create(db_session, _create())
    async with bind_persona_context(PERSONA_B):
        second = await service.create(db_session, _create())
    assert first.slug == second.slug
    assert first.id != second.id


async def test_rejects_empty_triggers_at_boundary() -> None:
    """触发短语全空白：DTO 边界拒绝（strip 后不足 1 字符）+ 服务层兜底拒绝。"""
    with pytest.raises(ValidationError):
        _create(triggers=["   "])
    with pytest.raises(BusinessException) as exc_info:
        _normalize_triggers([])
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# 编辑 / 状态 / 删除
# ---------------------------------------------------------------------------


async def test_update_increments_version_and_changes_slug(db_session: AsyncSession) -> None:
    """原地编辑：字段更新 + 版本递增；改名同步换 slug（无历史行）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        created = await service.create(db_session, _create())
        updated = await service.update(
            db_session,
            created.id,
            SkillUpdateModel(
                name="日报流程",
                triggers=["写日报"],
                content="1. 写今日完成\n2. 写明日计划",
                confidence=0.9,
            ),
        )
        rows = list((await db_session.execute(select(PersonaSkillRow))).scalars().all())
    assert updated.version == 2
    assert updated.slug == "日报流程"
    assert updated.triggers == ["写日报"]
    assert updated.confidence == 0.9
    assert len(rows) == 1  # 原地更新——不产生历史行


async def test_update_conflicting_slug_rejected(db_session: AsyncSession) -> None:
    """改名撞既有技能 slug：CONFLICT。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        await service.create(db_session, _create(name="周报流程"))
        second = await service.create(db_session, _create(name="日报流程"))
        with pytest.raises(BusinessException) as exc_info:
            await service.update(db_session, second.id, SkillUpdateModel(name="周报流程"))
    assert exc_info.value.code == ErrorCode.CONFLICT


async def test_update_missing_skill_not_found(db_session: AsyncSession) -> None:
    """编辑不存在技能：NOT_FOUND。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await service.update(db_session, 404, SkillUpdateModel(content="正文"))
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_set_status_toggle_idempotent(db_session: AsyncSession) -> None:
    """状态切换：停用 / 启用幂等（同值零变更成功，不推进版本）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        created = await service.create(db_session, _create())
        disabled = await service.set_status(db_session, created.id, SkillStatus.DISABLED)
        disabled_again = await service.set_status(db_session, created.id, SkillStatus.DISABLED)
        enabled = await service.set_status(db_session, created.id, SkillStatus.ACTIVE)
    assert disabled.status == SkillStatus.DISABLED
    assert disabled.version == 1
    assert disabled_again.status == SkillStatus.DISABLED
    assert enabled.status == SkillStatus.ACTIVE


async def test_delete_removes_row(db_session: AsyncSession) -> None:
    """硬删除：行消失；再次删除 NOT_FOUND。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        created = await service.create(db_session, _create())
        await service.delete(db_session, created.id)
        rows = list((await db_session.execute(select(PersonaSkillRow))).scalars().all())
        with pytest.raises(BusinessException) as exc_info:
            await service.delete(db_session, created.id)
    assert rows == []
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 列表（状态过滤 / 排序 / 游标）
# ---------------------------------------------------------------------------


async def test_list_status_filter_and_use_count_order(db_session: AsyncSession) -> None:
    """列表：状态过滤生效；使用次数降序（同次数按 id 升序）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        hot = await service.create(db_session, _create(name="热技能 A"))
        cold = await service.create(db_session, _create(name="冷技能 B"))
        disabled = await service.create(db_session, _create(name="停用 C"))
        await service.set_status(db_session, disabled.id, SkillStatus.DISABLED)
        hot_row = (
            (await db_session.execute(select(PersonaSkillRow).where(PersonaSkillRow.id == hot.id)))
            .scalars()
            .one()
        )
        hot_row.use_count = 5
        await db_session.commit()
        active_page = await service.list_skills(
            db_session, SkillQueryDTO(status=SkillStatus.ACTIVE)
        )
        disabled_page = await service.list_skills(
            db_session, SkillQueryDTO(status=SkillStatus.DISABLED)
        )
    assert [item.id for item in active_page.items] == [hot.id, cold.id]
    assert [item.id for item in disabled_page.items] == [disabled.id]


async def test_list_cursor_pagination_no_duplicates(db_session: AsyncSession) -> None:
    """列表游标：跨页不重不漏（(use_count, id) 复合位置）。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        created = [await service.create(db_session, _create(name=f"技能{index}")) for index in range(3)]
        first = await service.list_skills(db_session, SkillQueryDTO(limit=2))
        assert first.next_cursor is not None
        payload = decode_cursor(first.next_cursor)
        assert isinstance(payload, dict) and "use_count" in payload and "id" in payload
        second = await service.list_skills(
            db_session, SkillQueryDTO(limit=2, cursor=first.next_cursor)
        )
    seen = [item.id for item in (*first.items, *second.items)]
    assert sorted(seen) == sorted(item.id for item in created)
    assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------
# compose 匹配（触发短语 / 描述关键词 / 条数帽 / 使用计数）
# ---------------------------------------------------------------------------


async def test_match_trigger_literal_hit(db_session: AsyncSession) -> None:
    """触发短语字面命中：用户文本包含短语即命中（大小写不敏感）。"""
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(PERSONA_A):
        record = await service.create(
            db_session, _create(name="Weekly", triggers=["weekly report"])
        )
        hits = await service.match_for_compose(db_session, "帮我写 Weekly Report 吧")
    assert [item.id for item in hits] == [record.id]


async def test_match_description_keyword_hit(db_session: AsyncSession) -> None:
    """描述关键词弱命中：描述片段出现在用户文本中即命中。"""
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(PERSONA_A):
        record = await service.create(
            db_session,
            _create(
                name="总结风格",
                description="summary: 先给结论再展开",
                triggers=["总结一下"],
            ),
        )
        hits = await service.match_for_compose(db_session, "give me a Summary please")
    assert [item.id for item in hits] == [record.id]


async def test_match_no_hit_and_disabled_excluded(db_session: AsyncSession) -> None:
    """无包含关系不命中；停用技能不参与匹配。"""
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(PERSONA_A):
        enabled = await service.create(db_session, _create(name="启用技能", triggers=["写周报"]))
        disabled = await service.create(db_session, _create(name="停用技能", triggers=["写月报"]))
        await service.set_status(db_session, disabled.id, SkillStatus.DISABLED)
        none_hit = await service.match_for_compose(db_session, "今天天气不错")
        month_hit = await service.match_for_compose(db_session, "帮我写月报")
        week_hit = await service.match_for_compose(db_session, "帮我写周报")
    assert none_hit == []
    assert month_hit == []
    assert [item.id for item in week_hit] == [enabled.id]


async def test_match_caps_and_orders_by_use_count(db_session: AsyncSession) -> None:
    """条数帽与排序：命中多条时按使用次数降序取前两条（确定性）。"""
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(PERSONA_A):
        low = await service.create(db_session, _create(name="低优先", triggers=["总结"]))
        mid = await service.create(db_session, _create(name="中优先", triggers=["帮我总结"]))
        high = await service.create(db_session, _create(name="高优先", triggers=["总结一下"]))
        rows = list((await db_session.execute(select(PersonaSkillRow))).scalars().all())
        counts = {low.id: 1, mid.id: 2, high.id: 3}
        for row in rows:
            row.use_count = counts[row.id]
        await db_session.commit()
        hits = await service.match_for_compose(db_session, "帮我总结一下今天的事")
    assert len(hits) == SKILL_COMPOSE_MAX_SKILLS
    assert [item.id for item in hits] == [high.id, mid.id]


async def test_match_bumps_usage_fire_and_forget(
    db_session: AsyncSession, installed_engine: None
) -> None:
    """命中后使用计数经调度器写入（use_count +1 / last_used_ms 落时刻）。"""
    scheduled: list[Coroutine[Any, Any, None]] = []
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=scheduled.append)
    async with bind_persona_context(PERSONA_A):
        record = await service.create(db_session, _create())
        hits = await service.match_for_compose(db_session, "写周报")
        assert [item.id for item in hits] == [record.id]
        assert len(scheduled) == 1
        await scheduled[0]
        refreshed = (
            (await db_session.execute(select(PersonaSkillRow).where(PersonaSkillRow.id == record.id)))
            .scalars()
            .one()
        )
        await db_session.refresh(refreshed)
    assert refreshed.use_count == 1
    assert refreshed.last_used_ms == NOW


async def test_usage_bump_without_context_skipped() -> None:
    """无人格上下文：强化调度直接跳过（告警不抛出——回复优先）。"""
    scheduled: list[Coroutine[Any, Any, None]] = []
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=scheduled.append)
    service._schedule_usage_bump([1, 2])
    assert scheduled == []


# ---------------------------------------------------------------------------
# 梦境写入（upsert_mined）
# ---------------------------------------------------------------------------


async def test_upsert_mined_creates_mined_row(db_session: AsyncSession) -> None:
    """梦境新建：source=mined / active / version=1。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        result = await service.upsert_mined(db_session, _create(name="挖矿技能"))
    assert result.created is True
    assert result.merged is False
    assert result.record.source == SkillSource.MINED
    assert result.record.status == SkillStatus.ACTIVE
    assert result.record.version == 1


async def test_upsert_mined_merges_metadata_without_version_bump(
    db_session: AsyncSession,
) -> None:
    """同 slug 合并：置信度最大值 + 触发短语并集；正文 / 状态 / 版本不变。"""
    service = SkillsService(clock=lambda: NOW)
    async with bind_persona_context(PERSONA_A):
        created = await service.create(
            db_session, _create(name="周报流程", confidence=0.5, triggers=["写周报"])
        )
        await service.set_status(db_session, created.id, SkillStatus.DISABLED)
        result = await service.upsert_mined(
            db_session,
            _create(
                name="周报流程",
                confidence=0.9,
                triggers=["写周报", "本周总结"],
                content="模型写的新正文（不应覆盖主人正文）",
            ),
        )
        rows = list((await db_session.execute(select(PersonaSkillRow))).scalars().all())
    assert result.created is False
    assert result.merged is True
    assert result.record.confidence == 0.9
    assert result.record.triggers == ["写周报", "本周总结"]
    assert result.record.status == SkillStatus.DISABLED  # 主人停用不被梦境改回
    assert result.record.version == 1
    assert result.record.source == SkillSource.MANUAL
    assert "不应覆盖" not in result.record.content
    assert len(rows) == 1


async def test_upsert_mined_trigger_union_capped(db_session: AsyncSession) -> None:
    """触发短语并集上限：既有条目优先保留，总数不超过 SKILL_TRIGGERS_MAX。"""
    service = SkillsService(clock=lambda: NOW)
    base_triggers = [f"短语{i}" for i in range(SKILL_TRIGGERS_MAX)]
    async with bind_persona_context(PERSONA_A):
        await service.create(db_session, _create(name="并集技能", triggers=base_triggers))
        result = await service.upsert_mined(
            db_session,
            _create(name="并集技能", triggers=["新短语", "短语0"]),
        )
    assert len(result.record.triggers) == SKILL_TRIGGERS_MAX
    assert result.record.triggers[:SKILL_TRIGGERS_MAX] == base_triggers


# ---------------------------------------------------------------------------
# 人格隔离
# ---------------------------------------------------------------------------


async def test_skill_persona_isolation(db_session: AsyncSession) -> None:
    """人格隔离：他人格不可见 / 不可匹配 / 不可按 id 编辑（NOT_FOUND）。"""
    service = SkillsService(clock=lambda: NOW, usage_write_scheduler=lambda coro: coro.close())
    async with bind_persona_context(PERSONA_A):
        created = await service.create(db_session, _create())
    async with bind_persona_context(PERSONA_B):
        page = await service.list_skills(db_session, SkillQueryDTO())
        hits = await service.match_for_compose(db_session, "写周报")
        with pytest.raises(BusinessException) as exc_info:
            await service.delete(db_session, created.id)
    assert page.items == []
    assert hits == []
    assert exc_info.value.code == ErrorCode.NOT_FOUND
