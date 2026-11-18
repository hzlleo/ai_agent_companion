"""人格管理 admin 路由——多人格注册表全操作面（多激活可寻址模型）。

    - GET    /api/v1/personas                        —— 游标分页列表（status 过滤）
    - POST   /api/v1/personas                        —— 创建
    - POST   /api/v1/personas/batch-delete           —— 批量删除（部分成功形态）
    - GET    /api/v1/personas/primary                —— 读取当前主人格（无主人格 404）
    - GET    /api/v1/personas/presets                —— 官方特质预设库（画廊载荷）
    - POST   /api/v1/personas/import                 —— 导入（官方预设 JSON /
      用户卡片 multipart；内容哈希幂等 + 冲突策略）
    - POST   /api/v1/personas/import/batch           —— 批量导入（multipart
      多文件；部分成功形态）
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
harness.persona.orm）。``/primary`` / ``/presets`` / ``/import`` /
``/import/batch`` 字面路由先于 ``/{persona_id}`` 注册（FastAPI 顺序
匹配——字面路由须前置防路径参数吞并）。

导入端点形态（锁定决策——零新增依赖）：JSON 面（``application/json``
→ :class:`PersonaImportRequestDTO`）与 multipart 面（卡片文件 + 策略 /
期望名表单字段）并存；multipart 经 stdlib ``email`` 解析器解析（与
voice.asr 同模式——FastAPI ``File``/``Form`` 参数面需要
``python-multipart`` 运行时，本工程不引入）；请求体大小守卫为模块级
常量（管理面卡片文档小体量——不引入新 settings 键）。
"""

from __future__ import annotations

import json
from email.message import Message
from email.parser import BytesParser
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import get_async_db
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    PersonaBatchDeleteRequest,
    PersonaBatchDeleteResponseDTO,
    PersonaCardDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaImportBatchFileDTO,
    PersonaImportBatchResultDTO,
    PersonaImportRequestDTO,
    PersonaImportResultModel,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
    TraitLibraryDTO,
)
from packages.harness.persona.models import (
    PersonaImportConflictStrategy,
    PersonaImportReason,
    PersonaStateLayer,
    PersonaStatus,
)
from packages.harness.persona.service import PersonaService
from packages.harness.persona.trait_library import (
    generate_card_from_preset,
    load_official_components,
    load_official_presets,
)

router = APIRouter(prefix="/api/v1/personas", tags=["personas"])

#: 人格服务实例（无状态——Session 逐请求注入，服务可安全共享）。
persona_service = PersonaService()

#: 导入请求体大小上限（multipart 面；JSON 面共用——卡片文档小体量，
#: 2MB 已远超任何合法单卡 / 批量卡片集；不引入新 settings 键）。
PERSONA_IMPORT_MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024


async def _parse_import_json_request(request: Request) -> PersonaImportRequestDTO:
    """解析 JSON 形态的导入请求（→ PersonaImportRequestDTO 归一）。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 application/json）。

    Returns:
        归一后的导入请求 DTO（载荷恰一约束在此校验）。

    Raises:
        BusinessException: 请求体非法 JSON（``BAD_REQUEST`` → 400）；
            请求体超出大小上限（``VALIDATION_ERROR`` → 422）；DTO 校验
            失败（``VALIDATION_ERROR`` → 422——details 携带原因）。
    """
    raw = await request.body()
    if len(raw) > PERSONA_IMPORT_MAX_UPLOAD_BYTES:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="导入请求体超过大小上限",
            details={
                "max_upload_bytes": PERSONA_IMPORT_MAX_UPLOAD_BYTES,
                "size_bytes": len(raw),
            },
        )
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="导入请求体非法 JSON",
            details={"reason": str(exc)},
        ) from exc
    try:
        return PersonaImportRequestDTO.model_validate(payload)
    except ValidationError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="导入请求校验失败",
            details={"reason": str(exc)},
        ) from exc


