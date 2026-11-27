"""GUI 微信提示词 Unit 文件测试——byte-lock + 渲染契约。

byte-lock 语义（与其余 prompts 测试同约定）：逐模板锁定 sha256 摘要——提示词
变更（有意行为变更）必须同步上调版本常量并更新此处摘要；意外漂移直接红。
"""

from __future__ import annotations

import hashlib

from packages.harness.channels.wechat_gui import wechat_gui_prompts as prompts

#: 逐模板 sha256 摘要锁（UTF-8 字节）。
_BYTE_LOCKS: dict[str, str] = {
    "WECHAT_GUI_READER_CHARTER_TEMPLATE": (
        "9fe965e31b68642efb09b24f9da3e22e8bc509bdc3d613accc6cb51d7597f98c"
    ),
    "WECHAT_GUI_READER_INSTRUCTION_TEMPLATE": (
        "36681baaa8e311a894577159d0f4a73eb48ac1d6bfa350107375c6eeb8f078ff"
    ),
}

#: 版本常量清单（存在性 + X.Y.Z 形态）。
_VERSION_CONSTANTS = (
    "WECHAT_GUI_PROMPTS_VERSION",
    "WECHAT_GUI_READER_CHARTER_VERSION",
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
    """版本常量存在且为 X.Y.Z 形态。"""
    for name in _VERSION_CONSTANTS:
        value = getattr(prompts, name)
        assert isinstance(value, str) and value
        parts = value.split(".")
        assert len(parts) == 3 and all(part.isdigit() for part in parts)


async def test_render_charter_contract_keywords() -> None:
    """任务书渲染：严格 JSON / 标题 / 消息 / 置信度 / 占位约定在场。"""
    text = prompts.render_wechat_gui_reader_charter()
    assert text.rstrip().endswith("把握。")
    assert '"title"' in text and '"messages"' in text and '"confidence"' in text
    assert "is_self" in text and "ts_ms" in text
    assert "相对时间" in text and "读不出" in text
    assert "epoch" not in text
    assert "不臆测" in text
    assert "[图片]" in text


async def test_render_instruction_replaces_max_messages() -> None:
    """user 指令渲染：消息上限占位符被替换（无残留占位符）。"""
    text = prompts.render_wechat_gui_reader_instruction(max_messages=12)
    assert "12 条消息" in text
    assert "{max_messages}" not in text
