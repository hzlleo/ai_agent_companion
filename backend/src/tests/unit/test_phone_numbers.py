"""通道层手机号归一纯函数测试——与 SmsForwarder / WhatsApp 共享口径。"""

from __future__ import annotations

import pytest

from packages.harness.channels.phone_numbers import (
    normalize_phone_identifier,
    parse_phone_owner_numbers,
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


async def test_parse_phone_owner_numbers_normalizes_and_skips_empty() -> None:
    """属主清单：逗号分隔归一、空白与空项跳过。"""
    owners = parse_phone_owner_numbers("+86 138-0013-8000, 妈妈 ,")
    assert owners == frozenset({"13800138000", "妈妈"})


async def test_parse_phone_owner_numbers_empty_input() -> None:
    """空清单 → 空集（全部发送者按 peer 落档）。"""
    assert parse_phone_owner_numbers("") == frozenset()
