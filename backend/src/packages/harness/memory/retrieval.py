"""memory 域检索编排——索引管道 + 检索服务 + 存储装配工厂。

本模块是 P1 chunk 2 检索层的编排面（契约在 ``memory.retrieval_models``
与 ``memory.store``，实现在 ``memory.seekdb_store`` / ``memory.sqlite_store``）：

    - :class:`MemoryIndexer`      —— 索引管道（episode append → embed →
      upsert；失败逐条吞没——**索引永不阻断写入路径**）；
    - :class:`MemorySearchService` —— 检索服务（查询文本 → embed → store
      检索；dense / hybrid 模式向量化，keyword 直达）；
    - :func:`build_memory_store`  —— 存储装配工厂（``settings.seekdb.enabled``
      ? SeekDBStore : SQLiteFallbackStore——CLI / app 装配共用）；
    - :func:`episode_to_index_entry` / :func:`flatten_episode_content` ——
      情景事件记录 → 索引条目的构建辅助。

索引链顺序（单一真相，文档化于 docs/backend/memory.md 检索层）::

    append_episode(s)          写入 ff_memory_episodes（事务提交）
        ↓ commit 后（写入结果已不可逆）
        ↓ episode_to_index_entry（人格 ID 数值化 + 载荷文本化）
        ↓ dispatch：后台管理器绑定 → fire-and-forget（create_task + register，
          停机 drain）；未绑定 → 内联 await（确定性路径，测试面）
        ↓ embedder（gateway.embed_by_role，embed 角色）批量向量化
        ↓ store.upsert（同键覆盖——幂等重试安全）

嵌入职责边界：**向量只经 embedder 产出**（gateway.embed_by_role——
usage 域 purpose=embed 落账）；存储层永不自行向量化。

分发决策（文档化）：``MemoryIndexer.dispatch_*`` 在绑定了
``BackgroundTaskManager`` 时 fire-and-forget（索引链含网络调用——嵌入
端点 + SeekDB 写入，不应拖慢写入方响应；停机经管理器 drain 收口）；
未绑定时内联 await（CLI / 测试等确定性路径——索引结果可同步观测）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

from loguru import logger

from packages.harness.config.models import Settings
from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.llm_models import EmbeddingsResponseModel
from packages.harness.memory.dtos import EpisodeRecordDTO
from packages.harness.memory.models import (
    ChatTurnPayloadModel,
    DutyReportPayloadModel,
    EpisodePayload,
    PlatformActionPayloadModel,
)
from packages.harness.memory.retrieval_models import (
    MemoryIndexEntryDTO,
    MemoryRefType,
    MemorySearchMode,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
    SeekDBStoreConfig,
)
from packages.harness.memory.seekdb_store import SeekDBStore
from packages.harness.memory.sqlite_store import SQLiteFallbackStore
from packages.harness.memory.store import MemoryStore
from packages.harness.pools.manager import get_pool_manager

__all__ = [
    "MemoryEmbedder",
    "MemoryIndexer",
    "MemorySearchService",
    "build_memory_store",
    "episode_to_index_entry",
    "flatten_episode_content",
]

#: 嵌入器契约——文本序列 → 向量化响应（gateway.embed_by_role 天然符合：
#: embed 角色路由 + 池门控 + usage purpose=embed 落账）。
MemoryEmbedder = Callable[[Sequence[str]], Awaitable[EmbeddingsResponseModel]]


def flatten_episode_content(payload: EpisodePayload) -> str:
    """事件载荷 → 索引正文文本（按 kind 取文本字段，非空段换行拼接）。

    各 kind 的取段规则（文档化决策——保留可读角色前缀供检索展示）：

        - chat：``用户：{user_content}`` / ``助手：{assistant_content}``
          （非空段）；
        - duty：title / summary（非空段）；
        - platform：action / summary（非空段）；
        - dream / system：text。

    Args:
        payload: 按 kind 判别的事件载荷 Model Class。

    Returns:
        拼接后的索引正文（非空）。

    Raises:
        BusinessException: 载荷无可索引文本（全部文本段为空——
            ``VALIDATION_ERROR``；调用方吞没——索引 best-effort）。
    """
    parts: list[str]
    if isinstance(payload, ChatTurnPayloadModel):
        parts = []
        if payload.user_content:
            parts.append(f"用户：{payload.user_content}")
        if payload.assistant_content:
            parts.append(f"助手：{payload.assistant_content}")
    elif isinstance(payload, DutyReportPayloadModel):
        parts = [part for part in (payload.title, payload.summary) if part]
    elif isinstance(payload, PlatformActionPayloadModel):
        parts = [part for part in (payload.action, payload.summary) if part]
    else:
        parts = [payload.text] if payload.text else []
    text = "\n".join(parts)
    if not text:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="事件载荷无可索引文本",
            details={"payload_type": type(payload).__name__},
        )
    return text


def episode_to_index_entry(record: EpisodeRecordDTO) -> MemoryIndexEntryDTO:
    """情景事件记录 → 索引条目（人格 ID 数值化 + 载荷文本化）。

    Args:
        record: 情景事件记录 DTO（persona_id 为字符串形态——PersonaMixin
            约定；索引表面为 BigInt 数值）。

    Returns:
        索引条目 DTO（ref_type=episode）。

    Raises:
        ValueError: 人格 ID 非数值（PersonaMixin 允许任意字符串隔离轴，
            仅数值主键可入索引——调用方吞没，见 MemoryService._fire_indexing）。
    """
    return MemoryIndexEntryDTO(
        persona_id=int(record.persona_id),
        ref_type=MemoryRefType.EPISODE,
        ref_id=record.id,
        kind=record.kind.value,
        provenance=record.provenance,
        ts_ms=record.ts_ms,
        content=flatten_episode_content(record.content),
    )


class MemoryIndexer:
    """记忆索引管道（embed → upsert；失败逐条吞没——索引永不阻断写入）。

    构造注入：embedder（gateway.embed_by_role 绑定 embed 角色）+ store
    （build_memory_store 产出）+ 可选后台任务管理器（绑定即启用
    fire-and-forget 分发——见模块 docstring 分发决策）。
    """

    def __init__(
        self,
        *,
        embedder: MemoryEmbedder,
        store: MemoryStore,
        background_manager: BackgroundTaskManager | None = None,
    ) -> None:
        """初始化索引管道。

        Args:
            embedder: 嵌入器（文本批量 → 向量化响应；向量唯一来源）。
            store: 检索存储（upsert 写入面）。
            background_manager: 后台任务管理器（None = dispatch 内联
                await；绑定 = fire-and-forget + 停机 drain）。
        """
        self._embedder = embedder
        self._store = store
        self._background_manager = background_manager

    # ------------------------------------------------------------------
    # 索引工作面（awaited——结果可观测）
    # ------------------------------------------------------------------

    async def index_episode(self, entry: MemoryIndexEntryDTO) -> bool:
        """索引单条条目（嵌入 → upsert；失败吞没返回 False）。

        Args:
            entry: 索引条目。

        Returns:
            是否成功（False = 嵌入 / 写入失败——已告警吞没）。

        Raises:
            BusinessException: 嵌入响应缺少向量（``INTERNAL_ERROR``——
                内部捕获吞没，不上抛调用方）。
        """
        try:
            response = await self._embedder([entry.content])
            if not response.vectors:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="嵌入响应缺少向量",
                    details={"texts": 1, "vectors": len(response.vectors)},
                )
            await self._store.upsert([entry], [response.vectors[0]])
            return True
        except Exception:  # best-effort: 索引失败不阻断写入方（模块契约）
            logger.opt(exception=True).warning(
                "情景事件索引失败（已吞没——不影响写入结果）: ref_id={}", entry.ref_id
            )
            return False

    async def index_episodes(self, entries: Sequence[MemoryIndexEntryDTO]) -> list[bool]:
        """批量索引（**单次批量嵌入调用** → 单次批量 upsert；逐条结果）。

        批量失败降级（文档化决策）：整批嵌入 / 写入失败时逐条兜底重试
        ——隔离单条毒性输入（如单条文本触发端点校验失败），其余条目
        仍可入索引；嵌入端点整体不可用时逐条同样失败（全 False——
        仍是正确终态，日志量增大为已知代价）。

        Args:
            entries: 索引条目序列（空序列返回空列表）。

        Returns:
            逐条成功标记列表（与入参同序——部分成功形态）。

        Raises:
            BusinessException: 嵌入响应向量数量与输入不符（``INTERNAL_ERROR``
                ——内部捕获后降级逐条兜底，不上抛调用方）。
        """
        if not entries:
            return []
        try:
            response = await self._embedder([entry.content for entry in entries])
            vectors = response.vectors
            if len(vectors) != len(entries):
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="嵌入响应向量数量与输入不符",
                    details={"texts": len(entries), "vectors": len(vectors)},
                )
            await self._store.upsert(list(entries), vectors)
            return [True] * len(entries)
        except Exception:  # best-effort: 批量失败降级逐条兜底（见 docstring）
            logger.opt(exception=True).warning(
                "情景事件批量索引失败，降级逐条兜底: count={}", len(entries)
            )
            return [await self.index_episode(entry) for entry in entries]

    # ------------------------------------------------------------------
    # 写入方分发面（MemoryService 提交后调用）
    # ------------------------------------------------------------------

    async def dispatch_episode(self, entry: MemoryIndexEntryDTO) -> None:
        """单条分发（后台管理器绑定 → fire-and-forget；否则内联 await）。

        Args:
            entry: 索引条目。
        """
        if self._should_fire_and_forget():
            self._schedule(self.index_episode(entry))
            return
        await self.index_episode(entry)

    async def dispatch_episodes(self, entries: Sequence[MemoryIndexEntryDTO]) -> None:
        """批量分发（后台管理器绑定 → fire-and-forget；否则内联 await）。

        Args:
            entries: 索引条目序列（空序列直接返回）。
        """
        if not entries:
            return
        if self._should_fire_and_forget():
            self._schedule(self.index_episodes(entries))
            return
        await self.index_episodes(entries)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _should_fire_and_forget(self) -> bool:
        """判定分发形态（管理器绑定且非停机期 → fire-and-forget）。

        Returns:
            是否 fire-and-forget（停机期拒绝起新任务——降级内联路径）。
        """
        manager = self._background_manager
        return manager is not None and not manager.is_shutting_down

    def _schedule(self, coro: Coroutine[Any, Any, object]) -> None:
        """调度后台任务（create_task + register——停机 drain 收口）。

        Args:
            coro: 待调度的协程（index_episode / index_episodes——内部
                已吞没异常，任务恒不抛出）。
        """
        manager = self._background_manager
        if manager is None:  # pragma: no cover - _should_fire_and_forget 已保证非 None
            return
        task: asyncio.Task[object] = asyncio.create_task(coro)
        manager.register(task)
        logger.debug("情景事件索引已后台调度: task={}", id(task))


class MemorySearchService:
    """记忆检索服务（查询文本 → embed → store 检索）。

    无状态编排：dense / hybrid 模式先经 embedder 向量化查询文本
    （usage purpose=embed 落账），再交存储检索；keyword 模式直达存储。
    人格绑定由入口层完成（查询 DTO 的 persona_id 必填 + 上下文变量
    供 embedder 用量归属）。
    """

    def __init__(self, *, store: MemoryStore, embedder: MemoryEmbedder) -> None:
        """初始化检索服务。

        Args:
            store: 检索存储（build_memory_store 产出）。
            embedder: 嵌入器（dense / hybrid 模式的查询向量化面）。
        """
        self._store = store
        self._embedder = embedder

    async def search(self, query: MemorySearchQueryDTO) -> list[MemorySearchResultDTO]:
        """执行检索（模式分派：keyword 直达；dense / hybrid 先向量化）。

        Args:
            query: 检索查询 DTO（persona_id 必带——隔离谓词）。

        Returns:
            结果 DTO 列表（score 方向随模式——见 retrieval_models）。

        Raises:
            BusinessException: 嵌入失败（embedder 上抛——如 embed 角色未
                绑定 ``MODEL_NOT_FOUND``）；存储模式违规 / 驱动失败
                （存储层上抛）。
        """
        if query.mode == MemorySearchMode.KEYWORD:
            return await self._store.search(query, None)
        response = await self._embedder([query.query])
        if not response.vectors:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="嵌入响应缺少向量",
                details={"texts": 1, "vectors": 0},
            )
        return await self._store.search(query, response.vectors[0])


def build_memory_store(settings: Settings) -> MemoryStore:
    """按配置装配记忆检索存储（存储选择单一真源——CLI / app 共用）。

    选择规则（锁定决策）：

        - ``settings.seekdb.enabled=False``（默认）→
          :class:`SQLiteFallbackStore`（keyword-only——零配置开发可用，
          测试 hermetic）；
        - ``enabled=True`` → :class:`SeekDBStore`：优先注入
          PoolManager 共享池（已 start 且持有 seekdb 池——生命周期归
          PoolManager，app 停机经 ``pool_manager.stop()`` 收口）；否则
          自持池形态（惰性创建，``close`` 归存储——独立场景 / 冒烟）。

    Args:
        settings: 全局（或隔离）Settings。

    Returns:
        就绪的 :class:`MemoryStore` 实现（尚未建表——schema 在首次写入
        时惰性确保）。
    """
    seekdb_settings = settings.seekdb
    if not seekdb_settings.enabled:
        logger.info("记忆检索存储：SQLite 回退（keyword 模式——零向量依赖）")
        return SQLiteFallbackStore()
    config = SeekDBStoreConfig(
        host=seekdb_settings.host,
        port=seekdb_settings.port,
        user=seekdb_settings.user,
        password_env=seekdb_settings.password_env,
        database=seekdb_settings.database,
        vector_dim=seekdb_settings.vector_dim,
        pool_min=seekdb_settings.pool_min,
        pool_max=seekdb_settings.pool_max,
    )
    manager = get_pool_manager()
    if manager is not None and manager.started and manager.seekdb_pool is not None:
        logger.info("记忆检索存储：SeekDB（共享池注入——生命周期归 PoolManager）")
        return SeekDBStore(config, pool=manager.seekdb_pool)
    logger.info("记忆检索存储：SeekDB（自持池——惰性创建，close 归存储）")
    return SeekDBStore(config)
