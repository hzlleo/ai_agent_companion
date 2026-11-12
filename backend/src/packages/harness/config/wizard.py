#!/usr/bin/env python3
"""ai_agent_companion — 配置向导（facade）。

交互式配置生成工具，基于 .env.example / config.example.yaml /
extensions_config.example.json 模板生成最终配置文件。

用法::

    pixi run setup                       # 交互式配置
    pixi run setup -- --dry-run          # 预览不写入
    pixi run setup -- --non-interactive  # 使用默认值（CI/自动化）

本模块是 facade——仅保留 CLI 主流程（``run_wizard`` / ``_prompt_field`` /
``_print_summary`` / ``main``）与路径常量。其余职责拆分到子模块:

    - ``wizard_ui``        — ANSI 颜色、``_print_*`` 输出辅助、``_FIELD_LABELS``
    - ``wizard_fields``    — ``EnvField`` dataclass、``_is_sensitive_key`` /
      ``_is_bool_value`` / ``_is_numeric_value`` / ``stringify_env_value``
    - ``wizard_parsers``   — ``parse_env_template`` / ``parse_yaml_template`` /
      ``parse_json_template``
    - ``wizard_generators``— ``build_setup_schema`` / ``generate_env_content`` /
      ``mask_sensitive`` / ``write_config_files`` / ``_STEP_DEFINITIONS``
    - ``wizard_dtos``      — ``SetupSchema`` / ``SetupField`` / ``SetupStep`` /
      ``SetupConfigFileEntry``

路径常量（``ENV_EXAMPLE`` / ``ENV_FILE`` 等）定义在本 facade 中，子模块
``wizard_generators`` 通过 ``import packages.harness.config.wizard as _w``
延迟访问，以便测试 ``monkeypatch.setattr("packages.harness.config.wizard.ENV_FILE", ...)``
后子模块函数能感知到 patched 值。

``wizard_generators`` 与本模块存在循环依赖（generators 需要 wizard 的路径
常量，wizard 需要 generators 的 ``write_config_files`` / ``mask_sensitive`` /
``_STEP_DEFINITIONS``）。因此本模块使用 ``import ... as _wg`` 延迟绑定模块
对象，函数体内通过 ``_wg.xxx`` 访问属性——属性访问发生在运行时（两模块
均已加载完毕），不会触发 ImportError。

关于 ``print()`` 的使用（约束合法例外）:
    本模块是 **CLI 工具**，通过 ``main()`` 入口被 ``pixi run setup`` 调用，
    所有 ``print()`` 输出均为 CLI 交互式输出，面向终端用户，**非 runtime
    日志**。后端服务运行时不会执行本模块主流程；如需在服务运行时记录
    日志，应使用 ``loguru`` logger。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

import packages.harness.config.wizard_generators as _wg
from packages.harness.config.wizard_fields import (
    EnvField,
    _is_numeric_value,
)
from packages.harness.config.wizard_parsers import (
    parse_env_template,
    parse_json_template,
    parse_yaml_template,
)
from packages.harness.config.wizard_ui import (
    _Color,
    _print_error,
    _print_header,
    _print_info,
    _print_step,
    _print_success,
)

# ---------------------------------------------------------------------------
# 路径常量 — 基于 __file__ 推导，不依赖 CWD
# wizard.py 位于 backend/src/packages/harness/config/wizard.py；
# 运行契约为 ``pixi run setup``（backend pixi 环境的 activation 已设置
# PYTHONPATH=$PIXI_PROJECT_ROOT/src，packages.harness.* 可直接导入）。
# ---------------------------------------------------------------------------

BACKEND_DIR: Path = Path(__file__).resolve().parents[4]

ENV_EXAMPLE: Path = BACKEND_DIR / ".env.example"
CONFIG_EXAMPLE: Path = BACKEND_DIR / "config.example.yaml"
EXTENSIONS_EXAMPLE: Path = BACKEND_DIR / "extensions_config.example.json"

ENV_FILE: Path = BACKEND_DIR / ".env"
CONFIG_FILE: Path = BACKEND_DIR / "config.yaml"
EXTENSIONS_FILE: Path = BACKEND_DIR / "extensions_config.json"

__all__ = [
    "BACKEND_DIR",
    "CONFIG_EXAMPLE",
    "CONFIG_FILE",
    "ENV_EXAMPLE",
    "ENV_FILE",
    "EXTENSIONS_EXAMPLE",
    "EXTENSIONS_FILE",
    "main",
    "run_wizard",
]


# ---------------------------------------------------------------------------
# CLI 交互
# ---------------------------------------------------------------------------


def _prompt_field(
    field: EnvField,
    current_value: str,
    non_interactive: bool,
    *,
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
) -> str:
    """交互式提示用户输入单个字段值。

    - 敏感字段使用 getpass（隐藏输入）
    - 布尔字段显示 Y/n 提示
    - 数字字段验证输入为数字
    - 空输入保留当前值/默认值

    Args:
        field: 字段元数据。
        current_value: 当前值（来自已有 .env 或模板默认值）。
        non_interactive: 为 True 时直接返回当前值，不提示。
        input_fn: 文本输入函数注入点（默认 ``input``；测试注入应答序列）。
        secret_fn: 敏感输入函数注入点（默认 ``getpass.getpass``；测试注入）。

    Returns:
        用户输入的新值或保留的当前值。
    """
    if non_interactive:
        return current_value

    read_line = input_fn if input_fn is not None else input
    read_secret = secret_fn if secret_fn is not None else getpass.getpass

    display = "***" if (field.is_sensitive and current_value) else current_value

    prompt_text = f"  {field.label} ({field.key})"
    if display:
        prompt_text += f" [当前: {display}]"
    prompt_text += ": "

    if field.is_sensitive:
        new_val = read_secret(prompt_text)
    elif field.field_type == "boolean":
        default_y = current_value.lower() == "true"
        hint = "Y/n" if default_y else "y/N"
        raw = read_line(f"{prompt_text} ({hint}): ").strip().lower()
        if not raw:
            return current_value
        return "true" if raw in ("y", "yes") else "false"
    else:
        new_val = read_line(prompt_text).strip()

    if not new_val:
        return current_value

    if field.field_type == "number" and not _is_numeric_value(new_val):
        _print_info(f"值必须是数字，已忽略输入，使用默认值: {current_value}")
        return current_value

    return new_val


def _print_summary(values: dict[str, str], fields: list[EnvField]) -> None:
    """打印配置摘要（敏感字段打码）。

    Args:
        values: 字段值映射。
        fields: 字段元数据列表（提供中文标签）。
    """
    print(f"\n{_Color.CYAN}{'─' * 50}")
    print("  配置摘要")
    print(f"{'─' * 50}{_Color.RESET}")

    field_map: dict[str, EnvField] = {f.key: f for f in fields}
    masked = _wg.mask_sensitive(values)

    for step_name, prefixes in _wg._STEP_DEFINITIONS:
        step_has_fields = False
        for key in values:
            if any(key.startswith(p) for p in prefixes):
                if not step_has_fields:
                    print(f"\n  [{step_name}]")
                    step_has_fields = True
                field = field_map.get(key)
                label = field.label if field else key
                print(f"    {label}: {masked[key]}")


def run_wizard(
    dry_run: bool = False,
    non_interactive: bool = False,
    *,
    answers: dict[str, str] | None = None,
    confirm: str = "y",
) -> None:
    """运行配置向导主流程。

    1. 读取 .env.example 模板解析字段
    2. 如已有 .env，以现有值作为默认值
    3. 按步骤交互式收集用户输入
    4. 展示配置摘要
    5. 确认后写入三个文件

    Args:
        dry_run: 为 True 时仅预览不写入。
        non_interactive: 为 True 时使用默认值不提示（CI/自动化场景）。
        answers: 注入式应答（测试驱动入口）。非 None 时即全量注入面——
            命中 key 用注入值（空串视为保留默认值），未命中 key 保留
            当前值，全程零交互。
        confirm: 保存确认应答（默认 "y"；测试注入 "n" 可走取消分支）。
    """
    _print_header()

    # 验证模板文件存在
    if not ENV_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {ENV_EXAMPLE}")
        sys.exit(1)
    if not CONFIG_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {CONFIG_EXAMPLE}")
        sys.exit(1)
    if not EXTENSIONS_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {EXTENSIONS_EXAMPLE}")
        sys.exit(1)

    # 解析模板（同时验证 YAML / JSON 模板语法）
    fields = parse_env_template(ENV_EXAMPLE)
    parse_yaml_template(CONFIG_EXAMPLE)
    parse_json_template(EXTENSIONS_EXAMPLE)
    _print_info(f"已加载 {len(fields)} 个配置项（模板: {ENV_EXAMPLE.name}）")

    # 加载已有 .env 值作为默认值
    existing_values: dict[str, str] = {}
    if ENV_FILE.exists():
        _print_info("检测到已有 .env 文件，将以现有值作为默认值。")
        existing_fields = parse_env_template(ENV_FILE)
        existing_values = {f.key: f.default for f in existing_fields}

    # 按步骤收集用户输入
    use_defaults = dry_run or non_interactive
    values: dict[str, str] = {}
    injected = dict(answers) if answers is not None else None

    for step_name, prefixes in _wg._STEP_DEFINITIONS:
        _print_step(step_name)

        for f in fields:
            if not any(f.key.startswith(p) for p in prefixes):
                continue
            current = existing_values.get(f.key, f.default)
            if injected is not None:
                values[f.key] = injected.get(f.key) or current
                continue
            new_val = _prompt_field(f, current, use_defaults)
            values[f.key] = new_val

    # 展示摘要
    _print_summary(values, fields)

    # 确认并保存
    if dry_run:
        _print_info("(--dry-run 模式，不写入文件)")
        preview = _wg.write_config_files(values, dry_run=True)
        for fname, content in preview.items():
            _print_info(f"\n--- {fname} (预览前 10 行) ---")
            for line in content.splitlines()[:10]:
                print(f"  {line}")
        return

    if not non_interactive and confirm not in ("", "y", "yes"):
        _print_info("已取消，未写入任何文件。")
        return

    # 写入文件
    _wg.write_config_files(values)
    _print_success("配置已保存。请运行服务前检查 config.yaml 的模型注册表。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI 入口：解析命令行参数并运行向导。"""
    parser = argparse.ArgumentParser(
        description="ai_agent_companion 配置向导",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  pixi run setup                       # 交互式配置\n"
            "  pixi run setup -- --dry-run          # 预览不写入\n"
            "  pixi run setup -- --non-interactive  # 使用默认值\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览将写入的内容，不实际写入文件",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="使用模板默认值，不交互式提示（适用于 CI/自动化场景）",
    )
    args = parser.parse_args()
    run_wizard(dry_run=args.dry_run, non_interactive=args.non_interactive)


if __name__ == "__main__":
    main()
