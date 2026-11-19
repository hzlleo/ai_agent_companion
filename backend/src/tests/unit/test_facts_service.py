"""harness/persona facts 服务测试——结构化事实 / 证据链 / 更正循环（共享 SQLite）。

覆盖面：正文归一化与身份指纹（确定性 / 空白折叠）/ 创建（首版现行行 +
证据落库）/ 同身份写入的元数据刷新（置信度最大值 + 证据合并，不推进
版本）/ provenance 与 source 配对强制 / 正文边界 / 更正（新版本 +
旧行 superseded + 证据保留追加更正说明）/ 更正非现行行拒绝 / 更正并发
抢活动标记（冲突后单次重试收敛 / 重试仍冲突 INTERNAL_ERROR）/ 驳回
（幂等 + 驳回后同身份可重新起链）/ 硬删除（引用清理）/ 列表过滤与
游标分页 / compose 注入裁剪（置信度排序 + 条数帽 + 字符帽）/ 证据
校验（未知事件丢弃不抛出）/ 人格隔离。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
import packages.harness.persona.facts_orm as facts_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.facts import (
    FactsService,
    fact_content_hash,
    normalize_fact_content,
)
from packages.harness.persona.facts_models import (
    FACT_COMPOSE_MAX_FACTS,
    FactCorrectDTO,
    FactCreateDTO,
    FactEvidenceRefModel,
    FactQueryDTO,
    PersonaFactCategory,
    PersonaFactSource,
    PersonaFactStatus,
)
from packages.harness.persona.facts_orm import FACT_ACTIVE_MARKER, PersonaFactRow
from packages.harness.persona.service import bind_persona_context

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "fact-alpha"
PERSONA_B = "fact-beta"

#: 注入时钟（epoch 毫秒——更正说明时间戳的参考轴）。
NOW = 1_760_000_000_000

memory_service = MemoryService()
facts_service = FactsService(memory_service=memory_service, clock=lambda: NOW)


async def _episode(
    db_session: AsyncSession, *, user: str = "我喜欢喝无糖乌龙茶", importance: int = 6
) -> int:
    """在 PERSONA_A 作用域内追加一条 chat 情景事件（证据铺底）。"""
    async with bind_persona_context(PERSONA_A):
        record = await memory_service.append_episode(
            db_session,
            EpisodeCreateDTO(
                kind=EpisodeKind.CHAT,
                source="chat",
                content=ChatTurnPayloadModel(user_content=user, assistant_content="记住啦"),
                importance=importance,
                provenance=MemoryProvenance.OWNER,
                ts_ms=NOW - 1_000,
            ),
        )
    return record.id


def _evidence(episode_id: int, *, quote: str = "我喜欢喝无糖乌龙茶") -> FactEvidenceRefModel:
    """构造单条证据引用（测试铺底）。"""
    return FactEvidenceRefModel(
        episode_id=episode_id,
        quote=quote,
        ts_ms=NOW - 1_000,
        provenance=MemoryProvenance.OWNER,
    )


def _create(
    content: str = "主人喜欢喝无糖乌龙茶",
    *,
    category: PersonaFactCategory = PersonaFactCategory.PREFERENCE,
    confidence: float = 0.8,
    evidence: list[FactEvidenceRefModel] | None = None,
) -> FactCreateDTO:
    """构造事实创建入参（测试铺底）。"""
    return FactCreateDTO(
        category=category,
        content=content,
        confidence=confidence,
        evidence=evidence or [],
    )


# ---------------------------------------------------------------------------
# 归一化与身份指纹
# ---------------------------------------------------------------------------


async def test_normalize_collapses_whitespace_and_nfkc() -> None:
    """归一化：NFKC 折叠全角 + 空白折叠 + 首尾裁剪（同文不同排版收敛同身份）。"""
    assert normalize_fact_content("  主人\u3000喜欢   乌龙茶  ") == "主人 喜欢 乌龙茶"
    assert normalize_fact_content("ＡＢＣ") == "ABC"


async def test_content_hash_deterministic() -> None:
    """身份指纹：同归一化正文恒同摘要；不同正文不同摘要。"""
    first = fact_content_hash(normalize_fact_content("主人喜欢乌龙茶"))
    second = fact_content_hash(normalize_fact_content(" 主人喜欢乌龙茶 "))
    other = fact_content_hash(normalize_fact_content("主人喜欢咖啡"))
    assert first == second
    assert first != other
    assert len(first) == 64


# ---------------------------------------------------------------------------
# 创建 / 元数据刷新
# ---------------------------------------------------------------------------


async def test_create_first_version_with_evidence(db_session: AsyncSession) -> None:
    """创建：首版现行行（version=1 / active 标记 / 证据落库）。"""
    episode_id = await _episode(db_session)
    async with bind_persona_context(PERSONA_A):
        result = await facts_service.upsert_fact(
            db_session, _create(evidence=[_evidence(episode_id)])
        )
    record = result.record
    assert result.created is True
    assert record.version == 1
    assert record.status == PersonaFactStatus.ACTIVE
    assert record.source == PersonaFactSource.MANUAL
    assert record.provenance == MemoryProvenance.OWNER
    assert record.evidence[0].episode_id == episode_id


async def test_upsert_same_identity_refreshes_metadata(db_session: AsyncSession) -> None:
    """同身份写入：置信度取最大值 + 证据合并，不推进版本、不产生新行。"""
    episode_id = await _episode(db_session)
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(
            db_session, _create(confidence=0.5, evidence=[_evidence(episode_id)])
        )
        second = await facts_service.upsert_fact(
            db_session,
            _create(
                content="  主人喜欢喝无糖乌龙茶 ",
                confidence=0.9,
                evidence=[_evidence(episode_id, quote="无糖乌龙茶最好喝")],
            ),
        )
        history = await _history(db_session, second.record.id)
    assert second.created is False
    assert second.record.id == first.record.id
    assert second.record.version == 1
    assert second.record.confidence == 0.9
    assert len(second.record.evidence) == 2
    assert len(history) == 1


async def test_upsert_rejects_untrusted_and_unpaired_provenance(
    db_session: AsyncSession,
) -> None:
    """provenance 规则：untrusted 拒绝；dream 必须 agent；manual 必须 owner。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as untrusted:
            await facts_service.upsert_fact(
                db_session, _create(), provenance=MemoryProvenance.UNTRUSTED
            )
        with pytest.raises(BusinessException) as unpaired:
            await facts_service.upsert_fact(
                db_session,
                _create(),
                provenance=MemoryProvenance.OWNER,
                source=PersonaFactSource.DREAM,
            )
    assert untrusted.value.code == ErrorCode.VALIDATION_ERROR
    assert unpaired.value.code == ErrorCode.VALIDATION_ERROR


