#!/usr/bin/env python3
"""ai_agent_companion — 配置向导（facade）。

交互式配置生成工具，基于 .env.example / config.example.yaml /
extensions_config.example.json 模板生成最终配置文件。

用法::

    pixi run setup                       # 交互式配置
    pixi run setup -- --dry-run          # 预览不写入
    pixi run setup -- --non-interactive  # 使用默认值（CI/自动化）

本模块是 facade——仅保留 CLI 主流程（``run_wizard`` / ``_prompt_field`` /
``_print_summary`` / ``main``）。其余职责拆分到子模块:

    - ``wizard_ui``        — ANSI 颜色、``_print_*`` 输出辅助、``_FIELD_LABELS``
    - ``wizard_fields``    — ``EnvField`` dataclass、敏感 / 布尔 / 数字值
      判定、``parse_bool_answer`` 严格布尔应答解析
    - ``wizard_parsers``   — ``parse_env_template`` / ``parse_yaml_template`` /
      ``parse_json_template``
    - ``wizard_generators``— ``build_setup_schema`` / ``generate_env_content`` /
      ``mask_sensitive`` / ``write_config_files`` / ``_STEP_DEFINITIONS``
    - ``wizard_dtos``      — ``SetupSchema`` / ``SetupField`` / ``SetupStep`` /
      ``SetupConfigFileEntry``
    - ``wizard_paths``     — 模板 / 输出文件路径常量（本 facade 与
      ``wizard_generators`` 共享的叶子模块——两模块间无循环依赖，
      均经模块对象限定访问路径属性，测试 monkeypatch 一次即全局生效）

布尔应答契约（交互提示与注入式应答共用 ``parse_bool_answer``）:
    严格接受 ``y`` / ``yes`` / ``true`` / ``n`` / ``no`` / ``false``
    （大小写不敏感）；非法输入不默认可 / 默认否——交互模式重新提示
    （连续 3 次无效按 ``n`` 处理并提示），注入模式按「未提供」处理
    （回退当前值）并告警。

覆写确认契约（config.yaml / extensions_config.json 已存在且与模板
内容相异时）:
    交互模式经终端提示确认；注入式应答模式读 ``confirm_overwrite``
    注入键（严格布尔字面量，缺省 / 非法 = 拒绝——跳过写入并提示）；
    ``--non-interactive`` 模式一律拒绝（不静默丢弃 registry_admin
    管理内容）。.env 不受此契约约束（保留既有合并语义）。

关于 ``print()`` 的使用（约束合法例外）:
    本模块是 **CLI 工具**，通过 ``main()`` 入口被 ``pixi run setup`` 调用，
    所有 ``print()`` / ``input()`` 输出均为 CLI 交互式输出，面向终端
    用户，**非 runtime 日志**。后端服务运行时不会执行本模块主流程；
    如需在服务运行时记录日志，应使用 ``loguru`` logger。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from packages.harness.config import wizard_paths
from packages.harness.config.wizard_fields import (
    EnvField,
    _is_numeric_value,
    parse_bool_answer,
)
from packages.harness.config.wizard_generators import (
    _STEP_DEFINITIONS,
    mask_sensitive,
    write_config_files,
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

#: 布尔提示的最大重试次数（连续无效输入达到上限后按 ``n`` 处理）。
_BOOL_PROMPT_MAX_ATTEMPTS = 3

__all__ = [
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
    - 布尔字段严格解析 y/yes/true/n/no/false（大小写不敏感）：空输入
      保留当前值；非法输入重新提示（连续 ``_BOOL_PROMPT_MAX_ATTEMPTS``
      次无效后按 ``n`` 处理并提示）
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
        for _attempt in range(_BOOL_PROMPT_MAX_ATTEMPTS):
            raw = read_line(f"{prompt_text} ({hint}): ")
            if not raw.strip():
                return current_value
            parsed = parse_bool_answer(raw)
            if parsed is not None:
                return "true" if parsed else "false"
            _print_info("请输入 y/yes/true（是）或 n/no/false（否）。")
        _print_info(f"连续 {_BOOL_PROMPT_MAX_ATTEMPTS} 次无效输入，按 n 处理。")
        return "false"
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
    masked = mask_sensitive(values)

    for step_name, prefixes in _STEP_DEFINITIONS:
        step_has_fields = False
        for key in values:
            if any(key.startswith(p) for p in prefixes):
                if not step_has_fields:
                    print(f"\n  [{step_name}]")
                    step_has_fields = True
                field = field_map.get(key)
                label = field.label if field else key
                print(f"    {label}: {masked[key]}")


def _confirm_overwrite_interactive(output: Path) -> bool:
    """交互式覆写确认（严格布尔解析，连续 3 次无效按 ``n`` 处理）。

    Args:
        output: 待覆写的管理面配置文件路径（提示中展示）。

    Returns:
        是否放行覆写。
    """
    for _attempt in range(_BOOL_PROMPT_MAX_ATTEMPTS):
        raw = input(f"确认覆写 {output.name}？(y/N): ")
        parsed = parse_bool_answer(raw)
        if parsed is not None:
            return parsed
        _print_info("请输入 y/yes/true（是）或 n/no/false（否）。")
    _print_info(f"连续 {_BOOL_PROMPT_MAX_ATTEMPTS} 次无效输入，按 n 处理。")
    return False


def _make_overwrite_confirm(
    answers: dict[str, str] | None,
    non_interactive: bool,
) -> Callable[[Path], bool]:
    """构造 write_config_files 的覆写确认通道（按运行模式分派）。

    - 注入式应答模式（``answers`` 非 None）：读 ``confirm_overwrite``
      注入键做严格布尔解析，缺省 / 非法值一律拒绝（fail-safe）；
    - 交互模式：终端提示确认（见 :func:`_confirm_overwrite_interactive`）；
    - ``--non-interactive``：一律拒绝（无终端可确认，不静默覆写
      registry_admin 管理内容）。

    Args:
        answers: 注入式应答字典（None = 非注入模式）。
        non_interactive: 是否非交互模式。

    Returns:
        覆写确认回调（收到目标路径，返回是否放行覆写）。
    """
    if answers is not None:
        injected = answers

        def _injected_confirm(output: Path) -> bool:
            raw = injected.get("confirm_overwrite", "")
            parsed = parse_bool_answer(raw) if raw else False
            if raw and parsed is None:
                _print_info(f"注入应答 confirm_overwrite 非法布尔值（{raw!r}），按未提供处理。")
                return False
            return parsed is True

        return _injected_confirm

    if non_interactive:

        def _decline_confirm(output: Path) -> bool:
            return False

        return _decline_confirm

    return _confirm_overwrite_interactive


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
    3. 按步骤交互式收集用户输入（布尔应答严格解析，见模块 docstring）
    4. 展示配置摘要
    5. 确认后写入三个文件（config.yaml / extensions_config.json 覆写
       既有相异内容前需经覆写确认通道，见模块 docstring）

    Args:
        dry_run: 为 True 时仅预览不写入。
        non_interactive: 为 True 时使用默认值不提示（CI/自动化场景）。
        answers: 注入式应答（测试驱动入口）。非 None 时即全量注入面——
            命中 key 用注入值（空串视为保留默认值；布尔字段非法值按
            未提供处理并告警），未命中 key 保留当前值，全程零交互。
            ``confirm_overwrite`` 注入键控制管理面文件覆写（默认拒绝）。
        confirm: 保存确认应答（默认 "y"；测试注入 "n" 可走取消分支）。
    """
    _print_header()

    # 验证模板文件存在
    if not wizard_paths.ENV_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {wizard_paths.ENV_EXAMPLE}")
        sys.exit(1)
    if not wizard_paths.CONFIG_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {wizard_paths.CONFIG_EXAMPLE}")
        sys.exit(1)
    if not wizard_paths.EXTENSIONS_EXAMPLE.exists():
        _print_error(f"模板文件不存在: {wizard_paths.EXTENSIONS_EXAMPLE}")
        sys.exit(1)

    # 解析模板（同时验证 YAML / JSON 模板语法）
    fields = parse_env_template(wizard_paths.ENV_EXAMPLE)
    parse_yaml_template(wizard_paths.CONFIG_EXAMPLE)
    parse_json_template(wizard_paths.EXTENSIONS_EXAMPLE)
    _print_info(f"已加载 {len(fields)} 个配置项（模板: {wizard_paths.ENV_EXAMPLE.name}）")

    # 加载已有 .env 值作为默认值
    existing_values: dict[str, str] = {}
    if wizard_paths.ENV_FILE.exists():
        _print_info("检测到已有 .env 文件，将以现有值作为默认值。")
        existing_fields = parse_env_template(wizard_paths.ENV_FILE)
        existing_values = {f.key: f.default for f in existing_fields}

    # 按步骤收集用户输入
    use_defaults = dry_run or non_interactive
    values: dict[str, str] = {}
    injected = dict(answers) if answers is not None else None

    for step_name, prefixes in _STEP_DEFINITIONS:
        _print_step(step_name)

        for f in fields:
            if not any(f.key.startswith(p) for p in prefixes):
                continue
            current = existing_values.get(f.key, f.default)
            if injected is not None:
                injected_raw = injected.get(f.key, "")
                if f.field_type == "boolean" and injected_raw:
                    parsed_bool = parse_bool_answer(injected_raw)
                    if parsed_bool is None:
                        _print_info(
                            f"注入应答为非法布尔值（{f.key}={injected_raw!r}），按未提供处理。"
                        )
                        values[f.key] = current
                    else:
                        values[f.key] = "true" if parsed_bool else "false"
                else:
                    values[f.key] = injected_raw or current
                continue
            new_val = _prompt_field(f, current, use_defaults)
            values[f.key] = new_val

    # 展示摘要
    _print_summary(values, fields)

    # 确认并保存
    if dry_run:
        _print_info("(--dry-run 模式，不写入文件)")
        preview = write_config_files(values, dry_run=True)
        for fname, content in preview.items():
            _print_info(f"\n--- {fname} (预览前 10 行) ---")
            for line in content.splitlines()[:10]:
                print(f"  {line}")
        return

    if not non_interactive and confirm not in ("", "y", "yes"):
        _print_info("已取消，未写入任何文件。")
        return

    # 写入文件（管理面文件覆写经确认通道）
    overwrite_confirm = _make_overwrite_confirm(injected, non_interactive)
    write_config_files(values, confirm_overwrite=overwrite_confirm)
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
