"""关系进展派生纯函数测试——得分矩阵 / 衰减分档 / 阶段阶梯 / 夹取与裁剪。

测试策略：全部输入为手工构造的事实记录，时间由 ``now_ms`` 固定注入——
不读系统时钟、不触数据库，断言逐字节可复现。覆盖面：

    - 空事实 / 混类过滤 / 历史状态过滤 → 空摘要（stranger / 0）；
    - 阶段阶梯上下沿（熟悉 / 亲近 / 亲密下限的 下限-ε 与 下限 两侧）；
    - 时间衰减档位边界（每档上限的 含边界 与 越界 两侧）+ 未来时间戳夹 0；
    - 人格侧等级上限夹取（``intimacy_ceiling`` 与阈值表 ``max_intimacy_level``）
      及阶段回落，``score`` 保留原值；
    - 最近事件条数上限 / 时间倒序 / fact_id 倒序；摘要空白折叠与字符截断；
    - 边界短语识别与条数上限；确定性重复调用一致；
    - 阈值表结构不变式（阶梯递增 / 分档递增 / 末档无上限 / 权重非递增）；
    - 时间轴非法输入（负 ``now_ms`` / 负事实时间戳）拒绝。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.facts_models import (
    FactRecordDTO,
    PersonaFactCategory,
    PersonaFactSource,
    PersonaFactStatus,
)
from packages.harness.persona.relationship import (
    build_relationship_summary,
    decay_weight,
    derive_stage,
    empty_relationship_summary,
)
from packages.harness.persona.relationship_models import (
    RELATIONSHIP_STAGE_LABELS,
    RelationshipDecayBandModel,
    RelationshipStage,
    RelationshipThresholdsModel,
)

#: 固定派生时刻（2023-11-14 22:13:20 UTC——所有年龄断言以此为锚）。
_NOW_MS: int = 1_700_000_000_000

#: 一天的毫秒数（年龄构造与档位断言共用）。
_DAY_MS: int = 86_400_000


def _fact(
    fact_id: int = 1,
    *,
    content: str = "主人喜欢被叫小胖鱼",
    confidence: float = 0.8,
    updated_at: int | None = None,
    category: PersonaFactCategory = PersonaFactCategory.RELATIONSHIP,
    status: PersonaFactStatus = PersonaFactStatus.ACTIVE,
) -> FactRecordDTO:
    """构造事实记录（缺省为现行关系类事实——派生输入铺底）。"""
    ts = _NOW_MS if updated_at is None else updated_at
    return FactRecordDTO(
        id=fact_id,
        category=category,
        content=content,
        confidence=confidence,
        status=status,
        provenance=MemoryProvenance.OWNER,
        source=PersonaFactSource.MANUAL,
        evidence=[],
        superseded_by_id=None,
        version=1,
        created_at=ts,
        updated_at=ts,
    )


async def test_empty_facts_yield_stranger_zero() -> None:
    """空事实：stranger / 0 / 零事件 / 零边界，生成时刻如实注入。"""
    summary = build_relationship_summary([], now_ms=_NOW_MS)
    assert summary.stage is RelationshipStage.STRANGER
    assert summary.intimacy_level == 0
    assert summary.score == 0.0
    assert summary.event_count == 0
    assert summary.recent_events == []
    assert summary.boundaries == []
    assert summary.generated_at_ms == _NOW_MS


async def test_empty_relationship_summary_matches_empty_facts() -> None:
    """降级空摘要与空事实派生结果一致（读取失败路径的语义同源）。"""
    assert empty_relationship_summary(now_ms=_NOW_MS) == build_relationship_summary(
        [], now_ms=_NOW_MS
    )


async def test_non_relationship_and_non_active_facts_are_filtered() -> None:
    """混类 / 历史状态记录不参与派生（读取面过滤的纵深防御）。"""
    summary = build_relationship_summary(
        [
            _fact(1, category=PersonaFactCategory.PREFERENCE, confidence=1.0),
            _fact(2, status=PersonaFactStatus.SUPERSEDED, confidence=1.0),
            _fact(3, status=PersonaFactStatus.REJECTED, confidence=1.0),
        ],
        now_ms=_NOW_MS,
    )
    assert summary.event_count == 0
    assert summary.score == 0.0
    assert summary.stage is RelationshipStage.STRANGER


async def test_stage_ladder_lower_and_upper_edges() -> None:
    """阶段阶梯：下限-ε 落前一段，恰好下限进入该段（含边界）。"""
    # importance = confidence × 10，新鲜事实 decay = 1.0。
    assert derive_stage(1.99) is RelationshipStage.STRANGER
    assert derive_stage(2.0) is RelationshipStage.FAMILIAR
    assert derive_stage(9.99) is RelationshipStage.FAMILIAR
    assert derive_stage(10.0) is RelationshipStage.CLOSE
    assert derive_stage(23.99) is RelationshipStage.CLOSE
    assert derive_stage(24.0) is RelationshipStage.INTIMATE


async def test_score_uses_importance_times_decay() -> None:
    """得分 = Σ importance × decay：单条低置信新鲜事实落在熟悉段之前。"""
    summary = build_relationship_summary([_fact(confidence=0.199)], now_ms=_NOW_MS)
    assert summary.score == pytest.approx(1.99)
    assert summary.intimacy_level == 0

    boundary = build_relationship_summary([_fact(confidence=0.2)], now_ms=_NOW_MS)
    assert boundary.score == pytest.approx(2.0)
    assert boundary.stage is RelationshipStage.FAMILIAR
    assert boundary.intimacy_level == 1


async def test_three_strong_fresh_facts_reach_intimate() -> None:
    """三条满置信新鲜事实：得分 30 → 亲密 / 等级 3。"""
    summary = build_relationship_summary(
        [
            _fact(1, confidence=1.0),
            _fact(2, confidence=1.0),
            _fact(3, confidence=0.4),
        ],
        now_ms=_NOW_MS,
    )
    assert summary.score == pytest.approx(24.0)
    assert summary.stage is RelationshipStage.INTIMATE
    assert summary.intimacy_level == 3


@pytest.mark.parametrize(
    ("age_days", "expected_weight"),
    [
        (0, 1.0),
        (7, 1.0),
        (30, 0.75),
        (90, 0.5),
        (365, 0.25),
        (3650, 0.1),
    ],
)
async def test_decay_weight_band_edges_inclusive(age_days: int, expected_weight: float) -> None:
    """衰减分档：每档上限当天仍按该档计权（含边界）。"""
    assert decay_weight(age_days * _DAY_MS) == pytest.approx(expected_weight)


@pytest.mark.parametrize(
    ("age_days", "expected_weight"),
    [
        (8, 0.75),
        (31, 0.5),
        (91, 0.25),
        (366, 0.1),
    ],
)
async def test_decay_weight_rolls_over_after_band(age_days: int, expected_weight: float) -> None:
    """衰减分档：越过上限一天即降档（边界另一侧）。"""
    assert decay_weight(age_days * _DAY_MS) == pytest.approx(expected_weight)


async def test_decay_weight_clamps_future_timestamps_to_zero_age() -> None:
    """未来时间戳（时钟回拨）按年龄 0 处理——落最年轻档。"""
    assert decay_weight(-5 * _DAY_MS) == pytest.approx(1.0)
    summary = build_relationship_summary(
        [_fact(updated_at=_NOW_MS + 10 * _DAY_MS, confidence=1.0)],
        now_ms=_NOW_MS,
    )
    assert summary.score == pytest.approx(10.0)


async def test_old_fact_decay_reduces_score() -> None:
    """陈旧事实：得分按档位衰减（365 天前的满置信事实只值 2.5 分）。"""
    summary = build_relationship_summary(
        [_fact(updated_at=_NOW_MS - 365 * _DAY_MS, confidence=1.0)],
        now_ms=_NOW_MS,
    )
    assert summary.score == pytest.approx(2.5)
    assert summary.stage is RelationshipStage.FAMILIAR


async def test_intimacy_ceiling_clamps_level_and_stage() -> None:
    """人格侧等级上限：等级夹取到上限、阶段回落，原始得分不回写。"""
    facts = [_fact(1, confidence=1.0), _fact(2, confidence=1.0), _fact(3, confidence=1.0)]
    summary = build_relationship_summary(facts, now_ms=_NOW_MS, intimacy_ceiling=1)
    assert summary.score == pytest.approx(30.0)
    assert summary.intimacy_level == 1
    assert summary.stage is RelationshipStage.FAMILIAR


async def test_intimacy_ceiling_edges_are_clamped() -> None:
    """上限越界：负值夹到 0、超过 3 夹到 3。"""
    facts = [_fact(1, confidence=1.0), _fact(2, confidence=1.0), _fact(3, confidence=1.0)]
    zero = build_relationship_summary(facts, now_ms=_NOW_MS, intimacy_ceiling=-4)
    assert zero.intimacy_level == 0
    assert zero.stage is RelationshipStage.STRANGER
    top = build_relationship_summary(facts, now_ms=_NOW_MS, intimacy_ceiling=9)
    assert top.intimacy_level == 3
    assert top.stage is RelationshipStage.INTIMATE


async def test_threshold_max_intimacy_level_caps_derivation() -> None:
    """阈值表等级上界：上界 2 时满分也只到亲近（阶段随等级回落）。"""
    table = RelationshipThresholdsModel(max_intimacy_level=2)
    facts = [_fact(1, confidence=1.0), _fact(2, confidence=1.0), _fact(3, confidence=1.0)]
    summary = build_relationship_summary(facts, now_ms=_NOW_MS, thresholds=table)
    assert summary.intimacy_level == 2
    assert summary.stage is RelationshipStage.CLOSE


async def test_custom_threshold_table_changes_ladder() -> None:
    """注入阈值表：熟悉下限调低后同一事实进入熟悉段（构造注入面可用）。"""
    table = RelationshipThresholdsModel(stage_familiar_min_score=0.5)
    summary = build_relationship_summary(
        [_fact(confidence=0.1)], now_ms=_NOW_MS, thresholds=table
    )
    assert summary.score == pytest.approx(1.0)
    assert summary.stage is RelationshipStage.FAMILIAR


async def test_recent_events_capped_and_ordered_newest_first() -> None:
    """最近事件：最多 5 条、按时间倒序（同刻按 fact_id 倒序），计数含全量。"""
    facts = [
        _fact(index, content=f"关系进展 {index}", updated_at=_NOW_MS - index * _DAY_MS)
        for index in range(1, 8)
    ]
    summary = build_relationship_summary(facts, now_ms=_NOW_MS)
    assert summary.event_count == 7
    assert [event.fact_id for event in summary.recent_events] == [1, 2, 3, 4, 5]
    timestamps = [event.ts_ms for event in summary.recent_events]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_event_summary_collapses_whitespace_and_truncates() -> None:
    """事件摘要：空白折叠为单空格，超帽按字符截断（80 字符）。"""
    content = "主人\n\n今天   说了很多\t关于未来的事" + "呀" * 200
    summary = build_relationship_summary(
        [_fact(content=content)], now_ms=_NOW_MS
    )
    event = summary.recent_events[0]
    assert "\n" not in event.summary
    assert "  " not in event.summary
    assert len(event.summary) == 80
    assert event.importance == pytest.approx(8.0)


async def test_boundaries_detected_from_markers_and_capped() -> None:
    """边界约定：命中边界短语的事实按时间倒序入列，最多 3 条。"""
    facts = [
        _fact(1, content="说好不熬夜", updated_at=_NOW_MS),
        _fact(2, content="不要叫别人宝贝", updated_at=_NOW_MS - _DAY_MS),
        _fact(3, content="边界是睡前不聊工作", updated_at=_NOW_MS - 2 * _DAY_MS),
        _fact(4, content="不许翻旧账", updated_at=_NOW_MS - 3 * _DAY_MS),
        _fact(5, content="周末喜欢去爬山", updated_at=_NOW_MS - 4 * _DAY_MS),
    ]
    summary = build_relationship_summary(facts, now_ms=_NOW_MS)
    assert summary.boundaries == ["说好不熬夜", "不要叫别人宝贝", "边界是睡前不聊工作"]


async def test_summary_is_deterministic_for_same_inputs() -> None:
    """确定性：同一事实集与同一时刻的两次派生逐字段一致。"""
    facts = [
        _fact(2, content="说好一起看电影", confidence=0.7, updated_at=_NOW_MS - 3 * _DAY_MS),
        _fact(1, content="主人开始叫我小鱼", confidence=0.9, updated_at=_NOW_MS - _DAY_MS),
    ]
    first = build_relationship_summary(facts, now_ms=_NOW_MS)
    second = build_relationship_summary(facts, now_ms=_NOW_MS)
    assert first.model_dump() == second.model_dump()


async def test_stage_labels_cover_all_stages() -> None:
    """阶段中文称谓：四段齐备（提示词阶段行的映射真源）。"""
    assert RELATIONSHIP_STAGE_LABELS == {
        RelationshipStage.STRANGER: "初识",
        RelationshipStage.FAMILIAR: "熟悉",
        RelationshipStage.CLOSE: "亲近",
        RelationshipStage.INTIMATE: "亲密",
    }


async def test_negative_now_ms_rejected() -> None:
    """派生时刻为负：拒绝（时间轴统一 epoch 毫秒）。"""
    with pytest.raises(ValueError):
        build_relationship_summary([], now_ms=-1)


async def test_negative_fact_timestamp_rejected() -> None:
    """事实时间戳为负：拒绝（装配层错误）。"""
    with pytest.raises(ValueError):
        build_relationship_summary([_fact(updated_at=-1)], now_ms=_NOW_MS)


async def test_threshold_model_rejects_broken_ladder() -> None:
    """阈值表不变式：阶段下限必须严格递增。"""
    with pytest.raises(ValidationError):
        RelationshipThresholdsModel(stage_familiar_min_score=10.0, stage_close_min_score=5.0)


async def test_threshold_model_rejects_broken_decay_bands() -> None:
    """阈值表不变式：分档年龄递增 / 权重非递增 / 只有末档无上限。"""
    with pytest.raises(ValidationError):
        RelationshipThresholdsModel(
            decay_bands=(
                RelationshipDecayBandModel(age_days_max=30, weight=1.0),
                RelationshipDecayBandModel(age_days_max=7, weight=0.5),
            )
        )
    with pytest.raises(ValidationError):
        RelationshipThresholdsModel(
            decay_bands=(
                RelationshipDecayBandModel(age_days_max=7, weight=0.5),
                RelationshipDecayBandModel(age_days_max=30, weight=1.0),
            )
        )
    with pytest.raises(ValidationError):
        RelationshipThresholdsModel(
            decay_bands=(
                RelationshipDecayBandModel(age_days_max=None, weight=1.0),
                RelationshipDecayBandModel(age_days_max=30, weight=0.5),
            )
        )
