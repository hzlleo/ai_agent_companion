"""harness/multimodal 表情包标注提示词测试——byte-lock + 渲染契约。

byte-lock 语义（与 test_mm_prompts.py 同约定）：逐模板锁定 sha256 摘要——
prompt 文本变更（有意行为变更）必须同步上调版本常量并更新此处摘要；意外
漂移直接红。版本常量存在性与 X.Y.Z 形态一并锁定。
"""

from __future__ import annotations

import hashlib

from packages.harness.multimodal import sticker_prompts as prompts

#: 逐模板 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步版本常量。
_BYTE_LOCKS: dict[str, str] = {
    "STICKER_ANNOTATION_CHARTER_TEMPLATE": (
        "28c789f5d53bd3cedddf167b0ee15cf7383d4f5a9c5ee75e68a41052a594d688"
    ),
    "STICKER_ANNOTATION_INSTRUCTION_TEMPLATE": (
        "7e84586119d11fb0f621a1b0b730b034ec039b922aa9b5a67c46a6b2e09b19be"
    ),
}


async def test_prompt_templates_byte_lock() -> None:
    """byte-lock：模板 sha256 与锁定表一致（漂移即红）。"""
    for name, expected in _BYTE_LOCKS.items():
        value = getattr(prompts, name)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert digest == expected, (
            f"prompt 模板 {name} 已变更（sha256 不匹配）。如为有意变更："
            f"上调 STICKER_PROMPTS_VERSION 并同步更新 {__name__} 中的 _BYTE_LOCKS。"
        )


async def test_version_constant_shape_and_render_helpers() -> None:
    """版本常量为 X.Y.Z；渲染助手返回模板原文。"""
    parts = prompts.STICKER_PROMPTS_VERSION.split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts)
    assert prompts.render_sticker_annotation_charter() == (
        prompts.STICKER_ANNOTATION_CHARTER_TEMPLATE
    )
    assert prompts.render_sticker_annotation_instruction() == (
        prompts.STICKER_ANNOTATION_INSTRUCTION_TEMPLATE
    )


async def test_charter_requires_json_and_red_line() -> None:
    """任务书包含 JSON 输出契约与「只描述可见内容」红线。"""
    charter = prompts.STICKER_ANNOTATION_CHARTER_TEMPLATE
    assert "JSON" in charter
    assert "description" in charter and "tags" in charter
    assert "只描述画面真实可见" in charter
