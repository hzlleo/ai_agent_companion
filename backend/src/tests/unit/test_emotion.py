"""emotion 域测试——维度值域 / 增量叠加 / 半衰期衰减 / 摘要规则 / 服务持久化。

测试策略：纯函数面直调（时钟注入、无 I/O）+ 服务面注入内存 fake 存储
（零 SQL——真实人格状态表存取经 chat 集成测试覆盖）；环境开关经
monkeypatch 显式注入（不触碰进程真实 .env）。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from packages.harness.emotion import core
from packages.harness.emotion.emotion_models import (
    DIMENSION_LABELS,
    EMOTION_DOCUMENT_KEY,
    EmotionConfig,
    EmotionDeltaModel,
    EmotionDimension,
    EmotionSettings,
    EmotionStateModel,
)
from packages.harness.emotion.service import EmotionService

# ---------------------------------------------------------------------------
# 值域模型
# ---------------------------------------------------------------------------


async def test_dimension_enum_covers_eight_with_labels() -> None:
    """维度枚举：8 维齐备且中文展示名齐全（协议值域真源）。"""
    assert len(EmotionDimension) == 8
    assert [dimension.value for dimension in EmotionDimension] == [
        "joy",
        "affection",
        "shyness",
        "excitement",
        "calm",
        "sadness",
        "anxiety",
        "annoyance",
    ]
    assert set(DIMENSION_LABELS) == {dimension.value for dimension in EmotionDimension}


async def test_state_model_drops_unknown_and_clamps() -> None:
    """状态模型：未知维度丢弃、数值夹取 0..1（宽容边界）。"""
    state = EmotionStateModel(values={"joy": 1.7, "sadness": -0.4, "wat": 0.3}, updated_at_ms=1)
    assert state.values == {"joy": 1.0, "sadness": 0.0}


async def test_delta_model_clamps_to_unit_range() -> None:
    """增量模型：数值夹取 -1..1、未知维度丢弃。"""
    delta = EmotionDeltaModel(values={"joy": 5, "sadness": -9, "nope": 0.2})
    assert delta.values == {"joy": 1.0, "sadness": -1.0}


async def test_config_defaults_and_constraints() -> None:
    """引擎参数缺省：半衰期 6 小时 / 基线 0.5 / 阈值 0.15；非法值拒绝。"""
    config = EmotionConfig()
    assert config.decay_half_life_ms == 6 * 60 * 60 * 1000
    assert config.neutral_baseline == 0.5
    assert config.summary_threshold == 0.15
    with pytest.raises(ValueError):
        EmotionConfig(decay_half_life_ms=0)
    with pytest.raises(ValueError):
        EmotionConfig(summary_threshold=0)


async def test_settings_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMOTION_ env 开关：默认开启；显式关闭 + 参数覆写生效。"""
    monkeypatch.delenv("EMOTION_ENABLED", raising=False)
    assert EmotionSettings().enabled is True
    monkeypatch.setenv("EMOTION_ENABLED", "false")
    monkeypatch.setenv("EMOTION_DECAY_HALF_LIFE_MS", "3600000")
    settings = EmotionSettings()
    assert settings.enabled is False
    assert settings.to_config().decay_half_life_ms == 3600000

# ---------------------------------------------------------------------------
# 纯函数算法
# ---------------------------------------------------------------------------


async def test_apply_deltas_adds_and_clamps() -> None:
    """增量叠加：维度值累加并夹取 0..1、更新时间推进、入参不变。"""
    state = EmotionStateModel(values={"joy": 0.9}, updated_at_ms=1000)
    advanced = core.apply_deltas(state, {"joy": 0.5, "sadness": -0.2}, now_ms=2000)
    assert advanced.values["joy"] == 1.0
    assert advanced.values["sadness"] == 0.3
    assert advanced.updated_at_ms == 2000
    assert state.values == {"joy": 0.9}


async def test_apply_deltas_missing_dimension_starts_from_neutral() -> None:
    """未记录维度：叠加起点为中性缺省 0.5。"""
    state = EmotionStateModel(values={}, updated_at_ms=0)
    advanced = core.apply_deltas(state, {"affection": 0.25}, now_ms=10)
    assert advanced.values["affection"] == 0.75


