"""rhythm 域纯函数核心——睡眠窗口 / 回复延迟 / 打字模拟 / 生效值解析。

全纯函数（零 I/O / 零全局态）：``now_ms`` 由调用方注入（时钟可测），
``rng`` 为 :class:`random.Random` 注入（种子化确定性测试）；本模块被
service 薄门面与未来的心流层 / GUI 消费。

时区约定（naive 本地墙钟——scheduler 先例）：本地时刻经
``datetime.fromtimestamp``（无 tz 参数）解析——与
:func:`packages.harness.scheduler.next_daily_occurrence_ms` 同语义；
不引入 zoneinfo / DST 规则。下一个醒来时刻的推进计算本地自持
（镜像 scheduler 同语义）——rhythm_models 被 config 域聚合
（config → rhythm 单向），import scheduler 会经其 llm / persona
依赖链回环 config，故物理独立。

语义总览::

    is_sleeping(now, eff)            睡眠窗口判定（含 bedtime、不含 wake；
                                     窗口可跨午夜；bedtime == wake = 空窗口恒醒）
    reply_delay_ms(now, eff, rng)    拟人延迟采样（log-uniform + 快答尾）
    typing_duration_ms(len, eff)     打字模拟（base + 每字符速度，封顶 8s）
    next_active_window_start(now, eff)  下一个醒来时刻（严格晚于 now）
    resolve_effective(settings, override)  生效值解析（覆写合并 + 延迟缩放）
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Final

from packages.harness.rhythm.rhythm_models import (
    EffectiveRhythm,
    PersonaRhythmOverride,
    RhythmSettings,
    TypingSimulation,
)

__all__ = [
    "TYPING_DURATION_CAP_MS",
    "is_sleeping",
    "reply_delay_ms",
    "typing_duration_ms",
    "next_active_window_start",
    "resolve_effective",
]

#: 打字模拟耗时封顶毫秒（锁定 8s——超长文本不无限累计；拟人打字上限）。
TYPING_DURATION_CAP_MS: Final[int] = 8000


def _local_minute_of_day(now_ms: int) -> int:
    """epoch 毫秒 → 本地当日分钟数（naive 本地墙钟）。

    Args:
        now_ms: 当前时刻（epoch 毫秒——注入式）。

    Returns:
        当日分钟数（0-1439——``HH * 60 + MM``）。
    """
    local = datetime.fromtimestamp(now_ms / 1000)
    return local.hour * 60 + local.minute


def _hhmm_to_minute(value: str) -> int:
    """解析 "HH:MM" → 当日分钟数（形态已由模型层校验——core 内部消费面）。

    Args:
        value: 时刻字符串（"HH:MM" 24 小时制——EffectiveRhythm 物化字段
            的原料）。

    Returns:
        当日分钟数（0-1439）。
    """
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def is_sleeping(now_ms: int, effective: EffectiveRhythm) -> bool:
    """睡眠窗口判定（含 bedtime 端点、不含 wake 端点；窗口可跨午夜）。

    边界语义（文档化锁定）：

        - ``bedtime`` 时刻本身**在窗口内**（23:30 上床 → 23:30 即入睡）；
        - ``wake_time`` 时刻本身**在窗口外**（08:00 醒来即清醒）；
        - 跨午夜窗口（bedtime > wake，如 23:30 → 08:00）：当日
          ``[bedtime, 24:00)`` ∪ 次日 ``[00:00, wake)``；
        - 同日窗口（bedtime < wake，如 13:00 → 15:00 午休）：``[bedtime,
          wake)``；
        - 退化形态 ``bedtime == wake``：空窗口——恒醒（文档化：零长度
          睡眠窗口无意义，按永不入睡处理）。

    Args:
        now_ms: 当前时刻（epoch 毫秒——注入式）。
        effective: 生效节律（bedtime_minute / wake_minute 消费面）。

    Returns:
        当前处于睡眠窗口内返回 True。
    """
    bed = effective.bedtime_minute
    wake = effective.wake_minute
    if bed == wake:
        return False
    minute = _local_minute_of_day(now_ms)
    if bed < wake:
        return bed <= minute < wake
    return minute >= bed or minute < wake


def reply_delay_ms(now_ms: int, effective: EffectiveRhythm, rng: random.Random) -> int:
    """拟人回复延迟采样（bounded log-uniform + 概率快答尾）。

    采样语义：

        - 常规：``[min, max]`` 上的 log-uniform 采样（对数轴均匀——
          短延迟密 / 长延迟疏，贴合人类回复节奏）；
        - 快答（``burst_probability`` 机会）：``[min, min*2)`` 快尾
          log-uniform 采样（偶尔秒回的兴奋感；``min*2`` 超过 ``max``
          时夹逼到 ``max``）；
        - 边界夹逼：结果恒在 ``[min, max]`` 内（对数采样 + 取整后
          clamp；``min == max`` 时恒返回该值）。

    Args:
        now_ms: 当前时刻（epoch 毫秒——时钟轴一致性参数；当前采样不
            消费该值，为将来按时段调制延迟保留统一注入面）。
        effective: 生效节律（缩放后的延迟边界 + 快答概率消费面）。
        rng: 随机源（注入式——种子化确定性测试）。

    Returns:
        采样延迟毫秒（int——恒在 ``[reply_delay_min_ms,
        reply_delay_max_ms]`` 内）。
    """
    del now_ms
    low = effective.reply_delay_min_ms
    high = effective.reply_delay_max_ms
    if low == high:
        return low
    if rng.random() < effective.burst_probability:
        burst_high = min(low * 2, high)
        return _log_uniform_int(rng, low, burst_high)
    return _log_uniform_int(rng, low, high)


def _log_uniform_int(rng: random.Random, low: int, high: int) -> int:
    """[low, high] 上的 log-uniform 整数采样（对数轴均匀 + 边界夹逼）。

    Args:
        rng: 随机源（注入式）。
        low: 采样域下界（≥1——对数域要求正值）。
        high: 采样域上界（≥ low）。

    Returns:
        采样值（int——恒在 ``[low, high]`` 内；``low == high`` 恒返回该值）。
    """
    if low >= high:
        return low
    position = rng.random()
    drawn = math.exp(math.log(low) + position * (math.log(high) - math.log(low)))
    return max(low, min(high, int(drawn)))


def typing_duration_ms(text_len: int, effective: EffectiveRhythm) -> TypingSimulation:
    """打字模拟耗时（base + 每字符速度累计，封顶 8s）。

    Args:
        text_len: 文本长度（字符数——负数按 0 夹逼）。
        effective: 生效节律（typing_base_ms / typing_speed_ms_per_char
            消费面）。

    Returns:
        :class:`~packages.harness.rhythm.rhythm_models.TypingSimulation`
        （duration_ms 封顶 :data:`TYPING_DURATION_CAP_MS`；chars 为
        夹逼后的长度观测值）。
    """
    chars = max(0, text_len)
    raw = effective.typing_base_ms + chars * effective.typing_speed_ms_per_char
    duration = min(raw, TYPING_DURATION_CAP_MS)
    return TypingSimulation(duration_ms=duration, chars=chars)


def next_active_window_start(now_ms: int, effective: EffectiveRhythm) -> int:
    """下一个醒来时刻（严格晚于参考时刻的下一个 wake 出现）。

    语义（镜像 scheduler ``next_daily_occurrence_ms`` 的「下一个出现」
    ——本地自持防 import 环，见模块 docstring）：今日 ``wake_time``
    严格晚于参考时刻则取今日；否则取明日。入睡中的调用方据此调度
    苏醒；清醒中的调用方得到的是**下一个**活跃窗起点（今日 wake 已过
    → 明日 wake）。

    Args:
        now_ms: 参考时刻（epoch 毫秒——注入式）。
        effective: 生效节律（wake_minute 消费面）。

    Returns:
        下一个醒来时刻（epoch 毫秒——严格大于参考时刻）。
    """
    local = datetime.fromtimestamp(now_ms / 1000)
    candidate = local.replace(
        hour=effective.wake_minute // 60,
        minute=effective.wake_minute % 60,
        second=0,
        microsecond=0,
    )
    if candidate <= local:
        candidate += timedelta(days=1)
    return int(candidate.timestamp() * 1000)


def resolve_effective(
    settings: RhythmSettings, override: PersonaRhythmOverride | None
) -> EffectiveRhythm:
    """生效值解析（人格覆写合并 + 延迟温度缩放）。

    合并规则：时刻取覆写值优先（None 回落 settings 值）；延迟边界 =
    settings 边界 × ``delay_scale``（覆写缺省 1.0），缩放后四舍五入并
    夹逼下限 ≥1ms（log-uniform 对数域要求）；打字参数与快答概率为
    全局面（不参与人格覆写）。

    Args:
        settings: 节律配置（RHYTHM_ env 面或显式构造）。
        override: 人格级覆写（None = 全局面直通）。

    Returns:
        :class:`~packages.harness.rhythm.rhythm_models.EffectiveRhythm`
        （时刻字符串 + 物化分钟数 + 缩放后延迟边界）。
    """
    bedtime = settings.bedtime if override is None or override.bedtime is None else override.bedtime
    wake_time = (
        settings.wake_time if override is None or override.wake_time is None else override.wake_time
    )
    scale = 1.0 if override is None or override.delay_scale is None else override.delay_scale
    scaled_min = max(1, round(settings.reply_delay_min_ms * scale))
    scaled_max = max(scaled_min, round(settings.reply_delay_max_ms * scale))
    return EffectiveRhythm(
        bedtime=bedtime,
        wake_time=wake_time,
        bedtime_minute=_hhmm_to_minute(bedtime),
        wake_minute=_hhmm_to_minute(wake_time),
        reply_delay_min_ms=scaled_min,
        reply_delay_max_ms=scaled_max,
        typing_speed_ms_per_char=settings.typing_speed_ms_per_char,
        typing_base_ms=settings.typing_base_ms,
        burst_probability=settings.burst_probability,
        delay_scale=scale,
    )