async def test_upsert_rejects_blank_and_oversized_content(db_session: AsyncSession) -> None:
    """正文边界：全空白服务层拒绝（VALIDATION_ERROR）；超长在 DTO 边界拒绝。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as blank:
            await facts_service.upsert_fact(db_session, _create(content="   "))
    assert blank.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(ValidationError):
        _create(content="字" * 1001)


async def test_evidence_unknown_episode_dropped_not_raised(db_session: AsyncSession) -> None:
    """证据校验：未知事件 / 越作用域事件按丢弃处理（事实仍正常落库）。"""
    async with bind_persona_context(PERSONA_A):
        result = await facts_service.upsert_fact(db_session, _create(evidence=[_evidence(987654)]))
    assert result.record.evidence == []


async def test_evidence_untrusted_provenance_dropped(db_session: AsyncSession) -> None:
    """证据校验：untrusted 来源的证据剔除（平台内容不进证据链）。"""
    episode_id = await _episode(db_session)
    async with bind_persona_context(PERSONA_A):
        result = await facts_service.upsert_fact(
            db_session,
            _create(
                evidence=[
                    FactEvidenceRefModel(
                        episode_id=episode_id,
                        quote="平台转载内容",
                        ts_ms=NOW,
                        provenance=MemoryProvenance.UNTRUSTED,
                    )
                ]
            ),
        )
    assert result.record.evidence == []


# ---------------------------------------------------------------------------
# 更正链
# ---------------------------------------------------------------------------


async def _history(db_session: AsyncSession, fact_id: int) -> list[PersonaFactRow]:
    """取同一更正链的全部行（按 id 升序——旧版本在前；id 为链锚点断言用）。"""
    rows = list(
        (await db_session.execute(select(PersonaFactRow).order_by(PersonaFactRow.id.asc())))
        .scalars()
        .all()
    )
    assert all(row.id == fact_id or row.superseded_by_id is not None for row in rows)
    return rows


async def test_correct_creates_supersede_chain(db_session: AsyncSession) -> None:
    """更正：新版本现行行 + 旧行 superseded / 链接 + 证据保留追加更正说明。"""
    episode_id = await _episode(db_session)
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(
            db_session, _create(evidence=[_evidence(episode_id)])
        )
        corrected = await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content="主人其实更喜欢无糖绿茶")
        )
        history = await _history(db_session, corrected.id)
    assert corrected.version == 2
    assert corrected.source == PersonaFactSource.CORRECTION
    assert corrected.content == "主人其实更喜欢无糖绿茶"
    assert [row.status for row in history] == [
        PersonaFactStatus.SUPERSEDED.value,
        PersonaFactStatus.ACTIVE.value,
    ]
    old_row = history[0]
    assert old_row.id == first.record.id
    assert old_row.active_marker is None
    assert old_row.superseded_by_id == corrected.id
    quotes = [item.quote for item in corrected.evidence]
    assert "我喜欢喝无糖乌龙茶" in quotes
    assert any(quote.startswith("主人纠正：") for quote in quotes)


async def test_correct_requires_active_fact(db_session: AsyncSession) -> None:
    """更正：缺失 NOT_FOUND；历史版本 / 已驳回版本拒绝（VALIDATION_ERROR）。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content="主人喜欢淡茶")
        )
        with pytest.raises(BusinessException) as stale:
            await facts_service.correct_fact(
                db_session, first.record.id, FactCorrectDTO(content="再改一次")
            )
        with pytest.raises(BusinessException) as missing:
            await facts_service.correct_fact(db_session, 424242, FactCorrectDTO(content="不存在"))
    assert stale.value.code == ErrorCode.VALIDATION_ERROR
    assert missing.value.code == ErrorCode.NOT_FOUND


