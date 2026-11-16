"""look_at 管理 API——图像理解端点。

    - POST /api/v1/look_at —— 图片 multipart 上传 + 可选 question 字段 →
      ``ResultDTO`` 包裹的结构化中文描述（description / model_entry /
      latency_ms）。

契约说明：

    - **inbound multipart 零新增依赖**（与 voice ASR 同锁定决策）：
      FastAPI ``File`` / ``Form`` 参数面需要 ``python-multipart``
      运行时——本工程不引入该依赖；端点经原始 :class:`~fastapi.Request`
      读取请求体，用 **stdlib ``email`` 解析器**解析 multipart/form-data
      （RFC 2046 边界 / 引号与 RFC 2231 文件名由 stdlib 承担；字节载荷
      byte 级原样透传——经 httpx 原生编码体实证）。形态不符 / 缺
      ``image`` 字段 → ``BAD_REQUEST``（400）；
    - **MIME 解析序**（声明优先、扩展名兜底）：``image`` 部件的
      Content-Type 在允许清单内 → 直接采用；否则按文件扩展名映射
      （.png / .jpg / .jpeg / .webp / .gif）；仍未命中 → 透传声明值，
      由请求校验面拒绝（``VALIDATION_ERROR`` → 422）——兼容 curl
      无 Content-Type 上传与浏览器显式声明两种形态；
    - **请求校验**：``LookAtRequestModel``（MIME 允许清单 / 10MB 上限 /
      聚焦问题 ≤200 字符）在本层先行校验（系统边界校验——``pydantic
      ValidationError`` 包裹为 ``VALIDATION_ERROR``）；服务层同模型
      复校（直调 / 工具面单一真源）；
    - **服务装配**：``LookAtService`` 按请求构造，网关经
      ``_resolve_request_gateway`` 解析（``app.state.gateway`` 优先
      ——lifespan 装配形态，用量随装配网关发射；未装配回落裸构造
      ——与 memories 路由同一解析口径）；测试经 monkeypatch 替换
      模块级 ``LookAtService`` 构造面注入 fake；
    - **人格绑定**：缺省取当前激活人格（用量 persona_id 归属）；无
      激活人格 → 未绑定形态（persona_id 为空——look_at 是管理面工具，
      不因无人格而拒绝）。
"""

from __future__ import annotations

from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import get_async_db
from packages.harness.llm.gateway import LlmGateway
from packages.harness.multimodal.look_at import LookAtService
from packages.harness.multimodal.mm_models import (
    LOOK_AT_IMAGE_MIME_ALLOWLIST,
    LookAtRequestModel,
    LookAtResultModel,
)
from packages.harness.persona.service import PersonaService

router = APIRouter(prefix="/api/v1/look_at", tags=["look_at"])

#: 人格服务实例（无状态——Session 逐请求注入，服务可安全共享）。
persona_service = PersonaService()

#: 文件扩展名 → MIME 映射（声明 Content-Type 不在允许清单时的兜底）。
_EXTENSION_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _resolve_mime_type(declared: str | None, filename: str) -> str:
    """解析图片 MIME（声明优先、扩展名兜底、透传兜底）。

    Args:
        declared: multipart 部件声明的 Content-Type（None = 部件未声明）。
        filename: 原始文件名（multipart file 字段携带——扩展名兜底判据）。

    Returns:
        解析出的 MIME 类型（允许清单内的声明值 / 扩展名映射值；
        均未命中时透传归一化声明值——由请求校验面拒绝）。
    """
    if declared is not None:
        normalized = declared.strip().lower()
        if normalized in LOOK_AT_IMAGE_MIME_ALLOWLIST:
            return normalized
    mapped = _EXTENSION_MIME_MAP.get(Path(filename).suffix.lower())
    if mapped is not None:
        return mapped
    return (declared or "").strip().lower()


def _resolve_request_gateway(request: Request) -> LlmGateway:
    """解析请求可用的 LLM 网关（app.state.gateway 优先；缺省等价兜底）。

    Args:
        request: 当前请求（app.state.gateway 由 lifespan 装配）。

    Returns:
        用于 look_at 调用的 :class:`LlmGateway`（未装配 lifespan 的本地
        测试面回落裸构造——与装配形态同语义：全局单例延迟解析；用量
        接线归 lifespan 装配网关，与 memories 路由同一解析口径）。
    """
    gateway = cast("LlmGateway | None", getattr(request.app.state, "gateway", None))
    if gateway is None:
        return LlmGateway()
    return gateway


