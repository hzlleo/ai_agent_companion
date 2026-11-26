"""memory 域检索编排——索引管道 + 检索服务 + 存储装配工厂。

本模块是检索层的编排面（契约在 ``memory.retrieval_models``
与 ``memory.store``，实现在 ``memory.seekdb_store`` / ``memory.sqlite_store``）：

    - :class:`MemoryIndexer`      —— 索引管道（episode / curated 条目 →
      存储能力面判定 → embed → upsert；失败逐条吞没——**索引永不阻断
      写入路径**；另含按引用删除面 ``dispatch_delete``——管理面删除 /
      归档的生命周期收口）；
    - :class:`MemorySearchService` —— 检索服务（查询文本 → embed → store
      检索；命中策展行时经后台管理器异步写读强化（见下））；
    - :class:`CuratedAccessReinforcerFace` —— 写读强化面协议（检索服务
      只依赖协议——具体实现注入，缺省 None = 零强化）；
    - :func:`build_memory_store`  —— 存储装配工厂（``settings.seekdb.enabled``
      ? SeekDBStore : SQLiteFallbackStore——CLI / app 装配共用）；
    - :func:`episode_to_index_entry` / :func:`curated_to_index_entry` /
      :func:`flatten_episode_content` —— 业务行 → 索引条目的构建辅助。

索引链顺序（单一真相，文档化于 docs/backend/memory.md 检索层）::

    append_episode(s)          写入 ff_memory_episodes（事务提交）
        ↓ commit 后（写入结果已不可逆）
        ↓ episode_to_index_entry（人格 ID 数值化 + 载荷文本化）
        ↓ dispatch：后台管理器绑定 → fire-and-forget（create_task + register，
          停机 drain）；未绑定 → 内联 await（确定性路径，测试面）
        ↓ 存储能力面判定（``MemoryStore.supports_vectors``——False 时
          跳过嵌入，以 None 向量写入 keyword-only 回退存储）
        ↓ embedder（gateway.embed_by_role，embed 角色）批量向量化
        ↓ store.upsert（同键覆盖——幂等重试安全）

策展索引链（supersede 提交后的索引同步——同一次分发内完成新旧引用轮转）::

    upsert_curated（新 active 行已提交；旧行已被取代 / 首版无旧行）
        ↓ curated_to_index_entry（正文 = ``topic：content`` 紧凑检索文本）
        ↓ dispatch_curated：后台管理器绑定 → fire-and-forget；未绑定 → 内联
        ↓ 存储能力面判定（同上；False 时不产生 embed 用量）
        ↓ store.upsert（新行索引条目——ref_type=curated / ref_id=新行 id）
        ↓ store.delete（旧 superseded 行的索引条目——ref_id 取自已提交的
          supersede 事务；首版路径无此步）

索引删除链（生命周期收口——删除 / 归档后的 orphan-index 边界）::

    delete_episode / delete_curated / archive_curated
        业务行已删除 / 已归档（事务提交）
        ↓ dispatch_delete：后台管理器绑定 → fire-and-forget；未绑定 → 内联
        ↓ store.delete（按 persona + ref_type + ref_id 唯一键删除——幂等）

写读强化（**write-on-read caveat——文档化**）：策展层检索命中会推进
``ff_curated_memory.access_count`` / ``last_accessed_ms``（使用强化轴，
生命周期有效强度的输入）。该副作用是**读路径上的写**——语义与代价
必须显式声明：

    - 仅当检索服务注入了 ``reinforcer`` **且**绑定了后台任务管理器时
      才发生（缺省 / CLI / 测试面零写）；
    - 调度即返回：检索响应不等写入完成（fire-and-forget + 停机 drain
      收口），写失败仅告警——检索结果不受影响；
    - 计数是**单调近似**（并发检索的 +1 可能交错，非事务严格串行）；
      强度公式对计数取对数，微小交错对排序无实质影响；
    - 命中 episode 行**不**强化（情景层 append-only，无访问计数列）。

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
from typing import Any, Protocol

from loguru import logger

from packages.harness.config.models import Settings
from packages.harness.core.background_tasks import BackgroundTaskManager
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms as current_ms
from packages.harness.llm.llm_models import EmbeddingsResponseModel
from packages.harness.memory.dtos import CuratedRecordDTO, EpisodeRecordDTO
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
from packages.harness.pools.seekdb_compat import resolve_seekdb_config

__all__ = [
    "MemoryEmbedder",
    "MemoryIndexer",
    "MemorySearchService",
    "CuratedAccessReinforcerFace",
    "build_memory_store",
    "curated_to_index_entry",
    "episode_to_index_entry",
    "flatten_episode_content",
]


class CuratedAccessReinforcerFace(Protocol):
    """写读强化面协议（检索命中策展行后的访问计数推进——实现注入）。

    实现契约：恒不抛异常（失败仅日志——强化是增强面，不是正确性轴）；
    单次调用为批量（同一次检索的全部命中策展 id 一次推进）。
    """

    async def bump(
        self, persona_id: int, memory_ids: Sequence[int], accessed_ms: int
    ) -> None:
        """推进访问计数（access_count + 1 / last_accessed_ms）与落库。

        Args:
            persona_id: 人格主键（人格作用域绑定面）。
            memory_ids: 本次命中的策展记忆主键集合（去重后批量）。
            accessed_ms: 命中时刻（epoch 毫秒——注入时钟）。
        """
        ...

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


def curated_to_index_entry(record: CuratedRecordDTO, *, ts_ms: int) -> MemoryIndexEntryDTO:
    """策展记忆行 → 索引条目（检索正文 = ``主题：内容`` 的紧凑拼接形态）。

    正文格式（锁定决策）：``f"{record.topic}：{record.content}"``，以全角
    冒号分隔主题键与正文——主题键是管理面检索的强语义锚点，与正文一并
    进入全文 / 向量匹配面；该格式是索引层的可检索文本，不改写业务行
    内容（append-only：索引只是派生视图）。

    Args:
        record: 策展记忆记录 DTO（persona_id 为字符串形态——PersonaMixin
            约定；索引表面为 BigInt 数值）。
        ts_ms: 索引条目事件时间（epoch 毫秒——supersede 同步时刻，由调用
            方注入时钟提供）。

    Returns:
        索引条目 DTO（ref_type=curated）。

    Raises:
        ValueError: 人格 ID 非数值（PersonaMixin 允许任意字符串隔离轴，
            仅数值主键可入索引——调用方吞没并告警，与索引写入侧同规则）。
    """
    return MemoryIndexEntryDTO(
        persona_id=int(record.persona_id),
        ref_type=MemoryRefType.CURATED,
        ref_id=record.id,
        kind=record.kind.value,
        provenance=record.provenance,
        ts_ms=ts_ms,
        content=f"{record.topic}：{record.content}",
    )


class MemoryIndexer:
    """记忆索引管道（能力面判定 → embed → upsert；失败逐条吞没）。

    构造注入：embedder（gateway.embed_by_role 绑定 embed 角色）+ store
    （build_memory_store 产出）+ 可选后台任务管理器（绑定即启用
    fire-and-forget 分发——见模块 docstring 分发决策）。

    存储能力面（``MemoryStore.supports_vectors``）是嵌入的唯一门：False
    （keyword-only 回退存储）时索引管道**不调用嵌入器**，以 None 向量
    写入标量 / 正文列——消除每回合两次真实 embed 外呼的无用用量。
    索引面覆盖情景条目（``index_episode(s)``）与策展条目
    （``index_curated``——supersede 的新旧引用轮转）。
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
        """索引单条条目（能力面判定 → 嵌入 → upsert；失败吞没返回 False）。

        存储能力面 ``supports_vectors=False``（keyword-only 回退）时**跳过
        嵌入器调用**，以 None 向量写入（零 embed 用量——向量本就会被回退
        存储丢弃）。

        Args:
            entry: 索引条目。

        Returns:
            是否成功（False = 嵌入 / 写入失败——已告警吞没）。

        Raises:
            BusinessException: 嵌入响应向量数量与输入不符（``INTERNAL_ERROR``
                ——内部捕获吞没，不上抛调用方）。
        """
        try:
            vectors = await self._resolve_vectors([entry])
            await self._store.upsert([entry], vectors)
            return True
        except Exception:  # best-effort: 索引失败不阻断写入方（模块契约）
            logger.opt(exception=True).warning(
                "情景事件索引失败（已吞没——不影响写入结果）: ref_id={}", entry.ref_id
            )
            return False

    async def index_episodes(self, entries: Sequence[MemoryIndexEntryDTO]) -> list[bool]:
        """批量索引（**单次批量嵌入调用** → 单次批量 upsert；逐条结果）。

        存储能力面 ``supports_vectors=False`` 时跳过嵌入（单次 None 向量
        upsert）；支持向量时单次批量嵌入调用。

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
            vectors = await self._resolve_vectors(entries)
            await self._store.upsert(list(entries), vectors)
            return [True] * len(entries)
        except Exception:  # best-effort: 批量失败降级逐条兜底（见 docstring）
            logger.opt(exception=True).warning(
                "情景事件批量索引失败，降级逐条兜底: count={}", len(entries)
            )
            return [await self.index_episode(entry) for entry in entries]

    async def index_curated(
        self, entry: MemoryIndexEntryDTO, *, superseded_ref_id: int | None
    ) -> bool:
        """策展行索引同步（新 active 条目 upsert → 旧 superseded 条目删除）。

        调用方（``MemoryService.upsert_curated``）只在**新行已提交**后调用
        ——本方法负责索引侧的引用键轮转：先写新行索引条目（检索面立即
        可见新版本），成功后删除旧 superseded 行的索引条目（旧版本不再
        被检索命中）。首版路径 ``superseded_ref_id=None``（无删除步）。

        失败隔离（文档化决策）：新条目 upsert 失败时**不删除旧条目**——
        避免旧版本索引被移除后检索面完全丢失该主题；删除失败（新条目
        已写入）仅告警。两种失败都吞没（索引永不阻断写入路径）。

        Args:
            entry: 新 active 行的索引条目（ref_type=curated）。
            superseded_ref_id: 被取代旧行的主键（None = 首版无旧行）。

        Returns:
            是否成功（False = 嵌入 / 写入 / 删除失败——已告警吞没）。
        """
        try:
            vectors = await self._resolve_vectors([entry])
            await self._store.upsert([entry], vectors)
        except Exception:  # best-effort: 新条目写失败保留旧条目（见 docstring）
            logger.opt(exception=True).warning(
                "策展记忆索引写入失败（已吞没——保留旧索引条目）: ref_id={}", entry.ref_id
            )
            return False
        if superseded_ref_id is not None:
            try:
                await self._store.delete(
                    MemoryRefType.CURATED, superseded_ref_id, entry.persona_id
                )
            except Exception:  # best-effort: 旧条目删除失败仅告警（新条目已可见）
                logger.opt(exception=True).warning(
                    "旧策展记忆索引删除失败（已吞没——新索引条目已写入）: ref_id={}",
                    superseded_ref_id,
                )
        return True

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

    async def dispatch_curated(
        self, entry: MemoryIndexEntryDTO, *, superseded_ref_id: int | None
    ) -> None:
        """策展索引同步分发（后台管理器绑定 → fire-and-forget；否则内联）。

        新旧引用轮转在**同一次分发**内完成（单个后台任务 / 单次内联
        await）——不拆成两个独立分发，避免新行索引与旧行删除被抢占执行
        顺序。

        Args:
            entry: 新 active 行的索引条目。
            superseded_ref_id: 被取代旧行的主键（None = 首版无旧行）。
        """
        if self._should_fire_and_forget():
            self._schedule(
                self.index_curated(entry, superseded_ref_id=superseded_ref_id)
            )
            return
        await self.index_curated(entry, superseded_ref_id=superseded_ref_id)

    # ------------------------------------------------------------------
    # 索引删除面（删除 / 归档的生命周期收口）
    # ------------------------------------------------------------------

    async def delete_index_entry(
        self, ref_type: MemoryRefType, ref_id: int, persona_id: int
    ) -> bool:
        """按引用删除单条索引行（失败吞没返回 False——删除永不阻断业务）。

        幂等性：索引行不存在时存储层 DELETE 影响 0 行（正常终态——本方法
        仍返回 True，与「删除已达成目标」语义一致）。

        Args:
            ref_type: 引用类型（episode / curated）。
            ref_id: 引用行主键。
            persona_id: 人格主键（隔离谓词——数值形态）。

        Returns:
            是否成功（False = 存储层失败——已告警吞没）。
        """
        try:
            await self._store.delete(ref_type, ref_id, persona_id)
            return True
        except Exception:  # best-effort: 索引删除失败不阻断业务方（模块契约）
            logger.opt(exception=True).warning(
                "记忆索引删除失败（已吞没——不影响业务结果）: ref_type={}, ref_id={}",
                ref_type.value,
                ref_id,
            )
            return False

    async def dispatch_delete(
        self, ref_type: MemoryRefType, ref_id: int, persona_id: int
    ) -> None:
        """索引删除分发（后台管理器绑定 → fire-and-forget；否则内联 await）。

        Args:
            ref_type: 引用类型（episode / curated）。
            ref_id: 引用行主键。
            persona_id: 人格主键（隔离谓词——数值形态）。
        """
        if self._should_fire_and_forget():
            self._schedule(self.delete_index_entry(ref_type, ref_id, persona_id))
            return
        await self.delete_index_entry(ref_type, ref_id, persona_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _resolve_vectors(
        self, entries: Sequence[MemoryIndexEntryDTO]
    ) -> list[list[float]] | None:
        """按存储能力面产出向量集合（能力面 False 时短路嵌入）。

        能力面判定**先于**嵌入器调用：keyword-only 回退存储不持久化向量，
        注入的嵌入器每批调用都会产生真实用量（billing / usage 落账）却
        被丢弃——该短路是无用外呼的消除点。

        Args:
            entries: 待索引条目（非空——调用方已短路空集合）。

        Returns:
            与条目同序的向量列表；存储不支持向量时为 None。

        Raises:
            BusinessException: 嵌入响应向量数量与输入不符（``INTERNAL_ERROR``
                ——调用方捕获吞没 / 降级）。
        """
        if not self._store.supports_vectors:
            return None
        response = await self._embedder([entry.content for entry in entries])
        vectors = response.vectors
        if len(vectors) != len(entries):
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="嵌入响应向量数量与输入不符",
                details={"texts": len(entries), "vectors": len(vectors)},
            )
        return vectors

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
            coro: 待调度的协程（index_episode / index_episodes /
                index_curated——内部已吞没异常，任务恒不抛出）。
        """
        manager = self._background_manager
        if manager is None:  # pragma: no cover - _should_fire_and_forget 已保证非 None
            return
        task: asyncio.Task[object] = asyncio.create_task(coro)
        manager.register(task)
        logger.debug("记忆索引已后台调度: task={}", id(task))