def _decode_card_file(filename: str, payload: bytes) -> PersonaImportBatchFileDTO:
    """解析单个卡片文件载荷（multipart 部件 → 装配面 DTO）。

    Args:
        filename: 文件名（结果定位用）。
        payload: 文件字节载荷（须为 chara card v2 JSON 文档）。

    Returns:
        装配面 DTO（解析成功携带卡片；失败携带 error_message——不抛错，
        批量部分成功语义由调用方决定；单发面由调用方检查并转 422）。
    """
    try:
        document: object = json.loads(payload)
        card = CharaCardModel.model_validate(document)
    except (json.JSONDecodeError, ValidationError) as exc:
        return PersonaImportBatchFileDTO(
            filename=filename,
            card=None,
            error_message=f"卡片文件解析失败（须为 chara card v2 JSON）: {exc}",
        )
    return PersonaImportBatchFileDTO(filename=filename, card=card)


async def _parse_import_multipart_request(request: Request) -> PersonaImportRequestDTO:
    """解析 multipart 形态的单发导入请求（→ PersonaImportRequestDTO 归一）。

    识别 ``file``（卡片 JSON 文件——必填）与可选 ``conflict_strategy`` /
    ``desired_name`` 表单字段；经 stdlib ``email`` 解析器解析（与
    voice.asr 同模式——RFC 2046 边界 / RFC 2231 文件名归一由 stdlib 承担）。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 multipart/form-data）。

    Returns:
        归一后的导入请求 DTO（``preset_id`` 恒 None——文件导入即卡片载荷）。

    Raises:
        BusinessException: 请求形态不符（非 multipart → ``BAD_REQUEST``）；
            请求体为空 / 边界损坏（``BAD_REQUEST``）；请求体超上限
            （``VALIDATION_ERROR``）；缺 ``file`` 字段或文件无文件名
            （``BAD_REQUEST``）；策略 / 期望名表单值非法
            （``VALIDATION_ERROR``）；卡片文件解析失败
            （``VALIDATION_ERROR``）。
    """
    files, form_fields = await _parse_multipart_parts(request)
    if not files:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="人格导入上传缺少 file 字段（卡片 JSON 文件）",
        )
    strategy_text = form_fields.get("conflict_strategy")
    desired_name_text = form_fields.get("desired_name")
    strategy: PersonaImportConflictStrategy
    if strategy_text is None:
        strategy = PersonaImportConflictStrategy.SKIP
    else:
        try:
            strategy = PersonaImportConflictStrategy(strategy_text)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="导入冲突策略非法（须为 skip / rename / overwrite）",
                details={"conflict_strategy": strategy_text},
            ) from exc
    desired_name = desired_name_text.strip() if desired_name_text is not None else None
    if desired_name == "":
        desired_name = None
    file_dto = _decode_card_file(files[0][0], files[0][1])
    if file_dto.card is None:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="导入卡片文件解析失败（须为 chara card v2 JSON 文档）",
            details={
                "filename": file_dto.filename,
                "reason": file_dto.error_message or "",
            },
        )
    return PersonaImportRequestDTO(
        preset_id=None,
        card=file_dto.card,
        conflict_strategy=strategy,
        desired_name=desired_name,
    )


async def _parse_import_batch_multipart_request(
    request: Request,
) -> tuple[list[PersonaImportBatchFileDTO], PersonaImportConflictStrategy]:
    """解析 multipart 形态的批量导入请求（多文件 + 统一策略）。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 multipart/form-data；
            ``files`` 文件字段可重复 + 可选 ``conflict_strategy`` 表单字段）。

    Returns:
        ``(逐文件装配 DTO 列表, 冲突策略)`` 二元组（解析失败项以
        ``error_message`` 携带——批量部分成功语义由服务层落形）。

    Raises:
        BusinessException: 请求形态不符（非 multipart → ``BAD_REQUEST``）；
            请求体为空 / 边界损坏（``BAD_REQUEST``）；请求体超上限
            （``VALIDATION_ERROR``）；缺 ``files`` 字段（``BAD_REQUEST``）；
            策略表单值非法（``VALIDATION_ERROR``）。
    """
    files, form_fields = await _parse_multipart_parts(request)
    if not files:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="批量导入上传缺少 files 字段（卡片 JSON 文件，可多个）",
        )
    strategy_text = form_fields.get("conflict_strategy")
    strategy: PersonaImportConflictStrategy
    if strategy_text is None:
        strategy = PersonaImportConflictStrategy.SKIP
    else:
        try:
            strategy = PersonaImportConflictStrategy(strategy_text)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="导入冲突策略非法（须为 skip / rename / overwrite）",
                details={"conflict_strategy": strategy_text},
            ) from exc
    entries = [_decode_card_file(filename, payload) for filename, payload in files]
    return entries, strategy


