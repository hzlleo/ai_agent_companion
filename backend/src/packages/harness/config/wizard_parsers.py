"""Setup Wizard — 模板解析（.env / YAML / JSON）。

提供三个模板解析函数:

    - ``parse_env_template``: 解析 .env.example，提取 ``EnvField`` 列表
    - ``parse_yaml_template``: 解析 config.example.yaml，返回顶层字典
    - ``parse_json_template``: 解析 extensions_config.example.json，返回顶层字典

依赖 ``wizard_fields.EnvField`` 作为 .env 解析结果的载体。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import yaml

from packages.harness.config.wizard_fields import EnvField

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# .env KEY=value 行匹配（大写字母 + 数字 + 下划线）
_ENV_KEY_PATTERN: re.Pattern[str] = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


# ---------------------------------------------------------------------------
# 模板解析
# ---------------------------------------------------------------------------


def parse_env_template(path: Path) -> list[EnvField]:
    """解析 .env.example 模板，提取配置字段列表。

    解析规则:

        - 匹配 ``KEY=value`` 行（key 为大写字母+数字+下划线）
        - 跟踪每个 key 上方最近的注释行（# 开头）
        - 分隔线（=== / ---）和空行重置注释跟踪
        - 保持文件中的原始顺序

    Args:
        path: .env.example 文件路径。

    Returns:
        按文件顺序排列的 EnvField 列表。

    Raises:
        FileNotFoundError: 模板文件不存在。
    """
    fields: list[EnvField] = []
    pending_comment = ""

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        # 空行重置注释
        if not stripped:
            pending_comment = ""
            continue

        # 注释行
        if stripped.startswith("#"):
            comment_text = stripped.lstrip("#").strip()
            # 分隔线（仅由 = 或 - 组成）重置注释
            if comment_text and not set(comment_text) <= {"=", "-"}:
                pending_comment = comment_text
            else:
                pending_comment = ""
            continue

        # KEY=value 行
        match = _ENV_KEY_PATTERN.match(stripped)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            fields.append(
                EnvField(
                    key=key,
                    default=value,
                    comment=pending_comment,
                )
            )
            pending_comment = ""

    return fields


def parse_yaml_template(path: Path) -> dict[str, object]:
    """解析 config.example.yaml 模板，返回顶层字典结构。

    用于验证 YAML 语法正确性及展示给用户审阅。config.yaml 中的配置项
    （LLM 模型注册表 / 角色绑定 / 多模态回退链）为结构化数据，不做逐
    字段交互式编辑——所有用户可配置的基础设施参数均在 .env 中。

    Args:
        path: config.example.yaml 文件路径。

    Returns:
        YAML 解析后的顶层字典。

    Raises:
        FileNotFoundError: 模板文件不存在。
        ValueError: YAML 顶层结构不是字典。
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {path}: expected dict, got {type(data)}")
    return cast(dict[str, object], data)


def parse_json_template(path: Path) -> dict[str, object]:
    """解析 extensions_config.example.json 模板，返回顶层字典结构。

    extensions_config.json 定义 LLM 工具（function calling schema），
    通常不需要用户修改，直接从模板复制。

    Args:
        path: extensions_config.example.json 文件路径。

    Returns:
        JSON 解析后的顶层字典。

    Raises:
        FileNotFoundError: 模板文件不存在。
        ValueError: JSON 顶层结构不是字典。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid JSON structure in {path}: expected dict, got {type(data)}")
    return cast(dict[str, object], data)
