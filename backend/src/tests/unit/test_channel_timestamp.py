"""通道入站时间戳共享解析矩阵——SmsForwarder / WhatsApp 同源归一与回落。

两通道的入口包装都委托
:func:`packages.harness.channels.inbound_timestamp.resolve_epoch_ms_timestamp`；
本文件用同一参数矩阵覆盖秒 / 毫秒 / 非有限值（``NaN`` / ``Infinity`` /
字符串 ``"nan"`` / ``"inf"`` / ``"Infinity"`` / 溢出为 ``inf`` 的
``"1e999"``）/ 非法字符串 / 缺失 / 布尔 / 非正值，全部回落服务端时钟。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from packages.harness.channels.inbound_timestamp import resolve_epoch_ms_timestamp
from packages.harness.channels.sms_forwarder.sms_models import (
    resolve_sms_forwarder_timestamp_ms,
)
from packages.harness.channels.whatsapp.whatsapp_models import (
    resolve_whatsapp_timestamp_ms,
)

_FALLBACK_MS = 1_700_000_123_456
_RESOLVERS: tuple[tuple[str, Callable[..., int]], ...] = (
    ("sms-forwarder", resolve_sms_forwarder_timestamp_ms),
    ("whatsapp", resolve_whatsapp_timestamp_ms),
)

_SECONDS = 1_731_000_000
_MILLIS = 1_731_000_000_000


@pytest.mark.parametrize("entry,resolver", _RESOLVERS)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_SECONDS, _SECONDS * 1000),
        (str(_SECONDS), _SECONDS * 1000),
        (_SECONDS + 0.9, _SECONDS * 1000),
        (_MILLIS, _MILLIS),
        (str(_MILLIS), _MILLIS),
        (float("nan"), _FALLBACK_MS),
        (float("inf"), _FALLBACK_MS),
        (float("-inf"), _FALLBACK_MS),
        ("nan", _FALLBACK_MS),
        ("inf", _FALLBACK_MS),
        ("Infinity", _FALLBACK_MS),
        ("-Infinity", _FALLBACK_MS),
        ("1e999", _FALLBACK_MS),
        ("not-a-date", _FALLBACK_MS),
        ("", _FALLBACK_MS),
        ("   ", _FALLBACK_MS),
        (None, _FALLBACK_MS),
        (True, _FALLBACK_MS),
        (0, _FALLBACK_MS),
        (-5, _FALLBACK_MS),
        (42, _FALLBACK_MS),
    ],
)
async def test_channel_timestamp_matrix(
    entry: str,
    resolver: Callable[..., int],
    raw: object,
    expected: int,
) -> None:
    """两通道同一矩阵：秒 / 毫秒归一，非有限 / 非法 / 缺失 / 非正值回落。"""
    assert entry in {"sms-forwarder", "whatsapp"}
    assert resolver(raw, fallback_ms=_FALLBACK_MS) == expected


@pytest.mark.parametrize("entry,resolver", _RESOLVERS)
async def test_channels_share_identical_resolution(
    entry: str, resolver: Callable[..., int]
) -> None:
    """两通道对同一输入逐值一致（共享实现单源）。"""
    assert entry
    samples: list[object] = [
        _SECONDS,
        _MILLIS,
        "Infinity",
        "1e999",
        "bad",
        None,
        True,
    ]
    expected = [
        resolve_epoch_ms_timestamp(
            sample, fallback_ms=_FALLBACK_MS, channel_label="shared-check"
        )
        for sample in samples
    ]
    actual = [resolver(sample, fallback_ms=_FALLBACK_MS) for sample in samples]
    assert actual == expected
