"""voice 域声纹管理 API——PIPL 告知 / 同意 / 注册 / 校验 / 档案删除。

    - GET  /api/v1/voiceprints/notice                    —— PIPL 告知（目的 /
      范围 / 保存期 / 撤回权）
    - GET  /api/v1/voiceprints                           —— 当前人格全部档案
    - GET  /api/v1/voiceprints/{sender_id}               —— 单发送者档案
    - POST /api/v1/voiceprints/{sender_id}/consent       —— 授予同意
    - POST /api/v1/voiceprints/{sender_id}/consent/withdraw —— 撤回并清除样本
    - DELETE /api/v1/voiceprints/{sender_id}             —— 删除档案（同意记录
      保留为合规凭证）
    - POST /api/v1/voiceprints/{sender_id}/enroll        —— 批量注册样本
    - POST /api/v1/voiceprints/{sender_id}/verify        —— 校验音频

存储面：voice 域 ``ff_voiceprint_samples`` / ``ff_voiceprint_consents``（见
``harness/voice/voiceprint_service``）——本路由只做人格作用域解析与类型化
出入口；同意闸 / 样本校验 / 向量提取 / 保存期清理由服务层承担。

人格作用域约定（与提醒 / 事实 / 核心记忆面同轴）：端点先解析目标人格
（``persona_id`` 查询参数显式指定；缺省当前主人格，无主人格 ``NOT_FOUND``
fail loud），再经 ``bind_persona_context`` 绑定上下文后调用服务。声纹按
人格 × 发送者隔离，他人格档案不可见。

全部响应经 ``ResultDTO`` 封装；DB Session 经 ``database.session.get_async_db``
依赖注入（事务边界归服务层）；请求体为类型化 Model Classes。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reminders import resolve_target_persona
from packages.harness.core.dtos import ResultDTO
from packages.harness.database.session import get_async_db
from packages.harness.persona.service import bind_persona_context
from packages.harness.voice.voice_budget import (
    wire_default_voice_usage_counter,
    wire_default_voice_usage_recorder,
)
from packages.harness.voice.voiceprint_models import (
    VoiceprintConsentGrantDTO,
    VoiceprintConsentRecordDTO,
    VoiceprintDeleteResultModel,
    VoiceprintEnrollRequestDTO,
    VoiceprintEnrollResultModel,
    VoiceprintPrivacyNoticeDTO,
    VoiceprintProfileDTO,
    VoiceprintVerifyRequestDTO,
    VoiceprintVerifyResultModel,
    VoiceprintWithdrawResultModel,
)
from packages.harness.voice.voiceprint_service import VoiceprintService

router = APIRouter(prefix="/api/v1/voiceprints", tags=["voiceprints"])

#: 声纹服务实例（无状态——Session 逐请求注入，服务可安全共享）。
voiceprint_service = VoiceprintService(
    usage_recorder=wire_default_voice_usage_recorder(),
    usage_counter=wire_default_voice_usage_counter(),
)

@router.get("/notice", response_model=ResultDTO[VoiceprintPrivacyNoticeDTO])
async def get_privacy_notice() -> ResultDTO[VoiceprintPrivacyNoticeDTO]:
    """读取 PIPL 告知（目的 / 范围 / 保存期 / 撤回权）。

    Returns:
        告知 DTO（版本与保存期随配置）。
    """
    return ResultDTO.ok(data=voiceprint_service.notice(), message="声纹隐私告知已获取")


@router.get("", response_model=ResultDTO[list[VoiceprintProfileDTO]])
async def list_profiles(
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[VoiceprintProfileDTO]]:
    """读取当前人格的全部声纹档案（样本主体 ∪ 同意主体）。

    Args:
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        档案列表（发送者 ID 升序）。

    Raises:
        BusinessException: 目标人格不存在 / 无主人格（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        profiles = await voiceprint_service.list_profiles(db)
    return ResultDTO.ok(data=profiles, message="声纹档案已获取")


