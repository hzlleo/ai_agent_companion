"""harness/duty prompts Unit 文件测试——byte-lock + 渲染契约。

byte-lock 语义（与 test_chat_prompts.py 同约定）：逐模板锁定 sha256
摘要——prompt 文本变更（有意行为变更）必须同步上调版本常量并更新
此处摘要；意外漂移直接红。版本常量存在性 + 形态一并锁定。
"""

from __future__ import annotations

import hashlib

from packages.harness.duty import duty_prompts as prompts
from packages.harness.llm.llm_models import ToolDefinitionModel, ToolParametersSchema
from packages.harness.tools.tool_models import ToolResultModel

#: 逐模板 sha256 摘要锁（UTF-8 字节）——变更 prompt 须同步本表与版本常量。
_BYTE_LOCKS: dict[str, str] = {
    "DUTY_CHARTER_TEMPLATE": "b0b3270aa65c5363760ba8a1e900bd454bd555aff1085245ef21f4f348fc455b",
    "TOOL_LIST_TEMPLATE": "b2f1d0c3037423d06e9070b86daf2ad3f3e41b3c3a81c86c4f956cde12f762cb",
    "TOOL_RESULT_WRAPPER_TEMPLATE": (
        "07db419d331f35d962891feab0280e72e38b418d97829bdc5a12a5541d0196c3"
    ),
    "FINAL_REPORT_INSTRUCTION_TEMPLATE": (
        "4c4de0bf477246c093597bb74c08339a43aea5e5be7c61b9b1168613e2ce34e3"
    ),
    "SECTION_SEPARATOR": "75a11da44c802486bc6f65640aa48a730f0f684c5c07a42ba3cd1735eb3fb070",
}

#: 版本常量清单（存在性 + X.Y.Z 形态）。
_VERSION_CONSTANTS = (
    "DUTY_PROMPTS_VERSION",
    "DUTY_CHARTER_VERSION",
    "TOOL_LIST_VERSION",
    "TOOL_RESULT_WRAPPER_VERSION",
    "FINAL_REPORT_INSTRUCTION_VERSION",
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


def _specs() -> list[ToolDefinitionModel]:
    """两把测试工具定义。"""
    return [
        ToolDefinitionModel(
            name="get_current_time",
            description="获取当前时间",
            parameters=ToolParametersSchema(type="object"),
        ),
        ToolDefinitionModel(
            name="search_memory",
            description="检索记忆",
            parameters=ToolParametersSchema(type="object"),
        ),
    ]


async def test_render_duty_charter_substitutes_name_and_steps() -> None:
    """任务书渲染：人格名 + 步数上限替换；红线要点在场。"""
    text = prompts.render_duty_charter("大肥鱼", 8)
    assert "大肥鱼" in text
    assert "8 步" in text
    assert "【值班任务书】" in text
    assert "第一人称" in text
    assert "不得透露" in text and "不得向任何平台写入" in text


async def test_render_duty_charter_override_wins() -> None:
    """任务书覆写：非空整体替换正典模板。"""
    text = prompts.render_duty_charter("大肥鱼", 8, charter_override="自定义任务书")
    assert text == "自定义任务书"


async def test_render_duty_charter_blank_override_falls_back() -> None:
    """任务书覆写为空串：回落正典模板。"""
    text = prompts.render_duty_charter("大肥鱼", 3, charter_override="")
    assert "【值班任务书】" in text


async def test_render_tool_list_lines_each_spec() -> None:
    """工具清单：逐工具「名称：描述」行 + 帧文案。"""
    text = prompts.render_tool_list(_specs())
    assert "- get_current_time：获取当前时间" in text
    assert "- search_memory：检索记忆" in text
    assert "【可用工具】" in text


async def test_render_tool_list_empty_specs_renders_placeholder() -> None:
    """空工具清单：恒定帧结构（「无可用工具」占位行）。"""
    text = prompts.render_tool_list([])
    assert "（本轮无可用工具）" in text


async def test_render_tool_result_wraps_json_payload() -> None:
    """工具结果包裹：工具名 + 结果 JSON（含 ok / 时延轴）。"""
    result = ToolResultModel(tool="search_memory", ok=True, result={"count": 0}, latency_ms=3)
    text = prompts.render_tool_result(result)
    assert "【工具结果 · search_memory】" in text
    assert '"count":0' in text
    assert '"ok":true' in text


async def test_render_final_report_instruction_contains_name() -> None:
    """收尾指令：人格名替换 + 不再调用工具的强约束。"""
    text = prompts.render_final_report_instruction("大肥鱼")
    assert "大肥鱼" in text
    assert "不再调用任何工具" in text
