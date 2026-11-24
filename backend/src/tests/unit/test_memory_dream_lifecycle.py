"""harness/memory dreaming 生命周期测试——归档阶段 / 冻结过滤 / 置顶豁免。

覆盖面：归档阶段（旧 active 策展行按强度判定批量归档 + stats.archived +
decisions(action=archived / reason=strength_below_threshold / curated_id)）；
冻结双面（冻结主题不进合并 prompt 主题列表 + 提案目标为冻结主题时整条
丢弃、merged_from 逐事件落 skipped/frozen 决策）；置顶豁免（不归档）；
阶段顺序（归档发生在合并 / 日记之后——日记产物 id 已写入运行行）。

全部经假网关（零真实网络）；DB 经共享 SQLite fixture；时钟注入。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.dreaming_orm as dreaming_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.dreaming import (
    ARCHIVE_REASON_STRENGTH,
    SKIP_REASON_FROZEN,
    DreamService,
    select_archive_candidates,
)
from packages.harness.memory.dreaming_models import (
    DreamDecisionAction,
    DreamRunStatsModel,
)
from packages.harness.memory.dtos import (
    CuratedQueryDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
)
from packages.harness.memory.lifecycle import MS_PER_DAY, LifecycleConfig
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.orm import CuratedMemoryRow
from packages.harness.memory.service import MemoryService
from packages.harness.persona.service import bind_persona_context

#: 测试人格上下文轴（字符串轴——PersonaMixin 隔离轴）。
PERSONA_A = "dream-lifecycle"
#: 注入时钟（epoch 毫秒——窗口 / 强度判定参考轴）。
NOW = 1_760_000_000_000

#: 事实提取阶段的空提案响应（阶段序：gate → facts → merge；铺底用）。
_FACTS_EMPTY = "[]"

memory_service = MemoryService()


class FakeDreamGateway:
    """假 LLM 网关（按序弹出响应 + 捕获 prompt——零真实网络）。"""

    def __init__(self, responses: Sequence[str]) -> None:
        """初始化（响应队列）。"""
        self._responses = list(responses)
        self.calls: list[str] = []

    async def chat_by_role(
        self,
        role: Any,
        messages: Any,
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """捕获 prompt 并弹出响应。"""
        self.calls.append(messages[0].content)
        if not self._responses:
            raise AssertionError("假网关响应队列耗尽（调用次数超出装配）")
        return ChatResponseModel(content=self._responses.pop(0))


def _chat_dto(*, user: str, ts_ms: int, importance: int = 5) -> EpisodeCreateDTO:
    """构造 chat 回合事件 DTO（窗口铺底）。"""
    return EpisodeCreateDTO(
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(user_content=user, assistant_content="好"),
        importance=importance,
        provenance=MemoryProvenance.OWNER,
        ts_ms=ts_ms,
    )


async def _age_curated(db_session: AsyncSession, memory_id: int, *, days: int) -> None:
    """把策展行锚点时间回拨（生命周期年龄铺底——避免等待真实时间）。"""
    row = (
        await db_session.execute(
            select(CuratedMemoryRow).where(CuratedMemoryRow.id == memory_id)
        )
    ).scalar_one()
    row.created_at = NOW - days * MS_PER_DAY
    await db_session.commit()


def _dream_service(lifecycle_config: LifecycleConfig | None = None) -> DreamService:
    """构造梦境服务（共享记忆服务——归档走真实服务路径）。"""
    return DreamService(
        memory_service=memory_service,
        lifecycle_config=lifecycle_config if lifecycle_config is not None else LifecycleConfig(),
    )


# ---------------------------------------------------------------------------
# 纯函数——归档候选选择
# ---------------------------------------------------------------------------


async def test_select_archive_candidates_filters_and_orders(
    db_session: AsyncSession,
) -> None:
    """候选选择：仅超龄低强度 active 行；置顶 / 冻结 / 新行排除；id 升序。"""
    async with bind_persona_context(PERSONA_A):
        old = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="陈旧记忆", content="很久以前", importance=2
            ),
        )
        pinned = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="置顶记忆", content="重要", importance=2
            ),
        )
        frozen = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="冻结记忆", content="保护", importance=2
            ),
        )
        fresh = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="新记忆", content="刚记下", importance=2
            ),
        )
        await memory_service.set_curated_pinned(db_session, pinned.id, pinned=True)
        await memory_service.set_curated_frozen(db_session, frozen.id, frozen=True)
        await _age_curated(db_session, old.id, days=90)
        await _age_curated(db_session, pinned.id, days=90)
        await _age_curated(db_session, frozen.id, days=90)
        topics = await memory_service.query_curated(
            db_session, CuratedQueryDTO(status=CuratedStatus.ACTIVE, limit=50)
        )
    candidates = select_archive_candidates(
        topics.items, now_ms=NOW, cfg=LifecycleConfig()
    )
    assert [candidate.id for candidate in candidates] == [old.id]
    assert fresh.id not in [candidate.id for candidate in candidates]


# ---------------------------------------------------------------------------
# 编排——归档阶段
# ---------------------------------------------------------------------------


async def test_run_dream_archives_stale_curated(db_session: AsyncSession) -> None:
    """归档阶段：旧低强度行归档 + stats.archived + decisions(archived) 落库。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天很安静", ts_ms=NOW - 1_000)
        )
        stale = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="陈旧主题", content="旧事", importance=2
            ),
        )
        kept = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="新鲜主题", content="新事", importance=8
            ),
        )
        await _age_curated(db_session, stale.id, days=90)
        await _age_curated(db_session, kept.id, days=90)
        gateway = FakeDreamGateway([_FACTS_EMPTY, "[]", "[]", "今天很安静，心里很平稳。"])
        run = await _dream_service().run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", clock_ms=lambda: NOW
        )
        row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == stale.id)
            )
        ).scalar_one()
        kept_row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == kept.id)
            )
        ).scalar_one()
    assert run.status == "success"
    assert run.stats.archived == 1
    archived_decisions = [
        decision for decision in run.decisions if decision.action == DreamDecisionAction.ARCHIVED
    ]
    assert len(archived_decisions) == 1
    decision = archived_decisions[0]
    assert decision.curated_id == stale.id
    assert decision.episode_id == 0
    assert decision.reason == ARCHIVE_REASON_STRENGTH
    assert decision.topic == "陈旧主题"
    assert row.status == CuratedStatus.ARCHIVED.value
    assert row.active_marker is None
    assert kept_row.status == CuratedStatus.ACTIVE.value
    # 阶段顺序：日记先于归档（日记事件 id 已落运行行）。
    assert run.diary_episode_id is not None


