"""harness/memory MemoryIndexer + MemoryService 索引联动测试——假 embedder / 假 store。

覆盖面：批量嵌入调用形态（单次调用携带全部文本）/ 逐条失败隔离（毒性
输入只废自身）/ 单条索引与失败吞没 / dispatch 分发（后台管理器绑定 =
fire-and-forget 注册；未绑定 = 内联 await）/ 服务集成（append_episode
绑定 indexer 触发 upsert——人格 ID 数值化 + 载荷文本化；未绑定零调用
——M1 行为保持；去重命中不触发；非数值人格 ID 吞没）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.core.errors import BusinessException
from packages.harness.llm.llm_models import EmbeddingsResponseModel
from packages.harness.memory.dtos import EpisodeCreateDTO
from packages.harness.memory.models import ChatTurnPayloadModel, MemoryProvenance
from packages.harness.memory.retrieval import (
    MemoryIndexer,
    MemorySearchService,
    flatten_episode_content,
)
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemorySearchMode,
    MemorySearchQueryDTO,
)
from packages.harness.memory.service import MemoryService
from packages.harness.persona.service import bind_persona_context


class FakeEmbedder:
    """假嵌入器（记录调用；可注入毒性文本与失败形态）。"""

    def __init__(self, *, poison: str | None = None, fail_all: bool = False) -> None:
        """初始化（默认成功——按输入数量回送向量）。"""
        self.calls: list[list[str]] = []
        self._poison = poison
        self._fail_all = fail_all

    async def __call__(self, texts: Sequence[str]) -> EmbeddingsResponseModel:
        """批量向量化（捕获调用；毒文本 / 全局失败注入）。"""
        self.calls.append(list(texts))
        if self._fail_all:
            raise RuntimeError("embed endpoint down")
        if self._poison is not None and self._poison in texts:
            raise RuntimeError(f"poison text: {self._poison}")
        return EmbeddingsResponseModel(model="test-embed", vectors=[[0.1, 0.2] for _ in texts])


class FakeStore:
    """假检索存储（记录 upsert / search 调用——零真实 IO）。"""

    def __init__(self) -> None:
        """初始化（调用清单空）。"""
        self.upserts: list[tuple[list[MemoryIndexEntryDTO], list[list[float]]]] = []
        self.searches: list[tuple[MemorySearchQueryDTO, list[float] | None]] = []

    async def ensure_schema(self) -> None:
        """建表 no-op。"""
        return

    async def upsert(self, entries: list[MemoryIndexEntryDTO], vectors: list[list[float]]) -> None:
        """记录 upsert 调用。"""
        self.upserts.append((entries, vectors))

    async def delete(self, ref_type: Any, ref_id: int, persona_id: int) -> None:
        """delete no-op（本测试面不消费）。"""
        return

    async def search(
        self, query: MemorySearchQueryDTO, query_vector: list[float] | None
    ) -> list[Any]:
        """记录 search 调用（返回空——本测试面不消费结果）。"""
        self.searches.append((query, query_vector))
        return []

    async def close(self) -> None:
        """close no-op。"""
        return


def _entry(**overrides: Any) -> MemoryIndexEntryDTO:
    """构造索引条目（默认人格 7 / episode:11；测试铺底辅助）。"""
    payload: dict[str, Any] = {
        "persona_id": 7,
        "ref_type": "episode",
        "ref_id": 11,
        "kind": "chat",
        "provenance": "owner",
        "ts_ms": 1000,
        "content": "今天吃了小鱼干",
    }
    payload.update(overrides)
    return MemoryIndexEntryDTO.model_validate(payload)


def _chat_dto(user: str = "今天吃了很多小鱼干", assistant: str = "好棒呀") -> EpisodeCreateDTO:
    """构造 chat 回合事件 DTO（测试铺底辅助）。"""
    return EpisodeCreateDTO(
        kind="chat",
        source="chat",
        content=ChatTurnPayloadModel(user_content=user, assistant_content=assistant),
    )


# ---------------------------------------------------------------------------
# 索引工作面
# ---------------------------------------------------------------------------


async def test_index_episodes_batch_embed_call_shape() -> None:
    """批量嵌入调用形态：一次调用携带全部条目文本 + 单次批量 upsert。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    results = await indexer.index_episodes([_entry(), _entry(ref_id=12), _entry(ref_id=13)])

    assert results == [True, True, True]
    assert embedder.calls == [["今天吃了小鱼干", "今天吃了小鱼干", "今天吃了小鱼干"]]
    assert len(store.upserts) == 1
    entries, vectors = store.upserts[0]
    assert [entry.ref_id for entry in entries] == [11, 12, 13]
    assert vectors == [[0.1, 0.2]] * 3


