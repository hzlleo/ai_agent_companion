"""提醒管理 admin 路由——持久化提醒的操作面（P10=B）。

    - POST /api/v1/reminders                  —— 创建（persona 解析同记忆面）
    - GET  /api/v1/reminders                  —— 游标分页（status 过滤）
    - GET  /api/v1/reminders/{id}             —— 读取（管理面观测）
    - POST /api/v1/reminders/{id}/cancel      —— 取消（仅 pending）
    - POST /api/v1/reminders/batch-cancel     —— 批量取消（部分成功形态）

人格作用域约定（与记忆面同轴）：端点先解析目标人格（``persona_id``
查询参数显式指定；缺省 = 当前主人格，无主人格则 ``NOT_FOUND`` fail
loud），再经 ``bind_persona_context`` 绑定上下文后调用服务（自动注入 /
自动过滤）。指定人格不存在 → 404；他人格的提醒对本作用域不可见（同样
404——隔离语义）。

投递面（锁定语义，文档化于 docs/backend/reminders.md）：本路由只读写
库；到期投递由中央调度器的扫描作业经属主通道执行——无通道时提醒保持
pending（不消耗失败次数），通道恢复后自动补投。

全部响应经 ``ResultDTO`` 封装；分页经 ``PageResponse``；DB Session 经
``database.session.get_async_db`` 依赖注入（事务边界归服务层）；全部
参数与请求体为类型化 Model Classes（无裸 dict 出入口）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import get_async_db
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.reminders.models import (
    ReminderBatchCancelRequest,
    ReminderBatchCancelResponseDTO,
    ReminderCreateModel,
    ReminderQueryDTO,
    ReminderRecordDTO,
    ReminderStatus,
)
from packages.harness.reminders.service import ReminderService

router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])

#: 提醒服务实例（无状态——Session 逐请求注入，服务可安全共享）。
reminder_service = ReminderService()
#: 人格服务实例（目标人格解析面）。
persona_service = PersonaService()


async def resolve_target_persona(db: AsyncSession, persona_id: int | None) -> PersonaRecordDTO:
    """解析目标人格（显式 persona_id 优先；缺省当前主人格）。

    app/api 层的公共小件（提醒 / 长期指令两个 persona 作用域路由共用——
    与 memories 路由解析语义逐字同轴：无主人格的错误文案携带可操作
    指引，不复制第三份实现）。

    Args:
        db: 注入的异步 Session。
        persona_id: 显式人格主键（None = 主人格）。

    Returns:
        目标人格记录 DTO。

    Raises:
        BusinessException: 指定人格不存在（``NOT_FOUND``）；未指定且
            当前无主人格（``NOT_FOUND``——fail loud）。
    """
    if persona_id is not None:
        return await persona_service.get(db, persona_id)
    try:
        return await persona_service.get_primary_persona(db)
    except BusinessException as exc:
        if exc.code != ErrorCode.NOT_FOUND:
            raise
        raise BusinessException(
            code=ErrorCode.NOT_FOUND,
            message="当前没有主人格，请先设置一个人格为主人格再操作提醒面",
            details={"fix": "显式传入 persona_id 查询参数，或将一个人格设为主人格"},
        ) from exc


@router.post("", response_model=ResultDTO[ReminderRecordDTO])
async def create_reminder(
    req: ReminderCreateModel,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[ReminderRecordDTO]:
    """创建提醒（pending；到期时刻允许过去——创建即到期的补投语义）。

    Args:
        req: 创建入参（title / due_at_ms / source / meta）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        创建后的提醒记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            标题边界非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await reminder_service.create(db, req)
    return ResultDTO.ok(data=record, message="提醒已创建")


@router.get("", response_model=ResultDTO[PageResponse[ReminderRecordDTO]])
async def list_reminders(
    status: ReminderStatus | None = Query(None, description="状态过滤（None = 全部）"),
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[ReminderRecordDTO]]:
    """提醒游标分页列表（status 过滤；id 降序——新提醒在前）。

    Args:
        status: 状态过滤（None = 全部）。
        cursor: 分页游标（首页为 None）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早记录）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        page = await reminder_service.query(
            db, ReminderQueryDTO(status=status, cursor=cursor, limit=limit)
        )
    return ResultDTO.ok(data=page, message="提醒列表已获取")


@router.get("/{reminder_id}", response_model=ResultDTO[ReminderRecordDTO])
async def get_reminder(
    reminder_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[ReminderRecordDTO]:
    """按主键读取提醒（他人格 / 缺失均 404——隔离语义）。

    Args:
        reminder_id: 提醒主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        提醒记录。

    Raises:
        BusinessException: 目标人格 / 提醒不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await reminder_service.get(db, reminder_id)
    return ResultDTO.ok(data=record, message="提醒已获取")


@router.post("/{reminder_id}/cancel", response_model=ResultDTO[ReminderRecordDTO])
async def cancel_reminder(
    reminder_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[ReminderRecordDTO]:
    """取消提醒（仅 pending——已投递 / 已取消 / 已失败拒绝）。

    Args:
        reminder_id: 提醒主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        取消后的提醒记录（status=cancelled）。

    Raises:
        BusinessException: 目标人格 / 提醒不存在（``NOT_FOUND``）；状态
            非 pending（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await reminder_service.cancel(db, reminder_id)
    return ResultDTO.ok(data=record, message="提醒已取消")


@router.post("/batch-cancel", response_model=ResultDTO[ReminderBatchCancelResponseDTO])
async def batch_cancel_reminders(
    req: ReminderBatchCancelRequest,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[ReminderBatchCancelResponseDTO]:
    """批量取消提醒（部分成功形态——逐 id 独立判定）。

    Args:
        req: 批量取消请求（ids 1-100 个且不重复）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        批量取消响应（total / succeeded / failed / items——逐 id 结果）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await reminder_service.batch_cancel(db, req.ids)
    return ResultDTO.ok(data=result, message="批量取消已完成")
