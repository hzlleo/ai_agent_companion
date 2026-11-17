"""reminders 域服务——持久化提醒的 CRUD / 到期扫描 / 投递终态回写。

职责链（管理面 + 调度面共用同一服务实例——服务无状态，Session 逐次
注入）::

    create(session, model)            创建（pending；title 边界 + 到期允许过去）
    query(session, dto)               游标分页（status 过滤——id 降序）
    get(session, reminder_id)         按主键读取（人格作用域外 NOT_FOUND）
    cancel(session, reminder_id)      取消（仅 pending——其余状态 VALIDATION_ERROR）
    batch_cancel(session, ids)        批量取消（逐 id 独立判定——部分成功形态）
    scan_due_reminders(session, now)  到期扫描（pending 且 due_at_ms <= now）
    mark_delivered(session, ids, ts)  投递成功回写（status=delivered + 时刻）
    mark_failed(session, id, error)   投递失败回写（attempts++；达上限转 failed）

扫描并发语义（锁定决策）：

    - 选中语句带 ``FOR UPDATE SKIP LOCKED``（MySQL 8 / SeekDB 支持——并发
      扫描实例抢占不重叠的行）；SQLite 测试方言忽略该子句（单写者语义
      天然串行），方言差异文档化；
    - **扫描不投递**：本方法只返回到期行快照并结束事务（锁随提交释放），
      投递（网络 I/O）在事务之外由调用方执行——长网络等待不持行锁；
    - 投递成功后才 ``mark_delivered``；失败经 ``mark_failed`` 记账——
      「投递在库外、终态在库内」的窗口若进程崩溃，行保持 pending，下轮
      扫描重投（至少一次语义，文档化）。

失败上限（锁定）：连续失败达到 :data:`DELIVERY_FAILURE_LIMIT` 次后行转
``failed``（``last_error`` 保留末次原因）——不再重试，避免坏通道无限
打点。

人格作用域：全部方法与记忆域同轴（PersonaMixin 自动注入 / 过滤）——
调用方须先经 ``bind_persona_context`` 绑定上下文（API 面在解析目标
人格后绑定；扫描作业在解析主人格后绑定），否则 fail loud。
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.reminders.models import (
    ReminderBatchCancelItemDTO,
    ReminderBatchCancelResponseDTO,
    ReminderCreateModel,
    ReminderQueryDTO,
    ReminderRecordDTO,
    ReminderScanResultModel,
    ReminderSource,
    ReminderStatus,
)
from packages.harness.reminders.orm import ReminderRow

__all__ = [
    "DELIVERY_FAILURE_LIMIT",
    "SCAN_BATCH_LIMIT",
    "REMINDER_TITLE_MAX_LENGTH",
    "ReminderService",
]

#: 投递连续失败上限（达到即转 failed——见模块 docstring 锁定决策）。
DELIVERY_FAILURE_LIMIT: int = 3

#: 单轮扫描批量上限（到期行逐批投递——防一次扫描拉爆内存 / 通道限流）。
SCAN_BATCH_LIMIT: int = 100

#: 提醒标题长度上限（与 ORM 列宽一致——服务层边界复核）。
REMINDER_TITLE_MAX_LENGTH: int = 200

#: last_error 落库截断长度（与 ORM 列宽一致——通道异常文本可能超长）。
_LAST_ERROR_MAX_LENGTH: int = 512


def _reminder_record(row: ReminderRow) -> ReminderRecordDTO:
    """ORM 行 → 记录 DTO（字段直映——枚举值域校验在 DTO 边界）。

    Args:
        row: 提醒行。

    Returns:
        提醒记录 DTO。
    """
    return ReminderRecordDTO(
        id=row.id,
        persona_id=row.persona_id,
        title=row.title,
        due_at_ms=row.due_at_ms,
        status=ReminderStatus(row.status),
        delivery_attempts=row.delivery_attempts,
        last_error=row.last_error,
        delivered_at_ms=row.delivered_at_ms,
        source=ReminderSource(row.source),
        meta=row.meta,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ReminderService:
    """提醒服务（CRUD + 到期扫描 + 投递终态回写——无状态，Session 注入）。"""

    async def create(self, session: AsyncSession, model: ReminderCreateModel) -> ReminderRecordDTO:
        """创建提醒（pending；标题裁剪后校验长度，到期时刻允许过去）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            model: 创建入参。

        Returns:
            创建后的记录 DTO。

        Raises:
            BusinessException: 标题裁剪后为空 / 超长（``VALIDATION_ERROR``）。
        """
        title = model.title.strip()
        if not title or len(title) > REMINDER_TITLE_MAX_LENGTH:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="提醒标题须为 1-200 字符（首尾空白不计）",
                details={"length": len(title)},
            )
        row = ReminderRow(
            title=title,
            due_at_ms=model.due_at_ms,
            status=ReminderStatus.PENDING.value,
            delivery_attempts=0,
            last_error=None,
            delivered_at_ms=None,
            source=model.source.value,
            meta=model.meta,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            "提醒已创建: id={}, due_at_ms={}, source={}", row.id, row.due_at_ms, row.source
        )
        return _reminder_record(row)

    async def get(self, session: AsyncSession, reminder_id: int) -> ReminderRecordDTO:
        """按主键读取提醒（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_id: 提醒主键。

        Returns:
            记录 DTO。

        Raises:
            BusinessException: 提醒不存在（``NOT_FOUND``）。
        """
        row = await self._require(session, reminder_id)
        return _reminder_record(row)

    async def query(
        self, session: AsyncSession, query: ReminderQueryDTO
    ) -> PageResponse[ReminderRecordDTO]:
        """游标分页查询提醒（status 过滤；id 降序——新提醒在前）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询 DTO。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
        """
        stmt = select(ReminderRow)
        if query.status is not None:
            stmt = stmt.where(ReminderRow.status == query.status.value)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(ReminderRow.id < cursor_id)
        stmt = stmt.order_by(ReminderRow.id.desc()).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[_reminder_record(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def cancel(self, session: AsyncSession, reminder_id: int) -> ReminderRecordDTO:
        """取消提醒（仅 pending——已投递 / 已取消 / 已失败拒绝）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_id: 提醒主键。

        Returns:
            取消后的记录 DTO（status=cancelled）。

        Raises:
            BusinessException: 提醒不存在（``NOT_FOUND``）；状态非 pending
                （``VALIDATION_ERROR``——携当前状态）。
        """
        row = await self._require(session, reminder_id)
        if row.status != ReminderStatus.PENDING.value:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="仅待投递提醒可以取消",
                details={"id": reminder_id, "status": row.status},
            )
        row.status = ReminderStatus.CANCELLED.value
        await session.commit()
        await session.refresh(row)
        logger.info("提醒已取消: id={}", reminder_id)
        return _reminder_record(row)

    async def batch_cancel(
        self, session: AsyncSession, reminder_ids: list[int]
    ) -> ReminderBatchCancelResponseDTO:
        """批量取消（逐 id 独立判定——部分成功形态）。

        语义（与情景事件批量删除同轴）：逐 id 独立提交与判定，单项失败
        不回滚其他项；返回逐 id 结果（与提交顺序一致）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_ids: 待取消提醒主键列表（调用方已保证非空且不重复）。

        Returns:
            批量取消响应（total / succeeded / failed / items）。
        """
        items: list[ReminderBatchCancelItemDTO] = []
        for reminder_id in reminder_ids:
            try:
                await self.cancel(session, reminder_id)
            except BusinessException as exc:
                items.append(
                    ReminderBatchCancelItemDTO(
                        id=reminder_id,
                        success=False,
                        code=exc.code,
                        message=exc.message,
                    )
                )
            else:
                items.append(
                    ReminderBatchCancelItemDTO(id=reminder_id, success=True, message="已取消")
                )
        succeeded = sum(1 for item in items if item.success)
        return ReminderBatchCancelResponseDTO(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            items=items,
        )

    async def scan_due_reminders(
        self,
        session: AsyncSession,
        *,
        now_ms: int,
        limit: int = SCAN_BATCH_LIMIT,
    ) -> ReminderScanResultModel:
        """扫描到期待投递提醒（pending 且 due_at_ms <= now——快照返回）。

        语句带 ``FOR UPDATE SKIP LOCKED``（MySQL 系并发抢占不重叠；SQLite
        忽略）；**本方法不投递**——返回快照后调用方结束事务再投递（见
        模块 docstring 扫描并发语义）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定——扫描恒在
                单一人格作用域内进行）。
            now_ms: 判定时刻（epoch 毫秒——注入式，测试可控）。
            limit: 批量上限（1-:data:`SCAN_BATCH_LIMIT`）。

        Returns:
            扫描结果（``due`` 按 id 升序——投递顺序确定）。
        """
        batch = max(1, min(limit, SCAN_BATCH_LIMIT))
        stmt = (
            select(ReminderRow)
            .where(
                ReminderRow.status == ReminderStatus.PENDING.value,
                ReminderRow.due_at_ms <= now_ms,
            )
            .order_by(ReminderRow.id.asc())
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        due = [_reminder_record(row) for row in rows]
        logger.debug("提醒扫描: now_ms={}, due={}", now_ms, len(due))
        return ReminderScanResultModel(scanned=len(due), due=due)

    async def mark_delivered(
        self,
        session: AsyncSession,
        reminder_ids: list[int],
        *,
        delivered_at_ms: int,
    ) -> list[ReminderRecordDTO]:
        """投递成功回写（status=delivered + 投递时刻；attempts 保留历史）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_ids: 投递成功的提醒主键列表。
            delivered_at_ms: 投递成功时刻（epoch 毫秒）。

        Returns:
            回写后的记录 DTO 列表（与入参顺序一致）。

        Raises:
            BusinessException: 任一行不存在（``NOT_FOUND``——扫描快照与
                库态不一致，属异常路径）。
        """
        records: list[ReminderRecordDTO] = []
        for reminder_id in reminder_ids:
            row = await self._require(session, reminder_id)
            row.status = ReminderStatus.DELIVERED.value
            row.delivered_at_ms = delivered_at_ms
            row.last_error = None
            records.append(_reminder_record(row))
        await session.commit()
        logger.info("提醒投递成功回写: ids={}, delivered_at_ms={}", reminder_ids, delivered_at_ms)
        return records

    async def mark_failed(
        self,
        session: AsyncSession,
        reminder_id: int,
        *,
        error: str,
    ) -> ReminderRecordDTO:
        """投递失败回写（attempts++；达上限转 failed，last_error 记录原因）。

        未达上限时行保持 pending——下一轮扫描重试（连续失败语义）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_id: 提醒主键。
            error: 失败原因（落库截断至 512 字符）。

        Returns:
            回写后的记录 DTO（pending 或 failed）。

        Raises:
            BusinessException: 提醒不存在（``NOT_FOUND``）。
        """
        row = await self._require(session, reminder_id)
        attempts = row.delivery_attempts + 1
        row.delivery_attempts = attempts
        row.last_error = error[:_LAST_ERROR_MAX_LENGTH]
        if attempts >= DELIVERY_FAILURE_LIMIT:
            row.status = ReminderStatus.FAILED.value
        await session.commit()
        await session.refresh(row)
        logger.warning(
            "提醒投递失败: id={}, attempts={}, status={}, error={}",
            reminder_id,
            attempts,
            row.status,
            error,
        )
        return _reminder_record(row)

    async def count_pending(self, session: AsyncSession) -> int:
        """统计当前人格的待投递提醒数量（观测面）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            待投递提醒行数。
        """
        stmt = select(func.count()).select_from(ReminderRow).where(
            ReminderRow.status == ReminderStatus.PENDING.value
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _require(self, session: AsyncSession, reminder_id: int) -> ReminderRow:
        """按主键取行（人格作用域外 / 缺失均 NOT_FOUND——隔离语义）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            reminder_id: 提醒主键。

        Returns:
            提醒行。

        Raises:
            BusinessException: 提醒不存在（``NOT_FOUND``）。
        """
        row = (
            await session.execute(select(ReminderRow).where(ReminderRow.id == reminder_id))
        ).scalar_one_or_none()
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="提醒不存在（或不属于当前人格）",
                details={"id": reminder_id},
            )
        return row
