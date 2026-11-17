"""harness/persona directives 服务测试——常备指令 CRUD（共享 SQLite）。

覆盖面：创建（正文 / topic 派生 / 边界拒绝）/ 同 topic 覆盖 supersede
（version 递增 + 单 active）/ 按既有 topic 更新 / 列表现行（命名空间
收窄——同域普通 user_note 不可见）/ 注入文本读取（帽）/ 删除 / 非本域
行 NOT_FOUND / 人格隔离。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.dtos import CuratedQueryDTO, CuratedUpsertDTO
from packages.harness.memory.models import CuratedKind, CuratedStatus, MemoryProvenance
from packages.harness.memory.service import MemoryService
from packages.harness.persona.directives import (
    DIRECTIVES_QUERY_LIMIT,
    MAX_ACTIVE_DIRECTIVES,
    DirectivesService,
)
from packages.harness.persona.directives_models import (
    DIRECTIVE_TOPIC_PREFIX,
    DirectiveCreateDTO,
    DirectiveUpdateDTO,
)
from packages.harness.persona.service import bind_persona_context

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "dir-alpha"
PERSONA_B = "dir-beta"

directives_service = DirectivesService()
memory_service = MemoryService()


async def test_create_derives_namespaced_topic(db_session: AsyncSession) -> None:
    """创建：topic 按正文指纹派生并带命名空间前缀；正文裁剪落库。"""
    async with bind_persona_context(PERSONA_A):
        record = await directives_service.create(
            db_session, DirectiveCreateDTO(content="  以后都用简体中文  ")
        )
    assert record.topic.startswith(DIRECTIVE_TOPIC_PREFIX)
    assert record.content == "以后都用简体中文"
    assert record.version == 1


async def test_create_same_content_supersedes(db_session: AsyncSession) -> None:
    """同文重复创建：同 topic 收敛（指纹确定）——version 递增，旧行 superseded。"""
    async with bind_persona_context(PERSONA_A):
        first = await directives_service.create(
            db_session, DirectiveCreateDTO(content="每晚提醒我睡觉")
        )
        second = await directives_service.create(
            db_session, DirectiveCreateDTO(content="每晚提醒我睡觉")
        )
        history = await memory_service.get_curated_history(db_session, second.id)
    assert second.topic == first.topic
    assert second.id != first.id
    assert second.version == 2
    assert [row.status for row in history] == [CuratedStatus.SUPERSEDED, CuratedStatus.ACTIVE]


async def test_create_explicit_topic_overwrites(db_session: AsyncSession) -> None:
    """显式 topic：同 topic 覆盖为 supersede（编辑语义）；无前缀自动补齐。"""
    async with bind_persona_context(PERSONA_A):
        first = await directives_service.create(
            db_session, DirectiveCreateDTO(content="语气温柔一点", topic="tone")
        )
        second = await directives_service.create(
            db_session, DirectiveCreateDTO(content="语气可以痞一点", topic="tone")
        )
    assert first.topic == f"{DIRECTIVE_TOPIC_PREFIX}tone"
    assert second.topic == first.topic
    assert second.version == 2
    assert second.content == "语气可以痞一点"


async def test_create_rejects_boundaries(db_session: AsyncSession) -> None:
    """边界：空白正文服务层拒绝（VALIDATION_ERROR）；超长在 DTO 边界拒绝。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as blank:
            await directives_service.create(db_session, DirectiveCreateDTO(content="   "))
    assert blank.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(ValidationError):
        DirectiveCreateDTO(content="字" * 501)


