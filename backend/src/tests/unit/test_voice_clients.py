"""voice 域客户端测试——CosyVoice TTS / Qwen3 ASR（MockTransport 零真实网络）。

覆盖矩阵：

    - TTS：请求形状（URL / JSON 三键锁定体 / instruct_text 扩展键 /
      请求超时）→ 结果归一；错误映射（参数越界 VALIDATION_ERROR；
      传输异常 / 非 200 / 内容类型不符 / 空响应体 LLM_CALL_FAILED）；
    - ASR：multipart 请求形状（file 字段名 / 文件名 / model 表单字段 /
      language 提示 / 字节载荷）→ 结果归一；错误映射（空音频 / 空文件名
      VALIDATION_ERROR；传输异常 / 非 200 / JSON 非法 LLM_CALL_FAILED）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.voice.asr_client import Qwen3AsrClient
from packages.harness.voice.tts_client import CosyVoiceTtsClient

#: 测试用 WAV 载荷（内容任意——客户端不做容器解析，仅透传字节）。
_FAKE_WAV: bytes = b"RIFF-fake-wav-bytes"


def _tts_client(
    handler: Any, *, base_url: str = "http://tts.test", timeout_seconds: float = 5.0
) -> CosyVoiceTtsClient:
    """构造测试 TTS 客户端（MockTransport）。

    Args:
        handler: MockTransport 处理器。
        base_url: 服务基础 URL。
        timeout_seconds: 请求超时秒数。

    Returns:
        CosyVoiceTtsClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CosyVoiceTtsClient(
        http, base_url=base_url, request_timeout_seconds=timeout_seconds
    )