class MemorySearchService:
    """记忆检索服务（查询文本 → embed → store 检索 + 策展命中写读强化）。

    无状态编排：dense / hybrid 模式先经 embedder 向量化查询文本
    （usage purpose=embed 落账），再交存储检索；keyword 模式直达存储。
    人格绑定由入口层完成（查询 DTO 的 persona_id 必填 + 上下文变量
    供 embedder 用量归属）。

    写读强化（可选注入，缺省关闭）：``reinforcer`` + ``background_manager``
    双双就绪时，命中结果中的策展行（``ref_type=curated``）经后台任务
    异步推进访问计数（见模块 docstring 写读强化一节——写读副作用 /
    单调近似 / 失败仅告警）。
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedder: MemoryEmbedder,
        reinforcer: CuratedAccessReinforcerFace | None = None,
        background_manager: BackgroundTaskManager | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """初始化检索服务。

        Args:
            store: 检索存储（build_memory_store 产出）。
            embedder: 嵌入器（dense / hybrid 模式的查询向量化面）。
            reinforcer: 写读强化面（None = 零强化——命中不写访问计数）。
            background_manager: 后台任务管理器（None = 零强化——即使
                注入了 reinforcer 也不调度，避免阻塞检索响应）。
            clock: 注入时钟（epoch 毫秒；None = 当前真实时间——访问
                时刻落 ``last_accessed_ms``，测试可控）。
        """
        self._store = store
        self._embedder = embedder
        self._reinforcer = reinforcer
        self._background_manager = background_manager
        self._clock = clock if clock is not None else current_ms

    async def search(self, query: MemorySearchQueryDTO) -> list[MemorySearchResultDTO]:
        """执行检索（模式分派：keyword 直达；dense / hybrid 先向量化）。

        检索命中策展行时调度写读强化（fire-and-forget——不进入响应
        路径；未注入强化面 / 后台管理器时零副作用）。

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
            results = await self._store.search(query, None)
        else:
            response = await self._embedder([query.query])
            if not response.vectors:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="嵌入响应缺少向量",
                    details={"texts": 1, "vectors": 0},
                )
            results = await self._store.search(query, response.vectors[0])
        self._schedule_reinforcement(query.persona_id, results)
        return results

    def _schedule_reinforcement(
        self, persona_id: int, results: Sequence[MemorySearchResultDTO]
    ) -> None:
        """调度写读强化（命中策展行批次 + 后台管理器——否则零副作用）。

        触发条件（任一不满足即跳过）：

            - ``reinforcer`` 已注入；
            - ``background_manager`` 已绑定且非停机期（fire-and-forget
              形态的唯一载体——本方法不内联等待，检索响应零阻塞）；
            - 命中集中存在 ``ref_type=curated`` 的行（episode 行不强化）。

        Args:
            persona_id: 目标人格主键（强化写入的人格作用域）。
            results: 本次检索命中（按结果序去重策展 id）。
        """
        if self._reinforcer is None or self._background_manager is None:
            return
        if self._background_manager.is_shutting_down:
            return
        memory_ids = sorted(
            {
                result.ref_id
                for result in results
                if result.ref_type == MemoryRefType.CURATED
            }
        )
        if not memory_ids:
            return
        task: asyncio.Task[None] = asyncio.create_task(
            self._bump_quietly(persona_id, memory_ids)
        )
        self._background_manager.register(task)
        logger.debug("策展记忆访问强化已后台调度: count={}", len(memory_ids))

    async def _bump_quietly(self, persona_id: int, memory_ids: Sequence[int]) -> None:
        """执行强化并吞没异常（后台任务载荷——恒不抛出）。

        Args:
            persona_id: 目标人格主键。
            memory_ids: 命中策展记忆主键集合。
        """
        if self._reinforcer is None:  # pragma: no cover - 调度侧已保证非 None
            return
        try:
            await self._reinforcer.bump(persona_id, memory_ids, self._clock())
        except Exception:  # best-effort: 强化失败不影响检索结果（写读副作用面）
            logger.opt(exception=True).warning(
                "策展记忆访问强化失败（已吞没——不影响检索结果）: count={}",
                len(memory_ids),
            )


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

    Raises:
        BusinessException: 单栈 SeekDB 不变式违反（检索池显式 host / port
            与权威库不一致——``CONFIG_INVALID``）。
    """
    seekdb_settings = settings.seekdb
    if not seekdb_settings.enabled:
        logger.info("记忆检索存储：SQLite 回退（keyword 模式——零向量依赖）")
        return SQLiteFallbackStore()
    resolved = resolve_seekdb_config(settings)
    config = SeekDBStoreConfig(
        host=resolved.host,
        port=resolved.port,
        user=resolved.user,
        password_env=resolved.password_env,
        database=resolved.database,
        vector_dim=resolved.vector_dim,
        pool_min=resolved.pool_min,
        pool_max=resolved.pool_max,
    )
    manager = get_pool_manager()
    if manager is not None and manager.started and manager.seekdb_pool is not None:
        logger.info("记忆检索存储：SeekDB（共享池注入——生命周期归 PoolManager）")
        return SeekDBStore(config, pool=manager.seekdb_pool)
    logger.info("记忆检索存储：SeekDB（自持池——惰性创建，close 归存储）")
    return SeekDBStore(config)
