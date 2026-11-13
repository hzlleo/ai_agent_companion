"""harness/memory dreaming 测试——纯函数数学 / 规则门 / fail-closed 编排全链。

覆盖面：recency 半衰期精确数学（边界 = 0.5 / 单调衰减 / 未来钳位）；
dream_score 公式与加成应用；规则门（untrusted 硬排除——含 prompt 缺席、
既往晋升跳过、分数门、上限截断）；run_dream 窗口边界（左闭右开）；
LLM 合并 happy path（provenance=agent 晋升 + supersede 链推进 + 决策
记录 + 日记落库 + prompt 人格中立性 / 声线替换）；fail-closed（非法
JSON 零晋升 / 非数组拒绝 / 空输出拒绝 / LLM 异常落 failed）；提案逐条
校验（未知 merged_from 丢弃计数 / 字段越界丢弃）；日记失败保留晋升；
存储相关度命中（boost 应用 / store=None 降级 / 检索参数透传）；运行
日志生命周期（running → 终态 / 读面 / 人格隔离）。

全部经假网关 / 假存储（零真实网络）；DB 经共享 SQLite fixture。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.dreaming_orm as dreaming_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.config.model_entry import ModelRole
from packages.harness.core.errors import BusinessException
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.dreaming import (
    MS_PER_DAY,
    DreamService,
    dream_score,
    parse_merge_proposals,
    recency_decay,
    select_dream_candidates,
)
from packages.harness.memory.dreaming_models import (
    DreamDecisionAction,
    DreamJobConfig,
    DreamProposedItemModel,
    DreamRunQueryDTO,
)
from packages.harness.memory.dreaming_orm import DreamRunRow
from packages.harness.memory.dtos import (
    CuratedQueryDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
    EpisodeQueryDTO,
    EpisodeRecordDTO,
)
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.orm import CuratedMemoryRow
from packages.harness.memory.retrieval_models import (
    MemoryRefType,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.service import bind_persona_context

#: 测试人格上下文轴（字符串轴——PersonaMixin 隔离轴）。
PERSONA_A = "dream-alpha"
PERSONA_B = "dream-beta"
#: 数值人格轴（主题相关度检索面——索引 persona_id 列为 BigInt）。
PERSONA_NUM = 701

#: 注入时钟（epoch 毫秒——窗口 / 打分的参考轴）。
NOW = 1_760_000_000_000

memory_service = MemoryService()
dream_service = DreamService()

#: 24 小时窗口毫秒数（窗口边界断言用）。
DAY_MS = 24 * 3_600_000


class FakeDreamGateway:
    """假 LLM 网关（chat_by_role 调用捕获 + 排队响应——零真实网络）。

    Attributes:
        calls: 逐次调用捕获（role / messages / persona_id / purpose）。
    """

    def __init__(self, responses: Sequence[str]) -> None:
        """初始化（响应按序弹出；队列耗尽即测试装配错误）。"""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

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
        """按角色调用（捕获参数；弹出下一个排队响应）。"""
        self.calls.append(
            {
                "role": role,
                "messages": list(messages),
                "persona_id": persona_id,
                "purpose": purpose,
            }
        )
        if not self._responses:
            raise AssertionError("假网关响应队列耗尽（调用次数超出装配）")
        return ChatResponseModel(content=self._responses.pop(0))


class FailingDreamGateway(FakeDreamGateway):
    """恒失败网关（LLM 异常路径——fail-closed 断言面）。"""

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
        """按角色调用（恒抛业务异常——LLM 不可用面）。"""
        raise BusinessException(code="LLM_REQUEST_FAILED", message="梦境模型暂不可用（桩）")


class FakeKeywordStore:
    """假检索存储（keyword search 返回预置命中——捕获查询参数）。"""

    def __init__(self, results_by_query: dict[str, list[MemorySearchResultDTO]]) -> None:
        """初始化（按查询文本路由预置结果）。"""
        self._results_by_query = results_by_query
        self.queries: list[MemorySearchQueryDTO] = []

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[MemorySearchResultDTO]:
        """关键词检索（捕获查询；返回预置命中）。"""
        self.queries.append(query)
        return self._results_by_query.get(query.query, [])


def _chat_dto(
    *,
    user: str = "今天吃了很多小鱼干",
    assistant: str = "好棒呀",
    importance: int = 5,
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ts_ms: int | None = None,
) -> EpisodeCreateDTO:
    """构造 chat 回合事件 DTO（测试铺底辅助）。"""
    return EpisodeCreateDTO(
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(user_content=user, assistant_content=assistant),
        importance=importance,
        provenance=provenance,
        ts_ms=ts_ms,
    )


def _episode(
    episode_id: int,
    *,
    ts_ms: int = NOW,
    importance: int = 5,
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
) -> EpisodeRecordDTO:
    """构造最小情景事件记录（纯函数测试铺底）。"""
    return EpisodeRecordDTO(
        id=episode_id,
        persona_id=PERSONA_A,
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(user_content=f"事件{episode_id}", assistant_content="好的"),
        importance=importance,
        provenance=provenance,
        content_hash=f"hash-{episode_id:04d}",
        ts_ms=ts_ms,
        created_at=0,
        updated_at=0,
    )


def _proposal(
    topic: str, episode_ids: list[int], *, kind: str = "memory", importance: int = 5
) -> dict[str, Any]:
    """构造单条合并提案（JSON 数组元素——测试铺底辅助）。"""
    return {
        "topic": topic,
        "kind": kind,
        "content": f"关于{topic}的蒸馏记忆",
        "importance": importance,
        "merged_from": episode_ids,
    }


def _merge_response(proposals: list[dict[str, Any]], *, fenced: bool = True) -> str:
    """构造合并响应文本（可选代码围栏包裹——robust parse 面）。"""
    raw = json.dumps(proposals, ensure_ascii=False)
    return f"```json\n{raw}\n```" if fenced else raw


async def _count_curated(db_session: AsyncSession) -> int:
    """统计策展记忆行数（人格作用域内口径）。"""
    stmt = select(func.count()).select_from(CuratedMemoryRow)
    return int((await db_session.execute(stmt)).scalar_one())


async def _count_runs(db_session: AsyncSession) -> int:
    """统计梦境运行行数（人格作用域内口径）。"""
    stmt = select(func.count()).select_from(DreamRunRow)
    return int((await db_session.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# 纯函数——recency 半衰期精确数学
# ---------------------------------------------------------------------------


async def test_recency_decay_half_life_boundary_exact() -> None:
    """半衰期边界精确 0.5：恰过一个 / 两个半衰期为 0.5 / 0.25。"""
    assert recency_decay(NOW - 30 * MS_PER_DAY, NOW, 30.0) == 0.5
    assert recency_decay(NOW - 60 * MS_PER_DAY, NOW, 30.0) == 0.25
    assert recency_decay(NOW, NOW, 30.0) == 1.0


async def test_recency_decay_monotonic_decreasing() -> None:
    """单调衰减：年龄越大因子越小（严格递减）。"""
    factors = [recency_decay(NOW - day * MS_PER_DAY, NOW, 30.0) for day in range(0, 31, 5)]
    assert factors == sorted(factors, reverse=True)
    assert len(set(factors)) == len(factors)


async def test_recency_decay_future_ts_clamped_to_one() -> None:
    """未来时间戳钳位：不奖励未来（因子 1.0）。"""
    assert recency_decay(NOW + 10 * MS_PER_DAY, NOW, 30.0) == 1.0


async def test_recency_decay_invalid_half_life_rejected() -> None:
    """半衰期非正拒绝（VALIDATION_ERROR——装配错误面）。"""
    with pytest.raises(BusinessException) as exc_info:
        recency_decay(NOW, NOW, 0.0)
    assert exc_info.value.code == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 纯函数——dream_score 公式与加成
# ---------------------------------------------------------------------------


async def test_dream_score_formula_exact() -> None:
    """公式精确：importance × 衰减（无命中 = 无加成）。"""
    record = _episode(1, importance=4, ts_ms=NOW)
    cfg = DreamJobConfig(recency_half_life_days=30.0, boost=0.5)
    assert dream_score(record, NOW, cfg) == 4.0
    aged = _episode(2, importance=4, ts_ms=NOW - 30 * MS_PER_DAY)
    assert dream_score(aged, NOW, cfg) == 2.0


async def test_dream_score_boost_application() -> None:
    """加成应用：命中主题相关度集合 → importance × decay × (1+boost)。"""
    record = _episode(7, importance=4, ts_ms=NOW)
    cfg = DreamJobConfig(recency_half_life_days=30.0, boost=0.5)
    assert dream_score(record, NOW, cfg, topic_hits=frozenset({7})) == 6.0
    assert dream_score(record, NOW, cfg, topic_hits=frozenset({8})) == 4.0


# ---------------------------------------------------------------------------
# 纯函数——规则门（零模型调用）
# ---------------------------------------------------------------------------


async def test_select_untrusted_excluded_and_counted() -> None:
    """untrusted 硬排除：不入 selected、只计数、不产生决策。"""
    trusted = _episode(1, importance=5)
    untrusted = _episode(2, importance=10, provenance=MemoryProvenance.UNTRUSTED)
    selected, stats, decisions = select_dream_candidates(
        [trusted, untrusted], now_ms=NOW, cfg=DreamJobConfig()
    )
    assert [record.id for record in selected] == [1]
    assert stats.candidates == 1
    assert stats.skipped_untrusted == 1
    assert stats.gated == 1
    assert [decision.episode_id for decision in decisions] == []


async def test_select_min_score_gate() -> None:
    """分数门：低于阈值 → skipped 决策（理由含分数）。"""
    low = _episode(1, importance=1, ts_ms=NOW - 90 * MS_PER_DAY)  # 1×0.125=0.125
    selected, stats, decisions = select_dream_candidates(
        [low], now_ms=NOW, cfg=DreamJobConfig(min_score=1.0)
    )
    assert selected == []
    assert stats.gated == 0
    assert decisions[0].action == DreamDecisionAction.SKIPPED
    assert "低于阈值" in decisions[0].reason


async def test_select_cap_by_score_desc() -> None:
    """上限截断：按分数降序取前 N，溢出 skipped（理由含上限）。"""
    episodes = [
        _episode(1, importance=5),
        _episode(2, importance=8),
        _episode(3, importance=3),
    ]
    selected, stats, decisions = select_dream_candidates(
        episodes, now_ms=NOW, cfg=DreamJobConfig(max_candidates=2)
    )
    assert [record.id for record in selected] == [2, 1]
    assert stats.gated == 2
    skipped = [decision for decision in decisions if decision.action == DreamDecisionAction.SKIPPED]
    assert [decision.episode_id for decision in skipped] == [3]
    assert "上限" in skipped[0].reason


async def test_select_already_promoted_skip() -> None:
    """既往晋升跳过：id 命中既往集 → skipped（理由含既往）。"""
    record = _episode(1, importance=9)
    selected, stats, decisions = select_dream_candidates(
        [record],
        now_ms=NOW,
        cfg=DreamJobConfig(),
        already_promoted_ids=frozenset({1}),
    )
    assert selected == []
    assert stats.gated == 0
    assert decisions[0].action == DreamDecisionAction.SKIPPED
    assert "既往" in decisions[0].reason


# ---------------------------------------------------------------------------
# 纯函数——合并输出解析（robust parse）
# ---------------------------------------------------------------------------


async def test_parse_merge_proposals_fenced_valid() -> None:
    """围栏包裹的合法数组解析通过（fence 剥离）。"""
    raw = _merge_response([_proposal("主题A", [1]), _proposal("主题B", [2])])
    valid, dropped = parse_merge_proposals(raw)
    assert dropped == 0
    assert [item.topic for item in valid] == ["主题A", "主题B"]
    assert valid[0].merged_from == [1]


async def test_parse_merge_proposals_invalid_json_rejected() -> None:
    """非法 JSON 拒绝（VALIDATION_ERROR——运行标记 failed 的原因面）。"""
    with pytest.raises(BusinessException) as exc_info:
        parse_merge_proposals("这不是JSON{{{")
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "不是合法 JSON" in exc_info.value.message


async def test_parse_merge_proposals_non_array_rejected() -> None:
    """顶层非数组拒绝（输出契约 = JSON 数组）。"""
    with pytest.raises(BusinessException) as exc_info:
        parse_merge_proposals('{"items": []}')
    assert "必须是 JSON 数组" in exc_info.value.message


async def test_parse_merge_proposals_drops_invalid_items() -> None:
    """逐条校验失败丢弃计数（importance 越界 / merged_from 空）。"""
    raw = _merge_response(
        [
            _proposal("好提案", [1]),
            _proposal("越界", [2], importance=99),
            _proposal("空来源", []),
        ]
    )
    valid, dropped = parse_merge_proposals(raw)
    assert dropped == 2
    assert [item.topic for item in valid] == ["好提案"]
    assert isinstance(valid[0], DreamProposedItemModel)


# ---------------------------------------------------------------------------
# run_dream——窗口边界 / untrusted 硬门
# ---------------------------------------------------------------------------


async def test_run_dream_window_left_closed_right_open(db_session: AsyncSession) -> None:
    """窗口边界：``[now-24h, now)`` 左闭右开——界内含 / 右界与界外不含。"""
    async with bind_persona_context(PERSONA_A):
        at_start = await memory_service.append_episode(
            db_session, _chat_dto(user="窗口起点事件", ts_ms=NOW - DAY_MS, importance=5)
        )
        at_end = await memory_service.append_episode(
            db_session, _chat_dto(user="窗口终点事件", ts_ms=NOW, importance=5)
        )
        before = await memory_service.append_episode(
            db_session, _chat_dto(user="窗口之前事件", ts_ms=NOW - DAY_MS - 1, importance=5)
        )
        gateway = FakeDreamGateway(["[]", "今天很平静。"])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "success"
    assert run.stats.candidates == 1
    merge_prompt = gateway.calls[0]["messages"][0].content
    assert f"事件 #{at_start.id}" in merge_prompt
    assert f"事件 #{at_end.id}" not in merge_prompt
    assert f"事件 #{before.id}" not in merge_prompt


async def test_run_dream_untrusted_excluded_everywhere(db_session: AsyncSession) -> None:
    """untrusted 硬门：不进任何 LLM prompt（合并 + 日记）、只计数、无决策。"""
    async with bind_persona_context(PERSONA_A):
        trusted = await memory_service.append_episode(
            db_session, _chat_dto(user="主人今天很开心", ts_ms=NOW - 1_000, importance=6)
        )
        untrusted = await memory_service.append_episode(
            db_session,
            _chat_dto(
                user="不可信的平台垃圾内容",
                importance=10,
                provenance=MemoryProvenance.UNTRUSTED,
                ts_ms=NOW - 2_000,
            ),
        )
        gateway = FakeDreamGateway(["[]", "今天主人很开心。"])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "success"
    assert run.stats.candidates == 1
    assert run.stats.skipped_untrusted == 1
    merge_prompt = gateway.calls[0]["messages"][0].content
    diary_prompt = gateway.calls[1]["messages"][0].content
    assert "不可信的平台垃圾内容" not in merge_prompt
    assert f"事件 #{untrusted.id}" not in merge_prompt
    assert "不可信的平台垃圾内容" not in diary_prompt
    decision_ids = {decision.episode_id for decision in run.decisions}
    assert untrusted.id not in decision_ids
    assert trusted.id in decision_ids


# ---------------------------------------------------------------------------
# run_dream——合并 happy path / fail-closed
# ---------------------------------------------------------------------------


async def test_run_dream_merge_happy_path(db_session: AsyncSession) -> None:
    """合并 happy path：agent 晋升 + supersede 链推进 + 决策 + 日记 + prompt 契约。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY,
                topic="口味偏好",
                content="主人喜欢小鱼干",
                importance=5,
            ),
        )
        ep1 = await memory_service.append_episode(
            db_session, _chat_dto(user="我今天吃了很多虾干", ts_ms=NOW - 3_600_000, importance=6)
        )
        ep2 = await memory_service.append_episode(
            db_session, _chat_dto(user="我开始每周跑步了", ts_ms=NOW - 7_200_000, importance=5)
        )
        gateway = FakeDreamGateway(
            [
                _merge_response(
                    [_proposal("口味偏好", [ep1.id], importance=6), _proposal("运动习惯", [ep2.id])]
                ),
                "今天主人吃到了喜欢的虾干，还坚持去跑步了，真充实的一天。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert run.status == "success"
        assert run.error is None
        assert run.stats.candidates == 2
        assert run.stats.gated == 2
        assert run.stats.promoted == 1
        assert run.stats.superseded == 1
        assert run.stats.failed == 0
        decision_by_id = {decision.episode_id: decision for decision in run.decisions}
        assert decision_by_id[ep1.id].action == DreamDecisionAction.MERGED
        assert decision_by_id[ep1.id].topic == "口味偏好"
        assert decision_by_id[ep2.id].action == DreamDecisionAction.PROMOTED
        assert decision_by_id[ep2.id].topic == "运动习惯"
        # supersede 链推进：既有 owner v1 → agent v2 active（旧行 superseded）
        page = await memory_service.query_curated(db_session, CuratedQueryDTO(topic="口味偏好"))
        active_rows = [item for item in page.items if item.status == "active"]
        assert len(active_rows) == 1
        assert active_rows[0].version == 2
        assert active_rows[0].provenance == MemoryProvenance.AGENT
        assert active_rows[0].content == "关于口味偏好的蒸馏记忆"
        # 日记落库：kind=dream / source=dream:diary / provenance=agent
        assert run.diary_episode_id is not None
        diary = await memory_service.get_episode(db_session, run.diary_episode_id)
        assert diary.kind == EpisodeKind.DREAM
        assert diary.source == "dream:diary"
        assert diary.provenance == MemoryProvenance.AGENT
        assert diary.importance == 5
        assert "跑步" in diary.content.text
        # prompt 契约：合并人格中立 / 日记声线替换 / 用量归属
        merge_prompt = gateway.calls[0]["messages"][0].content
        assert "大肥鱼" not in merge_prompt
        assert f"事件 #{ep1.id}" in merge_prompt
        assert "- 口味偏好（kind=memory importance=5）：主人喜欢小鱼干" in merge_prompt
        diary_prompt = gateway.calls[1]["messages"][0].content
        assert "你是 大肥鱼" in diary_prompt
        assert gateway.calls[0]["role"] == ModelRole.DREAM
        assert gateway.calls[0]["purpose"] == "dream"
        assert gateway.calls[0]["persona_id"] == PERSONA_A


async def test_run_dream_malformed_json_fail_closed_zero_upserts(
    db_session: AsyncSession,
) -> None:
    """fail-closed：非法 JSON → 运行 failed + 零策展落库 + 不进日记阶段。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天去公园散步了", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway(["这不是JSON{{{"])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert run.status == "failed"
        assert "不是合法 JSON" in (run.error or "")
        assert await _count_curated(db_session) == 0
        assert run.diary_episode_id is None
        assert len(gateway.calls) == 1  # 合并失败后不再调用日记
        assert run.stats.promoted == 0


async def test_run_dream_non_array_output_fails(db_session: AsyncSession) -> None:
    """fail-closed：顶层非数组（对象形态）→ 运行 failed。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天读了书", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway(['{"items": []}'])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "failed"
    assert "必须是 JSON 数组" in (run.error or "")


async def test_run_dream_empty_merge_output_fails(db_session: AsyncSession) -> None:
    """fail-closed：合并模型空输出 → 运行 failed（原因含空内容）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天写字了", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway(["   "])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "failed"
    assert "空内容" in (run.error or "")


async def test_run_dream_llm_exception_fails_run(db_session: AsyncSession) -> None:
    """fail-closed：LLM 调用异常 → 运行 failed（错误码入 error）+ 零晋升。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="今天画画了", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FailingDreamGateway([])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert run.status == "failed"
        assert "LLM_REQUEST_FAILED" in (run.error or "")
        assert await _count_curated(db_session) == 0


async def test_run_dream_unknown_merged_from_dropped_and_counted(
    db_session: AsyncSession,
) -> None:
    """提案复核：merged_from 引用未知事件 → 该条丢弃计数，其余正常晋升。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="今天学了新菜谱", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway(
            [
                _merge_response(
                    [
                        _proposal("幻觉主题", [999]),
                        _proposal("真实主题", [ep.id]),
                    ]
                ),
                "今天主人学了新菜谱。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "success"
    assert run.stats.failed == 1
    assert run.stats.promoted == 1
    decision_by_id = {decision.episode_id: decision for decision in run.decisions}
    assert decision_by_id[ep.id].action == DreamDecisionAction.PROMOTED
    assert decision_by_id[ep.id].topic == "真实主题"


async def test_run_dream_invalid_item_fields_dropped(db_session: AsyncSession) -> None:
    """提案复核：字段越界（importance 99）→ 解析级丢弃计数。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="今天浇了花", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway(
            [
                _merge_response([_proposal("越界主题", [ep.id], importance=99)]),
                "今天主人浇了花。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "success"
    assert run.stats.failed == 1
    assert run.stats.promoted == 0
    decision_by_id = {decision.episode_id: decision for decision in run.decisions}
    assert decision_by_id[ep.id].action == DreamDecisionAction.SKIPPED


# ---------------------------------------------------------------------------
# run_dream——日记阶段
# ---------------------------------------------------------------------------


async def test_run_dream_diary_failure_keeps_promotions(db_session: AsyncSession) -> None:
    """日记失败：运行 failed 但已提交晋升保留（排序决策——决策如实记录）。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="今天整理了房间", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway([_merge_response([_proposal("家务", [ep.id])]), ""])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert run.status == "failed"
        assert "日记" in (run.error or "")
        assert run.diary_episode_id is None
        assert await _count_curated(db_session) == 1  # 晋升已提交保留
        assert run.stats.promoted == 1  # 决策如实记录
        decision_by_id = {decision.episode_id: decision for decision in run.decisions}
        assert decision_by_id[ep.id].action == DreamDecisionAction.PROMOTED


async def test_run_dream_diary_disabled_skips_phase(db_session: AsyncSession) -> None:
    """日记开关关闭：只调合并、不落日记事件。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="今天听了歌", ts_ms=NOW - 1_000, importance=6)
        )
        gateway = FakeDreamGateway([_merge_response([_proposal("音乐", [ep.id])])])
        run = await dream_service.run_dream(
            db_session,
            gateway=gateway,
            persona_name="大肥鱼",
            now_ms=NOW,
            cfg=DreamJobConfig(diary_enabled=False),
        )
        assert run.status == "success"
        assert run.diary_episode_id is None
        assert len(gateway.calls) == 1
        page = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(kind=EpisodeKind.DREAM)
        )
        assert page.items == []


async def test_run_dream_empty_window_zero_calls(db_session: AsyncSession) -> None:
    """空窗口：零 LLM 调用、零统计、成功终态（无事可做即正确）。"""
    async with bind_persona_context(PERSONA_A):
        gateway = FakeDreamGateway([])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "success"
    assert gateway.calls == []
    assert run.stats.candidates == 0
    assert run.diary_episode_id is None


# ---------------------------------------------------------------------------
# run_dream——规则门（经编排链验证窗口 / 分数 / 上限 / 既往晋升）
# ---------------------------------------------------------------------------


async def test_run_dream_min_score_gate_no_merge_call(db_session: AsyncSession) -> None:
    """分数门：全员低于阈值 → 零过门、不调合并（仅日记一次调用）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="随口寒暄", importance=1, ts_ms=NOW - 1_000)
        )
        gateway = FakeDreamGateway(["今天很平静。"])
        run = await dream_service.run_dream(
            db_session,
            gateway=gateway,
            persona_name="大肥鱼",
            now_ms=NOW,
            cfg=DreamJobConfig(min_score=2.0),
        )
    assert run.status == "success"
    assert run.stats.gated == 0
    assert len(gateway.calls) == 1  # 仅日记
    assert "日记" in gateway.calls[0]["messages"][0].content
    decision = run.decisions[0]
    assert decision.action == DreamDecisionAction.SKIPPED
    assert "低于阈值" in decision.reason


async def test_run_dream_cap_truncation(db_session: AsyncSession) -> None:
    """上限截断：3 条过门取分数前 2——溢出事件 skipped（理由含上限）。"""
    async with bind_persona_context(PERSONA_A):
        eps = [
            await memory_service.append_episode(
                db_session,
                _chat_dto(user=f"事件{index}", importance=importance, ts_ms=NOW - 1_000 - index),
            )
            for index, importance in enumerate((5, 8, 3))
        ]
        top_ids = [eps[1].id, eps[0].id]
        gateway = FakeDreamGateway(
            [_merge_response([_proposal("合并主题", top_ids)]), "今天发生了不少事。"]
        )
        run = await dream_service.run_dream(
            db_session,
            gateway=gateway,
            persona_name="大肥鱼",
            now_ms=NOW,
            cfg=DreamJobConfig(max_candidates=2),
        )
    assert run.status == "success"
    assert run.stats.gated == 2
    merge_prompt = gateway.calls[0]["messages"][0].content
    assert f"事件 #{eps[2].id}" not in merge_prompt
    decision_by_id = {decision.episode_id: decision for decision in run.decisions}
    assert decision_by_id[eps[2].id].action == DreamDecisionAction.SKIPPED
    assert "上限" in decision_by_id[eps[2].id].reason


async def test_run_dream_already_promoted_from_previous_success(
    db_session: AsyncSession,
) -> None:
    """既往成功运行的晋升集：同事件二次运行跳过（不重复巩固）。"""
    async with bind_persona_context(PERSONA_A):
        ep1 = await memory_service.append_episode(
            db_session, _chat_dto(user="第一次说的事", importance=6, ts_ms=NOW - 2_000)
        )
        ep2 = await memory_service.append_episode(
            db_session, _chat_dto(user="第二次说的新事", importance=6, ts_ms=NOW - 1_000)
        )
        first = await dream_service.run_dream(
            db_session,
            gateway=FakeDreamGateway(
                [_merge_response([_proposal("首夜主题", [ep1.id])]), "第一天的日记。"]
            ),
            persona_name="大肥鱼",
            now_ms=NOW,
        )
        assert first.status == "success"
        second = await dream_service.run_dream(
            db_session,
            gateway=FakeDreamGateway(
                [_merge_response([_proposal("次夜主题", [ep2.id])]), "第二天的日记。"]
            ),
            persona_name="大肥鱼",
            now_ms=NOW,
        )
        assert second.status == "success"
        decision_by_id = {decision.episode_id: decision for decision in second.decisions}
        assert decision_by_id[ep1.id].action == DreamDecisionAction.SKIPPED
        assert "既往" in decision_by_id[ep1.id].reason
        assert decision_by_id[ep2.id].action == DreamDecisionAction.PROMOTED
        # 首夜主题不被二次推进（ep1 未再晋升）
        page = await memory_service.query_curated(db_session, CuratedQueryDTO(topic="首夜主题"))
        assert len(page.items) == 1


# ---------------------------------------------------------------------------
# run_dream——存储相关度命中（boost）与降级
# ---------------------------------------------------------------------------


async def test_run_dream_store_hits_apply_boost(db_session: AsyncSession) -> None:
    """主题相关度命中：低于阈值事件经 boost 过门晋升（检索参数透传）。"""
    async with bind_persona_context(PERSONA_NUM):
        await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="跑步", content="主人每周跑步", importance=5
            ),
        )
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="我跑步了五公里", importance=2, ts_ms=NOW - 1_000)
        )
        fake_store = FakeKeywordStore(
            {
                "跑步": [
                    MemorySearchResultDTO(
                        ref_type=MemoryRefType.EPISODE,
                        ref_id=ep.id,
                        kind="chat",
                        provenance=MemoryProvenance.OWNER,
                        ts_ms=ep.ts_ms,
                        content="我跑步了五公里",
                        score=1.0,
                    )
                ]
            }
        )
        gateway = FakeDreamGateway(
            [
                _merge_response([_proposal("运动习惯", [ep.id])]),
                "今天主人去跑步了。",
            ]
        )
        run = await dream_service.run_dream(
            db_session,
            gateway=gateway,
            persona_name="大肥鱼",
            store=fake_store,
            now_ms=NOW,
            cfg=DreamJobConfig(min_score=2.9, boost=0.5),
        )
        assert run.status == "success"
        assert run.stats.gated == 1  # 2 × decay × 1.5 ≈ 3.0 ≥ 2.9（无加成 ≈ 2.0 < 2.9）
        assert run.stats.promoted == 1
        assert len(fake_store.queries) == 1
        assert fake_store.queries[0].persona_id == PERSONA_NUM
        assert fake_store.queries[0].mode.value == "keyword"
        assert fake_store.queries[0].limit == 3


async def test_run_dream_store_none_boost_zero(db_session: AsyncSession) -> None:
    """store=None 降级：boost=0——同分数门下事件不再过门（零合并调用）。"""
    async with bind_persona_context(PERSONA_NUM):
        await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.MEMORY, topic="跑步", content="主人每周跑步", importance=5
            ),
        )
        await memory_service.append_episode(
            db_session, _chat_dto(user="我跑步了五公里", importance=2, ts_ms=NOW - 1_000)
        )
        gateway = FakeDreamGateway(["今天主人好像运动了。"])
        run = await dream_service.run_dream(
            db_session,
            gateway=gateway,
            persona_name="大肥鱼",
            store=None,
            now_ms=NOW,
            cfg=DreamJobConfig(min_score=2.9, boost=0.5),
        )
    assert run.status == "success"
    assert run.stats.gated == 0  # 2 × decay × 1.0 ≈ 2.0 < 2.9（无加成不过门）
    assert len(gateway.calls) == 1  # 仅日记


# ---------------------------------------------------------------------------
# run_dream——运行日志生命周期 / 读面 / 隔离 / 入口守卫
# ---------------------------------------------------------------------------


async def test_run_dream_lifecycle_row_states(db_session: AsyncSession) -> None:
    """运行行生命周期：落库 → 终态（success 行 stats/decisions/finished 齐备）。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="生命周期事件", importance=6, ts_ms=NOW - 1_000)
        )
        gateway = FakeDreamGateway(
            [_merge_response([_proposal("生命主题", [ep.id])]), "今天的日记。"]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert await _count_runs(db_session) == 1
        rows = list((await db_session.execute(select(DreamRunRow))).scalars().all())
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "success"
        assert row.started_ms == NOW
        assert row.finished_ms is not None
        assert row.error is None
        assert row.stats["promoted"] == 1
        assert row.decisions[0]["episode_id"] == ep.id
        assert row.diary_episode_id == run.diary_episode_id


async def test_run_dream_failed_row_states(db_session: AsyncSession) -> None:
    """失败运行行：status=failed + error 落列（终态完整可追溯）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(
            db_session, _chat_dto(user="失败路径事件", importance=6, ts_ms=NOW - 1_000)
        )
        gateway = FakeDreamGateway(["坏输出"])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        assert run.status == "failed"
        rows = list((await db_session.execute(select(DreamRunRow))).scalars().all())
        assert rows[0].status == "failed"
        assert rows[0].error is not None
        assert rows[0].finished_ms is not None


async def test_dream_runs_read_and_isolation(db_session: AsyncSession) -> None:
    """运行读面：游标分页 + 详情 + 人格隔离（他人格 404 / 列表不可见）。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="读面事件", importance=6, ts_ms=NOW - 1_000)
        )
        for _ in range(2):
            await dream_service.run_dream(
                db_session,
                gateway=FakeDreamGateway(
                    [_merge_response([_proposal("读面主题", [ep.id])]), "日记。"]
                ),
                persona_name="大肥鱼",
                now_ms=NOW,
            )
        page = await dream_service.list_runs(db_session, DreamRunQueryDTO(limit=1))
        assert len(page.items) == 1
        assert page.next_cursor is not None
        detail = await dream_service.get_run(db_session, page.items[0].id)
        assert detail.decisions[0].episode_id == ep.id
        next_page = await dream_service.list_runs(
            db_session, DreamRunQueryDTO(limit=10, cursor=page.next_cursor)
        )
        assert len(next_page.items) == 1
    async with bind_persona_context(PERSONA_B):
        with pytest.raises(BusinessException) as exc_info:
            await dream_service.get_run(db_session, page.items[0].id)
        assert exc_info.value.code == "NOT_FOUND"
        empty = await dream_service.list_runs(db_session, DreamRunQueryDTO())
        assert empty.items == []


async def test_run_dream_missing_persona_context_fails_loud(
    db_session: AsyncSession,
) -> None:
    """入口守卫：未绑定人格上下文 → PERSONA_CONTEXT_REQUIRED（fail loud）。"""
    gateway = FakeDreamGateway([])
    with pytest.raises(BusinessException) as exc_info:
        await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert exc_info.value.code == "PERSONA_CONTEXT_REQUIRED"
