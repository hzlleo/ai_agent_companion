"""persona 域技能服务——程序性技能的 CRUD / compose 匹配 / 梦境合并面。

职责链（管理面 API / 面板 / 夜间梦境 / 对话组装共用同一服务）::

    create(session, dto)                    手工创建（slug 由名称派生）
    update(session, id, dto)                原地编辑（version 递增，无历史链）
    set_status(session, id, status)         启用 / 停用（面板状态切换）
    delete(session, id)                     硬删除（管理面显式动作）
    list_skills(session, query)             游标分页（状态过滤、使用次数降序）
    match_for_compose(session, user_text)   compose 命中面（字面短语匹配 + 强化）
    active_skill_refs(session)              现行技能清单（梦境提案的去重参考）
    upsert_mined(session, dto)              梦境写入（同 slug 元数据合并 / 新建）

写入语义（锁定）：

    - **身份 = (persona_id, slug)**：slug 由技能名归一化派生（NFKC 小写，
      非单词字符折叠为 ``-``，连续 ``-`` 去重，截断到 80 字符）——同一
      人格下同名技能指向同一行；手工创建撞已有 slug 即 ``CONFLICT``
      （拒绝静默覆盖：同名的两种含义应由主人决定改名或编辑）；
    - **原地版本**：编辑（``update``）推进 ``version``，不产生历史行——
      技能是操作指令，旧版文本无回溯语义价值（详见 ``skills_orm``）；
    - **mined 行与手工行同权**：``source=mined`` 只标记来源，编辑 / 停用 /
      删除等主人动作对两类行完全一致——挖矿产出不是不可变事实；
    - **梦境写入走元数据合并**：同 slug 既有行只做「置信度取最大值 +
      触发短语并集（上限 8 条）」刷新，不推进版本、不改正文 / 名称 /
      状态——重复挖到同一技能不应膨胀版本，主人停用的技能也不会被梦境
      悄悄重新启用。

compose 匹配面（:meth:`SkillsService.match_for_compose`）：

    - 候选集 = 现行 ``active`` 技能（状态过滤在查询层，扫描上限
      :data:`SKILL_MATCH_SCAN_LIMIT`）；
    - 命中判定 = **字面包含**：任一触发短语（NFKC + casefold 后）出现在
      本轮用户文本中；或描述关键词（描述按非单词字符切分后的 2-40 字符
      片段、最多 12 个、去重）任一出现在用户文本中——描述匹配是弱轴，
      触发短语是主轴；
    - 排序 = ``use_count`` 降序、同名按名称升序（确定性），取前
      :data:`~packages.harness.persona.skills_models.SKILL_COMPOSE_MAX_SKILLS`
      条；条数 / 字符帽的渲染侧裁剪在聊天域（
      ``render_skills_block``）；
    - **写读强化（fire-and-forget）**：命中后调度后台任务把命中行
      ``use_count + 1`` / ``last_used_ms = 当前时间``。计数失败只告警、
      不影响回复（强化是排序增强面而非正确性面）；调度面可经构造注入
      替换（测试注入同步执行器即得确定性）。

人格作用域：与记忆域同轴（PersonaMixin 自动注入 / 过滤）——调用方须先
经 ``bind_persona_context`` 绑定上下文，否则 fail loud。
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from loguru import logger
from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core import time_utils
from packages.harness.core.background_tasks import background_task_manager
from packages.harness.core.context import persona_id_var
from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import session_scope
from packages.harness.persona.service import bind_persona_context
from packages.harness.persona.skills_models import (
    SKILL_COMPOSE_MAX_SKILLS,
    SKILL_SLUG_MAX_LENGTH,
    SKILL_TRIGGERS_MAX,
    SkillCreateModel,
    SkillQueryDTO,
    SkillRecordDTO,
    SkillSource,
    SkillStatus,
    SkillUpdateModel,
    SkillUpsertResultModel,
)
from packages.harness.persona.skills_orm import PersonaSkillRow

__all__ = [
    "SKILL_MATCH_SCAN_LIMIT",
    "normalize_skill_slug",
    "SkillsService",
]

#: compose 匹配的候选扫描上限（现行技能全量评估的硬帽——防异常数据量
#: 把单轮回复的匹配面拖成全表扫描）。
SKILL_MATCH_SCAN_LIMIT: int = 500

#: 描述关键词的最大提取数（弱匹配轴的确定性上限）。
_DESCRIPTION_KEYWORD_LIMIT: int = 12

#: 描述关键词的长度区间（过短噪声大、过长几乎不可能被用户文本包含）。
_DESCRIPTION_KEYWORD_MIN_LENGTH: int = 2
_DESCRIPTION_KEYWORD_MAX_LENGTH: int = 40

#: 非单词字符切分正则（描述关键词提取——Unicode 单词字符之外均为分隔）。
_NON_WORD_PATTERN = re.compile(r"[^\w]+")

#: 连续连字符折叠正则（slug 归一化）。
_DASH_RUN_PATTERN = re.compile(r"-{2,}")

#: 使用计数写入调度器形态（接收待执行协程；缺省实现注册为后台任务）。
UsageWriteScheduler = Callable[[Coroutine[Any, Any, None]], None]


def normalize_skill_slug(name: str) -> str:
    """由技能名派生 slug（NFKC 小写 + 非单词字符折叠 + 连续连字符去重）。

    派生规则：NFKC 归一 → 首尾裁剪 → 小写 → 非 Unicode 单词字符折叠为
    ``-`` → 连续 ``-`` 去重 → 去首尾 ``-`` → 截断到
    :data:`~packages.harness.persona.skills_models.SKILL_SLUG_MAX_LENGTH`。
    CJK 字符属 Unicode 单词字符，故中文名会得到中文 slug（同人格内唯一
    即可，无需音译）。

    Args:
        name: 技能名（调用方已通过 1-120 字符边界校验）。

    Returns:
        归一化 slug（空串表示名称不含任何单词字符——调用方须拒绝）。

    Examples:
        >>> normalize_skill_slug("Weekly Report")
        'weekly-report'
        >>> normalize_skill_slug("周报流程（简版）")
        '周报流程-简版'
    """
    normalized = unicodedata.normalize("NFKC", name).strip().lower()
    slug = _NON_WORD_PATTERN.sub("-", normalized)
    slug = _DASH_RUN_PATTERN.sub("-", slug).strip("-")
    return slug[:SKILL_SLUG_MAX_LENGTH].rstrip("-")


def _normalize_match_text(text: str) -> str:
    """匹配用文本归一（NFKC + casefold——字面包含判定的统一形态）。

    Args:
        text: 原始文本（用户输入 / 触发短语 / 描述）。

    Returns:
        归一化文本（空串表示原文无有效内容）。
    """
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _normalize_triggers(triggers: Sequence[str]) -> list[str]:
    """触发短语归一（裁剪 + 去重 + 保原顺序——同形异写视为重复）。

    Args:
        triggers: 原始触发短语（DTO 边界已限定条数与单条长度）。

    Returns:
        去重后的触发短语列表（按 casefold 形态判重；保持首次出现顺序）。

    Raises:
        BusinessException: 归一后为空 / 超出条数上限
            （``VALIDATION_ERROR``——调用方可能绕过 DTO 直接构造）。
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for trigger in triggers:
        stripped = unicodedata.normalize("NFKC", trigger).strip()
        if not stripped:
            continue
        key = stripped.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(stripped)
    if not normalized or len(normalized) > SKILL_TRIGGERS_MAX:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"技能触发短语须为 1-{SKILL_TRIGGERS_MAX} 条非空短语",
            details={"count": len(normalized)},
        )
    return normalized


