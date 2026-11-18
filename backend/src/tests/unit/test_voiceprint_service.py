"""声纹服务测试——同意闸 / 注册 / 校验 / 删除 / 撤回 / 保存期 / 人格隔离。

覆盖面：默认关闭配置；同意授予（版本校验 / 幂等）与撤回（清样本留凭证）；
注册（最小样本数 / 时长 / 采样率 / 类型 / 字节 / 重复哈希 / 向量维度）；
校验（命中 / 阈值下 / 短音频拒识 / 无样本）；档案删除幂等；保存期清理；
人格 × 发送者隔离；失败路径文件回滚。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.voice.voiceprint_orm as voiceprint_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.service import bind_persona_context
from packages.harness.voice.voiceprint_models import (
    VOICEPRINT_CONSENT_TEXT_VERSION,
    VoiceprintConsentGrantDTO,
    VoiceprintEnrollRequestDTO,
    VoiceprintRejectReason,
    VoiceprintSampleUploadModel,
    VoiceprintSettings,
    VoiceprintVerifyReason,
    VoiceprintVerifyRequestDTO,
)
from packages.harness.voice.voiceprint_orm import VoiceprintConsentRow
from packages.harness.voice.voiceprint_service import VoiceprintService

#: 测试人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "vp-alpha"
PERSONA_B = "vp-beta"
SENDER = "user-1"


class FakeEmbedding:
    """测试用说话人向量提供方（按音频字节映射向量）。"""

    def __init__(self) -> None:
        self.vectors: dict[bytes, list[float]] = {}
        self.calls: list[tuple[bytes, str, str]] = []
        self.failure: BusinessException | None = None

    async def embed(self, audio: bytes, filename: str, content_type: str) -> list[float]:
        """返回配置向量（未配置时缺省 [1, 0]）。"""
        self.calls.append((audio, filename, content_type))
        if self.failure is not None:
            raise self.failure
        return list(self.vectors.get(audio, [1.0, 0.0]))


def _settings(**overrides: object) -> VoiceprintSettings:
    """构造已启用的测试配置（注册门槛收窄到便于覆盖的数值）。"""
    values: dict[str, object] = {
        "enabled": True,
        "embedding_base_url": "http://voiceprint.test",
        "min_samples": 2,
        "max_samples": 10,
        "max_batch_samples": 10,
        "min_sample_duration_ms": 1000,
        "min_verify_duration_ms": 800,
        "min_sample_rate_hz": 8000,
        "max_upload_bytes": 4096,
        "retention_days": 30,
    }
    values.update(overrides)
    return VoiceprintSettings.model_validate(values)


def _sample(
    audio: bytes,
    *,
    duration_ms: int = 2000,
    sample_rate_hz: int = 16000,
    content_type: str = "audio/wav",
) -> VoiceprintSampleUploadModel:
    """构造一条注册样本（base64 编码）。"""
    return VoiceprintSampleUploadModel(
        audio_base64=base64.b64encode(audio).decode("ascii"),
        content_type=content_type,
        duration_ms=duration_ms,
        sample_rate_hz=sample_rate_hz,
    )


async def _grant(service: VoiceprintService, session: AsyncSession, sender: str = SENDER) -> None:
    """铺底一条有效同意。"""
    await service.grant_consent(
        session, sender, VoiceprintConsentGrantDTO(consent_text_version=VOICEPRINT_CONSENT_TEXT_VERSION)
    )


async def test_settings_default_disabled_and_provider_closed() -> None:
    """缺省配置：功能关闭且未配置向量提供方。"""
    settings = VoiceprintSettings()
    assert settings.enabled is False
    assert settings.embedding_base_url == ""
    assert settings.min_samples >= 1
    assert settings.similarity_threshold == pytest.approx(0.75)


async def test_settings_rejects_inconsistent_sample_bounds() -> None:
    """样本上限低于下限 / 单批上限大于总上限 → 配置校验拒绝。"""
    with pytest.raises(ValueError):
        VoiceprintSettings(min_samples=5, max_samples=3)
    with pytest.raises(ValueError):
        VoiceprintSettings(min_samples=1, max_samples=3, max_batch_samples=4)
    with pytest.raises(ValueError):
        VoiceprintSettings(min_samples=5, max_samples=10, max_batch_samples=3)


async def test_notice_contains_pipl_duties(db_session: AsyncSession) -> None:
    """告知文本覆盖目的 / 范围 / 保存期 / 撤回权。"""
    service = VoiceprintService(settings=_settings(retention_days=90))
    notice = service.notice()
    assert notice.retention_days == 90
    for keyword in ("目的", "范围", "保存期", "撤回权", "隔离"):
        assert keyword in notice.text


async def test_enroll_requires_consent(db_session: AsyncSession) -> None:
    """未授予同意 → 409（同意先于处理）。"""
    service = VoiceprintService(settings=_settings())
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await service.enroll(
                db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
            )
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.details["reason"] == "consent_required"


async def test_disabled_feature_blocks_processing_but_not_management(
    db_session: AsyncSession,
) -> None:
    """功能关闭 / 提供方未配置：注册与校验 409，但同意 / 档案管理仍可用。"""
    for settings in (
        _settings(enabled=False),
        _settings(embedding_base_url=""),
    ):
        service = VoiceprintService(settings=settings, embedding=FakeEmbedding())
        async with bind_persona_context(PERSONA_A):
            profile = await service.get_profile(db_session, SENDER)
            consent = await service.grant_consent(db_session, SENDER, VoiceprintConsentGrantDTO())
            with pytest.raises(BusinessException) as enroll_error:
                await service.enroll(
                    db_session,
                    SENDER,
                    VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")]),
                )
            with pytest.raises(BusinessException) as verify_error:
                await service.verify(
                    db_session,
                    SENDER,
                    VoiceprintVerifyRequestDTO(sample=_sample(b"query")),
                )
            await service.withdraw_consent(db_session, SENDER)
        assert profile.sample_count == 0
        assert consent.active is True
        assert enroll_error.value.code == ErrorCode.CONFLICT
        assert verify_error.value.code == ErrorCode.CONFLICT


async def test_grant_consent_idempotent_and_version_checked(db_session: AsyncSession) -> None:
    """同版本有效同意幂等返回；版本不一致 422。"""
    service = VoiceprintService(settings=_settings())
    async with bind_persona_context(PERSONA_A):
        first = await service.grant_consent(db_session, SENDER, VoiceprintConsentGrantDTO())
        second = await service.grant_consent(db_session, SENDER, VoiceprintConsentGrantDTO())
        assert first.id == second.id
        assert first.active is True
        with pytest.raises(BusinessException) as exc_info:
            await service.grant_consent(
                db_session, SENDER, VoiceprintConsentGrantDTO(consent_text_version="v0")
            )
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_enroll_writes_samples_files_and_profile(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """注册成功：样本行 / 文件落盘 / 档案聚合（条数 / 时长 / 同意）。"""
    embedding = FakeEmbedding()
    service = VoiceprintService(
        settings=_settings(), embedding=embedding, storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        result = await service.enroll(
            db_session,
            SENDER,
            VoiceprintEnrollRequestDTO(samples=[_sample(b"sample-one"), _sample(b"sample-two")]),
        )
        profile = await service.get_profile(db_session, SENDER)
    assert result.accepted == 2
    assert result.rejected == []
    assert profile.sample_count == 2
    assert profile.total_duration_ms == 4000
    assert profile.consent is not None and profile.consent.active is True
    files = list(tmp_path.rglob("*.wav"))
    assert len(files) == 2
    assert all(path.read_bytes() in {b"sample-one", b"sample-two"} for path in files)
    assert len(embedding.calls) == 2


async def test_enroll_below_min_samples_rejected(db_session: AsyncSession) -> None:
    """请求样本数低于注册下限 → 422（批量门槛在同意之后校验）。"""
    service = VoiceprintService(settings=_settings(min_samples=3), embedding=FakeEmbedding())
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        with pytest.raises(BusinessException) as exc_info:
            await service.enroll(
                db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"x"), _sample(b"y")])
            )
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_enroll_rejects_invalid_samples_partially(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """类型 / base64 / 字节 / 时长 / 采样率非法样本逐条拒收，合法样本照常入库。"""
    service = VoiceprintService(
        settings=_settings(max_upload_bytes=1024), embedding=FakeEmbedding(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        result = await service.enroll(
            db_session,
            SENDER,
            VoiceprintEnrollRequestDTO(
                samples=[
                    _sample(b"ok-1"),  # 合法（4 字节）
                    _sample(b"ok-2"),
                    _sample(b"x" * 1025),  # 超字节
                    _sample(b"short", duration_ms=500),
                    _sample(b"rate", sample_rate_hz=4000),
                    _sample(b"gif", content_type="image/gif"),
                    VoiceprintSampleUploadModel(
                        audio_base64="!!!not-base64!!!",
                        content_type="audio/wav",
                        duration_ms=2000,
                        sample_rate_hz=16000,
                    ),
                ]
            ),
        )
    assert result.accepted == 2
    reasons = {item.reason for item in result.rejected}
    assert reasons == {
        VoiceprintRejectReason.PAYLOAD_TOO_LARGE,
        VoiceprintRejectReason.AUDIO_TOO_SHORT,
        VoiceprintRejectReason.SAMPLE_RATE_TOO_LOW,
        VoiceprintRejectReason.UNSUPPORTED_CONTENT_TYPE,
        VoiceprintRejectReason.PAYLOAD_INVALID,
    }


async def test_enroll_duplicate_hash_skipped(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同音频重复上传按哈希幂等跳过（不重复落盘）。"""
    service = VoiceprintService(
        settings=_settings(), embedding=FakeEmbedding(), storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"same"), _sample(b"other")])
        )
        result = await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"same"), _sample(b"third")])
        )
    assert result.accepted == 1
    assert [item.reason for item in result.rejected] == [VoiceprintRejectReason.DUPLICATE]
    assert len(list(tmp_path.rglob("*.wav"))) == 3


