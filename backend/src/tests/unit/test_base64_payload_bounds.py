"""base64 请求体长度帽边界测试——声纹 / 表情包 DTO（pydantic 层拒绝）。

覆盖矩阵：按服务层字节帽推导的 base64 字符串上限恰好通过；超一字符
（以及超字节帽对应的编码增量）在模型层拒绝；两个 DTO 的上限常量与
设置默认字节帽保持单一来源。
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from packages.harness.multimodal.sticker_models import (
    STICKER_IMAGE_BASE64_MAX_LENGTH,
    STICKER_MAX_UPLOAD_BYTES,
    StickerSettings,
    StickerUploadItemModel,
)
from packages.harness.voice.voiceprint_models import (
    VOICEPRINT_AUDIO_BASE64_MAX_LENGTH,
    VOICEPRINT_MAX_UPLOAD_BYTES,
    VoiceprintSampleUploadModel,
    VoiceprintSettings,
)


async def test_voiceprint_audio_base64_boundary() -> None:
    """声纹载荷：上限长度通过，超一字符拒绝。"""
    accepted = VoiceprintSampleUploadModel(
        audio_base64="A" * VOICEPRINT_AUDIO_BASE64_MAX_LENGTH,
        duration_ms=0,
        sample_rate_hz=0,
    )
    assert len(accepted.audio_base64) == VOICEPRINT_AUDIO_BASE64_MAX_LENGTH
    with pytest.raises(ValidationError):
        VoiceprintSampleUploadModel(
            audio_base64="A" * (VOICEPRINT_AUDIO_BASE64_MAX_LENGTH + 1),
            duration_ms=0,
            sample_rate_hz=0,
        )


async def test_voiceprint_base64_cap_covers_default_byte_cap() -> None:
    """声纹载荷：默认字节帽的 base64 编码通过，超一个编码单元拒绝。"""
    exact = base64.b64encode(b"x" * VOICEPRINT_MAX_UPLOAD_BYTES).decode("ascii")
    assert len(exact) <= VOICEPRINT_AUDIO_BASE64_MAX_LENGTH
    VoiceprintSampleUploadModel(audio_base64=exact, duration_ms=0, sample_rate_hz=0)
    oversized = base64.b64encode(
        b"x" * (VOICEPRINT_MAX_UPLOAD_BYTES + 3)
    ).decode("ascii")
    assert len(oversized) > VOICEPRINT_AUDIO_BASE64_MAX_LENGTH
    with pytest.raises(ValidationError):
        VoiceprintSampleUploadModel(
            audio_base64=oversized, duration_ms=0, sample_rate_hz=0
        )


async def test_sticker_image_base64_boundary() -> None:
    """表情包载荷：上限长度通过，超一字符拒绝。"""
    accepted = StickerUploadItemModel(
        image_base64="A" * STICKER_IMAGE_BASE64_MAX_LENGTH,
        source_note="自绘",
    )
    assert len(accepted.image_base64) == STICKER_IMAGE_BASE64_MAX_LENGTH
    with pytest.raises(ValidationError):
        StickerUploadItemModel(
            image_base64="A" * (STICKER_IMAGE_BASE64_MAX_LENGTH + 1),
            source_note="自绘",
        )


async def test_sticker_base64_cap_covers_default_byte_cap() -> None:
    """表情包载荷：默认字节帽的 base64 编码通过，超一个编码单元拒绝。"""
    exact = base64.b64encode(b"x" * STICKER_MAX_UPLOAD_BYTES).decode("ascii")
    assert len(exact) <= STICKER_IMAGE_BASE64_MAX_LENGTH
    StickerUploadItemModel(image_base64=exact, source_note="自绘")
    oversized = base64.b64encode(b"x" * (STICKER_MAX_UPLOAD_BYTES + 3)).decode("ascii")
    assert len(oversized) > STICKER_IMAGE_BASE64_MAX_LENGTH
    with pytest.raises(ValidationError):
        StickerUploadItemModel(image_base64=oversized, source_note="自绘")


async def test_dto_caps_derive_from_settings_defaults() -> None:
    """上限常量与设置默认字节帽同源（改一处不会漂移）。"""
    assert VoiceprintSettings().max_upload_bytes == VOICEPRINT_MAX_UPLOAD_BYTES
    assert StickerSettings().max_upload_bytes == STICKER_MAX_UPLOAD_BYTES
