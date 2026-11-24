"""通道层发送者归一纯函数测试——SMS 中国码口径与 WhatsApp 保国家码口径。"""

from __future__ import annotations

import pytest

from packages.harness.channels.phone_numbers import (
    normalize_phone_identifier,
    normalize_whatsapp_wa_id,
    parse_phone_owner_numbers,
    parse_whatsapp_owner_numbers,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+86 138-0013-8000", "13800138000"),
        ("0086 13800138000", "13800138000"),
        ("8613800138000", "13800138000"),
        ("+1 (555) 123-4567", "15551234567"),
        ("15551234567", "15551234567"),
        (" Alice.Wang ", "alicewang"),
        ("", ""),
        ("()", ""),
    ],
)
async def test_normalize_phone_identifier_matrix(raw: str, expected: str) -> None:
    """归一矩阵：分隔符剥离 / 中国码前缀归一 / 联系名小写 / 空值。"""
    assert normalize_phone_identifier(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8613800138000", "8613800138000"),
        ("+86 138 0013 8000", "8613800138000"),
        ("+86.138.0013.8000", "8613800138000"),
        ("0086 13800138000", "008613800138000"),
        ("+1 (555) 123-4567", "15551234567"),
        ("15551234567", "15551234567"),
        (" Alice.Wang ", "alicewang"),
        ("", ""),
    ],
)
async def test_normalize_whatsapp_wa_id_preserves_country_code(
    raw: str, expected: str
) -> None:
    """WhatsApp 归一：剥分隔符与前导 +，保留国家码（不剥 86 / 0086）。"""
    assert normalize_whatsapp_wa_id(raw) == expected


async def test_parse_phone_owner_numbers_normalizes_and_skips_empty() -> None:
    """属主清单：逗号分隔归一、空白与空项跳过。"""
    owners = parse_phone_owner_numbers("+86 138-0013-8000, 妈妈 ,")
    assert owners == frozenset({"13800138000", "妈妈"})


async def test_parse_phone_owner_numbers_empty_input() -> None:
    """空清单 → 空集（全部发送者按 peer 落档）。"""
    assert parse_phone_owner_numbers("") == frozenset()


async def test_parse_whatsapp_owner_numbers_preserves_country_code() -> None:
    """WhatsApp 属主清单：展示形态归一时保留国家码，空项跳过。"""
    owners = parse_whatsapp_owner_numbers("+86 138 0013 8000, 妈妈 ,")
    assert owners == frozenset({"8613800138000", "妈妈"})