def _description_keywords(description: str) -> list[str]:
    """描述关键词提取（弱匹配轴——确定性切分与上限）。

    规则（文档化于模块 docstring）：描述按非单词字符切分，保留 2-40
    字符片段，按 casefold 去重，最多 :data:`_DESCRIPTION_KEYWORD_LIMIT`
    个。中文描述无空格时整段可能成为单个片段——此时该轴自然退化（触发
    短语仍是主匹配轴）。

    Args:
        description: 触发描述文本。

    Returns:
        关键词列表（空列表表示描述无可用于弱匹配的片段）。
    """
    keywords: list[str] = []
    seen: set[str] = set()
    for chunk in _NON_WORD_PATTERN.split(unicodedata.normalize("NFKC", description)):
        keyword = chunk.strip().casefold()
        if not (
            _DESCRIPTION_KEYWORD_MIN_LENGTH <= len(keyword) <= _DESCRIPTION_KEYWORD_MAX_LENGTH
        ):
            continue
        if keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(keyword)
        if len(keywords) >= _DESCRIPTION_KEYWORD_LIMIT:
            break
    return keywords


def _record(row: PersonaSkillRow) -> SkillRecordDTO:
    """技能行 → 读取 DTO（触发短语 JSON 反序列化为字符串列表）。

    Args:
        row: 技能行。

    Returns:
        技能记录 DTO。
    """
    return SkillRecordDTO(
        id=row.id,
        name=row.name,
        slug=row.slug,
        description=row.description,
        triggers=[str(item) for item in row.triggers],
        content=row.content,
        status=SkillStatus(row.status),
        source=SkillSource(row.source),
        confidence=row.confidence,
        use_count=row.use_count,
        last_used_ms=row.last_used_ms,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SkillsService:
    """技能服务（CRUD + compose 匹配 + 写读强化 + 梦境合并写入）。

    构造注入：``clock``（None = 当前真实时间——强化时间戳，测试可控）；
    ``usage_write_scheduler``（None = 注册为进程后台任务——fire-and-forget
    的缺省实现；测试注入同步执行器或空执行器可获得确定性）。服务自身
    无状态（Session 逐次注入），可安全跨请求共享。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int] | None = None,
        usage_write_scheduler: UsageWriteScheduler | None = None,
    ) -> None:
        """初始化服务。

        Args:
            clock: 注入时钟（epoch 毫秒；None = 当前真实时间——命中计数的
                时间戳参考轴）。
            usage_write_scheduler: 使用计数写入调度器（None = 经
                ``asyncio.create_task`` + ``background_task_manager`` 注册
                后台任务；注入面供测试以确定性方式执行协程）。
        """
        self._clock = clock if clock is not None else time_utils.now_ms
        self._schedule_usage_write = (
            usage_write_scheduler
            if usage_write_scheduler is not None
            else self._schedule_background_usage_write
        )

    # ------------------------------------------------------------------
    # 写入面——创建 / 编辑 / 状态 / 删除 / 梦境合并
    # ------------------------------------------------------------------

    async def create(self, session: AsyncSession, dto: SkillCreateModel) -> SkillRecordDTO:
        """手工创建技能（slug 冲突即拒绝——不静默覆盖）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 创建入参（名称 / 描述 / 触发短语 / 正文 / 置信度）。

        Returns:
            新建的技能记录（source=manual / status=active / version=1）。

        Raises:
            BusinessException: 名称不含可派生字符（``VALIDATION_ERROR``）；
                同人格下 slug 已存在（``CONFLICT``）。
        """
        slug = self._require_slug(dto.name)
        if await self._find_by_slug(session, slug) is not None:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="同人格下已存在同名技能（slug 冲突），请改名或直接编辑该技能",
                details={"slug": slug},
            )
        row = PersonaSkillRow(
            name=dto.name.strip(),
            slug=slug,
            description=dto.description.strip(),
            triggers=_normalize_triggers(dto.triggers),
            content=dto.content,
            status=SkillStatus.ACTIVE.value,
            source=SkillSource.MANUAL.value,
            confidence=dto.confidence,
            use_count=0,
            last_used_ms=None,
            version=1,
        )
        session.add(row)
        try:
            await self._commit(session)
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="同人格下已存在同名技能（并发创建冲突），请改名或直接编辑该技能",
                details={"slug": slug},
            ) from exc
        logger.info("技能已创建: id={}, slug={}", row.id, slug)
        return _record(row)

    async def update(
        self, session: AsyncSession, skill_id: int, dto: SkillUpdateModel
    ) -> SkillRecordDTO:
        """原地编辑技能（``version`` 递增——不产生历史行）。

        提供 ``name`` 时重新派生 slug 并校验冲突（与他人格内其他技能
        重名 → ``CONFLICT``）；提供 ``triggers`` 时整集替换（去重后
        落库）。未提供的字段保持不变。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            skill_id: 技能主键。
            dto: 编辑入参（字段全部可选——None 表示保留）。

        Returns:
            编辑后的技能记录（version 已递增）。

        Raises:
            BusinessException: 技能不存在（``NOT_FOUND``）；新名称无法
                派生 slug / 触发短语为空（``VALIDATION_ERROR``）；新
                slug 与他人格内既有技能冲突（``CONFLICT``）。
        """
        row = await self._require_skill(session, skill_id)
        if dto.name is not None:
            new_slug = self._require_slug(dto.name)
            if new_slug != row.slug:
                existing = await self._find_by_slug(session, new_slug)
                if existing is not None and existing.id != row.id:
                    raise BusinessException(
                        code=ErrorCode.CONFLICT,
                        message="同人格下已存在同名技能（slug 冲突），请换一个名称",
                        details={"slug": new_slug},
                    )
            row.name = dto.name.strip()
            row.slug = new_slug
        if dto.description is not None:
            row.description = dto.description.strip()
        if dto.triggers is not None:
            row.triggers = _normalize_triggers(dto.triggers)
        if dto.content is not None:
            row.content = dto.content
        if dto.confidence is not None:
            row.confidence = dto.confidence
        row.version += 1
        try:
            await self._commit(session)
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="同人格下已存在同名技能（并发编辑冲突），请换一个名称",
                details={"id": skill_id},
            ) from exc
        logger.info("技能已编辑: id={}, version={}", skill_id, row.version)
        return _record(row)

    async def set_status(
        self, session: AsyncSession, skill_id: int, status: SkillStatus
    ) -> SkillRecordDTO:
        """切换技能状态（启用 / 停用——幂等同值调用为零变更成功）。

        停用不删除、不推进版本：主人随时可重新启用；停用期间该技能不
        参与 compose 匹配（状态过滤在查询层）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            skill_id: 技能主键。
            status: 目标状态（active / disabled）。

        Returns:
            状态切换后的技能记录（同值调用原样返回）。

        Raises:
            BusinessException: 技能不存在（``NOT_FOUND``）。
        """
        row = await self._require_skill(session, skill_id)
        if row.status == status.value:
            return _record(row)
        row.status = status.value
        await self._commit(session)
        logger.info("技能状态已切换: id={}, status={}", skill_id, status.value)
        return _record(row)

    async def delete(self, session: AsyncSession, skill_id: int) -> None:
        """硬删除技能（管理面显式动作——不可恢复）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            skill_id: 技能主键。

        Raises:
            BusinessException: 技能不存在（``NOT_FOUND``）。
        """
        row = await self._require_skill(session, skill_id)
        await session.delete(row)
        await self._commit(session)
        logger.info("技能已删除: id={}, slug={}", skill_id, row.slug)

    async def upsert_mined(
        self, session: AsyncSession, dto: SkillCreateModel
    ) -> SkillUpsertResultModel:
        """梦境写入（同 slug 元数据合并——不推进版本、不改正文 / 状态）。

        合并口径（锁定）：既有行（任何状态）只刷新 ``confidence =
        max(旧, 新)`` 与触发短语并集（去重后上限 8 条）；名称 / 描述 /
        正文 / 状态 / 版本保持不变——重复挖到同一技能是元数据刷新而非
        编辑，主人停用的技能也不会被梦境悄悄重新启用。无既有行时新建
        ``source=mined`` / ``status=active`` / ``version=1`` 行。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 提案入参（名称 / 描述 / 触发短语 / 正文 / 置信度）。

        Returns:
            写入结果（记录 + created / merged 判定）。

        Raises:
            BusinessException: 名称无法派生 slug / 触发短语为空
                （``VALIDATION_ERROR``）；并发创建撞唯一键后重试仍失败
                （``INTERNAL_ERROR``）。
        """
        slug = self._require_slug(dto.name)
        triggers = _normalize_triggers(dto.triggers)
        try:
            return await self._upsert_mined_once(
                session, slug=slug, dto=dto, triggers=triggers
            )
        except IntegrityError:
            await self._rollback_quietly(session)
            logger.info("技能挖矿写入并发冲突，重读既有行后按合并处理: slug={}", slug)
        try:
            return await self._upsert_mined_once(
                session, slug=slug, dto=dto, triggers=triggers
            )
        except IntegrityError as exc:
            await self._rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="技能挖矿写入并发冲突（重试后仍冲突）",
                details={"slug": slug},
            ) from exc

    async def _upsert_mined_once(
        self,
        session: AsyncSession,
        *,
        slug: str,
        dto: SkillCreateModel,
        triggers: Sequence[str],
    ) -> SkillUpsertResultModel:
        """单次梦境写入尝试（合并既有行 / 新建——无重试编排）。

        Args:
            session: 注入的异步 Session。
            slug: 归一化标识。
            dto: 提案入参。
            triggers: 已归一化的触发短语。

        Returns:
            写入结果（记录 + created / merged 判定）。

        Raises:
            IntegrityError: 并发双写撞 ``uq_ff_persona_skills_slug``
                （由公共入口重试收敛）。
        """
        existing = await self._find_by_slug(session, slug)
        if existing is not None:
            merged_triggers = self._merge_triggers(existing.triggers, triggers)
            existing.confidence = max(existing.confidence, dto.confidence)
            existing.triggers = merged_triggers
            await self._commit(session)
            logger.info(
                "技能挖矿并入既有行（元数据刷新）: id={}, slug={}", existing.id, slug
            )
            return SkillUpsertResultModel(record=_record(existing), created=False, merged=True)
        row = PersonaSkillRow(
            name=dto.name.strip(),
            slug=slug,
            description=dto.description.strip(),
            triggers=list(triggers),
            content=dto.content,
            status=SkillStatus.ACTIVE.value,
            source=SkillSource.MINED.value,
            confidence=dto.confidence,
            use_count=0,
            last_used_ms=None,
            version=1,
        )
        session.add(row)
        await self._commit(session)
        logger.info("技能挖矿新建: id={}, slug={}", row.id, slug)
        return SkillUpsertResultModel(record=_record(row), created=True, merged=False)

    @staticmethod
    def _merge_triggers(existing: Sequence[str], additions: Sequence[str]) -> list[str]:
        """触发短语并集（去重 + 上限截断——既有条目优先保留）。

        Args:
            existing: 既有触发短语。
            additions: 新增触发短语。

        Returns:
            并集列表（最多 :data:`SKILL_TRIGGERS_MAX` 条）。
        """
        merged: list[str] = []
        seen: set[str] = set()
        for trigger in (*existing, *additions):
            key = str(trigger).casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(str(trigger))
            if len(merged) >= SKILL_TRIGGERS_MAX:
                break
        return merged

    # ------------------------------------------------------------------
    # 读取面——列表 / 梦境参考清单 / compose 匹配
    # ------------------------------------------------------------------

    async def list_skills(
        self, session: AsyncSession, query: SkillQueryDTO
    ) -> PageResponse[SkillRecordDTO]:
        """技能游标分页列表（使用次数降序——高频技能在前）。

        游标语义：不透明游标承载 ``(use_count, id)`` 复合位置（同使用
        次数按 id 升序继续），跨页不重不漏。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询入参（状态过滤 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有下一页；total 恒 -1）。
        """
        stmt = select(PersonaSkillRow)
        if query.status is not None:
            stmt = stmt.where(PersonaSkillRow.status == query.status.value)
        cursor_payload = decode_cursor(query.cursor)
        if cursor_payload is not None:
            cursor_use_count = cursor_payload.get("use_count")
            cursor_id = cursor_payload.get("id")
            if isinstance(cursor_use_count, int) and isinstance(cursor_id, int):
                stmt = stmt.where(
                    or_(
                        PersonaSkillRow.use_count < cursor_use_count,
                        and_(
                            PersonaSkillRow.use_count == cursor_use_count,
                            PersonaSkillRow.id > cursor_id,
                        ),
                    )
                )
        stmt = stmt.order_by(
            desc(PersonaSkillRow.use_count), PersonaSkillRow.id.asc()
        ).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = (
            encode_cursor({"use_count": page_rows[-1].use_count, "id": page_rows[-1].id})
            if has_more and page_rows
            else None
        )
        return PageResponse.of_cursor(
            items=[_record(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def active_skill_refs(self, session: AsyncSession) -> list[SkillRecordDTO]:
        """现行技能清单（启用态、名称升序——梦境提案的去重参考面）。

        梦境 prompt 只需要名称 / slug（模板只列这两项），本方法仍返回
        完整记录以复用统一读取 DTO；调用方只消费需要的字段。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            启用态技能记录列表（名称升序；空列表 = 尚无技能）。
        """
        rows = list(
            (
                await session.execute(
                    select(PersonaSkillRow)
                    .where(PersonaSkillRow.status == SkillStatus.ACTIVE.value)
                    .order_by(PersonaSkillRow.name.asc(), PersonaSkillRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_record(row) for row in rows]

    async def match_for_compose(
        self, session: AsyncSession, user_text: str
    ) -> list[SkillRecordDTO]:
        """匹配本轮用户文本可用的技能（字面短语 / 描述关键词 + 强化调度）。

        匹配与排序语义见模块 docstring「compose 匹配面」；命中后经注入
        调度器 fire-and-forget 写入使用计数（失败只告警）。空白用户
        文本直接返回空列表（不调度计数、不产生任何 I/O）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            user_text: 本轮用户输入原文。

        Returns:
            命中技能记录列表（最多
            :data:`~packages.harness.persona.skills_models.SKILL_COMPOSE_MAX_SKILLS`
            条；空列表 = 无命中——调用方跳过整块）。
        """
        normalized_text = _normalize_match_text(user_text)
        if not normalized_text:
            return []
        rows = list(
            (
                await session.execute(
                    select(PersonaSkillRow)
                    .where(PersonaSkillRow.status == SkillStatus.ACTIVE.value)
                    .limit(SKILL_MATCH_SCAN_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        hits: list[PersonaSkillRow] = []
        for row in rows:
            if self._matches(normalized_text, row):
                hits.append(row)
        hits.sort(key=lambda row: (-row.use_count, row.name, row.id))
        selected = hits[:SKILL_COMPOSE_MAX_SKILLS]
        if selected:
            self._schedule_usage_bump([row.id for row in selected])
        return [_record(row) for row in selected]

    @staticmethod
    def _matches(normalized_user_text: str, row: PersonaSkillRow) -> bool:
        """单技能命中判定（触发短语字面包含 / 描述关键词弱匹配）。

        Args:
            normalized_user_text: 已归一化的用户文本。
            row: 候选技能行。

        Returns:
            True = 本轮文本命中该技能。
        """
        for trigger in row.triggers:
            if _normalize_match_text(str(trigger)) in normalized_user_text:
                return True
        for keyword in _description_keywords(row.description):
            if keyword in normalized_user_text:
                return True
        return False

    # ------------------------------------------------------------------
    # 内部——强化调度 / 行定位 / 事务
    # ------------------------------------------------------------------

    def _schedule_usage_bump(self, skill_ids: Sequence[int]) -> None:
        """调度使用计数写入（fire-and-forget——失败不影响回复链路）。

        人格上下文在调度时刻捕获（后台任务不依赖调用方上下文的存续）；
        缺上下文属调用方装配错误，此处只告警并跳过计数（回复优先）。

        Args:
            skill_ids: 本轮命中的技能主键列表（非空）。
        """
        persona_scope = persona_id_var.get()
        if persona_scope is None:
            logger.warning("技能命中但无人格上下文，跳过使用计数写入")
            return
        now_ms = self._clock()
        self._schedule_usage_write(
            self._bump_usage(persona_scope=persona_scope, skill_ids=list(skill_ids), now_ms=now_ms)
        )

    def _schedule_background_usage_write(self, coro: Coroutine[Any, Any, None]) -> None:
        """缺省调度实现（后台任务托管——进程关机时随管理器收敛）。

        Args:
            coro: 待执行的计数写入协程。
        """
        task = asyncio.create_task(coro, name="skill-usage-bump")
        background_task_manager.register(task)

    async def _bump_usage(
        self, *, persona_scope: str, skill_ids: Sequence[int], now_ms: int
    ) -> None:
        """写入命中计数（独立短事务——异常只告警不抛出）。

        Args:
            persona_scope: 命中时的人格作用域（显式绑定——后台任务不
                继承调用方上下文生命周期）。
            skill_ids: 命中技能主键列表。
            now_ms: 命中时刻（epoch 毫秒——``last_used_ms`` 写入值）。
        """
        try:
            async with session_scope() as session, bind_persona_context(persona_scope):
                await session.execute(
                    update(PersonaSkillRow)
                    .where(PersonaSkillRow.id.in_(list(skill_ids)))
                    .values(
                        use_count=PersonaSkillRow.use_count + 1,
                        last_used_ms=now_ms,
                    )
                )
        except Exception:  # best-effort：强化计数失败不进回复失败路径
            logger.opt(exception=True).warning(
                "技能使用计数写入失败（已吞没——不影响回复）: ids={}", list(skill_ids)
            )

    async def _find_by_slug(
        self, session: AsyncSession, slug: str
    ) -> PersonaSkillRow | None:
        """按 slug 取技能行（人格作用域内——任何状态）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            slug: 归一化标识。

        Returns:
            技能行；不存在为 None。
        """
        return (
            (
                await session.execute(
                    select(PersonaSkillRow).where(PersonaSkillRow.slug == slug)
                )
            )
            .scalars()
            .first()
        )

    async def _require_skill(self, session: AsyncSession, skill_id: int) -> PersonaSkillRow:
        """按主键取技能行（人格作用域外 / 缺失均 NOT_FOUND）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            skill_id: 技能主键。

        Returns:
            技能行。

        Raises:
            BusinessException: 技能不存在或不在当前人格作用域（``NOT_FOUND``）。
        """
        row = (
            (
                await session.execute(
                    select(PersonaSkillRow).where(PersonaSkillRow.id == skill_id)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="技能不存在",
                details={"id": skill_id},
            )
        return row

    @staticmethod
    def _require_slug(name: str) -> str:
        """派生并校验 slug（空派生结果 = 名称无有效字符）。

        Args:
            name: 技能名。

        Returns:
            归一化 slug。

        Raises:
            BusinessException: 名称不含任何可派生字符
                （``VALIDATION_ERROR``）。
        """
        slug = normalize_skill_slug(name)
        if not slug:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="技能名须至少包含一个字母、数字或文字字符（无法派生标识）",
                details={"name": name},
            )
        return slug

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
            await SkillsService._rollback_quietly(session)
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
            logger.opt(exception=True).debug("技能路径会话回滚失败（close 兜底）")
