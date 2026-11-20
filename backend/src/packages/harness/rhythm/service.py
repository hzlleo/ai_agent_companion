"""rhythm 域服务门面——RhythmService（生效节律 + 注入式时钟 / 随机源）。

本模块是 rhythm 域**唯一有状态件**（持有生效节律 + 时钟 + 随机源）；
节律数学全在 :mod:`packages.harness.rhythm.core`（纯函数）。门面只做
两件事：按注入时钟取当前时刻、把纯函数结果包装成消费面契约
（:class:`~packages.harness.rhythm.rhythm_models.RhythmGate`）。

消费面（前向兼容——文档化）：

    - **心流层**：``gate_reply`` / ``should_reply_now`` 是回复调度
      的决策入口（睡眠窗口静默 + 拟人延迟节拍）；本节律层交付门面与
      契约，心流接线在后继版本；
    - **GUI / 浏览器通道**：门结果（allowed + reason）为人设状态
      展示面（在线 / 休息中）；
    - **通道链路**：回复链路暂**不**消费门（既有回复行为零变化
      ——文档化决策；意图快径的短延迟属意图面真实感，见
      :mod:`packages.harness.intent.service`）。

直通语义（``enabled=False``）：门恒 ``allowed=True`` +
``suggested_delay_ms=0``（缺省零行为变化——RhythmSettings.enabled
锁定决策）。
"""

from __future__ import annotations

import random
from collections.abc import Callable

from packages.harness.core.time_utils import now_ms as default_now_ms
from packages.harness.rhythm.core import (
    is_sleeping,
    next_active_window_start,
    reply_delay_ms,
)
from packages.harness.rhythm.rhythm_models import EffectiveRhythm, RhythmGate

__all__ = ["Clock", "RhythmService"]

#: 时钟契约（epoch 毫秒——注入式，测试可控；本地别名防 scheduler
#: 重依赖环，语义与 :data:`packages.harness.scheduler.Clock` 一致）。
Clock = Callable[[], int]


class RhythmService:
    """节律服务门面（门判定 + 延迟采样；心流 / GUI 消费面）。

    构造注入（测试 / 装配友好）：时钟与随机源均可替换——测试以固定
    时钟与种子化随机源确定性驱动（零真实等待 / 零概率抖动）；生产用
    缺省 ``core.now_ms`` / 进程随机源。随机源与意图快径共享同一实例
    （app 装配单一随机源决策——见 docs/backend/rhythm.md 装配一节）。
    """

    def __init__(
        self,
        *,
        effective: EffectiveRhythm,
        enabled: bool = False,
        clock: Clock = default_now_ms,
        rng: random.Random | None = None,
    ) -> None:
        """初始化服务（无后台任务——纯查询面）。

        Args:
            effective: 生效节律（``resolve_effective`` 产出——per-persona
                覆写解析属心流层，本节律层装配用全局面）。
            enabled: 门控开关（False = 直通形态——allowed 恒 True、
                延迟恒 0；来自 ``RhythmSettings.enabled``）。
            clock: 时钟（epoch 毫秒；``now_ms`` 参数缺省时取值面）。
            rng: 随机源（None = 进程新随机源；延迟采样面——与意图快径
                共享实例由装配方决定）。
        """
        self._effective = effective
        self._enabled = enabled
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()

    @property
    def effective(self) -> EffectiveRhythm:
        """生效节律（观测面——GUI 状态展示 / 日志消费）。"""
        return self._effective

    async def gate_reply(self, now_ms: int | None = None) -> RhythmGate:
        """回复门判定（睡眠窗口静默 + 拟人延迟建议）。

        判定链：直通形态（enabled=False）→ 恒放行零延迟；睡眠窗口内
        → 拒绝 + 建议延迟 = 距下一个醒来时刻的毫秒数（调用方调度参考
        ——心流层静默排程）；清醒 → 放行 + 建议延迟 = 拟人延迟采样值
        （log-uniform + 快答尾）。

        Args:
            now_ms: 判定时刻（epoch 毫秒；None = 注入时钟取当前值）。

        Returns:
            :class:`~packages.harness.rhythm.rhythm_models.RhythmGate`
            （allowed + suggested_delay_ms + 中文 reason）。
        """
        now = now_ms if now_ms is not None else self._clock()
        if not self._enabled:
            return RhythmGate(
                allowed=True,
                suggested_delay_ms=0,
                reason="节律层未启用（RHYTHM_ENABLED=false）——直通形态",
            )
        if is_sleeping(now, self._effective):
            until_wake = max(0, next_active_window_start(now, self._effective) - now)
            return RhythmGate(
                allowed=False,
                suggested_delay_ms=until_wake,
                reason=(
                    f"睡眠窗口内（{self._effective.bedtime}-{self._effective.wake_time}）"
                    "——建议静默至醒来"
                ),
            )
        delay = reply_delay_ms(now, self._effective, self._rng)
        return RhythmGate(
            allowed=True,
            suggested_delay_ms=delay,
            reason=f"清醒时段——拟人延迟 {delay} 毫秒后回复",
        )

    async def should_reply_now(self, now_ms: int | None = None) -> bool:
        """即时回复判定（门的布尔投影——心流层轻量消费面）。

        前向兼容说明（文档化）：心流层将把本入口演化为带主动节拍
        （主动消息 / 打字节奏）的完整决策面；当前语义 = ``gate_reply``
        的 ``allowed`` 投影（睡眠窗口内 False，其余 True）。

        Args:
            now_ms: 判定时刻（epoch 毫秒；None = 注入时钟取当前值）。

        Returns:
            当前允许即时回复返回 True。
        """
        gate = await self.gate_reply(now_ms)
        return gate.allowed
