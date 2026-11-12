"""harness/memory service 测试——情景 / 策展 / 会话三面（共享 SQLite）。

覆盖面：append（单条 / 批量 / 去重双通道）/ 游标分页过滤 / 管理面删除
（单条 + 批量部分成功）/ 日记全天取回 / **人格隔离**（PersonaMixin
contextvars 自动过滤——双人格互不可见）/ curated supersede 语义（单
active 不变式 + 历史链 + untrusted 拒绝）/ 会话生命周期与消息 seq
递增 / 载荷判别解析与内容指纹。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import (
    ChatSessionQueryDTO,
    CuratedQueryDTO,
    CuratedUpsertDTO,
    EpisodeCreateDTO,
    EpisodeQueryDTO,
)
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    CuratedKind,
    CuratedStatus,
    DutyReportPayloadModel,
    EpisodeKind,
    EpisodeNotePayloadModel,
    MemoryProvenance,
    compute_content_hash,
    parse_episode_payload,
)
from packages.harness.memory.orm import (
    ChatMessageRow,
    ChatSessionRow,
    CuratedMemoryRow,
    MemoryEpisodeRow,
)
from packages.harness.memory.service import ChatSessionService, MemoryService
from packages.harness.persona.service import bind_persona_context

#: 测试用双人格上下文轴（PersonaMixin 无外键——任意字符串 ID 即隔离轴）。
PERSONA_A = "mem-alpha"
PERSONA_B = "mem-beta"

memory_service = MemoryService()
session_service = ChatSessionService()


def _chat_dto(
    *,
    source: str = "chat",
    user: str = "今天吃了很多小鱼干",
    assistant: str = "好棒呀",
    importance: int = 5,
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ts_ms: int | None = None,
    kind: EpisodeKind = EpisodeKind.CHAT,
) -> EpisodeCreateDTO:
    """构造 chat 回合事件 DTO（测试铺底辅助）。"""
    return EpisodeCreateDTO(
        kind=kind,
        source=source,
        content=ChatTurnPayloadModel(user_content=user, assistant_content=assistant),
        importance=importance,
        provenance=provenance,
        ts_ms=ts_ms,
    )


async def _count(session: AsyncSession, row_type: type[object]) -> int:
    """统计表行数（跨人格管理面口径——跳过人格自动过滤的守卫面断言）。"""
    stmt = select(func.count()).select_from(row_type)
    return int(
        (
            await session.execute(stmt, execution_options={"skip_persona_filter": True})
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# 情景层——append / 去重 / 批量
# ---------------------------------------------------------------------------


async def test_append_episode_returns_record(db_session: AsyncSession) -> None:
    """单条追加：主键分配 + 字段回读 + 缺省值（owner / ts 取当前）。"""
    async with bind_persona_context(PERSONA_A):
        record = await memory_service.append_episode(db_session, _chat_dto(ts_ms=1_000))
    assert record.id >= 1
    assert record.persona_id == PERSONA_A
    assert record.kind == EpisodeKind.CHAT
    assert record.provenance == MemoryProvenance.OWNER
    assert record.content.user_content == "今天吃了很多小鱼干"
    assert record.ts_ms == 1_000
    assert record.content_hash == compute_content_hash(
        EpisodeKind.CHAT, "chat", record.content
    )


async def test_append_episode_dedup_returns_existing(db_session: AsyncSession) -> None:
    """去重开关：同内容二次 append（dedup=True）返回既有行——不新增。"""
    async with bind_persona_context(PERSONA_A):
        first = await memory_service.append_episode(db_session, _chat_dto(ts_ms=1))
        second = await memory_service.append_episode(
            db_session, _chat_dto(ts_ms=2), dedup=True
        )
        assert second.id == first.id
    assert await _count(db_session, MemoryEpisodeRow) == 1


async def test_append_only_no_update_path(db_session: AsyncSession) -> None:
    """append-only 守卫：不去重时同内容二次追加 = 两条独立行（无覆盖路径）。"""
    async with bind_persona_context(PERSONA_A):
        first = await memory_service.append_episode(db_session, _chat_dto(ts_ms=1))
        second = await memory_service.append_episode(db_session, _chat_dto(ts_ms=2))
    assert second.id != first.id
    assert await _count(db_session, MemoryEpisodeRow) == 2


async def test_append_episodes_batch(db_session: AsyncSession) -> None:
    """批量追加：同序返回 + 主键递增 + 空批量为空列表。"""
    dtos = [
        _chat_dto(source="chat", user="早上好", ts_ms=1),
        EpisodeCreateDTO(
            kind=EpisodeKind.DUTY,
            source="duty:daily",
            content=DutyReportPayloadModel(title="巡检", summary="一切正常"),
        ),
        EpisodeCreateDTO(
            kind=EpisodeKind.SYSTEM,
            source="ops",
            content=EpisodeNotePayloadModel(text="系统注记"),
        ),
    ]
    async with bind_persona_context(PERSONA_A):
        records = await memory_service.append_episodes(db_session, dtos)
    assert [r.kind for r in records] == [EpisodeKind.CHAT, EpisodeKind.DUTY, EpisodeKind.SYSTEM]
    assert records[0].id < records[1].id < records[2].id

    async with bind_persona_context(PERSONA_A):
        assert await memory_service.append_episodes(db_session, []) == []


async def test_append_episodes_batch_dedup_channels(db_session: AsyncSession) -> None:
    """批量去重双通道：批内同指纹映射首项 + 库内既有指纹命中。"""
    async with bind_persona_context(PERSONA_A):
        existing = await memory_service.append_episode(db_session, _chat_dto(ts_ms=1))
        records = await memory_service.append_episodes(
            db_session,
            [_chat_dto(ts_ms=2), _chat_dto(ts_ms=3), _chat_dto(source="chat:other", user="新话题", ts_ms=4)],
            dedup=True,
        )
    # 第 1 项命中库内既有；第 2 项批内同指纹映射首项；第 3 项插入新行
    assert records[0].id == existing.id
    assert records[1].id == existing.id
    assert records[2].id != existing.id
    assert await _count(db_session, MemoryEpisodeRow) == 2


# ---------------------------------------------------------------------------
# 情景层——查询 / 删除 / 日记
# ---------------------------------------------------------------------------


async def test_query_episodes_filters_and_cursor(db_session: AsyncSession) -> None:
    """查询过滤面：kind / provenance / 时间范围（左闭右开）+ 游标接续。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episodes(
            db_session,
            [
                _chat_dto(ts_ms=100),
                _chat_dto(source="platform:qq", user="平台消息", provenance=MemoryProvenance.UNTRUSTED, ts_ms=200),
                EpisodeCreateDTO(
                    kind=EpisodeKind.DREAM,
                    source="dream",
                    content=EpisodeNotePayloadModel(text="梦境"),
                    ts_ms=300,
                ),
            ],
        )
        chat_page = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(kind=EpisodeKind.CHAT)
        )
        assert len(chat_page.items) == 2

        trusted = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(kind=EpisodeKind.CHAT, provenance=MemoryProvenance.OWNER)
        )
        assert len(trusted.items) == 1
        assert trusted.items[0].content.user_content == "今天吃了很多小鱼干"

        window = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(start_ms=100, end_ms=300)
        )
        assert [r.ts_ms for r in window.items] == [200, 100]  # id 降序

        first = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(limit=2)
        )
        assert first.has_more is True
        assert len(first.items) == 2
        second = await memory_service.query_episodes(
            db_session, EpisodeQueryDTO(limit=2, cursor=first.next_cursor)
        )
        assert len(second.items) == 1
        assert second.has_more is False
        assert {r.id for r in first.items}.isdisjoint({r.id for r in second.items})


