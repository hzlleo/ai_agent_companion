#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/extensions_config.example.json`` 生成。

从 ``ExtensionTool`` / ``ExtensionsConfig`` 内省字段 schema，配合
``ExtensionsConfig.default_tools()`` 示例数据（单一真相源）生成
``extensions_config.example.json`` 内容。JSON 不支持注释，因此使用
``$comment`` 字段承载字段 schema 说明。
"""

from __future__ import annotations

import json
from typing import Any

from generate_examples_helpers import _get_field_description

from packages.harness.config.models import ExtensionsConfig, ExtensionTool


def generate_extensions_example_json() -> str:
    """从 ExtensionTool / ExtensionsConfig 内省生成 extensions_config.example.json。

    Returns:
        完整的 ``extensions_config.example.json`` 文件内容字符串。
    """
    base_comment = (
        "ai_agent_companion LLM 工具配置模板（function calling schema）。"
        "用法：复制为 extensions_config.json 并按需调整。"
        "每个 tool 定义一个 LLM 可调用的函数，parameters 遵循 JSON Schema 规范。"
    )

    schema_parts: list[str] = [
        "",
        "ExtensionTool 字段:",
    ]
    for field_name, field_info in ExtensionTool.model_fields.items():
        description = _get_field_description(field_info)
        type_annotation = field_info.annotation
        type_name = getattr(type_annotation, "__name__", str(type_annotation))
        if description:
            schema_parts.append(f"  - {field_name} ({type_name}): {description}")
        else:
            schema_parts.append(f"  - {field_name} ({type_name})")

    schema_parts.extend(["", "ExtensionsConfig 顶层字段:"])
    for field_name, field_info in ExtensionsConfig.model_fields.items():
        description = _get_field_description(field_info)
        if description:
            schema_parts.append(f"  - {field_name}: {description}")

    comment_text = base_comment + "\n" + "\n".join(schema_parts)

    data: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": comment_text,
        "tools": ExtensionsConfig.default_tools(),
    }

    raw = json.dumps(data, ensure_ascii=False, indent=2)
    return raw + "\n"


__all__ = [
    "generate_extensions_example_json",
]
