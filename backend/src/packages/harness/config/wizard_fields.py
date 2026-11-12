"""Setup Wizard — 字段定义与值类型判断。

提供:

    - ``EnvField``: 单个 .env 配置字段 dataclass
    - ``_is_sensitive_key``: 判断 key 是否为敏感字段
    - ``_is_bool_value``: 判断值是否为布尔型
    - ``_is_numeric_value``: 判断值是否为纯数字
    - ``stringify_env_value``: 将值转换为 .env 字符串格式

依赖 ``wizard_ui._FIELD_LABELS`` 提供字段中文标签映射。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.harness.config.wizard_ui import _FIELD_LABELS

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 敏感字段关键词（小写匹配）
_SENSITIVE_PARTS: tuple[str, ...] = ("password", "secret", "token", "api_key")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class EnvField:
    """单个 .env 配置字段。

    Attributes:
        key: 环境变量名（大写下划线）。
        default: 模板中的默认值（可能为空字符串）。
        comment: 模板中该字段上方的注释（用于辅助说明）。
    """

    key: str
    default: str
    comment: str = ""

    @property
    def is_sensitive(self) -> bool:
        """是否为敏感字段（密码、密钥等）。"""
        return _is_sensitive_key(self.key)

    @property
    def field_type(self) -> str:
        """推断字段类型: password / boolean / number / text。"""
        if self.is_sensitive:
            return "password"
        if _is_bool_value(self.default):
            return "boolean"
        if _is_numeric_value(self.default):
            return "number"
        return "text"

    @property
    def label(self) -> str:
        """字段的中文标签（用于 UI 展示）。"""
        return _FIELD_LABELS.get(self.key, self.key)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    """判断 key 是否为敏感字段。

    Args:
        key: 环境变量名。

    Returns:
        命中敏感关键词（password/secret/token/api_key，小写匹配）返回 True。
    """
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _is_bool_value(value: str) -> bool:
    """判断值是否为布尔型（"true"/"false"，大小写不敏感）。

    Args:
        value: 模板默认值字符串。

    Returns:
        布尔字面量返回 True。
    """
    return value.lower() in ("true", "false")


def _is_numeric_value(value: str) -> bool:
    """判断值是否为纯数字（含负数）。

    Args:
        value: 模板默认值字符串。

    Returns:
        纯数字返回 True（空串返回 False）。
    """
    if not value:
        return False
    return value.lstrip("-").isdigit()


def stringify_env_value(value: str | int | bool | float) -> str:
    """将值转换为 .env 字符串格式。

    布尔值转为 ``"true"``/``"false"``（小写），其他类型使用 ``str()``。

    Args:
        value: 原始值（可能来自 JSON body 的 int/bool/string）。

    Returns:
        .env 兼容的字符串值。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