async def test_list_active_excludes_plain_user_notes(db_session: AsyncSession) -> None:
    """列表：命名空间收窄——普通 user_note 行不可见（同 kind 不同 topic）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.USER_NOTE,
                topic="备忘：买猫粮",
                content="普通备忘",
                provenance=MemoryProvenance.OWNER,
            ),
        )
        directive = await directives_service.create(
            db_session, DirectiveCreateDTO(content="以后都用简体中文")
        )
        listed = await directives_service.list_active(db_session)
        texts = await directives_service.list_active_texts(db_session)
    assert [item.id for item in listed] == [directive.id]
    assert texts == ["以后都用简体中文"]


async def test_list_active_orders_newest_first(db_session: AsyncSession) -> None:
    """列表：id 降序（新写入在前）——注入面取前 N 条的可预期顺序。"""
    async with bind_persona_context(PERSONA_A):
        for content in ("第一条", "第二条", "第三条"):
            await directives_service.create(db_session, DirectiveCreateDTO(content=content))
        listed = await directives_service.list_active(db_session)
    assert [item.content for item in listed] == ["第三条", "第二条", "第一条"]


async def test_list_active_texts_respects_cap(db_session: AsyncSession) -> None:
    """注入文本：条数帽（MAX_ACTIVE_DIRECTIVES 上限内）。"""
    async with bind_persona_context(PERSONA_A):
        for index in range(MAX_ACTIVE_DIRECTIVES + 3):
            await directives_service.create(
                db_session, DirectiveCreateDTO(content=f"指令 {index}")
            )
        texts = await directives_service.list_active_texts(db_session)
        all_items = await directives_service.list_active(db_session)
    assert len(texts) == MAX_ACTIVE_DIRECTIVES
    assert len(all_items) == MAX_ACTIVE_DIRECTIVES + 3
    assert all_items[0].content == f"指令 {MAX_ACTIVE_DIRECTIVES + 2}"
    assert DIRECTIVES_QUERY_LIMIT >= MAX_ACTIVE_DIRECTIVES


async def test_update_by_id_uses_existing_topic(db_session: AsyncSession) -> None:
    """按 id 更新：取既有 topic 定链（supersede +1）——同链不同主键。"""
    async with bind_persona_context(PERSONA_A):
        first = await directives_service.create(
            db_session, DirectiveCreateDTO(content="不要熬夜")
        )
        updated = await directives_service.update(
            db_session, first.id, DirectiveUpdateDTO(content="不要超过十二点睡")
        )
    assert updated.id != first.id
    assert updated.topic == first.topic
    assert updated.version == 2
    assert updated.content == "不要超过十二点睡"


async def test_update_missing_not_found(db_session: AsyncSession) -> None:
    """按 id 更新：缺失 → NOT_FOUND。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await directives_service.update(db_session, 404, DirectiveUpdateDTO(content="x 指令"))
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_delete_removes_active_row(db_session: AsyncSession) -> None:
    """删除：active 行移除——列表不再含该指令。"""
    async with bind_persona_context(PERSONA_A):
        record = await directives_service.create(
            db_session, DirectiveCreateDTO(content="以后都用简体中文")
        )
        await directives_service.delete(db_session, record.id)
        listed = await directives_service.list_active(db_session)
    assert listed == []


async def test_non_directive_row_invisible(db_session: AsyncSession) -> None:
    """非本域行：普通 user_note 主键在更新 / 删除面均 NOT_FOUND（隔离语义）。"""
    async with bind_persona_context(PERSONA_A):
        note = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.USER_NOTE,
                topic="备忘",
                content="普通备忘",
                provenance=MemoryProvenance.OWNER,
            ),
        )
        with pytest.raises(BusinessException) as update_exc:
            await directives_service.update(db_session, note.id, DirectiveUpdateDTO(content="新正文"))
        with pytest.raises(BusinessException) as delete_exc:
            await directives_service.delete(db_session, note.id)
    assert update_exc.value.code == ErrorCode.NOT_FOUND
    assert delete_exc.value.code == ErrorCode.NOT_FOUND


async def test_persona_isolation(db_session: AsyncSession) -> None:
    """人格隔离：他人格指令在列表 / 更新 / 删除面均不可见。"""
    async with bind_persona_context(PERSONA_A):
        record = await directives_service.create(
            db_session, DirectiveCreateDTO(content="以后都用简体中文")
        )
    async with bind_persona_context(PERSONA_B):
        listed = await directives_service.list_active(db_session)
        with pytest.raises(BusinessException) as exc_info:
            await directives_service.delete(db_session, record.id)
    assert listed == []
    assert exc_info.value.code == ErrorCode.NOT_FOUND
    # 同域查询面复核（策展层自动过滤——active 计数跨人格不可见）。
    async with bind_persona_context(PERSONA_A):
        page = await memory_service.query_curated(
            db_session, CuratedQueryDTO(kind=CuratedKind.USER_NOTE, status=CuratedStatus.ACTIVE)
        )
    assert [row.id for row in page.items] == [record.id]
