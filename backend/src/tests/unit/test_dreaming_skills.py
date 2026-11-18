"""梦境技能挖矿阶段测试——解析 / 落库 / 合并 / 逐条丢弃 / fail-closed / 阶段序。

覆盖面对齐人物事实阶段的测试口径（技能挖矿是 gate → facts → skills →
merge 链的第三阶段）：提案解析（围栏 / 逐条校验 / 结构失败）/ 新建 mined
行与统计 / 同 slug 元数据合并（置信度最大值 + 触发短语并集、版本与状态
不变）/ 逐条丢弃计数（置信度下限、未知证据编号、单次上限）/ 结构失败
零合并零日记 + 决策面补齐（reason=技能提取中断）/ prompt 阶段序与人格
中立 / 技能的人格隔离。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.dreaming_orm as dreaming_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.skills_orm as skills_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.errors import BusinessException
from packages.harness.llm.llm_models import ChatResponseModel
from packages.harness.memory.dreaming import (
    DreamService,
    parse_skills_proposals,
)
from packages.harness.memory.dreaming_models import (
    DreamDecisionAction,
    DreamSkillProposedItemModel,
)
from packages.harness.memory.dtos import CuratedQueryDTO, EpisodeCreateDTO
from packages.harness.memory.models import ChatTurnPayloadModel, EpisodeKind, MemoryProvenance
from packages.harness.memory.service import MemoryService
from packages.harness.persona.service import bind_persona_context
from packages.harness.persona.skills import SkillsService
from packages.harness.persona.skills_models import (
    SKILL_MIN_CONFIDENCE,
    SKILL_PROPOSAL_CAP,
    SkillCreateModel,
    SkillQueryDTO,
)

#: 测试人格上下文轴（PersonaMixin 隔离轴）。
PERSONA_A = "dream-skill-alpha"
PERSONA_B = "dream-skill-beta"

#: 注入时钟（epoch 毫秒——窗口 / 打分参考轴）。
NOW = 1_760_000_000_000

#: 事实阶段的空提案响应（铺底用——本套件聚焦技能阶段）。
_FACTS_EMPTY = "[]"

memory_service = MemoryService()
dream_service = DreamService()
skills_service = SkillsService()


class FakeDreamGateway:
    """假 LLM 网关（chat_by_role 调用捕获 + 排队响应——零真实网络）。"""

    def __init__(self, responses: list[str]) -> None:
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


def _chat_dto(*, user: str, importance: int = 6, ts_ms: int | None = None) -> EpisodeCreateDTO:
    """构造 chat 回合事件 DTO（测试铺底辅助）。"""
    return EpisodeCreateDTO(
        kind=EpisodeKind.CHAT,
        source="chat",
        content=ChatTurnPayloadModel(user_content=user, assistant_content="好的"),
        importance=importance,
        provenance=MemoryProvenance.OWNER,
        ts_ms=ts_ms if ts_ms is not None else NOW - 1_000,
    )


def _skill_proposal(
    name: str,
    episode_ids: list[int],
    *,
    description: str = "主人说「写周报」时使用",
    triggers: list[str] | None = None,
    content: str = "1. 先列三件事\n2. 再写进展",
    confidence: float = 0.7,
) -> dict[str, Any]:
    """构造单条技能提案（JSON 数组元素——测试铺底辅助）。"""
    return {
        "name": name,
        "description": description,
        "triggers": triggers or ["写周报"],
        "content": content,
        "confidence": confidence,
        "evidence_episode_ids": episode_ids,
    }


def _skills_response(items: list[dict[str, Any]], *, fenced: bool = True) -> str:
    """构造技能响应文本（可选代码围栏包裹——robust parse 面）。"""
    raw = json.dumps(items, ensure_ascii=False)
    return f"```json\n{raw}\n```" if fenced else raw


def _merge_response(proposal_refs: list[int]) -> str:
    """构造合并响应（fenced JSON 数组——引用给定事件 id）。"""
    proposals = [
        {
            "topic": "口味偏好",
            "kind": "memory",
            "content": "主人爱小鱼干",
            "importance": 6,
            "merged_from": proposal_refs,
        }
    ]
    return f"```json\n{json.dumps(proposals, ensure_ascii=False)}\n```"


# ---------------------------------------------------------------------------
# 提案解析（robust parse）
# ---------------------------------------------------------------------------


async def test_parse_skills_proposals_valid_and_dropped() -> None:
    """解析：围栏剥离后合法条目通过；字段越界 / 空证据条目丢弃计数。"""
    raw = _skills_response(
        [
            _skill_proposal("周报流程", [1]),
            _skill_proposal("越界正文", [2], content="长" * 4001),
            _skill_proposal("空证据", []),
        ]
    )
    valid, dropped = parse_skills_proposals(raw)
    assert dropped == 2
    assert [item.name for item in valid] == ["周报流程"]
    assert isinstance(valid[0], DreamSkillProposedItemModel)


async def test_parse_skills_proposals_structural_failures() -> None:
    """结构失败：非法 JSON / 顶层非数组 → VALIDATION_ERROR（运行标记 failed）。"""
    with pytest.raises(BusinessException) as invalid:
        parse_skills_proposals("这不是JSON{{{")
    assert "不是合法 JSON" in invalid.value.message
    with pytest.raises(BusinessException) as non_array:
        parse_skills_proposals('{"items": []}')
    assert "必须是 JSON 数组" in non_array.value.message


# ---------------------------------------------------------------------------
# 挖矿落库 / 合并 / 丢弃计数
# ---------------------------------------------------------------------------


async def test_run_dream_mines_skill_with_stats_and_phase_order(db_session: AsyncSession) -> None:
    """happy path：mined 行落库 + 统计 + 阶段序（事实 → 技能 → 合并 → 日记）。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="以后写周报都先列三件事")
        )
        gateway = FakeDreamGateway(
            [
                _FACTS_EMPTY,
                _skills_response([_skill_proposal("周报流程", [ep.id], confidence=0.8)]),
                _merge_response([ep.id]),
                "今天的日记。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        page = await skills_service.list_skills(db_session, SkillQueryDTO())
        curated = await memory_service.query_curated(db_session, CuratedQueryDTO(topic="口味偏好"))
    assert run.status == "success"
    assert run.stats.skills_created == 1
    assert run.stats.skills_merged == 0
    assert run.stats.skills_dropped == 0
    assert run.stats.promoted == 1
    assert len(page.items) == 1
    mined = page.items[0]
    assert mined.source == "mined"
    assert mined.status == "active"
    assert mined.version == 1
    assert mined.triggers == ["写周报"]
    assert curated.items[0].provenance == MemoryProvenance.AGENT
    # 阶段序：事实 → 技能（既有技能清单 + 候选事件）→ 合并（人格中立）。
    assert gateway.calls[0]["messages"][0].content.startswith("【人物事实提取任务】")
    skills_prompt = gateway.calls[1]["messages"][0].content
    assert skills_prompt.startswith("【技能挖矿任务】")
    assert f"事件 #{ep.id}" in skills_prompt
    assert "大肥鱼" not in skills_prompt
    assert gateway.calls[2]["messages"][0].content.startswith("【记忆整理任务】")
    for index in (0, 1, 2):
        assert gateway.calls[index]["purpose"] == "dream"
        assert gateway.calls[index]["persona_id"] == PERSONA_A


async def test_run_dream_merges_existing_skill_metadata(db_session: AsyncSession) -> None:
    """同 slug 合并：置信度最大值 + 触发短语并集；版本 / 状态 / 正文不变。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="以后写周报都先列三件事")
        )
        created = await skills_service.create(
            db_session,
            SkillCreateModel(
                name="周报流程",
                description="主人说「写周报」时使用",
                triggers=["写周报"],
                content="1. 主人自己的步骤",
                confidence=0.5,
            ),
        )
        gateway = FakeDreamGateway(
            [
                _FACTS_EMPTY,
                _skills_response(
                    [
                        _skill_proposal(
                            "周报流程",
                            [ep.id],
                            triggers=["写周报", "本周总结"],
                            content="模型写的新正文（不覆盖）",
                            confidence=0.9,
                        )
                    ]
                ),
                _merge_response([ep.id]),
                "今天的日记。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        page = await skills_service.list_skills(db_session, SkillQueryDTO())
    assert run.status == "success"
    assert run.stats.skills_merged == 1
    assert run.stats.skills_created == 0
    refreshed = page.items[0]
    assert refreshed.id == created.id
    assert refreshed.confidence == 0.9
    assert refreshed.triggers == ["写周报", "本周总结"]
    assert refreshed.version == 1
    assert refreshed.content == "1. 主人自己的步骤"
    # 技能阶段 prompt 列出既有技能名称 / slug（模型据此沿用同名合并）。
    skills_prompt = gateway.calls[1]["messages"][0].content
    assert "- 周报流程（slug: 周报流程）" in skills_prompt


async def test_run_dream_drops_invalid_skill_proposals(db_session: AsyncSession) -> None:
    """逐条丢弃：置信度低于下限 + 未知证据编号 + 超单次上限 → skills_dropped。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="以后写周报都先列三件事")
        )
        items = [
            _skill_proposal("低置信技能", [ep.id], confidence=SKILL_MIN_CONFIDENCE - 0.01),
            _skill_proposal("未知证据技能", [999]),
        ]
        items.extend(
            _skill_proposal(f"合格技能{index}", [ep.id], triggers=[f"触发{index}"])
            for index in range(SKILL_PROPOSAL_CAP + 1)
        )
        gateway = FakeDreamGateway(
            [
                _FACTS_EMPTY,
                _skills_response(items),
                _merge_response([ep.id]),
                "今天的日记。",
            ]
        )
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        page = await skills_service.list_skills(db_session, SkillQueryDTO(limit=50))
    assert run.status == "success"
    assert run.stats.skills_created == SKILL_PROPOSAL_CAP
    assert run.stats.skills_dropped == 3
    assert len(page.items) == SKILL_PROPOSAL_CAP


