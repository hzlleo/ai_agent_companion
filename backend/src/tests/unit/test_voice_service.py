"""voice 域服务测试——VoiceService 编排面（注入式 fake 客户端 + 记录器）。

覆盖矩阵：

    - 参数解析优先级：请求参数 > persona.voice_profile > settings 缺省
      （speaker / speed / instruct_text 透传）；
    - enabled 门控：TTS / ASR 未启用 → CONFLICT（错误信息含 env 旗标名）；
    - 用量发射：成功 / 失败行（purpose / model_entry / persona 绑定 /
      真实时延 / status / error_code）；记录器异常不阻断主调用；
    - 客户端惰性构建：注入优先；缺省经公共池（未启动 → INTERNAL_ERROR；
      启动后经共享 client 瞬时构建——MockTransport 实证）；
    - 默认接线：wire_default_voice_usage_recorder → usage 域真实落库
      （共享引擎 + engine holder 外部安装）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.engine import db_engine_holder
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.models import PersonaStatus
from packages.harness.usage.orm import UsageRecordRow
from packages.harness.voice import voice_service as voice_service_module
from packages.harness.voice.tts_client import CosyVoiceTtsClient
from packages.harness.voice.voice_models import (
    AsrResultModel,
    TtsResultModel,
    VoiceProfileModel,
    VoiceUsageEvent,
)
from packages.harness.voice.voice_service import (
    VoiceService,
    wire_default_voice_usage_recorder,
)

#: 测试用 WAV 载荷（内容任意——服务层透传不解析）。
_FAKE_WAV: bytes = b"RIFF-fake-wav"


class FakeTtsClient:
    """假 TTS 客户端（记录调用；可注入故障）。"""

    def __init__(
        self,
        *,
        audio: bytes = _FAKE_WAV,
        latency_ms: int = 7,
        error: BusinessException | None = None,
    ) -> None:
        """初始化假客户端。

        Args:
            audio: 合成产物字节。
            latency_ms: 合成时延。
            error: 故障注入（调用即抛）。
        """
        self.audio = audio
        self.latency_ms = latency_ms
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> TtsResultModel:
        """假合成（记录参数 + 返回固定产物 / 注入故障）。"""
        self.calls.append(
            {
                "text": text,
                "speaker": speaker,
                "speed": speed,
                "instruct_text": instruct_text,
            }
        )
        if self.error is not None:
            raise self.error
        return TtsResultModel(audio=self.audio, latency_ms=self.latency_ms)


class FakeAsrClient:
    """假 ASR 客户端（记录调用；可注入故障）。"""

    def __init__(
        self,
        *,
        error: BusinessException | None = None,
    ) -> None:
        """初始化假客户端。

        Args:
            error: 故障注入（调用即抛）。
        """
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def transcribe(
        self, audio: bytes, filename: str, *, language: str | None = None
    ) -> AsrResultModel:
        """假转写（记录参数 + 固定产物 / 注入故障）。"""
        self.calls.append({"audio": audio, "filename": filename, "language": language})
        if self.error is not None:
            raise self.error
        return AsrResultModel(text="你好世界", duration_ms=3400, language="zh")


class RecordingRecorder:
    """记录式用量记录器（收集事件——断言面）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.events: list[VoiceUsageEvent] = []

    async def __call__(self, event: VoiceUsageEvent) -> None:
        """记录事件。"""
        self.events.append(event)


class FailingRecorder:
    """故障记录器（发射即抛——观测面异常不阻断主调用断言面）。"""

    def __init__(self) -> None:
        """初始化故障记录器。"""
        self.calls = 0

    async def __call__(self, event: VoiceUsageEvent) -> None:
        """抛出异常（模拟 usage 域不可用）。"""
        self.calls += 1
        raise RuntimeError("usage sink down")


def _settings(*, tts_enabled: bool = True, asr_enabled: bool = True):
    """语音启用形态的隔离 Settings（其余分节代码默认）。

    Args:
        tts_enabled: TTS 分节开关。
        asr_enabled: ASR 分节开关。

    Returns:
        隔离 :class:`Settings` 实例。
    """
    return build_isolated_settings(
        voice=VoiceSettings(
            tts=TtsSettings(
                enabled=tts_enabled,
                base_url="http://tts.test",
                model_entry="cosyvoice3",
                default_speaker="中文女",
                default_speed=1.0,
            ),
            asr=AsrSettings(
                enabled=asr_enabled,
                base_url="http://asr.test",
                model_entry="qwen3-asr-0.6b",
                language="zh",
            ),
        )
    )


def _persona(profile: VoiceProfileModel | None = None) -> PersonaRecordDTO:
    """构造人格记录 DTO（可选语音绑定）。

    Args:
        profile: 语音绑定（None = 未绑定）。

    Returns:
        :class:`PersonaRecordDTO` 实例。
    """
    return PersonaRecordDTO(
        id=42,
        name="feiyu",
        display_name="大肥鱼",
        status=PersonaStatus.ACTIVE,
        is_primary=True,
        card=CharaCardModel(name="大肥鱼", first_mes="你回来啦！"),
        voice_profile=profile,
        created_at=1,
        updated_at=1,
    )


