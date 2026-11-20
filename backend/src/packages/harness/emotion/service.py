"""emotion 域服务——M 层情绪状态的读取 / 增量推进 / 摘要行产出。

持久化契约（人格状态表——layer=M 的 JSON 文档）::

    ff_persona_states.state = {
        "emotion": {"values": {"joy": 0.62, ...}, "updated_at_ms": 1760000000000},
        ...（其他 M 层字段保序保留）
    }

设计决策：

    - **状态访问面可注入**：:class:`EmotionStoreFace` 是结构化协议
      （load / save 全文档）；真实实现 :class:`PersonaStateEmotionStore`
      经 ``PersonaService.get_state / upsert_state`` 走 M 层状态表，
      测试注入内存 fake（纯函数面永不 tight-couple SQL）；
    - **时钟注入**：``clock`` 可注入（衰减 / 更新时刻的确定性测试面）；
      缺省 ``core.time_utils.now_ms``；
    - **宽容语义**：读取到非法 / 缺键文档一律视为「无情绪状态」
      （debug 记录）——协议面永不因陈旧数据抛异常；引擎停用
      （``EMOTION_ENABLED=false``）时本服务不读不写；
    - **写侧合并**：保存时保留 M 文档内其他键（心情备注 / 关系评价
      等由他人格状态消费者写入），只替换 ``emotion`` 键。

消费者面：chat 服务 reply 路径（推进 + 持久化 + meta 快照）与 compose
心情块（摘要行）；未来 TTS 风格 / 前端表情消费面直接读
本服务 current_state（见 docs/backend/emotion.md）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from loguru import logger
from pydantic import ValidationError

from packages.harness.core.time_utils import now_ms
from packages.harness.database.session import session_scope
from packages.harness.emotion import core
from packages.harness.emotion.emotion_models import (
    DIMENSION_LABELS,
    EMOTION_DOCUMENT_KEY,
    EmotionConfig,
    EmotionDimension,
    EmotionSettings,
    EmotionStateModel,
)
from packages.harness.persona.dtos import PersonaStateUpsertDTO
from packages.harness.persona.models import PersonaStateLayer
from packages.harness.persona.service import PersonaService

__all__ = [
    "EmotionService",
    "EmotionStoreFace",
    "PersonaStateEmotionStore",
]


class EmotionStoreFace(Protocol):
    """情绪状态访问面协议（结构化类型——M 层状态文档的 load / save）。

    真实实现 :class:`PersonaStateEmotionStore` 结构满足；测试注入内存
    fake 同形态（引擎纯函数面与 SQL 解耦的唯一接触点）。
    """

    async def load(self, persona_id: int) -> dict[str, Any] | None:
        """读取人格 M 层状态文档（未写过该层返回 None）。

        Args:
            persona_id: 人格主键。

        Returns:
            M 层文档字典；该层未写过时为 None。
        """
        ...

    async def save(self, persona_id: int, document: dict[str, Any]) -> None:
        """整体写入人格 M 层状态文档（upsert——整体替换语义）。

        Args:
            persona_id: 人格主键。
            document: 待写入的 M 层文档（调用方负责合并不相关键）。
        """
        ...


class PersonaStateEmotionStore:
    """M 层状态文档存取的真实实现（人格状态表——``ff_persona_states``）。"""

    def __init__(self, *, persona_service: PersonaService | None = None) -> None:
        """初始化存取面。

        Args:
            persona_service: 人格注册 / 状态服务（None = 现场构造无状态
                默认实例——服务无内部状态，安全共享）。
        """
        self._persona_service = persona_service if persona_service is not None else PersonaService()

    async def load(self, persona_id: int) -> dict[str, Any] | None:
        """读取 M 层状态文档（独立短事务——与其他状态消费者同口径）。

        Args:
            persona_id: 人格主键。

        Returns:
            M 层文档字典；未写过该层时为 None。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``——状态不能挂在
                不存在的人格上，透传）。
        """
        async with session_scope() as session:
            dto = await self._persona_service.get_state(session, persona_id, PersonaStateLayer.M)
        return dict(dto.state) if dto is not None else None

    async def save(self, persona_id: int, document: dict[str, Any]) -> None:
        """写入 M 层状态文档（upsert——整体替换）。

        Args:
            persona_id: 人格主键。
            document: 待写入文档（调用方已完成无关键合并）。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``，透传）。
        """
        async with session_scope() as session:
            await self._persona_service.upsert_state(
                session,
                persona_id,
                PersonaStateLayer.M,
                PersonaStateUpsertDTO(state=document),
            )


def _state_from_document(document: Mapping[str, Any] | None) -> EmotionStateModel | None:
    """M 层文档 → 情绪状态模型（缺键 / 非法文档宽容回落 None）。

    Args:
        document: M 层文档字典（None = 未写过该层）。

    Returns:
        情绪状态模型；无 ``emotion`` 键或形态非法时为 None。
    """
    if document is None:
        return None
    payload = document.get(EMOTION_DOCUMENT_KEY)
    if not isinstance(payload, dict):
        return None
    try:
        return EmotionStateModel.model_validate(payload)
    except ValidationError as exc:
        logger.debug("M 层情绪文档非法（已忽略）: reason={}", exc)
        return None


def _initial_state(config: EmotionConfig, at_ms: int) -> EmotionStateModel:
    """全维中性初始状态（首轮推进的基线文档）。

    Args:
        config: 引擎参数（中性基线）。
        at_ms: 初始时间锚（epoch 毫秒）。

    Returns:
        全部维度取中性基线、时间锚为 ``at_ms`` 的状态模型。
    """
    return EmotionStateModel(
        values={
            dimension.value: config.neutral_baseline for dimension in EmotionDimension
        },
        updated_at_ms=at_ms,
    )


class EmotionService:
    """轻情绪引擎服务（M 态读取 / 增量推进 / 摘要行——宽容协议面）。

    构造注入（测试 / 装配友好）：环境开关 / 引擎参数 / 状态访问面 /
    时钟均可显式注入；缺省形态读取 ``EMOTION_`` env 面（``EmotionSettings``）
    并按真实人格状态表存取。引擎停用（``EMOTION_ENABLED=false``）时
    :meth:`apply_deltas` 与 :meth:`summary` 均为空操作（零读写）。
    """

    def __init__(
        self,
        *,
        settings: EmotionSettings | None = None,
        config: EmotionConfig | None = None,
        store: EmotionStoreFace | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            settings: 环境开关（None = 现场构造 ``EmotionSettings`` 读取
                ``EMOTION_`` env）。
            config: 引擎参数（None = 由 settings 派生——构造注入优先，
                测试确定性面）。
            store: 状态访问面（None = 真实人格状态表实现）。
            clock: 时钟面（None = ``core.time_utils.now_ms``——衰减 /
                更新时刻的注入点）。
        """
        self._settings = settings if settings is not None else EmotionSettings()
        self._config = config if config is not None else self._settings.to_config()
        self._store: EmotionStoreFace = (
            store if store is not None else PersonaStateEmotionStore()
        )
        self._clock: Callable[[], int] = clock if clock is not None else now_ms

    @property
    def enabled(self) -> bool:
        """引擎是否启用（``EMOTION_ENABLED``——默认 True）。"""
        return self._settings.enabled

    @property
    def config(self) -> EmotionConfig:
        """当前引擎参数（半衰期 / 基线 / 摘要阈值）。"""
        return self._config

    @property
    def dimension_labels(self) -> dict[str, str]:
        """维度中文展示名表副本（消费面展示辅助）。"""
        return dict(DIMENSION_LABELS)

    async def current_state(self, persona_id: int) -> EmotionStateModel | None:
        """读取并衰减到当前时刻的情绪状态（只读——不回写）。

        Args:
            persona_id: 人格主键。

        Returns:
            衰减后的状态模型；该人格从未推进过情绪时为 None。
        """
        document = await self._store.load(persona_id)
        state = _state_from_document(document)
        if state is None:
            return None
        return core.decay_toward_baseline(state, self._clock(), self._config)

    async def apply_deltas(
        self, persona_id: int, deltas: Mapping[str, float]
    ) -> EmotionStateModel | None:
        """把一轮情绪增量推进到 M 态并持久化（引擎停用 / 空增量 → None）。

        语义：读取当前文档 → 衰减到此刻 → 叠加增量 → 合并写回
        （``emotion`` 键替换、其他 M 层键保留）。空增量不产生写事务
        （不合规模型零成本）。

        Args:
            persona_id: 人格主键。
            deltas: 增量字典（维度 → 增量；未知维度由引擎丢弃）。

        Returns:
            推进后的状态模型（meta 快照面）；停用或空增量为 None。
        """
        if not self._settings.enabled or not deltas:
            return None
        document = await self._store.load(persona_id)
        current = _state_from_document(document)
        at_ms = self._clock()
        base = (
            current
            if current is not None
            else _initial_state(self._config, at_ms)
        )
        decayed = core.decay_toward_baseline(base, at_ms, self._config)
        advanced = core.apply_deltas(decayed, deltas, at_ms)
        merged: dict[str, Any] = dict(document) if document is not None else {}
        merged[EMOTION_DOCUMENT_KEY] = advanced.model_dump()
        await self._store.save(persona_id, merged)
        logger.debug(
            "情绪状态已推进: persona_id={}, dims={}, updated_at_ms={}",
            persona_id,
            sorted(advanced.values),
            advanced.updated_at_ms,
        )
        return advanced

    async def summary(self, persona_id: int) -> str | None:
        """生成当前情绪摘要行（compose 心情块注入面）。

        Args:
            persona_id: 人格主键。

        Returns:
            中文摘要行（如 ``当前情绪：愉悦↑、羞怯↑``）；停用 / 无状态 /
            全中性时为 None。
        """
        if not self._settings.enabled:
            return None
        state = await self.current_state(persona_id)
        if state is None:
            return None
        return core.summarize_emotion(state, self._config)
