"""Setup Wizard — UI 辅助函数与颜色常量。

从 ``BaseSettings`` 子类的 ``Field(description=...)`` 内省出 env-var →
中文标签映射，并提供终端 ANSI 颜色码与一组 CLI 输出辅助函数
（``_print_header`` / ``_print_step`` / ``_print_info`` /
``_print_success`` / ``_print_error``）。

关于 ``print()`` 的使用（约束合法例外）:
    本模块的 ``print()`` 输出均为 CLI 交互式输出，面向终端用户，
    **非 runtime 日志**。后端服务运行时不加载向导主流程，因此不适用
    「禁止 print()」约束。如需在服务运行时记录日志，应使用 ``loguru``。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings

from packages.harness.config.models import (
    DatabaseConfig,
    LlmEndpointConfig,
    LogConfig,
    PoolsConfig,
    ServerConfig,
    Settings,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 参与内省的 BaseSettings 子配置类（顺序决定优先级：子配置在前，
# Settings 聚合器在后——聚合器仅贡献 env 字段）
_SETTINGS_CLASSES: tuple[type[BaseSettings], ...] = (
    ServerConfig,
    DatabaseConfig,
    LogConfig,
    PoolsConfig,
    LlmEndpointConfig,
    Settings,
)


# ---------------------------------------------------------------------------
# 字段标签内省
# ---------------------------------------------------------------------------


def _build_field_labels() -> dict[str, str]:
    """从 ``BaseSettings`` 子类的 ``Field(description=...)`` 内省 env-var → 标签映射。

    env-var 名 = ``env_prefix + field_name.upper()``；首次出现优先（子配置
    覆盖 Settings 聚合器同名项）；无 description 的字段（如嵌套子配置
    实例）被跳过。

    Returns:
        ``{env_var_name: 中文标签}`` 映射（仅 UI 展示用）。
    """
    labels: dict[str, str] = {}
    for cls in _SETTINGS_CLASSES:
        env_prefix = cls.model_config.get("env_prefix", "")
        for field_name, field_info in cls.model_fields.items():
            if not field_info.description:
                continue
            env_name = f"{env_prefix}{field_name.upper()}"
            if env_name not in labels:
                labels[env_name] = field_info.description
    return labels


# 字段中文标签映射（从 BaseSettings ``Field(description=...)`` 内省，仅 UI 展示用）
_FIELD_LABELS: dict[str, str] = _build_field_labels()


# ---------------------------------------------------------------------------
# ANSI 颜色
# ---------------------------------------------------------------------------


class _Color:
    """ANSI 颜色码（终端输出用）。"""

    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


# ---------------------------------------------------------------------------
# CLI 输出辅助
# ---------------------------------------------------------------------------


def _print_header() -> None:
    """打印向导头部。"""
    print(f"\n{_Color.CYAN}{_Color.BOLD}{'=' * 60}")
    print("  ai_agent_companion — 配置向导")
    print(f"{'=' * 60}{_Color.RESET}\n")


def _print_step(name: str) -> None:
    """打印步骤标题。

    Args:
        name: 步骤名称。
    """
    print(f"\n{_Color.CYAN}{_Color.BOLD}▶ {name}{_Color.RESET}")


def _print_info(msg: str) -> None:
    """打印提示信息。

    Args:
        msg: 提示文本。
    """
    print(f"{_Color.YELLOW}ℹ  {msg}{_Color.RESET}")


def _print_success(msg: str) -> None:
    """打印成功信息。

    Args:
        msg: 成功文本。
    """
    print(f"\n{_Color.GREEN}✓ {msg}{_Color.RESET}")


def _print_error(msg: str) -> None:
    """打印错误信息。

    Args:
        msg: 错误文本。
    """
    print(f"\n{_Color.RED}✗ {msg}{_Color.RESET}")