async def test_correct_converges_after_concurrent_active_conflict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更正并发抢活动标记：撞唯一键 → 单次重试并入竞争现行行（不抛 500）。

    竞态注入口径：竞争写入者在本事务提交前把同身份（同 category /
    normalized_hash）的活动行落库；钩子在该提交点回滚半成品、先提交竞争行，
    再用一条同身份的待插入行触发**真实** ``uq_ff_persona_facts_active``
    唯一键失败（SQLAlchemy ``IntegrityError`` 由 SQLite 抛出，非构造）——
    恰好复现「旧行降级后、新行置标记前」被并发写入者抢先的时序，重试路径因此面对
    一条真实可见的竞争现行行。
    """
    new_content = "主人其实更喜欢无糖绿茶"

    def _active_row() -> PersonaFactRow:
        return PersonaFactRow(
            category=PersonaFactCategory.PREFERENCE.value,
            content=new_content,
            normalized_hash=fact_content_hash(normalize_fact_content(new_content)),
            confidence=0.5,
            status=PersonaFactStatus.ACTIVE.value,
            provenance=MemoryProvenance.OWNER.value,
            source=PersonaFactSource.MANUAL.value,
            evidence=[],
            superseded_by_id=None,
            version=1,
            active_marker=FACT_ACTIVE_MARKER,
        )

    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        competitor = _active_row()
        real_commit = FactsService._commit
        attempts = 0
        conflicts: list[IntegrityError] = []

        async def racing_commit(session: AsyncSession) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await session.rollback()
                session.add(competitor)
                await real_commit(session)
                session.add(_active_row())
                try:
                    await real_commit(session)
                except IntegrityError as exc:
                    conflicts.append(exc)
                    raise
                return
            await real_commit(session)

        monkeypatch.setattr(FactsService, "_commit", staticmethod(racing_commit))
        corrected = await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content=new_content)
        )
        rows = list(
            (await db_session.execute(select(PersonaFactRow).order_by(PersonaFactRow.id.asc())))
            .scalars()
            .all()
        )
    assert attempts == 2
    assert len(conflicts) == 1
    assert "UNIQUE constraint failed" in str(conflicts[0].orig)
    assert corrected.id == competitor.id
    assert [row.status for row in rows] == [
        PersonaFactStatus.SUPERSEDED.value,
        PersonaFactStatus.ACTIVE.value,
    ]
    old_row = rows[0]
    assert old_row.id == first.record.id
    assert old_row.active_marker is None
    assert old_row.superseded_by_id == corrected.id
    assert sum(row.active_marker == FACT_ACTIVE_MARKER for row in rows) == 1
    assert any(item.quote.startswith("主人纠正：") for item in corrected.evidence)


async def test_correct_retry_conflict_raises_internal_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更正重试仍撞唯一键：INTERNAL_ERROR 上抛，事务回滚（终态无半成品行）。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        attempts = 0

        async def always_conflict(session: AsyncSession) -> None:
            nonlocal attempts
            attempts += 1
            raise IntegrityError(
                "UNIQUE constraint failed: uq_ff_persona_facts_active",
                None,
                RuntimeError("unique"),
            )

        monkeypatch.setattr(FactsService, "_commit", staticmethod(always_conflict))
        with pytest.raises(BusinessException) as exc_info:
            await facts_service.correct_fact(
                db_session, first.record.id, FactCorrectDTO(content="主人其实更喜欢无糖绿茶")
            )
        rows = list((await db_session.execute(select(PersonaFactRow))).scalars().all())
    assert exc_info.value.code == ErrorCode.INTERNAL_ERROR
    assert attempts == 2
    assert [(row.id, row.status, row.version) for row in rows] == [
        (first.record.id, PersonaFactStatus.ACTIVE.value, 1)
    ]


async def test_correct_same_content_refreshes_in_place(db_session: AsyncSession) -> None:
    """更正为归一化同文：元数据刷新（版本不推进）+ 更正说明追加。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        corrected = await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content="  主人喜欢喝无糖乌龙茶 ")
        )
    assert corrected.id == first.record.id
    assert corrected.version == 1
    assert any(item.quote.startswith("主人纠正：") for item in corrected.evidence)