@router.get("/{sender_id}", response_model=ResultDTO[VoiceprintProfileDTO])
async def get_profile(
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintProfileDTO]:
    """读取单发送者声纹档案（无样本时返回零条目空档案）。

    Args:
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        档案聚合。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        profile = await voiceprint_service.get_profile(db, sender_id)
    return ResultDTO.ok(data=profile, message="声纹档案已获取")


@router.post(
    "/{sender_id}/consent", response_model=ResultDTO[VoiceprintConsentRecordDTO]
)
async def grant_consent(
    req: VoiceprintConsentGrantDTO,
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintConsentRecordDTO]:
    """授予声纹处理同意（同版本有效同意存在时幂等返回）。

    Args:
        req: 授予入参（同意的告知文本版本）。
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        有效同意记录。

    Raises:
        BusinessException: 版本不一致（``VALIDATION_ERROR``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        consent = await voiceprint_service.grant_consent(db, sender_id, req)
    return ResultDTO.ok(data=consent, message="声纹同意已授予")


@router.post(
    "/{sender_id}/consent/withdraw", response_model=ResultDTO[VoiceprintWithdrawResultModel]
)
async def withdraw_consent(
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintWithdrawResultModel]:
    """撤回同意并清除该发送者的样本与向量。

    Args:
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        撤回结果（同意记录 + 清除样本条数）。

    Raises:
        BusinessException: 无有效同意（``NOT_FOUND``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await voiceprint_service.withdraw_consent(db, sender_id)
    return ResultDTO.ok(data=result, message="声纹同意已撤回，样本已清除")


@router.delete("/{sender_id}", response_model=ResultDTO[VoiceprintDeleteResultModel])
async def delete_profile(
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintDeleteResultModel]:
    """删除声纹档案（样本行 + 文件；同意记录保留为合规凭证）。

    Args:
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        删除结果（清除样本条数）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await voiceprint_service.delete_profile(db, sender_id)
    return ResultDTO.ok(data=result, message="声纹档案已删除")


@router.post(
    "/{sender_id}/enroll", response_model=ResultDTO[VoiceprintEnrollResultModel]
)
async def enroll_samples(
    req: VoiceprintEnrollRequestDTO,
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintEnrollResultModel]:
    """批量注册声纹样本（需先授予同意；逐样本部分成功）。

    Args:
        req: 注册入参（批量样本）。
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        注册结果（接收条数 + 拒收结局 + 注册后档案）。

    Raises:
        BusinessException: 未授予同意 / 功能未启用（``CONFLICT``）；样本
            条数 / 容量越界（``VALIDATION_ERROR``）；向量提供方失败
            （``LLM_CALL_FAILED``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await voiceprint_service.enroll(db, sender_id, req)
    return ResultDTO.ok(data=result, message="声纹注册完成")


@router.post(
    "/{sender_id}/verify", response_model=ResultDTO[VoiceprintVerifyResultModel]
)
async def verify_sample(
    req: VoiceprintVerifyRequestDTO,
    sender_id: str = Path(..., min_length=1, max_length=128, description="发送者 ID"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前主人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[VoiceprintVerifyResultModel]:
    """校验音频是否属于目标发送者（短音频 / 无样本 / 低于阈值均拒识）。

    Args:
        req: 校验入参（单条音频）。
        sender_id: 发送者 ID。
        persona_id: 目标人格主键（None = 当前主人格）。
        db: 注入的异步 Session。

    Returns:
        裁决结果（相似度 / 阈值 / 原因）。

    Raises:
        BusinessException: 未授予同意 / 功能未启用（``CONFLICT``）；音频
            载荷 / 类型 / 大小非法（``VALIDATION_ERROR``）；向量提供方
            失败（``LLM_CALL_FAILED``）。
    """
    persona = await resolve_target_persona(db, persona_id)
    async with bind_persona_context(persona.id):
        result = await voiceprint_service.verify(db, sender_id, req)
    return ResultDTO.ok(data=result, message="声纹校验完成")