async def test_run_dream_does_not_archive_pinned_or_frozen(
    db_session: AsyncSession,
) -> None:
    """置顶 / 冻结豁免：超龄低强度但受保护的行不归档（stats.archived=0）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天很安静", ts_ms=NOW - 1_000)
        )
        pinned = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="置顶保护", content="置顶", importance=2
            ),
        )
        frozen = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="冻结保护", content="冻结", importance=2
            ),
        )
        await memory_service.set_curated_pinned(db_session, pinned.id, pinned=True)
        await memory_service.set_curated_frozen(db_session, frozen.id, frozen=True)
        await _age_curated(db_session, pinned.id, days=120)
        await _age_curated(db_session, frozen.id, days=120)
        gateway = FakeDreamGateway([_FACTS_EMPTY, "[]", "[]", "今天很安静。"])
        run = await _dream_service().run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", clock_ms=lambda: NOW
        )
    assert run.stats.archived == 0
    assert [d for d in run.decisions if d.action == DreamDecisionAction.ARCHIVED] == []


async def test_archive_pass_skips_candidate_superseded_before_execution(
    db_session: AsyncSession,
) -> None:
    """候选快照后失效：执行点复核跳过——零归档、零决策、统计诚实。"""
    async with bind_persona_context(PERSONA_A):
        stale = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="快照陈旧", content="旧版", importance=2
            ),
        )
        await _age_curated(db_session, stale.id, days=90)
        snapshot = await memory_service.query_curated(
            db_session, CuratedQueryDTO(status=CuratedStatus.ACTIVE, limit=50)
        )
        replacement = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="快照陈旧", content="新版", importance=8
            ),
        )
        stats = DreamRunStatsModel()
        decisions: list[Any] = []
        archived = await _dream_service()._archive_pass(
            db_session,
            run_now=NOW,
            active_topics=list(snapshot.items),
            stats=stats,
            decisions=decisions,
        )
        stale_row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == stale.id)
            )
        ).scalar_one()
    assert archived == 0
    assert stats.archived == 0
    assert decisions == []
    assert stale_row.status == CuratedStatus.SUPERSEDED.value
    assert replacement.id != stale.id


# ---------------------------------------------------------------------------
# 冻结语义——prompt 排除 + 提案丢弃
# ---------------------------------------------------------------------------


async def test_frozen_topic_excluded_from_merge_prompt(
    db_session: AsyncSession,
) -> None:
    """冻结主题不进合并 prompt 主题列表（数据面过滤——模板字节零变化）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="主人提到养猫", ts_ms=NOW - 1_000, importance=8)
        )
        frozen = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="冻结主题键", content="不要动这条"
            ),
        )
        await memory_service.set_curated_frozen(db_session, frozen.id, frozen=True)
        visible = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="可见主题键", content="可以合并"
            ),
        )
        gateway = FakeDreamGateway([_FACTS_EMPTY, "[]", "[]", "今天很安静。"])
        run = await _dream_service().run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", clock_ms=lambda: NOW
        )
    assert run.status == "success"
    merge_prompt = gateway.calls[2]
    assert "冻结主题键" not in merge_prompt
    assert "可见主题键" in merge_prompt
    assert visible.id != frozen.id


