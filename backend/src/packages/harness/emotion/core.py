"""emotion 域核心算法（纯函数 Unit——可注入时钟 / 无 I/O / 无持久化）。

算法契约（文档化于 docs/backend/emotion.md「衰减数学」一节）：

    - :func:`apply_deltas` —— 增量叠加（维度值 + 增量 → 0..1 夹取）；
      入参状态不被修改（返回新模型）；
    - :func:`decay_toward_baseline` —— 指数半衰期衰减：偏离量按
      ``0.5 ** (elapsed / half_life_ms)`` 向中性基线回落（时间锚取
      状态 ``updated_at_ms``；时钟回拨按 0 耗时处理）；
    - :func:`summarize_emotion` —— 非中性维度摘要行（中文展示名 +
      方向标记；全中性返回 None）；
    - :func:`is_neutral` —— 中性判定（全部维度偏离低于摘要阈值）。

数值边界：状态值 / 增量按 8 维枚举校验（未知维度丢弃——debug 记录），
数值一律夹取到合法区间；本模块不抛业务异常（模型输出不可信，宽容
解析是协议面契约）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from loguru import logger

from packages.harness.emotion.emotion_models import (
    DIMENSION_LABELS,
    EmotionConfig,
    EmotionDimension,
    EmotionStateModel,
)

__all__ = [
    "apply_deltas",
    "decay_toward_baseline",
    "summarize_emotion",
    "is_neutral",
]

#: 未出现维度的中性值（与 EmotionConfig 缺省基线一致——引擎状态初始化
#: 会写入全维基线；本常量只承担纯函数面的缺省处理）。
_MISSING_DIMENSION_VALUE: float = 0.5


def _clamp(value: float, low: float, high: float) -> float:
    """数值夹取（闭区间）。

    Args:
        value: 原始数值。
        low: 下界。
        high: 上界。

    Returns:
        夹取后的数值。
    """
    return max(low, min(high, value))


def apply_deltas(
    state: EmotionStateModel, deltas: Mapping[str, float], now_ms: int
) -> EmotionStateModel:
    """把一轮情绪增量叠加到状态（维度值 + 增量 → 0..1 夹取）。

    语义：未知维度丢弃（debug 记录）；未出现的维度按中性缺省值
    （0.5）作为叠加起点；数值夹取到 0..1；``updated_at_ms`` 置为本次
    叠加时刻（衰减锚点推进）。入参不被修改（返回新模型——纯函数）。

    Args:
        state: 当前情绪状态。
        deltas: 增量字典（维度 → 增量；-1..1）。
        now_ms: 叠加时刻（epoch 毫秒——注入式时钟）。

    Returns:
        叠加后的新状态模型。
    """
    values: dict[str, float] = dict(state.values)
    for key, delta in deltas.items():
        try:
            dimension = EmotionDimension(key)
        except ValueError:
            logger.debug("情绪增量维度未知（已丢弃）: {!r}", key)
            continue
        try:
            numeric = float(delta)
        except (TypeError, ValueError):
            logger.debug("情绪增量非数值（已丢弃）: {!r}={!r}", key, delta)
            continue
        current = values.get(dimension.value, _MISSING_DIMENSION_VALUE)
        values[dimension.value] = _clamp(current + numeric, 0.0, 1.0)
    return EmotionStateModel(values=values, updated_at_ms=now_ms)


def decay_toward_baseline(
    state: EmotionStateModel, now_ms: int, config: EmotionConfig
) -> EmotionStateModel:
    """把状态按指数半衰期向中性基线衰减（时间锚 = 状态更新时间）。

    数学：``elapsed = max(0, now_ms - state.updated_at_ms)``；
    ``value = baseline + (value - baseline) * 0.5 ** (elapsed /
    half_life_ms)``——半衰期后偏离量减半，长期回落到基线。时钟回拨
    （elapsed <= 0）等价零耗时（状态原样返回，更新时间推进到 now_ms）。

    Args:
        state: 当前情绪状态（时间锚 = ``updated_at_ms``）。
        now_ms: 衰减计算时刻（epoch 毫秒——注入式时钟）。
        config: 引擎参数（半衰期 / 中性基线）。

    Returns:
        衰减后的新状态模型（入参不被修改）。
    """
    elapsed = max(0, now_ms - state.updated_at_ms)
    if elapsed == 0:
        return EmotionStateModel(values=dict(state.values), updated_at_ms=now_ms)
    factor = math.pow(0.5, elapsed / config.decay_half_life_ms)
    baseline = config.neutral_baseline
    values: dict[str, float] = {}
    for key, value in state.values.items():
        decayed = baseline + (value - baseline) * factor
        values[key] = _clamp(decayed, 0.0, 1.0)
    return EmotionStateModel(values=values, updated_at_ms=now_ms)


def is_neutral(state: EmotionStateModel, config: EmotionConfig) -> bool:
    """中性判定（全部已记录维度偏离基线低于摘要阈值）。

    Args:
        state: 情绪状态。
        config: 引擎参数（摘要阈值 / 中性基线）。

    Returns:
        True = 全部维度视为中性（无可感情绪）；False = 至少一维可感。
    """
    for value in state.values.values():
        if abs(value - config.neutral_baseline) >= config.summary_threshold:
            return False
    return True


def summarize_emotion(state: EmotionStateModel, config: EmotionConfig) -> str | None:
    """生成中文情绪摘要行（非中性维度按偏离幅度降序）。

    格式：``当前情绪：愉悦↑、羞怯↑``（方向标记 ↑ = 高于基线 / ↓ =
    低于基线）；全部维度低于阈值时返回 None（不注入心情块）。未知
    维度（状态模型边界已过滤，此处双保险）跳过。

    Args:
        state: 情绪状态。
        config: 引擎参数（摘要阈值 / 中性基线）。

    Returns:
        摘要行文本；全中性为 None。
    """
    deviations: list[tuple[str, float]] = []
    for key, value in state.values.items():
        try:
            dimension = EmotionDimension(key)
        except ValueError:
            continue
        deviation = value - config.neutral_baseline
        if abs(deviation) >= config.summary_threshold:
            deviations.append((dimension.value, deviation))
    if not deviations:
        return None
    deviations.sort(key=lambda item: (-abs(item[1]), item[0]))
    segments = [
        f"{DIMENSION_LABELS[dimension]}{'↑' if deviation > 0 else '↓'}"
        for dimension, deviation in deviations
    ]
    return "当前情绪：" + "、".join(segments)