async def test_index_episodes_per_entry_failure_isolation() -> None:
    """逐条失败隔离：批内毒性输入只废自身（批量失败降级逐条兜底）。"""
    embedder = FakeEmbedder(poison="毒药文本")
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    results = await indexer.index_episodes(
        [_entry(), _entry(ref_id=12, content="毒药文本"), _entry(ref_id=13)]
    )

    assert results == [True, False, True]
    # 一次批量调用（失败）+ 三次逐条兜底
    assert len(embedder.calls) == 4
    assert embedder.calls[0] == ["今天吃了小鱼干", "毒药文本", "今天吃了小鱼干"]
    # 兜底逐条 upsert：成功两条各自入索引
    assert len(store.upserts) == 2
    assert [entry.ref_id for entry in store.upserts[0][0]] == [11]
    assert [entry.ref_id for entry in store.upserts[1][0]] == [13]


async def test_index_episodes_embedder_down_all_fail() -> None:
    """嵌入端点整体不可用：全部失败（正确终态——不抛出）。"""
    embedder = FakeEmbedder(fail_all=True)
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    results = await indexer.index_episodes([_entry(), _entry(ref_id=12)])

    assert results == [False, False]
    assert store.upserts == []


async def test_index_episode_single_and_failure() -> None:
    """单条索引：单文本嵌入 + upsert；失败吞没返回 False。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    assert await indexer.index_episode(_entry()) is True
    assert embedder.calls == [["今天吃了小鱼干"]]
    assert len(store.upserts) == 1

    failing = MemoryIndexer(embedder=FakeEmbedder(fail_all=True), store=FakeStore())
    assert await failing.index_episode(_entry()) is False


async def test_index_episodes_empty_short_circuit() -> None:
    """空批量：零嵌入调用 + 零 upsert。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    assert await indexer.index_episodes([]) == []
    assert embedder.calls == []
    assert store.upserts == []


# ---------------------------------------------------------------------------
# dispatch 分发面
# ---------------------------------------------------------------------------


async def test_dispatch_without_manager_awaits_inline() -> None:
    """未绑定后台管理器：内联 await（分发返回即完成——确定性路径）。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)

    await indexer.dispatch_episode(_entry())

    assert len(store.upserts) == 1  # 返回时已写入（未走后台任务）
    await indexer.dispatch_episodes([_entry(ref_id=12), _entry(ref_id=13)])
    assert len(store.upserts) == 2


async def test_dispatch_with_manager_fire_and_forget() -> None:
    """绑定后台管理器：fire-and-forget（任务注册 + drain 后完成写入）。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    manager = BackgroundTaskManager()
    indexer = MemoryIndexer(embedder=embedder, store=store, background_manager=manager)

    await indexer.dispatch_episode(_entry())
    assert store.upserts == []  # 分发立即返回——未内联执行

    tasks = set(manager.tasks)
    assert len(tasks) == 1  # 已注册（停机 drain 收口）
    await asyncio.gather(*tasks)
    assert len(store.upserts) == 1


async def test_dispatch_during_shutdown_falls_back_inline() -> None:
    """停机期分发：拒绝起新任务——降级内联路径（写入仍被索引）。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    manager = BackgroundTaskManager()
    manager.mark_shutting_down()
    indexer = MemoryIndexer(embedder=embedder, store=store, background_manager=manager)

    await indexer.dispatch_episode(_entry())

    assert manager.tasks == set()  # 未注册新任务
    assert len(store.upserts) == 1  # 内联完成


# ---------------------------------------------------------------------------
# 服务集成（append → commit → embed → upsert）
# ---------------------------------------------------------------------------


async def test_service_append_fires_indexing_when_bound(db_session: AsyncSession) -> None:
    """绑定 indexer：append_episode 提交后触发 embed + upsert（链路完整）。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)
    service = MemoryService(indexer=indexer)

    async with bind_persona_context("7"):
        record = await service.append_episode(db_session, _chat_dto())

    assert record.persona_id == "7"
    assert embedder.calls == [["用户：今天吃了很多小鱼干\n助手：好棒呀"]]
    assert len(store.upserts) == 1
    entries, vectors = store.upserts[0]
    entry = entries[0]
    assert entry.persona_id == 7  # 数值化（PersonaMixin 字符串 → BigInt）
    assert entry.ref_type.value == "episode"
    assert entry.ref_id == record.id
    assert entry.kind == "chat"
    assert entry.provenance == MemoryProvenance.OWNER
    assert entry.content == "用户：今天吃了很多小鱼干\n助手：好棒呀"
    assert vectors == [[0.1, 0.2]]