async def test_frozen_topic_proposal_dropped_with_decision(
    db_session: AsyncSession,
) -> None:
    """提案目标为冻结主题：整条丢弃 + merged_from 逐事件落 skipped/frozen 决策。"""
    async with bind_persona_context(PERSONA_A):
        episode = await memory_service.append_episode(
            db_session, _chat_dto(user="主人说喜欢下雨天", ts_ms=NOW - 1_000, importance=9)
        )
        frozen = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="天气偏好", content="旧版天气偏好"
            ),
        )
        await memory_service.set_curated_frozen(db_session, frozen.id, frozen=True)
        proposal = json.dumps(
            [
                {
                    "topic": "天气偏好",
                    "kind": "memory",
                    "content": "主人喜欢下雨天",
                    "importance": 7,
                    "merged_from": [episode.id],
                }
            ],
            ensure_ascii=False,
        )
        gateway = FakeDreamGateway([_FACTS_EMPTY, "[]", proposal, "今天下雨，心情很好。"])
        run = await _dream_service().run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", clock_ms=lambda: NOW
        )
        frozen_row = (
            await db_session.execute(
                select(CuratedMemoryRow).where(CuratedMemoryRow.id == frozen.id)
            )
        ).scalar_one()
    assert run.status == "success"
    assert frozen_row.version == 1  # 未被合并提案改写链
    assert frozen_row.content == "旧版天气偏好"
    frozen_decisions = [
        decision for decision in run.decisions if decision.reason == SKIP_REASON_FROZEN
    ]
    assert [decision.episode_id for decision in frozen_decisions] == [episode.id]
    assert all(
        decision.action == DreamDecisionAction.SKIPPED for decision in frozen_decisions
    )
    assert run.stats.promoted == 0
    assert run.stats.superseded == 0


async def test_archive_runs_even_without_merge_candidates(
    db_session: AsyncSession,
) -> None:
    """无候选事件：归档阶段照常运行（窗口空不影响生命周期整理）。"""
    async with bind_persona_context(PERSONA_A):
        stale = await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="空窗口陈旧", content="旧事", importance=1
            ),
        )
        await _age_curated(db_session, stale.id, days=90)
        gateway = FakeDreamGateway([])
        run = await _dream_service(LifecycleConfig()).run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", clock_ms=lambda: NOW
        )
    assert run.status == "success"
    assert run.stats.archived == 1
    assert run.diary_episode_id is None
    assert gateway.calls == []
