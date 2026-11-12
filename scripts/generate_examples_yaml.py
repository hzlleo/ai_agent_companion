#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/config.example.yaml`` 生成。

从 ``LlmModelEntry`` / ``LlmConfig`` / ``MultimodalFallbackConfig`` 内省
字段 schema（Field description），配合 ``LlmConfig.default_models()``
示例数据（单一真相源）生成 ``config.example.yaml`` 内容。
"""

from __future__ import annotations

import types
from typing import get_args, get_origin

from generate_examples_helpers import _get_field_description
from pydantic import BaseModel

from packages.harness.config.model_entry import LlmModelEntry, ModelRole
from packages.harness.config.models import (
    LlmConfig,
    MultimodalFallbackConfig,
)


def _yaml_quote(value: str) -> str:
    """对 YAML 字符串值加引号（保持统一的引号风格）。

    Args:
        value: 原始字符串。

    Returns:
        双引号包裹的 YAML 字符串（空串为 ``""``）。
    """
    if not value:
        return '""'
    return f'"{value}"'


def _type_display_name(annotation: object) -> str:
    """渲染字段类型的可读名（嵌套模型 / Union / 容器取短名，去模块路径）。

    Args:
        annotation: 字段类型注解（pydantic 求值后的真实类型）。

    Returns:
        短名形态的类型字符串（如 ``LlmModelEntry | None`` /
        ``list[ModelRole]`` / ``int``）。
    """
    if annotation is type(None):
        return "None"
    if isinstance(annotation, types.UnionType):
        return " | ".join(_type_display_name(arg) for arg in annotation.__args__)
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if args:
            inner = ", ".join(_type_display_name(arg) for arg in args)
            return f"{_type_display_name(origin)}[{inner}]"
        return _type_display_name(origin)
    if hasattr(annotation, "__name__"):
        return str(annotation.__name__)
    return str(annotation).split(".")[-1] if "." in str(annotation) else str(annotation)


def generate_yaml_field_comments(cls: type[BaseModel]) -> list[str]:
    """从 Pydantic BaseModel 的 model_fields 生成字段 schema 注释行。

    输出格式（每行以 # 开头）::

        #   字段名 (类型): 描述

    Args:
        cls: Pydantic 模型类。

    Returns:
        注释行列表。
    """
    lines: list[str] = []
    for field_name, field_info in cls.model_fields.items():
        description = _get_field_description(field_info)
        type_name = _type_display_name(field_info.annotation)
        if description:
            lines.append(f"#   {field_name} ({type_name}): {description}")
        else:
            lines.append(f"#   {field_name} ({type_name})")
    return lines


def generate_config_example_yaml() -> str:
    """从配置模型内省 + 示例数据生成 config.example.yaml 内容。

    Returns:
        完整的 ``config.example.yaml`` 文件内容字符串。
    """
    lines: list[str] = [
        "# ai_agent_companion 功能配置模板",
        "#",
        "# 用法：复制为 config.yaml 并按需调整；或运行 `pixi run setup` 交互式生成。",
        "# 分层加载链（优先级从低到高）：代码默认值 < .env < 进程环境变量；",
        "# 本文件承载结构化配置（llm 模型注册表 / 角色绑定 / 多模态回退链），",
        "# 与 env 面不重叠。",
        "#",
        "# 所有配置键必须 lowercase_with_underscores。",
        "# 敏感信息（API Key、密码）不写在此文件，统一走 .env（条目经",
        "# api_key_env 指向环境变量名）。",
        "#",
        "# 本文件由 `pixi run gen-examples` 从 LlmModelEntry / LlmConfig /",
        "# MultimodalFallbackConfig 自动生成。",
        "# 不要手动编辑——请修改配置类的 Field(description=...) 后重新生成。",
        "",
        "# ===========================================================================",
        "# llm — LLM 模型配置列表（多 provider OpenAI 兼容注册表）",
        "# 每个 model 定义一个可调用的端点；api_key_env 指向 .env 中的变量名",
        "# （本地免鉴权端点可留空）；roles 声明该条目承接的角色。",
        "# LlmModelEntry 字段 schema:",
    ]

    lines.extend(generate_yaml_field_comments(LlmModelEntry))
    lines.extend(
        [
            f"#   角色值域: {', '.join(member.value for member in ModelRole)}",
            "# ===========================================================================",
            "llm:",
            "  models:",
        ]
    )

    for model in LlmConfig.default_models():
        lines.append("    - name: " + _yaml_quote(model["name"]))
        lines.append(f"      provider: {_yaml_quote(model['provider'])}")
        lines.append(f"      base_url: {_yaml_quote(model['base_url'])}")
        api_key_env = model["api_key_env"]
        if api_key_env:
            lines.append(f"      api_key_env: {_yaml_quote(api_key_env)}")
        else:
            lines.append('      api_key_env: ""  # 本地免鉴权端点')
        lines.append(f"      model: {_yaml_quote(model['model'])}")
        roles = model.get("roles", [])
        if roles:
            lines.append(f"      roles: [{', '.join(_yaml_quote(r) for r in roles)}]")
        else:
            lines.append("      roles: []")
        concurrency = model.get("concurrency")
        if concurrency is not None:
            lines.append(f"      concurrency: {concurrency}")
        if model.get("multimodal"):
            lines.append("      multimodal: true")

    lines.extend(
        [
            "",
            "  # 显式角色绑定（角色 → 条目名；优先于条目自身 roles 声明）。",
            "  # 示例（当前示例条目的 roles 声明已覆盖全部角色，无需显式绑定）：",
            '  # role_bindings:',
            '  #   chat: "main-chat"',
            '  role_bindings: {}',
            "",
            "  # ===========================================================================",
            "  # multimodal_fallback — look_at 角色非多模态时的回退链",
            "  # （本地视觉端点 → 远程多模态端点，本地优先；两级均可选）",
            "  # MultimodalFallbackConfig 字段 schema:",
        ]
    )
    lines.extend(generate_yaml_field_comments(MultimodalFallbackConfig))
    lines.extend(
        [
            "  # ===========================================================================",
            "  multimodal_fallback:",
            "    local:",
            '      name: "local-vision-fallback"',
            '      base_url: "http://127.0.0.1:8001/v1"',
            '      api_key_env: ""  # 本地免鉴权端点',
            '      model: "qwen2.5-vl-7b"',
            "      multimodal: true",
            "    remote:",
            '      name: "remote-vision-fallback"',
            '      base_url: "https://api.example.com/v1"',
            '      api_key_env: "REMOTE_VISION_API_KEY"',
            '      model: "glm-4.6v"',
            "      multimodal: true",
        ]
    )

    return "\n".join(lines) + "\n"


__all__ = [
    "_yaml_quote",
    "generate_yaml_field_comments",
    "generate_config_example_yaml",
]
