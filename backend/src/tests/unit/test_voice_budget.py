"""voice 域分面日预算测试——计量口径 / 闸门矩阵 / 实时与声纹接线。

覆盖矩阵：

    - 配置面：三组 env 键（TTS_ / ASR_ / VOICEPRINT_）读取、0..10,000
      域校验；
    - 闸门矩阵：每 purpose 的 persona / global 超限拒绝（details 含
      scope / used / limit / purpose，零出站零记账）、used=limit-1 放行、
      两档 0 不限且零计数查询；
    - purpose 隔离：活 SQLite 计数按 purpose 过滤（LLM 用途行不计入
      ASR 预算，语音行不计入 LLM 预算）；
    - 记账口径：每次成功调用恰好一行；失败与被拒调用不落行；
    - 声纹：注册 / 校验出站前闸；批量中途超限整体回滚（文件 + 样本行）；
    - 全部异步 + 共享 SQLite（StaticPool），零真实网络与真实等待。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.harness.config.models import AsrSettings, Settings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.llm.gateway import wire_default_usage_counter
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.models import PersonaStatus
from packages.harness.persona.service import bind_persona_context
from packages.harness.usage.budget_limits import DAILY_BUDGET_MAX
from packages.harness.usage.dtos import UsageCreateDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus
from packages.harness.usage.orm import UsageRecordRow
from packages.harness.usage.service import UsageService
from packages.harness.voice.voice_budget import (
    wire_default_voice_usage_counter,
    wire_default_voice_usage_recorder,
)
from packages.harness.voice.voice_models import AsrResultModel, TtsResultModel, VoiceUsageEvent
from packages.harness.voice.voice_service import VoiceService
from packages.harness.voice.voiceprint_models import (
    VOICEPRINT_CONSENT_TEXT_VERSION,
    VoiceprintConsentGrantDTO,
    VoiceprintEnrollRequestDTO,
    VoiceprintSampleUploadModel,
    VoiceprintSettings,
    VoiceprintVerifyRequestDTO,
)
from packages.harness.voice.voiceprint_orm import VoiceprintSampleRow
from packages.harness.voice.voiceprint_service import VoiceprintService
from tests.unit.test_voiceprint_service import _wav_bytes

#: 测试日桶起点（UTC 零点——计数口径与预算闸同轴）。
_DAY_START_MS: int = 1_731_000_000_000 // 86_400_000 * 86_400_000
#: 测试人格上下文轴。
_PERSONA = "42"
#: 测试用 WAV 载荷。
_FAKE_WAV: bytes = b"RIFF-fake-wav"


class FakeTtsClient:
    """假 TTS 客户端（记录调用；可注入故障）。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化假客户端。

        Args:
            error: 故障注入（调用即抛）。
        """
        self.error = error
        self.calls: list[str] = []

    async def synthesize(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> TtsResultModel:
        """假合成（记录文本 + 返回固定产物 / 注入故障）。"""
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return TtsResultModel(audio=_FAKE_WAV, latency_ms=1)


class FakeAsrClient:
    """假 ASR 客户端（记录调用；可注入故障）。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化假客户端。

        Args:
            error: 故障注入（调用即抛）。
        """
        self.error = error
        self.calls: list[str] = []

    async def transcribe(
        self, audio: bytes, filename: str, *, language: str | None = None
    ) -> AsrResultModel:
        """假转写（记录文件名 + 返回固定产物 / 注入故障）。"""
        self.calls.append(filename)
        if self.error is not None:
            raise self.error
        return AsrResultModel(text="你好", duration_ms=10, language="zh")


class FakeEmbedding:
    """假说话人向量提供方（记录调用）。"""

    def __init__(self) -> None:
        """初始化假提供方。"""
        self.calls: list[str] = []
        self.error: BusinessException | None = None

    async def embed(self, audio: bytes, filename: str, content_type: str) -> list[float]:
        """返回固定向量 / 注入故障。"""
        self.calls.append(filename)
        if self.error is not None:
            raise self.error
        return [1.0, 0.0]


class FakeBudgetCounter:
    """脚本化分面计数面（记录调用；可禁用以断言零查询）。"""

    def __init__(
        self,
        counts: dict[tuple[str, str | None], int],
        *,
        forbidden: bool = False,
    ) -> None:
        """初始化计数面。

        Args:
            counts: (purpose, persona_id) → 当日已用数。
            forbidden: True 时任何查询即失败（0 不限零查询断言）。
        """
        self.counts = counts
        self.forbidden = forbidden
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(self, purpose: str, persona_id: str | None, day_start_ms: int) -> int:
        """记录查询并返回脚本化计数。"""
        if self.forbidden:
            raise AssertionError("预算为 0 时不应发起计数查询")
        self.calls.append((purpose, persona_id, day_start_ms))
        return self.counts.get((purpose, persona_id), 0)


class RecordingRecorder:
    """记录式用量记录器。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.events: list[VoiceUsageEvent] = []

    async def __call__(self, event: VoiceUsageEvent) -> None:
        """收集事件。"""
        self.events.append(event)


