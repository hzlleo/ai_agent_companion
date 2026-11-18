"""harness/reminders service 测试——CRUD / 扫描 / 投递终态回写（共享 SQLite）。

覆盖面：创建（边界校验 / 过去时刻补投语义）/ 游标分页过滤 / 取消
（仅 pending）/ 批量取消部分成功 / 到期扫描（future 不计、cancelled 不计、
批量帽）/ 投递成功回写 / 失败计数与上限转 failed / **人格隔离**
（PersonaMixin contextvars 自动过滤——双人格互不可见）/ 批量请求唯一性
校验。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.reminders.orm as reminders_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.service import bind_persona_context
from packages.harness.reminders.models import (
    ReminderBatchCancelRequest,
    ReminderCreateModel,
    ReminderQueryDTO,
    ReminderSource,
    ReminderStatus,
)
from packages.harness.reminders.service import (
    DELIVERY_FAILURE_LIMIT,
    SCAN_BATCH_LIMIT,
    ReminderService,
)

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "rem-alpha"
PERSONA_B = "rem-beta"

reminder_service = ReminderService()


def _create_model(
    *,
    title: str = "喝水",
    due_at_ms: int = 2_000,
    source: ReminderSource = ReminderSource.MANUAL,
) -> ReminderCreateModel:
    """构造创建入参（测试铺底辅助）。"""
    return ReminderCreateModel(title=title, due_at_ms=due_at_ms, source=source)


async def test_create_returns_pending_record(db_session: AsyncSession) -> None:
    """创建：pending + 零尝试 + 无投递时刻 + 来源落库。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model())
    assert record.id >= 1
    assert record.persona_id == PERSONA_A
    assert record.status is ReminderStatus.PENDING
    assert record.delivery_attempts == 0
    assert record.delivered_at_ms is None
    assert record.last_error is None
    assert record.source is ReminderSource.MANUAL


async def test_create_trims_title_and_allows_past_due(db_session: AsyncSession) -> None:
    """创建：标题首尾空白裁剪；过去时刻允许（补投语义——创建即到期）。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(
            db_session, _create_model(title="  喝水  ", due_at_ms=1)
        )
        scan = await reminder_service.scan_due_reminders(db_session, now_ms=10)
    assert record.title == "喝水"
    assert scan.scanned == 1
    assert scan.due[0].id == record.id


async def test_create_rejects_blank_title(db_session: AsyncSession) -> None:
    """创建：全空白标题（裁剪后为空）拒绝——VALIDATION_ERROR。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await reminder_service.create(db_session, _create_model(title="   "))
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_query_filters_status_and_paginates(db_session: AsyncSession) -> None:
    """查询：status 过滤 + id 降序 + 游标翻页。"""
    async with bind_persona_context(PERSONA_A):
        first = await reminder_service.create(db_session, _create_model(title="一"))
        second = await reminder_service.create(db_session, _create_model(title="二"))
        await reminder_service.cancel(db_session, second.id)
        pending_page = await reminder_service.query(
            db_session, ReminderQueryDTO(status=ReminderStatus.PENDING)
        )
        all_page = await reminder_service.query(db_session, ReminderQueryDTO(limit=1))
        next_page = await reminder_service.query(
            db_session, ReminderQueryDTO(cursor=all_page.next_cursor, limit=1)
        )
    assert [item.id for item in pending_page.items] == [first.id]
    assert all_page.items[0].id == second.id
    assert all_page.next_cursor is not None
    assert next_page.items[0].id == first.id


async def test_cancel_only_pending(db_session: AsyncSession) -> None:
    """取消：pending → cancelled；重复取消拒绝（VALIDATION_ERROR）。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model())
        cancelled = await reminder_service.cancel(db_session, record.id)
        with pytest.raises(BusinessException) as exc_info:
            await reminder_service.cancel(db_session, record.id)
    assert cancelled.status is ReminderStatus.CANCELLED
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_cancel_missing_reminder_not_found(db_session: AsyncSession) -> None:
    """取消：不存在（或他人格）→ NOT_FOUND（隔离语义）。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model())
    async with bind_persona_context(PERSONA_B):
        with pytest.raises(BusinessException) as exc_info:
            await reminder_service.cancel(db_session, record.id)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_batch_cancel_partial_success(db_session: AsyncSession) -> None:
    """批量取消：部分成功（一项成功 / 一项已取消 / 一项不存在）。"""
    async with bind_persona_context(PERSONA_A):
        first = await reminder_service.create(db_session, _create_model(title="一"))
        second = await reminder_service.create(db_session, _create_model(title="二"))
        await reminder_service.cancel(db_session, second.id)
        result = await reminder_service.batch_cancel(
            db_session, [first.id, second.id, 999_999]
        )
    assert result.total == 3
    assert result.succeeded == 1
    assert result.failed == 2
    assert [item.success for item in result.items] == [True, False, False]
    assert result.items[1].code == ErrorCode.VALIDATION_ERROR
    assert result.items[2].code == ErrorCode.NOT_FOUND


