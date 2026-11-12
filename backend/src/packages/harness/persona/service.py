"""persona 域服务——人格注册表全生命周期管理（管理面真源）。

操作面：

    - 创建 / 更新 / 复制 / 删除（注册名唯一校验；复制可选深拷贝 M/S
      状态；删除有激活守卫）；
    - **事务性单激活切换**：``activate`` 在单事务内「全表清激活位 →
      激活目标」——激活 B 必然去激活 A（至多一个激活人格的不变式由
      事务原子性保证）；
    - 状态层读写（M/S upsert / 读取）；
    - 游标分页列表（status 过滤）；
    - 批量删除（部分成功形态——逐 id 独立事务，单项失败不阻塞其余）。

删除守卫（本工程锁定语义——**禁止删除当前激活人格**）：

    删除 ``is_active=True`` 的人格直接 ``CONFLICT`` 拒绝；调用方须先
    ``deactivate``（显式两步——避免「删除即静默掉线为零激活」的隐式
    状态跳变）。删除非激活人格时**同事务**清理其 M/S 状态行（SQLite
    测试方言默认不启用 FK 强制，服务层显式删除保证跨方言一致；DB 级
    ON DELETE CASCADE 仅作直删 SQL 的兜底）。

状态机约定：

    - ``status``（可用性）与 ``is_active``（单激活位）正交；激活要求
      ``status=active``（停用人格不可激活）；
    - ``status`` 切到 ``disabled`` 时若持有激活位则**自动清位**（停用
      即下线——杜绝「停用但仍激活」的矛盾态）；
    - 复制产物恒 ``disabled + is_active=False``（启用与激活均为显式
      后续动作）。

Session 契约：方法接收调用方注入的 :class:`AsyncSession`（app 层经
``get_async_db`` 依赖注入；测试直连共享引擎 Session）——服务层内部
显式 ``commit`` / 异常 ``rollback``（写路径事务边界归服务层，见
database.session.get_async_db 的分工约定）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core.context import RequestContext, bind_context
from packages.harness.core.dtos import PageResponse, decode_cursor, encode_cursor
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.persona.chara_card import CharaCardModel
from packages.harness.persona.dtos import (
    CharaCardPatchDTO,
    PersonaBatchDeleteItemDTO,
    PersonaBatchDeleteResponseDTO,
    PersonaCardDTO,
    PersonaCopyDTO,
    PersonaCreateDTO,
    PersonaQueryDTO,
    PersonaRecordDTO,
    PersonaStateDTO,
    PersonaStateUpsertDTO,
    PersonaUpdateDTO,
)
from packages.harness.persona.models import PersonaStateLayer, PersonaStatus
from packages.harness.persona.orm import PersonaRow, PersonaStateRow

__all__ = ["PersonaService", "bind_persona_context"]


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
        session: 待回滚的 :class:`AsyncSession`。
    """
    try:
        await session.rollback()
    except Exception:  # best-effort: 回滚失败由会话关闭兜底
        logger.opt(exception=True).debug("批量路径会话回滚失败（close 兜底）")