async def test_enroll_embedding_dim_mismatch(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同档案内向量维度不一致 → 该样本拒收。"""
    embedding = FakeEmbedding()
    embedding.vectors[b"first"] = [1.0, 0.0]
    embedding.vectors[b"second"] = [1.0, 0.0, 0.0]
    service = VoiceprintService(
        settings=_settings(), embedding=embedding, storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session,
            SENDER,
            VoiceprintEnrollRequestDTO(samples=[_sample(b"first"), _sample(b"tail")]),
        )
        result = await service.enroll(
            db_session,
            SENDER,
            VoiceprintEnrollRequestDTO(samples=[_sample(b"second"), _sample(b"fourth")]),
        )
    reasons = [item.reason for item in result.rejected]
    assert VoiceprintRejectReason.EMBEDDING_DIM_MISMATCH in reasons


async def test_enroll_embedding_failure_rolls_back_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """向量提供方失败 → LLM_CALL_FAILED 且已写文件回滚清理。"""
    embedding = FakeEmbedding()
    service = VoiceprintService(
        settings=_settings(), embedding=embedding, storage_root=tmp_path
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        embedding.failure = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="down")
        with pytest.raises(BusinessException) as exc_info:
            await service.enroll(
                db_session,
                SENDER,
                VoiceprintEnrollRequestDTO(samples=[_sample(b"one"), _sample(b"two")]),
            )
        profile = await service.get_profile(db_session, SENDER)
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert profile.sample_count == 0
    assert list(tmp_path.rglob("*.wav")) == []


async def test_verify_matched_and_below_threshold(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """校验：同向向量命中；正交向量低于阈值。"""
    embedding = FakeEmbedding()
    embedding.vectors[b"enroll-1"] = [1.0, 0.0]
    embedding.vectors[b"enroll-2"] = [1.0, 0.0]
    embedding.vectors[b"query-match"] = [0.9, 0.1]
    embedding.vectors[b"query-other"] = [0.0, 1.0]
    service = VoiceprintService(
        settings=_settings(similarity_threshold=0.8),
        embedding=embedding,
        storage_root=tmp_path,
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session,
            SENDER,
            VoiceprintEnrollRequestDTO(samples=[_sample(b"enroll-1"), _sample(b"enroll-2")]),
        )
        matched = await service.verify(
            db_session, SENDER, VoiceprintVerifyRequestDTO(sample=_sample(b"query-match"))
        )
        rejected = await service.verify(
            db_session, SENDER, VoiceprintVerifyRequestDTO(sample=_sample(b"query-other"))
        )
    assert matched.matched is True
    assert matched.reason is VoiceprintVerifyReason.MATCHED
    assert matched.similarity > 0.8
    assert rejected.matched is False
    assert rejected.reason is VoiceprintVerifyReason.BELOW_THRESHOLD


async def test_verify_short_audio_rejected_without_embedding(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """短音频拒识：不调用向量提供方。"""
    embedding = FakeEmbedding()
    service = VoiceprintService(settings=_settings(), embedding=embedding, storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
        )
        calls_before = len(embedding.calls)
        result = await service.verify(
            db_session,
            SENDER,
            VoiceprintVerifyRequestDTO(sample=_sample(b"short", duration_ms=100)),
        )
    assert result.matched is False
    assert result.reason is VoiceprintVerifyReason.AUDIO_TOO_SHORT
    assert len(embedding.calls) == calls_before


async def test_verify_without_samples(db_session: AsyncSession, tmp_path: Path) -> None:
    """无样本档案：拒识原因 no_samples。"""
    service = VoiceprintService(settings=_settings(), embedding=FakeEmbedding(), storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        result = await service.verify(
            db_session, SENDER, VoiceprintVerifyRequestDTO(sample=_sample(b"query"))
        )
    assert result.matched is False
    assert result.reason is VoiceprintVerifyReason.NO_SAMPLES


async def test_withdraw_clears_samples_and_keeps_consent_history(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """撤回：样本行 / 文件清除；同意行保留并写撤回时间。"""
    service = VoiceprintService(settings=_settings(), embedding=FakeEmbedding(), storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
        )
        result = await service.withdraw_consent(db_session, SENDER)
        profile = await service.get_profile(db_session, SENDER)
    assert result.deleted_samples == 2
    assert result.consent.revoked_at_ms is not None
    assert profile.sample_count == 0
    assert profile.consent is None
    assert list(tmp_path.rglob("*.wav")) == []
    async with bind_persona_context(PERSONA_A):
        rows = list((await db_session.execute(select(VoiceprintConsentRow))).scalars().all())
    assert len(rows) == 1
    assert rows[0].revoked_at_ms is not None


async def test_withdraw_without_consent_not_found(db_session: AsyncSession) -> None:
    """无有效同意时撤回 → 404。"""
    service = VoiceprintService(settings=_settings(), embedding=FakeEmbedding())
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await service.withdraw_consent(db_session, SENDER)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_delete_profile_is_idempotent(db_session: AsyncSession, tmp_path: Path) -> None:
    """删除档案：样本 + 文件清除；重复删除返回 0。"""
    service = VoiceprintService(settings=_settings(), embedding=FakeEmbedding(), storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
        )
        first = await service.delete_profile(db_session, SENDER)
        second = await service.delete_profile(db_session, SENDER)
    assert first.deleted_samples == 2
    assert second.deleted_samples == 0
    assert list(tmp_path.rglob("*.wav")) == []


async def test_retention_purge_removes_expired_samples(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """超过保存期的样本在读取前清理（行 + 文件）。"""
    current = [time_utils.now_ms()]
    service = VoiceprintService(
        settings=_settings(retention_days=1),
        embedding=FakeEmbedding(),
        storage_root=tmp_path,
        clock=lambda: current[0],
    )
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
        )
        current[0] += 2 * 86_400_000
        profile = await service.get_profile(db_session, SENDER)
    assert profile.sample_count == 0
    assert list(tmp_path.rglob("*.wav")) == []


async def test_persona_isolation_for_same_sender(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同一发送者在两人格下的档案互不可见（人格 × 发送者隔离）。"""
    embedding = FakeEmbedding()
    service = VoiceprintService(settings=_settings(), embedding=embedding, storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")])
        )
        profile_a = await service.get_profile(db_session, SENDER)
    async with bind_persona_context(PERSONA_B):
        profile_b = await service.get_profile(db_session, SENDER)
        await _grant(service, db_session)
        await service.enroll(
            db_session, SENDER, VoiceprintEnrollRequestDTO(samples=[_sample(b"c"), _sample(b"d")])
        )
        profile_b_after = await service.get_profile(db_session, SENDER)
    assert profile_a.sample_count == 2
    assert profile_b.sample_count == 0
    assert profile_b_after.sample_count == 2


async def test_list_profiles_includes_consent_only_senders(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """列表包含已同意未注册的发送者（管理面可见状态）。"""
    service = VoiceprintService(settings=_settings(), embedding=FakeEmbedding(), storage_root=tmp_path)
    async with bind_persona_context(PERSONA_A):
        await _grant(service, db_session, "consent-only")
        await _grant(service, db_session, "enrolled")
        await service.enroll(
            db_session,
            "enrolled",
            VoiceprintEnrollRequestDTO(samples=[_sample(b"a"), _sample(b"b")]),
        )
        profiles = await service.list_profiles(db_session)
    by_sender = {profile.sender_id: profile for profile in profiles}
    assert set(by_sender) == {"consent-only", "enrolled"}
    assert by_sender["consent-only"].sample_count == 0
    assert by_sender["enrolled"].sample_count == 2
