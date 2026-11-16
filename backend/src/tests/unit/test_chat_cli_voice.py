"""harness/chat cli --voice 旗标测试——语音回复面（注入式 fake，零真实网络）。

测试策略：隔离 settings（voice.tts.enabled 注入）+ tmp SQLite + 假网关 /
假语音服务 / 记录式假播放器（never real stdin / never real player / never
real TTS 服务）。

覆盖面：旗标缺省 = 零合成；旗标生效 = 人格绑定合成 + 播放收到音频字节；
合成失败 → 提示上屏 REPL 存活；TTS 未启用 → 提示 + 零合成；main 入口
--voice 旗标透传。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from packages.harness.chat import cli
from packages.harness.config.models import DatabaseConfig, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.base import Base
from packages.harness.llm.llm_models import ChatStreamChunkModel
from packages.harness.persona.dtos import PersonaCreateDTO
from packages.harness.persona.service import PersonaService
from packages.harness.voice.voice_models import TtsResultModel, VoiceProfileModel

#: 假合成产物（字节面——播放器收到的音频断言值）。
_FAKE_WAV: bytes = b"RIFF-cli-fake-wav"


class FakeGateway:
    """假 LLM 网关（流式面——逐块产出固定增量）。"""

    def __init__(self, stream_parts: Sequence[str] = ("早", "安")) -> None:
        """初始化假网关。

        Args:
            stream_parts: 流式增量块序列。
        """
        self.stream_parts = list(stream_parts)

    async def stream_by_role(
        self, role: Any, messages: Any, params: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（逐块产出）。"""
        for part in self.stream_parts:
            yield ChatStreamChunkModel(content=part)


class FakeVoiceService:
    """假语音服务（记录合成调用；可注入故障）。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化假服务。

        Args:
            error: 合成故障注入。
        """
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def synthesize(
        self,
        text: str,
        *,
        persona: Any = None,
        speaker: str | None = None,
        speed: float | None = None,
        request_id: str | None = None,
    ) -> TtsResultModel:
        """假合成（记录文本与绑定人格；返回固定产物 / 注入故障）。"""
        self.calls.append({"text": text, "persona": persona})
        if self.error is not None:
            raise self.error
        return TtsResultModel(audio=_FAKE_WAV, latency_ms=3)


