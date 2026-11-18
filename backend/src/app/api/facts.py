"""人物事实管理 admin 路由——结构化事实 / 证据链 / 更正循环的操作面。

    - GET    /api/v1/facts              —— 事实列表（类别 / 状态过滤 + 游标）
    - POST   /api/v1/facts              —— 手工录入事实（owner 来源）
    - PUT    /api/v1/facts/{id}         —— 主人更正（supersede 链 +1）
    - POST   /api/v1/facts/{id}/reject  —— 驳回（active → rejected，幂等）
    - DELETE /api/v1/facts/{id}         —— 硬删除（管理面显式动作）

存储面：persona 域 ``ff_persona_facts``（见 ``harness/persona/facts``）——
本路由只做人格作用域解析与类型化出入口；supersede / 证据链 / 归一化
指纹语义由服务层承担。

人格作用域约定（与记忆面同轴）：端点先解析目标人格（``persona_id``
查询参数显式指定；缺省 = 当前主人格，无主人格则 ``NOT_FOUND`` fail
loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。指定人格不
存在 → 404；他人格的事实对本作用域不可见（同样 404——隔离语义）。

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
from packages.harness.persona.facts import FactsService
from packages.harness.persona.facts_models import (
    FactCorrectDTO,
    FactCreateDTO,
    FactQueryDTO,
    FactRecordDTO,
    PersonaFactCategory,
    PersonaFactStatus,
)
from packages.harness.persona.service import bind_persona_context

router = APIRouter(prefix="/api/v1/facts", tags=["facts"])

#: 人物事实服务实例（无状态——Session 逐请求注入，服务可安全共享）。
facts_service = FactsService()


@router.get("", response_model=ResultDTO[PageResponse[FactRecordDTO]])
async def list_facts(
    category: PersonaFactCategory | None = Query(None, description="类别过滤（None = 全部）"),
    status: PersonaFactStatus = Query(
        PersonaFactStatus.ACTIVE, description="状态过滤（缺省 active）"
    ),
    cursor: str | None = Query(None, description="分页游标（首页为空）"),
    limit: int = Query(20, ge=1, le=200, description="页大小"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[FactRecordDTO]]:
    """列出人物事实（置信度降序——高可信事实在前）。

    Args:
        category: 类别过滤（None = 全部类别）。
        status: 状态过滤（缺省 active——历史 / 已驳回需显式指定）。
        cursor: 分页游标（首页为空）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        事实分页响应（游标模式——total 恒 -1）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    query = FactQueryDTO(category=category, status=status, cursor=cursor, limit=limit)
    async with bind_persona_context(persona.id):
        page = await facts_service.list_facts(db, query)
    return ResultDTO.ok(data=page, message="人物事实列表已获取")


@router.post("", response_model=ResultDTO[FactRecordDTO])
async def create_fact(
    req: FactCreateDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[FactRecordDTO]:
    """手工录入人物事实（owner 来源；同归一化身份收敛为元数据刷新）。

    Args:
        req: 创建入参（类别 / 正文 / 置信度 / 可选证据）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        写入后的现行事实记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            正文边界非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await facts_service.upsert_fact(db, req)
    return ResultDTO.ok(data=result.record, message="人物事实已保存")


@router.put("/{fact_id}", response_model=ResultDTO[FactRecordDTO])
async def correct_fact(
    fact_id: int,
    req: FactCorrectDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[FactRecordDTO]:
    """主人更正事实（新版本现行行 + 旧行 superseded + 证据保留追加）。

    Args:
        fact_id: 既有现行事实主键。
        req: 更正入参（新正文 / 可选新置信度）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        更正后的现行事实记录。

    Raises:
        BusinessException: 目标人格 / 事实不存在（``NOT_FOUND``）；非现行
            状态 / 正文边界非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await facts_service.correct_fact(db, fact_id, req)
    return ResultDTO.ok(data=record, message="人物事实已更正")


@router.post("/{fact_id}/reject", response_model=ResultDTO[FactRecordDTO])
async def reject_fact(
    fact_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[FactRecordDTO]:
    """驳回事实（active → rejected；已驳回的重复驳回幂等成功）。

    Args:
        fact_id: 事实主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        驳回后的事实记录。

    Raises:
        BusinessException: 目标人格 / 事实不存在（``NOT_FOUND``）；历史
            版本不可驳回（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await facts_service.reject_fact(db, fact_id)
    return ResultDTO.ok(data=record, message="人物事实已驳回")


@router.delete("/{fact_id}", response_model=ResultDTO[None])
async def delete_fact(
    fact_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """硬删除事实（证据链随之消失，不可恢复）。

    Args:
        fact_id: 事实主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        空载荷成功响应。

    Raises:
        BusinessException: 目标人格 / 事实不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        await facts_service.delete_fact(db, fact_id)
    return ResultDTO.ok(data=None, message="人物事实已删除")
