"""multimodal 表情包管理 API——上传 / 列表 / 标签修改 / 删除 / VLM 标注。

    - GET    /api/v1/stickers                      —— 列表（标签 / 标注状态过滤；
      可选内联文件 base64）
    - POST   /api/v1/stickers                      —— 批量上传（JSON base64；
      来源备注必填）
    - POST   /api/v1/stickers/batch-delete         —— 批量删除
    - GET    /api/v1/stickers/{sticker_id}         —— 单条读取
    - PATCH  /api/v1/stickers/{sticker_id}         —— 标签 / 描述 / 来源备注修改
    - DELETE /api/v1/stickers/{sticker_id}         —— 单条删除
    - POST   /api/v1/stickers/{sticker_id}/annotate —— VLM 标注（失败落状态，
      可重试）
    - GET    /api/v1/stickers/{sticker_id}/file    —— 原始文件字节
      （Content-Type 为记录 MIME）

存储面：multimodal 域 ``ff_stickers``（见
``harness/multimodal/sticker_service``）——本路由只做人格作用域解析与
类型化出入口；哈希去重 / 类型与大小校验 / 来源备注强制 / 标注状态机由
服务层承担。

人格作用域约定（与声纹 / 记忆面同轴）：端点先解析目标人格
（``persona_id`` 查询参数显式指定；缺省当前主人格，无主人格 ``NOT_FOUND``
fail loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。表情包
库按人格隔离。

全部响应经 ``ResultDTO`` 封装（文件端点除外——原始字节无法套 JSON 包裹）；
DB Session 经 ``database.session.get_async_db`` 依赖注入。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reminders import resolve_target_persona
from packages.harness.core.dtos import ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.multimodal.sticker_models import (
    StickerAnnotationResultModel,
    StickerAnnotationStatus,
    StickerBatchDeleteRequest,
    StickerBatchDeleteResultModel,
    StickerBatchUploadDTO,
    StickerBatchUploadResultModel,
    StickerQueryDTO,
    StickerRecordDTO,
    StickerUpdateModel,
)
from packages.harness.multimodal.sticker_service import StickerService
from packages.harness.persona.service import bind_persona_context

router = APIRouter(prefix="/api/v1/stickers", tags=["stickers"])

#: 表情包服务实例（无状态——Session 逐请求注入，服务可安全共享）。
sticker_service = StickerService()


@router.get("", response_model=ResultDTO[list[StickerRecordDTO]])
async def list_stickers(
    tag: str | None = Query(None, max_length=24, description="标签过滤（精确匹配）"),
    annotation_status: StickerAnnotationStatus | None = Query(
        None, description="标注状态过滤"
    ),
    limit: int = Query(100, ge=1, le=200, description="返回条数上限"),
    include_data: bool = Query(False, description="是否内联文件 base64（缩略图用）"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[StickerRecordDTO]]:
    """读取表情包列表（新入库在前；标签 / 标注状态过滤）。

    Args:
        tag: 标签过滤（精确匹配）。
        annotation_status: 标注状态过滤。
        limit: 返回条数上限（1-200）。
        include_data: 是否内联文件 base64。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        表情包记录列表。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）；
            功能未启用（``CONFLICT``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    query = StickerQueryDTO(
        tag=tag,
        annotation_status=annotation_status,
        limit=limit,
        include_data=include_data,
    )
    async with bind_persona_context(persona.id):
        records = await sticker_service.list_stickers(db, query)
    return ResultDTO.ok(data=records, message="表情包列表已获取")


