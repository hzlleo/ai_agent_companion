"""LLM 条件路由策略求值——纯函数裁决 / 时段换算 / 目标绑定检查。

执行边界（锁定）：

    - 本模块只消费策略列表、本轮输入（角色 / 亲密度 / 时刻 / 摘要可用性 /
      用途）与解析器；**不读数据库、不读系统时钟、不发请求**——``now_ms``
      由调用方注入（测试可用固定时刻构造时段判定）；
    - 求值顺序固定为：按原角色过滤 → 优先级降序（同优先级声明序）→ 首个
      条件全命中（AND）的策略 → 目标角色绑定检查；先命中者即结论；
    - 目标未绑定且策略声明回落 → 原角色直连（``fallback_unbound``）；声明
      不回落 → ``BusinessException(MODEL_NOT_FOUND)``（details 含策略名）；
    - 无策略命中 → 原角色直连（``direct``）；
    - 纯函数不产生日志副作用；命中/回落日志由网关在调用点记录。

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
        3. 逐条检查条件（亲密度区间 / 摘要可用性 / 时段 / 用途——AND）；
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
        裁决结果（生效角色 + 命中策略名 + 原因）。

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
    for _, policy in ordered:
        if policy.source_role != source_role:
            continue
        if not _condition_matches(
            policy.condition,
            intimacy_level=intimacy_level,
            now_ms=now_ms,
            has_summary=has_summary,
            purpose=purpose,
        ):
            continue
        return _decide_target(source_role=source_role, policy=policy, resolver=resolver)
    return RouteDecisionModel(
        effective_role=source_role,
        policy_name=None,
        reason=RouteDecisionReason.DIRECT,
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
    hour = datetime.fromtimestamp(now_ms / 1000, tz=ZoneInfo(window.timezone)).hour
    if window.end_hour > window.start_hour:
        return window.start_hour <= hour < window.end_hour
    return hour >= window.start_hour or hour < window.end_hour


def _decide_target(
    *,
    source_role: str,
    policy: RoutePolicyModel,
    resolver: ModelResolver,
) -> RouteDecisionModel:
    """检查目标角色绑定并给出裁决（未绑定按策略回落或抛错）。

    Args:
        source_role: 本轮原角色名。
        policy: 已命中条件的策略。
        resolver: 模型解析器。

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
            )
        raise
    if target_entry is not None:
        return RouteDecisionModel(
            effective_role=policy.target_role,
            policy_name=policy.name,
            reason=RouteDecisionReason.ROUTED,
        )
    if policy.fallback_to_source:
        return RouteDecisionModel(
            effective_role=source_role,
            policy_name=policy.name,
            reason=RouteDecisionReason.FALLBACK_UNBOUND,
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
) -> bool:
    """判定条件是否全命中（各维度 AND；未声明的维度不约束）。

    Args:
        condition: 命中条件。
        intimacy_level: 本轮亲密等级（None = 摘要不可用）。
        now_ms: 本轮时刻（epoch 毫秒）。
        has_summary: 关系摘要是否可用。
        purpose: 本轮调用用途。

    Returns:
        True 表示全部已声明维度均命中。
    """
    if condition.min_intimacy_level is not None and (
        intimacy_level is None or intimacy_level < condition.min_intimacy_level
    ):
        return False
    if condition.max_intimacy_level is not None and (
        intimacy_level is None or intimacy_level > condition.max_intimacy_level
    ):
        return False
    if condition.requires_summary is not None and has_summary is not condition.requires_summary:
        return False
    if condition.purposes and purpose not in condition.purposes:
        return False
    return condition.time_window is None or time_window_matches(
        condition.time_window, now_ms=now_ms
    )
