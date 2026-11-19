"""chat 域检测前文本归一——注入守卫与合规守卫共用的纯函数 Unit。

归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除零宽字符
（U+200B / U+200C / U+200D / FEFF）→ 空白折叠为单空格。调用方只在
**归一副本**上做规则匹配与命中留证，原文始终由调用方持有（会话回合 /
包裹内容保真不受影响）。

两个守卫共用本函数的目的：任何检测面（入站注入信号 / 人格输出助手腔）
都不会被全角字符、零宽插入或多空格变形绕过，且留证片段与匹配文本同源。
本模块零 I/O、零依赖（标准库 unicodedata only）。
"""

from __future__ import annotations

import unicodedata

__all__ = ["ZERO_WIDTH_CHARS", "normalize_for_matching"]

#: 检测前需要剔除的零宽字符（不可见字符可插入关键词中间绕过字面规则）。
ZERO_WIDTH_CHARS: str = "\u200b\u200c\u200d\ufeff"


def normalize_for_matching(text: str) -> str:
    """构造检测用的归一副本（不修改原文，只供规则匹配与命中留证）。

    归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除零宽字符
    （U+200B / U+200C / U+200D / FEFF）→ 空白折叠为单空格。原文仍由调用
    方持有（会话回合 / 包裹内容保真），本副本只用于检测。

    Args:
        text: 待检测的原始文本。

    Returns:
        归一副本（零宽字符已剔除、空白已折叠；空输入返回空串）。
    """
    folded = unicodedata.normalize("NFKC", text)
    without_zero_width = folded.translate(str.maketrans("", "", ZERO_WIDTH_CHARS))
    return " ".join(without_zero_width.split())