async def _parse_multipart_parts(
    request: Request,
) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    """解析 multipart 请求体（stdlib email 解析器——零新增依赖）。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 multipart/form-data）。

    Returns:
        ``(文件部件列表, 文本表单字段映射)`` 二元组——文件部件为
        ``(文件名, 字节载荷)``（字段声明序）；文本字段同名取首值。

    Raises:
        BusinessException: 请求形态不符（非 multipart → ``BAD_REQUEST``）；
            请求体为空 / 边界损坏 / 无部件（``BAD_REQUEST``）；文件部件
            缺文件名（``BAD_REQUEST``）；请求体超上限
            （``VALIDATION_ERROR``——守卫先行，不进入解析）。
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="人格导入上传须为 multipart/form-data 形态",
            details={"content_type": content_type or "(缺失)"},
        )
    raw = await request.body()
    if not raw:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="人格导入上传请求体为空",
        )
    if len(raw) > PERSONA_IMPORT_MAX_UPLOAD_BYTES:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="导入请求体超过大小上限",
            details={
                "max_upload_bytes": PERSONA_IMPORT_MAX_UPLOAD_BYTES,
                "size_bytes": len(raw),
            },
        )
    header_block = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser().parsebytes(header_block + raw)
    if not message.is_multipart():
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="人格导入上传的 multipart 请求体非法（边界缺失 / 损坏）",
        )
    parts = message.get_payload()
    if not isinstance(parts, list):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="人格导入上传的 multipart 请求体非法（无部件）",
        )
    files: list[tuple[str, bytes]] = []
    fields: dict[str, str] = {}
    # is_multipart + list 判定后部件恒为 Message（typeshed 的兼容宽类型
    # 经 cast 归一——运行期形态由上述守卫保证；与 voice.asr 同模式）。
    for part in cast("list[Message]", parts):
        param = part.get_param("name", header="content-disposition")
        if not isinstance(param, str):
            # RFC 2231 编码的字段名（实践不出现——file/files 等字段名恒
            # ASCII 直传；与 voice.asr 的字符串等值比较同语义）跳过。
            continue
        name = param
        if name in ("file", "files"):
            payload = part.get_payload(decode=True)
            filename = part.get_filename()
            if not isinstance(payload, bytes) or filename is None:
                raise BusinessException(
                    code=ErrorCode.BAD_REQUEST,
                    message="人格导入上传的文件字段缺少文件名或载荷非法",
                )
            files.append((filename, payload))
        elif name not in fields:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace").strip()
                if text:
                    fields[name] = text
    return files, fields


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


@router.get("/presets", response_model=ResultDTO[TraitLibraryDTO])
async def list_trait_presets() -> ResultDTO[TraitLibraryDTO]:
    """读取官方特质预设库（画廊载荷——预设 + 组件注册表）。

    数据经 ``load_official_presets`` / ``load_official_components``
    路径相对装载（首次调用校验 + 缓存；非法数据文件 fail loud——
    模块导入期零副作用）。

    Returns:
        特质库载荷（presets + components——前端按 id 映射解析芯片）。

    Raises:
        BusinessException: 官方预设库数据非法（``INTERNAL_ERROR`` →
            500——details 携带装载失败原因）。
    """
    try:
        presets = load_official_presets()
        components = load_official_components()
    except ValueError as exc:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="官方预设库数据非法（装载失败）",
            details={"reason": str(exc)},
        ) from exc
    return ResultDTO.ok(
        data=TraitLibraryDTO(presets=presets, components=list(components.values()))
    )


@router.post("/import", response_model=ResultDTO[PersonaImportResultModel])
async def import_persona(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaImportResultModel]:
    """导入人格（官方预设 JSON 或用户卡片 multipart / JSON）。

    载荷分支（锁定语义）：

        - ``preset_id``（JSON）——服务端按官方预设 + 组件库确定性生成
            卡片（基础名 = 期望名或预设名 + 预设后缀；可按
            ``intimacy_level_override`` 覆盖预设推荐的亲密等级；生成冲突
            软警告并入结果 ``warnings``；预设建议语音随创建落库）；
        - ``card``（JSON）或 ``file``（multipart 卡片 JSON 文件）——
            用户自有卡片文档直载。

    冲突判定序（服务层锁定语义）：内容同卡恒幂等跳过（与策略无关）；
    名称冲突按 ``conflict_strategy``（skip 缺省 / rename / overwrite）。

    Args:
        request: 原始 ASGI 请求（application/json 或 multipart/form-data）。
        db: 注入的异步 Session。

    Returns:
        导入结果（imported / persona_id / reason / warnings）。

    Raises:
        BusinessException: 请求形态 / 载荷校验失败（``BAD_REQUEST`` /
            ``VALIDATION_ERROR``）；官方预设不存在（``NOT_FOUND`` →
            404）；官方预设库数据非法（``INTERNAL_ERROR``）。
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if media_type == "multipart/form-data":
        dto = await _parse_import_multipart_request(request)
    else:
        dto = await _parse_import_json_request(request)
    warnings: list[str] = []
    voice_profile = None
    card: CharaCardModel
    desired_name: str | None
    if dto.preset_id is not None:
        try:
            presets = load_official_presets()
            registry = load_official_components()
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="官方预设库数据非法（装载失败）",
                details={"reason": str(exc)},
            ) from exc
        preset = next((p for p in presets if p.id == dto.preset_id), None)
        if preset is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="官方预设不存在",
                details={"preset_id": dto.preset_id},
            )
        base_name = dto.desired_name or preset.name_zh
        generation = generate_card_from_preset(
            preset,
            registry,
            base_name=base_name,
            intimacy_level_override=dto.intimacy_level_override,
        )
        card = generation.card
        warnings = [warning.message for warning in generation.warnings]
        voice_profile = preset.suggested_voice
        desired_name = None  # 期望名已拼入卡片名（防双拼）
    else:
        # 载荷恰一约束（PersonaImportRequestDTO 模型校验）保证 card 非 None。
        card = cast("CharaCardModel", dto.card)
        desired_name = dto.desired_name
    result = await persona_service.import_persona(
        db,
        card,
        strategy=dto.conflict_strategy,
        desired_name=desired_name,
        warnings=warnings,
        voice_profile=voice_profile,
    )
    if result.imported:
        message = "人格已导入"
    elif result.reason == PersonaImportReason.IDENTICAL_CONTENT:
        message = "检测到同内容人格已存在（幂等跳过）"
    else:
        message = "注册名已存在，按跳过策略未导入"
    return ResultDTO.ok(data=result, message=message)


@router.post("/import/batch", response_model=ResultDTO[PersonaImportBatchResultDTO])
async def import_personas_batch(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PersonaImportBatchResultDTO]:
    """批量导入人格（multipart 多文件——部分成功形态）。

    逐文件独立事务：解析失败 / 内部错误记失败项，不阻塞其余文件；
    幂等跳过与名称冲突（skip 策略）为合法结果（success=True）。

    Args:
        request: 原始 ASGI 请求（multipart/form-data：``files`` 文件
            字段可重复 + 可选 ``conflict_strategy`` 表单字段）。
        db: 注入的异步 Session。

    Returns:
        批量导入结果（total / succeeded / failed / imported_count /
        逐文件结果）。

    Raises:
        BusinessException: 请求形态 / 载荷校验失败（``BAD_REQUEST`` /
            ``VALIDATION_ERROR``）。
    """
    entries, strategy = await _parse_import_batch_multipart_request(request)
    result = await persona_service.import_personas_batch(db, entries, strategy=strategy)
    return ResultDTO.ok(
        data=result,
        message=(
            f"批量导入完成（成功 {result.succeeded} / 失败 {result.failed} /"
            f" 新导入 {result.imported_count}）"
        ),
    )


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
