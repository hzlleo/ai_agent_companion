"""Setup Wizard — 配置 schema 构建与文件生成。

提供:

    - ``build_setup_schema``: 基于 .env.example 构建表单 schema
    - ``generate_env_content``: 基于模板和用户值生成 .env 文件内容
    - ``mask_sensitive``: 敏感值打码（用于摘要展示）
    - ``write_config_files``: 写入 .env / config.yaml / extensions_config.json

路径常量（``ENV_EXAMPLE`` / ``ENV_FILE`` 等）定义在 ``wizard.py``（facade）
中，本模块通过 ``import packages.harness.config.wizard as _w`` 延迟访问——
函数体内引用 ``_w.ENV_EXAMPLE`` 等属性发生在运行时（wizard.py 完成加载
后），以便测试 ``monkeypatch.setattr("packages.harness.config.wizard.ENV_FILE", ...)``
后本模块函数能感知到 patched 值。

这种「模块对象延迟属性访问」是处理 facade 循环依赖的标准模式：
``import`` 时仅捕获模块对象引用，不访问其属性，因此不会触发 ImportError。
"""

from __future__ import annotations

from pathlib import Path

import packages.harness.config.wizard as _w
from packages.harness.config.wizard_dtos import (
    SetupConfigFileEntry,
    SetupField,
    SetupSchema,
    SetupStep,
)
from packages.harness.config.wizard_fields import _is_sensitive_key
from packages.harness.config.wizard_parsers import _ENV_KEY_PATTERN, parse_env_template

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 步骤定义: (步骤名, 匹配前缀列表)
# 按配置域分组，与 .env.example 分节顺序一致
_STEP_DEFINITIONS: list[tuple[str, list[str]]] = [
    ("服务配置", ["SERVER_"]),
    ("数据库配置", ["DATABASE_"]),
    ("日志配置", ["LOG_"]),
    ("连接池配置", ["POOLS_"]),
    ("LLM 端点配置", ["LLM_"]),
    ("其他配置", ["ENV"]),
]


# ---------------------------------------------------------------------------
# Schema 构建
# ---------------------------------------------------------------------------


def build_setup_schema() -> SetupSchema:
    """基于 .example 文件动态构建配置表单 schema。

    读取 .env.example 模板，解析出所有配置字段，按步骤分组返回。

    Returns:
        :class:`SetupSchema`，包含 ``steps`` 列表和 ``config_files`` 信息。
        每个 step 包含 ``name`` 和 ``fields`` 列表，每个 field 包含
        ``key`` / ``label`` / ``type`` / ``default``。
    """
    fields = parse_env_template(_w.ENV_EXAMPLE)

    steps: list[SetupStep] = []
    for step_name, prefixes in _STEP_DEFINITIONS:
        step_fields: list[SetupField] = []
        for f in fields:
            if any(f.key.startswith(prefix) for prefix in prefixes):
                step_fields.append(
                    SetupField(
                        key=f.key,
                        label=f.label,
                        type=f.field_type,
                        default=f.default,
                    )
                )
        if step_fields:
            steps.append(SetupStep(name=step_name, fields=step_fields))

    return SetupSchema(
        steps=steps,
        config_files={
            "env": SetupConfigFileEntry(template=".env.example", output=".env"),
            "config": SetupConfigFileEntry(template="config.example.yaml", output="config.yaml"),
            "extensions": SetupConfigFileEntry(
                template="extensions_config.example.json",
                output="extensions_config.json",
            ),
        },
    )


# ---------------------------------------------------------------------------
# 生成与写入
# ---------------------------------------------------------------------------


def generate_env_content(template_path: Path, values: dict[str, str]) -> str:
    """基于模板和用户值生成 .env 文件内容。

    保留模板的原始结构（注释、分隔线、空行、格式），仅替换存在于
    ``values`` 中的 key 的值。

    Args:
        template_path: .env.example 模板路径。
        values: 用户输入的配置值字典。

    Returns:
        生成的 .env 文件内容字符串。
    """
    lines = template_path.read_text(encoding="utf-8").splitlines(keepends=True)
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        match = _ENV_KEY_PATTERN.match(stripped)
        if match:
            key = match.group(1)
            if key in values:
                newline = "\n" if line.endswith("\n") else ""
                result.append(f"{key}={values[key]}{newline}")
                continue
        result.append(line)

    output = "".join(result)
    if not output.endswith("\n"):
        output += "\n"
    return output


def mask_sensitive(values: dict[str, str]) -> dict[str, str]:
    """返回敏感值打码后的副本（用于摘要展示）。

    Args:
        values: 原始配置值字典。

    Returns:
        敏感字段值替换为 ``***`` 的副本，非敏感字段保持原值。
    """
    result: dict[str, str] = {}
    for key, val in values.items():
        if _is_sensitive_key(key) and val:
            result[key] = "***"
        else:
            result[key] = val
    return result


def write_config_files(
    values: dict[str, str],
    *,
    dry_run: bool = False,
    env_template: Path | None = None,
    config_template: Path | None = None,
    extensions_template: Path | None = None,
    env_output: Path | None = None,
    config_output: Path | None = None,
    extensions_output: Path | None = None,
) -> dict[str, str]:
    """写入三个配置文件。

    - .env: 基于 .env.example 模板，用 ``values`` 替换对应字段
    - config.yaml: 从 config.example.yaml 复制（结构化配置，用户可手动编辑）
    - extensions_config.json: 从 extensions_config.example.json 复制

    Args:
        values: 用户输入的 .env 配置值。
        dry_run: 为 True 时仅返回内容预览，不实际写入文件。
        env_template: 自定义 .env 模板路径（默认 ENV_EXAMPLE）。
        config_template: 自定义 config.yaml 模板路径（默认 CONFIG_EXAMPLE）。
        extensions_template: 自定义 extensions 模板路径（默认 EXTENSIONS_EXAMPLE）。
        env_output: 自定义 .env 输出路径（默认 ENV_FILE）。
        config_output: 自定义 config.yaml 输出路径（默认 CONFIG_FILE）。
        extensions_output: 自定义 extensions 输出路径（默认 EXTENSIONS_FILE）。

    Returns:
        ``{filename: content}`` 字典，包含三个文件的内容（用于预览/dry-run）。
    """
    # 路径常量通过 _w 延迟访问，以便测试 monkeypatch wizard.ENV_FILE 等后能感知
    env_tmpl = env_template or _w.ENV_EXAMPLE
    config_tmpl = config_template or _w.CONFIG_EXAMPLE
    ext_tmpl = extensions_template or _w.EXTENSIONS_EXAMPLE
    env_out = env_output or _w.ENV_FILE
    config_out = config_output or _w.CONFIG_FILE
    ext_out = extensions_output or _w.EXTENSIONS_FILE

    # 生成 .env 内容（模板替换）
    env_content = generate_env_content(env_tmpl, values)

    # config.yaml 和 extensions_config.json 从模板复制
    # （用户可配置的基础设施参数均在 .env 中，这两个文件为结构化配置）
    config_content = config_tmpl.read_text(encoding="utf-8")
    ext_content = ext_tmpl.read_text(encoding="utf-8")

    result: dict[str, str] = {
        ".env": env_content,
        "config.yaml": config_content,
        "extensions_config.json": ext_content,
    }

    if dry_run:
        return result

    env_out.write_text(env_content, encoding="utf-8")
    config_out.write_text(config_content, encoding="utf-8")
    ext_out.write_text(ext_content, encoding="utf-8")

    return result
