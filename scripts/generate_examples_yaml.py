#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/config.example.yaml`` 生成。

从 ``LlmModelEntry`` / ``LlmConfig`` / ``RoutePolicyModel`` /
``RouteConditionModel`` / ``TimeWindowModel`` / ``MultimodalFallbackConfig`` /
``SearchConfig`` / ``SearchProviderEntry`` / ``WebSearchSettings`` 内省
字段 schema（Field description）+ 代码默认值（单一真相源）生成
``config.example.yaml`` 内容。
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
    SearchConfig,
    SearchProviderEntry,
    WebSearchSettings,
)
from packages.harness.llm.route_policy_models import (
    RouteConditionModel,
    RoutePolicyModel,
    TimeWindowModel,
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


def _yaml_scalar(value: object) -> str:
    """渲染 YAML 标量（布尔值转小写字面量，字符串统一加引号）。

    Args:
        value: 字段默认值（bool / int / float / str）。

    Returns:
        YAML 标量文本。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _yaml_quote(value)
    return str(value)


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
        if origin is tuple and args and args[-1] is Ellipsis:
            inner = ", ".join(_type_display_name(arg) for arg in args[:-1])
            return f"tuple[{inner}, ...]"
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


def _search_section_lines() -> list[str]:
    """生成 ``search`` 段示例行（provider 注册表 + web_search 运营子块）。

    provider 示例值经 :class:`SearchProviderEntry` 构造物化（缺省
    api_key_env / model 指向各 kind 的登记值），运营子块取
    :class:`WebSearchSettings` 的字段代码默认——两处都不读进程环境，
    产物与开发者 shell 无关。

    Returns:
        含分节注释与示例值的行列表（顶层段缩进已就位）。
    """
    lines: list[str] = [
        "",
        "# ===========================================================================",
        "# search — 搜索 provider 注册表 + web_search 运营子块（默认关闭）",
        "# providers 列表序 = failover 尝试序（bocha 主 / zhipu 兜底）；",
        "# 条目只携带 api_key_env 变量名，密钥本值永不落 yaml。",
        "# SearchConfig 字段 schema:",
    ]
    lines.extend(generate_yaml_field_comments(SearchConfig))
    lines.extend(["# SearchProviderEntry 字段 schema:", *generate_yaml_field_comments(SearchProviderEntry)])
    lines.extend(["# WebSearchSettings 字段 schema:", *generate_yaml_field_comments(WebSearchSettings)])
    lines.extend(
        [
            "# ===========================================================================",
            "search:",
            "  providers:",
        ]
    )
    for name, kind in (("bocha-main", "bocha"), ("zhipu-fallback", "zhipu")):
        entry: dict[str, object] = SearchProviderEntry(name=name, kind=kind).model_dump()
        # base_url 被模型校验物化为 kind 官方缺省；示例按「空=按 kind 缺省」
        # 语义省略该键，缺省表仍是唯一真源。
        entry.pop("base_url", None)
        for index, key in enumerate(entry):
            prefix = "    - " if index == 0 else "      "
            lines.append(f"{prefix}{key}: {_yaml_scalar(entry[key])}")
    lines.append("  web_search:")
    for field_name, field_info in WebSearchSettings.model_fields.items():
        if field_info.default is None:
            continue
        lines.append(f"    {field_name}: {_yaml_scalar(field_info.default)}")
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
        "# 本文件承载结构化配置（llm 模型注册表 / 角色绑定 / 多模态回退链 /",
        "# search 搜索 provider 注册表），与 env 面不重叠。",
        "#",
        "# 所有配置键必须 lowercase_with_underscores。",
        "# 敏感信息（API Key、密码）不写在此文件，统一走 .env（条目经",
        "# api_key_env 指向环境变量名）。",
        "#",
        "# 本文件由 `pixi run gen-examples` 从 LlmModelEntry / LlmConfig /",
        "# RoutePolicyModel / MultimodalFallbackConfig / SearchConfig /",
        "# SearchProviderEntry / WebSearchSettings 自动生成。",
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

    bindings = LlmConfig.default_role_bindings()
    lines.extend(
        [
            "",
            "  # 显式角色绑定（角色 → 条目名；优先于条目自身 roles 声明）。",
            "  # 注册表五角色由条目 roles 声明承接；此处仅绑定条件路由策略的",
            "  # 目标角色（chat_night → 夜间条目）。",
            '  # 示例：',
            '  # role_bindings:',
            '  #   chat: "main-chat"',
        ]
    )
    if bindings:
        lines.append("  role_bindings:")
        for role, entry_name in bindings.items():
            lines.append(f"    {role}: {_yaml_quote(entry_name)}")
    else:
        lines.append("  role_bindings: {}")

    lines.extend(
        [
            "",
            "  # ===========================================================================",
            "  # route_policies — 条件路由策略（原角色在条件命中时改由目标角色条目",
            "  # 承接；默认空列表 = 零行为变更）。条件维度：亲密度区间 / 关系摘要",
            "  # 可用性 / 时段（IANA 时区，end<=start 视为跨午夜）/ 对话类型（用途名）。",
            "  # 示例：夜间 chat → chat_night（Asia/Shanghai）。chat_night 须在",
            "  # models 中有条目并经 role_bindings 绑定（示例已绑定到 chat-night）。",
            "  # RoutePolicyModel 字段 schema:",
        ]
    )
    lines.extend(generate_yaml_field_comments(RoutePolicyModel))
    lines.extend(["  # RouteConditionModel 字段 schema:", *generate_yaml_field_comments(RouteConditionModel)])
    lines.extend(["  # TimeWindowModel 字段 schema:", *generate_yaml_field_comments(TimeWindowModel)])
    lines.extend(["  route_policies:"])
    for policy in LlmConfig.default_route_policies():
        lines.append(f"    - name: {_yaml_quote(policy['name'])}")
        lines.append(f"      source_role: {_yaml_quote(policy['source_role'])}")
        lines.append(f"      target_role: {_yaml_quote(policy['target_role'])}")
        lines.append(f"      priority: {policy['priority']}")
        lines.append("      condition:")
        window = policy["condition"]["time_window"]
        lines.append("        time_window:")
        lines.append(f"          start_hour: {window['start_hour']}")
        lines.append(f"          end_hour: {window['end_hour']}")
        lines.append(f"          timezone: {_yaml_quote(window['timezone'])}")
        fallback = "true" if policy["fallback_to_source"] else "false"
        lines.append(f"      fallback_to_source: {fallback}")
    lines.extend(
        [
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
    lines.extend(_search_section_lines())

    return "\n".join(lines) + "\n"


__all__ = [
    "_yaml_quote",
    "generate_yaml_field_comments",
    "generate_config_example_yaml",
]
