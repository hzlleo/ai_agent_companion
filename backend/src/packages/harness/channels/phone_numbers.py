"""通道层手机号 / 联系名归一（SmsForwarder 与 WhatsApp 共享纯函数面）。

第三方转发类通道（手机短信转发、WhatsApp 云 API）的发送者标识形态不受
本侧控制：手机端模板可能带 ``+86`` / 空格 / 连字符等展示形态，WhatsApp
云 API 的 ``wa_id`` 则是不带加号的纯数字国际号码。两侧的会话键与属主
清单匹配必须使用同一归一函数，否则同一联系人会在不同通道里出现互不
相认的键（属主清单失效 / 重投去重失效）。

本模块只承载归一纯函数（无路由副作用 / 无 I/O / 无配置耦合）；通道模型
模块经委托消费（SmsForwarder 保留原导出名，WhatsApp 直接复用）。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["normalize_phone_identifier", "parse_phone_owner_numbers"]

#: 手机号分隔符（空格 / 连字符 / 括号 / 点——展示形态归一为纯号码）。
_SEPARATORS_RE: Final[re.Pattern[str]] = re.compile(r"[\s\-().]")


def normalize_phone_identifier(raw: str) -> str:
    """归一发送者标识（手机号去分隔符 / 国家码，联系名小写）。

    规则（文档化决策）：

        - 去除空格 / 连字符 / 括号 / 点等展示分隔符；
        - 纯数字号码去掉 ``+86`` / ``0086`` / ``86`` 前缀（中国手机号
          11 位归一）——第三方模板可能带国际码，属主清单与发送者两侧
          使用同一归一键；
        - 其余（联系名）小写化，便于属主清单大小写不敏感比较。

    Args:
        raw: 原始发送者（手机号 / 联系名 / WhatsApp ``wa_id``）。

    Returns:
        归一后的发送者键；空串表示无发送者。
    """
    cleaned = _SEPARATORS_RE.sub("", raw.strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if digits.isdigit():
            if len(digits) == 13 and digits.startswith("86"):
                return digits[2:]
            return digits
        return cleaned.lower()
    if cleaned.isdigit():
        if len(cleaned) == 15 and cleaned.startswith("0086"):
            return cleaned[4:]
        if len(cleaned) == 13 and cleaned.startswith("86"):
            return cleaned[2:]
        return cleaned
    return cleaned.lower()


def parse_phone_owner_numbers(raw: str) -> frozenset[str]:
    """解析属主号码 / 联系名清单（逗号分隔；空串 = 空集）。

    Args:
        raw: 配置原值（``SMS_FORWARDER_OWNER_NUMBERS`` /
            ``WHATSAPP_OWNER_NUMBERS`` 等逗号分隔清单）。

    Returns:
        归一后的属主键集合（沿用 :func:`normalize_phone_identifier` 的
        归一口径）。
    """
    owners: set[str] = set()
    for item in raw.split(","):
        normalized = normalize_phone_identifier(item)
        if normalized:
            owners.add(normalized)
    return frozenset(owners)
