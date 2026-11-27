"""LLM 条件路由策略求值——纯函数裁决 / 时段换算 / 目标绑定检查。

执行边界（锁定）：

    - 本模块只消费策略列表、本轮输入（角色 / 亲密度 / 时刻 / 摘要可用性 /
      用途）与解析器；**不读数据库、不读系统时钟、不发请求**——``now_ms``
      由调用方注入（测试可用固定时刻构造时段判定）；
    - 求值顺序固定为：按原角色过滤 → 优先级降序（同优先级声明序）→ 首个
      条件全命中（AND）的策略 → 目标角色绑定检查；先命中者即结论；
    - 每条源角色匹配的策略都进入求值轨迹（未命中策略也保留逐维结果，供
      排障定位卡在哪个维度）；无源匹配策略时轨迹为空；
    - 目标未绑定且策略声明回落 → 原角色直连（``fallback_unbound``）；声明
      不回落 → ``BusinessException(MODEL_NOT_FOUND)``（details 含策略名）；
    - 无策略命中 → 原角色直连（``direct``）；
    - 纯函数不产生日志副作用；命中/回落/查询轨迹日志由网关在调用点记录。

依赖方向：llm → config（``ModelResolver`` 类型，仅调用解析面）+ 本域
``route_policy_models``；不消费 pools / usage（网关持有）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from packages.harness.config.model_entry import ModelRole
from packages.harness.config.model_resolver import ModelResolver
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.route_policy_models import (
    ConditionResultModel,
    PolicyEvalTraceModel,
    RouteConditionModel,
    RouteDecisionModel,
    RouteDecisionReason,
    RoutePolicyModel,
    TimeWindowModel,
)

__all__ = ["evaluate_route", "time_window_matches"]


def evaluate_route(
    role: ModelRole | str,
    policies: Sequence[RoutePolicyModel],
    *,
    intimacy_level: int | None,
    now_ms: int,
    has_summary: bool,
    purpose: str,
    resolver: ModelResolver,
) -> RouteDecisionModel:
    """裁决本轮生效角色（确定性纯函数——无 I/O、无日志）。

    求值顺序（先命中者即结论）：

        1. 仅保留 ``source_role`` 等于本轮原角色的策略；
        2. 按 ``priority`` 降序、同优先级按声明序排序；
        3. 逐条检查条件（亲密度区间 / 摘要可用性 / 时段 / 用途——AND），
           每条源匹配策略的逐维结果写入 ``trace``；
        4. 首个条件全命中的策略进入目标绑定检查：已绑定 → ``routed``；
           未绑定且允许回落 → ``fallback_unbound``；未绑定且禁止回落 →
           抛 ``MODEL_NOT_FOUND``（details 含策略名）；
        5. 无策略命中 → ``direct``。

    Args:
        role: 本轮原角色（枚举成员或角色名）。
        policies: 条件路由策略列表（声明序）。
        intimacy_level: 本轮关系摘要的亲密等级（0-3；None = 摘要不可用）。
        now_ms: 本轮时刻（epoch 毫秒——时段判定唯一时间来源）。
        has_summary: 关系摘要是否可用（``requires_summary`` 判定输入）。
        purpose: 本轮调用用途（``resolved_purpose``——对话类型维度输入）。
        resolver: 模型解析器（目标角色绑定检查）。

    Returns:
        裁决结果（生效角色 + 命中策略名 + 原因 + 源匹配策略求值轨迹）。

    Raises:
        ValueError: ``intimacy_level`` 越出 0-3（调用方编程错误）。
        BusinessException: 策略命中但目标未绑定且禁止回落
            （``MODEL_NOT_FOUND``，details 含策略名 / 原角色 / 目标角色）。
    """
    if intimacy_level is not None and not 0 <= intimacy_level <= 3:
        raise ValueError(f"intimacy_level 须在 0-3 之间: {intimacy_level!r}")

    source_role = role.value if isinstance(role, ModelRole) else str(role)
    ordered = sorted(
        enumerate(policies),
        key=lambda item: (-item[1].priority, item[0]),
    )
    trace: list[PolicyEvalTraceModel] = []
    for _, policy in ordered:
        if policy.source_role != source_role:
            continue
        results = _condition_matches(
            policy.condition,
            intimacy_level=intimacy_level,
            now_ms=now_ms,
            has_summary=has_summary,
            purpose=purpose,
        )
        trace.append(
            PolicyEvalTraceModel(
                policy_name=policy.name,
                source_matched=True,
                conditions=results,
            )
        )
        if any(result.constrained and not result.matched for result in results):
            continue
        return _decide_target(
            source_role=source_role,
            policy=policy,
            resolver=resolver,
            trace=tuple(trace),
        )
    return RouteDecisionModel(
        effective_role=source_role,
        policy_name=None,
        reason=RouteDecisionReason.DIRECT,
        trace=tuple(trace),
    )


def time_window_matches(window: TimeWindowModel, *, now_ms: int) -> bool:
    """判定注入时刻是否落在时段窗口内（跨午夜窗口按两段并集处理）。

    窗口语义：``end_hour > start_hour`` 为同日半开区间 ``[start, end)``；
    ``end_hour <= start_hour`` 为跨午夜，即 ``[start, 24) ∪ [0, end)``
    （``end_hour == start_hour`` 因此覆盖全天）。

    Args:
        window: 时段窗口（时区已校验）。
        now_ms: 判定时刻（epoch 毫秒）。

    Returns:
        True 表示该时刻在窗口内。
    """
    hour = _window_hour(window, now_ms=now_ms)
    if window.end_hour > window.start_hour:
        return window.start_hour <= hour < window.end_hour
    return hour >= window.start_hour or hour < window.end_hour


def _window_hour(window: TimeWindowModel, *, now_ms: int) -> int:
    """换算注入时刻在窗口时区的小时（判定与排障摘要同源）。

    Args:
        window: 时段窗口（时区已校验）。
        now_ms: 判定时刻（epoch 毫秒）。

    Returns:
        该时刻在窗口时区的小时（0-23）。
    """
    return datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(window.timezone)).hour


def _decide_target(
    *,
    source_role: str,
    policy: RoutePolicyModel,
    resolver: ModelResolver,
    trace: tuple[PolicyEvalTraceModel, ...],
) -> RouteDecisionModel:
    """检查目标角色绑定并给出裁决（未绑定按策略回落或抛错）。

    Args:
        source_role: 本轮原角色名。
        policy: 已命中条件的策略。
        resolver: 模型解析器。
        trace: 截至该策略的求值轨迹（随裁决返回供排障）。

    Returns:
        裁决结果（``routed`` / ``fallback_unbound`` / ``fallback_error``）。

    Raises:
        BusinessException: 目标未绑定且 ``fallback_to_source=False``
            （``MODEL_NOT_FOUND``）。
    """
    try:
        target_entry = resolver.resolve_optional(policy.target_role)
    except BusinessException:
        if policy.fallback_to_source:
            return RouteDecisionModel(
                effective_role=source_role,
                policy_name=policy.name,
                reason=RouteDecisionReason.FALLBACK_ERROR,
                trace=trace,
            )
        raise
    if target_entry is not None:
        return RouteDecisionModel(
            effective_role=policy.target_role,
            policy_name=policy.name,
            reason=RouteDecisionReason.ROUTED,
            trace=trace,
        )
    if policy.fallback_to_source:
        return RouteDecisionModel(
            effective_role=source_role,
            policy_name=policy.name,
            reason=RouteDecisionReason.FALLBACK_UNBOUND,
            trace=trace,
        )
    raise BusinessException(
        code=ErrorCode.MODEL_NOT_FOUND,
        message=f"条件路由策略的目标角色未绑定模型条目: policy={policy.name}",
        details={
            "reason": "目标角色未绑定模型条目",
            "policy": policy.name,
            "source_role": source_role,
            "target_role": policy.target_role,
        },
    )


def _condition_matches(
    condition: RouteConditionModel,
    *,
    intimacy_level: int | None,
    now_ms: int,
    has_summary: bool,
    purpose: str,
) -> tuple[ConditionResultModel, ...]:
    """求值条件各维度并返回逐项结果（各维度 AND；未声明维度不产生项）。

    Args:
        condition: 命中条件。
        intimacy_level: 本轮亲密等级（None = 摘要不可用）。
        now_ms: 本轮时刻（epoch 毫秒）。
        has_summary: 关系摘要是否可用。
        purpose: 本轮调用用途。

    Returns:
        已声明维度的求值结果元组；全部项均非「已约束且未命中」时策略命中
        （空元组即恒命中）。
    """
    results: list[ConditionResultModel] = []
    if condition.min_intimacy_level is not None:
        min_level = condition.min_intimacy_level
        matched = intimacy_level is not None and intimacy_level >= min_level
        detail = (
            f"level unavailable < min={min_level}"
            if intimacy_level is None
            else f"level={intimacy_level} {'>=' if matched else '<'} min={min_level}"
        )
        results.append(
            ConditionResultModel(
                dimension="intimacy", constrained=True, matched=matched, detail=detail
            )
        )
    if condition.max_intimacy_level is not None:
        max_level = condition.max_intimacy_level
        matched = intimacy_level is not None and intimacy_level <= max_level
        detail = (
            f"level unavailable > max={max_level}"
            if intimacy_level is None
            else f"level={intimacy_level} {'<=' if matched else '>'} max={max_level}"
        )
        results.append(
            ConditionResultModel(
                dimension="intimacy", constrained=True, matched=matched, detail=detail
            )
        )
    if condition.requires_summary is not None:
        matched = has_summary is condition.requires_summary
        if condition.requires_summary:
            detail = "summary available" if matched else "summary unavailable"
        else:
            detail = "summary unavailable" if matched else "summary available"
        results.append(
            ConditionResultModel(
                dimension="summary", constrained=True, matched=matched, detail=detail
            )
        )
    if condition.purposes:
        matched = purpose in condition.purposes
        allowed = "{" + ",".join(condition.purposes) + "}"
        results.append(
            ConditionResultModel(
                dimension="purposes",
                constrained=True,
                matched=matched,
                detail=f"purpose={purpose} {'in' if matched else 'not in'} {allowed}",
            )
        )
    if condition.time_window is not None:
        matched = time_window_matches(condition.time_window, now_ms=now_ms)
        hour = _window_hour(condition.time_window, now_ms=now_ms)
        window = f"[{condition.time_window.start_hour},{condition.time_window.end_hour})"
        results.append(
            ConditionResultModel(
                dimension="time_window",
                constrained=True,
                matched=matched,
                detail=f"hour={hour} {'in' if matched else 'not in'} {window}",
            )
        )
    return tuple(results)
