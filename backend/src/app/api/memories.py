"""记忆管理 admin 路由——三层记忆的操作面（P1 chunk 1 交付）。

    - GET    /api/v1/memories/episodes                    —— 情景事件游标分页（过滤面）
    - POST   /api/v1/memories/episodes                    —— 手工注入（provenance 缺省 owner）
    - POST   /api/v1/memories/episodes/batch-delete       —— 批量删除（部分成功形态）
    - GET    /api/v1/memories/episodes/{id}               —— 读取
    - DELETE /api/v1/memories/episodes/{id}               —— 删除（append-only 的管理面出口）
    - GET    /api/v1/memories/curated                     —— 策展记忆游标分页（过滤面）
    - POST   /api/v1/memories/curated/upsert              —— upsert（supersede 语义）
    - GET    /api/v1/memories/curated/{id}/history        —— 历史链读取
    - DELETE /api/v1/memories/curated/{id}                —— 删除
    - GET    /api/v1/memories/sessions                    —— 会话游标分页
    - GET    /api/v1/memories/sessions/{id}               —— 会话详情（含全量消息）
    - DELETE /api/v1/memories/sessions/{id}               —— 删除（消息同事务清理）
    - POST   /api/v1/memories/search                      —— 记忆检索（P1 chunk 2：dense /
                                                                 keyword / hybrid 三模式）

人格作用域约定：三张 PersonaMixin 表的记忆数据按人格逻辑隔离——全部
端点先解析目标人格（``persona_id`` 查询参数显式指定；缺省 = 当前激活
人格，无激活人格则 ``NOT_FOUND`` fail loud），再经 ``bind_persona_context``
绑定上下文后调用服务（自动注入 / 自动过滤）。指定人格不存在 → 404；
他人格的资源对本作用域不可见（同样 404——隔离语义）。检索端点同构：
请求体 ``persona_id`` 可选（缺省激活人格），解析后作为检索查询的
必带隔离谓词（索引表无 PersonaMixin——显式 persona_id 列过滤）。

全部响应经 ``ResultDTO`` 封装；分页经 ``PageResponse``；DB Session 经
``database.session.get_async_db`` 依赖注入（事务边界归服务层）；全部
参数与请求体为类型化 Model Classes（无裸 dict 出入口）。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.config.models import settings as global_settings
from packages.harness.core.dtos import PageResponse, ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import get_async_db
from packages.harness.llm.gateway import LlmGateway
from packages.harness.memory.dtos import (
    ChatSessionDetailDTO,
    ChatSessionQueryDTO,
    ChatSessionSummaryDTO,
    CuratedQueryDTO,
    CuratedRecordDTO,
    CuratedUpsertDTO,
    EpisodeBatchDeleteRequest,
    EpisodeBatchDeleteResponseDTO,
    EpisodeCreateDTO,
    EpisodeQueryDTO,
    EpisodeRecordDTO,
)
from packages.harness.memory.models import (
    CuratedKind,
    CuratedStatus,
    EpisodeKind,
    MemoryProvenance,
)
from packages.harness.memory.retrieval import MemorySearchService, build_memory_store
from packages.harness.memory.retrieval_models import (
    MemorySearchQueryDTO,
    MemorySearchRequestDTO,
    MemorySearchResultDTO,
)
from packages.harness.memory.service import ChatSessionService, MemoryService
from packages.harness.memory.store import MemoryStore
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])

#: 记忆服务实例（无状态——Session 逐请求注入，服务可安全共享；indexer
#: 未绑定——app 装配的索引联动属后续 chunk，本路由保持 M1 写入行为）。
memory_service = MemoryService()
#: 会话持久化服务实例（同上）。
chat_session_service = ChatSessionService()
#: 人格服务实例（目标人格解析面）。
persona_service = PersonaService()

#: 检索存储进程级缓存（build_memory_store 产出——SeekDB 自持池 / 回退
#: 存储的 once 守卫跨请求复用；注入式池形态下无状态，缓存仅省构造）。
_memory_store: MemoryStore | None = None


def _get_memory_store() -> MemoryStore:
    """取记忆检索存储（进程级 once 构造——存储选择见 build_memory_store）。

    Returns:
        就绪的 :class:`MemoryStore`（SeekDB / SQLite 回退——由全局
        settings.seekdb.enabled 决定）。
    """
    global _memory_store
    if _memory_store is None:
        _memory_store = build_memory_store(global_settings)
    return _memory_store


def _resolve_request_gateway(request: Request) -> LlmGateway:
    """解析请求可用的 LLM 网关（app.state.gateway 优先；缺省等价兜底）。

    Args:
        request: 当前请求（app.state.gateway 由 lifespan 装配）。

    Returns:
        用于查询向量化的 :class:`LlmGateway`（未装配 lifespan 的本地
        测试面回落裸构造——与装配形态同语义：全局单例延迟解析）。
    """
    gateway = cast("LlmGateway | None", getattr(request.app.state, "gateway", None))
    if gateway is None:
        return LlmGateway()
    return gateway


async def _resolve_target_persona(
    db: AsyncSession, persona_id: int | None
) -> PersonaRecordDTO:
    """解析目标人格（显式 persona_id 优先；缺省当前激活人格）。

    Args:
        db: 注入的异步 Session。
        persona_id: 显式人格主键（None = 激活人格）。

    Returns:
        目标人格记录 DTO。

    Raises:
        BusinessException: 指定人格不存在（``NOT_FOUND``）；未指定且
            当前无激活人格（``NOT_FOUND``——fail loud，记忆面无人格
            上下文不可操作）。
    """
    if persona_id is not None:
        return await persona_service.get(db, persona_id)
    record = await persona_service.get_active_persona(db)
    if record is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND,
            message="当前没有激活人格，请先创建并激活一个人格再操作记忆面",
            details={"fix": "显式传入 persona_id 查询参数，或先激活一个人格"},
        )
    return record


# ---------------------------------------------------------------------------
# 情景事件（episodic——append-only 事件日志）
# ---------------------------------------------------------------------------


@router.get("/episodes", response_model=ResultDTO[PageResponse[EpisodeRecordDTO]])
async def list_episodes(
    kind: EpisodeKind | None = Query(None, description="事件类型过滤（None = 全部）"),
    provenance: MemoryProvenance | None = Query(None, description="来源可信度过滤"),
    start_ms: int | None = Query(None, ge=0, description="范围起点（epoch 毫秒，含）"),
    end_ms: int | None = Query(None, ge=0, description="范围终点（epoch 毫秒，不含）"),
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[EpisodeRecordDTO]]:
    """情景事件游标分页列表（id 降序；kind / provenance / 时间范围过滤）。

    Args:
        kind: 事件类型过滤（None = 全部）。
        provenance: 来源可信度过滤（None = 全部）。
        start_ms: 时间范围起点（epoch 毫秒，含）。
        end_ms: 时间范围终点（epoch 毫秒，不含）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        page = await memory_service.query_episodes(
            db,
            EpisodeQueryDTO(
                kind=kind,
                provenance=provenance,
                start_ms=start_ms,
                end_ms=end_ms,
                cursor=cursor,
                limit=limit,
            ),
        )
    return ResultDTO.ok(data=page)


