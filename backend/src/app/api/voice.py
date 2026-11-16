"""voice 管理 API——TTS 合成 / ASR 转写端点。

    - POST /api/v1/voice/tts —— 文本 → WAV 音频字节（``audio/wav`` 响应 +
      ``X-Latency-Ms`` 合成时延头；人格绑定优先级：请求参数 >
      persona.voice_profile > settings 缺省）；
    - POST /api/v1/voice/asr —— 音频文件 multipart 上传 → ``ResultDTO``
      包裹的转写结果（text / duration_ms / language）。

契约说明：

    - **TTS 响应是原始字节**（非 ``ResultDTO`` JSON——音频面无法走统一
      包裹；成功即 ``200 audio/wav``）；**错误仍走统一异常处理器** →
      ``ResultDTO`` error 形态（HTTP 状态按 ErrorCode 映射——TTS 未启用
      409 / 人格缺失 404 / 请求校验 400）；
    - **人格解析**：``persona_id`` 显式给定（缺失 → 404）；缺省取当前
      激活人格；两者皆无 → 未绑定形态（settings 缺省参数 + 用量
      persona_id 为空——语音是可选叠加面，不因无人格而拒绝）；
    - **inbound multipart 零新增依赖**（锁定决策）：FastAPI ``File`` /
      ``Form`` 参数面需要 ``python-multipart`` 运行时——本工程不引入该
      依赖；ASR 端点经原始 :class:`~fastapi.Request` 读取请求体，用
      **stdlib ``email`` 解析器**解析 multipart/form-data（RFC 2046 边界 /
      引号与 RFC 2231 文件名由 stdlib 承担；字节载荷 byte 级原样透传
      ——经 httpx 原生编码体实证）。形态不符 / 缺 ``file`` 字段 →
      ``BAD_REQUEST``（400）；
    - **服务装配**：模块级 :class:`VoiceService`（用量默认接线）；客户端
      在调用时经公共池共享 httpx client 瞬时构建（lifespan 已启动池——
      请求期可解析；测试经 monkeypatch 替换模块级实例注入 fake）。
"""

from __future__ import annotations

from email.message import Message
from email.parser import BytesParser
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import get_async_db
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService
from packages.harness.voice.voice_models import AsrResultModel, VoiceTtsRequestDTO
from packages.harness.voice.voice_service import (
    VoiceService,
    wire_default_voice_usage_recorder,
)

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

#: 人格服务实例（无状态——Session 逐请求注入，服务可安全共享）。
persona_service = PersonaService()
#: 语音服务实例（客户端调用时经公共池瞬时构建；用量默认接线）。
voice_service = VoiceService(usage_recorder=wire_default_voice_usage_recorder())


async def _resolve_persona(
    db: AsyncSession, persona_id: int | None
) -> PersonaRecordDTO | None:
    """解析合成绑定人格（显式 ID 优先；缺省当前激活人格；皆无 → None）。

    Args:
        db: 注入的异步 Session。
        persona_id: 显式人格主键（None = 当前激活人格）。

    Returns:
        绑定人格记录；显式 ID 缺失时抛 ``NOT_FOUND``，无激活人格时
        返回 None（未绑定形态——不拒绝合成）。

    Raises:
        BusinessException: 显式 persona_id 不存在（``NOT_FOUND``）。
    """
    if persona_id is not None:
        return await persona_service.get(db, persona_id)
    return await persona_service.get_active_persona(db)