class PersonaService:
    """人格注册表服务（无状态——Session 由调用方注入，事务边界归本层）。"""

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
        """创建人格（注册名唯一校验；单激活位恒 False）。

        Args:
            session: 注入的异步 Session。
            dto: 创建 DTO（status 缺省 disabled；展示名缺省回落 card.name）。

        Returns:
            落库后的记录 DTO（含主键）。

        Raises:
            BusinessException: 注册名已占用（``CONFLICT``）。
        """
        await self._ensure_name_free(session, dto.name)
        row = PersonaRow(
            name=dto.name,
            display_name=dto.display_name or dto.card.name,
            status=dto.status.value,
            is_active=False,
            chara_card=dto.card.model_dump(),
        )
        session.add(row)
        await self._commit(session)
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

        重命名走唯一校验（排除自身）；status 切到 disabled 时若持有激活
        位自动清位（停用即下线）；卡片补丁合并语义见 :meth:`_merge_card`。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。
            dto: 更新 DTO（不含 is_active——单激活位仅经 activate /
                deactivate 事务性操作）。

        Returns:
            更新后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；注册名被占用
                （``CONFLICT``）；卡片合并校验失败（``VALIDATION_ERROR``）。
        """
        row = await self._require_row(session, persona_id)
        if dto.name is not None and dto.name != row.name:
            await self._ensure_name_free(session, dto.name, exclude_id=persona_id)
            row.name = dto.name
        if dto.display_name is not None:
            row.display_name = dto.display_name
        if dto.status is not None and dto.status.value != row.status:
            row.status = dto.status.value
            if dto.status == PersonaStatus.DISABLED and row.is_active:
                # 停用即下线：清激活位（杜绝「停用但仍激活」矛盾态）
                row.is_active = False
                logger.info("停用人格并自动清激活位: id={}", persona_id)
        if dto.card is not None:
            row.chara_card = self._merge_card(row.chara_card, dto.card)
        await self._commit(session)
        return PersonaRecordDTO.model_validate(row)

    async def copy(
        self, session: AsyncSession, persona_id: int, dto: PersonaCopyDTO
    ) -> PersonaRecordDTO:
        """复制人格（新注册名；可选深拷贝 M/S 状态；产物恒 disabled + 不激活）。

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
            is_active=False,
            chara_card=CharaCardModel.model_validate(source.chara_card).model_dump(),
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
                        state=dict(state_row.state),
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
        """删除人格（**激活守卫：当前激活人格禁止删除**——先 deactivate）。

        非激活人格删除时同事务清理其 M/S 状态行（跨方言一致——SQLite
        测试方言默认不启用 FK 强制，DB 级 CASCADE 仅作直删 SQL 兜底）。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；当前激活人格
                禁止删除（``CONFLICT``，details 携带指引）。
        """
        row = await self._require_row(session, persona_id)
        if row.is_active:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="不能删除当前激活人格，请先停用激活（deactivate）再删除",
                details={
                    "persona_id": persona_id,
                    "fix": "先调用 deactivate（POST /api/v1/personas/{id}/deactivate）",
                },
            )
        await session.execute(
            delete(PersonaStateRow).where(PersonaStateRow.persona_id == persona_id)
        )
        await session.delete(row)
        await self._commit(session)
        logger.info("人格已删除: id={}", persona_id)

    async def batch_delete(
        self, session: AsyncSession, ids: Sequence[int]
    ) -> PersonaBatchDeleteResponseDTO:
        """批量删除（部分成功形态——逐 id 独立事务，单项失败不阻塞其余）。

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
        succeeded = sum(1 for item in items if item.success)
        return PersonaBatchDeleteResponseDTO(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            items=items,
        )

    # ------------------------------------------------------------------
    # 单激活切换（事务性）
    # ------------------------------------------------------------------

    async def activate(self, session: AsyncSession, persona_id: int) -> PersonaRecordDTO:
        """激活人格（**事务性单激活切换**——激活 B 必然去激活 A）。

        单事务内「全表清激活位 → 激活目标」：任一步失败整体回滚，至多
        一个激活人格的不变式由事务原子性保证。目标须 ``status=active``
        （停用人格激活被拒）；重复激活当前激活人格为幂等 no-op。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            激活后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）；停用人格不可
                激活（``CONFLICT``）。
        """
        row = await self._require_row(session, persona_id)
        if row.status != PersonaStatus.ACTIVE.value:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message="停用状态的人格不可激活，请先启用（status=active）",
                details={"persona_id": persona_id, "status": row.status},
            )
        # 单事务切换：全表清位 + 激活目标（commit 原子提交）
        await session.execute(
            update(PersonaRow).where(PersonaRow.is_active.is_(True)).values(is_active=False)
        )
        row.is_active = True
        await self._commit(session)
        logger.info("人格已激活（单激活切换）: id={}", persona_id)
        return PersonaRecordDTO.model_validate(row)

    async def deactivate(self, session: AsyncSession, persona_id: int) -> PersonaRecordDTO:
        """停用激活（清目标激活位；幂等——未激活人格停用为 no-op）。

        停用后允许零激活人格（系统态）；重新激活经 :meth:`activate`。

        Args:
            session: 注入的异步 Session。
            persona_id: 人格主键。

        Returns:
            停用后的记录 DTO。

        Raises:
            BusinessException: 人格不存在（``NOT_FOUND``）。
        """
        row = await self._require_row(session, persona_id)
        row.is_active = False
        await self._commit(session)
        logger.info("人格激活位已停用: id={}", persona_id)
        return PersonaRecordDTO.model_validate(row)

    async def get_active_persona(self, session: AsyncSession) -> PersonaRecordDTO | None:
        """读取当前唯一激活人格（无激活人格返回 None——系统态）。

        Args:
            session: 注入的异步 Session。

        Returns:
            激活人格记录 DTO；零激活时为 None。
        """
        row = (
            (
                await session.execute(
                    select(PersonaRow).where(PersonaRow.is_active.is_(True)).limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return PersonaRecordDTO.model_validate(row)

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