@router.post("", response_model=ResultDTO[StickerBatchUploadResultModel])
async def upload_stickers(
    req: StickerBatchUploadDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerBatchUploadResultModel]:
    """批量上传表情包（来源备注必填；同人格同文件按哈希去重）。

    Args:
        req: 批量上传入参。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        上传结果（成功记录 + 被拒条目）。

    Raises:
        BusinessException: 功能未启用（``CONFLICT``）；批量条数超过配置
            上限（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await sticker_service.upload_batch(db, req)
    return ResultDTO.ok(data=result, message="表情包上传完成")


@router.post(
    "/batch-delete", response_model=ResultDTO[StickerBatchDeleteResultModel]
)
async def batch_delete_stickers(
    req: StickerBatchDeleteRequest,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerBatchDeleteResultModel]:
    """批量删除表情包（不存在的主键进入 missing 列表）。

    Args:
        req: 批量删除入参。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        删除结果（部分成功语义）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await sticker_service.delete_batch(db, req)
    return ResultDTO.ok(data=result, message="表情包删除完成")


@router.get("/{sticker_id}", response_model=ResultDTO[StickerRecordDTO])
async def get_sticker(
    sticker_id: int = Path(..., ge=1, description="表情包主键"),
    include_data: bool = Query(False, description="是否内联文件 base64"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerRecordDTO]:
    """读取单条表情包。

    Args:
        sticker_id: 表情包主键。
        include_data: 是否内联文件 base64。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        表情包记录。

    Raises:
        BusinessException: 记录不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await sticker_service.get_sticker(
            db, sticker_id, include_data=include_data
        )
    return ResultDTO.ok(data=record, message="表情包已获取")


@router.patch("/{sticker_id}", response_model=ResultDTO[StickerRecordDTO])
async def update_sticker(
    req: StickerUpdateModel,
    sticker_id: int = Path(..., ge=1, description="表情包主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerRecordDTO]:
    """修改表情包标签 / 描述 / 来源备注（至少一项）。

    Args:
        req: 修改入参。
        sticker_id: 表情包主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        修改后的记录。

    Raises:
        BusinessException: 记录不存在（``NOT_FOUND``）；标签 / 描述 / 来源
            备注非法（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        record = await sticker_service.update_sticker(db, sticker_id, req)
    return ResultDTO.ok(data=record, message="表情包已更新")


@router.delete("/{sticker_id}", response_model=ResultDTO[StickerBatchDeleteResultModel])
async def delete_sticker(
    sticker_id: int = Path(..., ge=1, description="表情包主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerBatchDeleteResultModel]:
    """删除单条表情包（行 + 文件；缺失主键幂等报告）。

    Args:
        sticker_id: 表情包主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        删除结果（缺失时 ``missing_ids`` 携带该主键）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await sticker_service.delete_sticker(db, sticker_id)
    return ResultDTO.ok(data=result, message="表情包已删除")


@router.post(
    "/{sticker_id}/annotate", response_model=ResultDTO[StickerAnnotationResultModel]
)
async def annotate_sticker(
    sticker_id: int = Path(..., ge=1, description="表情包主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[StickerAnnotationResultModel]:
    """VLM 标注表情包（描述 + 标签；失败落 failed 状态且可重试）。

    Args:
        sticker_id: 表情包主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        标注结果（成功 / 失败均为可读结构——失败不抛依赖异常）。

    Raises:
        BusinessException: 记录不存在（``NOT_FOUND``）；功能未启用
            （``CONFLICT``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await sticker_service.annotate(db, sticker_id)
    return ResultDTO.ok(data=result, message=result.message)


@router.get("/{sticker_id}/file")
async def get_sticker_file(
    sticker_id: int = Path(..., ge=1, description="表情包主键"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """读取表情包原始文件（Content-Type 为记录 MIME）。

    Args:
        sticker_id: 表情包主键。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        原始图片字节响应（``Content-Disposition: inline``）。

    Raises:
        BusinessException: 记录或文件不存在（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        binary = await sticker_service.get_binary(db, sticker_id)
    extension = binary.record.content_type.partition("/")[2] or "bin"
    return Response(
        content=binary.content,
        media_type=binary.record.content_type,
        headers={
            "Content-Disposition": f'inline; filename="sticker-{binary.record.id}.{extension}"'
        },
    )
