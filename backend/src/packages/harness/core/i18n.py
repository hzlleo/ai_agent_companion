"""后端错误消息多语言支持。

提供 ``get_message()`` 查找函数 + ``_resolve_locale`` locale 解析。
错误消息映射表 ``MESSAGES`` 独立在 ``i18n_messages`` 子模块（与
``ErrorCode`` 1:1 对齐，由回归测试锁定）。

``BusinessException`` 通过 ``register_message_resolver`` 回调关联到此处，
中间件解析 ``Accept-Language`` 后设置 ``locale_var`` contextvar，
异常处理器据此返回对应语言的错误消息。

locale 解析优先级：显式传入 ``locale`` 参数 → ``locale_var`` contextvar
（请求上下文）→ 默认 ``zh-CN``。
"""

from __future__ import annotations

import contextlib
from typing import Any

from loguru import logger

from packages.harness.core.context import locale_var
from packages.harness.core.enums import SUPPORTED_LOCALES, Locale
from packages.harness.core.errors import register_message_resolver
from packages.harness.core.i18n_messages import MESSAGES

__all__ = ["get_message", "resolve_locale"]

_DEFAULT_LOCALE: str = str(Locale.ZH_CN)


def resolve_locale(locale: str | None) -> str:
    """解析 locale：显式传入优先，否则从 ``locale_var`` contextvar 读取。

    服务层调用 ``get_message()`` 时通常已在请求上下文中，中间件已将
    ``Accept-Language`` 解析结果写入 ``locale_var``。本函数让
    ``get_message()`` 默认读取该 contextvar，调用方无需显式传 locale。

    Args:
        locale: 显式传入的 locale；非 None 时直接返回（未知值原样透传，
            由 ``get_message`` 的表回退兜底）。

    Returns:
        最终使用的 locale 字符串；contextvar 未设置时回退到 ``_DEFAULT_LOCALE``。
    """
    if locale is not None:
        return locale
    try:
        resolved = locale_var.get()
    except LookupError:
        resolved = _DEFAULT_LOCALE
    except Exception as exc:  # defensive: 任何异常都不应阻塞消息查找
        logger.opt(exception=True).debug(
            "locale_var 读取异常，回退到默认 locale={}: {}", _DEFAULT_LOCALE, exc
        )
        resolved = _DEFAULT_LOCALE
    if resolved != _DEFAULT_LOCALE and resolved not in SUPPORTED_LOCALES:
        logger.debug("未知 locale={}，回退到默认 locale={}", resolved, _DEFAULT_LOCALE)
        resolved = _DEFAULT_LOCALE
    return resolved


def get_message(key: str, locale: str | None = None, **kwargs: Any) -> str:
    """查找消息文本，支持占位符插值。

    ``locale`` 默认为 ``None``，此时从 ``locale_var`` contextvar 读取当前
    请求 locale。``locale_var`` 未设置时回退到 ``_DEFAULT_LOCALE``
    （``zh-CN``）。调用方仍可显式传 ``locale`` 覆盖（如离线脚本 / 后台任务）。

    查找顺序：``MESSAGES``（ErrorCode 体系）→ 未命中返回 key 本身
    （不抛异常，便于日志降级）。locale 未命中时回退 ``zh-CN``，仍缺失
    回退 key 本身。

    Args:
        key: 消息 key（如 ``"NOT_FOUND"``，与 ``ErrorCode`` 枚举值 1:1 对齐）。
        locale: 语言代码（``"zh-CN"`` / ``"en"``）；``None`` 时从 ``locale_var`` 读取。

    **kwargs: 占位符参数（如 ``name="test"`` 替换 ``{name}``）。

    Returns:
        对应语言的消息文本；key 不存在时返回 key 本身。
    """
    resolved_locale = resolve_locale(locale)
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    text = entry.get(resolved_locale) or entry.get(_DEFAULT_LOCALE) or key
    if kwargs:
        with contextlib.suppress(KeyError, IndexError):
            text = text.format(**kwargs)
    return text


# 注册消息解析器到 BusinessException——使 core 层可通过 code + details
# 自动派生 i18n 消息，无需调用方显式传 message。依赖方向：
# core.i18n → core.errors（同层回调注册，合法）。
register_message_resolver(lambda code, details: get_message(code, **(details or {})))
