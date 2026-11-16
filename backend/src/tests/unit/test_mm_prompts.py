"""harness/multimodal prompts Unit 文件测试——byte-lock + 渲染契约。

byte-lock 语义（与 test_duty_prompts.py 同约定）：逐模板锁定 sha256
摘要——prompt 文本变更（有意行为变更）必须同步上调版本常量并更新
此处摘要；意外漂移直接红。版本常量存在性 + 形态一并锁定。
"""

from __future__ import annotations

import hashlib

from packages.harness.multimodal import mm_prompts as prompts

#: 逐模板 / 文案 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步本表
#: 与版本常量（聚焦区块文案随 LOOK_AT_CHARTER_VERSION 同版本管理）。
_BYTE_LOCKS: dict[str, str] = {
    "LOOK_AT_CHARTER_TEMPLATE": (
        "c054651f95ed165b8f16b2e3263cf83e7a7f04014f3f879b699c23bdaa318ee3"
    ),
    "LOOK_AT_QUESTION_FOCUS_TEMPLATE": (
        "e30d29786e33275436aa887559b4e8f2e65db4b50521208f9d90394c7446a45b"
    ),
    "LOOK_AT_DEFAULT_FOCUS": (
        "f639eefdcfe4603ad5e12959de6dbc0df2a1acb49ddd4a7f369e2aeac995bba4"
    ),
    "LOOK_AT_DEFAULT_INSTRUCTION": (
        "b24bf458e77e3ab801c8bd7a1cefda501b3964c54df8d80fb9b1740ee40e3b20"
    ),
}

#: 版本常量清单（存在性 + X.Y.Z 形态）。
_VERSION_CONSTANTS = (
    "LOOK_AT_PROMPTS_VERSION",
    "LOOK_AT_CHARTER_VERSION",
)


async def test_prompt_templates_byte_lock() -> None:
    """byte-lock：逐模板 / 文案 sha256 摘要与锁定表一致（漂移即红）。"""
    for name, expected in _BYTE_LOCKS.items():
        value = getattr(prompts, name)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert digest == expected, (
            f"prompt 模板 {name} 已变更（sha256 不匹配）。如为有意变更："
            f"上调对应版本常量并同步更新 {__name__} 中的 _BYTE_LOCKS。"
        )


async def test_version_constants_present_and_semver_like() -> None:
    """版本常量存在且为 X.Y.Z 形态（非空 + 三段数字）。"""
    for name in _VERSION_CONSTANTS:
        value = getattr(prompts, name)
        assert isinstance(value, str) and value
        parts = value.split(".")
        assert len(parts) == 3 and all(part.isdigit() for part in parts), (
            f"版本常量 {name}={value!r} 应为 X.Y.Z 形态"
        )


async def test_render_charter_contains_five_sections_and_red_line() -> None:
    """任务书渲染：五段结构化框架 + 红线要点在场。"""
    text = prompts.render_look_at_charter(None)
    assert "【视觉理解任务书】" in text
    assert "主体" in text and "场景" in text
    assert "文字内容" in text and "OCR" in text
    assert "图表数据" in text
    assert "值得注意的细节" in text
    assert "不猜测图中不存在的文字或数据" in text
    assert "不确定" in text


async def test_render_charter_with_question_focus() -> None:
    """问题给定：聚焦区块携带问题原文（描述完成后紧扣提问作答）。"""
    text = prompts.render_look_at_charter("图里的表格总计是多少")
    assert "「图里的表格总计是多少」" in text
    assert "聚焦回答" in text


async def test_render_charter_without_question_uses_default_focus() -> None:
    """问题缺省 / 空串：缺省聚焦（整体总结收束）。"""
    text_none = prompts.render_look_at_charter(None)
    text_blank = prompts.render_look_at_charter("")
    assert text_none == text_blank
    assert "聚焦回答" in text_none
    assert prompts.LOOK_AT_DEFAULT_FOCUS in text_none


async def test_render_user_instruction_passthrough_question() -> None:
    """user 指令渲染：问题给定时即问题本身（text 分块透传）。"""
    assert prompts.render_user_instruction("这只猫在做什么") == "这只猫在做什么"


async def test_render_user_instruction_default_when_no_question() -> None:
    """user 指令渲染：问题缺省 / 空串 → 缺省短指令。"""
    assert prompts.render_user_instruction(None) == prompts.LOOK_AT_DEFAULT_INSTRUCTION
    assert prompts.render_user_instruction("") == prompts.LOOK_AT_DEFAULT_INSTRUCTION