async def _parse_look_at_upload(request: Request) -> tuple[bytes, str, str | None]:
    """解析 look_at 上传的 multipart/form-data 请求体（stdlib email 解析器）。

    识别 ``image``（图片文件——字节载荷 + 文件名 + 声明 MIME）与可选
    ``question``（聚焦问题文本）两个字段；同名多字段取首个（其余忽略
    ——管理面上传形态由调用方保证）。文件名字段经 stdlib 的 RFC 2231 /
    引号解析归一。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 multipart/form-data）。

    Returns:
        ``(image 字节, mime_type, question | None)`` 三元组。

    Raises:
        BusinessException: 请求形态不符（非 multipart / 空请求体 / 边界
            非法 / 缺 ``image`` 字段 / ``image`` 字段无文件名——均
            ``BAD_REQUEST`` → 400）。
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="图像理解上传须为 multipart/form-data 形态（携带 image 字段）",
            details={"content_type": content_type or "(缺失)"},
        )
    raw = await request.body()
    if not raw:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="图像理解上传请求体为空",
        )
    header_block = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode()
    message = BytesParser().parsebytes(header_block + raw)
    if not message.is_multipart():
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="图像理解上传的 multipart 请求体非法（边界缺失 / 损坏）",
        )
    image: bytes | None = None
    filename: str | None = None
    declared_mime: str | None = None
    question: str | None = None
    parts = message.get_payload()
    if not isinstance(parts, list):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="图像理解上传的 multipart 请求体非法（无部件）",
        )
    # is_multipart + list 判定后部件恒为 Message（typeshed 的兼容宽类型
    # 经 cast 归一——运行期形态由上述守卫保证）。
    for part in cast("list[Message]", parts):
        name = part.get_param("name", header="content-disposition")
        if name == "image":
            if image is None:
                payload = part.get_payload(decode=True)
                part_filename = part.get_filename()
                if not isinstance(payload, bytes) or part_filename is None:
                    raise BusinessException(
                        code=ErrorCode.BAD_REQUEST,
                        message="图像理解上传的 image 字段缺少文件名或载荷非法",
                    )
                image = payload
                filename = part_filename
                declared_mime = part.get_content_type()
        elif name == "question" and question is None:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace").strip()
                question = text if text else None
    if image is None or filename is None:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="图像理解上传缺少 image 字段（图片文件）",
        )
    return image, _resolve_mime_type(declared_mime, filename), question


@router.post("", response_model=ResultDTO[LookAtResultModel])
async def look_at_image(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[LookAtResultModel]:
    """图片 → 结构化中文描述（multipart 上传；用量绑定当前激活人格）。

    Args:
        request: 原始 ASGI 请求（multipart/form-data：``image`` 图片字段 +
            可选 ``question`` 聚焦问题字段——解析见
            ``_parse_look_at_upload``；网关经 ``app.state.gateway``
            优先解析）。
        db: 注入的 Session。

    Returns:
        ``ResultDTO`` 包裹的图像理解结果（description / model_entry /
        latency_ms）。

    Raises:
        BusinessException: 上传形态不符（``BAD_REQUEST`` → 400）；图像 /
            MIME / 聚焦问题校验失败（``VALIDATION_ERROR`` → 422）；
            look_at 链路失败（``LLM_CALL_FAILED`` → 503 等）。
    """
    image, mime_type, question = await _parse_look_at_upload(request)
    try:
        payload = LookAtRequestModel(image=image, mime_type=mime_type, question=question)
    except ValidationError as exc:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="图像理解请求参数非法（图像 / MIME / 聚焦问题）",
            details={"reason": str(exc)[:300]},
        ) from exc
    persona = await persona_service.get_active_persona(db)
    service = LookAtService(gateway=_resolve_request_gateway(request))
    result = await service.describe(
        payload.image,
        payload.mime_type,
        payload.question,
        persona_id=str(persona.id) if persona is not None else None,
    )
    return ResultDTO.ok(data=result, message="图像理解完成")
