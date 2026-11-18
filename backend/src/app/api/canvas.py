"""canvas 域管理 API——画布产物列表 / 创建 / 读取 / 修订 / 删除 / 导出。

    - GET    /api/v1/canvas                 —— 列表（类型过滤；最近修改在前）
    - POST   /api/v1/canvas                 —— 创建（标题 / 类型 / 自包含内容）
    - GET    /api/v1/canvas/{canvas_id}     —— 单条读取（含内容）
    - PATCH  /api/v1/canvas/{canvas_id}     —— 修订（标题 / 内容；revision 递增）
    - DELETE /api/v1/canvas/{canvas_id}     —— 删除（缺失主键幂等报告）
    - GET    /api/v1/canvas/{canvas_id}/export —— 单文件导出
      （Content-Type 按产物类型；Content-Disposition attachment）

存储面：canvas 域 ``ff_canvas_documents``（见
``harness/canvas/canvas_service``）——本路由只做人格作用域解析与类型化
出入口；校验 / 修订号推进 / 导出命名由服务层与模型层承担。

人格作用域约定（与表情包 / 声纹 / 记忆面同轴）：端点先解析目标人格
（``persona_id`` 查询参数显式指定；缺省当前主人格，无主人格 ``NOT_FOUND``
fail loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。他人格的
画布不可见（404 隔离语义）。

响应形态：管理面端点经 ``ResultDTO`` 封装；导出端点直出原始文本字节
（无法套 JSON 包裹——Content-Type 与文件名即契约）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reminders import resolve_target_persona
from packages.harness.canvas.canvas_models import (
    CanvasCreateDTO,
    CanvasDeleteResultModel,
    CanvasKind,
    CanvasQueryDTO,
    CanvasRecordDTO,
    CanvasUpdateDTO,
)
from packages.harness.canvas.canvas_service import CanvasService
from packages.harness.core.dtos import ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.persona.service import bind_persona_context

router = APIRouter(prefix="/api/v1/canvas", tags=["canvas"])

#: 画布服务实例（无状态——Session 逐请求注入，服务可安全共享）。
canvas_service = CanvasService()

#: 导出响应的附加头（文件名含 ASCII 回退名——非 ASCII 标题不落进头字段）。
_EXPORT_HEADERS_TEMPLATE: str = 'attachment; filename="{filename}"'


@router.get("", response_model=ResultDTO[list[CanvasRecordDTO]])
async def list_canvases(
    kind: CanvasKind | None = Query(None, description="产物类型过滤"),
    limit: int = Query(100, ge=1, le=200, description="返回条数上限"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[CanvasRecordDTO]]:
    """读取画布列表（最近修改在前；可选类型过滤）。

    Args:
        kind: 产物类型过滤（None = 全部）。
        limit: 返回条数上限（1-200）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        画布记录列表。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    query = CanvasQueryDTO(kind=kind, limit=limit)
    async with bind_persona_context(persona.id):
        records = await canvas_service.list_documents(db, query)
    return ResultDTO.ok(data=records, message="画布列表已获取")


@router.post("", response_model=ResultDTO[CanvasRecordDTO])
async def create_canvas(
    req: CanvasCreateDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CanvasRecordDTO]:
    """创建画布产物（revision=1）。

    Args:
        req: 创建入参（标题 / 类型 / 内容）。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        创建后的记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            标题或内容纯空白（``VALIDATION_ERROR``——请求模型层拒绝）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await canvas_service.create_document(db, req)
    return ResultDTO.ok(data=record, message="画布已创建")


@router.get("/{canvas_id}", response_model=ResultDTO[CanvasRecordDTO])
async def get_canvas(
    canvas_id: int = Path(ge=1, description="画布主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CanvasRecordDTO]:
    """读取单条画布（含自包含内容）。

    Args:
        canvas_id: 画布主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        画布记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格 / 画布不存在
            或不属于当前人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await canvas_service.get_document(db, canvas_id)
    return ResultDTO.ok(data=record, message="画布已获取")


@router.patch("/{canvas_id}", response_model=ResultDTO[CanvasRecordDTO])
async def update_canvas(
    req: CanvasUpdateDTO,
    canvas_id: int = Path(ge=1, description="画布主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CanvasRecordDTO]:
    """修订画布（标题 / 内容按需修改；revision 递增）。

    Args:
        req: 更新入参（至少一项）。
        canvas_id: 画布主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        修订后的记录。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格 / 画布不存在
            （``NOT_FOUND``）；两项皆缺或修改值纯空白
            （``VALIDATION_ERROR``——请求模型层拒绝）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await canvas_service.update_document(db, canvas_id, req)
    return ResultDTO.ok(data=record, message="画布已修订")


@router.delete("/{canvas_id}", response_model=ResultDTO[CanvasDeleteResultModel])
async def delete_canvas(
    canvas_id: int = Path(ge=1, description="画布主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CanvasDeleteResultModel]:
    """删除画布（缺失主键幂等——deleted_ids 为空报告）。

    Args:
        canvas_id: 画布主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        删除结果（实际删除的主键列表）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await canvas_service.delete_document(db, canvas_id)
    return ResultDTO.ok(data=result, message="画布删除结果")


@router.get("/{canvas_id}/export")
async def export_canvas(
    canvas_id: int = Path(ge=1, description="画布主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """导出画布（单文件下载——Content-Type 按产物类型固定）。

    Args:
        canvas_id: 画布主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        原始产物字节响应（attachment + 文件名带主键与修订号）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格 / 画布不存在
            （``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        export = await canvas_service.export_document(db, canvas_id)
    return Response(
        content=export.content.encode("utf-8"),
        media_type=export.media_type,
        headers={
            "Content-Disposition": _EXPORT_HEADERS_TEMPLATE.format(filename=export.filename)
        },
    )
