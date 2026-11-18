"""persona 域关系进展纯逻辑——事实聚合 / 时间衰减 / 阶段派生 / 摘要构建。

本模块是关系进展摘要的唯一算法实现（无 I/O、无会话、不读系统时钟）：
输入是人物事实层读出的关系类现行事实记录，输出是一份确定性摘要。时间
一律由调用方注入 ``now_ms``（Linux Epoch 毫秒），同一输入在同一时刻的
输出逐字节稳定，测试可固定在任意时刻断言。

计算链（锁定——阈值与系数见 ``persona.relationship_models``）::

    过滤（category=relationship 且 status=active）
      → 按 (updated_at 降序, id 降序) 排序（确定性求和序）
      → importance = confidence × importance_scale
      → decay(age) 按年龄分档取权（未来时间戳按年龄 0 处理）
      → score = Σ importance × decay（夹取 4 位小数）
      → stage / intimacy_level 由得分阶梯派生并夹取 0-3
      → 最近事件（时间倒序 ≤ recent_event_limit）+ 边界约定（命中边界
        短语 ≤ boundary_limit）

人格侧等级上限：``intimacy_ceiling`` 供调用方传入「人格 / 预设允许的最
高亲密等级」；``None`` 表示运行期拿不到该值（当前聊天上下文的实际形态
——预设等级只存在于生成期），此时只输出事实派生值并在阶段行如实标注。
传入上限时等级按 0-3 夹取，阶段随夹取后的等级回落（摘要里的 ``score``
保留原始得分，不回写夹取值）。

读取失败 / 空事实的降级语义由调用方决定：空事实列表即产出 stranger / 0
的空摘要（:func:`empty_relationship_summary`），聊天链路在读取异常时同
样回落到该形态并记 warning——摘要块是增强面，不阻断回复。
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.harness.persona.facts_models import FactRecordDTO, PersonaFactStatus
from packages.harness.persona.relationship_models import (
    DEFAULT_RELATIONSHIP_THRESHOLDS,
    RELATIONSHIP_BOUNDARY_MARKERS,
    RELATIONSHIP_FACT_CATEGORY,
    RelationshipEventModel,
    RelationshipStage,
    RelationshipSummaryModel,
    RelationshipThresholdsModel,
)

__all__ = [
    "decay_weight",
    "derive_stage",
    "build_relationship_summary",
    "empty_relationship_summary",
]

#: 一天的毫秒数（衰减分档以「天」表达，比较前统一换算——禁止散落魔数）。
_MILLISECONDS_PER_DAY: int = 86_400_000

#: 亲密等级 → 关系阶段（夹取后的回落映射——等级与阶段必须自洽）。
_STAGE_BY_LEVEL: dict[int, RelationshipStage] = {
    0: RelationshipStage.STRANGER,
    1: RelationshipStage.FAMILIAR,
    2: RelationshipStage.CLOSE,
    3: RelationshipStage.INTIMATE,
}


def decay_weight(
    age_ms: int, thresholds: RelationshipThresholdsModel | None = None
) -> float:
    """按事实年龄取时间衰减权重（分档表见锁定阈值模型）。

    分档语义（锁定）：年龄以天为单位落入首个覆盖该年龄的档位（档位
    上限含边界）；未来时间戳（``age_ms < 0``）按年龄 0 处理，落最年轻
    档——时钟回拨不应把事实权重打到最低。

    Args:
        age_ms: 事实年龄（毫秒；负值按 0 处理）。
        thresholds: 阈值表（None = 锁定缺省表）。

    Returns:
        该年龄档的衰减权重（0-1）。
    """
    table = thresholds if thresholds is not None else DEFAULT_RELATIONSHIP_THRESHOLDS
    effective_age = max(age_ms, 0)
    for band in table.decay_bands:
        if band.age_days_max is None or effective_age <= band.age_days_max * _MILLISECONDS_PER_DAY:
            return band.weight
    return table.decay_bands[-1].weight


def derive_stage(
    score: float, thresholds: RelationshipThresholdsModel | None = None
) -> RelationshipStage:
    """由关系得分派生阶段（阈值阶梯 + 等级上界夹取）。

    阶梯语义（锁定）：得分达到某段下限即进入该段（含边界）；等级上界由
    阈值表的 ``max_intimacy_level`` 约束，阶段随夹取后的等级回落——例如
    上界为 2 时，再高的得分也只到「亲近」。

    Args:
        score: 关系得分（衰减加权和）。
        thresholds: 阈值表（None = 锁定缺省表）。

    Returns:
        派生出的关系阶段。
    """
    table = thresholds if thresholds is not None else DEFAULT_RELATIONSHIP_THRESHOLDS
    return _STAGE_BY_LEVEL[_derive_level(score, table)]


def build_relationship_summary(
    facts: Sequence[FactRecordDTO],
    *,
    now_ms: int,
    thresholds: RelationshipThresholdsModel | None = None,
    intimacy_ceiling: int | None = None,
) -> RelationshipSummaryModel:
    """把关系类现行事实聚合为关系进展摘要（确定性纯函数）。

    输入过滤只保留 ``category=relationship`` 且 ``status=active`` 的记录
    ——读取面已按类别 / 状态过滤，此处再过滤一层保证直调面的语义自洽
    （传入混类 / 历史记录不会污染摘要）。排序、求和与列表截取全部按
    ``(updated_at 降序, id 降序)`` 进行，同输入必得同输出。

    Args:
        facts: 事实记录序列（可用读取面返回的整页——本函数自行过滤类别
            与状态）。
        now_ms: 派生时刻（Linux Epoch 毫秒——调用方注入，模块内不读系统
            时钟）。
        thresholds: 阈值表（None = 锁定缺省表）。
        intimacy_ceiling: 人格侧亲密等级上限（None = 拿不到上限，只输出
            事实派生值；传入时按 0-3 夹取，阶段随夹取后的等级回落）。

    Returns:
        关系进展摘要（空事实时为 stranger / 0 / 零事件）。

    Raises:
        ValueError: ``now_ms`` / 事实时间戳为负（时间轴统一 epoch 毫秒，
            负值属装配层错误）。
    """
    if now_ms < 0:
        raise ValueError("关系进展的 now_ms 不能为负（Linux Epoch 毫秒）")
    table = thresholds if thresholds is not None else DEFAULT_RELATIONSHIP_THRESHOLDS
    candidates = [
        fact
        for fact in facts
        if fact.category == RELATIONSHIP_FACT_CATEGORY
        and fact.status == PersonaFactStatus.ACTIVE
    ]
    ordered = sorted(candidates, key=lambda fact: (fact.updated_at, fact.id), reverse=True)
    score = 0.0
    events: list[RelationshipEventModel] = []
    boundaries: list[str] = []
    for fact in ordered:
        if fact.updated_at < 0:
            raise ValueError("关系类事实的 updated_at 不能为负（Linux Epoch 毫秒）")
        importance = round(min(max(fact.confidence, 0.0), 1.0) * table.importance_scale, 2)
        score += importance * decay_weight(now_ms - fact.updated_at, table)
        summary = _normalize_summary(fact.content, table.event_summary_max_chars)
        if not summary:
            continue
        if len(events) < table.recent_event_limit:
            events.append(
                RelationshipEventModel(
                    fact_id=fact.id,
                    summary=summary,
                    importance=importance,
                    ts_ms=fact.updated_at,
                )
            )
        if len(boundaries) < table.boundary_limit and _is_boundary(fact.content):
            boundaries.append(summary)
    level = _derive_level(round(score, 4), table)
    if intimacy_ceiling is not None:
        level = min(level, max(0, min(3, intimacy_ceiling)))
    return RelationshipSummaryModel(
        stage=_STAGE_BY_LEVEL[level],
        intimacy_level=level,
        score=round(score, 4),
        event_count=len(candidates),
        recent_events=events,
        boundaries=boundaries,
        generated_at_ms=now_ms,
    )


def empty_relationship_summary(
    *,
    now_ms: int,
    thresholds: RelationshipThresholdsModel | None = None,
) -> RelationshipSummaryModel:
    """构造空摘要（stranger / 0 / 零事件——空事实与读取失败的降级形态）。

    Args:
        now_ms: 派生时刻（Linux Epoch 毫秒）。
        thresholds: 阈值表（None = 锁定缺省表——空摘要只用到等级上界）。

    Returns:
        stranger 阶段的空摘要。
    """
    return build_relationship_summary((), now_ms=now_ms, thresholds=thresholds)


def _derive_level(score: float, thresholds: RelationshipThresholdsModel) -> int:
    """由得分派生亲密等级（阈值阶梯 + 等级上界夹取）。

    Args:
        score: 关系得分。
        thresholds: 阈值表。

    Returns:
        0-3 的亲密等级（不超过阈值表的等级上界）。
    """
    if score >= thresholds.stage_intimate_min_score:
        raw_level = 3
    elif score >= thresholds.stage_close_min_score:
        raw_level = 2
    elif score >= thresholds.stage_familiar_min_score:
        raw_level = 1
    else:
        raw_level = 0
    return min(raw_level, thresholds.max_intimacy_level)


def _normalize_summary(content: str, max_chars: int) -> str:
    """归一化并截断事件摘要（空白折叠 + 字符截断）。

    Args:
        content: 事实正文。
        max_chars: 摘要字符上限。

    Returns:
        单行摘要（空正文返回空串）。
    """
    collapsed = " ".join(content.split())
    return collapsed[:max_chars]


def _is_boundary(content: str) -> bool:
    """判定事实正文是否表达相处边界（命中边界短语表）。

    Args:
        content: 事实正文。

    Returns:
        True = 命中边界短语（进入摘要 boundaries 列表）。
    """
    return any(marker in content for marker in RELATIONSHIP_BOUNDARY_MARKERS)
