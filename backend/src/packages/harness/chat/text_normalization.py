"""chat 域检测前文本归一——注入守卫与合规守卫共用的纯函数 Unit。

归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除全部格式类不可见
字符（Unicode ``Cf`` 类别全族 + 变体选择符 U+FE00–FE0F；``Mn`` 组合附加符
保留不动）→ 空白折叠为单空格。调用方只在**归一副本**上做规则匹配与命中
留证，原文始终由调用方持有（会话回合 / 包裹内容保真不受影响）。

两个守卫共用本函数的目的：任何检测面（入站注入信号 / 人格输出助手腔）
都不会被全角字符、不可见格式类插入或多空格变形绕过，且留证片段与匹配
文本同源。本模块零 I/O、零依赖（标准库 unicodedata only）。
"""

from __future__ import annotations

import unicodedata

__all__ = ["ZERO_WIDTH_CHARS", "normalize_for_matching"]

#: 常见零宽字符样例（U+200B / U+200C / U+200D / FEFF）——实现按 Unicode
#: ``Cf`` 类别全族剔除，不限于本表；保留常量供文档与调用方参考。
ZERO_WIDTH_CHARS: str = "\u200b\u200c\u200d\ufeff"

#: 变体选择符区间（U+FE00–U+FE0F；Unicode 类别为 ``Mn``，需与组合附加符区分）。
_VARIATION_SELECTOR_START: str = "\ufe00"
_VARIATION_SELECTOR_END: str = "\ufe0f"


def _is_invisible_format(char: str) -> bool:
    """判定字符是否属于检测前应剔除的不可见格式类。

    剔除面为 Unicode ``Cf`` 类别（零宽 / 软连字符 / word joiner / 方向标记
    等全部格式控制符）与变体选择符 U+FE00–FE0F；``Mn`` 组合附加符不在剔除
    面内（重音文本保持可读与可匹配）。

    Args:
        char: 待判定的单个字符。

    Returns:
        True = 不可见格式类（归一链剔除），False = 保留。
    """
    return unicodedata.category(char) == "Cf" or (
        _VARIATION_SELECTOR_START <= char <= _VARIATION_SELECTOR_END
    )


def normalize_for_matching(text: str) -> str:
    """构造检测用的归一副本（不修改原文，只供规则匹配与命中留证）。

    归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除全部格式类
    不可见字符（Unicode ``Cf`` 类别全族 + 变体选择符 U+FE00–FE0F；``Mn``
    组合附加符保留）→ 空白折叠为单空格。原文仍由调用方持有（会话回合 /
    包裹内容保真），本副本只用于检测。

    Args:
        text: 待检测的原始文本。

    Returns:
        归一副本（格式类不可见字符已剔除、空白已折叠；空输入返回空串）。
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(char for char in folded if not _is_invisible_format(char))
    return " ".join(visible.split())
