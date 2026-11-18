"""技能管理 admin 路由——程序性技能 / 触发短语 / 启用状态的操作面。

    - GET    /api/v1/skills                  —— 技能列表（状态过滤 + 游标）
    - POST   /api/v1/skills                  —— 手工创建技能（slug 由名称派生）
    - PUT    /api/v1/skills/{id}             —— 原地编辑（版本递增）
    - POST   /api/v1/skills/{id}/enable      —— 启用（参与 compose 匹配）
    - POST   /api/v1/skills/{id}/disable     —— 停用（不参与匹配）
    - DELETE /api/v1/skills/{id}             —— 硬删除（管理面显式动作）

存储面：persona 域 ``ff_persona_skills``（见 ``harness/persona/skills``）——
本路由只做人格作用域解析与类型化出入口；slug 归一 / 唯一键冲突 /
梦境合并语义由服务层承担。

人格作用域约定（与记忆 / 事实面同轴）：端点先解析目标人格（``persona_id``
查询参数显式指定；缺省 = 当前主人格，无主人格则 ``NOT_FOUND`` fail
loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。指定人格不
存在 → 404；他人格的技能对本作用域不可见（同样 404——隔离语义）。

全部响应经 ``ResultDTO`` 封装；DB Session 经 ``database.session.
get_async_db`` 依赖注入（事务边界归服务层）；全部参数与请求体为类型化
Model Classes（无裸 dict 出入口）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reminders import resolve_target_persona
from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.persona.service import bind_persona_context
from packages.harness.persona.skills import SkillsService
from packages.harness.persona.skills_models import (
    SkillCreateModel,
    SkillQueryDTO,
    SkillRecordDTO,
    SkillStatus,
    SkillUpdateModel,
)

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

#: 技能服务实例（无状态——Session 逐请求注入，服务可安全共享；命中计数
#: 写入由服务内部 fire-and-forget 调度）。
skills_service = SkillsService()


@router.get("", response_model=ResultDTO[PageResponse[SkillRecordDTO]])
async def list_skills(
    status: SkillStatus | None = Query(None, description="状态过滤（None = 全部状态）"),
    cursor: str | None = Query(None, description="分页游标（首页为空）"),
    limit: int = Query(20, ge=1, le=200, description="页大小"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[SkillRecordDTO]]:
    """列出技能（使用次数降序——高频技能在前）。

    Args:
        status: 状态过滤（None = 全部状态）。
        cursor: 分页游标（首页为空）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        技能分页响应（游标模式——total 恒 -1）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    query = SkillQueryDTO(status=status, cursor=cursor, limit=limit)
    async with bind_persona_context(persona.id):
        page = await skills_service.list_skills(db, query)
    return ResultDTO.ok(data=page, message="技能列表已获取")


@router.post("", response_model=ResultDTO[SkillRecordDTO])
async def create_skill(
    req: SkillCreateModel,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[SkillRecordDTO]:
    """手工创建技能（同名 slug 冲突返回 409——不静默覆盖）。

    Args:
        req: 创建入参（名称 / 描述 / 触发短语 / 正文 / 置信度）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        新建的技能记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            名称无法派生标识（``VALIDATION_ERROR``）；同人格同名技能已
            存在（``CONFLICT``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await skills_service.create(db, req)
    return ResultDTO.ok(data=record, message="技能已创建")


@router.put("/{skill_id}", response_model=ResultDTO[SkillRecordDTO])
async def update_skill(
    skill_id: int,
    req: SkillUpdateModel,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[SkillRecordDTO]:
    """原地编辑技能（版本递增——不产生历史行）。

    Args:
        skill_id: 技能主键。
        req: 编辑入参（字段全部可选——None 表示保留）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        编辑后的技能记录。

    Raises:
        BusinessException: 目标人格 / 技能不存在（``NOT_FOUND``）；新名称
            无法派生标识（``VALIDATION_ERROR``）；新 slug 冲突
            （``CONFLICT``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await skills_service.update(db, skill_id, req)
    return ResultDTO.ok(data=record, message="技能已保存")


@router.post("/{skill_id}/enable", response_model=ResultDTO[SkillRecordDTO])
async def enable_skill(
    skill_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[SkillRecordDTO]:
    """启用技能（参与 compose 匹配；幂等）。

    Args:
        skill_id: 技能主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        启用后的技能记录。

    Raises:
        BusinessException: 目标人格 / 技能不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await skills_service.set_status(db, skill_id, SkillStatus.ACTIVE)
    return ResultDTO.ok(data=record, message="技能已启用")


@router.post("/{skill_id}/disable", response_model=ResultDTO[SkillRecordDTO])
async def disable_skill(
    skill_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[SkillRecordDTO]:
    """停用技能（不参与 compose 匹配；幂等）。

    Args:
        skill_id: 技能主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        停用后的技能记录。

    Raises:
        BusinessException: 目标人格 / 技能不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await skills_service.set_status(db, skill_id, SkillStatus.DISABLED)
    return ResultDTO.ok(data=record, message="技能已停用")


@router.delete("/{skill_id}", response_model=ResultDTO[None])
async def delete_skill(
    skill_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """硬删除技能（不可恢复）。

    Args:
        skill_id: 技能主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        空载荷成功响应。

    Raises:
        BusinessException: 目标人格 / 技能不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        await skills_service.delete(db, skill_id)
    return ResultDTO.ok(data=None, message="技能已删除")
