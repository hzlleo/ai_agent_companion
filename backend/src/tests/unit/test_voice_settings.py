"""voice 域配置测试——VoiceSettings / TtsSettings / AsrSettings env 面契约。

覆盖面：代码默认值（零配置开发可用）、env 覆盖形态（VOICE_TTS_ /
VOICE_ASR_ 前缀逐键）、build_isolated_settings voice 参数注入、边界
校验（语速区间 / 语言空串归一）。
"""

from __future__ import annotations

import pytest

from packages.harness.config.models import AsrSettings, TtsSettings, VoiceSettings
from packages.harness.config.settings import build_isolated_settings


class TestDefaults:
    async def test_tts_defaults_disabled(self) -> None:
        """TTS 缺省关闭 + CosyVoice 包装服务契约默认值。"""
        tts = TtsSettings()
        assert tts.enabled is False
        assert tts.base_url == "http://127.0.0.1:9880"
        assert tts.model_entry == "cosyvoice3"
        assert tts.request_timeout_seconds == 30.0
        assert tts.default_speaker == "中文女"
        assert tts.default_speed == 1.0

    async def test_asr_defaults_disabled(self) -> None:
        """ASR 缺省关闭 + Qwen3-ASR OpenAI 兼容端点默认值。"""
        asr = AsrSettings()
        assert asr.enabled is False
        assert asr.base_url == "http://127.0.0.1:8082"
        assert asr.model_entry == "qwen3-asr-0.6b"
        assert asr.request_timeout_seconds == 60.0
        assert asr.language is None

    async def test_voice_aggregate_defaults(self) -> None:
        """VoiceSettings 聚合容器缺省双关闭。"""
        voice = VoiceSettings()
        assert voice.tts.enabled is False
        assert voice.asr.enabled is False


class TestEnvOverrides:
    async def test_tts_env_prefix_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VOICE_TTS_ 前缀逐键覆盖（enabled / base_url / speaker / speed）。"""
        monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
        monkeypatch.setenv("VOICE_TTS_BASE_URL", "http://10.0.0.5:9880")
        monkeypatch.setenv("VOICE_TTS_MODEL_ENTRY", "cosyvoice3-cloned")
        monkeypatch.setenv("VOICE_TTS_DEFAULT_SPEAKER", "克隆音色A")
        monkeypatch.setenv("VOICE_TTS_DEFAULT_SPEED", "1.5")
        tts = TtsSettings()
        assert tts.enabled is True
        assert tts.base_url == "http://10.0.0.5:9880"
        assert tts.model_entry == "cosyvoice3-cloned"
        assert tts.default_speaker == "克隆音色A"
        assert tts.default_speed == 1.5

    async def test_asr_env_prefix_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VOICE_ASR_ 前缀逐键覆盖 + language 空串归一 None。"""
        monkeypatch.setenv("VOICE_ASR_ENABLED", "true")
        monkeypatch.setenv("VOICE_ASR_BASE_URL", "http://10.0.0.6:8082")
        monkeypatch.setenv("VOICE_ASR_LANGUAGE", "")
        asr = AsrSettings()
        assert asr.enabled is True
        assert asr.base_url == "http://10.0.0.6:8082"
        assert asr.language is None

    async def test_asr_language_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VOICE_ASR_LANGUAGE 非空值生效（语言提示）。"""
        monkeypatch.setenv("VOICE_ASR_LANGUAGE", "zh")
        assert AsrSettings().language == "zh"


class TestValidation:
    async def test_tts_speed_out_of_bounds_rejected(self) -> None:
        """语速越界（<0.5 / >2.0）拒绝——装配错误面。"""
        with pytest.raises(ValueError):
            TtsSettings(default_speed=0.3)
        with pytest.raises(ValueError):
            TtsSettings(default_speed=2.5)

    async def test_empty_base_url_rejected(self) -> None:
        """base_url 空串拒绝（服务地址必填）。"""
        with pytest.raises(ValueError):
            TtsSettings(base_url="")


class TestIsolatedBuild:
    async def test_build_isolated_settings_voice_param(self) -> None:
        """build_isolated_settings voice 参数：显式实例原样嵌入（不重读 env）。"""
        voice = VoiceSettings(tts=TtsSettings(enabled=True, default_speaker="音色X"))
        settings = build_isolated_settings(voice=voice)
        assert settings.voice is voice
        assert settings.voice.tts.enabled is True
        assert settings.voice.tts.default_speaker == "音色X"
        assert settings.voice.asr.enabled is False

    async def test_build_isolated_settings_voice_default(self) -> None:
        """voice 未传入：代码默认（双关闭）——既有调用面零破坏。"""
        settings = build_isolated_settings()
        assert settings.voice.tts.enabled is False
        assert settings.voice.asr.enabled is False
