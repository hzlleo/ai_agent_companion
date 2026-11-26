"""chat 域检测前文本归一——注入守卫与合规守卫共用的纯函数 Unit。

归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除**默认可忽略
码点**（Unicode ``Default_Ignorable_Code_Point`` **属性全族**：软连字符 /
方向标记 / Hangul 填充符 / 变体选择符 / 音乐与速记格式控制符 / 标签字符 /
未赋值但具有可忽略属性的码点段，另含 ``Cf`` 全类零宽格式控制符）→ 空白
折叠为单空格。调用方只在**归一副本**上做规则匹配与命中留证，原文始终由
调用方持有（会话回合 / 包裹内容保真不受影响）。

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

#: Unicode ``Default_Ignorable_Code_Point`` 属性全族的码点区间（Unicode
#: 15 DerivedCoreProperties 口径）。覆盖软连字符 / 阿拉伯字母标记 / Khmer
#: 固有元音 / 蒙古文自由变体选择符与元音分隔符 / 零宽格式族 / Hangul
#: 填充符 / 变体选择符 / 音乐与速记格式控制符 / 标签字符，以及 U+FFF0–
#: FFF8 与 U+E0000–E0FFF 中未赋值但声明可忽略的码点。区间外字符保留。
_DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _is_default_ignorable(char: str) -> bool:
    """判定字符是否属于检测前应剔除的默认可忽略码点。

    剔除面：Unicode ``Default_Ignorable_Code_Point`` 属性全族（区间表
    :data:`_DEFAULT_IGNORABLE_RANGES`，含未赋值但声明可忽略的码点段）+
    ``Cf`` 全类（零宽格式控制符是零视觉噪声的超集面）。可见组合附加符
    不在剔除面内（重音文本保持可读与可匹配）。

    Args:
        char: 待判定的单个字符。

    Returns:
        True = 默认可忽略（归一链剔除），False = 保留。
    """
    if unicodedata.category(char) == "Cf":
        return True
    code = ord(char)
    return any(start <= code <= end for start, end in _DEFAULT_IGNORABLE_RANGES)


def normalize_for_matching(text: str) -> str:
    """构造检测用的归一副本（不修改原文，只供规则匹配与命中留证）。

    归一链（锁定）：Unicode NFKC（全角 / 兼容字符折叠）→ 剔除默认可忽略
    码点（``Default_Ignorable_Code_Point`` 属性全族 + ``Cf`` 全类）→ 空白
    折叠为单空格。原文仍由调用方持有（会话回合 / 包裹内容保真），本副本
    只用于检测。

    Args:
        text: 待检测的原始文本。

    Returns:
        归一副本（默认可忽略码点已剔除、空白已折叠；空输入返回空串）。
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(char for char in folded if not _is_default_ignorable(char))
    return " ".join(visible.split())