# ---------------------------------------------------------------------------
# fail-closed（结构失败 / 空输出）
# ---------------------------------------------------------------------------


async def test_run_dream_skills_invalid_json_fails_before_merge(
    db_session: AsyncSession,
) -> None:
    """技能阶段结构失败：运行 failed + 零合并 / 零日记 + 决策面补齐。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="今天去了公园")
        )
        gateway = FakeDreamGateway([_FACTS_EMPTY, "这不是JSON{{{"])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
        curated = await memory_service.query_curated(
            db_session, CuratedQueryDTO(topic="口味偏好")
        )
    assert run.status == "failed"
    assert "不是合法 JSON" in (run.error or "")
    assert len(gateway.calls) == 2  # 技能阶段失败后不再调用合并 / 日记
    assert curated.items == []
    decision = {decision.episode_id: decision for decision in run.decisions}[ep.id]
    assert decision.action == DreamDecisionAction.SKIPPED
    assert "技能提取中断" in decision.reason


async def test_run_dream_skills_empty_output_fails(db_session: AsyncSession) -> None:
    """技能阶段空输出：运行 failed（原因含空内容——fail-closed 同轴）。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(db_session, _chat_dto(user="今天写字了"))
        gateway = FakeDreamGateway([_FACTS_EMPTY, "   "])
        run = await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    assert run.status == "failed"
    assert "空内容" in (run.error or "")


# ---------------------------------------------------------------------------
# 人格隔离
# ---------------------------------------------------------------------------


async def test_mined_skill_persona_isolation(db_session: AsyncSession) -> None:
    """挖矿落库在运行人格作用域内：他人格不可见（隔离语义）。"""
    async with bind_persona_context(PERSONA_A):
        ep = await memory_service.append_episode(
            db_session, _chat_dto(user="以后写周报都先列三件事")
        )
        gateway = FakeDreamGateway(
            [
                _FACTS_EMPTY,
                _skills_response([_skill_proposal("周报流程", [ep.id])]),
                _merge_response([ep.id]),
                "日记。",
            ]
        )
        await dream_service.run_dream(
            db_session, gateway=gateway, persona_name="大肥鱼", now_ms=NOW
        )
    async with bind_persona_context(PERSONA_B):
        page = await skills_service.list_skills(db_session, SkillQueryDTO())
    assert page.items == []