async def test_apply_deltas_unknown_dimension_ignored() -> None:
    """未知维度增量：忽略（零副作用——不抛异常）。"""
    state = EmotionStateModel(values={"joy": 0.5}, updated_at_ms=0)
    advanced = core.apply_deltas(state, {"melancholy": 0.9}, now_ms=10)
    assert advanced.values == {"joy": 0.5}


async def test_decay_halves_deviation_after_half_life() -> None:
    """衰减：半衰期后偏离量精确减半（基线 0.5——数学锚定）。"""
    config = EmotionConfig()
    state = EmotionStateModel(values={"joy": 0.9}, updated_at_ms=0)
    decayed = core.decay_toward_baseline(state, config.decay_half_life_ms, config)
    assert decayed.values["joy"] == pytest.approx(0.7, abs=1e-9)


async def test_decay_two_half_lives_quarters_deviation() -> None:
    """衰减：两个半衰期后偏离量为四分之一。"""
    config = EmotionConfig()
    state = EmotionStateModel(values={"sadness": 0.1}, updated_at_ms=0)
    decayed = core.decay_toward_baseline(state, config.decay_half_life_ms * 2, config)
    assert decayed.values["sadness"] == pytest.approx(0.4, abs=1e-9)


async def test_decay_clock_rollback_is_zero_elapsed() -> None:
    """时钟回拨（now < updated）：按零耗时处理（状态原样）。"""
    config = EmotionConfig()
    state = EmotionStateModel(values={"joy": 0.8}, updated_at_ms=5000)
    decayed = core.decay_toward_baseline(state, 1000, config)
    assert decayed.values == {"joy": 0.8}
    assert decayed.updated_at_ms == 1000


async def test_summarize_lists_non_neutral_dims_by_magnitude() -> None:
    """摘要：非中性维度按偏离幅度降序 + 方向标记。"""
    config = EmotionConfig()
    state = EmotionStateModel(
        values={"joy": 0.9, "shyness": 0.72, "calm": 0.52, "sadness": 0.3},
        updated_at_ms=0,
    )
    summary = core.summarize_emotion(state, config)
    assert summary is not None
    assert summary.startswith("当前情绪：")
    assert summary.index("愉悦↑") < summary.index("羞怯↑") < summary.index("低落↓")
    assert "平静" not in summary


async def test_summarize_neutral_returns_none() -> None:
    """摘要：全中性（含阈值边缘内）返回 None。"""
    config = EmotionConfig()
    state = EmotionStateModel(values={"joy": 0.6, "sadness": 0.44}, updated_at_ms=0)
    assert core.summarize_emotion(state, config) is None
    assert core.is_neutral(state, config) is True


async def test_is_neutral_false_when_threshold_reached() -> None:
    """中性判定：偏离恰达阈值即视为非中性。"""
    config = EmotionConfig()
    state = EmotionStateModel(values={"anxiety": 0.65}, updated_at_ms=0)
    assert core.is_neutral(state, config) is False

# ---------------------------------------------------------------------------
# 服务面（内存 fake 存储 + 注入时钟）
# ---------------------------------------------------------------------------


class FakeStore:
    """内存状态存储（记录读写——零 SQL）。"""

    def __init__(self) -> None:
        """初始化空存储。"""
        self.documents: dict[int, dict[str, Any]] = {}
        self.loads: list[int] = []
        self.saves: list[tuple[int, dict[str, Any]]] = []

    async def load(self, persona_id: int) -> dict[str, Any] | None:
        """读取文档副本（None = 未写过）。"""
        self.loads.append(persona_id)
        document = self.documents.get(persona_id)
        return copy.deepcopy(document) if document is not None else None

    async def save(self, persona_id: int, document: dict[str, Any]) -> None:
        """写入文档副本（记录写入事件）。"""
        self.saves.append((persona_id, copy.deepcopy(document)))
        self.documents[persona_id] = copy.deepcopy(document)