async def test_get_and_delete_episode(db_session: AsyncSession) -> None:
    """单条读取 / 删除：成功路径 + 缺失 NOT_FOUND。"""
    async with bind_persona_context(PERSONA_A):
        record = await memory_service.append_episode(db_session, _chat_dto())
        fetched = await memory_service.get_episode(db_session, record.id)
        assert fetched.id == record.id

        await memory_service.delete_episode(db_session, record.id)
        with pytest.raises(BusinessException) as exc_info:
            await memory_service.get_episode(db_session, record.id)
        assert exc_info.value.code == ErrorCode.NOT_FOUND
        # 删除缺失项同样 NOT_FOUND（幂等失败面）
        with pytest.raises(BusinessException):
            await memory_service.delete_episode(db_session, record.id)


async def test_batch_delete_episodes_partial_success(db_session: AsyncSession) -> None:
    """批量删除部分成功：存在项成功 + 缺失项 NOT_FOUND，互不阻塞。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episode(db_session, _chat_dto(user="保留"))
        removed = await memory_service.append_episode(db_session, _chat_dto(user="删除"))
        result = await memory_service.batch_delete_episodes(
            db_session, [removed.id, 999]
        )
    assert result.total == 2
    assert result.succeeded == 1
    assert result.failed == 1
    by_id = {item.id: item for item in result.items}
    assert by_id[removed.id].success is True
    assert by_id[999].success is False
    assert by_id[999].code == ErrorCode.NOT_FOUND
    assert await _count(db_session, MemoryEpisodeRow) == 1


async def test_fetch_day_episodes_ordered(db_session: AsyncSession) -> None:
    """日记输入面：左闭右开窗口 + (ts, id) 升序稳定排列。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.append_episodes(
            db_session,
            [
                _chat_dto(user="晚上", ts_ms=900),
                _chat_dto(user="早上", ts_ms=100),
                _chat_dto(user="同刻第二", ts_ms=100),
                _chat_dto(user="次日", ts_ms=1_500),
            ],
        )
        day = await memory_service.fetch_day_episodes(
            db_session, start_ms=100, end_ms=1_000
        )
    assert [r.content.user_content for r in day] == ["早上", "同刻第二", "晚上"]


