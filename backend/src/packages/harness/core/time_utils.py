"""时间戳工具 —— 统一使用 epoch 毫秒（BigInteger）存储时间。

所有 DB 时间戳列（``created_at`` / ``updated_at`` / ``last_active_at`` 等）
均以 epoch 毫秒整数存储，避免 ``DateTime`` 跨方言（MySQL vs SQLite）
行为差异与时区问题。

- ``now_ms()``   —— 当前时间的 epoch 毫秒（用作 ORM ``default`` / ``onupdate``）
- ``to_ms(dt)``  —— ``datetime`` → epoch 毫秒（None 安全，返回 0）
- ``from_ms(ms)``—— epoch 毫秒 → ``datetime``（仅用于展示/日志，不写库）
- ``format_duration_ms(ms)`` —— 毫秒 → 人类可读时长（RATIFIED 三档规格）
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

__all__ = ["format_duration_ms", "now_ms", "to_ms", "from_ms"]


def now_ms() -> int:
    """当前时间作为 epoch 毫秒整数。

    作为 ORM ``mapped_column(default=now_ms)`` 的 Python 侧默认值，
    以及 ``onupdate=now_ms`` 的自动刷新值。每次调用返回新值（callable）。

    Returns:
        当前 epoch 毫秒（如 ``1721012345678``）。
    """
    return int(time.time() * 1000)


def to_ms(dt: datetime | None) -> int:
    """将 ``datetime`` 转换为 epoch 毫秒。

    用于将外部 ``datetime``（如 API 查询参数）转换为 DB 比较所需的
    epoch 毫秒整数。``dt`` 为 ``None`` 时返回 0（避免 ``None`` 比较）。

    Args:
        dt: 待转换的 ``datetime``（naive 或 aware 均可）。

    Returns:
        epoch 毫秒整数；``dt`` 为 ``None`` 时返回 0。
    """
    if dt is None:
        return 0
    return int(dt.timestamp() * 1000)


def from_ms(ms: int | None) -> datetime | None:
    """将 epoch 毫秒转换为 ``datetime``（UTC，仅用于展示/日志）。

    **不用于 DB 写入**——DB 列始终存储 epoch 毫秒整数。
    仅在日志输出、调试打印或前端展示需要 ``datetime`` 对象时使用。

    Args:
        ms: epoch 毫秒整数（可为 ``None``）。

    Returns:
        UTC ``datetime`` 对象；``ms`` 为 ``None`` 时返回 ``None``。
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def format_duration_ms(
    ms: int | float | None,
    *,
    unit_ms: str = "毫秒",
    unit_seconds: str = "秒",
    unit_minutes: str = "分",
) -> str | None:
    """毫秒 → 人类可读时长字符串（RATIFIED 三档规格）。

    规格边界：

        - ``>= 60000``  → 「X分Y秒」（``Y`` 为 0 时省略秒段，如 ``120000 → 2分``）；
        - ``>= 1000``   → 「X.Y秒」（1 位小数，如 ``3450 → 3.5秒``）；
        - ``< 1000``    → 「N毫秒」（如 ``850 → 850毫秒``）。

    单位文案可参数化（报告层透传 locale 资源；缺省中文即规格字面字符串，
    如 ``150228 → 2分30秒``）。负数按 0 夹逼（时长语义非负）。

    Args:
        ms: 时长毫秒数（``None`` 原样返回，由调用方映射「未提供」）。
        unit_ms: 毫秒单位文案。
        unit_seconds: 秒单位文案。
        unit_minutes: 分单位文案。

    Returns:
        格式化时长字符串；``ms`` 为 ``None`` 时返回 ``None``。
    """
    if ms is None:
        return None
    total = int(ms)
    if total < 0:
        total = 0
    if total < 1_000:
        return f"{total}{unit_ms}"
    if total < 60_000:
        return f"{total / 1_000:.1f}{unit_seconds}"
    minutes, remainder = divmod(total, 60_000)
    seconds = remainder // 1_000
    if seconds == 0:
        return f"{minutes}{unit_minutes}"
    return f"{minutes}{unit_minutes}{seconds}{unit_seconds}"