# ---------------------------------------------------------------------------
# 驳回 / 删除
# ---------------------------------------------------------------------------


async def test_reject_is_idempotent_and_reopens_chain(db_session: AsyncSession) -> None:
    """驳回：active → rejected（标记清空）；重复驳回幂等；同身份可重新起链。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        rejected = await facts_service.reject_fact(db_session, first.record.id)
        again = await facts_service.reject_fact(db_session, first.record.id)
        row = (
            await db_session.execute(
                select(PersonaFactRow).where(PersonaFactRow.id == first.record.id)
            )
        ).scalar_one()
        recreated = await facts_service.upsert_fact(db_session, _create())
    assert rejected.status == PersonaFactStatus.REJECTED
    assert again.status == PersonaFactStatus.REJECTED
    assert row.active_marker is None
    assert recreated.created is True
    assert recreated.record.id != first.record.id
    assert recreated.record.version == 1


async def test_reject_history_row_rejected_with_error(db_session: AsyncSession) -> None:
    """驳回：历史版本无驳回语义（VALIDATION_ERROR）。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content="主人喜欢淡茶")
        )
        with pytest.raises(BusinessException) as exc_info:
            await facts_service.reject_fact(db_session, first.record.id)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


async def test_delete_clears_superseded_links(db_session: AsyncSession) -> None:
    """硬删除：行消失；指向已删除主键的 superseded_by_id 引用被清空。"""
    async with bind_persona_context(PERSONA_A):
        first = await facts_service.upsert_fact(db_session, _create())
        corrected = await facts_service.correct_fact(
            db_session, first.record.id, FactCorrectDTO(content="主人喜欢淡茶")
        )
        await facts_service.delete_fact(db_session, corrected.id)
        rows = list((await db_session.execute(select(PersonaFactRow))).scalars().all())
    assert [row.id for row in rows] == [first.record.id]
    assert rows[0].superseded_by_id is None


# ---------------------------------------------------------------------------
# 列表 / compose 注入面
# ---------------------------------------------------------------------------


async def test_list_filters_and_orders_by_confidence(db_session: AsyncSession) -> None:
    """列表：类别 / 状态过滤 + 置信度降序（高可信事实在前）。"""
    async with bind_persona_context(PERSONA_A):
        await facts_service.upsert_fact(db_session, _create("主人喜欢淡茶", confidence=0.4))
        await facts_service.upsert_fact(
            db_session,
            _create("主人在杭州工作", category=PersonaFactCategory.BIOGRAPHY, confidence=0.9),
        )
        page = await facts_service.list_facts(
            db_session, FactQueryDTO(category=PersonaFactCategory.BIOGRAPHY)
        )
        all_active = await facts_service.list_facts(db_session, FactQueryDTO())
    assert [item.content for item in page.items] == ["主人在杭州工作"]
    assert [item.confidence for item in all_active.items] == [0.9, 0.4]


