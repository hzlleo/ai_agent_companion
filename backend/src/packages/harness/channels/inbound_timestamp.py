"""通道入站时间戳解析的单源实现（SmsForwarder / WhatsApp 共用）。

手机端转发与 Meta 推送都以平台秒级时间戳（数值 / 数值字符串）进入载荷，
两条通道的统一语义：

    - 秒级数值（≥ :data:`EPOCH_SECONDS_MIN`）→ ×1000 归一 epoch 毫秒；
    - 毫秒级数值（≥ :data:`EPOCH_MILLIS_MIN`）→ 原样；
    - 缺失 / 空串 / 布尔 / 非正值 / 过小数值 / 非法字符串 / 非有限浮点
      （``NaN`` / ``Infinity`` / 字符串 ``"nan"`` / ``"inf"`` /
      ``"Infinity"`` / 溢出为 ``inf`` 的 ``"1e999"``）→ 回落服务端时钟
      epoch 毫秒（恒正，不抛异常）；
    - 溢出（``int()`` 的 ``OverflowError``）与无法解析同样回落。

``channel_label`` 只参与诊断日志（错误语义定位面）——两条通道的归一
结果与回落行为完全一致；入口包装函数（各通道 models 模块）保留自有名字
与 docstring，内部只委托本模块。
"""

from __future__ import annotations

import math
from typing import Final

from loguru import logger

__all__ = [
    "EPOCH_MILLIS_MIN",
    "EPOCH_SECONDS_MIN",
    "resolve_epoch_ms_timestamp",
]

#: 秒级时间戳下界（低于该值视为非法——回落到服务端时钟）。
EPOCH_SECONDS_MIN: Final[int] = 1_000_000_000

#: 毫秒级时间戳下界（达到该值按毫秒解释，避免二次放大）。
EPOCH_MILLIS_MIN: Final[int] = 1_000_000_000_000


def _fallback(
    *,
    channel_label: str,
    value: int | float | str | None,
    fallback_ms: int,
    reason: str,
) -> int:
    """回落服务端时钟（非法值有内容时记 debug 诊断——缺失不记）。

    Args:
        channel_label: 通道标识（诊断日志定位面）。
        value: 原始时间戳。
        fallback_ms: 回落时刻（服务端时钟 epoch 毫秒）。
        reason: 非法原因（诊断日志）。

    Returns:
        回落时刻。
    """
    if value is not None and not (isinstance(value, str) and not value.strip()):
        logger.debug(
            "{} 时间戳非法（{}，回落服务端时钟）: value={!r}",
            channel_label,
            reason,
            value,
        )
    return fallback_ms


def resolve_epoch_ms_timestamp(
    value: int | float | str | None,
    *,
    fallback_ms: int,
    channel_label: str,
) -> int:
    """把平台时间戳归一为 epoch 毫秒（非法 / 非有限 / 溢出回落服务端时钟）。

    Args:
        value: 原始时间戳（数值 / 数值字符串 / None）。
        fallback_ms: 非法或缺失时的回落时刻（服务端时钟 epoch 毫秒）。
        channel_label: 通道标识（诊断日志定位面）。

    Returns:
        epoch 毫秒时间戳（恒正）。
    """
    if isinstance(value, bool):
        return _fallback(
            channel_label=channel_label,
            value=value,
            fallback_ms=fallback_ms,
            reason="布尔值",
        )
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return _fallback(
                channel_label=channel_label,
                value=value,
                fallback_ms=fallback_ms,
                reason="非有限浮点",
            )
        try:
            candidate = int(value)
        except (OverflowError, ValueError):
            return _fallback(
                channel_label=channel_label,
                value=value,
                fallback_ms=fallback_ms,
                reason="浮点溢出",
            )
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return _fallback(
                channel_label=channel_label,
                value=value,
                fallback_ms=fallback_ms,
                reason="空字符串",
            )
        try:
            candidate = int(text)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return _fallback(
                    channel_label=channel_label,
                    value=value,
                    fallback_ms=fallback_ms,
                    reason="非法字符串",
                )
            if not math.isfinite(parsed):
                return _fallback(
                    channel_label=channel_label,
                    value=value,
                    fallback_ms=fallback_ms,
                    reason="非有限字符串",
                )
            try:
                candidate = int(parsed)
            except (OverflowError, ValueError):
                return _fallback(
                    channel_label=channel_label,
                    value=value,
                    fallback_ms=fallback_ms,
                    reason="字符串数值溢出",
                )
    else:
        return _fallback(
            channel_label=channel_label,
            value=value,
            fallback_ms=fallback_ms,
            reason="类型不支持",
        )
    if candidate <= 0:
        return _fallback(
            channel_label=channel_label,
            value=value,
            fallback_ms=fallback_ms,
            reason="非正值",
        )
    if candidate >= EPOCH_MILLIS_MIN:
        return candidate
    if candidate >= EPOCH_SECONDS_MIN:
        return candidate * 1000
    return _fallback(
        channel_label=channel_label,
        value=value,
        fallback_ms=fallback_ms,
        reason="低于秒级下界",
    )