@router.post("/episodes", response_model=ResultDTO[EpisodeRecordDTO])
async def create_episode(
    req: EpisodeCreateDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[EpisodeRecordDTO]:
    """手工注入情景事件（管理面；provenance 缺省 owner）。

    Args:
        req: 创建请求（kind / source / content 载荷 / importance /
            provenance / ts_ms）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        落库后的记录 DTO。

    Raises:
        BusinessException: 目标人格不存在（``NOT_FOUND``）；载荷与 kind
            不匹配（``VALIDATION_ERROR``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        episode = await memory_service.append_episode(db, req)
    return ResultDTO.ok(data=episode, message="情景事件已记录")


@router.post("/episodes/batch-delete", response_model=ResultDTO[EpisodeBatchDeleteResponseDTO])
async def batch_delete_episodes(
    req: EpisodeBatchDeleteRequest,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[EpisodeBatchDeleteResponseDTO]:
    """批量删除情景事件（部分成功形态——逐 id 独立事务）。

    Args:
        req: 批量删除请求（ids 1-100 个）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        批量结果（total / succeeded / failed / 逐 id 结果——缺失项记
        NOT_FOUND 失败）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        result = await memory_service.batch_delete_episodes(db, req.ids)
    return ResultDTO.ok(
        data=result, message=f"批量删除完成（成功 {result.succeeded} / 失败 {result.failed}）"
    )


@router.get("/episodes/{episode_id}", response_model=ResultDTO[EpisodeRecordDTO])
async def get_episode(
    episode_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[EpisodeRecordDTO]:
    """按主键读取情景事件（人格作用域外 / 缺失均 404）。

    Args:
        episode_id: 事件主键。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        记录 DTO。

    Raises:
        BusinessException: 事件不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        episode = await memory_service.get_episode(db, episode_id)
    return ResultDTO.ok(data=episode)


@router.delete("/episodes/{episode_id}", response_model=ResultDTO[None])
async def delete_episode(
    episode_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """删除单条情景事件（append-only 的管理面显式出口）。

    Args:
        episode_id: 事件主键。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        空 data 成功响应。

    Raises:
        BusinessException: 事件不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        await memory_service.delete_episode(db, episode_id)
    return ResultDTO[None].ok(message="情景事件已删除")


# ---------------------------------------------------------------------------
# 策展记忆（curated——supersede 语义）
# ---------------------------------------------------------------------------


@router.get("/curated", response_model=ResultDTO[PageResponse[CuratedRecordDTO]])
async def list_curated(
    kind: CuratedKind | None = Query(None, description="策展类型过滤（None = 全部）"),
    topic: str | None = Query(None, description="主题键精确过滤"),
    status: CuratedStatus | None = Query(None, description="状态过滤（None = 全部）"),
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[CuratedRecordDTO]]:
    """策展记忆游标分页列表（id 降序；kind / topic / status 过滤）。

    Args:
        kind: 策展类型过滤（None = 全部）。
        topic: 主题键精确过滤（None = 全部）。
        status: 状态过滤（None = 全部；active = 现行版本面）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        page = await memory_service.query_curated(
            db,
            CuratedQueryDTO(
                kind=kind, topic=topic, status=status, cursor=cursor, limit=limit
            ),
        )
    return ResultDTO.ok(data=page)


@router.post("/curated/upsert", response_model=ResultDTO[CuratedRecordDTO])
async def upsert_curated(
    req: CuratedUpsertDTO,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[CuratedRecordDTO]:
    """策展记忆 upsert（supersede 语义——同 (persona, kind, topic) 单 active）。

    Args:
        req: upsert 请求（kind / topic / content / importance / provenance）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        新 active 行的记录 DTO。

    Raises:
        BusinessException: 目标人格不存在（``NOT_FOUND``）；provenance=
            untrusted（``VALIDATION_ERROR``——平台内容永不入策展层）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        curated = await memory_service.upsert_curated(db, req)
    return ResultDTO.ok(data=curated, message="策展记忆已保存")


@router.get("/curated/{memory_id}/history", response_model=ResultDTO[list[CuratedRecordDTO]])
async def get_curated_history(
    memory_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[CuratedRecordDTO]]:
    """读取策展记忆历史链（supersede 链全量——版本升序）。

    Args:
        memory_id: 链内任一记忆主键（锚点）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        历史链记录列表（版本升序）。

    Raises:
        BusinessException: 记忆不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        history = await memory_service.get_curated_history(db, memory_id)
    return ResultDTO.ok(data=history)


@router.delete("/curated/{memory_id}", response_model=ResultDTO[None])
async def delete_curated(
    memory_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """删除单条策展记忆（管理面显式动作）。

    Args:
        memory_id: 记忆主键。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        空 data 成功响应。

    Raises:
        BusinessException: 记忆不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        await memory_service.delete_curated(db, memory_id)
    return ResultDTO[None].ok(message="策展记忆已删除")


# ---------------------------------------------------------------------------
# 会话（working——chat 会话持久化面）
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=ResultDTO[PageResponse[ChatSessionSummaryDTO]])
async def list_sessions(
    cursor: str | None = Query(None, description="分页游标（首页为 None）"),
    limit: int = Query(20, ge=1, le=200, description="页大小（1-200）"),
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[PageResponse[ChatSessionSummaryDTO]]:
    """会话游标分页列表（id 降序——新会话在前）。

    Args:
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200）。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        分页响应（``next_cursor`` 非空表示还有更早会话；total 恒 -1）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        page = await chat_session_service.list_sessions(
            db, ChatSessionQueryDTO(cursor=cursor, limit=limit)
        )
    return ResultDTO.ok(data=page)


@router.get("/sessions/{session_id}", response_model=ResultDTO[ChatSessionDetailDTO])
async def get_session(
    session_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[ChatSessionDetailDTO]:
    """读取会话详情（摘要 + 全量消息 seq 升序）。

    Args:
        session_id: 会话主键。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        会话详情 DTO。

    Raises:
        BusinessException: 会话不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        detail = await chat_session_service.get_session(db, session_id)
    return ResultDTO.ok(data=detail)


@router.delete("/sessions/{session_id}", response_model=ResultDTO[None])
async def delete_session(
    session_id: int,
    persona_id: int | None = Query(None, description="目标人格主键（None = 当前激活人格）"),
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[None]:
    """删除会话（消息同事务清理）。

    Args:
        session_id: 会话主键。
        persona_id: 目标人格主键（None = 当前激活人格）。
        db: 注入的异步 Session。

    Returns:
        空 data 成功响应。

    Raises:
        BusinessException: 会话不存在或不在目标人格作用域（``NOT_FOUND``）。
    """
    record = await _resolve_target_persona(db, persona_id)
    async with bind_persona_context(record.id):
        await chat_session_service.delete_session(db, session_id)
    return ResultDTO[None].ok(message="会话已删除")


# ---------------------------------------------------------------------------
# 记忆检索（retrieval——P1 chunk 2：索引面混合检索）
# ---------------------------------------------------------------------------


@router.post("/search", response_model=ResultDTO[list[MemorySearchResultDTO]])
async def search_memories(
    req: MemorySearchRequestDTO,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> ResultDTO[list[MemorySearchResultDTO]]:
    """记忆检索（dense / keyword / hybrid 三模式——索引面混合检索）。

    链路：解析目标人格（请求体 ``persona_id`` 可选——缺省激活人格）→
    ``bind_persona_context``（供嵌入用量归属）→ ``MemorySearchService``
    （dense / hybrid 先经网关 embed 角色向量化查询文本）→ 存储检索
    （SeekDB 混合 / SQLite 回退 keyword）。

    Args:
        req: 检索请求体（query / mode / 过滤 / limit / persona_id 可选）。
        request: 当前请求（app.state.gateway 解析——lifespan 装配优先）。
        db: 注入的异步 Session（人格解析面）。

    Returns:
        命中结果列表（score 方向随模式——见 retrieval_models）。

    Raises:
        BusinessException: 目标人格不存在或无激活人格（``NOT_FOUND``）；
            请求校验失败（``VALIDATION_ERROR``——空 query / limit 越界
            等经 FastAPI 422）；回退存储收到向量模式（``VALIDATION_ERROR``
            ——「回退存储不支持向量检索」）；嵌入失败（embed 角色未绑定
            ``MODEL_NOT_FOUND`` 等）。
    """
    record = await _resolve_target_persona(db, req.persona_id)
    gateway = _resolve_request_gateway(request)
    service = MemorySearchService(
        store=_get_memory_store(),
        embedder=gateway.embed_by_role,
    )
    query = MemorySearchQueryDTO(
        persona_id=record.id,
        query=req.query,
        mode=req.mode,
        kinds=req.kinds,
        provenance=req.provenance,
        start_ms=req.start_ms,
        end_ms=req.end_ms,
        limit=req.limit,
    )
    async with bind_persona_context(record.id):
        results = await service.search(query)
    return ResultDTO.ok(data=results)
