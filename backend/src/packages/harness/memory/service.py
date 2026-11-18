"""memory 域服务——MemoryService（情景 / 策展）+ ChatSessionService（会话）。

MemoryService（情景层 + 策展层）：

    - 情景层 append-only：append / append_batch（可选 content_hash 去重
      ——幂等摄入）/ 游标分页查询 / 单条读取 / 管理面删除（单条 + 批量
      部分成功）/ 日记输入面（全天事件按时间序取回）；
    - 策展层 supersede：upsert（同 (persona, kind, topic) active 行 →
      单事务内「新行落库 → 旧行标记 superseded + 链接」——至多一条
      active 的不变式由 DB 唯一键 ``uq_ff_curated_active`` 强制，
      并发双写经唯一键拒绝 + 重试一次收敛）/ 查询 / 单条读取 / 历史链
      读取 / 管理面删除；**untrusted 来源一律拒绝**（VALIDATION_ERROR
      ——平台内容永不入策展层，检索 / 晋升链路依赖此不变式）。

ChatSessionService（工作层）：

    - 会话生命周期：create / list（游标）/ get（含全量消息；可选嵌套变体）
      / delete（消息单语句批量清理 + 会话行 ORM 删除——两语句同事务）；
    - 消息追加：append_message（seq = message_count + 1 递增——会话行
      经 ``SELECT ... FOR UPDATE`` 行锁串行化计数，复合唯一键
      ``uq_ff_chat_messages_seq_variant`` 的变体身份语义见 ORM 模块文档
      + 冲突重试一次；会话行 message_count / last_active_at 同事务更新）；
    - 消息变体（swipes——``append_variant`` / ``list_variants``）：变体行
      seq = 父回合 seq、``variant_of_id`` 指向父 assistant 回合、
      ``variant_index`` 从父回合既有最大值续编；不推进 message_count /
      last_active_at（正典链计数只度量真实对话回合——变体是探索面）；
      仅 assistant 正典回合可作父回合（服务层强制，变体不再生成变体）；
    - 选定提升（``replace_message``）：以选中变体正文 / 元数据替换正典
      回合（父行幸存、变体行保留——select-variant 的唯一写点）。

人格作用域契约：三张 PersonaMixin 表（episodes / curated / sessions）
的读写经 database.base 的自动注入 / 自动过滤隔离——**调用方须先经
``persona.bind_persona_context`` 绑定人格上下文**（缺上下文 fail loud，
见 ``PERSONA_CONTEXT_REQUIRED``）；消息行（非 PersonaMixin）经所属
会话行间接隔离（先定位人格作用域会话，再按键取消息）。

Session 契约：方法接收调用方注入的 :class:`AsyncSession`（app 层经
``get_async_db``；测试直连共享引擎 Session）——服务层内部显式
``commit`` / 异常 ``rollback``（写路径事务边界归服务层）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from loguru import logger
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import (
    ChatMessageDTO,
    ChatMessageMetaModel,
    ChatMessageMiniDTO,
    ChatSessionDetailDTO,
    ChatSessionQueryDTO,
    ChatSessionSummaryDTO,
    CuratedQueryDTO,
    CuratedRecordDTO,
    CuratedUpsertDTO,
    EpisodeBatchDeleteItemDTO,
    EpisodeBatchDeleteResponseDTO,
    EpisodeCreateDTO,
    EpisodeQueryDTO,
    EpisodeRecordDTO,
)
from packages.harness.memory.models import (
    CuratedStatus,
    EpisodeKind,
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
from packages.harness.memory.retrieval import (
    MemoryIndexer,
    episode_to_index_entry,
)

__all__ = ["MemoryService", "ChatSessionService"]


async def _rollback_quietly(session: AsyncSession) -> None:
    """尽力回滚（批量路径逐项失败时清理会话待处理态——不注入新异常）。

    Args:
        session: 待回滚的异步 Session。
    """
    try:
        await session.rollback()
    except Exception:  # best-effort: 回滚失败由会话关闭兜底
        logger.opt(exception=True).debug("批量路径会话回滚失败（close 兜底）")


async def _commit(session: AsyncSession) -> None:
    """提交事务（异常回滚后原样上抛——写路径统一出口）。

    Args:
        session: 注入的异步 Session。

    Raises:
        Exception: 提交失败（已回滚，由上层异常处理器接面）。
    """
    try:
        await session.commit()
    except Exception:
        await _rollback_quietly(session)
        raise


def _episode_record(row: MemoryEpisodeRow) -> EpisodeRecordDTO:
    """情景事件行 → 记录 DTO（content 按 kind 判别解析）。

    Args:
        row: ORM 行。

    Returns:
        记录 DTO。

    Raises:
        BusinessException: 行 content 与 kind 不符（``VALIDATION_ERROR``
            ——写入经 DTO 边界校验，理论不可达；防御直改库场景）。
    """
    return EpisodeRecordDTO(
        id=row.id,
        persona_id=row.persona_id,
        kind=EpisodeKind(row.kind),
        source=row.source,
        content=parse_episode_payload(row.kind, row.content),
        importance=row.importance,
        provenance=MemoryProvenance(row.provenance),
        content_hash=row.content_hash,
        ts_ms=row.ts_ms,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: 策展单 active 守卫标记值（与 CuratedStatus.ACTIVE 同字面——DB 唯一
#: 键 uq_ff_curated_active 的非 NULL 侧标记；superseded 行为 NULL）。
_CURATED_ACTIVE_MARKER: str = CuratedStatus.ACTIVE.value


def _message_dto(
    row: ChatMessageRow,
    variants: Sequence[ChatMessageMiniDTO] | None = None,
) -> ChatMessageDTO:
    """会话消息行 → DTO（可选嵌套变体——详情读侧装配面）。

    Args:
        row: 消息 ORM 行（正典链行或变体行）。
        variants: 嵌套变体摘要列表（正典 assistant 回合按序号升序；
            None = 非嵌套查询——空列表）。

    Returns:
        消息 DTO（meta 原样按元数据模型归一）。
    """
    return ChatMessageDTO(
        id=row.id,
        session_id=row.session_id,
        seq=row.seq,
        role=ChatMessageRole(row.role),
        content=row.content,
        ts_ms=row.ts_ms,
        meta=(
            ChatMessageMetaModel.model_validate(row.meta) if row.meta is not None else None
        ),
        variants=list(variants) if variants is not None else [],
    )


class MemoryService:
    """记忆域服务（情景 + 策展；无状态——Session 由调用方注入）。

    检索索引联动（可选）：构造注入 ``indexer``
    （:class:`MemoryIndexer`——默认 None，写入路径零索引调用）后，
    情景事件**提交后**触发索引链（append → commit → embed → upsert；
    best-effort——任何失败仅告警，不影响写入结果与响应，见
    ``_fire_indexing``）。
    """

    def __init__(self, *, indexer: MemoryIndexer | None = None) -> None:
        """初始化服务。

        Args:
            indexer: 记忆索引管道（None = 未绑定——写入路径零索引调用；
                绑定后 append 提交即触发索引分发）。
        """
        self._indexer = indexer

    # ------------------------------------------------------------------
    # 情景层——append / 查询 / 删除 / 日记
    # ------------------------------------------------------------------

    async def append_episode(
        self,
        session: AsyncSession,
        dto: EpisodeCreateDTO,
        *,
        dedup: bool = False,
    ) -> EpisodeRecordDTO:
        """追加单条情景事件（append-only——无更新路径）。

        Args:
            session: 注入的异步 Session。
            dto: 创建 DTO（provenance 缺省 owner；ts_ms 缺省当前时间）。
            dedup: 内容指纹去重开关（True 时同 (persona, kind, source,
                content) 已存在则返回既有行——幂等摄入；同指纹多行时取
                id 最小行，命中确定；False 恒插入）。

        Returns:
            落库后的记录 DTO（去重命中时为既有行——命中路径不触发
            索引，既有事件假定已索引）。
        """
        content_hash = compute_content_hash(dto.kind, dto.source, dto.content)
        if dedup:
            existing = (
                (
                    await session.execute(
                        select(MemoryEpisodeRow)
                        .where(MemoryEpisodeRow.content_hash == content_hash)
                        .order_by(MemoryEpisodeRow.id.asc())
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                logger.debug("情景事件去重命中: hash={}", content_hash)
                return _episode_record(existing)
        row = MemoryEpisodeRow(
            kind=dto.kind.value,
            source=dto.source,
            content=dto.content.model_dump(),
            importance=dto.importance,
            provenance=dto.provenance.value,
            content_hash=content_hash,
            ts_ms=dto.ts_ms if dto.ts_ms is not None else now_ms(),
        )
        session.add(row)
        await _commit(session)
        record = _episode_record(row)
        await self._fire_indexing([record])
        return record

    async def append_episodes(
        self,
        session: AsyncSession,
        dtos: Sequence[EpisodeCreateDTO],
        *,
        dedup: bool = False,
    ) -> list[EpisodeRecordDTO]:
        """批量追加情景事件（单事务——全量成功或全量回滚）。

        去重（``dedup=True``）双通道：库内既有指纹（persona 作用域自动
        过滤）+ 批内指纹（同批同指纹后续项映射到首项结果）。

        Args:
            session: 注入的异步 Session。
            dtos: 创建 DTO 序列（空序列直接返回空列表）。
            dedup: 内容指纹去重开关。

        Returns:
            记录 DTO 列表（与入参同序；去重命中项为既有 / 批内首项行）。
        """
        if not dtos:
            return []
        records: list[EpisodeRecordDTO] = []
        batch_seen: dict[str, EpisodeRecordDTO] = {}
        pending: list[MemoryEpisodeRow] = []
        pending_records: list[EpisodeRecordDTO] = []
        for dto in dtos:
            content_hash = compute_content_hash(dto.kind, dto.source, dto.content)
            if dedup:
                if content_hash in batch_seen:
                    records.append(batch_seen[content_hash])
                    continue
                existing = (
                    (
                        await session.execute(
                            select(MemoryEpisodeRow)
                            .where(MemoryEpisodeRow.content_hash == content_hash)
                            .order_by(MemoryEpisodeRow.id.asc())
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    record = _episode_record(existing)
                    records.append(record)
                    batch_seen[content_hash] = record
                    continue
            row = MemoryEpisodeRow(
                kind=dto.kind.value,
                source=dto.source,
                content=dto.content.model_dump(),
                importance=dto.importance,
                provenance=dto.provenance.value,
                content_hash=content_hash,
                ts_ms=dto.ts_ms if dto.ts_ms is not None else now_ms(),
            )
            session.add(row)
            await session.flush()  # 取主键（去重 map 需要落库后形态）
            record = _episode_record(row)
            records.append(record)
            batch_seen[content_hash] = record
            pending.append(row)
            pending_records.append(record)
        if pending:
            await _commit(session)
            logger.info("情景事件批量落库: count={}", len(pending))
            await self._fire_indexing(pending_records)
        return records

    async def query_episodes(
        self, session: AsyncSession, query: EpisodeQueryDTO
    ) -> PageResponse[EpisodeRecordDTO]:
        """情景事件游标分页查询（id 降序——新事件在前；时间范围左闭右开）。

        Args:
            session: 注入的异步 Session。
            query: 查询 DTO（kind / provenance / 时间范围 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
        """
        stmt = select(MemoryEpisodeRow)
        if query.kind is not None:
            stmt = stmt.where(MemoryEpisodeRow.kind == query.kind.value)
        if query.provenance is not None:
            stmt = stmt.where(MemoryEpisodeRow.provenance == query.provenance.value)
        if query.start_ms is not None:
            stmt = stmt.where(MemoryEpisodeRow.ts_ms >= query.start_ms)
        if query.end_ms is not None:
            stmt = stmt.where(MemoryEpisodeRow.ts_ms < query.end_ms)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(MemoryEpisodeRow.id < cursor_id)
        stmt = stmt.order_by(desc(MemoryEpisodeRow.id)).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[_episode_record(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def get_episode(self, session: AsyncSession, episode_id: int) -> EpisodeRecordDTO:
        """按主键读取情景事件（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session。
            episode_id: 事件主键。

        Returns:
            记录 DTO。

        Raises:
            BusinessException: 事件不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_episode(session, episode_id)
        return _episode_record(row)

    async def delete_episode(self, session: AsyncSession, episode_id: int) -> None:
        """删除单条情景事件（管理面显式动作——append-only 的唯一出口）。

        Args:
            session: 注入的异步 Session。
            episode_id: 事件主键。

        Raises:
            BusinessException: 事件不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_episode(session, episode_id)
        await session.delete(row)
        await _commit(session)
        logger.info("情景事件已删除（管理面）: id={}", episode_id)

    async def batch_delete_episodes(
        self, session: AsyncSession, ids: Sequence[int]
    ) -> EpisodeBatchDeleteResponseDTO:
        """批量删除情景事件（部分成功形态——逐 id 独立事务）。

        Args:
            session: 注入的异步 Session。
            ids: 待删除事件主键序列（唯一性由请求 DTO
                ``EpisodeBatchDeleteRequest`` 校验强制——重复项 422）。

        Returns:
            批量删除响应（total / succeeded / failed / 逐 id 结果）。
        """
        items: list[EpisodeBatchDeleteItemDTO] = []
        for episode_id in ids:
            try:
                await self.delete_episode(session, episode_id)
                items.append(
                    EpisodeBatchDeleteItemDTO(
                        id=episode_id, success=True, message="情景事件已删除"
                    )
                )
            except BusinessException as exc:
                await _rollback_quietly(session)
                items.append(
                    EpisodeBatchDeleteItemDTO(
                        id=episode_id,
                        success=False,
                        code=exc.code,
                        message=exc.message,
                    )
                )
        succeeded = sum(1 for item in items if item.success)
        return EpisodeBatchDeleteResponseDTO(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            items=items,
        )

    async def fetch_day_episodes(
        self,
        session: AsyncSession,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[EpisodeRecordDTO]:
        """日记输入面——取回时间范围内全部情景事件（时间升序）。

        排序 ``(ts_ms, id)`` 升序——同刻多事件按写入序稳定排列；范围左
        闭右开（``start_ms <= ts_ms < end_ms``——按天切一日记）。

        Args:
            session: 注入的异步 Session。
            start_ms: 范围起点（epoch 毫秒，含）。
            end_ms: 范围终点（epoch 毫秒，不含）。

        Returns:
            事件记录列表（时间序）。
        """
        rows = list(
            (
                await session.execute(
                    select(MemoryEpisodeRow)
                    .where(
                        MemoryEpisodeRow.ts_ms >= start_ms,
                        MemoryEpisodeRow.ts_ms < end_ms,
                    )
                    .order_by(MemoryEpisodeRow.ts_ms.asc(), MemoryEpisodeRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_episode_record(row) for row in rows]

    async def _fire_indexing(self, records: Sequence[EpisodeRecordDTO]) -> None:
        """提交后触发检索索引链（best-effort——任何失败仅告警）。

        链路：episode_to_index_entry（人格 ID 数值化 + 载荷文本化）→
        indexer.dispatch（fire-and-forget / 内联 await 见 retrieval 模块
        分发决策）。索引未绑定时零调用。

        Args:
            records: 本次新增落库的记录（去重命中项不含——既有事件
                假定已索引）。
        """
        if self._indexer is None or not records:
            return
        try:
            entries = [episode_to_index_entry(record) for record in records]
        except Exception:  # best-effort: 条目构建失败（载荷无文本 / 人格 ID 非数值）不阻断写入
            logger.opt(exception=True).warning("情景事件索引条目构建失败（已吞没——不影响写入结果）")
            return
        try:
            if len(entries) == 1:
                await self._indexer.dispatch_episode(entries[0])
            else:
                await self._indexer.dispatch_episodes(entries)
        except Exception:  # best-effort: 分发失败不阻断写入方
            logger.opt(exception=True).warning("情景事件索引分发失败（已吞没——不影响写入结果）")

    async def _require_episode(self, session: AsyncSession, episode_id: int) -> MemoryEpisodeRow:
        """按主键取情景事件行（人格作用域自动过滤）；缺失 fail loud。

        Args:
            session: 注入的异步 Session。
            episode_id: 事件主键。

        Returns:
            对应 :class:`MemoryEpisodeRow`。

        Raises:
            BusinessException: 事件不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = (
            (
                await session.execute(
                    select(MemoryEpisodeRow).where(MemoryEpisodeRow.id == episode_id)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="情景事件不存在",
                details={"episode_id": episode_id},
            )
        return row

    # ------------------------------------------------------------------
    # 策展层——upsert（supersede）/ 查询 / 历史 / 删除
    # ------------------------------------------------------------------

    async def upsert_curated(
        self, session: AsyncSession, dto: CuratedUpsertDTO
    ) -> CuratedRecordDTO:
        """策展记忆 upsert（supersede 语义——单事务原子切换 + 并发收敛）。

        链路：同 (persona, kind, topic) 无 active 行 → 插入新 active 行
        （version=1）；有 → 新行落库（version=旧行+1）→ 旧行标记
        superseded + superseded_by_id 链接 → 新行取得 active 标记——
        单事务提交。**至多一条 active 的不变式由 DB 唯一键
        ``uq_ff_curated_active (persona_id, kind, topic, active_marker)``
        强制**（active 行 ``active_marker='active'``、superseded 行
        NULL——MySQL / SQLite 唯一键均忽略 NULL，多代 superseded 行
        共存）；并发 upsert 撞唯一键时回滚后重试一次（重读 active 行
        走 supersede 分支），再冲突则映射 ``INTERNAL_ERROR``。

        Args:
            session: 注入的异步 Session。
            dto: upsert DTO。

        Returns:
            新 active 行的记录 DTO。

        Raises:
            BusinessException: provenance=untrusted（``VALIDATION_ERROR``
                ——平台内容永不入策展层）；重试后仍并发冲突
                （``INTERNAL_ERROR``）。
        """
        if dto.provenance == MemoryProvenance.UNTRUSTED:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="untrusted 来源的内容禁止进入策展记忆",
                details={"topic": dto.topic, "provenance": dto.provenance.value},
            )
        try:
            return await self._upsert_curated_once(session, dto)
        except IntegrityError:
            # 并发 upsert 抢先落了 active 行——回滚后重读走 supersede 分支
            await _rollback_quietly(session)
            logger.info(
                "策展记忆并发单 active 冲突，重读 active 行后重试: topic={}", dto.topic
            )
        try:
            return await self._upsert_curated_once(session, dto)
        except IntegrityError as exc:
            await _rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="策展记忆并发写入冲突（重试后仍冲突）",
                details={"topic": dto.topic},
            ) from exc

    async def _upsert_curated_once(
        self, session: AsyncSession, dto: CuratedUpsertDTO
    ) -> CuratedRecordDTO:
        """单次 upsert 尝试（无重试——由 ``upsert_curated`` 编排并发收敛）。

        语句序（确定性——腾位后占位）：无既有 active 行 → 新行直接携带
        active 标记插入；有 → 新行先以 NULL 标记落库（取主键）→ 旧行
        解除标记 + superseded + 链接（flush）→ 新行取得 active 标记 →
        提交。任一 flush 点至多一条 ``active_marker='active'`` 行。

        Args:
            session: 注入的异步 Session。
            dto: upsert DTO（untrusted 已在公共入口拒绝）。

        Returns:
            新 active 行的记录 DTO。

        Raises:
            IntegrityError: 并发双写撞 ``uq_ff_curated_active``（由上层
                重试收敛）。
        """
        active_row = (
            (
                await session.execute(
                    select(CuratedMemoryRow).where(
                        CuratedMemoryRow.kind == dto.kind.value,
                        CuratedMemoryRow.topic == dto.topic,
                        CuratedMemoryRow.status == CuratedStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .first()
        )
        new_version = active_row.version + 1 if active_row is not None else 1
        row = CuratedMemoryRow(
            kind=dto.kind.value,
            topic=dto.topic,
            content=dto.content,
            importance=dto.importance,
            provenance=dto.provenance.value,
            status=CuratedStatus.ACTIVE.value,
            superseded_by_id=None,
            version=new_version,
            active_marker=_CURATED_ACTIVE_MARKER if active_row is None else None,
        )
        session.add(row)
        if active_row is not None:
            await session.flush()  # 取新行主键——旧行链接需要
            active_row.status = CuratedStatus.SUPERSEDED.value
            active_row.superseded_by_id = row.id
            active_row.active_marker = None
            await session.flush()  # 旧行先解除 active 标记——唯一键腾位（语句序确定）
            row.active_marker = _CURATED_ACTIVE_MARKER
            logger.info(
                "策展记忆已取代: topic={}, old_version={}, new_version={}",
                dto.topic,
                active_row.version,
                new_version,
            )
        await _commit(session)
        return CuratedRecordDTO.model_validate(row)

    async def get_curated(self, session: AsyncSession, memory_id: int) -> CuratedRecordDTO:
        """按主键读取策展记忆（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session。
            memory_id: 记忆主键。

        Returns:
            记录 DTO。

        Raises:
            BusinessException: 记忆不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_curated(session, memory_id)
        return CuratedRecordDTO.model_validate(row)

    async def query_curated(
        self, session: AsyncSession, query: CuratedQueryDTO
    ) -> PageResponse[CuratedRecordDTO]:
        """策展记忆游标分页查询（id 降序；kind / topic / status 过滤）。

        Args:
            session: 注入的异步 Session。
            query: 查询 DTO。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1）。
        """
        stmt = select(CuratedMemoryRow)
        if query.kind is not None:
            stmt = stmt.where(CuratedMemoryRow.kind == query.kind.value)
        if query.topic is not None:
            stmt = stmt.where(CuratedMemoryRow.topic == query.topic)
        if query.status is not None:
            stmt = stmt.where(CuratedMemoryRow.status == query.status.value)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(CuratedMemoryRow.id < cursor_id)
        stmt = stmt.order_by(desc(CuratedMemoryRow.id)).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[CuratedRecordDTO.model_validate(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def get_curated_history(
        self, session: AsyncSession, memory_id: int
    ) -> list[CuratedRecordDTO]:
        """读取策展记忆历史链（supersede 链全量——版本升序）。

        链等价性：supersede 语义保证同 (persona, kind, topic) 全部行构成
        单链——历史链即该 (kind, topic) 全部行按 (version, id) 升序。

        Args:
            session: 注入的异步 Session。
            memory_id: 链内任一记忆主键（锚点——取其 kind/topic 定链）。

        Returns:
            历史链记录列表（版本升序；管理面删除导致多代链时同代按 id 序）。

        Raises:
            BusinessException: 记忆不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_curated(session, memory_id)
        chain_rows = list(
            (
                await session.execute(
                    select(CuratedMemoryRow)
                    .where(
                        CuratedMemoryRow.kind == row.kind,
                        CuratedMemoryRow.topic == row.topic,
                    )
                    .order_by(CuratedMemoryRow.version.asc(), CuratedMemoryRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [CuratedRecordDTO.model_validate(chain_row) for chain_row in chain_rows]

    async def delete_curated(self, session: AsyncSession, memory_id: int) -> None:
        """删除单条策展记忆（管理面显式动作）。

        删除 active 行后该 topic 无 active 行，下次 upsert 重新起链
        （version 从 1 重新计数——文档化决策）。

        Args:
            session: 注入的异步 Session。
            memory_id: 记忆主键。

        Raises:
            BusinessException: 记忆不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_curated(session, memory_id)
        await session.delete(row)
        await _commit(session)
        logger.info("策展记忆已删除（管理面）: id={}", memory_id)

    async def _require_curated(
        self, session: AsyncSession, memory_id: int
    ) -> CuratedMemoryRow:
        """按主键取策展记忆行（人格作用域自动过滤）；缺失 fail loud。

        Args:
            session: 注入的异步 Session。
            memory_id: 记忆主键。

        Returns:
            对应 :class:`CuratedMemoryRow`。

        Raises:
            BusinessException: 记忆不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = (
            (
                await session.execute(
                    select(CuratedMemoryRow).where(CuratedMemoryRow.id == memory_id)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="策展记忆不存在",
                details={"memory_id": memory_id},
            )
        return row


class ChatSessionService:
    """会话持久化服务（工作层——会话生命周期 + 消息追加）。

    人格作用域：会话行挂 PersonaMixin（自动注入 / 过滤——调用方先绑定
    ``bind_persona_context``）；消息行经所属会话间接隔离。
    """

    async def create_session(
        self,
        session: AsyncSession,
        *,
        title: str | None = None,
    ) -> ChatSessionSummaryDTO:
        """创建会话行（人格作用域自动注入 persona_id）。

        Args:
            session: 注入的异步 Session。
            title: 会话标题（None / 空 = 「未命名会话」）。

        Returns:
            落库后的会话摘要 DTO。
        """
        row = ChatSessionRow(
            title=title if title else "未命名会话",
            message_count=0,
            last_active_at=now_ms(),
        )
        session.add(row)
        await _commit(session)
        logger.info("会话已落库: id={}, title={}", row.id, row.title)
        return ChatSessionSummaryDTO.model_validate(row)

    async def list_sessions(
        self, session: AsyncSession, query: ChatSessionQueryDTO
    ) -> PageResponse[ChatSessionSummaryDTO]:
        """会话游标分页列表（id 降序——新会话在前；人格作用域自动过滤）。

        Args:
            session: 注入的异步 Session。
            query: 查询 DTO（游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早会话；total 恒 -1）。
        """
        stmt = select(ChatSessionRow)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(ChatSessionRow.id < cursor_id)
        stmt = stmt.order_by(desc(ChatSessionRow.id)).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[ChatSessionSummaryDTO.model_validate(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def get_session(
        self, session: AsyncSession, session_id: int, *, include_variants: bool = False
    ) -> ChatSessionDetailDTO:
        """读取会话详情（正典链摘要 + 全量消息 seq 升序；可选嵌套变体）。

        消息列表**恒只含正典对话链行**（``variant_of_id IS NULL``）——
        变体不进入 seq 链（与父回合同 seq），默认不返回；``include_variants``
        为真时按父回合主键分组把变体摘要嵌套进对应正典 assistant 回合
        （``variants`` 字段，序号升序）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            include_variants: 是否嵌套变体列表（默认 False——既有读侧
                零变更；前端会话详情打开时置真）。

        Returns:
            会话详情 DTO。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_session(session, session_id)
        message_rows = list(
            (
                await session.execute(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.session_id == session_id,
                        ChatMessageRow.variant_of_id.is_(None),
                    )
                    .order_by(ChatMessageRow.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        variants_by_parent: dict[int, list[ChatMessageMiniDTO]] = {}
        if include_variants and message_rows:
            variant_rows = list(
                (
                    await session.execute(
                        select(ChatMessageRow)
                        .where(
                            ChatMessageRow.session_id == session_id,
                            ChatMessageRow.variant_of_id.is_not(None),
                        )
                        .order_by(
                            ChatMessageRow.variant_of_id.asc(),
                            ChatMessageRow.variant_index.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for variant_row in variant_rows:
                if variant_row.variant_of_id is None:
                    continue
                variants_by_parent.setdefault(variant_row.variant_of_id, []).append(
                    ChatMessageMiniDTO(
                        id=variant_row.id,
                        content=variant_row.content,
                        ts_ms=variant_row.ts_ms,
                        meta=(
                            ChatMessageMetaModel.model_validate(variant_row.meta)
                            if variant_row.meta is not None
                            else None
                        ),
                    )
                )
        return ChatSessionDetailDTO(
            id=row.id,
            persona_id=row.persona_id,
            title=row.title,
            message_count=row.message_count,
            created_at=row.created_at,
            last_active_at=row.last_active_at,
            messages=[
                _message_dto(message, variants_by_parent.get(message.id))
                for message in message_rows
            ],
        )

    async def ensure_session(self, session: AsyncSession, session_id: int) -> None:
        """校验会话存在且处于当前人格作用域（轻量归属检查——无消息读取）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        await self._require_session(session, session_id)

    async def require_message(
        self, session: AsyncSession, session_id: int, message_id: int
    ) -> ChatMessageRow:
        """按主键取会话消息行（归属校验；正典链行与变体行均可）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            message_id: 消息主键。

        Returns:
            消息 ORM 行（调用方按 role / variant_of_id 判定语义）。

        Raises:
            BusinessException: 消息不存在或不属于该会话（``NOT_FOUND``）。
        """
        row = (
            (
                await session.execute(
                    select(ChatMessageRow).where(
                        ChatMessageRow.id == message_id,
                        ChatMessageRow.session_id == session_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="会话消息不存在",
                details={"session_id": session_id, "message_id": message_id},
            )
        return row

    async def append_variant(
        self,
        session: AsyncSession,
        session_id: int,
        *,
        parent_message_id: int,
        variant_index: int,
        content: str,
        ts_ms: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessageDTO:
        """追加单条消息变体（父回合下的候选回复——不推进正典计数）。

        变体行 seq = 父回合 seq、``variant_of_id`` = 父回合主键、
        ``variant_index`` 由调用方给出（同父回合内唯一——复合唯一键兜底）；
        会话行 ``message_count`` / ``last_active_at`` **不更新**（变体是
        探索面，正典链计数只度量真实对话回合，文档化决策）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            parent_message_id: 父回合主键（须为 assistant 正典链行）。
            variant_index: 父回合内序号（>=1——续编由调用方计算）。
            content: 变体可见正文（非空）。
            ts_ms: 变体生成时间（epoch 毫秒；None = 当前时间）。
            meta: 变体元数据（心声 / 情绪增量 / variant_index；原样落 JSON）。

        Returns:
            落库后的变体消息 DTO（``variants`` 恒空）。

        Raises:
            BusinessException: 父回合不存在 / 非 assistant / 本身是变体
                （``VALIDATION_ERROR``）；序号非法（``VALIDATION_ERROR``）；
                并发撞复合唯一键（``INTERNAL_ERROR``——重试由调用方）。
        """
        parent = await self.require_message(session, session_id, parent_message_id)
        if parent.role != ChatMessageRole.ASSISTANT.value:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="仅 assistant 回合可生成变体",
                details={"message_id": parent_message_id, "role": parent.role},
            )
        if parent.variant_of_id is not None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="变体不可再生成变体（父回合须为正典链行）",
                details={"message_id": parent_message_id},
            )
        if variant_index < 1:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="变体序号必须从 1 起",
                details={"variant_index": variant_index},
            )
        row = ChatMessageRow(
            session_id=session_id,
            seq=parent.seq,
            role=ChatMessageRole.ASSISTANT.value,
            content=content,
            ts_ms=ts_ms if ts_ms is not None else now_ms(),
            meta=meta,
            variant_of_id=parent.id,
            variant_index=variant_index,
        )
        session.add(row)
        try:
            await _commit(session)
        except IntegrityError as exc:
            await _rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="变体并发生成冲突（同父回合同序号已被占用）",
                details={"parent_message_id": parent_message_id, "variant_index": variant_index},
            ) from exc
        return _message_dto(row)

    async def list_variants(
        self, session: AsyncSession, session_id: int, parent_message_id: int
    ) -> list[ChatMessageDTO]:
        """读取父回合下的全部变体（序号升序——续编序号计算面）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            parent_message_id: 父回合主键（缺失即 ``NOT_FOUND``）。

        Returns:
            变体消息 DTO 列表（variant_index 升序）。
        """
        await self.require_message(session, session_id, parent_message_id)
        rows = list(
            (
                await session.execute(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.session_id == session_id,
                        ChatMessageRow.variant_of_id == parent_message_id,
                    )
                    .order_by(ChatMessageRow.variant_index.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_message_dto(row) for row in rows]

    async def list_canonical_messages_before(
        self, session: AsyncSession, session_id: int, *, before_seq: int
    ) -> list[ChatMessageDTO]:
        """读取正典对话链在指定 seq 之前的全部回合（时间序——重生成上下文面）。

        **只回放正典链**（``variant_of_id IS NULL``）：变体是探索面，不
        进入后续组装（swipes 语义——每回合针对原始对话脊柱重掷；文档化
        限制，见 docs/backend/chat.md「消息变体」）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            before_seq: 上界（严格小于——待重生成父回合的 seq）。

        Returns:
            正典回合 DTO 列表（seq 升序；``variants`` 恒空）。
        """
        rows = list(
            (
                await session.execute(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.session_id == session_id,
                        ChatMessageRow.variant_of_id.is_(None),
                        ChatMessageRow.seq < before_seq,
                    )
                    .order_by(ChatMessageRow.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_message_dto(row) for row in rows]

    async def next_variant_index(
        self, session: AsyncSession, session_id: int, parent_message_id: int
    ) -> int:
        """计算父回合下一变体序号（既有最大值 + 1；无变体为 1）。

        变体不清洗（重生成追加而非替换——历史候选全部保留，文档化
        决策）：序号续编避免撞复合唯一键。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            parent_message_id: 父回合主键。

        Returns:
            下一可用变体序号（>=1）。

        Raises:
            BusinessException: 父回合不存在或不属于该会话（``NOT_FOUND``）。
        """
        await self.require_message(session, session_id, parent_message_id)
        max_index = (
            await session.execute(
                select(func.max(ChatMessageRow.variant_index)).where(
                    ChatMessageRow.session_id == session_id,
                    ChatMessageRow.variant_of_id == parent_message_id,
                )
            )
        ).scalar_one()
        return (int(max_index) if max_index is not None else 0) + 1

    async def replace_message(
        self,
        session: AsyncSession,
        session_id: int,
        message_id: int,
        *,
        content: str,
        meta: dict[str, Any] | None,
    ) -> ChatMessageDTO:
        """以选定变体内容替换正典回合正文 / 元数据（select-variant 写点）。

        父回合幸存且保持正典链身份（``variant_of_id`` 仍为 NULL）；变体行
        原样保留（历史可回溯——不清洗，文档化决策）。仅 assistant 正典
        链行可被替换（服务层强制）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            message_id: 正典回合主键。
            content: 新正文（选中变体可见文本——非空）。
            meta: 新元数据（选中变体协议元数据 + 选中注记）。

        Returns:
            更新后的正典消息 DTO。

        Raises:
            BusinessException: 消息不存在（``NOT_FOUND``）；非 assistant /
                本身是变体（``VALIDATION_ERROR``）。
        """
        row = await self.require_message(session, session_id, message_id)
        if row.role != ChatMessageRole.ASSISTANT.value or row.variant_of_id is not None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="仅正典 assistant 回合可被变体替换",
                details={"message_id": message_id, "role": row.role},
            )
        row.content = content
        row.meta = meta
        await _commit(session)
        return _message_dto(row)

    async def delete_session(self, session: AsyncSession, session_id: int) -> None:
        """删除会话（消息 + 会话行同事务清理——两语句形态）。

        消息经单条批量 DELETE 语句按 session_id 清理（不逐行加载 /
        逐行删除）；会话行经 ORM delete（人格作用域已由 ``_require_session``
        校验）。SQLite 测试方言不启用 FK 强制——显式两语句保持跨方言
        一致（DB 级 CASCADE 仅直删 SQL 兜底）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = await self._require_session(session, session_id)
        await session.execute(
            delete(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        )
        await session.delete(row)
        await _commit(session)
        logger.info("会话已删除（含消息）: id={}", session_id)

    async def append_message(
        self,
        session: AsyncSession,
        session_id: int,
        *,
        role: ChatMessageRole,
        content: str,
        ts_ms: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessageDTO:
        """追加会话消息（seq = message_count + 1 递增；会话行同事务更新）。

        并发不变式（双层防线）：会话行经 ``SELECT ... FOR UPDATE``
        锁定读取（MySQL 行锁串行化同会话并发追加——第二事务阻塞至
        首者提交后取到新计数；SQLite 方言无 FOR UPDATE，但其单写者
        天然串行）；正典链 seq 的唯一性由该行锁计数维持（迁移 0010 后
        DB 级复合键 ``uq_ff_chat_messages_seq_variant`` 的守卫面让位于
        变体身份——正典行两列 NULL 被唯一键忽略，见 ORM 模块文档）；并发
        双写撞键时回滚重读计数重算 seq 重试一次，再冲突映射
        ``INTERNAL_ERROR``。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            role: 消息角色。
            content: 消息文本（非空）。
            ts_ms: 消息时间（epoch 毫秒；None = 当前时间）。
            meta: 回合元数据（可空——心声 / 情绪增量 / 情绪快照；缺省
                None 保持既有调用方零变更）。

        Returns:
            落库后的消息 DTO。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域
                （``NOT_FOUND``）；重试后仍 seq 冲突（``INTERNAL_ERROR``）。
        """
        try:
            message = await self._append_message_once(
                session, session_id, role=role, content=content, ts_ms=ts_ms, meta=meta
            )
        except IntegrityError:
            # 并发追加抢先落了同 seq 行——回滚重读计数重算（行锁下重读
            # 已含并发赢家推进后的计数）
            await _rollback_quietly(session)
            logger.info("会话消息 seq 并发冲突，重读计数重算重试: session_id={}", session_id)
            try:
                message = await self._append_message_once(
                    session, session_id, role=role, content=content, ts_ms=ts_ms, meta=meta
                )
            except IntegrityError as exc:
                await _rollback_quietly(session)
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="会话消息并发写入冲突（重试后仍 seq 冲突）",
                    details={"session_id": session_id},
                ) from exc
        return ChatMessageDTO.model_validate(message)

    async def _append_message_once(
        self,
        session: AsyncSession,
        session_id: int,
        *,
        role: ChatMessageRole,
        content: str,
        ts_ms: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessageRow:
        """单次消息追加尝试（行锁读取 + 计数推进——无重试）。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            role: 消息角色。
            content: 消息文本（非空）。
            ts_ms: 消息时间（epoch 毫秒；None = 当前时间）。
            meta: 回合元数据（可空——原样落 JSON 列）。

        Returns:
            落库后的消息 ORM 行。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域
                （``NOT_FOUND``）。
            IntegrityError: 并发追加撞复合唯一键 ``uq_ff_chat_messages_seq_variant``
                （由上层重试收敛）。
        """
        row = await self._require_session(session, session_id, for_update=True)
        resolved_ts = ts_ms if ts_ms is not None else now_ms()
        message = ChatMessageRow(
            session_id=session_id,
            seq=row.message_count + 1,
            role=role.value,
            content=content,
            ts_ms=resolved_ts,
            meta=meta,
        )
        session.add(message)
        row.message_count = message.seq
        row.last_active_at = resolved_ts
        await _commit(session)
        return message

    async def _require_session(
        self, session: AsyncSession, session_id: int, *, for_update: bool = False
    ) -> ChatSessionRow:
        """按主键取会话行（人格作用域自动过滤）；缺失 fail loud。

        Args:
            session: 注入的异步 Session。
            session_id: 会话主键。
            for_update: True 时行锁读取（``SELECT ... FOR UPDATE``——
                MySQL 行锁串行化并发；SQLite 方言忽略该子句，由其单
                写者串行承担同等语义）。

        Returns:
            对应 :class:`ChatSessionRow`。

        Raises:
            BusinessException: 会话不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        stmt = select(ChatSessionRow).where(ChatSessionRow.id == session_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="会话不存在",
                details={"session_id": session_id},
            )
        return row
