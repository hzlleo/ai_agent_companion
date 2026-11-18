"""harness/persona 核心记忆服务测试——懒建 / 主人编辑 / 人格自编辑 / 修订链。

覆盖面：四块懒建（固定块序、种子字段）/ 主人编辑（内容变更推进版本 +
owner-admin 修订、锁定切换不留修订、零变更幂等、清空正文）/ 主人超限
拒绝 / 人格自编辑（replace 与 append 语义、成功修订含会话主键、锁定
块可读拒绝、目标全文超限不改行、会话主键落块行）/ 修订历史倒序与
条数上限 / 构造旋钮 / 人格隔离。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.persona.core_memory_orm as core_memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.core_memory import CoreMemoryService
from packages.harness.persona.core_memory_models import (
    CORE_BLOCK_ORDER,
    AgentEditMode,
    AgentEditModel,
    AgentEditResultModel,
    CoreBlockKey,
    CoreMemoryActor,
    CoreMemorySource,
    OwnerEditModel,
)
from packages.harness.persona.core_memory_orm import CoreMemoryRevisionRow
from packages.harness.persona.service import bind_persona_context

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "core-alpha"
PERSONA_B = "core-beta"

#: 注入时钟（epoch 毫秒——修订时刻的参考轴）。
NOW = 1_760_100_000_000


def _service(max_length: int = 2000) -> CoreMemoryService:
    """构造注时钟服务（上限可按测试收窄）。"""
    return CoreMemoryService(content_max_length=max_length, clock=lambda: NOW)


async def _agent_edit(
    service: CoreMemoryService,
    session: AsyncSession,
    block: CoreBlockKey,
    content: str,
    *,
    mode: AgentEditMode = AgentEditMode.REPLACE,
    session_id: int | None = 7,
) -> AgentEditResultModel:
    """执行一次人格自编辑（测试铺底）。"""
    return await service.agent_edit(
        session,
        AgentEditModel(block=block, content=content, mode=mode),
        session_id=session_id,
    )


async def test_list_blocks_seeds_fixed_four_block_set(db_session: AsyncSession) -> None:
    """首次读取懒建四个空块：固定块序 / 空正文 / 未锁定 / 首版 owner 种子。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        blocks = await service.list_blocks(db_session)
    assert [block.block for block in blocks] == list(CORE_BLOCK_ORDER)
    assert all(block.content == "" for block in blocks)
    assert all(block.locked is False for block in blocks)
    assert all(block.version == 1 for block in blocks)
    assert all(block.updated_by == CoreMemoryActor.OWNER for block in blocks)
    assert all(block.last_edited_session_id is None for block in blocks)


async def test_list_blocks_second_read_does_not_duplicate(db_session: AsyncSession) -> None:
    """重复读取幂等（种子只补缺失键——不重复插入）。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        await service.list_blocks(db_session)
        again = await service.list_blocks(db_session)
    assert len(again) == 4


async def test_owner_update_writes_revision_and_bumps_version(db_session: AsyncSession) -> None:
    """主人编辑：版本递增 + owner/admin 修订 + 无会话归属。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        block = await service.owner_update(
            db_session,
            CoreBlockKey.SELF_IDENTITY,
            OwnerEditModel(content="我是一条会写字的鱼。"),
        )
        assert block.version == 2
        assert block.updated_by == CoreMemoryActor.OWNER
        assert block.content == "我是一条会写字的鱼。"
        assert block.last_edited_session_id is None
        revisions = await service.list_revisions(db_session, CoreBlockKey.SELF_IDENTITY)
    assert len(revisions) == 1
    assert revisions[0].version == 2
    assert revisions[0].content == "我是一条会写字的鱼。"
    assert revisions[0].provenance == CoreMemoryActor.OWNER
    assert revisions[0].source == CoreMemorySource.ADMIN
    assert revisions[0].session_id is None
    assert revisions[0].ts_ms == NOW


