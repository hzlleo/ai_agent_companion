"""通道层发送者标识归一（SmsForwarder 与 WhatsApp 各自口径的纯函数面）。

第三方转发类通道的发送者标识形态不受本侧控制：手机端短信模板可能带
``+86`` / 空格 / 连字符等展示形态；WhatsApp 云 API 的 ``wa_id`` 是不带
加号的**国际号码**（国家码是号码本身的一部分，不能剥掉）。两侧的会话键、
属主清单匹配与出站目标必须各自使用同口径归一函数：

    - SmsForwarder：中国码前缀归一（``+86`` / ``0086`` / ``86`` 剥为 11 位
      手机号），与手机端模板常见的国内号码形态对齐；
    - WhatsApp：只剥展示分隔符与前导 ``+``，**保留全部数字**——剥掉国家码
      会让 ``+86 138 0013 8000`` 与 Cloud API 的 ``8613800138000`` 互不
      相认（会话键分裂 / 属主清单失效 / 出站投递到错误号码）。

本模块只承载归一纯函数（无路由副作用 / 无 I/O / 无配置耦合）；通道模型
模块经委托消费（SmsForwarder 保留原导出名，WhatsApp 直接复用）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

__all__ = [
    "normalize_phone_identifier",
    "normalize_whatsapp_wa_id",
    "parse_phone_owner_numbers",
    "parse_whatsapp_owner_numbers",
]

#: 手机号分隔符（空格 / 连字符 / 括号 / 点——展示形态归一为纯号码）。
_SEPARATORS_RE: Final[re.Pattern[str]] = re.compile(r"[\s\-().]")


def normalize_phone_identifier(raw: str) -> str:
    """归一 SmsForwarder 发送者标识（去分隔符 / 中国码前缀，联系名小写）。

    规则（文档化决策）：

        - 去除空格 / 连字符 / 括号 / 点等展示分隔符；
        - 纯数字号码去掉 ``+86`` / ``0086`` / ``86`` 前缀（中国手机号
          11 位归一）——第三方模板可能带国际码，属主清单与发送者两侧
          使用同一归一键；
        - 其余（联系名）小写化，便于属主清单大小写不敏感比较。

    本函数只服务 SmsForwarder；WhatsApp 的 ``wa_id`` 必须保留国家码，
    使用 :func:`normalize_whatsapp_wa_id`（两通道的会话键不共享归一）。

    Args:
        raw: 原始发送者（手机号 / 联系名）。

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


def normalize_whatsapp_wa_id(raw: str) -> str:
    """归一 WhatsApp ``wa_id``（剥展示分隔符与前导 ``+``，保留全部数字）。

    规则（文档化决策）：

        - 去除空格 / 连字符 / 括号 / 点等展示分隔符；
        - 去掉前导 ``+``（Cloud API 的 ``wa_id`` 不带加号）；
        - **保留全部数字**——国家码是 WhatsApp 号码的一部分，不剥
          ``86`` / ``0086``（与 SmsForwarder 的中国码前缀归一不同）；
        - 非纯数字（异常推送 / 联系名形态）小写化，保持与属主清单可比较。

    发送者键、会话键（``wa:<wa_id>``）、属主清单与出站 ``to`` 必须使用
    同一归一口径，否则带国家码的号码会在两侧得到不同键。

    Args:
        raw: 原始 ``wa_id`` / ``chat_id`` 目标段 / 属主清单条目。

    Returns:
        归一后的国际号码（纯数字）或小写文本；空串表示无标识。
    """
    cleaned = _SEPARATORS_RE.sub("", raw.strip())
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if not cleaned:
        return ""
    if cleaned.isdigit():
        return cleaned
    return cleaned.lower()


def parse_phone_owner_numbers(raw: str) -> frozenset[str]:
    """解析 SmsForwarder 属主号码 / 联系名清单（逗号分隔；空串 = 空集）。

    Args:
        raw: 配置原值（``SMS_FORWARDER_OWNER_NUMBERS`` 等逗号分隔清单）。

    Returns:
        归一后的属主键集合（沿用 :func:`normalize_phone_identifier` 的
        归一口径）。
    """
    return _parse_owner_list(raw, normalize_phone_identifier)


def parse_whatsapp_owner_numbers(raw: str) -> frozenset[str]:
    """解析 WhatsApp 属主号码清单（逗号分隔；空串 = 空集）。

    Args:
        raw: 配置原值（``WHATSAPP_OWNER_NUMBERS`` 逗号分隔清单——允许
            展示形态，如 ``+86 138 0013 8000``）。

    Returns:
        归一后的属主键集合（沿用 :func:`normalize_whatsapp_wa_id` 的
        保国家码口径）。
    """
    return _parse_owner_list(raw, normalize_whatsapp_wa_id)


def _parse_owner_list(raw: str, normalize: Callable[[str], str]) -> frozenset[str]:
    """按给定归一函数解析逗号分隔清单（空项 / 归一为空的值跳过）。

    Args:
        raw: 配置原值（逗号分隔）。
        normalize: 逐项归一函数（口径由调用方决定）。

    Returns:
        归一后的键集合。
    """
    owners: set[str] = set()
    for item in raw.split(","):
        normalized = normalize(item)
        if normalized:
            owners.add(normalized)
    return frozenset(owners)
