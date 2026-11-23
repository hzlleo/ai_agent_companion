"""VoiceService 流式通道测试——双轨门控 / 预算闸 / 计量口径。

覆盖矩阵（对齐 R4-2 分面语义，不改变既有批量语义）：

    - 可用性：开关 × 客户端注入双门控；
    - ASR 流式：被预算拒绝零建连零出站零落行；开启成功计 1 次、收尾
      （finish / cancel）各恰 1 行；开启失败不落行；cancel 幂等不重复记账；
    - TTS 流式：每句成功合成恰 1 行；被拒零建连；消费侧取消不落行；
    - 参数解析：人格绑定优先于 settings（流式与批量同源）。

全部用例经注入 fake 客户端 / 计数面 / 记录器驱动（零网络、零真实等待）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.models import PersonaStatus
from packages.harness.voice.voice_models import VoiceProfileModel, VoiceUsageEvent
from packages.harness.voice.voice_service import VoiceService
from tests.realtime_fakes import FakeAsrStreamHandle


class _FakeStreamAsrClient:
    """流式识别客户端 fake（记录握手参数；可注入失败）。"""

    def __init__(self) -> None:
        self.handle = FakeAsrStreamHandle()
        self.error: BusinessException | None = None
        self.calls: list[dict[str, Any]] = []
        self.partial_callback: Any = None

    async def open(
        self,
        *,
        sample_rate_hz: int,
        language: str | None,
        on_partial: Any,
    ) -> FakeAsrStreamHandle:
        """假握手（记录参数 + 按脚本抛错 / 返回句柄）。"""
        self.calls.append({"sample_rate_hz": sample_rate_hz, "language": language})
        self.partial_callback = on_partial
        if self.error is not None:
            raise self.error
        return self.handle


class _FakeStreamTtsClient:
    """流式合成客户端 fake（逐分片产出；可注入失败 / 门闩）。"""

    sample_rate_hz = 24000

    def __init__(self) -> None:
        self.fragments: list[bytes] = [b"\x01\x00" * 8]
        self.error: BusinessException | None = None
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    def synthesize_stream(
        self,
        text: str,
        *,
        speaker: str | None = None,
        speed: float = 1.0,
        instruct_text: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """假流式合成（记录参数；逐分片产出或抛错）。"""
        self.calls.append(
            {
                "text": text,
                "speaker": speaker,
                "speed": speed,
                "instruct_text": instruct_text,
            }
        )

        async def _generate() -> AsyncGenerator[bytes, None]:
            try:
                if self.error is not None:
                    raise self.error
                for fragment in self.fragments:
                    yield fragment
            finally:
                self.closed += 1

        return _generate()


class _UsageRecorder:
    """用量事件记录器（内存收集——断言行数与字段）。"""

    def __init__(self) -> None:
        self.events: list[VoiceUsageEvent] = []

    async def __call__(self, event: VoiceUsageEvent) -> None:
        """收集一条用量事件。"""
        self.events.append(event)


class _BudgetCounter:
    """脚本化预算计数面（记录查询轴——断言 0/0 零查询）。"""

    def __init__(self, *, persona_used: int = 0, global_used: int = 0) -> None:
        self.persona_used = persona_used
        self.global_used = global_used
        self.calls: list[tuple[str, str | None, int]] = []

    async def __call__(self, purpose: str, persona_id: str | None, day_start: int) -> int:
        """按轴返回当日已用调用数。"""
        self.calls.append((purpose, persona_id, day_start))
        return self.persona_used if persona_id is not None else self.global_used


def _settings(
    *,
    asr_streaming: bool = True,
    tts_streaming: bool = True,
    tts_persona_budget: int = 0,
    asr_persona_budget: int = 0,
) -> Any:
    """构造流式启用形态的隔离 Settings（批量分面同开）。"""
    return build_isolated_settings(
        voice=VoiceSettings(
            tts=TtsSettings(
                enabled=True,
                model_entry="cosyvoice3",
                default_speaker="中文女",
                default_speed=1.0,
                streaming_enabled=tts_streaming,
                streaming_sample_rate_hz=24000,
                daily_budget_persona=tts_persona_budget,
            ),
            asr=AsrSettings(
                enabled=True,
                model_entry="qwen3-asr-0.6b",
                language="zh",
                streaming_enabled=asr_streaming,
                daily_budget_persona=asr_persona_budget,
            ),
        )
    )


def _persona(profile: VoiceProfileModel | None = None) -> PersonaRecordDTO:
    """构造人格记录 DTO（可选语音绑定）。"""
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


async def _no_partial(text: str) -> None:
    """进行中回调占位（服务层原样透传）。"""
    return None


async def test_availability_requires_flag_and_client() -> None:
    """可用性：开关关 / 客户端缺失任一为假即不可用。"""
    no_clients = VoiceService(settings=_settings())
    assert no_clients.streaming_asr_available is False
    assert no_clients.streaming_tts_available is False
    with_clients_off = VoiceService(
        settings=_settings(asr_streaming=False, tts_streaming=False),
        asr_stream_client=_FakeStreamAsrClient(),
        tts_stream_client=_FakeStreamTtsClient(),
    )
    assert with_clients_off.streaming_asr_available is False
    assert with_clients_off.streaming_tts_available is False
    ready = VoiceService(
        settings=_settings(),
        asr_stream_client=_FakeStreamAsrClient(),
        tts_stream_client=_FakeStreamTtsClient(),
    )
    assert ready.streaming_asr_available is True
    assert ready.streaming_tts_available is True
    assert ready.streaming_tts_sample_rate_hz == 24000


async def test_asr_stream_open_without_client_conflicts_zero_connect() -> None:
    """未注入客户端：CONFLICT（流式通道未接通）。"""
    service = VoiceService(settings=_settings())
    with pytest.raises(BusinessException) as excinfo:
        await service.open_asr_stream(
            sample_rate_hz=16000, on_partial=_no_partial
        )
    assert excinfo.value.code == ErrorCode.CONFLICT


async def test_asr_stream_budget_rejected_zero_connect_zero_usage() -> None:
    """预算拒绝：RATE_LIMITED、零握手、零用量行。"""
    client = _FakeStreamAsrClient()
    recorder = _UsageRecorder()
    counter = _BudgetCounter(persona_used=1)
    service = VoiceService(
        settings=_settings(asr_persona_budget=1),
        asr_stream_client=client,
        usage_recorder=recorder,
        usage_counter=counter,
    )
    with pytest.raises(BusinessException) as excinfo:
        await service.open_asr_stream(
            sample_rate_hz=16000, on_partial=_no_partial, persona=_persona()
        )
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert client.calls == []
    assert recorder.events == []


async def test_asr_stream_finish_records_exactly_one_usage_row() -> None:
    """开启成功 + finish 落定：恰 1 行成功用量（purpose asr / 人格绑定）。"""
    client = _FakeStreamAsrClient()
    recorder = _UsageRecorder()
    counter = _BudgetCounter()
    service = VoiceService(
        settings=_settings(asr_persona_budget=5),
        asr_stream_client=client,
        usage_recorder=recorder,
        usage_counter=counter,
    )
    handle = await service.open_asr_stream(
        sample_rate_hz=16000, on_partial=_no_partial, persona=_persona()
    )
    await handle.feed(b"\x00\x00")
    assert client.handle.fed == [b"\x00\x00"]
    result = await handle.finish()
    assert result.text == "流式转写"
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.purpose == "asr"
    assert event.provider == "qwen3-asr"
    assert event.model_entry == "qwen3-asr-0.6b"
    assert event.persona_id == "42"
    assert event.status == "success"
    assert counter.calls
    assert counter.calls[0] == ("asr", "42", counter.calls[0][2])


async def test_asr_stream_cancel_records_one_row_and_is_idempotent() -> None:
    """取消（barge-in / 关闭）：连接已开启 → 落 1 行；重复取消不重复记账。"""
    client = _FakeStreamAsrClient()
    recorder = _UsageRecorder()
    service = VoiceService(
        settings=_settings(), asr_stream_client=client, usage_recorder=recorder
    )
    handle = await service.open_asr_stream(
        sample_rate_hz=16000, on_partial=_no_partial
    )
    await handle.cancel()
    await handle.cancel()
    assert client.handle.cancel_count == 2
    assert len(recorder.events) == 1


async def test_asr_stream_open_failure_records_no_usage_row() -> None:
    """握手失败：业务异常上抛且零用量行（调用方回退批量）。"""
    client = _FakeStreamAsrClient()
    client.error = BusinessException(
        code=ErrorCode.LLM_CALL_FAILED, message="握手失败"
    )
    recorder = _UsageRecorder()
    service = VoiceService(
        settings=_settings(), asr_stream_client=client, usage_recorder=recorder
    )
    with pytest.raises(BusinessException):
        await service.open_asr_stream(
            sample_rate_hz=16000, on_partial=_no_partial
        )
    assert recorder.events == []


async def test_tts_stream_yields_fragments_and_records_one_row() -> None:
    """每句成功流式合成：分片原样产出 + 恰 1 行 usage（purpose tts）。"""
    client = _FakeStreamTtsClient()
    recorder = _UsageRecorder()
    service = VoiceService(
        settings=_settings(), tts_stream_client=client, usage_recorder=recorder
    )
    fragments = [
        fragment async for fragment in service.synthesize_stream("你好")
    ]
    assert b"".join(fragments) == b"\x01\x00" * 8
    assert client.calls[0]["speaker"] == "中文女"
    assert client.calls[0]["speed"] == 1.0
    assert len(recorder.events) == 1
    assert recorder.events[0].purpose == "tts"
    assert recorder.events[0].provider == "cosyvoice"


async def test_tts_stream_empty_audio_records_no_usage_row() -> None:
    """空音频（零非空分片）：不落流式 usage 行（回退批量由批量路径记账）。"""
    client = _FakeStreamTtsClient()
    client.fragments = []
    recorder = _UsageRecorder()
    service = VoiceService(
        settings=_settings(), tts_stream_client=client, usage_recorder=recorder
    )
    fragments = [fragment async for fragment in service.synthesize_stream("你好")]
    assert fragments == []
    assert client.closed == 1
    assert recorder.events == []


async def test_tts_stream_persona_profile_overrides_settings() -> None:
    """人格绑定优先于 settings 缺省（流式与批量同源解析）。"""
    client = _FakeStreamTtsClient()
    service = VoiceService(settings=_settings(), tts_stream_client=client)
    persona = _persona(VoiceProfileModel(speaker="克隆音色", speed=1.3, instruct_text="低语"))
    async for _fragment in service.synthesize_stream("你好", persona=persona):
        continue
    assert client.calls[0] == {
        "text": "你好",
        "speaker": "克隆音色",
        "speed": 1.3,
        "instruct_text": "低语",
    }


async def test_tts_stream_budget_rejected_zero_connect_zero_usage() -> None:
    """TTS 预算拒绝：RATE_LIMITED、零建连、零用量行。"""
    client = _FakeStreamTtsClient()
    recorder = _UsageRecorder()
    counter = _BudgetCounter(persona_used=2)
    service = VoiceService(
        settings=_settings(tts_persona_budget=2),
        tts_stream_client=client,
        usage_recorder=recorder,
        usage_counter=counter,
    )
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in service.synthesize_stream("你好", persona=_persona()):
            continue
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert client.calls == []
    assert recorder.events == []


async def test_tts_stream_consumer_cancel_records_no_usage_row() -> None:
    """消费侧取消（barge-in）：不落行（与批量句级取消等价）。"""
    client = _FakeStreamTtsClient()
    client.fragments = [b"\x01\x00", b"\x02\x00"]
    recorder = _UsageRecorder()
    service = VoiceService(
        settings=_settings(), tts_stream_client=client, usage_recorder=recorder
    )
    stream = service.synthesize_stream("你好")
    await stream.__anext__()
    await stream.aclose()
    assert client.closed == 1
    assert recorder.events == []


async def test_tts_stream_without_client_conflicts() -> None:
    """未注入流式客户端：CONFLICT（会话层据可用性回退批量，不会触发）。"""
    service = VoiceService(settings=_settings())
    with pytest.raises(BusinessException) as excinfo:
        async for _fragment in service.synthesize_stream("你好"):
            continue
    assert excinfo.value.code == ErrorCode.CONFLICT