async def test_episode_persona_isolation(db_session: AsyncSession) -> None:
    """人格隔离：A 的事件对 B 不可见（查询 / 读取 / 删除均 NOT_FOUND 等效）。"""
    async with bind_persona_context(PERSONA_A):
        record = await memory_service.append_episode(db_session, _chat_dto())
        await memory_service.append_episode(db_session, _chat_dto(user="A 的第二条"))

    async with bind_persona_context(PERSONA_B):
        page = await memory_service.query_episodes(db_session, EpisodeQueryDTO())
        assert page.items == []
        with pytest.raises(BusinessException) as exc_info:
            await memory_service.get_episode(db_session, record.id)
        assert exc_info.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(BusinessException):
            await memory_service.delete_episode(db_session, record.id)
        # B 自己追加不受 A 影响
        own = await memory_service.append_episode(db_session, _chat_dto(user="B 的事件"))
        assert own.persona_id == PERSONA_B

    async with bind_persona_context(PERSONA_A):
        page = await memory_service.query_episodes(db_session, EpisodeQueryDTO())
        assert len(page.items) == 2  # B 的删除尝试未影响 A 的行


# ---------------------------------------------------------------------------
# 策展层——supersede / 历史 / untrusted 拒绝 / 隔离
# ---------------------------------------------------------------------------


def _curated_dto(
    *,
    topic: str = "口味偏好",
    content: str = "主人喜欢小鱼干",
    provenance: MemoryProvenance = MemoryProvenance.OWNER,
    importance: int = 6,
) -> CuratedUpsertDTO:
    """构造策展 upsert DTO（测试铺底辅助）。"""
    return CuratedUpsertDTO(
        kind=CuratedKind.MEMORY,
        topic=topic,
        content=content,
        importance=importance,
        provenance=provenance,
    )