async def test_list_cursor_pagination_no_duplicates(db_session: AsyncSession) -> None:
    """游标分页：置信度同分按 id 降序继续——跨页不重不漏。"""
    async with bind_persona_context(PERSONA_A):
        for index in range(4):
            await facts_service.upsert_fact(db_session, _create(f"事实条目{index}", confidence=0.6))
        first_page = await facts_service.list_facts(db_session, FactQueryDTO(limit=2))
        assert first_page.next_cursor is not None
        second_page = await facts_service.list_facts(
            db_session, FactQueryDTO(limit=2, cursor=first_page.next_cursor)
        )
    ids = [item.id for item in first_page.items] + [item.id for item in second_page.items]
    assert len(ids) == len(set(ids)) == 4


async def test_list_status_filter_includes_rejected(db_session: AsyncSession) -> None:
    """列表：状态过滤——rejected 面需要显式指定。"""
    async with bind_persona_context(PERSONA_A):
        record = await facts_service.upsert_fact(db_session, _create())
        await facts_service.reject_fact(db_session, record.record.id)
        active_page = await facts_service.list_facts(db_session, FactQueryDTO())
        rejected_page = await facts_service.list_facts(
            db_session, FactQueryDTO(status=PersonaFactStatus.REJECTED)
        )
    assert active_page.items == []
    assert [item.id for item in rejected_page.items] == [record.record.id]


async def test_top_facts_for_compose_caps(db_session: AsyncSession) -> None:
    """compose 注入面：只取现行事实、置信度降序、条数帽 + 字符帽。"""
    async with bind_persona_context(PERSONA_A):
        for index in range(FACT_COMPOSE_MAX_FACTS + 3):
            await facts_service.upsert_fact(
                db_session,
                _create(f"事实{index}", confidence=0.1 + index / 100),
            )
        rejected = await facts_service.upsert_fact(
            db_session, _create("被驳回的高可信事实", confidence=0.99)
        )
        await facts_service.reject_fact(db_session, rejected.record.id)
        lines = await facts_service.top_facts_for_compose(db_session)
        strict = await facts_service.top_facts_for_compose(
            db_session, limit=FACT_COMPOSE_MAX_FACTS, max_chars=40
        )
    assert len(lines) == FACT_COMPOSE_MAX_FACTS
    assert all("被驳回" not in line for line in lines)
    assert lines[0].startswith("【偏好】事实")
    assert len(strict) < len(lines)
    assert sum(len(line) + 1 for line in strict) <= 41


async def test_persona_isolation(db_session: AsyncSession) -> None:
    """人格隔离：他人格事实不可见 / 不可更正（NOT_FOUND）。"""
    async with bind_persona_context(PERSONA_A):
        created = await facts_service.upsert_fact(db_session, _create())
    async with bind_persona_context(PERSONA_B):
        page = await facts_service.list_facts(db_session, FactQueryDTO())
        with pytest.raises(BusinessException) as exc_info:
            await facts_service.correct_fact(
                db_session, created.record.id, FactCorrectDTO(content="他人格更正")
            )
    assert page.items == []
    assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_active_marker_invariant_enforced(db_session: AsyncSession) -> None:
    """单 active 不变式：现行行标记为 guard 值（唯一键身份轴的落库形态）。"""
    async with bind_persona_context(PERSONA_A):
        created = await facts_service.upsert_fact(db_session, _create())
        row = (
            await db_session.execute(
                select(PersonaFactRow).where(PersonaFactRow.id == created.record.id)
            )
        ).scalar_one()
    assert row.active_marker == FACT_ACTIVE_MARKER
    assert row.normalized_hash == fact_content_hash(normalize_fact_content("主人喜欢喝无糖乌龙茶"))


async def test_correct_happy_path_commits_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更正成功路径只提交一次：重试编排不得在无冲突时重复执行写入。"""
    commits = 0
    real_commit = FactsService._commit

    async def counting_commit(session: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        await real_commit(session)

    async with bind_persona_context(PERSONA_A):
        created = await facts_service.upsert_fact(db_session, _create())
        monkeypatch.setattr(FactsService, "_commit", staticmethod(counting_commit))
        corrected = await facts_service.correct_fact(
            db_session, created.record.id, FactCorrectDTO(content="主人其实更喜欢无糖绿茶")
        )
    assert commits == 1
    assert corrected.version == 2