class RecordingPlayer:
    """记录式假播放器（收集音频字节——恒报播放成功）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.received: list[bytes] = []

    async def __call__(self, audio: bytes) -> bool:
        """记录音频字节。"""
        self.received.append(audio)
        return True


class SequenceInput:
    """序列假输入（逐行返回；耗尽后以 EOFError 表达结束）。"""

    def __init__(self, lines: Sequence[str]) -> None:
        """初始化假输入。

        Args:
            lines: 逐行返回的输入序列。
        """
        self._lines = iter(lines)

    def __call__(self, prompt: str) -> str:
        """返回下一行（耗尽即 EOFError）。"""
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise EOFError from exc


class RecordingOutput:
    """记录假输出（追加式——断言面）。"""

    def __init__(self) -> None:
        """初始化记录器。"""
        self.text = ""

    def __call__(self, text: str) -> None:
        """记录输出文本。"""
        self.text += text


async def _seed_active_persona(
    db_path: Path, profile: VoiceProfileModel | None = None
) -> int:
    """在 tmp SQLite 文件中预置一个激活人格（可选语音绑定）。

    Args:
        db_path: SQLite 库文件路径。
        profile: 语音绑定（None = 不绑定）。

    Returns:
        已激活人格的主键。
    """
    engine: AsyncEngine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        service = PersonaService()
        payload: dict[str, Any] = {
            "name": "voice-cli-feiyu",
            "status": "active",
            "card": {
                "name": "大肥鱼",
                "description": "圆滚滚的橘色胖鱼",
                "personality": "元气",
                "first_mes": "你回来啦！",
                "post_history_instructions": "回复保持简短。",
            },
        }
        if profile is not None:
            payload["voice_profile"] = profile.model_dump()
        created = await service.create(session, PersonaCreateDTO.model_validate(payload))
        await service.activate(session, created.id)
        persona_id = created.id
    await engine.dispose()
    return persona_id


def _sqlite_config(db_path: Path) -> DatabaseConfig:
    """tmp SQLite 数据库配置（显式注入——隔离真实配置）。"""
    return DatabaseConfig(type="sqlite", name=str(db_path))


def _voice_enabled_settings() -> Any:
    """voice.tts.enabled 形态隔离 Settings（其余代码默认）。"""
    return build_isolated_settings(
        voice=VoiceSettings(tts=TtsSettings(enabled=True))
    )


async def test_cli_voice_off_zero_synthesis(tmp_path: Path) -> None:
    """旗标缺省：零合成调用（注入的语音服务不被消费——opt-in 契约）。"""
    db_path = tmp_path / "voice-off.db"
    await _seed_active_persona(db_path)
    fake_service = FakeVoiceService()
    player = RecordingPlayer()
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),
        gateway=FakeGateway(),
        input_fn=SequenceInput(["在吗", "/quit"]),
        output_fn=recorder,
        voice_service=fake_service,
        play_audio_fn=player,
    )
    assert exit_code == 0
    assert fake_service.calls == []
    assert player.received == []


async def test_cli_voice_on_synthesizes_and_plays(tmp_path: Path) -> None:
    """旗标生效：流式回复聚合全文 → 人格绑定合成 → 播放收到音频字节。"""
    db_path = tmp_path / "voice-on.db"
    profile = VoiceProfileModel(speaker="克隆音色A", speed=1.25)
    persona_id = await _seed_active_persona(db_path, profile)
    fake_service = FakeVoiceService()
    player = RecordingPlayer()
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        settings=_voice_enabled_settings(),
        database_config=_sqlite_config(db_path),
        gateway=FakeGateway(stream_parts=("早", "安")),
        input_fn=SequenceInput(["在吗", "/quit"]),
        output_fn=recorder,
        voice=True,
        voice_service=fake_service,
        play_audio_fn=player,
    )
    assert exit_code == 0
    assert len(fake_service.calls) == 1
    call = fake_service.calls[0]
    assert call["text"] == "早安"  # 流式增量聚合为完整回复文本
    bound = call["persona"]
    assert bound is not None
    assert bound.id == persona_id
    assert bound.voice_profile is not None
    assert bound.voice_profile.speaker == "克隆音色A"
    assert player.received == [_FAKE_WAV]
    assert "再见。" in recorder.text


async def test_cli_voice_synthesis_failure_keeps_repl_alive(tmp_path: Path) -> None:
    """合成失败：[语音合成失败] 提示上屏，REPL 不崩（/quit 正常退出）。"""
    db_path = tmp_path / "voice-err.db"
    await _seed_active_persona(db_path)
    failure = BusinessException(code=ErrorCode.CONFLICT, message="语音合成未启用")
    fake_service = FakeVoiceService(error=failure)
    player = RecordingPlayer()
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        settings=_voice_enabled_settings(),
        database_config=_sqlite_config(db_path),
        gateway=FakeGateway(),
        input_fn=SequenceInput(["在吗", "/quit"]),
        output_fn=recorder,
        voice=True,
        voice_service=fake_service,
        play_audio_fn=player,
    )
    assert exit_code == 0
    assert "[语音合成失败] 语音合成未启用" in recorder.text
    assert player.received == []
    assert "再见。" in recorder.text


async def test_cli_voice_tts_disabled_hint_and_zero_synthesis(tmp_path: Path) -> None:
    """--voice 但 TTS 未启用：中文提示 + 纯文本继续 + 零合成。"""
    db_path = tmp_path / "voice-disabled.db"
    await _seed_active_persona(db_path)
    fake_service = FakeVoiceService()
    player = RecordingPlayer()
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        database_config=_sqlite_config(db_path),  # settings 缺省（TTS 关闭）
        gateway=FakeGateway(),
        input_fn=SequenceInput(["在吗", "/quit"]),
        output_fn=recorder,
        voice=True,
        voice_service=fake_service,
        play_audio_fn=player,
    )
    assert exit_code == 0
    assert "VOICE_TTS_ENABLED" in recorder.text
    assert "[语音] TTS 未启用" in recorder.text
    assert fake_service.calls == []
    assert player.received == []


async def test_cli_voice_assembles_service_when_not_injected(tmp_path: Path) -> None:
    """voice_service 未注入 + TTS 启用：现场装配真实服务（零合成调用——
    /quit 即退出，覆盖装配分支）。"""
    db_path = tmp_path / "voice-real.db"
    await _seed_active_persona(db_path)
    recorder = RecordingOutput()

    exit_code = await cli.run_chat_cli(
        settings=_voice_enabled_settings(),
        database_config=_sqlite_config(db_path),
        input_fn=SequenceInput(["/quit"]),
        output_fn=recorder,
        voice=True,
        play_audio_fn=RecordingPlayer(),
    )
    assert exit_code == 0
    assert "已连接人格：大肥鱼" in recorder.text


async def test_cli_main_voice_flag_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main 入口：--voice 旗标解析并透传 run_chat_cli（voice=True）。"""
    captured: dict[str, object] = {}

    async def fake_run_chat_cli(**kwargs: Any) -> int:
        """假 REPL（记录 voice 旗标）。"""
        captured.update(kwargs)
        return 0

    def fake_asyncio_run(coro: Any) -> int:
        """假 asyncio.run（驱动无 await 协程至完成）。"""
        try:
            coro.send(None)
        except StopIteration as result:
            return int(result.value or 0)
        coro.close()
        return 0

    monkeypatch.setattr(cli, "run_chat_cli", fake_run_chat_cli)
    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys, "argv", ["chat", "--voice"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0
    assert captured["voice"] is True


async def test_cli_main_no_voice_flag_defaults_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main 入口：无旗标 → voice=False 透传。"""
    captured: dict[str, object] = {}

    async def fake_run_chat_cli(**kwargs: Any) -> int:
        """假 REPL（记录 voice 旗标）。"""
        captured.update(kwargs)
        return 0

    def fake_asyncio_run(coro: Any) -> int:
        """假 asyncio.run。"""
        try:
            coro.send(None)
        except StopIteration as result:
            return int(result.value or 0)
        coro.close()
        return 0

    monkeypatch.setattr(cli, "run_chat_cli", fake_run_chat_cli)
    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(sys, "argv", ["chat"])
    with pytest.raises(SystemExit):
        cli.main()
    assert captured["voice"] is False
