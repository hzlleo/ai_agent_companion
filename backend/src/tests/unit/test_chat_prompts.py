"""harness/chat prompts Unit 文件测试——byte-lock + 渲染契约。

byte-lock 语义：逐模板锁定 sha256 摘要——prompt 文本变更（有意行为
变更）必须同步上调版本常量并更新此处摘要；意外漂移直接红。版本常量
存在性 + 形态一并锁定。
"""

from __future__ import annotations

import hashlib

from packages.harness.chat import prompts
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)

#: 逐模板 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步本表与版本常量。
_BYTE_LOCKS: dict[str, str] = {
    "CHARTER_TEMPLATE": "c8db58524cfd94382b0aec1b17bba24b253ac2e3a5a1feee37baff5fbb2e1b02",
    "PERSONA_NAME_HEADER_TEMPLATE": (
        "b57da3cee1563aa0f345eab963c732ca5dd8798efceeac463d382c4fdbb24bf6"
    ),
    "PERSONA_DESCRIPTION_SECTION_TEMPLATE": (
        "7f98ddb9b07f5fb31d0bbf4bcd527e8fb52afdd368b03e528b2b4b13fe093456"
    ),
    "PERSONA_PERSONALITY_SECTION_TEMPLATE": (
        "4b316448c9c45896799fe15d6cd0bd2799a01c73bc6e2a0e2d5810ad89756011"
    ),
    "PERSONA_SCENARIO_SECTION_TEMPLATE": (
        "ea9cc3a5d605d3ae11390068d92df8dd81ab0d4765acdf0a563c7f7716941823"
    ),
    "WORLD_BOOK_ENTRY_TEMPLATE": (
        "007b739ddef98c72132b706fa3fb966289a4bfd1d7d1ce252d9b27ec125b98d9"
    ),
    "WORLD_BOOK_SECTION_SEPARATOR": (
        "75a11da44c802486bc6f65640aa48a730f0f684c5c07a42ba3cd1735eb3fb070"
    ),
    "POST_HISTORY_TEMPLATE": "b93aafac8bd969ed35e69de059137f85a69f45af5d9174dd6b28ae6184d7b42d",
    "FIRST_GREETING_FALLBACK_TEMPLATE": (
        "cb91bfe944c2f86d882d08f102f45ee7508f36a51fc0c69d705d9e55d34e66eb"
    ),
}

#: 版本常量清单（存在性 + 非空字符串形态）。
_VERSION_CONSTANTS = (
    "PROMPTS_VERSION",
    "CHARTER_TEMPLATE_VERSION",
    "PERSONA_SYSTEM_PROMPT_VERSION",
    "WORLD_BOOK_ENTRY_TEMPLATE_VERSION",
    "POST_HISTORY_TEMPLATE_VERSION",
    "FIRST_GREETING_FALLBACK_VERSION",
)


async def test_prompt_templates_byte_lock() -> None:
    """byte-lock：逐模板 sha256 摘要与锁定表一致（漂移即红）。"""
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


def _card(**overrides: str) -> CharaCardModel:
    """最小人格卡片（字段可覆写）。"""
    fields: dict[str, str] = {
        "name": "大肥鱼",
        "description": "一条圆滚滚的橘色胖鱼",
        "personality": "元气、贪吃、爱撒娇",
        "scenario": "周末午后的鱼缸边",
    }
    fields.update(overrides)
    return CharaCardModel.model_validate(fields)


async def test_render_persona_system_prompt_full_card() -> None:
    """全字段卡片：四段按序组装（自称头 → 设定 → 性格 → 场景）。"""
    text = prompts.render_persona_system_prompt(_card())
    assert "你现在扮演「大肥鱼」" in text
    assert text.index("【角色设定】") < text.index("【性格特点】") < text.index("【当前场景】")
    assert "一条圆滚滚的橘色胖鱼" in text
    assert "元气、贪吃、爱撒娇" in text
    assert "周末午后的鱼缸边" in text


async def test_render_persona_system_prompt_skips_empty_sections() -> None:
    """空字段段跳过——不产出空标题块。"""
    text = prompts.render_persona_system_prompt(_card(description="", personality="", scenario=""))
    assert "【角色设定】" not in text
    assert "【性格特点】" not in text
    assert "【当前场景】" not in text
    assert "你现在扮演「大肥鱼」" in text


async def test_render_persona_system_prompt_override_wins() -> None:
    """system_prompt 覆写：正典自定义提示整体替换模板组装。"""
    text = prompts.render_persona_system_prompt(_card(system_prompt="自定义正典提示"))
    assert text == "自定义正典提示"


async def test_render_world_book_block_wraps_and_joins() -> None:
    """世界书块：条目逐个包裹 + 分隔符连接；空列表空串。"""
    book = CharaBookModel(
        entries=[
            CharaBookEntryModel(content="第一条设定", constant=True),
            CharaBookEntryModel(content="第二条设定", constant=True),
        ]
    )
    block = prompts.render_world_book_block(book.entries)
    assert block.count("【世界书 · 补充设定】") == 2
    assert "第一条设定" in block and "第二条设定" in block
    assert prompts.WORLD_BOOK_SECTION_SEPARATOR in block
    assert prompts.render_world_book_block([]) == ""


async def test_render_post_history_message_contains_instructions_and_name() -> None:
    """历史后再注入：指令原文 + 人格称呼名均在帧内。"""
    text = prompts.render_post_history_message(
        _card(post_history_instructions="说话保持简短，多用语气词。")
    )
    assert "说话保持简短，多用语气词。" in text
    assert "大肥鱼" in text
    assert "【重要提醒】" in text


async def test_render_first_greeting_card_message_wins() -> None:
    """开场白：卡片 first_mes 优先。"""
    text = prompts.render_first_greeting(_card(first_mes="你回来啦！"))
    assert text == "你回来啦！"


async def test_render_first_greeting_fallback_rendered_with_name() -> None:
    """开场白兜底：first_mes 为空时按称呼名渲染模板。"""
    text = prompts.render_first_greeting(_card(first_mes=""))
    assert "大肥鱼" in text
    assert "今天想聊点什么" in text


async def test_render_charter_returns_template_verbatim() -> None:
    """章程渲染：无占位符——原样返回模板。"""
    assert prompts.render_charter() == prompts.CHARTER_TEMPLATE
