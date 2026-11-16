"""harness/multimodal mm_models 测试——请求 / 工具参数校验 + data URL 助手。

纯 Model 层测试（零 HTTP / 零数据库 / 零真实配置）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.multimodal.mm_models import (
    LOOK_AT_IMAGE_MAX_BYTES,
    LOOK_AT_IMAGE_MIME_ALLOWLIST,
    LookAtRequestModel,
    LookAtToolArgsModel,
    build_data_url,
    parse_data_url,
)

#: 测试图片字节（内容无关——契约面是字节透传）。
_PNG_BYTES: bytes = b"\x89PNG\r\n\x1a\n fake-image-body"


# ---------------------------------------------------------------------------
# LookAtRequestModel
# ---------------------------------------------------------------------------


async def test_request_model_accepts_valid_payload() -> None:
    """合法请求：字节原样 / MIME 归一 / 问题可选。"""
    request = LookAtRequestModel(image=_PNG_BYTES, mime_type="image/png", question="图里有几只猫")
    assert request.image == _PNG_BYTES
    assert request.mime_type == "image/png"
    assert request.question == "图里有几只猫"
    bare = LookAtRequestModel(image=_PNG_BYTES, mime_type=" image/WebP ")
    assert bare.mime_type == "image/webp"
    assert bare.question is None


async def test_request_model_normalizes_mime_case_and_space() -> None:
    """MIME 归一：大小写 / 首尾空白折叠后按允许清单判定。"""
    request = LookAtRequestModel(image=_PNG_BYTES, mime_type="  IMAGE/GIF  ")
    assert request.mime_type == "image/gif"


async def test_request_model_rejects_unknown_mime() -> None:
    """MIME 不在允许清单：拒绝（bmp / heic 等非通用支持面）。"""
    with pytest.raises(ValidationError):
        LookAtRequestModel(image=_PNG_BYTES, mime_type="image/bmp")
    with pytest.raises(ValidationError):
        LookAtRequestModel(image=_PNG_BYTES, mime_type="image/heic")


async def test_request_model_rejects_empty_image() -> None:
    """空图像字节：拒绝。"""
    with pytest.raises(ValidationError):
        LookAtRequestModel(image=b"", mime_type="image/png")


async def test_request_model_rejects_oversized_image() -> None:
    """超 10MB 上限：拒绝（调用方先行压缩 / 裁剪）。"""
    oversized = b"x" * (LOOK_AT_IMAGE_MAX_BYTES + 1)
    with pytest.raises(ValidationError):
        LookAtRequestModel(image=oversized, mime_type="image/png")


async def test_request_model_rejects_over_limit_question() -> None:
    """聚焦问题超 200 字符：拒绝（问题是大方向提示，不是长指令）。"""
    with pytest.raises(ValidationError):
        LookAtRequestModel(image=_PNG_BYTES, mime_type="image/png", question="问" * 201)


async def test_mime_allowlist_contents() -> None:
    """允许清单值域锁定：png / jpeg / webp / gif。"""
    assert sorted(LOOK_AT_IMAGE_MIME_ALLOWLIST) == [
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    ]

# ---------------------------------------------------------------------------
# LookAtToolArgsModel
# ---------------------------------------------------------------------------


async def test_tool_args_defaults() -> None:
    """工具参数缺省：mime png / 问题 None；base64 非空契约。"""
    args = LookAtToolArgsModel(image_base64="aGVsbG8=")
    assert args.mime_type == "image/png"
    assert args.question is None
    with pytest.raises(ValidationError):
        LookAtToolArgsModel(image_base64="")


async def test_tool_args_rejects_unknown_fields() -> None:
    """工具参数 extra=forbid：未知字段拒绝（LLM 幻觉参数面收敛）。"""
    with pytest.raises(ValidationError):
        LookAtToolArgsModel.model_validate(
            {"image_base64": "aGVsbG8=", "unexpected": "x"}
        )


async def test_tool_args_validates_mime_and_question() -> None:
    """工具参数面与服务请求面同规则：MIME 清单 + 问题上限。"""
    args = LookAtToolArgsModel(image_base64="aGVsbG8=", mime_type="IMAGE/JPEG", question="看图表")
    assert args.mime_type == "image/jpeg"
    with pytest.raises(ValidationError):
        LookAtToolArgsModel(image_base64="aGVsbG8=", mime_type="image/tiff")
    with pytest.raises(ValidationError):
        LookAtToolArgsModel(image_base64="aGVsbG8=", question="问" * 201)


# ---------------------------------------------------------------------------
# data URL 助手
# ---------------------------------------------------------------------------


async def test_data_url_roundtrip() -> None:
    """构建 → 解析往返：MIME 与字节无损。"""
    data_url = build_data_url(_PNG_BYTES, "image/png")
    assert data_url.startswith("data:image/png;base64,")
    mime_type, image = parse_data_url(data_url)
    assert mime_type == "image/png"
    assert image == _PNG_BYTES


async def test_parse_data_url_rejects_malformed_forms() -> None:
    """解析拒绝面：非 data 前缀 / 缺逗号 / 非 base64 / 空 MIME / 非法载荷。"""
    with pytest.raises(ValueError, match="data:"):
        parse_data_url("https://example.com/a.png")
    with pytest.raises(ValueError, match="逗号"):
        parse_data_url("data:image/png;base64")
    with pytest.raises(ValueError, match="base64"):
        parse_data_url("data:image/png,raw-payload")
    with pytest.raises(ValueError, match="MIME"):
        parse_data_url("data:;base64,QUJD")
    with pytest.raises(ValueError, match="base64 载荷"):
        parse_data_url("data:image/png;base64,!!!not-base64!!!")
