#!/usr/bin/env python3
"""``.example`` 配置文件生成器——``backend/.env.example`` 生成。

从 :class:`BaseSettings` 子类的 ``model_fields`` 内省字段信息，按分节
生成 ``.env.example`` 内容。示例永不与模型漂移：字段增删 / 默认值 /
描述变更后重新运行 ``pixi run gen-examples`` 即同步。
"""

from __future__ import annotations

from generate_examples_helpers import (
    _get_env_var_name,
    _get_field_description,
    _is_scalar_env_field,
    _stringify_default,
)
from pydantic_settings import BaseSettings

from packages.harness.config.models import (
    DatabaseConfig,
    LlmEndpointConfig,
    LogConfig,
    PoolsConfig,
    ServerConfig,
    Settings,
)

# .env.example 分节标题: (section_title, config_classes)
_ENV_SECTIONS: list[tuple[str, list[type[BaseSettings]]]] = [
    ("SERVER — Web 服务配置", [ServerConfig]),
    ("DATABASE — 数据库连接配置（MySQL 8 权威 / SQLite 测试）", [DatabaseConfig]),
    ("LOG — 日志配置（三 sink：console + 文本 + JSON）", [LogConfig]),
    ("POOLS — 共享池配置（线程池 / HTTP 池 / LLM 信号量 / 熔断）", [PoolsConfig]),
    ("LLM — 默认端点 env 面（模型注册表经 config.yaml 配置）", [LlmEndpointConfig]),
]


def generate_env_section(
    title: str,
    classes: list[type[BaseSettings]],
    *,
    field_filter: str | None = None,
) -> list[str]:
    """生成 .env.example 的单个分节。

    输出格式::

        # =========================================================================== []
        # <title>
        # =========================================================================== []
        # <field description>
        <ENV_VAR>=<default>
        ...

    Args:
        title: 分节标题。
        classes: BaseSettings 子类列表。
        field_filter: 若提供，仅生成字段名与此值匹配的字段（用于 Settings.env）。

    Returns:
        分节文本行列表。
    """
    separator = "# " + "=" * 75
    lines: list[str] = [separator, f"# {title}", separator]

    for cls in classes:
        for field_name, field_info in cls.model_fields.items():
            if field_filter is not None and field_name != field_filter:
                continue
            if not _is_scalar_env_field(field_info):
                continue
            env_var = _get_env_var_name(field_name, cls)
            description = _get_field_description(field_info)
            default = _stringify_default(field_info.default, env_var)

            if description:
                lines.append(f"# {description}")
            lines.append(f"{env_var}={default}")
            lines.append("")  # 字段间空行

    return lines


def generate_env_example() -> str:
    """从 BaseSettings 子类内省生成 .env.example 内容。

    Returns:
        完整的 ``.env.example`` 文件内容字符串。
    """
    lines: list[str] = [
        "# ai_agent_companion backend 环境变量模板",
        "#",
        "# 用法：复制为 .env 并填写真实值；或运行 `pixi run setup` 交互式生成。",
        "# 分层加载链（优先级从低到高）：代码默认值 < .env < 进程环境变量；",
        "# config.yaml / extensions_config.json 承载结构化配置（llm 模型注册表 /",
        "# 角色绑定 / 多模态回退链 / 工具清单），与 env 面不重叠。",
        "#",
        "# 所有变量名必须 UPPERCASE_WITH_UNDERSCORES。",
        "#",
        "# 本文件由 `pixi run gen-examples` 从 packages/harness/config 的 Pydantic 配置类自动生成。",
        "# 不要手动编辑——请修改配置类的 Field(description=...) 后重新生成。",
        "",
    ]

    for title, classes in _ENV_SECTIONS:
        lines.extend(generate_env_section(title, classes))

    # 末尾 ENV 分节（Settings 类的 env 字段，无 env_prefix → ENV）
    # Settings 类包含嵌套子配置字段（server / database / ...），仅保留 env 字段
    lines.extend(generate_env_section("ENV — 运行环境标识", [Settings], field_filter="env"))

    # 去除尾部多余空行
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()

    return "\n".join(lines) + "\n"


__all__ = [
    "_ENV_SECTIONS",
    "generate_env_section",
    "generate_env_example",
]
