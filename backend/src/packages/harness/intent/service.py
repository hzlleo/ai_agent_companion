"""intent 域服务——IntentShortcutService（快径委托 + 拟真延迟面）。

职责（两项，均薄）：

    - :meth:`IntentShortcutService.try_shortcut` —— 纯委托
      :func:`packages.harness.intent.matcher.match_intent`（人格称呼提取
      + 匹配）；**恒不抛异常**（内部任何异常归一 ``handled=False``——
      快径失败永不阻断正常 LLM 回复链路）；
    - :meth:`IntentShortcutService.realism_delay` —— 快径拟真延迟
      （800-1500ms 随机 + 可注入休眠面）。

拟真延迟决策（文档化锁定）：快径回复用**短固定区间** 800-1500ms
随机延迟，**不走**节律门的完整延迟（上限 45s——罐头回复是「顺手
秒回」语义，长延迟反而失真）；随机源与 rhythm 域共享同一实例
（app 装配单一随机源——见 docs/backend/intent.md 装配一节）。睡眠
门控不适用于快径（心流层统一承载回复调度决策）。

延迟注入面：``rng``（随机源）与 ``sleep``（休眠函数，秒）均可构造
    注入——测试以假随机源 + 假休眠面确定性驱动（零真实等待，断言
    延迟落在区间内）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from loguru import logger

from packages.harness.core.time_utils import now_ms as default_now_ms
from packages.harness.intent.intent_models import (
    DirectiveSinkFace,
    IntentKind,
    IntentMatchModel,
    PersonaNaming,
)
from packages.harness.intent.intent_texts import DIRECTIVE_CONFIRMED_TEXT
from packages.harness.intent.matcher import match_intent
from packages.harness.rhythm.rhythm_models import EffectiveRhythm

__all__ = [
    "INTENT_SHORTCUT_DELAY_MIN_MS",
    "INTENT_SHORTCUT_DELAY_MAX_MS",
    "SleepFace",
    "IntentShortcutService",
]

#: 快径拟真延迟下限毫秒（锁定 800——「顺手秒回」语义下界）。
INTENT_SHORTCUT_DELAY_MIN_MS: int = 800

#: 快径拟真延迟上限毫秒（锁定 1500——短区间上界；不走节律门完整延迟）。
INTENT_SHORTCUT_DELAY_MAX_MS: int = 1500

#: 休眠面契约（秒——注入式，测试可控；本地别名，语义与 scheduler
#: ``Sleep`` 一致——import scheduler 会经 config 聚合链回环，故物理独立）。
SleepFace = Callable[[float], Awaitable[None]]


class IntentShortcutService:
    """意图快径服务（匹配委托 + 拟真延迟；恒不抛异常）。

    消费面：chat 域 ``reply`` 前钩（经 :class:`IntentShortcutFace`
    协议结构化消费——app 装配仅在**通道路径**注入；CLI / duty 不注入，
    行为零变化）。构造注入（测试 / 装配友好）：随机源 / 休眠面 /
    时钟 / 生效节律均可替换。
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        sleep: SleepFace = asyncio.sleep,
        effective: EffectiveRhythm | None = None,
        clock: Callable[[], int] = default_now_ms,
        directive_sink: DirectiveSinkFace | None = None,
    ) -> None:
        """初始化服务（无后台任务——纯委托面）。

        Args:
            rng: 随机源（None = 进程新随机源；与 rhythm 域共享实例由
                装配方决定——拟真延迟采样面）。
            sleep: 休眠函数（秒——缺省 ``asyncio.sleep``；测试注入假
                休眠面零等待）。
            effective: 生效节律（None = 问候深夜带用固定 23-05 小时带；
                提供时深夜带 = 人格睡眠窗口——rhythm 值面与门控开关
                ``RHYTHM_ENABLED`` 正交，作息描述恒可用于文案选带）。
            clock: 时钟（epoch 毫秒；``now_ms`` 参数缺省时取值面）。
            directive_sink: 长期指令落库面（None = 无副作用形态——
                DIRECTIVE 命中只回附和文案，不落库；装配方注入通道
                专属闭包，见模块 docstring）。
        """
        self._rng = rng if rng is not None else random.Random()
        self._sleep = sleep
        self._effective = effective
        self._clock = clock
        self._directive_sink = directive_sink

    async def try_shortcut(
        self, text: str, persona: PersonaNaming, now_ms: int | None = None
    ) -> IntentMatchModel:
        """意图快径尝试（纯委托 + 人格称呼提取；恒不抛异常）。

        人格称呼提取：``display_name`` 非空优先、回落 ``persona_name``
        （注册名）——罐头文案的 ``{persona_name}`` 注入值。

        Args:
            text: 用户消息原文（空 / 纯空白 → 恒未命中）。
            persona: 人格命名面（会话状态 / 人格记录同形满足——
                ``persona_name`` + ``display_name``）。
            now_ms: 判定时刻（epoch 毫秒；None = 注入时钟取当前值）。

        Returns:
            :class:`~packages.harness.intent.intent_models.IntentMatchModel`
            （未命中 / 内部异常 = ``handled=False``——文本照常流入 LLM）。
        """
        try:
            now = now_ms if now_ms is not None else self._clock()
            persona_display = persona.display_name or persona.persona_name
            match = match_intent(text, persona_display, now, self._effective)
            return await self._apply_directive_side_effect(match)
        except Exception:
            # 快径失败永不阻断正常回复链路（隔离边界：本服务是回复链路
            # 的可选加速面——内部任何异常归一未命中，错误细节进日志）。
            logger.opt(exception=True).warning("意图快径内部异常（归一未命中，文本照常流入 LLM）")
            return IntentMatchModel()

    async def _apply_directive_side_effect(self, match: IntentMatchModel) -> IntentMatchModel:
        """DIRECTIVE 命中副作用（落库成功 → 替换为「已记住」确认文案）。

        语义（锁定）：sink 未接线 / 落库失败 / 正文缺失时原样返回附和
        文案（不承诺「记住」——下一轮 compose 读不到指令时不自相矛盾）；
        副作用异常在此吞没（快径链路恒不抛，错误进日志）。

        Args:
            match: 匹配结果（matcher 产出）。

        Returns:
            处理后的匹配结果（DIRECTIVE + 落库成功时 response_text 为
            确认文案）。
        """
        if (
            not match.handled
            or match.kind is not IntentKind.DIRECTIVE
            or not match.directive_text
        ):
            return match
        if self._directive_sink is None:
            return match
        try:
            saved = await self._directive_sink(match.directive_text)
        except Exception:  # 副作用异常不阻断回复链路（退化为附和文案）
            logger.opt(exception=True).warning("长期指令落库异常（退化为附和文案）")
            return match
        if not saved:
            logger.warning("长期指令未落库（退化为附和文案）: text={}", match.directive_text)
            return match
        return match.model_copy(
            update={"response_text": DIRECTIVE_CONFIRMED_TEXT.format(directive=match.directive_text)}
        )

    async def realism_delay(self) -> None:
        """快径拟真延迟（800-1500ms 随机采样 + 休眠面等待）。

        采样经注入随机源 ``randint(800, 1500)`` 毫秒；等待经注入休眠
        面以秒为单位执行（``delay_ms / 1000``）——测试注入假面断言
        区间，零真实等待。

        Raises:
            Exception: 休眠面异常（透传——注入面契约；生产
                ``asyncio.sleep`` 除取消外恒不抛）。
        """
        delay_ms = self._rng.randint(INTENT_SHORTCUT_DELAY_MIN_MS, INTENT_SHORTCUT_DELAY_MAX_MS)
        await self._sleep(delay_ms / 1000)