async def test_curated_upsert_supersedes(db_session: AsyncSession) -> None:
    """supersede 语义：首版 active v1 → 再 upsert 出 active v2，旧行链接——恒单 active。"""
    async with bind_persona_context(PERSONA_A):
        first = await memory_service.upsert_curated(db_session, _curated_dto())
        assert first.version == 1
        assert first.status == CuratedStatus.ACTIVE
        assert first.superseded_by_id is None

        second = await memory_service.upsert_curated(
            db_session, _curated_dto(content="主人现在更爱虾干")
        )
        assert second.version == 2
        assert second.status == CuratedStatus.ACTIVE
        assert second.superseded_by_id is None

        refetched_first = await memory_service.get_curated(db_session, first.id)
        assert refetched_first.status == CuratedStatus.SUPERSEDED
        assert refetched_first.superseded_by_id == second.id

        active_rows = list(
            (
                await db_session.execute(
                    select(CuratedMemoryRow).where(
                        CuratedMemoryRow.topic == "口味偏好",
                        CuratedMemoryRow.status == CuratedStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.id for row in active_rows] == [second.id]


async def test_curated_history_chain(db_session: AsyncSession) -> None:
    """历史链：三版本链全量版本升序；链中锚点（任意版本）取同一链。"""
    async with bind_persona_context(PERSONA_A):
        v1 = await memory_service.upsert_curated(db_session, _curated_dto(content="v1"))
        v2 = await memory_service.upsert_curated(db_session, _curated_dto(content="v2"))
        v3 = await memory_service.upsert_curated(db_session, _curated_dto(content="v3"))

        from_head = await memory_service.get_curated_history(db_session, v1.id)
        from_middle = await memory_service.get_curated_history(db_session, v2.id)
    assert [r.id for r in from_head] == [v1.id, v2.id, v3.id]
    assert [r.version for r in from_head] == [1, 2, 3]
    assert [r.content for r in from_head] == ["v1", "v2", "v3"]
    assert [r.id for r in from_middle] == [v1.id, v2.id, v3.id]


async def test_curated_untrusted_rejected(db_session: AsyncSession) -> None:
    """untrusted 拒绝：VALIDATION_ERROR 且零行落库（策展层准入不变式）。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await memory_service.upsert_curated(
                db_session, _curated_dto(provenance=MemoryProvenance.UNTRUSTED)
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert await _count(db_session, CuratedMemoryRow) == 0


async def test_curated_query_and_delete(db_session: AsyncSession) -> None:
    """查询过滤面（kind / topic / status）+ 删除后重新起链。"""
    async with bind_persona_context(PERSONA_A):
        await memory_service.upsert_curated(db_session, _curated_dto(topic="口味偏好"))
        await memory_service.upsert_curated(db_session, _curated_dto(topic="口味偏好", content="v2"))
        await memory_service.upsert_curated(
            db_session,
            CuratedUpsertDTO(
                kind=CuratedKind.RELATIONSHIP, topic="关系温度", content="逐渐亲近"
            ),
        )
        active_only = await memory_service.query_curated(
            db_session, CuratedQueryDTO(status=CuratedStatus.ACTIVE)
        )
        assert len(active_only.items) == 2

        by_topic = await memory_service.query_curated(
            db_session, CuratedQueryDTO(topic="关系温度")
        )
        assert len(by_topic.items) == 1
        assert by_topic.items[0].kind == CuratedKind.RELATIONSHIP

        by_kind = await memory_service.query_curated(
            db_session, CuratedQueryDTO(kind=CuratedKind.MEMORY)
        )
        assert len(by_kind.items) == 2

        taste_active = next(
            r for r in active_only.items if r.topic == "口味偏好"
        )
        await memory_service.delete_curated(db_session, taste_active.id)
        with pytest.raises(BusinessException):
            await memory_service.get_curated(db_session, taste_active.id)

        # 删除 active 行后重新起链：version 从 1 重新计数
        fresh = await memory_service.upsert_curated(db_session, _curated_dto(topic="口味偏好"))
        assert fresh.version == 1


async def test_curated_persona_isolation(db_session: AsyncSession) -> None:
    """策展层人格隔离：同 topic 双人格各自独立单 active 链。"""
    async with bind_persona_context(PERSONA_A):
        a1 = await memory_service.upsert_curated(db_session, _curated_dto())
        a2 = await memory_service.upsert_curated(db_session, _curated_dto(content="A-v2"))
    async with bind_persona_context(PERSONA_B):
        b1 = await memory_service.upsert_curated(db_session, _curated_dto(content="B-v1"))
        page = await memory_service.query_curated(
            db_session, CuratedQueryDTO(status=CuratedStatus.ACTIVE)
        )
        assert len(page.items) == 1
        assert page.items[0].id == b1.id
        with pytest.raises(BusinessException):
            await memory_service.get_curated(db_session, a1.id)
        with pytest.raises(BusinessException):
            await memory_service.get_curated_history(db_session, a2.id)
    assert a2.superseded_by_id is None
    assert b1.version == 1  # B 的链不受 A 影响


# ---------------------------------------------------------------------------
# 会话层——生命周期 / 消息追加 / 隔离
# ---------------------------------------------------------------------------


async def test_session_create_list_get(db_session: AsyncSession) -> None:
    """会话创建（标题缺省 / 指定）+ 列表 id 降序 + 详情含 seq 升序消息。"""
    async with bind_persona_context(PERSONA_A):
        first = await session_service.create_session(db_session)
        assert first.title == "未命名会话"
        assert first.message_count == 0
        assert first.persona_id == PERSONA_A

        second = await session_service.create_session(db_session, title="周末闲聊")
        page = await session_service.list_sessions(
            db_session, ChatSessionQueryDTO(limit=10)
        )
        assert [s.id for s in page.items] == [second.id, first.id]
        assert page.items[0].title == "周末闲聊"

        detail = await session_service.get_session(db_session, second.id)
        assert detail.title == "周末闲聊"
        assert detail.messages == []


async def test_session_append_message_seq(db_session: AsyncSession) -> None:
    """消息追加：seq 递增 + 会话行计数 / 最后活跃时间同步。"""
    async with bind_persona_context(PERSONA_A):
        summary = await session_service.create_session(db_session, title="计数会话")
        first = await session_service.append_message(
            db_session, summary.id, role=ChatMessageRole.USER, content="在吗", ts_ms=1_000
        )
        second = await session_service.append_message(
            db_session, summary.id, role=ChatMessageRole.ASSISTANT, content="在的", ts_ms=2_000
        )
        assert (first.seq, second.seq) == (1, 2)
        assert (first.role, second.role) == (ChatMessageRole.USER, ChatMessageRole.ASSISTANT)

        detail = await session_service.get_session(db_session, summary.id)
        assert detail.message_count == 2
        assert [m.seq for m in detail.messages] == [1, 2]
        assert detail.last_active_at >= second.ts_ms


async def test_session_delete_removes_messages(db_session: AsyncSession) -> None:
    """会话删除：消息同事务清理（行级验证）。"""
    async with bind_persona_context(PERSONA_A):
        summary = await session_service.create_session(db_session)
        await session_service.append_message(
            db_session, summary.id, role=ChatMessageRole.USER, content="将被删除"
        )
        await session_service.delete_session(db_session, summary.id)
    assert await _count(db_session, ChatMessageRow) == 0
    assert await _count(db_session, ChatSessionRow) == 0
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException):
            await session_service.get_session(db_session, summary.id)


async def test_session_append_missing_session(db_session: AsyncSession) -> None:
    """消息追加到不存在会话：NOT_FOUND fail loud。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await session_service.append_message(
                db_session, 999, role=ChatMessageRole.USER, content="无宿主"
            )
        assert exc_info.value.code == ErrorCode.NOT_FOUND


async def test_session_persona_isolation(db_session: AsyncSession) -> None:
    """会话层人格隔离：B 看不到 A 的会话（列表 / 详情 / 删除均隔离）。"""
    async with bind_persona_context(PERSONA_A):
        a_session = await session_service.create_session(db_session, title="A 的会话")
        await session_service.append_message(
            db_session, a_session.id, role=ChatMessageRole.USER, content="A 说"
        )
    async with bind_persona_context(PERSONA_B):
        page = await session_service.list_sessions(db_session, ChatSessionQueryDTO())
        assert page.items == []
        with pytest.raises(BusinessException):
            await session_service.get_session(db_session, a_session.id)
        with pytest.raises(BusinessException):
            await session_service.delete_session(db_session, a_session.id)
        with pytest.raises(BusinessException):
            await session_service.append_message(
                db_session, a_session.id, role=ChatMessageRole.USER, content="B 借会话"
            )
    assert await _count(db_session, ChatMessageRow) == 1  # B 的尝试零影响


async def test_session_missing_raises_not_found(db_session: AsyncSession) -> None:
    """缺失会话读取：NOT_FOUND（错误契约）。"""
    async with bind_persona_context(PERSONA_A):
        with pytest.raises(BusinessException) as exc_info:
            await session_service.get_session(db_session, 424242)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 载荷判别 / 内容指纹（models + dtos 契约）
# ---------------------------------------------------------------------------


async def test_payload_dispatch_by_kind() -> None:
    """DTO 边界载荷判别：dict 输入按 kind 解析为对应 Model Class。"""
    dto = EpisodeCreateDTO.model_validate(
        {
            "kind": "chat",
            "source": "chat",
            "content": {"user_content": "早", "assistant_content": "晚"},
        }
    )
    assert isinstance(dto.content, ChatTurnPayloadModel)
    assert dto.content.user_content == "早"
    assert dto.importance == 5
    assert dto.provenance == MemoryProvenance.OWNER

    duty = EpisodeCreateDTO.model_validate(
        {"kind": "duty", "source": "duty:daily", "content": {"title": "巡检"}}
    )
    assert isinstance(duty.content, DutyReportPayloadModel)


async def test_payload_kind_mismatch_rejected() -> None:
    """载荷类型与 kind 不匹配：校验失败（直供模型实例防错配）。"""
    with pytest.raises(ValidationError):
        EpisodeCreateDTO(
            kind=EpisodeKind.CHAT,
            source="chat",
            content=DutyReportPayloadModel(title="错配"),
        )
    with pytest.raises(ValidationError):
        EpisodeCreateDTO.model_validate(
            {"kind": "platform", "source": "p", "content": "不是对象"}
        )


async def test_parse_episode_payload_invalid_inputs() -> None:
    """判别解析错误契约：非法 kind / 非 mapping 载荷 → VALIDATION_ERROR。"""
    with pytest.raises(BusinessException) as kind_exc:
        parse_episode_payload("bogus", {"text": "x"})
    assert kind_exc.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as raw_exc:
        parse_episode_payload(EpisodeKind.SYSTEM, "not-a-dict")
    assert raw_exc.value.code == ErrorCode.VALIDATION_ERROR


async def test_content_hash_stable_and_discriminating() -> None:
    """内容指纹：同 (kind, source, content) 恒同值；任一轴变化即不同。"""
    payload = ChatTurnPayloadModel(user_content="早", assistant_content="晚")
    base = compute_content_hash(EpisodeKind.CHAT, "chat", payload)
    assert base == compute_content_hash("chat", "chat", payload.model_dump())
    assert len(base) == 16
    assert base != compute_content_hash(EpisodeKind.DUTY, "chat", payload)
    assert base != compute_content_hash(EpisodeKind.CHAT, "chat:other", payload)
    assert base != compute_content_hash(
        EpisodeKind.CHAT, "chat", ChatTurnPayloadModel(user_content="晚")
    )
