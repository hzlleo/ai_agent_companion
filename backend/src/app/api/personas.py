"""人格管理 admin 路由——多人格注册表全操作面（多激活可寻址模型）。

    - GET    /api/v1/personas                        —— 游标分页列表（status 过滤）
    - POST   /api/v1/personas                        —— 创建
    - POST   /api/v1/personas/batch-delete           —— 批量删除（部分成功形态）
    - GET    /api/v1/personas/primary                —— 读取当前主人格（无主人格 404）
    - GET    /api/v1/personas/{id}                   —— 读取
    - PUT    /api/v1/personas/{id}                   —— 部分更新（重命名 / 状态 / 卡片补丁）
    - DELETE /api/v1/personas/{id}                   —— 删除（主人格守卫见服务层）
    - POST   /api/v1/personas/{id}/copy              —— 复制（可选深拷贝 M/S 状态）
    - POST   /api/v1/personas/{id}/set-primary       —— 设为主人格（事务性切换）
    - POST   /api/v1/personas/{id}/disable           —— 停用（生命周期；主人格守卫拒绝）
    - GET    /api/v1/personas/{id}/card              —— 读取卡片
    - PUT    /api/v1/personas/{id}/card              —— 整体替换卡片
    - GET    /api/v1/personas/{id}/states            —— 读取已写状态层
    - PUT    /api/v1/personas/{id}/states/{layer}    —— 状态层 upsert（M/S）

全部响应经 ``ResultDTO`` 封装；分页经 ``PageResponse``；DB Session 经
``database.session.get_async_db`` 依赖注入（事务边界归服务层）；全部
参数与请求体为类型化 Model Classes（无裸 dict 出入口）。注册表是全局
管理面数据（无人格上下文自动过滤——非 PersonaMixin 决策见
harness.persona.orm）。``/primary`` 字面路由先于 ``/{persona_id}``
注册（FastAPI 顺序匹配——字面路由须前置防路径参数吞并）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    PersonaBatchDeleteRequest,
    PersonaBatchDeleteResponseDTO,
    PersonaCardDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.models import PersonaStateLayer, PersonaStatus
from packages.harness.persona.service import PersonaService

router = APIRouter(prefix="/api/v1/personas", tags=["personas"])

#: 人格服务实例（无状态——Session 逐请求注入，服务可安全共享）。
persona_service = PersonaService()


@router.get("", response_model=ResultDTO[PageResponse[PersonaRecordDTO]])
async def list_personas(
    status: PersonaStatus | None = Query(None, description="可用性状态过滤（None = 全部）"),
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[PersonaRecordDTO]]:
    """人格游标分页列表（id 降序——新人格在前；status 可选过滤）。

    Args:
        status: 可用性状态过滤（None = 全部）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200）。
        db: 注入的异步 Session。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
    """
    page = await persona_service.query(
        db, PersonaQueryDTO(status=status, cursor=cursor, limit=limit)
    )
    return ResultDTO.ok(data=page)


@router.post("", response_model=ResultDTO[PersonaRecordDTO])
async def create_persona(
    req: PersonaCreateDTO,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """创建人格（注册名唯一校验；status 缺省 disabled；非主人格）。

    Args:
        req: 创建请求（注册名 + 卡片必填）。
        db: 注入的异步 Session。

    Returns:
        落库后的记录 DTO。

    Raises:
        BusinessException: 注册名已占用（``CONFLICT`` → 409）。
    """
    record = await persona_service.create(db, req)
    return ResultDTO.ok(data=record, message="人格已创建")


@router.post("/batch-delete", response_model=ResultDTO[PersonaBatchDeleteResponseDTO])
async def batch_delete_personas(
    req: PersonaBatchDeleteRequest,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaBatchDeleteResponseDTO]:
    """批量删除人格（部分成功形态——逐 id 独立事务，单项失败不阻塞其余）。

    Args:
        req: 批量删除请求（ids 1-100 个）。
        db: 注入的异步 Session。

    Returns:
        批量结果（total / succeeded / failed / 逐 id 结果——主人格项
        记 CONFLICT 失败，缺失项记 NOT_FOUND 失败）。
    """
    result = await persona_service.batch_delete(db, req.ids)
    return ResultDTO.ok(
        data=result, message=f"批量删除完成（成功 {result.succeeded} / 失败 {result.failed}）"
    )


@router.get("/primary", response_model=ResultDTO[PersonaRecordDTO])
async def get_primary_persona(
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """读取当前主人格（无主人格 fail loud）。

    Args:
        db: 注入的异步 Session。

    Returns:
        当前主人格记录 DTO。

    Raises:
        BusinessException: 当前无主人格（``NOT_FOUND`` → 404——清晰
            中文错误指引先 set-primary）。
    """
    record = await persona_service.get_primary_persona(db)
    return ResultDTO.ok(data=record)


@router.get("/{persona_id}", response_model=ResultDTO[PersonaRecordDTO])
async def get_persona(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """按主键读取人格。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        记录 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND`` → 404）。
    """
    record = await persona_service.get(db, persona_id)
    return ResultDTO.ok(data=record)


@router.put("/{persona_id}", response_model=ResultDTO[PersonaRecordDTO])
async def update_persona(
    persona_id: int,
    req: PersonaUpdateDTO,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """部分更新人格（仅出现字段生效；不含主人格标记——主人格经 set-primary）。

    Args:
        persona_id: 人格主键。
        req: 更新请求（重命名 / 展示名 / 状态 / 卡片补丁）。
        db: 注入的异步 Session。

    Returns:
        更新后的记录 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）；注册名被占用
            （``CONFLICT``）；停用当前主人格（``CONFLICT``——两步守卫）；
            卡片合并校验失败（``VALIDATION_ERROR``）。
    """
    record = await persona_service.update(db, persona_id, req)
    return ResultDTO.ok(data=record, message="人格已更新")


@router.delete("/{persona_id}", response_model=ResultDTO[None])
async def delete_persona(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """删除人格（主人格守卫：当前主人格禁止删除——先另设主人格）。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        空 data 成功响应。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）；当前主人格禁止
            删除（``CONFLICT``）。
    """
    await persona_service.delete(db, persona_id)
    return ResultDTO[None].ok(message="人格已删除")


@router.post("/{persona_id}/copy", response_model=ResultDTO[PersonaRecordDTO])
async def copy_persona(
    persona_id: int,
    req: PersonaCopyDTO,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """复制人格（新注册名；可选深拷贝 M/S 状态；产物恒 disabled + 非主人格）。

    Args:
        persona_id: 源人格主键。
        req: 复制请求（new_name + include_states）。
        db: 注入的异步 Session。

    Returns:
        复制产物的记录 DTO。

    Raises:
        BusinessException: 源人格不存在（``NOT_FOUND``）；新注册名被占用
            （``CONFLICT``）。
    """
    record = await persona_service.copy(db, persona_id, req)
    return ResultDTO.ok(data=record, message="人格已复制")


@router.post("/{persona_id}/set-primary", response_model=ResultDTO[PersonaRecordDTO])
async def set_persona_primary(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """设为主人格（事务性切换——设 B 为主必然解除 A；DB 唯一键强制）。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        设为主人格后的记录 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）；停用状态的人格
            不可设为主人格（``CONFLICT``）；并发切换重试后仍冲突
            （``INTERNAL_ERROR``）。
    """
    record = await persona_service.set_primary(db, persona_id)
    return ResultDTO.ok(data=record, message="人格已设为主人格（事务性切换）")


@router.post("/{persona_id}/disable", response_model=ResultDTO[PersonaRecordDTO])
async def disable_persona(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaRecordDTO]:
    """停用人格（纯生命周期操作——status 切 disabled；幂等）。

    主人格守卫：停用当前主人格被拒绝（``CONFLICT``——先另设主人格）。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        停用后的记录 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）；停用当前主人格
            （``CONFLICT``）。
    """
    record = await persona_service.disable(db, persona_id)
    return ResultDTO.ok(data=record, message="人格已停用")


@router.get("/{persona_id}/card", response_model=ResultDTO[PersonaCardDTO])
async def get_persona_card(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaCardDTO]:
    """读取人格卡片（chara card v2 文档）。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        卡片读取 DTO（含最近更新时间）。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）。
    """
    card = await persona_service.get_card(db, persona_id)
    return ResultDTO.ok(data=card)


@router.put("/{persona_id}/card", response_model=ResultDTO[PersonaCardDTO])
async def replace_persona_card(
    persona_id: int,
    req: CharaCardModel,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaCardDTO]:
    """整体替换人格卡片（PUT 语义——非补丁合并；补丁合并走 PUT ``/{id}``）。

    Args:
        persona_id: 人格主键。
        req: 完整卡片请求（chara card v2 文档）。
        db: 注入的异步 Session。

    Returns:
        替换后的卡片读取 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）。
    """
    card = await persona_service.replace_card(db, persona_id, req)
    return ResultDTO.ok(data=card, message="人格卡片已替换")


@router.get("/{persona_id}/states", response_model=ResultDTO[list[PersonaStateDTO]])
async def list_persona_states(
    persona_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[PersonaStateDTO]]:
    """读取人格全部已写状态层（M/S；layer 升序）。

    Args:
        persona_id: 人格主键。
        db: 注入的异步 Session。

    Returns:
        状态 DTO 列表（仅已写过的层；全新人格为空列表）。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）。
    """
    states = await persona_service.get_states(db, persona_id)
    return ResultDTO.ok(data=states)


@router.put("/{persona_id}/states/{layer}", response_model=ResultDTO[PersonaStateDTO])
async def upsert_persona_state(
    persona_id: int,
    layer: PersonaStateLayer,
    req: PersonaStateUpsertDTO,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaStateDTO]:
    """状态层 upsert（M=中期心情/关系；S=短期会话状态；state 整体替换）。

    Args:
        persona_id: 人格主键。
        layer: 状态层（路径参数——M / S）。
        req: 状态写入请求（state JSON 文档）。
        db: 注入的异步 Session。

    Returns:
        落库后的状态 DTO。

    Raises:
        BusinessException: 人格不存在（``NOT_FOUND``）。
    """
    state = await persona_service.upsert_state(db, persona_id, layer, req)
    return ResultDTO.ok(data=state, message="人格状态已更新")
