"""长期指令管理 admin 路由——常备指令（USER.md 式）的操作面（D7-14）。

    - GET    /api/v1/directives           —— 现行指令列表（active——新写入在前）
    - POST   /api/v1/directives           —— 创建（同 topic 覆盖为 supersede）
    - PUT    /api/v1/directives/{id}      —— 按既有 topic 重写（supersede 链 +1）
    - DELETE /api/v1/directives/{id}      —— 删除（管理面显式动作）

存储面：策展记忆域 ``kind=user_note`` + ``topic='directive:<slug>'`` 行
（见 ``harness/persona/directives``）——本路由只做人格作用域解析与
类型化出入口；supersede / 历史链语义由策展层承担。

人格作用域约定（与记忆面同轴）：端点先解析目标人格（``persona_id``
查询参数显式指定；缺省 = 当前主人格，无主人格则 ``NOT_FOUND`` fail
loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。指定人格不
存在 → 404；他人格的指令对本作用域不可见（同样 404——隔离语义）。

全部响应经 ``ResultDTO`` 封装；DB Session 经 ``database.session.
get_async_db`` 依赖注入（事务边界归服务层）；全部参数与请求体为类型化
Model Classes（无裸 dict 出入口）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reminders import resolve_target_persona
from packages.harness.core.dtos import ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.directives_models import (
    DirectiveCreateDTO,
    DirectiveRecordDTO,
    DirectiveUpdateDTO,
)
from packages.harness.persona.service import bind_persona_context

router = APIRouter(prefix="/api/v1/directives", tags=["directives"])

#: 长期指令服务实例（无状态——Session 逐请求注入，服务可安全共享）。
directives_service = DirectivesService()


@router.get("", response_model=ResultDTO[list[DirectiveRecordDTO]])
async def list_directives(
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[DirectiveRecordDTO]]:
    """列出现行长期指令（active 行——id 降序，新写入在前）。

    Args:
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        现行指令记录列表。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        records = await directives_service.list_active(db)
    return ResultDTO.ok(data=records, message="长期指令列表已获取")


@router.post("", response_model=ResultDTO[DirectiveRecordDTO])
async def create_directive(
    req: DirectiveCreateDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[DirectiveRecordDTO]:
    """创建长期指令（缺省指纹 topic；显式同 topic 覆盖为 supersede）。

    Args:
        req: 创建入参（content / topic）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        新建 / 取代后的现行指令记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            正文 / topic 边界非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await directives_service.create(db, req)
    return ResultDTO.ok(data=record, message="长期指令已保存")


@router.put("/{directive_id}", response_model=ResultDTO[DirectiveRecordDTO])
async def update_directive(
    directive_id: int,
    req: DirectiveUpdateDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[DirectiveRecordDTO]:
    """按既有 topic 重写长期指令（supersede 链 +1；旧行标记 superseded）。

    Args:
        directive_id: 既有指令主键（取其 topic 定链）。
        req: 更新入参（新正文）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        取代后的现行指令记录。

    Raises:
        BusinessException: 目标人格 / 指令不存在（``NOT_FOUND``）；
            正文非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await directives_service.update(db, directive_id, req)
    return ResultDTO.ok(data=record, message="长期指令已更新")


@router.delete("/{directive_id}", response_model=ResultDTO[None])
async def delete_directive(
    directive_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """删除长期指令（策展行删除——管理面显式动作）。

    Args:
        directive_id: 指令（策展行）主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        空载荷成功响应。

    Raises:
        BusinessException: 目标人格 / 指令不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        await directives_service.delete(db, directive_id)
    return ResultDTO.ok(data=None, message="长期指令已删除")