async def test_owner_update_clears_content_allowed(db_session: AsyncSession) -> None:
    """主人可清空正文（0 字符边界合法——清空同样留修订）。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        await service.owner_update(
            db_session, CoreBlockKey.OPEN_NOTES, OwnerEditModel(content="临时备忘")
        )
        cleared = await service.owner_update(
            db_session, CoreBlockKey.OPEN_NOTES, OwnerEditModel(content="")
        )
        revisions = await service.list_revisions(db_session, CoreBlockKey.OPEN_NOTES)
    assert cleared.content == ""
    assert revisions[0].content == ""
    assert len(revisions) == 2


async def test_owner_update_same_content_is_idempotent(db_session: AsyncSession) -> None:
    """零变更保存：不推进版本、不追加修订（面板重复保存安全）。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        first = await service.owner_update(
            db_session, CoreBlockKey.OWNER_PROFILE, OwnerEditModel(content="主人喜欢乌龙茶")
        )
        second = await service.owner_update(
            db_session, CoreBlockKey.OWNER_PROFILE, OwnerEditModel(content="主人喜欢乌龙茶")
        )
        revisions = await service.list_revisions(db_session, CoreBlockKey.OWNER_PROFILE)
    assert second.version == first.version
    assert len(revisions) == 1


async def test_owner_lock_toggle_bumps_version_without_revision(
    db_session: AsyncSession,
) -> None:
    """仅锁定切换：版本递增但修订日志不增（修订只记录文本历史）。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        locked = await service.owner_update(
            db_session,
            CoreBlockKey.RELATIONSHIP_STORY,
            OwnerEditModel(content="我们一起看过海。", locked=True),
        )
        before = len(await service.list_revisions(db_session, CoreBlockKey.RELATIONSHIP_STORY))
        unlocked = await service.owner_update(
            db_session,
            CoreBlockKey.RELATIONSHIP_STORY,
            OwnerEditModel(content="我们一起看过海。", locked=False),
        )
        after = len(await service.list_revisions(db_session, CoreBlockKey.RELATIONSHIP_STORY))
    assert locked.locked is True and locked.version == 2
    assert unlocked.locked is False and unlocked.version == 3
    assert before == after == 1


async def test_owner_update_over_cap_rejected_without_write(db_session: AsyncSession) -> None:
    """主人超限（构造旋钮 10）：VALIDATION_ERROR 且正文 / 版本不变。"""
    service = _service(max_length=10)
    async with bind_persona_context(PERSONA_A):
        await service.list_blocks(db_session)
        with pytest.raises(BusinessException) as exc_info:
            await service.owner_update(
                db_session, CoreBlockKey.SELF_IDENTITY, OwnerEditModel(content="x" * 11)
            )
        blocks = await service.list_blocks(db_session)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    target = next(block for block in blocks if block.block == CoreBlockKey.SELF_IDENTITY)
    assert target.content == "" and target.version == 1


async def test_agent_replace_writes_agent_chat_revision(db_session: AsyncSession) -> None:
    """人格替换：ok=True + 版本递增 + agent/chat 修订 + 块行会话归属。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        result = await _agent_edit(
            service, db_session, CoreBlockKey.SELF_IDENTITY, "我是一条会写字的鱼。"
        )
        blocks = await service.list_blocks(db_session)
        revisions = await service.list_revisions(db_session, CoreBlockKey.SELF_IDENTITY)
    assert result.ok is True
    assert result.length == len("我是一条会写字的鱼。")
    assert "已更新" in result.message
    target = next(block for block in blocks if block.block == CoreBlockKey.SELF_IDENTITY)
    assert target.version == 2
    assert target.updated_by == CoreMemoryActor.AGENT
    assert target.last_edited_session_id == 7
    assert revisions[0].provenance == CoreMemoryActor.AGENT
    assert revisions[0].source == CoreMemorySource.CHAT
    assert revisions[0].session_id == 7


