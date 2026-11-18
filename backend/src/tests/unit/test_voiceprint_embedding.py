"""声纹说话人向量客户端测试（MockTransport 零真实网络）。

覆盖面：multipart 请求形状（路径 / file 字段 / model 字段 / 字节载荷）→
向量归一（直连形态与 OpenAI 兼容形态）；错误映射（空音频 / 空文件名
VALIDATION_ERROR；传输异常 / 非 200 / JSON 非法 / 向量缺失或非有限
LLM_CALL_FAILED）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.voice.voiceprint_embedding import VoiceprintEmbeddingClient

#: 测试音频载荷（内容任意——客户端不做容器解析，仅透传字节）。
_FAKE_WAV: bytes = b"RIFF-fake-wav"


def _client(handler: Any) -> VoiceprintEmbeddingClient:
    """构造测试客户端（MockTransport）。"""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VoiceprintEmbeddingClient(
        http,
        base_url="http://voiceprint.test/",
        endpoint_path="embedding",
        model_id="voiceprint-test",
        request_timeout_seconds=3.0,
    )


async def test_embed_posts_multipart_and_returns_vector() -> None:
    """请求形状：POST /embedding、file + model 字段、原始字节载荷。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json={"embedding": [0.1, -0.2, 0.3]})

    vector = await _client(handler).embed(_FAKE_WAV, "voiceprint.wav", "audio/wav")
    assert vector == [0.1, -0.2, 0.3]
    assert captured["url"] == "http://voiceprint.test/embedding"
    assert "multipart/form-data" in captured["content_type"]
    assert b'name="model"' in captured["body"]
    assert b"voiceprint-test" in captured["body"]
    assert b'name="file"; filename="voiceprint.wav"' in captured["body"]
    assert _FAKE_WAV in captured["body"]


async def test_embed_accepts_openai_compatible_shape() -> None:
    """OpenAI 兼容形态（data[0].embedding）同样归一为向量。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1, 2.5]}]})

    vector = await _client(handler).embed(_FAKE_WAV, "a.wav", "audio/wav")
    assert vector == [1.0, 2.5]


async def test_embed_rejects_empty_audio_and_filename() -> None:
    """空音频 / 空文件名 → VALIDATION_ERROR（不发请求）。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embedding": [1.0]})

    client = _client(handler)
    with pytest.raises(BusinessException) as audio_error:
        await client.embed(b"", "a.wav", "audio/wav")
    with pytest.raises(BusinessException) as name_error:
        await client.embed(_FAKE_WAV, "  ", "audio/wav")
    assert audio_error.value.code == ErrorCode.VALIDATION_ERROR
    assert name_error.value.code == ErrorCode.VALIDATION_ERROR
    assert calls == 0


async def test_embed_maps_transport_error() -> None:
    """传输异常 → LLM_CALL_FAILED（details 携带 service / base_url）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embed(_FAKE_WAV, "a.wav", "audio/wav")
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert exc_info.value.details["service"] == "voiceprint-embedding"


async def test_embed_maps_non_200() -> None:
    """非 200 → LLM_CALL_FAILED（携带状态码与响应体片段）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embed(_FAKE_WAV, "a.wav", "audio/wav")
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
    assert exc_info.value.details["status_code"] == 500
    assert exc_info.value.details["body_snippet"] == "boom"


@pytest.mark.parametrize(
    "payload",
    [
        {"embedding": []},
        {"embedding": "nope"},
        {"embedding": [1, True]},
        {"embedding": [1, "x"]},
        {"embedding": [0.0, float("inf")]},
        {"data": []},
        {},
    ],
)
async def test_embed_rejects_broken_payload(payload: dict[str, Any]) -> None:
    """响应向量缺失 / 非法元素 / 非有限值 → LLM_CALL_FAILED。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload, allow_nan=True))

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embed(_FAKE_WAV, "a.wav", "audio/wav")
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED


async def test_embed_rejects_non_json_body() -> None:
    """响应不是 JSON → LLM_CALL_FAILED。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    with pytest.raises(BusinessException) as exc_info:
        await _client(handler).embed(_FAKE_WAV, "a.wav", "audio/wav")
    assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