class TestSynthesizeResolution:
    async def test_settings_defaults_when_no_persona(self) -> None:
        """无人格绑定：settings 缺省 speaker / speed（无 instruct）。"""
        client = FakeTtsClient()
        service = VoiceService(
            tts_client=client, settings=_settings(), usage_recorder=None
        )
        await service.synthesize("你好")
        assert client.calls == [
            {
                "text": "你好",
                "speaker": "中文女",
                "speed": 1.0,
                "instruct_text": None,
            }
        ]

    async def test_persona_profile_overrides_settings(self) -> None:
        """人格绑定覆盖 settings 缺省（speaker / speed / instruct 透传）。"""
        client = FakeTtsClient()
        service = VoiceService(
            tts_client=client, settings=_settings(), usage_recorder=None
        )
        profile = VoiceProfileModel(
            speaker="克隆音色B", speed=1.5, instruct_text="轻快地说"
        )
        await service.synthesize("你好", persona=_persona(profile))
        assert client.calls[0]["speaker"] == "克隆音色B"
        assert client.calls[0]["speed"] == 1.5
        assert client.calls[0]["instruct_text"] == "轻快地说"

    async def test_explicit_params_override_persona_profile(self) -> None:
        """请求级显式参数覆盖人格绑定（优先级：请求 > 人格 > settings）。"""
        client = FakeTtsClient()
        service = VoiceService(
            tts_client=client, settings=_settings(), usage_recorder=None
        )
        profile = VoiceProfileModel(speaker="克隆音色B", speed=1.5)
        await service.synthesize(
            "你好", persona=_persona(profile), speaker="英文女", speed=0.75
        )
        assert client.calls[0]["speaker"] == "英文女"
        assert client.calls[0]["speed"] == 0.75

    async def test_disabled_tts_maps_conflict(self) -> None:
        """TTS 未启用：CONFLICT（信息含 VOICE_TTS_ENABLED 旗标名）。"""
        client = FakeTtsClient()
        service = VoiceService(
            tts_client=client, settings=_settings(tts_enabled=False), usage_recorder=None
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.synthesize("你好")
        assert exc_info.value.code == ErrorCode.CONFLICT
        assert "VOICE_TTS_ENABLED" in exc_info.value.message
        assert client.calls == []


class TestSynthesizeUsage:
    async def test_success_row_emitted(self) -> None:
        """成功行：purpose=tts / model_entry / persona 绑定 / 真实时延。"""
        recorder = RecordingRecorder()
        service = VoiceService(
            tts_client=FakeTtsClient(),
            settings=_settings(),
            usage_recorder=recorder,
        )
        result = await service.synthesize("你好", persona=_persona())
        assert result.audio == _FAKE_WAV
        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.purpose == "tts"
        assert event.model_entry == "cosyvoice3"
        assert event.provider == "cosyvoice"
        assert event.persona_id == "42"
        assert event.status == "success"
        assert event.error_code is None
        assert event.latency_ms >= 0

    async def test_error_row_emitted_and_raised(self) -> None:
        """失败行：status=error / error_code 携带；异常原样上抛。"""
        recorder = RecordingRecorder()
        failure = BusinessException(
            code=ErrorCode.LLM_CALL_FAILED, message="语音合成失败"
        )
        service = VoiceService(
            tts_client=FakeTtsClient(error=failure),
            settings=_settings(),
            usage_recorder=recorder,
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.synthesize("你好", persona=_persona())
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.status == "error"
        assert event.error_code == str(ErrorCode.LLM_CALL_FAILED)
        assert event.latency_ms >= 0

    async def test_recorder_failure_never_breaks_call(self) -> None:
        """记录器异常：仅告警——合成结果照常返回（观测面不阻断）。"""
        recorder = FailingRecorder()
        service = VoiceService(
            tts_client=FakeTtsClient(),
            settings=_settings(),
            usage_recorder=recorder,
        )
        result = await service.synthesize("你好")
        assert result.audio == _FAKE_WAV
        assert recorder.calls == 1

    async def test_no_recorder_is_noop(self) -> None:
        """未接线记录器：零记录（no-op——调用照常）。"""
        service = VoiceService(
            tts_client=FakeTtsClient(), settings=_settings(), usage_recorder=None
        )
        result = await service.synthesize("你好")
        assert result.audio == _FAKE_WAV


class TestTranscribe:
    async def test_language_resolution_settings_default(self) -> None:
        """语言解析：缺省 settings.voice.asr.language。"""
        client = FakeAsrClient()
        service = VoiceService(
            asr_client=client, settings=_settings(), usage_recorder=None
        )
        await service.transcribe(b"audio", "memo.wav")
        assert client.calls[0]["language"] == "zh"

    async def test_language_explicit_override(self) -> None:
        """语言解析：请求参数覆盖 settings 缺省。"""
        client = FakeAsrClient()
        service = VoiceService(
            asr_client=client, settings=_settings(), usage_recorder=None
        )
        await service.transcribe(b"audio", "memo.wav", language="en")
        assert client.calls[0]["language"] == "en"

    async def test_disabled_asr_maps_conflict(self) -> None:
        """ASR 未启用：CONFLICT（信息含 VOICE_ASR_ENABLED 旗标名）。"""
        client = FakeAsrClient()
        service = VoiceService(
            asr_client=client, settings=_settings(asr_enabled=False), usage_recorder=None
        )
        with pytest.raises(BusinessException) as exc_info:
            await service.transcribe(b"audio", "memo.wav")
        assert exc_info.value.code == ErrorCode.CONFLICT
        assert "VOICE_ASR_ENABLED" in exc_info.value.message

    async def test_asr_usage_and_persona_binding(self) -> None:
        """ASR 用量行：purpose=asr / provider / persona 绑定。"""
        recorder = RecordingRecorder()
        service = VoiceService(
            asr_client=FakeAsrClient(), settings=_settings(), usage_recorder=recorder
        )
        result = await service.transcribe(b"audio", "memo.wav", persona=_persona())
        assert result.text == "你好世界"
        event = recorder.events[0]
        assert event.purpose == "asr"
        assert event.model_entry == "qwen3-asr-0.6b"
        assert event.provider == "qwen3-asr"
        assert event.persona_id == "42"
        assert event.status == "success"


class TestLazyClientBuild:
    async def test_pool_missing_maps_internal_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """客户端未注入且公共池未启动：INTERNAL_ERROR（调用顺序契约面）。"""
        monkeypatch.setattr(voice_service_module, "get_pool_manager", lambda: None)
        service = VoiceService(settings=_settings(), usage_recorder=None)
        with pytest.raises(BusinessException) as exc_info:
            await service.synthesize("你好")
        assert exc_info.value.code == ErrorCode.INTERNAL_ERROR

    async def test_lazy_build_from_pool_shared_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺省构建：经公共池共享 client 瞬时构建真实客户端（MockTransport 实证）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "http://tts.test/tts"
            return httpx.Response(
                200, content=_FAKE_WAV, headers={"content-type": "audio/wav"}
            )

        mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        class FakePoolManager:
            """假公共池管理器（started + 共享 client 下发面）。"""

            started = True

            def get_http_client(self) -> httpx.AsyncClient:
                """下发 MockTransport 共享 client。"""
                return mock_http

        monkeypatch.setattr(
            voice_service_module, "get_pool_manager", lambda: FakePoolManager()
        )
        recorder = RecordingRecorder()
        service = VoiceService(settings=_settings(), usage_recorder=recorder)
        result = await service.synthesize("你好")
        assert result.audio == _FAKE_WAV
        assert recorder.events[0].purpose == "tts"
        await mock_http.aclose()

    async def test_injected_client_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """注入客户端优先（池不可用也不触达——注入面短路）。"""
        monkeypatch.setattr(voice_service_module, "get_pool_manager", lambda: None)
        client = FakeTtsClient()
        service = VoiceService(tts_client=client, settings=_settings())
        result = await service.synthesize("你好")
        assert result.audio == _FAKE_WAV
        assert len(client.calls) == 1

    async def test_lazy_build_constructs_real_client_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """惰性构建产物类型：真实 CosyVoiceTtsClient（wire 参数来自 settings）。"""
        mock_http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=_FAKE_WAV, headers={"content-type": "audio/wav"}
                )
            )
        )

        class FakePoolManager:
            """假公共池管理器。"""

            started = True

            def get_http_client(self) -> httpx.AsyncClient:
                """下发 MockTransport 共享 client。"""
                return mock_http

        monkeypatch.setattr(
            voice_service_module, "get_pool_manager", lambda: FakePoolManager()
        )
        service = VoiceService(settings=_settings())
        resolved = service._resolve_tts_client()
        assert isinstance(resolved, CosyVoiceTtsClient)
        await mock_http.aclose()


