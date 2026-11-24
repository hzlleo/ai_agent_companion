"""persona 域服务——人格注册表全生命周期管理（管理面真源）。

操作面（多激活可寻址模型——多人格同时可用，主人格唯一）：

    - 创建 / 更新 / 复制 / 删除（注册名唯一校验；复制可选深拷贝 M/S
      状态；删除有主人格守卫）；
    - **导入**（``import_persona`` / ``import_personas_batch``）：预设
      生成卡片或用户卡片文档 → 内容哈希幂等（同卡恒跳过——与策略
      无关）→ 名称冲突按策略（skip / rename / overwrite）落库；批量
      为部分成功形态（逐文件独立事务）；
    - **事务性主人格切换**：``set_primary`` 在单事务内「全表清主人格
      标记 → 目标取标记」——设 B 为主人格必然解除 A（至多一个主人格
      的不变式由 DB 唯一键 ``uq_ff_personas_primary`` 强制 + 并发冲突
      重试收敛——与策展记忆单 active 同模式）；
    - **可寻址集**：``list_addressable`` 返回全部 enabled 人格（status=
      active——通道 / 心流 / 多会话按上下文寻址的消费面）；
    - 状态层读写（M/S upsert / 读取）；
    - 游标分页列表（status 过滤）；
    - 批量删除（部分成功形态——逐 id 独立事务，单项失败不阻塞其余）。

主人格读取（``get_primary_persona``）：无主人格（全新库 / 从未设置）
**fail loud**——清晰中文错误指引先 set-primary（消费面原将「无激活」
翻译为错误，此语义由服务层统一承担）。

守卫（本工程锁定语义——两步显式，杜绝隐式状态跳变）：

    - **删除守卫**：删除当前主人格直接 ``CONFLICT`` 拒绝；调用方须先
      将另一个人格设为主人格（``set_primary``）再删除（避免「删除即
      静默掉线为零主人格」的隐式跳变）。删除非主人格时**同事务**级联
      清理其记忆域与画像域全部行（M/S 状态 / 情景 / 策展 / 梦境运行
      日志 / 人物事实 / 技能 / 核心记忆块与修订 / 会话与消息），并同步
      清理回退检索索引行（SQLite 测试方言默认不启用 FK 强制，服务层
      显式批删保证跨方言一致；DB 级 CASCADE 仅作直删 SQL 的兜底；
      SeekDB 生产索引提交后经注入 indexer best-effort 清理）。
    - **停用守卫**：停用（``disable`` / ``update(status=disabled)``）
      当前主人格同样 ``CONFLICT`` 拒绝——先另设主人格再停用（主人格
      须恒在位；与删除守卫同一两步模式）。

状态机约定：

    - ``status``（可用性）与 ``is_primary_marker``（主人格标记）正交；
      设为主人格要求 ``status=active``（停用人格不可设为主人格）；
    - ``disable`` 是纯生命周期操作（status 切 disabled；主人格守卫
      拒绝——见上）；已停用人格重复停用为幂等 no-op；
    - 复制产物恒 ``disabled + 非主人格``（启用与设为主人格均为显式
      后续动作）；导入产物同样恒 ``disabled + 非主人格``。

Session 契约：方法接收调用方注入的 :class:`AsyncSession`（app 层经
``get_async_db`` 依赖注入；测试直连共享引擎 Session）——服务层内部
显式 ``commit`` / 异常 ``rollback``（写路径事务边界归服务层，见
database.session.get_async_db 的分工约定）。
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from loguru import logger
from sqlalchemy import BigInteger, Integer, delete, desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.context import RequestContext, bind_context
from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.file_utils import unlink_missing_ok
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.database.base import PERSONA_FILTER_SKIP_OPTION, Base
from packages.harness.database.models import load_all_models
from packages.harness.memory.orm import (
    ChatMessageRow,
    ChatSessionRow,
    CuratedMemoryRow,
    MemoryEpisodeRow,
)
from packages.harness.memory.retrieval import MemoryIndexer
from packages.harness.memory.retrieval_models import MemoryRefType
from packages.harness.multimodal.sticker_orm import StickerRow
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.core_memory_orm import (
    CoreMemoryBlockRow,
    CoreMemoryRevisionRow,
)
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaBatchDeleteItemDTO,
    PersonaBatchDeleteResponseDTO,
    PersonaCardDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaImportBatchFileDTO,
    PersonaImportBatchItemDTO,
    PersonaImportBatchResultDTO,
    PersonaImportResultModel,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.facts_orm import PersonaFactRow
from packages.harness.persona.models import (
    PersonaImportConflictStrategy,
    PersonaImportReason,
    PersonaStateLayer,
    PersonaStatus,
)
from packages.harness.persona.orm import PERSONA_PRIMARY_MARKER, PersonaRow, PersonaStateRow
from packages.harness.voice.voice_models import VoiceProfileModel
from packages.harness.voice.voiceprint_orm import VoiceprintSampleRow

__all__ = [
    "PersonaService",
    "bind_persona_context",
    "canonical_card_hash",
    "PERSONA_DELETE_RETAINED_TABLES",
    "REFERENCE_SCOPED_PERSONA_TABLES",
]

#: 人格删除后**保留**的 persona_id 作用域表（跨人格聚合真源——用量账按条目 /
#: 用途 / 时间做全库聚合，人格删除后历史用量仍需可查）。
PERSONA_DELETE_RETAINED_TABLES: Final[frozenset[str]] = frozenset({"ff_usage_records"})

#: 无 persona_id 列、按归属轴（会话 / 块）显式清理的依赖表。新增同类表必须
#: 在此声明或自挂 persona_id 列（后者由元数据派生自动覆盖）。
REFERENCE_SCOPED_PERSONA_TABLES: Final[frozenset[str]] = frozenset(
    {"ff_chat_messages", "ff_core_memory_revisions"}
)


def _persona_cascade_models() -> tuple[type[Any], ...]:
    """从 ``Base.metadata`` 派生人格删除的全部 persona_id 作用域模型。

    级联范围不再手写清单：任何含 ``persona_id`` 列的表（含未来新增表，
    含未挂 ``PersonaMixin`` 的整数列依赖表）都自动进入删除集合；保留表
    （:data:`PERSONA_DELETE_RETAINED_TABLES`）除外。结构守卫测试断言本
    集合与元数据 persona_id 表全集（扣除保留表）相等。

    Returns:
        待按 persona_id 批删的 ORM 模型元组（``Base.registry`` 注册序）。
    """
    load_all_models()
    persona_tables = {
        table.name
        for table in Base.metadata.tables.values()
        if "persona_id" in table.c
        and table.name not in PERSONA_DELETE_RETAINED_TABLES
    }
    models: list[type[Any]] = []
    for mapper in Base.registry.mappers:
        tablename = getattr(mapper.class_, "__tablename__", None)
        if tablename in persona_tables:
            models.append(mapper.class_)
    return tuple(models)


def _persona_value_for(model: type[Any], persona_id: int) -> int | str:
    """按列的存储类型折算 persona_id 比较值（跨方言类型一致）。

    PersonaMixin 的 ``persona_id`` 为字符串列，部分手工依赖表为整数列
    （如 ``ff_persona_states`` / ``ff_memory_index_lite``）；从列类型派生
    比较值，避免硬编码。

    Args:
        model: ORM 模型类。
        persona_id: 人格主键。

    Returns:
        整数列返回 int，其余（字符串列）返回 str。
    """
    column_type = model.__table__.c.persona_id.type
    if isinstance(column_type, (Integer, BigInteger)):
        return persona_id
    return str(persona_id)


@asynccontextmanager
async def bind_persona_context(persona_id: int | str) -> AsyncIterator[RequestContext]:
    """绑定人格上下文作用域（运行期任务绑定——chat 会话等入口消费）。

    ``core.context.bind_context`` 的薄封装：人格注册表主键为 BigInt，
    而上下文轴 ``persona_id_var`` / PersonaMixin 均为字符串形态——
    此处经 ``str(persona_id)`` 归一后绑定（int / str 均可传入）。作用域
    退出恢复原上下文（合并语义与嵌套隔离见 core.context）。

    用法::

        async with bind_persona_context(persona.id):
            await chat_service.reply(...)   # PersonaMixin 表自动隔离

    Args:
        persona_id: 人格主键（int）或其字符串形态。

    Yields:
        合并后绑定的 :class:`RequestContext` 实例。
    """
    async with bind_context(persona_id=str(persona_id)) as ctx:
        yield ctx


async def _rollback_quietly(session: AsyncSession) -> None:
    """尽力回滚（批量路径逐项失败时清理会话待处理态——不向调用方注入新异常）。

    Args:
        session: 待回滚的异步 Session。
    """
    try:
        await session.rollback()
    except Exception:  # best-effort: 回滚失败由会话关闭兜底
        logger.opt(exception=True).debug("批量路径会话回滚失败（close 兜底）")


def canonical_card_hash(card: CharaCardModel) -> str:
    """卡片内容哈希（导入幂等判据——sha256 of canonical JSON）。

    规范形态：``model_dump()``（模型字段序 + 缺省补齐——同一文档恒同
    形态）→ ``sort_keys`` 键序稳定化 → 紧凑分隔符 + 中文原样序列化 →
    sha256 hexdigest。同一卡片文档（含字段序差异的等价 JSON）恒同哈希。

    Args:
        card: 待哈希的卡片模型。

    Returns:
        64 字符 hex 摘要。
    """
    canonical = json.dumps(
        card.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class PersonaService:
    """人格注册表服务（无状态——Session 由调用方注入，事务边界归本层）。"""

    def __init__(self, *, indexer: MemoryIndexer | None = None) -> None:
        """初始化服务。

        Args:
            indexer: 记忆检索索引管道（None = 无索引联动——单元测试 /
                直调面；绑定形态下人格删除提交后按 ref 集合清理检索
                索引行，进程级装配经 app.state.memory_indexer 注入）。
        """
        self._indexer = indexer

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _require_row(self, session: AsyncSession, persona_id: int) -> PersonaRow:
        """按主键取人格行；缺失 fail loud。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            对应 :class:`PersonaRow`（命中 identity map 时直接复用）。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        row = await session.get(PersonaRow, persona_id)
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="人格不存在",
                details={"persona_id": persona_id},
            )
        return row

    async def _ensure_name_free(
        self,
        session: AsyncSession,
        name: str,
        exclude_id: int | None = None,
    ) -> None:
        """注册名唯一性守卫（创建 / 重命名 / 复制共用）。

        Args:
            session: 注入的异步 Session。
            name: 待占用的注册名。
            exclude_id: 重命名场景排除自身主键（None = 全表校验）。

        Raises:
            BusinessException: 注册名已被占用（``CONFLICT``，details 携带
                占用方主键）。
        """
        stmt = select(PersonaRow.id).where(PersonaRow.name == name)
        if exclude_id is not None:
            stmt = stmt.where(PersonaRow.id != exclude_id)
        holder_id = (await session.execute(stmt)).scalar_one_or_none()
        if holder_id is not None:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="人格注册名已存在",
                details={"name": name, "conflict_persona_id": holder_id},
            )

    def _merge_card(self, current: dict[str, Any], patch: CharaCardPatchDTO) -> dict[str, Any]:
        """卡片补丁合并（仅出现字段覆写 → 整体重校验）。

        Args:
            current: 既有卡片 JSON 文档（行 chara_card 列原值）。
            patch: 卡片补丁 DTO。

        Returns:
            合并后的卡片 JSON 文档（经 :class:`CharaCardModel` 整体重校验
            的序列化形态）。

        Raises:
            BusinessException: 合并结果校验失败（``VALIDATION_ERROR``——
                如补丁把 name 置空、世界书条目规格违约）。
        """
        try:
            merged = CharaCardModel.model_validate(
                {**current, **patch.model_dump(exclude_unset=True)}
            )
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="人格卡片合并校验失败",
                details={"reason": str(exc)},
            ) from exc
        return merged.model_dump()

    async def _commit(self, session: AsyncSession) -> None:
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

    # ------------------------------------------------------------------
    # 注册 CRUD
    # ------------------------------------------------------------------

    async def create(self, session: AsyncSession, dto: PersonaCreateDTO) -> PersonaRecordDTO:
        """创建人格（注册名唯一校验；产物恒非主人格）。

        Args:
            session: 注入的异步 Session。
            dto: 创建 DTO（status 缺省 disabled；展示名缺省回落 card.name；
                voice_profile 可选绑定）。

        Returns:
            落库后的记录 DTO（含主键）。

        Raises:
            BusinessException: 注册名已占用（``CONFLICT``——含唯一名校验
                命中与并发写入撞唯一约束两形态；后者经 ``IntegrityError``
                捕获归一为同语义 409）。
        """
        await self._ensure_name_free(session, dto.name)
        row = PersonaRow(
            name=dto.name,
            display_name=dto.display_name or dto.card.name,
            status=dto.status.value,
            is_primary_marker=None,
            chara_card=dto.card.model_dump(),
            voice_profile=dto.voice_profile.model_dump() if dto.voice_profile is not None else None,
        )
        session.add(row)
        try:
            await self._commit(session)
        except IntegrityError as exc:
            # 并发同名竞态：唯一性预检窗口外的撞约束——归一为 CONFLICT
            # （_commit 出口已回滚会话，此处无需再回滚）。
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="人格注册名已存在（并发写入冲突）",
                details={"name": dto.name},
            ) from exc
        logger.info("人格已创建: id={}, name={}", row.id, row.name)
        return PersonaRecordDTO.model_validate(row)

    async def get(self, session: AsyncSession, persona_id: int) -> PersonaRecordDTO:
        """按主键读取人格。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        row = await self._require_row(session, persona_id)
        return PersonaRecordDTO.model_validate(row)

    async def query(
        self, session: AsyncSession, query: PersonaQueryDTO
    ) -> PageResponse[PersonaRecordDTO]:
        """游标分页列表（id 降序——新人格在前；status 可选过滤）。

        Args:
            session: 注入的异步 Session。
            query: 查询 DTO（status 过滤 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录；total 恒 -1
            ——注册表行数有限，计数不在分页路径上执行）。
        """
        stmt = select(PersonaRow)
        if query.status is not None:
            stmt = stmt.where(PersonaRow.status == query.status.value)
        cursor_payload = decode_cursor(query.cursor)
        cursor_id = cursor_payload.get("id") if cursor_payload is not None else None
        if isinstance(cursor_id, int):
            stmt = stmt.where(PersonaRow.id < cursor_id)
        stmt = stmt.order_by(desc(PersonaRow.id)).limit(query.limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = encode_cursor({"id": page_rows[-1].id}) if has_more and page_rows else None
        return PageResponse.of_cursor(
            items=[PersonaRecordDTO.model_validate(row) for row in page_rows],
            page_size=query.limit,
            next_cursor=next_cursor,
        )

    async def update(
        self, session: AsyncSession, persona_id: int, dto: PersonaUpdateDTO
    ) -> PersonaRecordDTO:
        """部分更新人格（重命名 / 展示名 / 状态 / 卡片补丁——仅出现字段生效）。

        重命名走唯一校验（排除自身）；status 切到 disabled 时若该人格是
        当前主人格则**守卫拒绝**（先另设主人格再停用——主人格须恒在位）；
        卡片补丁合并语义见 :meth:`_merge_card`。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            dto: 更新 DTO（不含主人格标记——主人格仅经 set_primary
                事务性操作）。

        Returns:
            更新后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；注册名被占用
                （``CONFLICT``——含并发重命名撞唯一约束形态）；停用当前
                主人格（``CONFLICT``——两步守卫）；卡片合并校验失败
                （``VALIDATION_ERROR``）。
        """
        row = await self._require_row(session, persona_id)
        if dto.name is not None and dto.name != row.name:
            await self._ensure_name_free(session, dto.name, exclude_id=persona_id)
            row.name = dto.name
        if dto.display_name is not None:
            row.display_name = dto.display_name
        if dto.status is not None and dto.status.value != row.status:
            if dto.status == PersonaStatus.DISABLED and row.is_primary:
                # 停用守卫：主人格停用须先另设主人格（两步显式——与
                # disable 操作面同一守卫语义）。
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message="不能停用当前主人格，请先将另一个人格设为主人格再停用",
                    details={
                        "persona_id": persona_id,
                        "fix": "先调用 set-primary（POST /api/v1/personas/{id}/set-primary）",
                    },
                )
            row.status = dto.status.value
        if dto.card is not None:
            row.chara_card = self._merge_card(row.chara_card, dto.card)
        if "voice_profile" in dto.model_fields_set:
            # 出现即整体替换（显式 null 清除绑定；语音绑定无字段级合并）
            row.voice_profile = (
                dto.voice_profile.model_dump() if dto.voice_profile is not None else None
            )
        # 提交前留存目标名（IntegrityError 回滚会过期行属性——不可再读行）
        target_name = dto.name if dto.name is not None else row.name
        try:
            await self._commit(session)
        except IntegrityError as exc:
            # 并发重命名竞态：两个会话同窗通过预检后撞 name 唯一约束。
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="人格注册名已存在（并发写入冲突）",
                details={"name": target_name, "persona_id": persona_id},
            ) from exc
        return PersonaRecordDTO.model_validate(row)

    async def copy(
        self, session: AsyncSession, persona_id: int, dto: PersonaCopyDTO
    ) -> PersonaRecordDTO:
        """复制人格（新注册名；可选深拷贝 M/S 状态；产物恒 disabled + 非主人格）。

        Args:
            session: 注入的异步 Session。
            persona_id: 源人格主键。
            dto: 复制 DTO（new_name 唯一校验；include_states 深拷贝开关）。

        Returns:
            复制产物的记录 DTO。

        Raises:
            BusinessException: 源人格不存在（``NOT_FOUND``）；新注册名被
                占用（``CONFLICT``）。
        """
        source = await self._require_row(session, persona_id)
        await self._ensure_name_free(session, dto.new_name)
        new_row = PersonaRow(
            name=dto.new_name,
            display_name=dto.display_name or source.display_name,
            status=PersonaStatus.DISABLED.value,
            is_primary_marker=None,
            chara_card=CharaCardModel.model_validate(source.chara_card).model_dump(),
            voice_profile=dict(source.voice_profile) if source.voice_profile is not None else None,
        )
        session.add(new_row)
        await session.flush()  # 先取新行主键，状态深拷贝需要

        if dto.include_states:
            state_rows = list(
                (
                    await session.execute(
                        select(PersonaStateRow).where(PersonaStateRow.persona_id == persona_id)
                    )
                )
                .scalars()
                .all()
            )
            for state_row in state_rows:
                session.add(
                    PersonaStateRow(
                        persona_id=new_row.id,
                        layer=state_row.layer,
                        state=copy.deepcopy(state_row.state),
                    )
                )
        await self._commit(session)
        logger.info(
            "人格已复制: source_id={}, new_id={}, include_states={}",
            persona_id,
            new_row.id,
            dto.include_states,
        )
        return PersonaRecordDTO.model_validate(new_row)

    async def delete(self, session: AsyncSession, persona_id: int) -> None:
        """删除人格（**主人格守卫：当前主人格禁止删除**——先另设主人格）。

        级联范围从 ``Base.metadata`` 派生（同一事务内按 persona_id 批删——
        本工程无跨域外键依赖，由服务层显式批删保证 SQLite / MySQL 方言
        一致）：

            - 全部挂 ``PersonaMixin`` 的表（当前为 ``ff_persona_states`` /
              ``ff_memory_episodes`` / ``ff_curated_memory`` / ``ff_dream_runs``
              / ``ff_persona_facts`` / ``ff_persona_skills`` /
              ``ff_core_memory_blocks`` / ``ff_chat_sessions`` / ``ff_stickers``
              / ``ff_voiceprint_samples`` / ``ff_voiceprint_consents`` /
              ``ff_reminders`` / ``ff_canvas_documents``；未来新增表自动
              纳入——:func:`_persona_cascade_models` 派生，不维护手写清单）；
            - 无 persona_id 的依赖表按归属轴清理：``ff_chat_messages`` 经
              session ids；``ff_core_memory_revisions`` 经块 ids；
              ``ff_memory_index_lite`` 经 persona_id 谓词（整数列）；
            - ``ff_usage_records`` 有意**保留**（跨人格聚合真源——见
              :data:`PERSONA_DELETE_RETAINED_TABLES`）。

        磁盘文件同生命周期（锁定边界）：删除前先收集表情包
        ``storage_path`` 与声纹样本 ``sample_path``；**事务提交后**逐路径
        best-effort 删除（失败仅告警，不翻转删除结果——DB 行已清理，
        残留文件不参与查询）。收集失败不阻断：行删除仍以 DB 为准。

        检索索引行同步清理：``ff_memory_index_lite``（SQLite 回退索引表）
        在同事务内按 persona_id 删除；SeekDB 生产索引（``ff_memory_index``，
        独立数据面）在提交后经注入的 ``indexer.dispatch_delete``
        best-effort 清理（失败不翻转删除结果——与 memory 域既有索引删除
        面同语义）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；当前主人格
                禁止删除（``CONFLICT``，details 携带指引）。
        """
        row = await self._require_row(session, persona_id)
        if row.is_primary:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="不能删除当前主人格，请先将另一个人格设为主人格（set-primary）再删除",
                details={
                    "persona_id": persona_id,
                    "fix": "先调用 set-primary（POST /api/v1/personas/{id}/set-primary）",
                },
            )
        skip_filter = {PERSONA_FILTER_SKIP_OPTION: True}
        episode_ids = list(
            (
                await session.execute(
                    select(MemoryEpisodeRow.id).where(
                        MemoryEpisodeRow.persona_id == str(persona_id)
                    ),
                    execution_options=skip_filter,
                )
            )
            .scalars()
            .all()
        )
        curated_ids = list(
            (
                await session.execute(
                    select(CuratedMemoryRow.id).where(
                        CuratedMemoryRow.persona_id == str(persona_id)
                    ),
                    execution_options=skip_filter,
                )
            )
            .scalars()
            .all()
        )
        session_ids = list(
            (
                await session.execute(
                    select(ChatSessionRow.id).where(ChatSessionRow.persona_id == str(persona_id)),
                    execution_options=skip_filter,
                )
            )
            .scalars()
            .all()
        )
        file_paths = await self._collect_persona_file_paths(
            session, persona_id, skip_filter=skip_filter
        )
        if session_ids:
            await session.execute(
                delete(ChatMessageRow).where(ChatMessageRow.session_id.in_(session_ids))
            )
        # 自引用更正链（事实 / 策展）先置空悬挂链接——避免单语句批量删除
        # 在 MySQL 自引用外键上的删除序歧义（SQLite 测试方言不强制 FK）。
        await session.execute(
            update(PersonaFactRow)
            .where(PersonaFactRow.persona_id == str(persona_id))
            .values(superseded_by_id=None),
            execution_options=skip_filter,
        )
        await session.execute(
            update(CuratedMemoryRow)
            .where(CuratedMemoryRow.persona_id == str(persona_id))
            .values(superseded_by_id=None),
            execution_options=skip_filter,
        )
        # 修订链在块行之前删除（块行 CASCADE 的子表；显式序保证跨方言一致）。
        await session.execute(
            delete(CoreMemoryRevisionRow).where(
                CoreMemoryRevisionRow.block_id.in_(
                    select(CoreMemoryBlockRow.id).where(
                        CoreMemoryBlockRow.persona_id == str(persona_id)
                    )
                )
            )
        )
        for model in _persona_cascade_models():
            await session.execute(
                delete(model).where(
                    model.persona_id == _persona_value_for(model, persona_id)
                ),
                execution_options=skip_filter,
            )
        await session.delete(row)
        await self._commit(session)
        await self._purge_remote_index(
            persona_id, episode_ids=episode_ids, curated_ids=curated_ids
        )
        await self._purge_persona_files(file_paths)
        logger.info(
            "人格已删除: id={}, episodes={}, curated={}, sessions={}, files={}",
            persona_id,
            len(episode_ids),
            len(curated_ids),
            len(session_ids),
            len(file_paths),
        )

    async def _collect_persona_file_paths(
        self,
        session: AsyncSession,
        persona_id: int,
        *,
        skip_filter: dict[str, bool],
    ) -> list[Path]:
        """收集人格删除前需要清理的磁盘文件路径（行删除即失去定位面）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            skip_filter: 绕过人格自动过滤的 execution options（管理面显式谓词）。

        Returns:
            表情包文件路径 + 声纹样本路径（去重；已存在性由删除面容错）。
        """
        sticker_paths = (
            (
                await session.execute(
                    select(StickerRow.storage_path).where(
                        StickerRow.persona_id == str(persona_id)
                    ),
                    execution_options=skip_filter,
                )
            )
            .scalars()
            .all()
        )
        sample_paths = (
            (
                await session.execute(
                    select(VoiceprintSampleRow.sample_path).where(
                        VoiceprintSampleRow.persona_id == str(persona_id)
                    ),
                    execution_options=skip_filter,
                )
            )
            .scalars()
            .all()
        )
        return [Path(path) for path in (*sticker_paths, *sample_paths)]

    async def _purge_persona_files(self, paths: Sequence[Path]) -> None:
        """提交后 best-effort 删除人格的磁盘文件（失败仅告警，不阻断）。

        Args:
            paths: 事务提交前收集的文件路径。
        """
        for path in paths:
            try:
                await run_blocking_ops(unlink_missing_ok, path)
            except OSError:
                logger.opt(exception=True).warning(
                    "人格删除的文件清理失败（已吞没，DB 行已删除）: {}", path
                )

    async def _purge_remote_index(
        self,
        persona_id: int,
        *,
        episode_ids: Sequence[int],
        curated_ids: Sequence[int],
    ) -> None:
        """提交后清理生产检索索引（SeekDB 独立数据面——best-effort）。

        索引行已在业务库回退索引表内同事务删除；生产 SeekDB 索引经注入
        的 indexer 逐 ref 分发删除（fire-and-forget / 内联由 indexer 的
        后台管理器配置决定）。未注入 indexer 时零调用。

        Args:
            persona_id: 人格主键（索引隔离谓词）。
            episode_ids: 已删除的情景事件主键集合。
            curated_ids: 已删除的策展记忆主键集合。
        """
        if self._indexer is None:
            return
        refs: list[tuple[MemoryRefType, int]] = [
            (MemoryRefType.EPISODE, ref_id) for ref_id in episode_ids
        ]
        refs.extend((MemoryRefType.CURATED, ref_id) for ref_id in curated_ids)
        for ref_type, ref_id in refs:
            try:
                await self._indexer.dispatch_delete(ref_type, ref_id, persona_id)
            except Exception:  # best-effort: 索引清理失败不翻转人格删除结果
                logger.opt(exception=True).warning(
                    "人格删除的检索索引清理失败（已吞没）: ref_type={}, ref_id={}",
                    ref_type.value,
                    ref_id,
                )

    async def batch_delete(
        self, session: AsyncSession, ids: Sequence[int]
    ) -> PersonaBatchDeleteResponseDTO:
        """批量删除（部分成功形态——逐 id 独立事务，单项失败不阻塞其余）。

        单项失败捕获面为**全部异常**：``BusinessException`` 沿用其错误码
        与消息；其余未知异常归一为 ``INTERNAL_ERROR`` 失败项 + 告警日志
        （partial-success 承诺——任何单项故障都不阻塞其余 id 的删除，
        也不向调用方注入原始异常打断整批）。

        Args:
            session: 注入的异步 Session。
            ids: 待删除人格主键序列（去重由调用方约束；重复 id 的后续
                项按 NOT_FOUND 记失败）。

        Returns:
            批量删除响应（total / succeeded / failed / 逐 id 结果）。
        """
        items: list[PersonaBatchDeleteItemDTO] = []
        for persona_id in ids:
            try:
                await self.delete(session, persona_id)
                items.append(
                    PersonaBatchDeleteItemDTO(id=persona_id, success=True, message="人格已删除")
                )
            except BusinessException as exc:
                await _rollback_quietly(session)
                items.append(
                    PersonaBatchDeleteItemDTO(
                        id=persona_id,
                        success=False,
                        code=exc.code,
                        message=exc.message,
                    )
                )
            except Exception:
                await _rollback_quietly(session)
                logger.opt(exception=True).warning(
                    "批量删除单项出现未知异常（归一为 INTERNAL_ERROR 失败项）: persona_id={}",
                    persona_id,
                )
                items.append(
                    PersonaBatchDeleteItemDTO(
                        id=persona_id,
                        success=False,
                        code=ErrorCode.INTERNAL_ERROR,
                        message="删除人格时发生内部错误",
                    )
                )
        succeeded = sum(1 for item in items if item.success)
        return PersonaBatchDeleteResponseDTO(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            items=items,
        )

    # ------------------------------------------------------------------
    # 导入面（预设生成卡片 / 用户卡片文档——内容哈希幂等 + 冲突策略）
    # ------------------------------------------------------------------

    @staticmethod
    def _free_suffixed_name(taken_names: set[str], base: str) -> str:
        """自动后缀寻名（``base-2`` 起递增——首个空闲名）。

        Args:
            taken_names: 已占用注册名集合。
            base: 后缀基名（期望名 / 卡片名）。

        Returns:
            首个空闲的 ``base-N`` 名。

        Raises:
            BusinessException: ``-2`` 至 ``-100`` 全部被占用（极端注册表
                形态——``INTERNAL_ERROR``，调用方换名重试）。
        """
        for suffix in range(2, 101):
            candidate = f"{base}-{suffix}"
            if candidate not in taken_names:
                return candidate
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="自动改名后缀已耗尽（名字-2 至 名字-100 均被占用），请显式指定注册名",
            details={"base": base},
        )

    async def import_persona(
        self,
        session: AsyncSession,
        card: CharaCardModel,
        *,
        strategy: PersonaImportConflictStrategy = PersonaImportConflictStrategy.SKIP,
        desired_name: str | None = None,
        warnings: Sequence[str] = (),
        voice_profile: VoiceProfileModel | None = None,
    ) -> PersonaImportResultModel:
        """导入人格（内容哈希幂等 + 名称冲突策略——导入操作面）。

        判定序（锁定语义）：

            1. **内容同卡幂等**：导入卡片与既有人格卡片哈希一致 →
               ``{imported: false, reason: identical_content, persona_id:
               既有行}``——**与冲突策略无关**（同卡重导恒幂等）；
            2. 目标注册名（``desired_name`` 或卡片名）空闲 → 创建新行
               （恒 disabled + 非主人格；展示名回落卡片名；预设导入
               可携带 ``voice_profile`` 落库）；
            3. 名称冲突 → 策略生效：``skip``（缺省）返回
               ``name_conflict`` 未导入；``rename`` 自动后缀
               ``名字-2`` 起寻名落库；``overwrite`` 整体替换占用行卡片
               （**保留** M/S 状态 / status / 主人格标记 / 语音绑定）。

        Args:
            session: 注入的异步 Session。
            card: 导入卡片（预设生成产物或用户自有卡片文档）。
            strategy: 名称冲突策略（缺省 skip）。
            desired_name: 期望注册名（None = 卡片名；预设导入时卡片名
                已含期望名 + 预设后缀——调用方传 None 防双拼）。
            warnings: 透传警告文案（预设生成冲突软警告等——并入结果）。
            voice_profile: 预设建议语音绑定（None = 不绑定；仅创建
                路径落库，覆盖路径保留既有绑定）。

        Returns:
            导入结果（imported / persona_id / reason / warnings）。

        Raises:
            BusinessException: 期望名为空白（``VALIDATION_ERROR``）；
                并发写入撞名（``CONFLICT``）；自动后缀耗尽
                （``INTERNAL_ERROR``）。
        """
        target_name = (
            desired_name if desired_name is not None else card.name
        ).strip()
        if not target_name:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="导入期望名不能为空白",
                details={"desired_name": desired_name},
            )
        card_hash = canonical_card_hash(card)
        rows = list((await session.execute(select(PersonaRow))).scalars().all())
        for row in rows:
            existing_hash = canonical_card_hash(CharaCardModel.model_validate(row.chara_card))
            if existing_hash == card_hash:
                # 内容同卡幂等：与策略无关——同卡重导恒跳过。
                return PersonaImportResultModel(
                    imported=False,
                    persona_id=row.id,
                    reason=PersonaImportReason.IDENTICAL_CONTENT,
                    warnings=list(warnings),
                )
        holder = next((row for row in rows if row.name == target_name), None)
        if holder is None:
            new_row = PersonaRow(
                name=target_name,
                display_name=card.name,
                status=PersonaStatus.DISABLED.value,
                is_primary_marker=None,
                chara_card=card.model_dump(),
                voice_profile=voice_profile.model_dump() if voice_profile is not None else None,
            )
            session.add(new_row)
            try:
                await self._commit(session)
            except IntegrityError as exc:
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message="人格导入并发写入冲突（注册名被抢占）",
                    details={"name": target_name},
                ) from exc
            logger.info("人格已导入: id={}, name={}", new_row.id, new_row.name)
            return PersonaImportResultModel(
                imported=True,
                persona_id=new_row.id,
                warnings=list(warnings),
            )
        if strategy == PersonaImportConflictStrategy.SKIP:
            return PersonaImportResultModel(
                imported=False,
                persona_id=holder.id,
                reason=PersonaImportReason.NAME_CONFLICT,
                warnings=list(warnings),
            )
        if strategy == PersonaImportConflictStrategy.RENAME:
            # 期望名被占用：自动后缀寻名（desired_name 已在判定序 2 用尽
            # 空闲形态——到达此处必然被占用，直接后缀）。
            final_name = self._free_suffixed_name(
                {row.name for row in rows}, target_name
            )
            new_row = PersonaRow(
                name=final_name,
                display_name=card.name,
                status=PersonaStatus.DISABLED.value,
                is_primary_marker=None,
                chara_card=card.model_dump(),
                voice_profile=voice_profile.model_dump() if voice_profile is not None else None,
            )
            session.add(new_row)
            try:
                await self._commit(session)
            except IntegrityError as exc:
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message="人格导入并发写入冲突（注册名被抢占）",
                    details={"name": final_name},
                ) from exc
            logger.info("人格已导入（自动改名）: id={}, name={}", new_row.id, new_row.name)
            return PersonaImportResultModel(
                imported=True,
                persona_id=new_row.id,
                warnings=list(warnings),
            )
        # overwrite：整体替换占用行卡片——保留 M/S 状态 / status /
        # 主人格标记 / 语音绑定（仅卡片文档替换）。
        holder.chara_card = card.model_dump()
        await self._commit(session)
        logger.info("人格卡片已覆盖导入: id={}, name={}", holder.id, holder.name)
        return PersonaImportResultModel(
            imported=True,
            persona_id=holder.id,
            warnings=list(warnings),
        )

    async def import_personas_batch(
        self,
        session: AsyncSession,
        files: Sequence[PersonaImportBatchFileDTO],
        *,
        strategy: PersonaImportConflictStrategy = PersonaImportConflictStrategy.SKIP,
    ) -> PersonaImportBatchResultDTO:
        """批量导入（部分成功形态——逐文件独立事务，单文件失败不阻塞其余）。

        单文件失败捕获面为**全部异常**：``BusinessException`` 沿用其
        错误码与消息；其余未知异常归一为 ``INTERNAL_ERROR`` 失败项 +
        告警日志。解析失败（``card=None``）直接记 ``VALIDATION_ERROR``
        失败项。幂等 / 冲突跳过是**合法结果**（success=True、
        imported=False——reason 呈现原因）。

        Args:
            session: 注入的异步 Session。
            files: 逐文件装配面（文件名 + 卡片或解析错误）。
            strategy: 名称冲突策略（整批统一；缺省 skip）。

        Returns:
            批量导入结果（total / succeeded / failed / imported_count /
            逐文件结果——与提交顺序一致）。
        """
        items: list[PersonaImportBatchItemDTO] = []
        for file_entry in files:
            if file_entry.card is None or file_entry.error_message is not None:
                items.append(
                    PersonaImportBatchItemDTO(
                        filename=file_entry.filename,
                        success=False,
                        imported=False,
                        code=ErrorCode.VALIDATION_ERROR,
                        message=file_entry.error_message or "卡片文件解析失败",
                    )
                )
                continue
            try:
                result = await self.import_persona(
                    session, file_entry.card, strategy=strategy
                )
                if result.imported:
                    message = "已导入"
                elif result.reason == PersonaImportReason.IDENTICAL_CONTENT:
                    message = "内容同卡已存在，幂等跳过"
                else:
                    message = "注册名已存在，按跳过策略未导入"
                items.append(
                    PersonaImportBatchItemDTO(
                        filename=file_entry.filename,
                        success=True,
                        imported=result.imported,
                        persona_id=result.persona_id,
                        reason=result.reason,
                        message=message,
                    )
                )
            except BusinessException as exc:
                await _rollback_quietly(session)
                items.append(
                    PersonaImportBatchItemDTO(
                        filename=file_entry.filename,
                        success=False,
                        imported=False,
                        code=exc.code,
                        message=exc.message,
                    )
                )
            except Exception:
                await _rollback_quietly(session)
                logger.opt(exception=True).warning(
                    "批量导入单文件出现未知异常（归一为 INTERNAL_ERROR 失败项）: filename={}",
                    file_entry.filename,
                )
                items.append(
                    PersonaImportBatchItemDTO(
                        filename=file_entry.filename,
                        success=False,
                        imported=False,
                        code=ErrorCode.INTERNAL_ERROR,
                        message="导入人格时发生内部错误",
                    )
                )
        succeeded = sum(1 for item in items if item.success)
        imported_count = sum(1 for item in items if item.imported)
        return PersonaImportBatchResultDTO(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            imported_count=imported_count,
            items=items,
        )

    # ------------------------------------------------------------------
    # 主人格切换（事务性）+ 可寻址集
    # ------------------------------------------------------------------

    async def set_primary(self, session: AsyncSession, persona_id: int) -> PersonaRecordDTO:
        """设为主人格（**事务性切换**——设 B 为主必然解除 A）。

        单事务内「全表清主人格标记 → 目标取标记」：任一步失败整体回滚。
        **至多一个主人格的不变式由 DB 唯一键 ``uq_ff_personas_primary``
        强制**（标记列唯一、NULL 共存——与策展记忆单 active 同模式）；
        并发 set_primary 撞唯一键时回滚后重试一次（重读当前主人格走
        解除-再占位分支），再冲突则映射 ``INTERNAL_ERROR``。目标须
        ``status=active``（停用人格不可设为主人格）；重复设置当前主人格
        为幂等 no-op。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            设为主人格后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；停用人格不可
                设为主人格（``CONFLICT``）；重试后仍并发冲突
                （``INTERNAL_ERROR``）。
        """
        try:
            return await self._set_primary_once(session, persona_id)
        except IntegrityError:
            # 并发切换抢先占了主人格标记——回滚后重读状态重试（策展
            # upsert 同模式的单次重试收敛）。
            await _rollback_quietly(session)
            logger.info("主人格并发切换冲突，回滚后重试: persona_id={}", persona_id)
        try:
            return await self._set_primary_once(session, persona_id)
        except IntegrityError as exc:
            await _rollback_quietly(session)
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="主人格并发切换冲突（重试后仍冲突）",
                details={"persona_id": persona_id},
            ) from exc

    async def _set_primary_once(
        self, session: AsyncSession, persona_id: int
    ) -> PersonaRecordDTO:
        """单次主人格切换尝试（无重试——由 :meth:`set_primary` 编排并发收敛）。

        语句序（确定性——腾位后占位）：守卫校验（存在 + status=active +
        幂等短路）→ 全表清主人格标记（腾出唯一键位）→ 目标行取标记
        （占位）→ 单事务提交。任一 flush 点至多一行持有 ``'primary'``。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            设为主人格后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；停用人格不可
                设为主人格（``CONFLICT``）。
            IntegrityError: 并发切换撞 ``uq_ff_personas_primary``（由上层
                重试收敛）。
        """
        row = await self._require_row(session, persona_id)
        if row.status != PersonaStatus.ACTIVE.value:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="停用状态的人格不可设为主人格，请先启用（status=active）",
                details={"persona_id": persona_id, "status": row.status},
            )
        if row.is_primary_marker == PERSONA_PRIMARY_MARKER:
            # 幂等：重复设置当前主人格为 no-op（不动 DB 状态）。
            return PersonaRecordDTO.model_validate(row)
        # 单事务切换：全表清标记 + 目标占位（commit 原子提交）。
        await session.execute(
            update(PersonaRow)
            .where(PersonaRow.is_primary_marker.is_not(None))
            .values(is_primary_marker=None)
        )
        row.is_primary_marker = PERSONA_PRIMARY_MARKER
        await self._commit(session)
        logger.info("人格已设为主人格（事务性切换）: id={}", persona_id)
        return PersonaRecordDTO.model_validate(row)

    async def disable(self, session: AsyncSession, persona_id: int) -> PersonaRecordDTO:
        """停用人格（纯生命周期操作——status 切 disabled）。

        **主人格守卫**：停用当前主人格直接 ``CONFLICT`` 拒绝（先经
        :meth:`set_primary` 将另一个人格设为主人格——两步显式，与删除
        守卫同模式；主人格须恒在位）。已停用人格重复停用为幂等 no-op。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            停用后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；停用当前主人格
                （``CONFLICT``，details 携带指引）。
        """
        row = await self._require_row(session, persona_id)
        if row.is_primary:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="不能停用当前主人格，请先将另一个人格设为主人格（set-primary）再停用",
                details={
                    "persona_id": persona_id,
                    "fix": "先调用 set-primary（POST /api/v1/personas/{id}/set-primary）",
                },
            )
        if row.status == PersonaStatus.DISABLED.value:
            # 幂等：已停用人格重复停用为 no-op（不动 DB 状态）。
            return PersonaRecordDTO.model_validate(row)
        row.status = PersonaStatus.DISABLED.value
        await self._commit(session)
        logger.info("人格已停用（生命周期下线）: id={}", persona_id)
        return PersonaRecordDTO.model_validate(row)

    async def get_primary_persona(self, session: AsyncSession) -> PersonaRecordDTO:
        """读取当前主人格（无主人格 fail loud——清晰中文错误指引）。

        消费面（chat 会话 / 值班 / 梦境 / 记忆面缺省目标）原将「无激活
        人格」翻译为各自错误——统一由本方法承担语义 parity：无主人格
        （全新库 / 从未设置）抛 ``NOT_FOUND``（清晰中文消息 + fix 指引）；
        无人格可正常工作的消费面（语音 / 图像理解的未绑定形态）自行
        捕获本错误归零。

        Args:
            session: 注入的异步 Session。

        Returns:
            当前主人格记录 DTO。

        Raises:
            BusinessException: 当前无主人格（``NOT_FOUND``——fail loud）。
        """
        row = (
            (
                await session.execute(
                    select(PersonaRow)
                    .where(PersonaRow.is_primary_marker == PERSONA_PRIMARY_MARKER)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="当前没有主人格，请先创建并启用一个人格，再将其设为主人格",
                details={"fix": "POST /api/v1/personas/{id}/set-primary（设为主人格）"},
            )
        return PersonaRecordDTO.model_validate(row)

    async def list_addressable(self, session: AsyncSession) -> list[PersonaRecordDTO]:
        """读取可寻址人格集（全部 enabled 人格——status=active）。

        多激活可寻址模型的核心读取面：enabled 人格可同时被通道 / 心流 /
        多会话按上下文寻址绑定（主人格仅是缺省解析目标，非唯一可用者）。
        排序 id 升序（注册序——稳定确定性）。

        Args:
            session: 注入的异步 Session。

        Returns:
            可寻址人格记录列表（空列表 = 无 enabled 人格）。
        """
        rows = list(
            (
                await session.execute(
                    select(PersonaRow)
                    .where(PersonaRow.status == PersonaStatus.ACTIVE.value)
                    .order_by(PersonaRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [PersonaRecordDTO.model_validate(row) for row in rows]

    # ------------------------------------------------------------------
    # 卡片面
    # ------------------------------------------------------------------

    async def get_card(self, session: AsyncSession, persona_id: int) -> PersonaCardDTO:
        """读取人格卡片（chara card v2 文档）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            卡片读取 DTO（含最近更新时间）。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        row = await self._require_row(session, persona_id)
        return PersonaCardDTO(
            persona_id=row.id,
            card=CharaCardModel.model_validate(row.chara_card),
            updated_at=row.updated_at,
        )

    async def replace_card(
        self, session: AsyncSession, persona_id: int, card: CharaCardModel
    ) -> PersonaCardDTO:
        """整体替换人格卡片（PUT 语义——非补丁合并）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            card: 完整卡片模型（已通过 DTO 边界校验）。

        Returns:
            替换后的卡片读取 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        row = await self._require_row(session, persona_id)
        row.chara_card = card.model_dump()
        await self._commit(session)
        return PersonaCardDTO(
            persona_id=row.id,
            card=CharaCardModel.model_validate(row.chara_card),
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # 状态面（M/S 层）
    # ------------------------------------------------------------------

    async def upsert_state(
        self,
        session: AsyncSession,
        persona_id: int,
        layer: PersonaStateLayer,
        dto: PersonaStateUpsertDTO,
    ) -> PersonaStateDTO:
        """状态层 upsert（每 (persona_id, layer) 一行；state 整体替换）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            layer: 状态层（M=中期心情/关系；S=短期会话状态）。
            dto: 状态写入 DTO。

        Returns:
            落库后的状态 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``——状态不能挂在
                不存在的人格上）。
        """
        await self._require_row(session, persona_id)
        row = (
            (
                await session.execute(
                    select(PersonaStateRow).where(
                        PersonaStateRow.persona_id == persona_id,
                        PersonaStateRow.layer == layer.value,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            row = PersonaStateRow(persona_id=persona_id, layer=layer.value, state=dto.state)
            session.add(row)
        else:
            row.state = dto.state
        await self._commit(session)
        return PersonaStateDTO.model_validate(row)

    async def get_state(
        self, session: AsyncSession, persona_id: int, layer: PersonaStateLayer
    ) -> PersonaStateDTO | None:
        """读取指定层状态（未写过该层返回 None——空态合法）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            layer: 状态层（M/S）。

        Returns:
            状态 DTO；该层未写过时为 None。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        await self._require_row(session, persona_id)
        row = (
            (
                await session.execute(
                    select(PersonaStateRow).where(
                        PersonaStateRow.persona_id == persona_id,
                        PersonaStateRow.layer == layer.value,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            return None
        return PersonaStateDTO.model_validate(row)

    async def get_states(self, session: AsyncSession, persona_id: int) -> list[PersonaStateDTO]:
        """读取人格全部已写状态层（layer 升序——M 在前 S 在后）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            状态 DTO 列表（仅已写过的层；空列表 = 全新人格零状态）。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        await self._require_row(session, persona_id)
        rows = list(
            (
                await session.execute(
                    select(PersonaStateRow)
                    .where(PersonaStateRow.persona_id == persona_id)
                    .order_by(PersonaStateRow.layer.asc())
                )
            )
            .scalars()
            .all()
        )
        return [PersonaStateDTO.model_validate(row) for row in rows]