async def test_batch_cancel_request_rejects_duplicates() -> None:
    """批量请求：重复 id 在 DTO 边界拒绝（422 面）。"""
    with pytest.raises(ValidationError):
        ReminderBatchCancelRequest.model_validate({"ids": [1, 1]})
    with pytest.raises(ValidationError):
        ReminderBatchCancelRequest.model_validate({"ids": []})


async def test_scan_excludes_future_and_cancelled(db_session: AsyncSession) -> None:
    """扫描：仅 pending 且 due_at_ms <= now——future / cancelled 不计。"""
    async with bind_persona_context(PERSONA_A):
        due = await reminder_service.create(db_session, _create_model(due_at_ms=100))
        await reminder_service.create(db_session, _create_model(due_at_ms=10_000))
        cancelled = await reminder_service.create(db_session, _create_model(due_at_ms=100))
        await reminder_service.cancel(db_session, cancelled.id)
        scan = await reminder_service.scan_due_reminders(db_session, now_ms=1_000)
    assert scan.scanned == 1
    assert [item.id for item in scan.due] == [due.id]


async def test_scan_batch_cap_and_persona_isolation(db_session: AsyncSession) -> None:
    """扫描：批量帽生效（limit 收窄）+ 跨人格不可见（作用域隔离）。"""
    async with bind_persona_context(PERSONA_A):
        for index in range(3):
            await reminder_service.create(db_session, _create_model(title=f"a{index}", due_at_ms=1))
        capped = await reminder_service.scan_due_reminders(db_session, now_ms=10, limit=2)
    async with bind_persona_context(PERSONA_B):
        await reminder_service.create(db_session, _create_model(title="b", due_at_ms=1))
        isolated = await reminder_service.scan_due_reminders(db_session, now_ms=10)
    assert capped.scanned == 2
    assert isolated.scanned == 1
    assert isolated.due[0].title == "b"
    assert SCAN_BATCH_LIMIT >= 2


async def test_mark_delivered_sets_terminal_state(db_session: AsyncSession) -> None:
    """投递成功回写：delivered + 时刻 + last_error 清空；扫描不再命中。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model(due_at_ms=1))
        marked = await reminder_service.mark_delivered(
            db_session, [record.id], delivered_at_ms=5_000
        )
        scan = await reminder_service.scan_due_reminders(db_session, now_ms=10)
    assert marked[0].status is ReminderStatus.DELIVERED
    assert marked[0].delivered_at_ms == 5_000
    assert marked[0].last_error is None
    assert scan.scanned == 0


async def test_mark_delivered_missing_not_found(db_session: AsyncSession) -> None:
    """投递成功回写：行不存在 → NOT_FOUND（快照与库态不一致的异常路径）。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await reminder_service.mark_delivered(db_session, [404], delivered_at_ms=1)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_mark_failed_counts_up_to_limit(db_session: AsyncSession) -> None:
    """投递失败：attempts 递增；达上限转 failed（仍 pending 期可重试）。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model(due_at_ms=1))
        states: list[ReminderStatus] = []
        for attempt in range(1, DELIVERY_FAILURE_LIMIT + 1):
            failed = await reminder_service.mark_failed(
                db_session, record.id, error=f"第 {attempt} 次失败"
            )
            states.append(failed.status)
            assert failed.delivery_attempts == attempt
    assert states[:-1] == [ReminderStatus.PENDING] * (DELIVERY_FAILURE_LIMIT - 1)
    assert states[-1] is ReminderStatus.FAILED
    assert failed.last_error == f"第 {DELIVERY_FAILURE_LIMIT} 次失败"


async def test_mark_failed_truncates_long_error(db_session: AsyncSession) -> None:
    """投递失败：超长错误文本落库截断（512 字符列宽守卫）。"""
    async with bind_persona_context(PERSONA_A):
        record = await reminder_service.create(db_session, _create_model(due_at_ms=1))
        failed = await reminder_service.mark_failed(db_session, record.id, error="错" * 900)
    assert failed.last_error is not None
    assert len(failed.last_error) == 512


async def test_count_pending_scoped(db_session: AsyncSession) -> None:
    """待投递计数：人格作用域内口径（他人格行不计）。"""
    async with bind_persona_context(PERSONA_A):
        await reminder_service.create(db_session, _create_model())
        await reminder_service.create(db_session, _create_model())
        count_a = await reminder_service.count_pending(db_session)
    async with bind_persona_context(PERSONA_B):
        count_b = await reminder_service.count_pending(db_session)
    assert count_a == 2
    assert count_b == 0