def _service(store: FakeStore, *, enabled: bool = True, now: int = 1000) -> EmotionService:
    """构造注入式服务（固定时钟——确定性面）。"""
    return EmotionService(
        settings=EmotionSettings(enabled=enabled),
        store=store,
        clock=lambda: now,
    )


async def test_service_apply_persists_merged_document() -> None:
    """服务推进：写入 M 文档 emotion 键，其他 M 层键保留（合并写）。"""
    store = FakeStore()
    store.documents[7] = {"relationship": {"score": 3}, EMOTION_DOCUMENT_KEY: None}
    service = _service(store, now=1000)
    advanced = await service.apply_deltas(7, {"joy": 0.3})
    assert advanced is not None
    assert advanced.values["joy"] == pytest.approx(0.8)
    saved = store.saves[-1][1]
    assert saved["relationship"] == {"score": 3}
    assert saved[EMOTION_DOCUMENT_KEY]["values"]["joy"] == pytest.approx(0.8)
    assert saved[EMOTION_DOCUMENT_KEY]["updated_at_ms"] == 1000


async def test_service_apply_initializes_full_baseline_state() -> None:
    """服务推进：首轮（无状态）初始化全维基线后叠加（8 维齐备）。"""
    store = FakeStore()
    service = _service(store, now=1000)
    advanced = await service.apply_deltas(7, {"anxiety": 0.2})
    assert advanced is not None
    assert set(advanced.values) == {dimension.value for dimension in EmotionDimension}
    assert advanced.values["anxiety"] == pytest.approx(0.7)
    assert advanced.values["joy"] == pytest.approx(0.5)


async def test_service_apply_empty_deltas_no_write() -> None:
    """空增量：零读写（不合规模型零成本）。"""
    store = FakeStore()
    service = _service(store)
    assert await service.apply_deltas(7, {}) is None
    assert store.saves == []
    assert store.loads == []


async def test_service_disabled_is_noop() -> None:
    """引擎停用：推进 / 摘要均空操作（零存储访问）。"""
    store = FakeStore()
    store.documents[7] = {
        EMOTION_DOCUMENT_KEY: {"values": {"joy": 0.9}, "updated_at_ms": 0}
    }
    service = _service(store, enabled=False)
    assert service.enabled is False
    assert await service.apply_deltas(7, {"joy": 0.5}) is None
    assert await service.summary(7) is None
    assert store.loads == []


async def test_service_summary_decays_before_describing() -> None:
    """摘要：读取后按注入时钟衰减（半衰期后偏离减半——愉悦保留 / 微值与阈值关系）。"""
    store = FakeStore()
    store.documents[7] = {
        EMOTION_DOCUMENT_KEY: {"values": {"joy": 0.9}, "updated_at_ms": 0}
    }
    config = EmotionConfig()
    service = EmotionService(
        settings=EmotionSettings(enabled=True),
        config=config,
        store=store,
        clock=lambda: config.decay_half_life_ms,
    )
    summary = await service.summary(7)
    assert summary is not None
    assert "愉悦↑" in summary
    # 衰减后偏离 0.4（> 0.15 阈值）——摘要仍非空；再过一个半衰期回落到 0.7 偏离 0.2。
    assert store.saves == []  # 摘要只读，不回写


async def test_service_summary_neutral_state_returns_none() -> None:
    """摘要：状态全中性 / 无状态 → None（compose 不注入心情块）。"""
    store = FakeStore()
    store.documents[7] = {
        EMOTION_DOCUMENT_KEY: {"values": {"joy": 0.55}, "updated_at_ms": 0}
    }
    service = _service(store, now=1000)
    assert await service.summary(7) is None
    assert await service.summary(9) is None


async def test_service_current_state_ignores_malformed_document() -> None:
    """非法 M 文档（emotion 键形态错误）：宽容回落 None（不抛异常）。"""
    store = FakeStore()
    store.documents[7] = {EMOTION_DOCUMENT_KEY: "not-a-dict"}
    service = _service(store)
    assert await service.current_state(7) is None