def _asr_client(
    handler: Any, *, base_url: str = "http://asr.test", timeout_seconds: float = 6.0
) -> Qwen3AsrClient:
    """构造测试 ASR 客户端（MockTransport）。

    Args:
        handler: MockTransport 处理器。
        base_url: 服务基础 URL。
        timeout_seconds: 请求超时秒数。

    Returns:
        Qwen3AsrClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Qwen3AsrClient(
        http,
        base_url=base_url,
        model_id="qwen3-asr-0.6b",
        request_timeout_seconds=timeout_seconds,
    )


class TestTtsClient:
    async def test_request_shape_locked_three_keys(self) -> None:
        """缺省请求体恰为 ``{text, speaker, speed}`` 三键（speaker null 形态）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.content)
            captured["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, content=_FAKE_WAV, headers={"content-type": "audio/wav"})

        client = _tts_client(handler)
        result = await client.synthesize("你好", speaker=None, speed=1.0)
        assert captured["url"] == "http://tts.test/tts"
        assert captured["json"] == {"text": "你好", "speaker": None, "speed": 1.0}
        assert captured["timeout"]["read"] == pytest.approx(5.0)
        assert result.audio == _FAKE_WAV
        assert result.content_type == "audio/wav"
        assert result.latency_ms >= 0

    async def test_request_shape_speaker_and_instruct_extension(self) -> None:
        """speaker 透传 + instruct_text 存在时作为扩展键携带（人格绑定面）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, content=_FAKE_WAV, headers={"content-type": "audio/wav"})

        client = _tts_client(handler)
        await client.synthesize(
            "早安", speaker="中文女", speed=1.25, instruct_text="温柔地说"
        )
        assert captured["json"] == {
            "text": "早安",
            "speaker": "中文女",
            "speed": 1.25,
            "instruct_text": "温柔地说",
        }

    async def test_text_over_limit_maps_validation_error(self) -> None:
        """文本超 500 字符：VALIDATION_ERROR（客户端本地校验——零网络）。"""
        client = _tts_client(lambda request: httpx.Response(200, content=b"x"))
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("长" * 501)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_speed_out_of_range_maps_validation_error(self) -> None:
        """语速越界：VALIDATION_ERROR。"""
        client = _tts_client(lambda request: httpx.Response(200, content=b"x"))
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("你好", speed=2.5)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_200_maps_call_failed(self) -> None:
        """非 200 状态：LLM_CALL_FAILED（details 携带状态码与片段）。"""
        client = _tts_client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("你好")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["status_code"] == 500
        assert exc_info.value.details["service"] == "cosyvoice3"

    async def test_transport_error_maps_call_failed(self) -> None:
        """传输异常（连接失败）：LLM_CALL_FAILED。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _tts_client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("你好")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_wrong_content_type_maps_call_failed(self) -> None:
        """响应 Content-Type 非 audio/wav：LLM_CALL_FAILED（契约破坏）。"""
        client = _tts_client(
            lambda request: httpx.Response(200, json={"error": "not audio"})
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("你好")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert "audio/wav" in exc_info.value.message

    async def test_content_type_with_charset_accepted(self) -> None:
        """Content-Type 携带参数后缀（如 ``audio/wav; charset=...``）仍接受。"""
        client = _tts_client(
            lambda request: httpx.Response(
                200,
                content=_FAKE_WAV,
                headers={"content-type": "audio/wav; charset=binary"},
            )
        )
        result = await client.synthesize("你好")
        assert result.audio == _FAKE_WAV

    async def test_empty_body_maps_call_failed(self) -> None:
        """200 但空响应体：LLM_CALL_FAILED。"""
        client = _tts_client(
            lambda request: httpx.Response(
                200, content=b"", headers={"content-type": "audio/wav"}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.synthesize("你好")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


class TestAsrClient:
    async def test_multipart_request_shape(self) -> None:
        """multipart 形状：file 字段（文件名 + 字节载荷）+ model 表单字段。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            captured["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, json={"text": "你好世界"})

        client = _asr_client(handler)
        audio = b"RIFF....audio-bytes"
        result = await client.transcribe(audio, "memo.wav")
        assert captured["content_type"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="memo.wav"' in captured["body"]
        assert audio in captured["body"]
        assert b'name="model"' in captured["body"]
        assert b"qwen3-asr-0.6b" in captured["body"]
        # 未提供 language → 不携带 language 表单字段
        assert b'name="language"' not in captured["body"]
        assert captured["timeout"]["read"] == pytest.approx(6.0)
        assert result.text == "你好世界"
        assert result.duration_ms is None
        assert result.language is None

    async def test_language_hint_form_field(self) -> None:
        """language 提示：作为可选表单字段携带。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(
                200, json={"text": "hello", "duration_ms": 1200, "language": "en"}
            )

        client = _asr_client(handler)
        result = await client.transcribe(b"audio", "memo.mp3", language="en")
        assert b'name="language"' in captured["body"]
        assert b"en" in captured["body"]
        assert result.text == "hello"
        assert result.duration_ms == 1200
        assert result.language == "en"

    async def test_empty_audio_maps_validation_error(self) -> None:
        """空音频：VALIDATION_ERROR（零网络）。"""
        client = _asr_client(lambda request: httpx.Response(200, json={"text": "x"}))
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"", "memo.wav")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_blank_filename_maps_validation_error(self) -> None:
        """空 / 纯空白文件名：VALIDATION_ERROR。"""
        client = _asr_client(lambda request: httpx.Response(200, json={"text": "x"}))
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"audio", "   ")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_200_maps_call_failed(self) -> None:
        """非 200 状态：LLM_CALL_FAILED。"""
        client = _asr_client(lambda request: httpx.Response(503, text="overloaded"))
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"audio", "memo.wav")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["service"] == "qwen3-asr"

    async def test_transport_error_maps_call_failed(self) -> None:
        """传输异常：LLM_CALL_FAILED。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = _asr_client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"audio", "memo.wav")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_invalid_json_maps_call_failed(self) -> None:
        """响应非 JSON：LLM_CALL_FAILED。"""
        client = _asr_client(lambda request: httpx.Response(200, text="not-json"))
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"audio", "memo.wav")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_missing_text_field_maps_call_failed(self) -> None:
        """JSON 缺 text 字段：LLM_CALL_FAILED（契约破坏）。"""
        client = _asr_client(lambda request: httpx.Response(200, json={"duration_ms": 5}))
        with pytest.raises(BusinessException) as exc_info:
            await client.transcribe(b"audio", "memo.wav")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