def _voice_settings(
    *,
    tts_persona: int = 0,
    tts_global: int = 0,
    asr_persona: int = 0,
    asr_global: int = 0,
) -> Settings:
    """构造启用形态的隔离 Settings（预算档显式注入）。"""
    return build_isolated_settings(
        voice=VoiceSettings(
            tts=TtsSettings(
                enabled=True,
                base_url="http://tts.test",
                model_entry="cosyvoice3",
                daily_budget_persona=tts_persona,
                daily_budget_global=tts_global,
            ),
            asr=AsrSettings(
                enabled=True,
                base_url="http://asr.test",
                model_entry="qwen3-asr-0.6b",
                daily_budget_persona=asr_persona,
                daily_budget_global=asr_global,
            ),
        )
    )


def _voiceprint_settings(**overrides: object) -> VoiceprintSettings:
    """构造已启用的声纹配置（门槛收窄到便于覆盖的数值）。"""
    values: dict[str, object] = {
        "enabled": True,
        "embedding_base_url": "http://voiceprint.test",
        "min_samples": 1,
        "max_samples": 10,
        "max_batch_samples": 10,
        "min_sample_duration_ms": 1000,
        "min_verify_duration_ms": 800,
        "min_sample_rate_hz": 8000,
        "max_upload_bytes": 1_000_000,
        "retention_days": 30,
    }
    values.update(overrides)
    return VoiceprintSettings.model_validate(values)


def _sample(audio: bytes) -> VoiceprintSampleUploadModel:
    """构造一条注册样本（base64 编码）。"""
    return VoiceprintSampleUploadModel(
        audio_base64=base64.b64encode(_wav_bytes(audio)).decode("ascii"),
        content_type="audio/wav",
        duration_ms=2000,
        sample_rate_hz=16000,
    )


@pytest.fixture
async def wired_engine(shared_db_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """把共享 SQLite 引擎外部安装到 engine holder（用后复位）。"""
    db_engine_holder.async_engine = shared_db_engine
    db_engine_holder.session_factory = async_sessionmaker(
        bind=shared_db_engine, expire_on_commit=False
    )
    db_engine_holder.owner_token = None
    try:
        yield shared_db_engine
    finally:
        db_engine_holder.reset()


async def _seed_usage(
    purpose: UsagePurpose, count: int, *, status: UsageStatus = UsageStatus.SUCCESS
) -> None:
    """灌入指定 purpose 的当日用量行。"""
    service = UsageService()
    for _ in range(count):
        await service.create(
            UsageCreateDTO(
                ts_ms=_DAY_START_MS + 1,
                persona_id=_PERSONA,
                model_entry="seed",
                purpose=purpose,
                status=status,
            )
        )


async def _usage_rows(purpose: UsagePurpose) -> list[UsageRecordRow]:
    """读取指定 purpose 的用量行。"""
    async with db_engine_holder.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.purpose == purpose.value)
                )
            )
            .scalars()
            .all()
        )


