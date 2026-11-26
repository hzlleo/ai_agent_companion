"""voice 域分面日预算闸与计量接线（ASR / TTS / voiceprint）。

设计决策（与 llm 网关日预算同语义）：

    - **按 purpose 分面**：ASR / TTS / voiceprint 各自独立计数与上限，
      互不占用；查询强制携带 purpose + 成功状态过滤（LLM / search 行与
      历史失败行不计入）；
    - **计量口径为调用次数**：每次真实出站调用成功后落一行 usage
      （persona 上下文存在时带 persona_id；失败与被预算拒绝的调用
      不落行——被拒绝的调用不消耗预算）；
    - **判定顺序**：persona 档先行、global 档其后；两档均 0 = 不限且
      不发起计数查询；超限抛既有 ``RATE_LIMITED``（details 含
      scope / used / limit / purpose，不含任何原文内容）；
    - **软竞态**：计数经 usage 表查询（DB 口径），跨 worker 各自查询
      同一张表，结果近似全局一致——并发请求可能同时读到同一剩余额度
      并同时放行（最多超出并发数条）；不引入 Redis 式原子计数；
    - **有效上限 10,000**：计数走 usage 游标分页，页帽 50 × 每页 200
      = 10,000 行封顶（配置层 ``le=10_000`` 同约束）；
    - **不硬依赖 usage 领域服务**：预算常量从 usage.budget_limits（仅标准
      库依赖）引用；计数 / 记录器均为注入式回调，默认接线函数在函数体内
      惰性 import usage 服务（与 llm 网关同模式）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.usage.budget_limits import (
    BUDGET_COUNT_PAGE_CAP,
    BUDGET_COUNT_PAGE_LIMIT,
)
from packages.harness.voice.voice_models import VoiceUsageEvent

__all__ = [
    "DailyBudgetLimits",
    "VoiceBudgetCounter",
    "VoiceUsageRecorder",
    "emit_voice_usage",
    "enforce_daily_budget",
    "wire_default_voice_usage_counter",
    "wire_default_voice_usage_recorder",
]

#: 用量记录器契约——voice 域服务 → usage 域的注入式异步回调。
VoiceUsageRecorder = Callable[[VoiceUsageEvent], Awaitable[None]]
#: 分面预算计数契约——(purpose, persona_id, 日桶起点毫秒) → 当日调用行数。
VoiceBudgetCounter = Callable[[str, str | None, int], Awaitable[int]]

#: UTC 日桶宽（毫秒）——预算「今日」边界（与 usage 域 time_bucket=day 同口径）。
DAY_BUCKET_MS: int = 86_400_000


@dataclass(frozen=True)
class DailyBudgetLimits:
    """单分面预算上限（配置面解析产物）。

    Attributes:
        global_limit: 全局每日调用数上限（0 = 不限）。
        persona_limit: 单人格每日调用数上限（0 = 不限）。
    """

    global_limit: int
    persona_limit: int


async def enforce_daily_budget(
    *,
    purpose: str,
    label: str,
    limits: DailyBudgetLimits,
    persona_id: str | None,
    counter: VoiceBudgetCounter | None,
    clock: Callable[[], int],
) -> None:
    """分面日预算门禁（persona 档先行——更紧的先报；超限即抛）。

    Args:
        purpose: 调用用途（asr / tts / voiceprint——计数过滤轴）。
        label: 错误信息中的能力名（如「TTS 语音合成」）。
        limits: 该分面的双档上限（均 0 = 不限——零计数查询）。
        persona_id: 人格 ID（None = 系统调用——跳过 persona 档）。
        counter: 注入式计数面（None = 未接线——零预算门禁）。
        clock: 时钟（epoch 毫秒——日桶边界，测试注入）。

    Raises:
        BusinessException: 任一档超限（``RATE_LIMITED``——details 含
            scope / used / limit / purpose；persona 档另含 persona_id）。
    """
    if counter is None:
        return
    if limits.persona_limit <= 0 and limits.global_limit <= 0:
        return
    day_start = (clock() // DAY_BUCKET_MS) * DAY_BUCKET_MS
    if persona_id is not None and limits.persona_limit > 0:
        persona_used = await counter(purpose, persona_id, day_start)
        if persona_used >= limits.persona_limit:
            _raise_budget_exceeded(
                purpose=purpose,
                label=label,
                scope="persona",
                used=persona_used,
                limit=limits.persona_limit,
                persona_id=persona_id,
            )
    if limits.global_limit > 0:
        global_used = await counter(purpose, None, day_start)
        if global_used >= limits.global_limit:
            _raise_budget_exceeded(
                purpose=purpose,
                label=label,
                scope="global",
                used=global_used,
                limit=limits.global_limit,
                persona_id=None,
            )


def _raise_budget_exceeded(
    *,
    purpose: str,
    label: str,
    scope: str,
    used: int,
    limit: int,
    persona_id: str | None,
) -> None:
    """抛分面预算超限错误（统一 message / details 形状）。

    Args:
        purpose: 调用用途。
        label: 错误信息中的能力名。
        scope: 超限档位（persona / global）。
        used: 当日已用调用数。
        limit: 该档上限。
        persona_id: 人格 ID（global 档为 None）。

    Raises:
        BusinessException: ``RATE_LIMITED``（429）。
    """
    details: dict[str, object] = {
        "scope": scope,
        "used": used,
        "limit": limit,
        "purpose": purpose,
    }
    if persona_id is not None:
        details["persona_id"] = persona_id
    logger.warning(
        "语音域日预算已用尽（调用前拒绝）: purpose={}, scope={}, used={}, limit={}, persona={}",
        purpose,
        scope,
        used,
        limit,
        persona_id,
    )
    raise BusinessException(
        code=ErrorCode.RATE_LIMITED,
        message=f"{label} 每日预算已用尽（{scope}）: used={used}, limit={limit}",
        details=details,
    )


async def emit_voice_usage(
    recorder: VoiceUsageRecorder | None,
    *,
    purpose: str,
    model_entry: str,
    provider: str,
    persona_id: str | None,
    request_id: str | None,
    ts_ms: int,
    latency_ms: int,
) -> None:
    """发射成功用量事件（未接线 no-op；记录器异常仅告警不阻断）。

    只在真实出站调用成功后调用——失败与被预算拒绝的调用不落行。

    Args:
        recorder: 注入式记录器（None = 未接线）。
        purpose: 调用用途（asr / tts / voiceprint）。
        model_entry: 模型条目名。
        provider: provider 类型。
        persona_id: 人格 ID（None = 系统调用）。
        request_id: 请求追踪 ID。
        ts_ms: 调用发起时间（epoch 毫秒）。
        latency_ms: 端到端时延毫秒。
    """
    if recorder is None:
        return
    event = VoiceUsageEvent(
        ts_ms=ts_ms,
        persona_id=persona_id,
        purpose=purpose,
        model_entry=model_entry,
        provider=provider,
        latency_ms=latency_ms,
        status="success",
        request_id=request_id,
        error_code=None,
    )
    try:
        await recorder(event)
    except Exception:
        logger.opt(exception=True).warning(
            "voice 用量记录发射失败（不阻断主调用）: purpose={}, model_entry={}",
            purpose,
            model_entry,
        )


def wire_default_voice_usage_recorder() -> VoiceUsageRecorder:
    """装配默认 voice 用量记录器（绑定 usage 域服务——app / CLI 装配调用）。

    voice 域不硬依赖 usage 域：本函数是唯一接线点，函数体内惰性 import
    （调用时才解析 usage 模块——voice 模块导入期零耦合；与 llm 网关
    wire_default_usage_recorder 同模式）。装配形态::

        service = VoiceService(usage_recorder=wire_default_voice_usage_recorder())

    Returns:
        将 :class:`VoiceUsageEvent` 映射为 ``UsageCreateDTO``（token 恒
        0——语音调用无 token 语义）落库的异步记录器。
    """
    from packages.harness.usage.dtos import UsageCreateDTO
    from packages.harness.usage.models import UsagePurpose, UsageStatus
    from packages.harness.usage.service import UsageService

    service = UsageService()

    async def _record(event: VoiceUsageEvent) -> None:
        """事件 → usage 域创建 DTO（值域枚举化；token 0/0/0）→ 落库。"""
        await service.create(
            UsageCreateDTO(
                ts_ms=event.ts_ms,
                persona_id=event.persona_id,
                model_entry=event.model_entry,
                provider=event.provider,
                purpose=UsagePurpose(event.purpose),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=event.latency_ms,
                status=UsageStatus(event.status),
                request_id=event.request_id,
                error_code=event.error_code,
            )
        )

    return _record


def wire_default_voice_usage_counter() -> VoiceBudgetCounter:
    """装配默认分面预算计数面（绑定 usage 域查询——app / CLI 装配调用）。

    计数口径 = 当日 UTC 日桶内、指定 purpose 的**成功** usage 行：查询强制
    按 purpose 与 status 过滤（LLM / search / 其它分面行与历史失败行不计入），
    跨 worker 近似一致。
    分页枚举同 llm 网关（页帽 50 × 每页 200 = 10,000 行有效上限）。

    Returns:
        ``(purpose, persona_id, day_start_ms) → 当日行数`` 的异步计数器。
    """
    from packages.harness.usage.dtos import UsageQueryDTO
    from packages.harness.usage.models import UsagePurpose, UsageStatus
    from packages.harness.usage.service import UsageService

    service = UsageService()

    async def _count(purpose: str, persona_id: str | None, day_start_ms: int) -> int:
        """游标分页统计当日指定 purpose 行数（页数安全上限防异常膨胀）。"""
        resolved_purpose = UsagePurpose(purpose)
        total = 0
        cursor: str | None = None
        for _ in range(BUDGET_COUNT_PAGE_CAP):
            page = await service.query(
                UsageQueryDTO(
                    persona_id=persona_id,
                    purpose=resolved_purpose,
                    status=UsageStatus.SUCCESS,
                    start_ms=day_start_ms,
                    cursor=cursor,
                    limit=BUDGET_COUNT_PAGE_LIMIT,
                )
            )
            total += len(page.items)
            cursor = page.next_cursor
            if not cursor:
                break
        return total

    return _count
