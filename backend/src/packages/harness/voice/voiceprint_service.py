"""voice 域声纹服务——注册 / 校验 / 删除 / 同意授予与撤回 / 过期清理。

职责链（管理 API 与后续通道声纹消费面共用同一服务）::

    enroll(session, sender_id, dto)        批量注册（同意闸 → 样本校验 →
                                           向量提取 → 落盘落库）
    verify(session, sender_id, dto)        校验（短音频拒识 → 档案聚合向量
                                           余弦相似度 → 阈值裁决）
    get_profile / list_profiles            档案读取（样本摘要 + 有效同意）
    delete_profile(session, sender_id)     删除档案（样本行 + 文件清除；
                                           同意记录保留为合规凭证）
    grant_consent / withdraw_consent       同意授予 / 撤回（撤回同批清除
                                           样本行与文件）
    notice()                               PIPL 告知（目的 / 范围 / 保存期
                                           / 撤回权）

隐私与隔离（PIPL 合规面）：

    - **同意先于处理**：注册与校验都要求当前存在有效同意（``revoked_at_ms``
      为空且版本与当前告知版本一致），否则 409 ``CONFLICT``；
    - **撤回 / 删除即清**：样本音频文件与向量行同批清除——行删除即索引
      失效，文件删除尽力而为（失败仅告警，文件残留不参与比对）；
    - **保存期**：注册 / 校验 / 读取前先清理超过 ``retention_days`` 的样本
      （行 + 文件）；同意记录不受保存期清理（合规凭证）；
    - **人格 × 发送者隔离**：样本 / 同意行均走 PersonaMixin 自动作用域；
      不同人格的同名发送者互不可见。

配置与提供方：

    - 功能总闸 = ``VOICEPRINT_ENABLED`` 且 ``VOICEPRINT_EMBEDDING_BASE_URL``
      非空；任一不满足时**注册 / 校验**关闭（409 ``CONFLICT``，不产生半可用
      态）；同意授予 / 撤回 / 档案读取与删除不受总闸限制——关停功能后仍可
      履行撤回权与清理数据；
    - 向量提取经既有共享 HTTP 池（``pools.manager``）——零私建连接、零新增
      依赖；测试经构造注入 fake 提供方；
    - 阈值 / 样本条数 / 时长 / 采样率 / 字节上限 / 保存期全部来自
      ``VoiceprintSettings``（构造注入可覆盖，缺省读全局 settings）。

文件 I/O 一律经 ``run_blocking_ops`` 下沉线程池（硬约束：阻塞 I/O 不占
事件循环）；写入采用「临时文件 + fsync + ``os.replace``」原子换名。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.config.models import settings as global_settings
from packages.harness.core import time_utils
from packages.harness.core.context import persona_id_var, request_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.file_utils import unlink_missing_ok, write_bytes_atomic
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.pools.manager import get_pool_manager
from packages.harness.voice.voice_budget import (
    DailyBudgetLimits,
    VoiceBudgetCounter,
    VoiceUsageRecorder,
    emit_voice_usage,
    enforce_daily_budget,
)
from packages.harness.voice.voiceprint_embedding import VoiceprintEmbeddingClient
from packages.harness.voice.voiceprint_models import (
    VOICEPRINT_AUDIO_MIME_ALLOWLIST,
    VOICEPRINT_CONSENT_TEXT_VERSION,
    VOICEPRINT_PRIVACY_NOTICE_VERSION,
    VoiceprintConsentGrantDTO,
    VoiceprintConsentRecordDTO,
    VoiceprintDeleteResultModel,
    VoiceprintEmbeddingFace,
    VoiceprintEnrollRequestDTO,
    VoiceprintEnrollResultModel,
    VoiceprintPrivacyNoticeDTO,
    VoiceprintProfileDTO,
    VoiceprintRejectedSampleModel,
    VoiceprintRejectReason,
    VoiceprintSampleRecordDTO,
    VoiceprintSampleUploadModel,
    VoiceprintSettings,
    VoiceprintVerifyReason,
    VoiceprintVerifyRequestDTO,
    VoiceprintVerifyResultModel,
    VoiceprintWithdrawResultModel,
    render_privacy_notice,
)
from packages.harness.voice.voiceprint_orm import VoiceprintConsentRow, VoiceprintSampleRow

__all__ = ["VOICEPRINT_MIME_EXTENSIONS", "VoiceprintService"]

#: MIME → 样本文档扩展名（落盘命名与向量服务 filename 字段共用）。
VOICEPRINT_MIME_EXTENSIONS: Final[dict[str, str]] = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
}

#: 声纹用量行的 provider 值（说话人向量服务）。
_VOICEPRINT_PROVIDER: Final[str] = "voiceprint-embedding"


class VoiceprintService:
    """声纹服务（注册 / 校验 / 档案读取 / 删除 / 同意 / 过期清理）。

    构造注入（测试 / 装配友好）：settings / embedding / storage_root /
    usage_recorder / usage_counter / clock 均可显式覆盖；缺省 settings 读
    全局单例、embedding 经公共池瞬时构建、storage_root 取
    settings.storage_dir。服务自身无状态（Session 逐次注入），可安全跨
    请求共享。
    """

    def __init__(
        self,
        *,
        settings: VoiceprintSettings | None = None,
        embedding: VoiceprintEmbeddingFace | None = None,
        storage_root: Path | None = None,
        usage_recorder: VoiceUsageRecorder | None = None,
        usage_counter: VoiceBudgetCounter | None = None,
        clock: Callable[[], int] = time_utils.now_ms,
    ) -> None:
        """初始化服务。

        Args:
            settings: 声纹配置（None = 全局 settings.voiceprint）。
            embedding: 向量提供方（None = 调用时经公共池瞬时构建）。
            storage_root: 样本落盘根目录（None = settings.storage_dir）。
            usage_recorder: 用量记录器（None = 未接线——零记录，装配时经
                wire_default_voice_usage_recorder 绑定）。
            usage_counter: 分面预算计数面（None = 零预算门禁——纯编排 /
                测试形态；装配时经 wire_default_voice_usage_counter 绑定）。
            clock: 时钟（epoch 毫秒——保存期截止 / 同意时间轴 / 预算日桶）。
        """
        self._settings = settings
        self._embedding = embedding
        self._storage_root = storage_root
        self._usage_recorder = usage_recorder
        self._usage_counter = usage_counter
        self._clock = clock

    # ------------------------------------------------------------------
    # 告知面
    # ------------------------------------------------------------------

    def notice(self) -> VoiceprintPrivacyNoticeDTO:
        """读取 PIPL 告知（目的 / 范围 / 保存期 / 撤回权）。

        Returns:
            告知 DTO（版本与同意文本版本同源；保存期取配置）。
        """
        settings = self._resolve_settings()
        return VoiceprintPrivacyNoticeDTO(
            notice_version=VOICEPRINT_PRIVACY_NOTICE_VERSION,
            consent_text_version=VOICEPRINT_CONSENT_TEXT_VERSION,
            retention_days=settings.retention_days,
            text=render_privacy_notice(settings.retention_days),
        )

    # ------------------------------------------------------------------
    # 同意面
    # ------------------------------------------------------------------

    async def grant_consent(
        self, session: AsyncSession, sender_id: str, dto: VoiceprintConsentGrantDTO
    ) -> VoiceprintConsentRecordDTO:
        """授予同意（当前有效同意同版本时幂等返回）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 发送者 ID。
            dto: 授予入参（同意的告知文本版本）。

        Returns:
            有效同意记录（新授予或既有同版本记录）。

        Raises:
            BusinessException: 版本与当前告知不一致（``VALIDATION_ERROR``）。
        """
        if dto.consent_text_version != VOICEPRINT_CONSENT_TEXT_VERSION:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="同意文本版本与当前隐私告知不一致",
                details={
                    "provided": dto.consent_text_version,
                    "expected": VOICEPRINT_CONSENT_TEXT_VERSION,
                },
            )
        active = await self._active_consent(session, sender_id)
        if active is not None:
            return self._consent_record(active)
        row = VoiceprintConsentRow(
            sender_id=sender_id,
            consent_text_version=dto.consent_text_version,
            granted_at_ms=self._clock(),
            revoked_at_ms=None,
        )
        session.add(row)
        await self._commit(session)
        logger.info("声纹同意已授予: sender_id={}, version={}", sender_id, row.consent_text_version)
        return self._consent_record(row)

    async def withdraw_consent(
        self, session: AsyncSession, sender_id: str
    ) -> VoiceprintWithdrawResultModel:
        """撤回同意（写入撤回时间并清除该发送者的全部样本与文件）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 发送者 ID。

        Returns:
            撤回结果（撤回后的同意记录 + 清除样本条数）。

        Raises:
            BusinessException: 无有效同意（``NOT_FOUND``）。
        """
        active = await self._active_consent(session, sender_id)
        if active is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="该发送者当前没有有效的声纹同意记录",
                details={"sender_id": sender_id},
            )
        active.revoked_at_ms = self._clock()
        deleted_samples, paths = await self._delete_sample_rows(session, sender_id)
        await self._commit(session)
        await self._remove_files(paths)
        logger.info(
            "声纹同意已撤回: sender_id={}, deleted_samples={}", sender_id, deleted_samples
        )
        return VoiceprintWithdrawResultModel(
            consent=self._consent_record(active), deleted_samples=deleted_samples
        )

    # ------------------------------------------------------------------
    # 注册面
    # ------------------------------------------------------------------

    async def enroll(
        self, session: AsyncSession, sender_id: str, dto: VoiceprintEnrollRequestDTO
    ) -> VoiceprintEnrollResultModel:
        """批量注册声纹样本（同意闸 + 逐样本校验 + 向量提取 + 原子落盘）。

        部分成功语义：单条样本的载荷 / 类型 / 时长 / 采样率 / 重复 / 向量
        维度问题进入 ``rejected``（其余照常接收）；提供方调用失败整体上抛
        ``LLM_CALL_FAILED``（已写入的文件回滚清理）。并发注册撞
        ``uq_ff_voiceprint_samples_persona_sender_sha`` 时整体回滚并清理
        本次文件，按 duplicate 语义上抛 ``CONFLICT``。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 发送者 ID（平台侧用户标识）。
            dto: 注册入参（批量样本）。

        Returns:
            注册结果（接收条数 + 逐样本拒收结局 + 注册后档案）。

        Raises:
            BusinessException: 功能未启用 / 无有效同意（``CONFLICT``）；
                样本条数低于注册下限 / 高于单批上限 / 超出档案容量
                （``VALIDATION_ERROR``）；日预算超限（``RATE_LIMITED``
                ——已写文件回滚清理）；向量提供方失败
                （``LLM_CALL_FAILED``）。
        """
        settings = self._require_available()
        await self._require_active_consent(session, sender_id)
        if len(dto.samples) < settings.min_samples:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"声纹注册至少需要 {settings.min_samples} 条样本",
                details={"provided": len(dto.samples), "min_samples": settings.min_samples},
            )
        if len(dto.samples) > settings.max_batch_samples:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"单次声纹注册最多 {settings.max_batch_samples} 条样本",
                details={
                    "provided": len(dto.samples),
                    "max_batch_samples": settings.max_batch_samples,
                },
            )
        await self._purge_expired(session, sender_id)
        existing = await self._list_sample_rows(session, sender_id)
        if len(existing) + len(dto.samples) > settings.max_samples:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"声纹档案样本数上限为 {settings.max_samples} 条",
                details={
                    "existing": len(existing),
                    "incoming": len(dto.samples),
                    "max_samples": settings.max_samples,
                },
            )
        existing_hashes = {row.sample_sha256 for row in existing}
        embedding_dim = existing[0].embedding_dim if existing else None
        accepted = 0
        rejected: list[VoiceprintRejectedSampleModel] = []
        written_paths: list[Path] = []
        try:
            for index, sample in enumerate(dto.samples):
                audio, reason, message = self._validate_sample(
                    sample, settings, min_duration_ms=settings.min_sample_duration_ms
                )
                if audio is None:
                    rejected.append(
                        VoiceprintRejectedSampleModel(
                            index=index,
                            reason=reason
                            if reason is not None
                            else VoiceprintRejectReason.PAYLOAD_INVALID,
                            message=message,
                        )
                    )
                    continue
                digest = hashlib.sha256(audio).hexdigest()
                if digest in existing_hashes:
                    rejected.append(
                        VoiceprintRejectedSampleModel(
                            index=index,
                            reason=VoiceprintRejectReason.DUPLICATE,
                            message="该样本已存在于当前声纹档案（按哈希去重）",
                        )
                    )
                    continue
                extension = VOICEPRINT_MIME_EXTENSIONS[sample.content_type.lower()]
                vector = await self._embed_with_budget(
                    settings, audio, f"voiceprint{extension}", sample.content_type
                )
                if embedding_dim is None:
                    embedding_dim = len(vector)
                elif len(vector) != embedding_dim:
                    rejected.append(
                        VoiceprintRejectedSampleModel(
                            index=index,
                            reason=VoiceprintRejectReason.EMBEDDING_DIM_MISMATCH,
                            message=(
                                f"说话人向量维度与既有档案不一致"
                                f"（{len(vector)} != {embedding_dim}）"
                            ),
                        )
                    )
                    continue
                path = await self._write_sample_file(sender_id, digest, extension, audio)
                written_paths.append(path)
                session.add(
                    VoiceprintSampleRow(
                        sender_id=sender_id,
                        sample_path=str(path),
                        sample_sha256=digest,
                        duration_ms=sample.duration_ms,
                        sample_rate_hz=sample.sample_rate_hz,
                        embedding=json.dumps(vector, ensure_ascii=False),
                        embedding_dim=embedding_dim,
                    )
                )
                existing_hashes.add(digest)
                accepted += 1
            await self._commit(session)
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            existing_paths = set(
                (
                    await session.execute(
                        select(VoiceprintSampleRow.sample_path).where(
                            VoiceprintSampleRow.sender_id == sender_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            await self._remove_files(
                [path for path in written_paths if str(path) not in existing_paths]
            )
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="声纹样本已存在（并发注册按唯一键收敛）",
                details={
                    "reason": VoiceprintRejectReason.DUPLICATE.value,
                    "sender_id": sender_id,
                },
            ) from exc
        except Exception:
            await self._rollback_quietly(session)
            await self._remove_files(written_paths)
            raise
        profile = await self.get_profile(session, sender_id)
        logger.info(
            "声纹注册完成: sender_id={}, accepted={}, rejected={}",
            sender_id,
            accepted,
            len(rejected),
        )
        return VoiceprintEnrollResultModel(
            profile=profile, accepted=accepted, rejected=rejected
        )

    # ------------------------------------------------------------------
    # 校验面
    # ------------------------------------------------------------------

    async def verify(
        self, session: AsyncSession, sender_id: str, dto: VoiceprintVerifyRequestDTO
    ) -> VoiceprintVerifyResultModel:
        """校验音频是否属于目标发送者（短音频 / 无样本 / 低于阈值均拒识）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 被比对的目标发送者 ID。
            dto: 校验入参（单条音频）。

        Returns:
            裁决结果（相似度 / 阈值 / 原因）。

        Raises:
            BusinessException: 功能未启用 / 无有效同意（``CONFLICT``）；
                音频载荷 / 类型 / 大小非法（``VALIDATION_ERROR``）；日预算
                超限（``RATE_LIMITED``——零出站零记账）；向量提供方失败
                （``LLM_CALL_FAILED``）。
        """
        settings = self._require_available()
        await self._require_active_consent(session, sender_id)
        if dto.sample.duration_ms < settings.min_verify_duration_ms:
            return VoiceprintVerifyResultModel(
                sender_id=sender_id,
                matched=False,
                similarity=0.0,
                threshold=settings.similarity_threshold,
                reason=VoiceprintVerifyReason.AUDIO_TOO_SHORT,
            )
        audio, reason, message = self._validate_sample(
            dto.sample, settings, min_duration_ms=settings.min_verify_duration_ms
        )
        if audio is None:
            reject_reason = (
                reason if reason is not None else VoiceprintRejectReason.PAYLOAD_INVALID
            )
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"声纹校验音频非法：{message}",
                details={"reason": reject_reason.value},
            )
        await self._purge_expired(session, sender_id)
        rows = await self._list_sample_rows(session, sender_id)
        if not rows:
            return VoiceprintVerifyResultModel(
                sender_id=sender_id,
                matched=False,
                similarity=0.0,
                threshold=settings.similarity_threshold,
                reason=VoiceprintVerifyReason.NO_SAMPLES,
            )
        extension = VOICEPRINT_MIME_EXTENSIONS[dto.sample.content_type.lower()]
        vector = await self._embed_with_budget(
            settings, audio, f"voiceprint-query{extension}", dto.sample.content_type
        )
        aggregate = _aggregate_vectors([json.loads(row.embedding) for row in rows])
        similarity = _cosine_similarity(vector, aggregate)
        matched = similarity >= settings.similarity_threshold
        return VoiceprintVerifyResultModel(
            sender_id=sender_id,
            matched=matched,
            similarity=similarity,
            threshold=settings.similarity_threshold,
            reason=(
                VoiceprintVerifyReason.MATCHED
                if matched
                else VoiceprintVerifyReason.BELOW_THRESHOLD
            ),
        )

    # ------------------------------------------------------------------
    # 档案读取 / 删除
    # ------------------------------------------------------------------

    async def get_profile(
        self, session: AsyncSession, sender_id: str
    ) -> VoiceprintProfileDTO:
        """读取单发送者声纹档案（先清理过期样本）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 发送者 ID。

        Returns:
            档案聚合（无样本时为零条目的空档案——不报错）。
        """
        settings = self._resolve_settings()
        await self._purge_expired(session, sender_id)
        rows = await self._list_sample_rows(session, sender_id)
        consent = await self._active_consent(session, sender_id)
        return VoiceprintProfileDTO(
            sender_id=sender_id,
            sample_count=len(rows),
            total_duration_ms=sum(row.duration_ms for row in rows),
            samples=[self._sample_record(row) for row in rows],
            consent=self._consent_record(consent) if consent is not None else None,
            privacy_notice_version=VOICEPRINT_PRIVACY_NOTICE_VERSION,
            privacy_notice=render_privacy_notice(settings.retention_days),
        )

    async def list_profiles(self, session: AsyncSession) -> list[VoiceprintProfileDTO]:
        """读取当前人格下全部声纹档案（样本主体 ∪ 同意主体）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            档案列表（发送者 ID 升序；无样本但曾授予同意的发送者也会出现
            ——便于管理面展示「已同意未注册」状态）。
        """
        await self._purge_expired(session, None)
        sample_senders = list(
            (
                await session.execute(select(VoiceprintSampleRow.sender_id).distinct())
            )
            .scalars()
            .all()
        )
        consent_senders = list(
            (
                await session.execute(select(VoiceprintConsentRow.sender_id).distinct())
            )
            .scalars()
            .all()
        )
        sender_ids = sorted(set(sample_senders) | set(consent_senders))
        return [await self.get_profile(session, sender_id) for sender_id in sender_ids]

    async def delete_profile(
        self, session: AsyncSession, sender_id: str
    ) -> VoiceprintDeleteResultModel:
        """删除声纹档案（样本行 + 文件；同意记录保留）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 发送者 ID。

        Returns:
            删除结果（清除样本条数；零样本时 idempotent 成功）。
        """
        deleted_samples, paths = await self._delete_sample_rows(session, sender_id)
        await self._commit(session)
        await self._remove_files(paths)
        logger.info("声纹档案已删除: sender_id={}, deleted_samples={}", sender_id, deleted_samples)
        return VoiceprintDeleteResultModel(
            sender_id=sender_id, deleted_samples=deleted_samples
        )

    # ------------------------------------------------------------------
    # 内部——配置 / 同意 / 样本读写 / 文件 / 向量
    # ------------------------------------------------------------------

    def _resolve_settings(self) -> VoiceprintSettings:
        """取声纹配置（注入优先；None 读全局 settings.voiceprint）。"""
        return self._settings if self._settings is not None else global_settings.voiceprint

    def _require_available(self) -> VoiceprintSettings:
        """校验声纹处理可用（总闸 + 提供方均就绪——仅注册 / 校验需要）。

        Returns:
            生效配置。

        Raises:
            BusinessException: 未启用（``CONFLICT``——与 voice TTS / ASR
                禁用态同形态；details 指明两个前置键）。管理面（同意 /
                档案 / 删除）不受本闸限制——关停功能后仍可撤回与清理。
        """
        settings = self._resolve_settings()
        if not settings.enabled or not settings.embedding_base_url.strip():
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="声纹识别未启用（需 VOICEPRINT_ENABLED=true 且配置 VOICEPRINT_EMBEDDING_BASE_URL）",
                details={
                    "reason": "voiceprint disabled",
                    "env_flags": ["VOICEPRINT_ENABLED", "VOICEPRINT_EMBEDDING_BASE_URL"],
                },
            )
        return settings

    async def _active_consent(
        self, session: AsyncSession, sender_id: str
    ) -> VoiceprintConsentRow | None:
        """读取当前有效同意（撤回时间为空且版本与当前告知一致）。"""
        stmt = (
            select(VoiceprintConsentRow)
            .where(
                VoiceprintConsentRow.sender_id == sender_id,
                VoiceprintConsentRow.revoked_at_ms.is_(None),
                VoiceprintConsentRow.consent_text_version
                == VOICEPRINT_CONSENT_TEXT_VERSION,
            )
            .order_by(desc(VoiceprintConsentRow.granted_at_ms), desc(VoiceprintConsentRow.id))
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    async def _require_active_consent(
        self, session: AsyncSession, sender_id: str
    ) -> VoiceprintConsentRow:
        """要求有效同意存在（否则 409——PIPL 同意先于处理）。

        Raises:
            BusinessException: 无有效同意（``CONFLICT``，details 指引授予
                端点与当前同意文本版本）。
        """
        row = await self._active_consent(session, sender_id)
        if row is None:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="声纹处理需要先授予同意（调用同意端点后再注册 / 校验）",
                details={
                    "reason": "consent_required",
                    "sender_id": sender_id,
                    "consent_text_version": VOICEPRINT_CONSENT_TEXT_VERSION,
                },
            )
        return row

    async def _list_sample_rows(
        self, session: AsyncSession, sender_id: str
    ) -> list[VoiceprintSampleRow]:
        """读取发送者的样本行（入库时间升序——档案展示顺序）。"""
        stmt = (
            select(VoiceprintSampleRow)
            .where(VoiceprintSampleRow.sender_id == sender_id)
            .order_by(VoiceprintSampleRow.created_at, VoiceprintSampleRow.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _delete_sample_rows(
        self, session: AsyncSession, sender_id: str
    ) -> tuple[int, list[Path]]:
        """删除发送者全部样本行（返回条数与待删文件路径；不提交事务）。"""
        rows = await self._list_sample_rows(session, sender_id)
        paths = [Path(row.sample_path) for row in rows]
        for row in rows:
            await session.delete(row)
        return len(rows), paths

    async def _purge_expired(
        self, session: AsyncSession, sender_id: str | None
    ) -> None:
        """清理超过保存期的样本行与文件（同意记录不清理）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            sender_id: 限定发送者（None = 当前人格全量）。
        """
        settings = self._resolve_settings()
        cutoff = self._clock() - settings.retention_days * 86_400_000
        stmt = select(VoiceprintSampleRow).where(VoiceprintSampleRow.created_at < cutoff)
        if sender_id is not None:
            stmt = stmt.where(VoiceprintSampleRow.sender_id == sender_id)
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return
        paths = [Path(row.sample_path) for row in rows]
        for row in rows:
            await session.delete(row)
        await self._commit(session)
        await self._remove_files(paths)
        logger.info(
            "声纹样本过期清理: count={}, retention_days={}, sender_id={}",
            len(rows),
            settings.retention_days,
            sender_id or "(全部)",
        )

    def _validate_sample(
        self,
        sample: VoiceprintSampleUploadModel,
        settings: VoiceprintSettings,
        *,
        min_duration_ms: int,
    ) -> tuple[bytes | None, VoiceprintRejectReason | None, str]:
        """校验单条样本（载荷 / 类型 / 字节 / 时长 / 采样率）。

        Args:
            sample: 待校验样本。
            settings: 生效配置（类型 / 字节 / 采样率门槛来源）。
            min_duration_ms: 本次处理路径的最短时长门槛（注册用注册门槛，
                校验用校验门槛——两条路径独立，互不套用）。

        Returns:
            ``(音频字节 | None, 拒收原因 | None, 中文说明)`` 三元组；通过时
            原因为 None。
        """
        content_type = sample.content_type.strip().lower()
        if content_type not in VOICEPRINT_AUDIO_MIME_ALLOWLIST:
            return (
                None,
                VoiceprintRejectReason.UNSUPPORTED_CONTENT_TYPE,
                f"不支持的音频类型 {sample.content_type}（允许 WAV / MP3 / OGG / WebM / MP4）",
            )
        try:
            audio = base64.b64decode("".join(sample.audio_base64.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            return (
                None,
                VoiceprintRejectReason.PAYLOAD_INVALID,
                f"音频 base64 载荷非法：{exc}",
            )
        if not audio:
            return (
                None,
                VoiceprintRejectReason.PAYLOAD_INVALID,
                "音频载荷解码后为空",
            )
        if len(audio) > settings.max_upload_bytes:
            return (
                None,
                VoiceprintRejectReason.PAYLOAD_TOO_LARGE,
                f"音频字节数 {len(audio)} 超过上限 {settings.max_upload_bytes}",
            )
        if sample.duration_ms < min_duration_ms:
            return (
                None,
                VoiceprintRejectReason.AUDIO_TOO_SHORT,
                f"音频时长 {sample.duration_ms}ms 低于下限 {min_duration_ms}ms",
            )
        if sample.sample_rate_hz < settings.min_sample_rate_hz:
            return (
                None,
                VoiceprintRejectReason.SAMPLE_RATE_TOO_LOW,
                f"采样率 {sample.sample_rate_hz}Hz 低于下限 {settings.min_sample_rate_hz}Hz",
            )
        return audio, None, ""

    async def _embed_with_budget(
        self,
        settings: VoiceprintSettings,
        audio: bytes,
        filename: str,
        content_type: str,
    ) -> list[float]:
        """经分面预算闸执行一次向量提取，并在成功后记录用量。

        每次真实出站调用独立判定 / 记录（注册批量的逐样本循环共用本
        方法——单样本被预算拒绝时整体回滚由调用方异常路径承担）。

        Args:
            settings: 声纹配置（预算上限与记账条目来源）。
            audio: 音频字节。
            filename: 向量服务 filename 字段。
            content_type: 音频 MIME。

        Returns:
            说话人向量。

        Raises:
            BusinessException: 日预算超限（``RATE_LIMITED``——details 含
                scope / used / limit / purpose，零出站零记账）；向量
                提供方失败（``LLM_CALL_FAILED``——失败不记账）。
        """
        persona_id = persona_id_var.get()
        await enforce_daily_budget(
            purpose="voiceprint",
            label="声纹向量提取",
            limits=DailyBudgetLimits(
                global_limit=settings.daily_budget_global,
                persona_limit=settings.daily_budget_persona,
            ),
            persona_id=persona_id,
            counter=self._usage_counter,
            clock=self._clock,
        )
        started_ms = self._clock()
        vector = await self._resolve_embedding().embed(audio, filename, content_type)
        await emit_voice_usage(
            self._usage_recorder,
            purpose="voiceprint",
            model_entry=settings.embedding_model,
            provider=_VOICEPRINT_PROVIDER,
            persona_id=persona_id,
            request_id=request_id_var.get(),
            ts_ms=started_ms,
            latency_ms=max(0, self._clock() - started_ms),
        )
        return vector

    def _resolve_embedding(self) -> VoiceprintEmbeddingFace:
        """取向量提供方（注入优先；缺省经公共池共享 client 瞬时构建）。

        Raises:
            BusinessException: 公共池未启动（``INTERNAL_ERROR``——与
                VoiceService 同前置契约）。
        """
        if self._embedding is not None:
            return self._embedding
        settings = self._resolve_settings()
        manager = get_pool_manager()
        if manager is None or not manager.started:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="公共池未初始化，请先在 lifespan startup 中调用 pool_manager.start()",
            )
        return VoiceprintEmbeddingClient(
            manager.get_http_client(),
            base_url=settings.embedding_base_url,
            endpoint_path=settings.embedding_path,
            model_id=settings.embedding_model,
            request_timeout_seconds=settings.request_timeout_seconds,
        )

    def _storage_dir(self) -> Path:
        """样本落盘根目录（注入优先；缺省 config.storage_dir）。"""
        if self._storage_root is not None:
            return self._storage_root
        return Path(self._resolve_settings().storage_dir)

    async def _write_sample_file(
        self, sender_id: str, digest: str, extension: str, audio: bytes
    ) -> Path:
        """原子写入样本文件（临时文件 + fsync + os.replace——线程池下沉）。"""
        root = self._storage_dir()
        sender_dir = root / hashlib.sha256(sender_id.encode("utf-8")).hexdigest()[:16]
        target = sender_dir / f"{digest}{extension}"
        return await run_blocking_ops(write_bytes_atomic, sender_dir, target, audio)

    async def _remove_files(self, paths: list[Path]) -> None:
        """尽力删除文件（失败仅告警——DB 行已清理，残留文件不参与比对）。"""
        for path in paths:
            try:
                await run_blocking_ops(unlink_missing_ok, path)
            except OSError:
                logger.opt(exception=True).warning("声纹样本文件删除失败: {}", path)

    @staticmethod
    def _sample_record(row: VoiceprintSampleRow) -> VoiceprintSampleRecordDTO:
        """样本行 → 读取 DTO（向量本身不回传）。"""
        return VoiceprintSampleRecordDTO(
            id=row.id,
            sender_id=row.sender_id,
            sample_sha256=row.sample_sha256,
            duration_ms=row.duration_ms,
            sample_rate_hz=row.sample_rate_hz,
            embedding_dim=row.embedding_dim,
            created_at=row.created_at,
        )

    @staticmethod
    def _consent_record(row: VoiceprintConsentRow) -> VoiceprintConsentRecordDTO:
        """同意行 → 读取 DTO。"""
        return VoiceprintConsentRecordDTO(
            id=row.id,
            sender_id=row.sender_id,
            consent_text_version=row.consent_text_version,
            granted_at_ms=row.granted_at_ms,
            revoked_at_ms=row.revoked_at_ms,
        )

    @staticmethod
    async def _commit(session: AsyncSession) -> None:
        """提交事务（失败回滚后原样上抛）。"""
        try:
            await session.commit()
        except Exception:
            await VoiceprintService._rollback_quietly(session)
            raise

    @staticmethod
    async def _rollback_quietly(session: AsyncSession) -> None:
        """尽力回滚（异常路径清理会话待处理态——不注入新异常）。"""
        try:
            await session.rollback()
        except Exception:  # best-effort: 回滚失败由会话关闭兜底
            logger.opt(exception=True).debug("声纹路径会话回滚失败（close 兜底）")


def _aggregate_vectors(vectors: list[list[float]]) -> list[float]:
    """样本向量取均值（校验比对用的档案聚合向量）。

    Args:
        vectors: 同维度向量列表（非空）。

    Returns:
        逐维均值向量。

    Raises:
        ValueError: 向量列表为空或维度不一致（档案内部不变式破坏）。
    """
    if not vectors:
        raise ValueError("档案向量列表为空")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("档案内样本向量维度不一致")
    aggregate = [0.0] * dimension
    for vector in vectors:
        for index, value in enumerate(vector):
            aggregate[index] += float(value)
    return [value / len(vectors) for value in aggregate]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两向量的余弦相似度（结果夹取到 [-1, 1]）。

    Args:
        left: 查询向量。
        right: 档案聚合向量。

    Returns:
        余弦相似度；任一向量零范数时返回 0.0（无方向信息——按不相似处理）。
    """
    if len(left) != len(right):
        raise ValueError("余弦相似度的两向量维度不一致")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