class TestBudgetConfig:
    async def test_env_keys_and_constructor_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """三组 env 键按 name 直接生效；构造器按字段名覆盖同样生效。"""
        monkeypatch.setenv("TTS_DAILY_BUDGET_GLOBAL", "3")
        monkeypatch.setenv("ASR_DAILY_BUDGET_PERSONA", "4")
        monkeypatch.setenv("VOICEPRINT_DAILY_BUDGET_GLOBAL", "5")
        assert TtsSettings().daily_budget_global == 3
        assert AsrSettings().daily_budget_persona == 4
        assert VoiceprintSettings().daily_budget_global == 5
        assert TtsSettings(daily_budget_persona=2).daily_budget_persona == 2

    async def test_upper_bound_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置硬上限：单源常量值通过，常量 + 1 在配置层拒绝。"""
        monkeypatch.setenv("TTS_DAILY_BUDGET_GLOBAL", str(DAILY_BUDGET_MAX))
        assert TtsSettings().daily_budget_global == DAILY_BUDGET_MAX
        monkeypatch.setenv("TTS_DAILY_BUDGET_GLOBAL", str(DAILY_BUDGET_MAX + 1))
        with pytest.raises(ValidationError):
            TtsSettings()
        monkeypatch.setenv("VOICEPRINT_DAILY_BUDGET_PERSONA", str(DAILY_BUDGET_MAX + 1))
        with pytest.raises(ValidationError):
            VoiceprintSettings()


class TestVoiceBudgetGate:
    async def test_tts_persona_exceeded_zero_outbound_zero_usage(self) -> None:
        """TTS persona 档超限：RATE_LIMITED + details，零出站零记账。"""
        client = FakeTtsClient()
        recorder = RecordingRecorder()
        counter = FakeBudgetCounter({("tts", _PERSONA): 2})
        service = VoiceService(
            tts_client=client,
            settings=_voice_settings(tts_persona=2),
            usage_recorder=recorder,
            usage_counter=counter,
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.synthesize("你好", persona=_persona_record())
        assert exc_info.value.code == ErrorCode.RATE_LIMITED
        assert exc_info.value.details == {
            "scope": "persona",
            "used": 2,
            "limit": 2,
            "purpose": "tts",
            "persona_id": _PERSONA,
        }
        assert client.calls == []
        assert recorder.events == []
        assert counter.calls[0][0] == "tts"

    async def test_asr_global_exceeded_without_persona_context(self) -> None:
        """ASR 无人格上下文：只查 global 档，超限即拒且零出站。"""
        client = FakeAsrClient()
        counter = FakeBudgetCounter({("asr", None): 3})
        service = VoiceService(
            asr_client=client,
            settings=_voice_settings(asr_global=3),
            usage_counter=counter,
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.transcribe(b"audio", "memo.wav")
        assert exc_info.value.details == {
            "scope": "global",
            "used": 3,
            "limit": 3,
            "purpose": "asr",
        }
        assert client.calls == []
        assert [call[1] for call in counter.calls] == [None]

    async def test_tts_global_and_asr_persona_exceeded(self) -> None:
        """TTS global 与 ASR persona 两档同语义拒绝（零出站）。"""
        tts_client = FakeTtsClient()
        tts_service = VoiceService(
            tts_client=tts_client,
            settings=_voice_settings(tts_global=2),
            usage_counter=FakeBudgetCounter({("tts", None): 2}),
        )
        with pytest.raises(BusinessException) as tts_error:
            await tts_service.synthesize("你好", persona=_persona_record())
        assert tts_error.value.details["scope"] == "global"
        assert tts_client.calls == []

        asr_client = FakeAsrClient()
        asr_service = VoiceService(
            asr_client=asr_client,
            settings=_voice_settings(asr_persona=2),
            usage_counter=FakeBudgetCounter({("asr", _PERSONA): 2}),
        )
        with pytest.raises(BusinessException) as asr_error:
            await asr_service.transcribe(b"audio", "memo.wav", persona=_persona_record())
        assert asr_error.value.details["scope"] == "persona"
        assert asr_error.value.details["purpose"] == "asr"
        assert asr_client.calls == []

    async def test_used_below_limit_allows_and_exact_limit_rejects(self) -> None:
        """used=limit-1 放行（调用后恰达上限）；used=limit 拒绝。"""
        allowed = FakeBudgetCounter({("tts", _PERSONA): 1})
        client = FakeTtsClient()
        recorder = RecordingRecorder()
        service = VoiceService(
            tts_client=client,
            settings=_voice_settings(tts_persona=2),
            usage_recorder=recorder,
            usage_counter=allowed,
        )
        result = await service.synthesize("你好", persona=_persona_record())
        assert result.audio == _FAKE_WAV
        assert client.calls == ["你好"]
        assert len(recorder.events) == 1

        rejected = FakeBudgetCounter({("tts", _PERSONA): 2})
        blocked = VoiceService(
            tts_client=FakeTtsClient(),
            settings=_voice_settings(tts_persona=2),
            usage_counter=rejected,
        )
        with pytest.raises(BusinessException) as exc_info:
            await blocked.synthesize("你好", persona=_persona_record())
        assert exc_info.value.code == ErrorCode.RATE_LIMITED

    async def test_zero_budgets_unlimited_without_counting(self) -> None:
        """两档 0 = 不限：零计数查询，调用照常。"""
        counter = FakeBudgetCounter({}, forbidden=True)
        service = VoiceService(
            tts_client=FakeTtsClient(),
            asr_client=FakeAsrClient(),
            settings=_voice_settings(),
            usage_counter=counter,
        )
        await service.synthesize("你好")
        await service.transcribe(b"audio", "memo.wav")
        assert counter.calls == []

    async def test_failure_is_not_recorded(self) -> None:
        """出站失败：异常上抛且不落用量行。"""
        recorder = RecordingRecorder()
        failure = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="down")
        service = VoiceService(
            tts_client=FakeTtsClient(error=failure),
            settings=_voice_settings(),
            usage_recorder=recorder,
        )
        with pytest.raises(BusinessException):
            await service.synthesize("你好")
        assert recorder.events == []

    async def test_purpose_filter_isolated_on_live_sqlite(self, wired_engine: AsyncEngine) -> None:
        """活 SQLite：各分面计数只含本 purpose；LLM 计数不含语音行。"""
        await _seed_usage(UsagePurpose.CHAT, 2)
        await _seed_usage(UsagePurpose.ASR, 3)
        await _seed_usage(UsagePurpose.ASR, 1, status=UsageStatus.ERROR)
        await _seed_usage(UsagePurpose.TTS, 4)
        await _seed_usage(UsagePurpose.VOICEPRINT, 5)
        counter = wire_default_voice_usage_counter()
        assert await counter("asr", _PERSONA, _DAY_START_MS) == 3
        assert await counter("tts", _PERSONA, _DAY_START_MS) == 4
        assert await counter("voiceprint", _PERSONA, _DAY_START_MS) == 5
        assert await wire_default_usage_counter()(_PERSONA, _DAY_START_MS) == 2

    async def test_success_metering_one_row_and_rejection_not_counted(
        self, wired_engine: AsyncEngine
    ) -> None:
        """成功调用恰好一行；随后被预算拒绝的调用零出站零新增行。"""
        client = FakeTtsClient()
        service = VoiceService(
            tts_client=client,
            settings=_voice_settings(tts_persona=1),
            usage_recorder=wire_default_voice_usage_recorder(),
            usage_counter=wire_default_voice_usage_counter(),
        )
        async with bind_persona_context(_PERSONA):
            await service.synthesize("第一句")
            with pytest.raises(BusinessException) as exc_info:
                await service.synthesize("第二句")
        assert exc_info.value.code == ErrorCode.RATE_LIMITED
        assert client.calls == ["第一句"]
        rows = await _usage_rows(UsagePurpose.TTS)
        assert len(rows) == 1
        assert rows[0].persona_id == _PERSONA
        assert rows[0].status == "success"
        assert rows[0].input_tokens == 0


class TestVoiceprintBudget:
    async def test_persona_exceeded_rejects_before_embed(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """声纹 persona 超限：零出站、无文件、无样本行。"""
        embedding = FakeEmbedding()
        counter = FakeBudgetCounter({("voiceprint", _PERSONA): 1})
        service = VoiceprintService(
            settings=_voiceprint_settings(daily_budget_persona=1),
            embedding=embedding,
            storage_root=tmp_path,
            usage_counter=counter,
        )
        async with bind_persona_context(_PERSONA):
            await _grant(service, db_session)
            with pytest.raises(BusinessException) as exc_info:
                await service.enroll(
                    db_session, "user-1", VoiceprintEnrollRequestDTO(samples=[_sample(b"a")])
                )
            assert await _sample_count(db_session) == 0
        assert exc_info.value.details["purpose"] == "voiceprint"
        assert exc_info.value.details["scope"] == "persona"
        assert embedding.calls == []
        assert list(tmp_path.rglob("*.wav")) == []

    async def test_global_exceeded_on_verify_zero_outbound(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """声纹校验超限：出站前拒绝且不新增向量调用。"""
        embedding = FakeEmbedding()
        setup = VoiceprintService(
            settings=_voiceprint_settings(), embedding=embedding, storage_root=tmp_path
        )
        async with bind_persona_context(_PERSONA):
            await _grant(setup, db_session)
            await setup.enroll(
                db_session, "user-1", VoiceprintEnrollRequestDTO(samples=[_sample(b"enroll")])
            )
            calls_after_enroll = len(embedding.calls)
            blocked = VoiceprintService(
                settings=_voiceprint_settings(daily_budget_global=1),
                embedding=embedding,
                storage_root=tmp_path,
                usage_counter=FakeBudgetCounter({("voiceprint", None): 1}),
            )
            with pytest.raises(BusinessException) as exc_info:
                await blocked.verify(
                    db_session, "user-1", VoiceprintVerifyRequestDTO(sample=_sample(b"query"))
                )
        assert exc_info.value.code == ErrorCode.RATE_LIMITED
        assert exc_info.value.details["scope"] == "global"
        assert len(embedding.calls) == calls_after_enroll

    async def test_zero_budgets_unlimited_without_counting(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """声纹两档 0 = 不限：零计数查询，注册照常成功。"""
        counter = FakeBudgetCounter({}, forbidden=True)
        service = VoiceprintService(
            settings=_voiceprint_settings(),
            embedding=FakeEmbedding(),
            storage_root=tmp_path,
            usage_counter=counter,
        )
        async with bind_persona_context(_PERSONA):
            await _grant(service, db_session)
            result = await service.enroll(
                db_session, "user-1", VoiceprintEnrollRequestDTO(samples=[_sample(b"a")])
            )
        assert result.accepted == 1
        assert counter.calls == []

    async def test_embedding_failure_not_recorded(
        self, wired_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """向量提供方失败：不落用量行且已写文件回滚。"""
        embedding = FakeEmbedding()
        embedding.error = BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="down")
        service = VoiceprintService(
            settings=_voiceprint_settings(),
            embedding=embedding,
            storage_root=tmp_path,
            usage_recorder=wire_default_voice_usage_recorder(),
        )
        async with bind_persona_context(_PERSONA):
            await _grant(service, db_session)
            with pytest.raises(BusinessException):
                await service.enroll(
                    db_session, "user-1", VoiceprintEnrollRequestDTO(samples=[_sample(b"a")])
                )
            assert await _sample_count(db_session) == 0
        assert list(tmp_path.rglob("*.wav")) == []
        assert await _usage_rows(UsagePurpose.VOICEPRINT) == []

    async def test_mid_batch_exhaustion_rolls_back_files_and_rows(
        self, wired_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """批量注册中途超限：整体回滚（文件 + 样本行），成功调用已记一行。"""
        embedding = FakeEmbedding()
        service = VoiceprintService(
            settings=_voiceprint_settings(min_samples=2, daily_budget_persona=1),
            embedding=embedding,
            storage_root=tmp_path,
            usage_recorder=wire_default_voice_usage_recorder(),
            usage_counter=wire_default_voice_usage_counter(),
        )
        async with bind_persona_context(_PERSONA):
            await _grant(service, db_session)
            with pytest.raises(BusinessException) as exc_info:
                await service.enroll(
                    db_session,
                    "user-1",
                    VoiceprintEnrollRequestDTO(samples=[_sample(b"one"), _sample(b"two")]),
                )
            assert await _sample_count(db_session) == 0
        assert exc_info.value.code == ErrorCode.RATE_LIMITED
        assert list(tmp_path.rglob("*.wav")) == []
        rows = await _usage_rows(UsagePurpose.VOICEPRINT)
        assert len(rows) == 1
        assert embedding.calls == ["voiceprint.wav"]

    async def test_success_records_one_row_per_embed(
        self, wired_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """注册 + 校验每次真实向量调用恰好一行（无重复记账）。"""
        embedding = FakeEmbedding()
        service = VoiceprintService(
            settings=_voiceprint_settings(min_samples=2),
            embedding=embedding,
            storage_root=tmp_path,
            usage_recorder=wire_default_voice_usage_recorder(),
        )
        async with bind_persona_context(_PERSONA):
            await _grant(service, db_session)
            await service.enroll(
                db_session,
                "user-1",
                VoiceprintEnrollRequestDTO(samples=[_sample(b"one"), _sample(b"two")]),
            )
            await service.verify(
                db_session, "user-1", VoiceprintVerifyRequestDTO(sample=_sample(b"query"))
            )
        assert len(embedding.calls) == 3
        rows = await _usage_rows(UsagePurpose.VOICEPRINT)
        assert len(rows) == 3
        assert {row.persona_id for row in rows} == {_PERSONA}


async def _grant(
    service: VoiceprintService, session: AsyncSession, sender: str = "user-1"
) -> None:
    """铺底一条有效同意。"""
    await service.grant_consent(
        session,
        sender,
        VoiceprintConsentGrantDTO(consent_text_version=VOICEPRINT_CONSENT_TEXT_VERSION),
    )


async def _sample_count(session: AsyncSession) -> int:
    """统计当前会话可见的声纹样本行数。"""
    rows = (await session.execute(select(VoiceprintSampleRow))).scalars().all()
    return len(rows)


def _persona_record() -> PersonaRecordDTO:
    """构造最小人格记录（预算 persona 轴与 IPC 上下文一致）。"""
    return PersonaRecordDTO(
        id=int(_PERSONA),
        name="feiyu",
        display_name="大肥鱼",
        status=PersonaStatus.ACTIVE,
        is_primary=True,
        card=CharaCardModel(name="大肥鱼", first_mes="你回来啦！"),
        created_at=1,
        updated_at=1,
    )