async def _parse_multipart_upload(request: Request) -> tuple[bytes, str, str | None]:
    """解析 ASR 上传的 multipart/form-data 请求体（stdlib email 解析器）。

    识别 ``file``（音频文件——字节载荷 + 文件名）与可选 ``language``
    （语言提示文本）两个字段；同名多字段取首个（其余忽略——管理面
    上传形态由调用方保证）。文件名字段经 stdlib 的 RFC 2231 / 引号
    解析归一。

    Args:
        request: 原始 ASGI 请求（Content-Type 须为 multipart/form-data）。

    Returns:
        ``(audio 字节, 文件名, language | None)`` 三元组。

    Raises:
        BusinessException: 请求形态不符（非 multipart / 空请求体 / 边界
            非法 / 缺 ``file`` 字段 / ``file`` 字段无文件名——均
            ``BAD_REQUEST`` → 400）。
    """
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="语音识别上传须为 multipart/form-data 形态（携带 file 字段）",
            details={"content_type": content_type or "(缺失)"},
        )
    raw = await request.body()
    if not raw:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="语音识别上传请求体为空",
        )
    header_block = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode()
    message = BytesParser().parsebytes(header_block + raw)
    if not message.is_multipart():
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="语音识别上传的 multipart 请求体非法（边界缺失 / 损坏）",
        )
    audio: bytes | None = None
    filename: str | None = None
    language: str | None = None
    parts = message.get_payload()
    if not isinstance(parts, list):
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="语音识别上传的 multipart 请求体非法（无部件）",
        )
    # is_multipart + list 判定后部件恒为 Message（typeshed 的兼容宽类型
    # 经 cast 归一——运行期形态由上述守卫保证）。
    for part in cast("list[Message]", parts):
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            if audio is None:
                payload = part.get_payload(decode=True)
                part_filename = part.get_filename()
                if not isinstance(payload, bytes) or part_filename is None:
                    raise BusinessException(
                        code=ErrorCode.BAD_REQUEST,
                        message="语音识别上传的 file 字段缺少文件名或载荷非法",
                    )
                audio = payload
                filename = part_filename
        elif name == "language" and language is None:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace").strip()
                language = text if text else None
    if audio is None or filename is None:
        raise BusinessException(
            code=ErrorCode.BAD_REQUEST,
            message="语音识别上传缺少 file 字段（音频文件）",
        )
    return audio, filename, language


@router.post("/tts")
async def synthesize_speech(
    req: VoiceTtsRequestDTO,
    db: AsyncSession = Depends(get_async_db),
) -> Response:
    """文本 → WAV 音频（人格绑定参数解析见模块 docstring）。

    Args:
        req: 合成请求（text 必填；persona_id / speaker / speed 可选）。
        db: 注入的异步 Session。

    Returns:
        ``audio/wav`` 字节响应 + ``X-Latency-Ms``（合成往返时延毫秒）。

    Raises:
        BusinessException: TTS 未启用（``CONFLICT`` → 409）；人格不存在
            （``NOT_FOUND`` → 404）；请求校验失败（FastAPI 400）。
    """
    persona = await _resolve_persona(db, req.persona_id)
    result = await voice_service.synthesize(
        req.text, persona=persona, speaker=req.speaker, speed=req.speed
    )
    return Response(
        content=result.audio,
        media_type=result.content_type,
        headers={"X-Latency-Ms": str(result.latency_ms)},
    )


@router.post("/asr", response_model=ResultDTO[AsrResultModel])
async def transcribe_audio(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[AsrResultModel]:
    """音频文件 → 转写文本（multipart file 上传；用量绑定当前激活人格）。

    Args:
        request: 原始 ASGI 请求（multipart/form-data：``file`` 音频字段 +
            可选 ``language`` 提示字段——解析见 ``_parse_multipart_upload``）。
        db: 注入的异步 Session。

    Returns:
        ``ResultDTO`` 包裹的转写结果（text / duration_ms / language）。

    Raises:
        BusinessException: ASR 未启用（``CONFLICT`` → 409）；上传形态不符
            （``BAD_REQUEST`` → 400）；音频 / 文件名语义非法
            （``VALIDATION_ERROR`` → 422）。
    """
    audio, filename, language = await _parse_multipart_upload(request)
    persona = await persona_service.get_active_persona(db)
    result = await voice_service.transcribe(
        audio, filename, persona=persona, language=language
    )
    return ResultDTO.ok(data=result, message="语音识别完成")