async def test_service_append_unbound_zero_calls(db_session: AsyncSession) -> None:
    """M1 行为保持：未绑定 indexer 的 append 零索引调用。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    service = MemoryService()  # indexer 缺省 None

    async with bind_persona_context("7"):
        await service.append_episode(db_session, _chat_dto())

    assert embedder.calls == []
    assert store.upserts == []


async def test_service_append_batch_fires_single_batch_embed(
    db_session: AsyncSession,
) -> None:
    """批量 append：一次批量嵌入（单 dispatch_episodes）+ 单批量 upsert。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)
    service = MemoryService(indexer=indexer)

    async with bind_persona_context("7"):
        records = await service.append_episodes(
            db_session, [_chat_dto(user="第一回合"), _chat_dto(user="第二回合")]
        )

    assert len(records) == 2
    assert len(embedder.calls) == 1  # 批量形态：单次调用两条文本
    assert embedder.calls[0] == [
        "用户：第一回合\n助手：好棒呀",
        "用户：第二回合\n助手：好棒呀",
    ]
    assert len(store.upserts) == 1
    assert [entry.ref_id for entry in store.upserts[0][0]] == [r.id for r in records]


async def test_service_append_dedup_hit_skips_indexing(db_session: AsyncSession) -> None:
    """去重命中：返回既有行不触发索引（既有事件假定已索引）。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)
    service = MemoryService(indexer=indexer)

    async with bind_persona_context("7"):
        first = await service.append_episode(db_session, _chat_dto(), dedup=True)
        assert len(store.upserts) == 1
        second = await service.append_episode(db_session, _chat_dto(), dedup=True)

    assert second.id == first.id
    assert len(store.upserts) == 1  # 命中路径零追加索引


async def test_service_non_numeric_persona_swallowed(db_session: AsyncSession) -> None:
    """非数值人格 ID：条目构建失败吞没——写入成功、零索引调用。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    indexer = MemoryIndexer(embedder=embedder, store=store)
    service = MemoryService(indexer=indexer)

    async with bind_persona_context("mem-alpha"):  # 任意字符串隔离轴（非数值）
        record = await service.append_episode(db_session, _chat_dto())

    assert record.id is not None  # 写入不受索引失败影响
    assert store.upserts == []


# ---------------------------------------------------------------------------
# 检索服务 + 构建辅助
# ---------------------------------------------------------------------------


async def test_search_service_mode_dispatch() -> None:
    """检索服务模式分派：keyword 直达（零嵌入）；dense 先嵌入再检索。"""
    embedder = FakeEmbedder()
    store = FakeStore()
    service = MemorySearchService(store=store, embedder=embedder)

    keyword_query = MemorySearchQueryDTO(
        persona_id=7, query="小鱼干", mode=MemorySearchMode.KEYWORD
    )
    await service.search(keyword_query)
    assert embedder.calls == []
    assert store.searches == [(keyword_query, None)]

    dense_query = MemorySearchQueryDTO(persona_id=7, query="小鱼干", mode=MemorySearchMode.DENSE)
    await service.search(dense_query)
    assert embedder.calls == [["小鱼干"]]
    assert store.searches[1] == (dense_query, [0.1, 0.2])


async def test_flatten_episode_content_rules() -> None:
    """载荷文本化规则：chat 双段前缀 / duty 双段 / platform 双段 / note 单段 / 空拒收。"""
    assert (
        flatten_episode_content(ChatTurnPayloadModel(user_content="早", assistant_content="晚"))
        == "用户：早\n助手：晚"
    )
    assert (
        flatten_episode_content(ChatTurnPayloadModel(user_content="", assistant_content="只回复"))
        == "助手：只回复"
    )
    from packages.harness.memory.models import (
        DutyReportPayloadModel,
        EpisodeNotePayloadModel,
        PlatformActionPayloadModel,
    )

    assert flatten_episode_content(DutyReportPayloadModel(title="巡检", summary="正常")) == (
        "巡检\n正常"
    )
    assert (
        flatten_episode_content(PlatformActionPayloadModel(action="msg", summary="收到"))
        == "msg\n收到"
    )
    assert flatten_episode_content(EpisodeNotePayloadModel(text="梦境注记")) == "梦境注记"
    with pytest.raises(BusinessException):
        flatten_episode_content(ChatTurnPayloadModel(user_content="", assistant_content=""))