class TestDefaultUsageWiring:
    async def test_wire_default_recorder_persists_usage_row(
        self, shared_db_engine: AsyncEngine
    ) -> None:
        """默认接线：VoiceUsageEvent → ff_usage_records 真实落库（token 0/0/0）。"""
        db_engine_holder.async_engine = shared_db_engine
        db_engine_holder.session_factory = async_sessionmaker(
            bind=shared_db_engine, expire_on_commit=False
        )
        db_engine_holder.owner_token = None
        try:
            recorder = wire_default_voice_usage_recorder()
            await recorder(
                VoiceUsageEvent(
                    ts_ms=1_700_000_000_000,
                    persona_id="42",
                    purpose="tts",
                    model_entry="cosyvoice3",
                    provider="cosyvoice",
                    latency_ms=123,
                    status="success",
                    request_id=None,
                    error_code=None,
                )
            )
            async with db_engine_holder.session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(UsageRecordRow).where(
                                UsageRecordRow.purpose == "tts"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(rows) == 1
            row = rows[0]
            assert row.model_entry == "cosyvoice3"
            assert row.provider == "cosyvoice"
            assert row.persona_id == "42"
            assert row.input_tokens == 0
            assert row.output_tokens == 0
            assert row.total_tokens == 0
            assert row.latency_ms == 123
            assert row.status == "success"
        finally:
            db_engine_holder.reset()
