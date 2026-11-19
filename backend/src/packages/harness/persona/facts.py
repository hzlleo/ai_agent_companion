"""persona 域人物事实服务——结构化事实 / 证据链 / 更正循环 / compose 注入面。

职责链（管理面 API / 面板 / 夜间梦境 / 对话组装共用同一服务）::

    upsert_fact(session, dto, provenance=?, source=?)   创建 / 同身份元数据刷新
    correct_fact(session, id, dto)                      主人更正（supersede 链 +1）
    reject_fact(session, id)                            驳回（active → rejected）
    delete_fact(session, id)                            硬删除（管理面显式动作）
    list_facts(session, query)                          游标分页（类别 / 状态过滤）
    top_facts_for_compose(session, ...)                 compose 注入面（高置信裁剪）

写入语义（锁定——与 ``ff_curated_memory`` 同一已验证的 supersede 模式）：

    - **事实身份 = 归一化正文指纹**：正文经 NFKC + 空白折叠 + 裁剪后取
      sha256；同 ``(persona, category, hash)`` 的现行行唯一（DB 唯一键
      含可空守卫标记 ``active_marker``——non-active 行 NULL，两方言唯一键
      均忽略 NULL，故历史行共存、驳回后可重新起链）；
    - **同身份重复写入 = 元数据刷新**：置信度取 ``max(旧, 新)`` + 证据
      追加合并，不推进版本、不产生新行（梦境反复提取同一条事实不应膨胀
      历史链）；
    - **更正 = 新版本 + 旧行 superseded**：新正文成为现行行（``version``
      = 旧行 + 1），旧行 ``superseded_by_id`` 指向新行；证据**保留旧行
      全部引用并追加更正说明**（``quote`` 形如「主人纠正：…」，最多 200
      字符），来源 ``correction`` / 可信度 ``owner``；
    - **provenance 规则（服务层强制）**：``dream`` 来源必须 ``agent``；
      ``manual`` / ``correction`` 必须 ``owner``；``untrusted`` 一律拒绝
      （平台内容不进画像——与策展层同一硬门）；
    - **证据校验**：带 ``episode_id`` 的证据引用须指向当前人格作用域内
      真实存在的情景事件；校验失败按「丢弃该条证据 + 告警」处理，**不**
      抛出（画像写入不因单条证据失效而失败——证据是增强面）。

compose 注入面（:meth:`FactsService.top_facts_for_compose`）：现行事实按
置信度降序取前 :data:`~packages.harness.persona.facts_models.FACT_COMPOSE_MAX_FACTS`
条、总字符不超过
:data:`~packages.harness.persona.facts_models.FACT_COMPOSE_MAX_CHARS`
（含类别标签与行符号）——输出已装配的注入行（``【类别标签】正文``），
由 chat 域模板包裹为【关于对方】块。

人格作用域：与记忆域同轴（PersonaMixin 自动注入 / 过滤）——调用方须先
经 ``bind_persona_context`` 绑定上下文，否则 fail loud。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Sequence

from loguru import logger
from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core import time_utils
from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.memory.dtos import EpisodeRecordDTO
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.retrieval import flatten_episode_content
from packages.harness.memory.service import MemoryService
from packages.harness.persona.facts_models import (
    FACT_CATEGORY_LABELS,
    FACT_COMPOSE_MAX_CHARS,
    FACT_COMPOSE_MAX_FACTS,
    FACT_CONTENT_MAX_LENGTH,
    FACT_EVIDENCE_QUOTE_MAX_LENGTH,
    FactCorrectDTO,
    FactCreateDTO,
    FactEvidenceRefModel,
    FactQueryDTO,
    FactRecordDTO,
    FactUpsertResultModel,
    PersonaFactCategory,
    PersonaFactSource,
    PersonaFactStatus,
)
from packages.harness.persona.facts_orm import FACT_ACTIVE_MARKER, PersonaFactRow

__all__ = [
    "normalize_fact_content",
    "fact_content_hash",
    "FactsService",
]

#: 空白折叠正则（归一化——多空白折为单空格）。
_WHITESPACE_PATTERN = re.compile(r"\s+")

#: 更正说明前缀（证据追加面的固定措辞——主人纠正的可审计痕迹）。
_CORRECTION_QUOTE_PREFIX = "主人纠正："


def normalize_fact_content(content: str) -> str:
    """归一化事实正文（NFKC + 空白折叠 + 首尾裁剪——事实身份的前置步骤）。

    Args:
        content: 原始正文。

    Returns:
        归一化后的正文（空串表示原文仅有空白）。
    """
    normalized = unicodedata.normalize("NFKC", content).strip()
    return _WHITESPACE_PATTERN.sub(" ", normalized)


def fact_content_hash(normalized_content: str) -> str:
    """计算事实身份指纹（归一化正文的 sha256 hex——64 字符）。

    Args:
        normalized_content: 已归一化的正文（调用方先经
            :func:`normalize_fact_content`）。

    Returns:
        sha256 十六进制摘要（事实身份轴，落 ``normalized_hash`` 列）。
    """
    return hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()


def _record(row: PersonaFactRow) -> FactRecordDTO:
    """事实行 → 读取 DTO（证据 JSON 反序列化为 Model Class 列表）。

    Args:
        row: 事实行。

    Returns:
        事实记录 DTO。
    """
    evidence = [FactEvidenceRefModel.model_validate(item) for item in row.evidence]
    return FactRecordDTO(
        id=row.id,
        category=PersonaFactCategory(row.category),
        content=row.content,
        confidence=row.confidence,
        status=PersonaFactStatus(row.status),
        provenance=MemoryProvenance(row.provenance),
        source=PersonaFactSource(row.source),
        evidence=evidence,
        superseded_by_id=row.superseded_by_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_content(content: str) -> str:
    """校验并归一化事实正文（1-1000 字符——归一化后计长）。

    Args:
        content: 原始正文。

    Returns:
        归一化后的正文。

    Raises:
        BusinessException: 归一化后为空 / 超长（``VALIDATION_ERROR``）。
    """
    normalized = normalize_fact_content(content)
    if not normalized or len(normalized) > FACT_CONTENT_MAX_LENGTH:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="人物事实正文须为 1-1000 字符（首尾空白与连续空白不计入重复）",
            details={"length": len(normalized)},
        )
    return normalized


def _merge_evidence(
    existing: Sequence[FactEvidenceRefModel],
    additions: Sequence[FactEvidenceRefModel],
) -> list[FactEvidenceRefModel]:
    """合并证据引用（去重键 = ``(episode_id, quote)``——追加合并语义）。

    Args:
        existing: 既有证据（保持原顺序）。
        additions: 追加证据（同键已存在则跳过）。

    Returns:
        合并后的证据列表（既有在前）。
    """
    merged: list[FactEvidenceRefModel] = list(existing)
    seen = {(item.episode_id, item.quote) for item in merged}
    for item in additions:
        key = (item.episode_id, item.quote)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


class FactsService:
    """人物事实服务（结构化事实 CRUD + 更正链 + compose 注入文本读取）。

    构造注入：``memory_service``（None = 现场构造无状态默认实例——证据
    校验的情景事件读取面）；``clock``（None = 当前真实时间——更正说明的
    时间戳，测试注入可控）。服务自身无状态（Session 逐次注入），可安全
    跨请求共享。
    """

    def __init__(
        self,
        *,
        memory_service: MemoryService | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            memory_service: 记忆域服务（证据引用的事件存在性校验面；None =
                现场构造默认实例——无状态共享安全）。
            clock: 注入时钟（epoch 毫秒；None = 当前真实时间——更正说明
                时间戳的参考轴，测试可控）。
        """
        self._memory = memory_service if memory_service is not None else MemoryService()
        self._clock = clock if clock is not None else time_utils.now_ms

    # ------------------------------------------------------------------
    # 写入面——创建 / 元数据刷新 / 更正 / 驳回 / 删除
    # ------------------------------------------------------------------

    async def upsert_fact(
        self,
        session: AsyncSession,
        dto: FactCreateDTO,
        *,
        provenance: MemoryProvenance = MemoryProvenance.OWNER,
        source: PersonaFactSource = PersonaFactSource.MANUAL,
    ) -> FactUpsertResultModel:
        """创建事实（同身份现行事实已存在时按元数据刷新收敛）。

        写入口径：正文归一化 → sha256 身份指纹 → 同 ``(category, hash)``
        现行行存在则刷新（置信度取最大值 + 证据追加合并，不推进版本）；
        不存在则插入首版现行行（``version=1``）。所有权（provenance）与
        来源（source）的配对由服务层强制校验。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 创建入参（类别 / 正文 / 置信度 / 可选证据）。
            provenance: 来源可信度（``dream`` 须 ``agent``；``manual`` /
                ``correction`` 须 ``owner``）。
            source: 事实来源（缺省 ``manual``——管理面手工录入面）。

        Returns:
            写入结果（记录 + 是否新建行——梦境统计的 created / merged
            判定输入）。

        Raises:
            BusinessException: 正文边界非法 / provenance 与 source 不配对 /
                provenance 为 untrusted（``VALIDATION_ERROR``）；并发唯一键
                冲突重试后仍失败（``INTERNAL_ERROR``）。
        """
        self._require_provenance_pairing(provenance=provenance, source=source)
        content = _validate_content(dto.content)
        evidence = await self._validated_evidence(session, dto.evidence)
        try:
            return await self._upsert_once(
                session,
                category=dto.category,
                content=content,
                confidence=dto.confidence,
                evidence=evidence,
                provenance=provenance,
                source=source,
                demote=None,
            )
        except IntegrityError:
            await self._rollback_quietly(session)
            logger.info(
                "人物事实并发单 active 冲突，重读现行行后重试: category={}", dto.category.value
            )
        try:
            return await self._upsert_once(
                session,
                category=dto.category,
                content=content,
                confidence=dto.confidence,
                evidence=evidence,
                provenance=provenance,
                source=source,
                demote=None,
            )
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="人物事实并发写入冲突（重试后仍冲突）",
                details={"category": dto.category.value},
            ) from exc

    async def correct_fact(
        self, session: AsyncSession, fact_id: int, dto: FactCorrectDTO
    ) -> FactRecordDTO:
        """主人更正事实（新版本现行行 + 旧行 superseded + 证据保留追加）。

        更正语义（锁定）：新正文成为现行行（``version`` = 旧行 + 1，来源
        ``correction`` / 可信度 ``owner``）；旧行全部证据保留并追加更正
        说明（``quote`` 形如「主人纠正：…」——最多 200 字符）；旧行标记
        ``superseded`` 并链接 ``superseded_by_id``。正文归一化后与旧行同
        身份时不产生新版本（元数据刷新 + 更正说明追加——文档化边界）；若
        更正回更早的正文（目标身份已有其它现行行），证据合并到该现行行并
        直接把旧行置为 superseded（同样不新起版本——同身份单现行行不变式）。
        并发面：同身份写入者先抢到活动标记时提交撞唯一键 → 回滚后
        重读目标行并**单次**重试（重试会走「目标身份已有现行行」的合并分支
        收敛）；重试仍冲突按 ``INTERNAL_ERROR`` 上抛。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            fact_id: 既有现行事实主键。
            dto: 更正入参（新正文 / 可选新置信度——None 保留原值）。

        Returns:
            更正后的现行事实记录。

        Raises:
            BusinessException: 事实不存在（``NOT_FOUND``）；非现行状态 /
                正文边界非法（``VALIDATION_ERROR``）；并发抢活动标记冲突
                重试后仍失败（``INTERNAL_ERROR``）。
        """
        row = await self._require_fact(session, fact_id)
        if row.status != PersonaFactStatus.ACTIVE.value:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="只有现行事实可更正（历史版本 / 已驳回事实不可更正）",
                details={"id": fact_id, "status": row.status},
            )
        content = _validate_content(dto.content)
        now_ms = self._clock()
        prior_evidence = [FactEvidenceRefModel.model_validate(item) for item in row.evidence]
        correction_note = FactEvidenceRefModel(
            episode_id=None,
            quote=(f"{_CORRECTION_QUOTE_PREFIX}{content}")[:FACT_EVIDENCE_QUOTE_MAX_LENGTH],
            ts_ms=now_ms,
            provenance=MemoryProvenance.OWNER,
        )
        confidence = dto.confidence if dto.confidence is not None else row.confidence
        evidence = _merge_evidence(prior_evidence, [correction_note])
        category = PersonaFactCategory(row.category)
        try:
            result = await self._upsert_once(
                session,
                category=category,
                content=content,
                confidence=confidence,
                evidence=evidence,
                provenance=MemoryProvenance.OWNER,
                source=PersonaFactSource.CORRECTION,
                demote=row,
            )
        except IntegrityError:
            await self._rollback_quietly(session)
            logger.info(
                "人物事实更正并发抢活动标记冲突，重读行后重试: id={}, category={}",
                fact_id,
                category.value,
            )
            row = await self._require_fact(session, fact_id)  # 回滚已使行对象过期，重读
        try:
            result = await self._upsert_once(
                session,
                category=category,
                content=content,
                confidence=confidence,
                evidence=evidence,
                provenance=MemoryProvenance.OWNER,
                source=PersonaFactSource.CORRECTION,
                demote=row,
            )
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="人物事实并发写入冲突（重试后仍冲突）",
                details={"id": fact_id, "category": category.value},
            ) from exc
        logger.info(
            "人物事实已更正: old_id={}, new_id={}, version={}",
            fact_id,
            result.record.id,
            result.record.version,
        )
        return result.record

    async def reject_fact(self, session: AsyncSession, fact_id: int) -> FactRecordDTO:
        """驳回事实（active → rejected；守卫标记清空——同身份可重新起链）。

        幂等语义：已驳回的事实再次驳回为零变更成功返回（面板重复操作 /
        重试安全）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            fact_id: 事实主键。

        Returns:
            驳回后的事实记录（已驳回时原样返回）。

        Raises:
            BusinessException: 事实不存在（``NOT_FOUND``）；非现行 / 非已
                驳回状态（``VALIDATION_ERROR``——历史版本无驳回语义）。
        """
        row = await self._require_fact(session, fact_id)
        if row.status == PersonaFactStatus.REJECTED.value:
            return _record(row)
        if row.status != PersonaFactStatus.ACTIVE.value:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="只有现行事实可驳回（历史版本无驳回语义）",
                details={"id": fact_id, "status": row.status},
            )
        row.status = PersonaFactStatus.REJECTED.value
        row.active_marker = None
        await self._commit(session)
        logger.info("人物事实已驳回: id={}, category={}", fact_id, row.category)
        return _record(row)

    async def delete_fact(self, session: AsyncSession, fact_id: int) -> None:
        """硬删除事实（管理面显式动作——证据链随之消失，不可恢复）。

        删除前清空指向本行的 ``superseded_by_id`` 引用（历史行不再悬挂
        到已删除主键——读面链链接保持自洽；MySQL 侧 FK ON DELETE SET NULL
        为直删 SQL 的兜底）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            fact_id: 事实主键。

        Raises:
            BusinessException: 事实不存在（``NOT_FOUND``）。
        """
        row = await self._require_fact(session, fact_id)
        await session.execute(
            update(PersonaFactRow)
            .where(PersonaFactRow.superseded_by_id == fact_id)
            .values(superseded_by_id=None)
        )
        await session.delete(row)
        await self._commit(session)
        logger.info("人物事实已删除: id={}", fact_id)

    # ------------------------------------------------------------------
    # 读取面——列表 / compose 注入
    # ------------------------------------------------------------------

    async def list_facts(
        self, session: AsyncSession, query: FactQueryDTO
    ) -> PageResponse[FactRecordDTO]:
        """事实游标分页列表（置信度降序——高可信事实在前）。

        游标语义：不透明游标承载 ``(confidence, id)`` 复合位置（浮点置信
        度同分时按 id 降序继续），跨页不重不漏。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询入参（类别 / 状态过滤 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有下一页；total 恒 -1）。
        """
        stmt = select(PersonaFactRow).where(PersonaFactRow.status == query.status.value)
        if query.category is not None:
            stmt = stmt.where(PersonaFactRow.category == query.category.value)
        cursor_payload = decode_cursor(query.cursor)
        if cursor_payload is not None:
            cursor_confidence = cursor_payload.get("confidence")
            cursor_id = cursor_payload.get("id")
            if isinstance(cursor_confidence, (int, float)) and isinstance(cursor_id, int):
                stmt = stmt.where(
                    or_(
                        PersonaFactRow.confidence < float(cursor_confidence),
                        and_(
                            PersonaFactRow.confidence == float(cursor_confidence),
                            PersonaFactRow.id < cursor_id,
                        ),
                    )
                )
        stmt = stmt.order_by(desc(PersonaFactRow.confidence), desc(PersonaFactRow.id)).limit(
            query.limit + 1
        )
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = (
            encode_cursor({"confidence": page_rows[-1].confidence, "id": page_rows[-1].id})
            if has_more and page_rows
            else None
        )
        return PageResponse.of_cursor(
            items=[_record(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def top_facts_for_compose(
        self,
        session: AsyncSession,
        *,
        limit: int = FACT_COMPOSE_MAX_FACTS,
        max_chars: int = FACT_COMPOSE_MAX_CHARS,
    ) -> list[str]:
        """读取 compose 注入行（现行高置信事实——条数 / 字符双帽裁剪）。

        渲染语义（锁定）：每行 ``【类别标签】正文``（类别标签见
        :data:`~packages.harness.persona.facts_models.FACT_CATEGORY_LABELS`）；
        按置信度降序取前 ``limit`` 条，累计字符（含换行分隔）超出
        ``max_chars`` 即停止（后续低置信事实不进注入块——列表面仍可见）。
        置信度同分按 id 升序（先录入者优先——确定性排序）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            limit: 条数上限（缺省 12——compose 渲染帽）。
            max_chars: 总字符上限（缺省 1500——含类别标签与行符号）。

        Returns:
            注入行列表（空列表 = 无现行事实——调用方跳过整块）。
        """
        stmt = (
            select(PersonaFactRow)
            .where(PersonaFactRow.status == PersonaFactStatus.ACTIVE.value)
            .order_by(desc(PersonaFactRow.confidence), PersonaFactRow.id.asc())
            .limit(max(limit, 1))
        )
        rows = list((await session.execute(stmt)).scalars().all())
        lines: list[str] = []
        used_chars = 0
        for row in rows:
            label = FACT_CATEGORY_LABELS[PersonaFactCategory(row.category)]
            line = f"【{label}】{row.content}"
            added = len(line) + (1 if lines else 0)
            if used_chars + added > max_chars:
                break
            lines.append(line)
            used_chars += added
        return lines

    # ------------------------------------------------------------------
    # 内部——写入执行 / 证据校验 / 行定位 / 事务
    # ------------------------------------------------------------------

    async def _upsert_once(
        self,
        session: AsyncSession,
        *,
        category: PersonaFactCategory,
        content: str,
        confidence: float,
        evidence: Sequence[FactEvidenceRefModel],
        provenance: MemoryProvenance,
        source: PersonaFactSource,
        demote: PersonaFactRow | None,
    ) -> FactUpsertResultModel:
        """单次写入尝试（同身份收敛 / 新建 / 更正取代——无重试编排）。

        语句序（确定性——腾位后占位）：更正路径先以 NULL 标记插入新行
        （取主键）→ 旧行解除标记 + superseded + 链接（flush）→ 新行取得
        active 标记；同身份现行行存在时只做元数据刷新（置信度最大值 +
        证据合并）。

        Args:
            session: 注入的异步 Session。
            category: 事实类别。
            content: 已归一化正文。
            confidence: 置信度（0-1）。
            evidence: 已校验证据列表。
            provenance: 来源可信度。
            source: 事实来源。
            demote: 被取代的现行行（更正路径——None = 普通写入）。

        Returns:
            写入结果（记录 + 是否新建行）。

        Raises:
            IntegrityError: 并发双写撞 ``uq_ff_persona_facts_active``（由
                公共入口重试收敛）。
        """
        normalized_hash = fact_content_hash(content)
        existing = await self._find_active(session, category, normalized_hash)
        if existing is not None:
            refreshed = _merge_evidence(
                [FactEvidenceRefModel.model_validate(item) for item in existing.evidence],
                evidence,
            )
            existing.confidence = max(existing.confidence, confidence)
            existing.evidence = [item.model_dump(mode="json") for item in refreshed]
            if demote is not None and demote.id != existing.id:
                demote.status = PersonaFactStatus.SUPERSEDED.value
                demote.active_marker = None
                demote.superseded_by_id = existing.id
            await self._commit(session)
            return FactUpsertResultModel(record=_record(existing), created=False)
        new_version = demote.version + 1 if demote is not None else 1
        row = PersonaFactRow(
            category=category.value,
            content=content,
            normalized_hash=normalized_hash,
            confidence=confidence,
            status=PersonaFactStatus.ACTIVE.value,
            provenance=provenance.value,
            source=source.value,
            evidence=[item.model_dump(mode="json") for item in evidence],
            superseded_by_id=None,
            version=new_version,
            active_marker=FACT_ACTIVE_MARKER if demote is None else None,
        )
        session.add(row)
        if demote is not None:
            await session.flush()  # 取新行主键——旧行链接需要
            demote.status = PersonaFactStatus.SUPERSEDED.value
            demote.superseded_by_id = row.id
            demote.active_marker = None
            await session.flush()  # 旧行先解除 active 标记——唯一键腾位
            row.active_marker = FACT_ACTIVE_MARKER
        await self._commit(session)
        return FactUpsertResultModel(record=_record(row), created=True)

    async def _find_active(
        self, session: AsyncSession, category: PersonaFactCategory, normalized_hash: str
    ) -> PersonaFactRow | None:
        """取同身份的现行事实行（``active_marker='active'``——唯一键身份轴）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            category: 事实类别。
            normalized_hash: 正文归一化指纹。

        Returns:
            现行事实行；不存在为 None。
        """
        return (
            (
                await session.execute(
                    select(PersonaFactRow).where(
                        PersonaFactRow.category == category.value,
                        PersonaFactRow.normalized_hash == normalized_hash,
                        PersonaFactRow.active_marker == FACT_ACTIVE_MARKER,
                    )
                )
            )
            .scalars()
            .first()
        )

    async def _validated_evidence(
        self, session: AsyncSession, refs: Sequence[FactEvidenceRefModel]
    ) -> list[FactEvidenceRefModel]:
        """校验证据引用（事件存在性 + 人格作用域——失败丢弃并告警）。

        校验口径：``episode_id`` 为 None 的手工证据直接保留；带事件回链
        的证据逐条经 ``MemoryService.get_episode`` 读取（人格作用域外 /
        缺失 → ``NOT_FOUND``）——失败按「丢弃该条证据 + 告警」处理，
        **不**阻断事实写入（证据是增强面）。重复引用（同事件同摘录）去重。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            refs: 待校验证据引用列表。

        Returns:
            通过校验的证据列表（保持原顺序；不可信来源的证据剔除）。
        """
        validated: list[FactEvidenceRefModel] = []
        for ref in refs:
            if ref.provenance == MemoryProvenance.UNTRUSTED:
                logger.warning(
                    "人物事实证据来源为 untrusted，已丢弃: episode_id={}", ref.episode_id
                )
                continue
            if ref.episode_id is not None:
                try:
                    await self._memory.get_episode(session, ref.episode_id)
                except BusinessException:
                    logger.warning(
                        "人物事实证据事件不存在或不在本作用域，已丢弃该条证据: episode_id={}",
                        ref.episode_id,
                    )
                    continue
            validated.append(ref)
        return _merge_evidence([], validated)

    @staticmethod
    def _require_provenance_pairing(
        *, provenance: MemoryProvenance, source: PersonaFactSource
    ) -> None:
        """校验来源与可信度的配对（梦境=agent / 手工与更正=owner）。

        Args:
            provenance: 来源可信度。
            source: 事实来源。

        Raises:
            BusinessException: untrusted / 非配对组合（``VALIDATION_ERROR``）。
        """
        if provenance == MemoryProvenance.UNTRUSTED:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="untrusted 来源的内容禁止进入人物事实",
                details={"source": source.value},
            )
        expected = (
            MemoryProvenance.AGENT if source is PersonaFactSource.DREAM else MemoryProvenance.OWNER
        )
        if provenance != expected:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="人物事实的 provenance 与 source 不配对",
                details={
                    "source": source.value,
                    "provenance": provenance.value,
                    "expected": expected.value,
                },
            )

    async def _require_fact(self, session: AsyncSession, fact_id: int) -> PersonaFactRow:
        """按主键取事实行（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            fact_id: 事实主键。

        Returns:
            事实行。

        Raises:
            BusinessException: 事实不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = (
            (await session.execute(select(PersonaFactRow).where(PersonaFactRow.id == fact_id)))
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="人物事实不存在",
                details={"id": fact_id},
            )
        return row

    @staticmethod
    async def _commit(session: AsyncSession) -> None:
        """提交事务（失败回滚后原样上抛——写路径统一出口）。

        Args:
            session: 注入的异步 Session。

        Raises:
            Exception: 提交失败（已回滚，由上层异常处理器接面）。
        """
        try:
            await session.commit()
        except Exception:
            await FactsService._rollback_quietly(session)
            raise

    @staticmethod
    async def _rollback_quietly(session: AsyncSession) -> None:
        """尽力回滚（异常路径清理会话待处理态——不注入新异常）。

        Args:
            session: 待回滚的异步 Session。
        """
        try:
            await session.rollback()
        except Exception:  # best-effort: 回滚失败由会话关闭兜底
            logger.opt(exception=True).debug("人物事实路径会话回滚失败（close 兜底）")

    def evidence_text(self, record: EpisodeRecordDTO) -> str:
        """事件载荷 → 证据摘录文本（复用检索层的取段规则）。

        Args:
            record: 情景事件记录。

        Returns:
            逐字摘录（≤200 字符——超长按字符截断；无文本载荷为空串）。
        """
        try:
            text = flatten_episode_content(record.content)
        except BusinessException:
            return ""
        return text[:FACT_EVIDENCE_QUOTE_MAX_LENGTH]
