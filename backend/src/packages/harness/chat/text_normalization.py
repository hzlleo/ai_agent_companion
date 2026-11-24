"""chat 域检测前文本归一——注入守卫与合规守卫共用的纯函数 Unit。

归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除**默认可忽略
码点**（Unicode ``Default_Ignorable_Code_Point`` 的可见性等价集：``Cf``
全类 + 变体选择符 / 蒙古文自由变体选择符 / 组合用字形连接符等不可见
``Mn`` 族 + Hangul 填充符）→ 空白折叠为单空格。调用方只在**归一副本**上
做规则匹配与命中留证，原文始终由调用方持有（会话回合 / 包裹内容保真不受
影响）。

为什么不剔除全部 ``Mn``：组合附加符（如 U+0301）是可见字形的一部分，
剔除会让重音文本的匹配面与显示面脱节；只有「不产生字形、只影响渲染
选择」的 ``Mn`` 族属于默认可忽略面。Hangul 填充符（U+115F / U+1160 /
U+3164 / U+FFA0）不产生独立字形，同样是零视觉噪声——插入它们不应改变
检测结论。

两个守卫共用本函数的目的：任何检测面（入站注入信号 / 人格输出助手腔）
都不会被全角字符、不可见格式类插入或多空格变形绕过，且留证片段与匹配
文本同源。本模块零 I/O、零依赖（标准库 unicodedata only）。
"""

from __future__ import annotations

import unicodedata

__all__ = ["ZERO_WIDTH_CHARS", "normalize_for_matching"]

#: 常见零宽字符样例（U+200B / U+200C / U+200D / FEFF）——实现按 Unicode
#: 默认可忽略码点集剔除，不限于本表；保留常量供文档与调用方参考。
ZERO_WIDTH_CHARS: str = "\u200b\u200c\u200d\ufeff"

#: 不可见 ``Mn`` 码点区间（变体选择符 / 蒙古文自由变体选择符 / 变体
#: 选择符补充）。区间外 ``Mn``（如重音附加符）保留。
_INVISIBLE_MN_RANGES: tuple[tuple[int, int], ...] = (
    (0x180B, 0x180D),
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)

#: 单点不可见 ``Mn`` 码点（组合用字形连接符——不产生字形，仅影响渲染组合）。
_INVISIBLE_MN_CHARS: frozenset[str] = frozenset("\u034f")

#: Hangul 填充符（无独立字形的占位字母——NFKC 前后形态均覆盖）。
_HANGUL_FILLERS: frozenset[str] = frozenset("\u115f\u1160\u3164\uffa0")


def _is_default_ignorable(char: str) -> bool:
    """判定字符是否属于检测前应剔除的默认可忽略码点。

    剔除面：Unicode ``Cf`` 全类（零宽 / 软连字符 / word joiner / 方向标记
    等格式控制符）+ 不可见 ``Mn`` 族（U+034F、U+180B–180D、U+FE00–FE0F、
    U+E0100–E01EF）+ Hangul 填充符。可见组合附加符不在剔除面内（重音文本
    保持可读与可匹配）。

    Args:
        char: 待判定的单个字符。

    Returns:
        True = 默认可忽略（归一链剔除），False = 保留。
    """
    if unicodedata.category(char) == "Cf":
        return True
    if char in _INVISIBLE_MN_CHARS or char in _HANGUL_FILLERS:
        return True
    code = ord(char)
    return any(start <= code <= end for start, end in _INVISIBLE_MN_RANGES)


def normalize_for_matching(text: str) -> str:
    """构造检测用的归一副本（不修改原文，只供规则匹配与命中留证）。

    归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除默认可忽略
    码点（``Cf`` 全类 + 不可见 ``Mn`` 族 + Hangul 填充符）→ 空白折叠为
    单空格。原文仍由调用方持有（会话回合 / 包裹内容保真），本副本只用于
    检测。

    Args:
        text: 待检测的原始文本。

    Returns:
        归一副本（默认可忽略码点已剔除、空白已折叠；空输入返回空串）。
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(char for char in folded if not _is_default_ignorable(char))
    return " ".join(visible.split())
