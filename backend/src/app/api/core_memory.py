"""核心记忆管理 admin 路由——自编辑自我模型块 / 锁定开关 / 修订历史的操作面。

    - GET /api/v1/core-memory/blocks                  —— 四块读取（首次读取懒建空行）
    - PUT /api/v1/core-memory/blocks/{block_key}      —— 主人编辑（可清空 / 可锁定）
    - GET /api/v1/core-memory/blocks/{block_key}/revisions —— 修订历史（最新在前）

存储面：persona 域 ``ff_core_memory_blocks`` / ``ff_core_memory_revisions``
（见 ``harness/persona/core_memory``）——本路由只做人格作用域解析与
类型化出入口；块序 / 版本推进 / 修订写入 / 锁定闸语义由服务层承担。

人格作用域约定（与记忆 / 事实 / 技能面同轴）：端点先解析目标人格
（``persona_id`` 查询参数显式指定；缺省 = 当前主人格，无主人格则
``NOT_FOUND`` fail loud），再经 ``bind_persona_context`` 绑定上下文后
调用服务。指定人格不存在 → 404；他人格的块对本作用域不可见（同样 404
——隔离语义；修订经块归属间接隔离）。

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
from packages.harness.persona.core_memory import CoreMemoryService
from packages.harness.persona.core_memory_models import (
    CORE_MEMORY_REVISIONS_PAGE_LIMIT,
    CoreBlockKey,
    CoreMemoryBlockDTO,
    CoreMemoryRevDTO,
    OwnerEditModel,
)
from packages.harness.persona.service import bind_persona_context

router = APIRouter(prefix="/api/v1/core-memory", tags=["core-memory"])

#: 核心记忆服务实例（无状态——Session 逐请求注入，服务可安全共享）。
core_memory_service = CoreMemoryService()


@router.get("/blocks", response_model=ResultDTO[list[CoreMemoryBlockDTO]])
async def list_blocks(
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[CoreMemoryBlockDTO]]:
    """读取四个核心记忆块（固定块序——自我认知 / 关于对方 / 我们的故事 / 备忘）。

    首次读取会懒建四个空块行（服务层语义——面板无需处理「块不存在」
    中间态）。

    Args:
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        四块记录列表（空块正文为空串）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        blocks = await core_memory_service.list_blocks(db)
    return ResultDTO.ok(data=blocks, message="核心记忆已获取")


@router.put("/blocks/{block_key}", response_model=ResultDTO[CoreMemoryBlockDTO])
async def update_block(
    block_key: CoreBlockKey,
    req: OwnerEditModel,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CoreMemoryBlockDTO]:
    """主人编辑核心记忆块（可清空正文 / 可切换锁定；内容变更留修订）。

    Args:
        block_key: 块键（封闭四元组——服务层校验非法值）。
        req: 编辑入参（新正文 0-2000 字符 / 可选锁定开关）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        编辑后的块记录（版本已推进；零变更时原样返回）。

    Raises:
        BusinessException: 目标人格 / 块不存在（``NOT_FOUND``）；正文
            超出上限（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        block = await core_memory_service.owner_update(db, block_key, req)
    return ResultDTO.ok(data=block, message="核心记忆已保存")


@router.get("/blocks/{block_key}/revisions", response_model=ResultDTO[list[CoreMemoryRevDTO]])
async def list_revisions(
    block_key: CoreBlockKey,
    limit: int = Query(
        CORE_MEMORY_REVISIONS_PAGE_LIMIT,
        ge=1,
        le=100,
        description="返回条数上限（最新在前）",
    ),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[CoreMemoryRevDTO]]:
    """读取块的修订历史（最新在前——版本 / 主键双键倒序）。

    Args:
        block_key: 块键（封闭四元组）。
        limit: 返回条数上限（1-100，缺省 20）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        修订记录列表（新版在前；空列表 = 该块尚无内容变更）。

    Raises:
        BusinessException: 目标人格 / 块不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        revisions = await core_memory_service.list_revisions(db, block_key, limit=limit)
    return ResultDTO.ok(data=revisions, message="核心记忆修订已获取")