async def test_agent_append_keeps_existing_text(db_session: AsyncSession) -> None:
    """append 模式：既有正文保留并以换行分隔追加新段。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        await _agent_edit(service, db_session, CoreBlockKey.OPEN_NOTES, "第一行")
        result = await _agent_edit(
            service, db_session, CoreBlockKey.OPEN_NOTES, "第二行", mode=AgentEditMode.APPEND
        )
        blocks = await service.list_blocks(db_session)
    target = next(block for block in blocks if block.block == CoreBlockKey.OPEN_NOTES)
    assert result.ok is True
    assert target.content == "第一行\n第二行"
    assert result.length == len("第一行\n第二行")


async def test_agent_edit_locked_block_returns_readable_refusal(
    db_session: AsyncSession,
) -> None:
    """锁定块：ok=False + 中文可读拒绝（含块名）+ 当前长度，不改任何行。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        await service.owner_update(
            db_session,
            CoreBlockKey.SELF_IDENTITY,
            OwnerEditModel(content="既有的自我认知", locked=True),
        )
        result = await _agent_edit(service, db_session, CoreBlockKey.SELF_IDENTITY, "新写法")
        blocks = await service.list_blocks(db_session)
    target = next(block for block in blocks if block.block == CoreBlockKey.SELF_IDENTITY)
    assert result.ok is False
    assert result.message == "核心记忆块「自我认知」已锁定，仅主人可修改"
    assert result.length == len("既有的自我认知")
    assert target.content == "既有的自我认知"
    assert target.version == 2


async def test_agent_edit_over_cap_checks_target_text_before_write(
    db_session: AsyncSession,
) -> None:
    """追加后超限：按目标全文计长拒绝（失败长度 = 当前长度，行不变）。"""
    service = _service(max_length=8)
    async with bind_persona_context(PERSONA_A):
        await _agent_edit(service, db_session, CoreBlockKey.OPEN_NOTES, "12345")
        result = await _agent_edit(
            service, db_session, CoreBlockKey.OPEN_NOTES, "6789", mode=AgentEditMode.APPEND
        )
        blocks = await service.list_blocks(db_session)
    target = next(block for block in blocks if block.block == CoreBlockKey.OPEN_NOTES)
    assert result.ok is False
    assert result.length == 5
    assert "超出上限" in result.message
    assert target.content == "12345"
    assert target.version == 2


async def test_agent_edit_replace_over_cap_rejected(db_session: AsyncSession) -> None:
    """替换模式同样按正文计长（超过构造上限即拒绝）。"""
    service = _service(max_length=4)
    async with bind_persona_context(PERSONA_A):
        result = await _agent_edit(service, db_session, CoreBlockKey.OPEN_NOTES, "12345")
    assert result.ok is False
    assert result.length == 0


async def test_list_revisions_newest_first_and_capped(db_session: AsyncSession) -> None:
    """修订历史：版本倒序；limit 截断取最新 N 条。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        for index in range(3):
            await service.owner_update(
                db_session, CoreBlockKey.SELF_IDENTITY, OwnerEditModel(content=f"版本{index}")
            )
        all_revisions = await service.list_revisions(db_session, CoreBlockKey.SELF_IDENTITY)
        capped = await service.list_revisions(db_session, CoreBlockKey.SELF_IDENTITY, limit=2)
    assert [revision.content for revision in all_revisions] == ["版本2", "版本1", "版本0"]
    assert [revision.version for revision in all_revisions] == [4, 3, 2]
    assert [revision.content for revision in capped] == ["版本2", "版本1"]


async def test_revisions_are_append_only_rows(db_session: AsyncSession) -> None:
    """修订表只增不改：三次内容变更产生三行且行内容不可变。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        for text in ("第一版", "第二版", "第三版"):
            await service.owner_update(
                db_session, CoreBlockKey.SELF_IDENTITY, OwnerEditModel(content=text)
            )
        rows = list(
            (
                await db_session.execute(
                    select(CoreMemoryRevisionRow).order_by(CoreMemoryRevisionRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
    assert [row.content for row in rows] == ["第一版", "第二版", "第三版"]
    assert all(row.provenance == CoreMemoryActor.OWNER.value for row in rows)


async def test_blocks_are_persona_scoped(db_session: AsyncSession) -> None:
    """人格隔离：他人格的块不可见（各自懒建独立四块）。"""
    service = _service()
    async with bind_persona_context(PERSONA_A):
        await service.owner_update(
            db_session, CoreBlockKey.SELF_IDENTITY, OwnerEditModel(content="A 的自我认知")
        )
    async with bind_persona_context(PERSONA_B):
        blocks_b = await service.list_blocks(db_session)
    assert all(block.content == "" for block in blocks_b)
    assert len(blocks_b) == 4


async def test_service_rejects_non_positive_cap() -> None:
    """构造旋钮非法：上限非正即装配错误（INTERNAL_ERROR）。"""
    with pytest.raises(BusinessException) as exc_info:
        CoreMemoryService(content_max_length=0)
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
