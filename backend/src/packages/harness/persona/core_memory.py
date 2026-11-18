"""persona 域核心记忆服务——自编辑核心记忆块 / 修订链 / 工具注册面。

职责链（管理面 API / 面板 / 对话工具 / 聊天组装共用同一服务）::

    list_blocks(session)                     读取四块（首次读取懒建空行）
    owner_update(session, block, dto)        主人编辑（可清空 / 可锁定）
    agent_edit(session, dto, session_id=?)   人格自编辑（锁定 / 超限 → ok=False）
    list_revisions(session, block, limit=?)  修订历史（最新在前）
    register_core_memory_tools(...)          注册 ``edit_core_memory`` 工具

写入语义（锁定）：

    - **块集合固定**：四个块键由
      :class:`~packages.harness.persona.core_memory_models.CoreBlockKey`
      锁定；首次读取（任何读 / 写入口）先补齐四个空行（懒建种子——
      调用方无需处理「块尚未创建」），并发懒建撞唯一键时静默收敛
      （另一请求已完成种子，重读即可）；
    - **版本与修订**：任何内容变更或锁定状态变更推进 ``version``；内容
      变更时同步插入一条修订行（写入后全文 + 当次版本号 + 写入者 +
      来源 + 会话主键 + 时刻）；**只切换锁定、正文不变时不产生修订**
      （修订日志记录「文本历史」，锁定开关只是状态位）；
    - **配对强制**：管理面编辑恒 ``owner`` / ``admin``；工具编辑恒
      ``agent`` / ``chat``——两个轴不允许交叉组合；
    - **persona 内容上限**：人格写入须非空（1-上限）、主人可清空（0-上限）；
      上限是服务构造旋钮（缺省
      :data:`~packages.harness.persona.core_memory_models.CORE_MEMORY_CONTENT_MAX_LENGTH`），
      追加 / 替换都在**落库前**按目标全文计长——超限不改任何行；
    - **锁定闸（人格侧）**：锁定块的 ``agent_edit`` 恒返回 ``ok=False``
      且不抛异常（LLM 工具路径需要可读反馈而非错误中断）；解锁与改写
      只能由主人经管理面完成；
    - **append 语义**：既有正文非空时以换行分隔追加（保留原文——人格
      的增量式自我补充不应覆盖既有叙述）；空块直接写入该段。

工具面（:func:`register_core_memory_tools`）：注册与工具名
:data:`~packages.harness.persona.core_memory_models.CORE_MEMORY_TOOL_NAME`
同名的单工具，执行期经 ``session_id_provider`` 读取当前对话会话主键
（聊天服务在工具循环期间绑定——见 ``chat.service``）。**结构性的写入
闸不在工具内**：聊天服务按发送者来源分级（``sender_provenance``）从
单轮工具列表里直接移除本工具——群聊 peer 的回合根本看不到它，模型
无从「忘记不调用」。

人格作用域：与记忆 / 事实 / 技能域同轴（PersonaMixin 自动注入 / 过滤）
——调用方须先经 ``bind_persona_context`` 绑定上下文，否则 fail loud。
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.core import time_utils
from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import session_scope
from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.persona.core_memory_models import (
    CORE_BLOCK_LABELS,
    CORE_BLOCK_ORDER,
    CORE_MEMORY_CONTENT_MAX_LENGTH,
    CORE_MEMORY_REVISIONS_PAGE_LIMIT,
    CORE_MEMORY_TOOL_NAME,
    AgentEditMode,
    AgentEditModel,
    AgentEditResultModel,
    CoreBlockKey,
    CoreMemoryActor,
    CoreMemoryBlockDTO,
    CoreMemoryRevDTO,
    CoreMemorySource,
    OwnerEditModel,
)
from packages.harness.persona.core_memory_orm import (
    CoreMemoryBlockRow,
    CoreMemoryRevisionRow,
)
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolExecutor, ToolSpecModel

__all__ = [
    "core_memory_session_id_var",
    "CoreMemoryService",
    "register_core_memory_tools",
]

#: 当前对话会话主键的上下文绑定面（工具执行期读取——聊天服务在工具循环
#: 期间设置 / 复位；None 表示工具不在对话回合内执行）。
core_memory_session_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "core_memory_session_id", default=None
)

#: edit_core_memory 参数 JSON Schema（与 AgentEditModel 字段对齐）。
_CORE_MEMORY_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "block": {
                "type": "string",
                "description": "目标记忆块：self_identity 自我认知 / owner_profile 关于对方 / "
                "relationship_story 我们的故事 / open_notes 备忘",
                "enum": [key.value for key in CORE_BLOCK_ORDER],
            },
            "content": {
                "type": "string",
                "description": "要写入的内容（1-2000 字符）",
                "minLength": 1,
                "maxLength": CORE_MEMORY_CONTENT_MAX_LENGTH,
            },
            "mode": {
                "type": "string",
                "description": "写入方式：replace 整块替换（默认）/ append 末尾追加",
                "enum": [mode.value for mode in AgentEditMode],
            },
        },
        "required": ["block", "content"],
        "additionalProperties": False,
    }
)

#: edit_core_memory 工具描述（进入 LLM 请求 tools 面——调用时机说明）。
_CORE_MEMORY_TOOL_DESCRIPTION: str = (
    "维护你的长期核心记忆：记录稳定的自我认知、对对方的了解、共同经历与长期备忘。"
    "只记录稳定不变的信息，不要记录一时的情绪或寒暄；写满时可先精简再写入。"
)


def _block_record(row: CoreMemoryBlockRow) -> CoreMemoryBlockDTO:
    """块行 → 读取 DTO（块键 / 正文 / 锁定态 / 版本 / 修订归属面归一）。

    Args:
        row: 核心记忆块行。

    Returns:
        核心记忆块 DTO。
    """
    return CoreMemoryBlockDTO(
        id=row.id,
        block=CoreBlockKey(row.block_key),
        content=row.content,
        locked=row.locked,
        version=row.version,
        updated_by=CoreMemoryActor(row.updated_by),
        last_edited_session_id=row.last_edited_session_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_record(row: CoreMemoryRevisionRow, block: CoreBlockKey) -> CoreMemoryRevDTO:
    """修订行 → 读取 DTO（块键由调用方按已作用域化块行传入）。

    Args:
        row: 修订行。
        block: 修订所属块键（调用方已按人格作用域解析）。

    Returns:
        修订记录 DTO。
    """
    return CoreMemoryRevDTO(
        id=row.id,
        block=block,
        version=row.version,
        content=row.content,
        provenance=CoreMemoryActor(row.provenance),
        source=CoreMemorySource(row.source),
        session_id=row.session_id,
        ts_ms=row.ts_ms,
        created_at=row.created_at,
    )


class CoreMemoryService:
    """核心记忆服务（四块读取 / 主人编辑 / 人格自编辑 / 修订读取）。

    构造注入：``content_max_length``（内容上限——缺省 2000，测试与未来
    更窄部署可收窄）；``clock``（None = 当前真实时间——修订时刻，测试
    注入可控）。服务自身无状态（Session 逐次注入），可安全跨请求共享。
    """

    def __init__(
        self,
        *,
        content_max_length: int = CORE_MEMORY_CONTENT_MAX_LENGTH,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            content_max_length: 块正文上限（构造旋钮——主人与人格写入
                共同硬边界；须为正整数）。
            clock: 注入时钟（epoch 毫秒；None = 当前真实时间——修订时刻
                的参考轴）。

        Raises:
            BusinessException: 上限非正（``INTERNAL_ERROR``——装配面错误）。
        """
        if content_max_length <= 0:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="核心记忆内容上限须为正整数",
                details={"content_max_length": content_max_length},
            )
        self._max_content_length = content_max_length
        self._clock = clock if clock is not None else time_utils.now_ms

    # ------------------------------------------------------------------
    # 读取面——四块 / 修订历史
    # ------------------------------------------------------------------

    async def list_blocks(self, session: AsyncSession) -> list[CoreMemoryBlockDTO]:
        """读取四个核心记忆块（首次读取懒建空行；固定块序返回）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            四个块记录（按 :data:`~packages.harness.persona.core_memory_models.CORE_BLOCK_ORDER`
            排列——自我认知 / 关于对方 / 我们的故事 / 备忘）。
        """
        await self._ensure_seeded(session)
        rows = list((await session.execute(select(CoreMemoryBlockRow))).scalars().all())
        by_key = {CoreBlockKey(row.block_key): row for row in rows}
        return [_block_record(by_key[key]) for key in CORE_BLOCK_ORDER if key in by_key]

    async def list_revisions(
        self,
        session: AsyncSession,
        block: CoreBlockKey,
        *,
        limit: int = CORE_MEMORY_REVISIONS_PAGE_LIMIT,
    ) -> list[CoreMemoryRevDTO]:
        """读取块的修订历史（最新在前——版本 / 主键双键倒序）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            block: 块键。
            limit: 返回条数上限（缺省 20——面板抽屉的缺省页大小）。

        Returns:
            修订记录列表（新版在前；空列表 = 该块尚无内容变更）。
        """
        await self._ensure_seeded(session)
        row = await self._require_block(session, block)
        stmt = (
            select(CoreMemoryRevisionRow)
            .where(CoreMemoryRevisionRow.block_id == row.id)
            .order_by(desc(CoreMemoryRevisionRow.version), desc(CoreMemoryRevisionRow.id))
            .limit(max(limit, 1))
        )
        revisions = list((await session.execute(stmt)).scalars().all())
        return [_revision_record(revision, block) for revision in revisions]

    # ------------------------------------------------------------------
    # 写入面——主人编辑 / 人格自编辑
    # ------------------------------------------------------------------

    async def owner_update(
        self, session: AsyncSession, block: CoreBlockKey, dto: OwnerEditModel
    ) -> CoreMemoryBlockDTO:
        """主人编辑块（可清空正文 / 可切换锁定；内容变更留修订）。

        语义：正文与既有值相同且未提供锁定开关时为零变更成功（不推进
        版本、不写修订——面板重复保存幂等）；内容变更推进版本并写一条
        ``owner`` / ``admin`` 修订；仅锁定开关变更也推进版本，但**不写
        修订**（修订日志只记录文本历史）。管理面编辑不是对话行为——
        ``last_edited_session_id`` 归 None。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            block: 块键。
            dto: 编辑入参（新正文 0-上限 / 可选锁定开关）。

        Returns:
            编辑后的块记录（零变更时原样返回）。

        Raises:
            BusinessException: 正文超出构造上限（``VALIDATION_ERROR``）；
                块行缺失（``NOT_FOUND``——懒建后不应出现，防御面）。
        """
        if len(dto.content) > self._max_content_length:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"核心记忆块正文不能超过 {self._max_content_length} 个字符",
                details={"length": len(dto.content), "max": self._max_content_length},
            )
        await self._ensure_seeded(session)
        row = await self._require_block(session, block)
        content_changed = dto.content != row.content
        locked_changed = dto.locked is not None and dto.locked != row.locked
        if not content_changed and not locked_changed:
            return _block_record(row)
        if content_changed:
            row.content = dto.content
        if dto.locked is not None and dto.locked != row.locked:
            row.locked = dto.locked
        row.version += 1
        row.updated_by = CoreMemoryActor.OWNER.value
        row.last_edited_session_id = None
        if content_changed:
            self._add_revision(
                session,
                row,
                provenance=CoreMemoryActor.OWNER,
                source=CoreMemorySource.ADMIN,
                session_id=None,
            )
        await self._commit(session)
        logger.info(
            "核心记忆块已由主人编辑: block={}, version={}, locked={}, content_changed={}",
            block.value,
            row.version,
            row.locked,
            content_changed,
        )
        return _block_record(row)

    async def agent_edit(
        self,
        session: AsyncSession,
        dto: AgentEditModel,
        *,
        session_id: int | None = None,
    ) -> AgentEditResultModel:
        """人格自编辑块（锁定 / 超限 → ``ok=False``，不向 LLM 路径抛异常）。

        写入口径：锁定块直接拒绝（中文反馈含块名）；``append`` 以换行
        分隔在既有正文后追加、``replace`` 整块替换；两种模式都按**目标
        全文**在落库前计长，超限即拒绝且不改任何行。成功推进版本、
        ``updated_by=agent``、记录 ``last_edited_session_id`` 并写一条
        ``agent`` / ``chat`` 修订（含会话主键）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            dto: 工具入参（块键 / 正文 / 模式）。
            session_id: 当前对话会话主键（None = 无对话上下文——仍允许
                写入，修订的 ``session_id`` 记 NULL）。

        Returns:
            编辑结果（成功含结果长度；锁定 / 超限为 ``ok=False`` 的可读
            反馈——模型可换块或精简后重试）。
        """
        await self._ensure_seeded(session)
        row = await self._require_block(session, dto.block)
        label = CORE_BLOCK_LABELS[dto.block]
        if row.locked:
            return AgentEditResultModel(
                ok=False,
                message=f"核心记忆块「{label}」已锁定，仅主人可修改",
                length=len(row.content),
            )
        candidate = (
            dto.content
            if dto.mode is AgentEditMode.REPLACE or not row.content
            else f"{row.content}\n{dto.content}"
        )
        if len(candidate) > self._max_content_length:
            return AgentEditResultModel(
                ok=False,
                message=(
                    f"核心记忆块「{label}」写入后将超出上限"
                    f"（当前 {len(row.content)} 字，上限 {self._max_content_length} 字）"
                ),
                length=len(row.content),
            )
        row.content = candidate
        row.version += 1
        row.updated_by = CoreMemoryActor.AGENT.value
        row.last_edited_session_id = session_id
        self._add_revision(
            session,
            row,
            provenance=CoreMemoryActor.AGENT,
            source=CoreMemorySource.CHAT,
            session_id=session_id,
        )
        await self._commit(session)
        logger.info(
            "核心记忆块已由人格编辑: block={}, version={}, mode={}, length={}",
            dto.block.value,
            row.version,
            dto.mode.value,
            len(candidate),
        )
        return AgentEditResultModel(
            ok=True,
            message=f"核心记忆块「{label}」已更新（当前 {len(candidate)} 字）",
            length=len(candidate),
        )

    # ------------------------------------------------------------------
    # 内部——懒建种子 / 行定位 / 修订写入 / 事务
    # ------------------------------------------------------------------

    async def _ensure_seeded(self, session: AsyncSession) -> None:
        """补齐四个空块行（懒建——并发唯一键冲突静默收敛）。

        种子语义：缺失的块键插入 ``content=''`` / ``locked=False`` /
        ``version=1`` / ``updated_by=owner``（系统代主人初始化）行；撞
        ``uq_ff_core_memory_blocks_key`` 说明另一请求已完成种子——回滚
        后由调用方照常读取（幂等）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Raises:
            BusinessException: 人格上下文缺失（PersonaMixin 守卫——透传）。
        """
        rows = list((await session.execute(select(CoreMemoryBlockRow))).scalars().all())
        existing = {row.block_key for row in rows}
        missing = [key for key in CORE_BLOCK_ORDER if key.value not in existing]
        if not missing:
            return
        session.add_all(
            [
                CoreMemoryBlockRow(
                    block_key=key.value,
                    content="",
                    locked=False,
                    version=1,
                    updated_by=CoreMemoryActor.OWNER.value,
                    last_edited_session_id=None,
                )
                for key in missing
            ]
        )
        try:
            await session.commit()
        except IntegrityError:
            await self._rollback_quietly(session)
            logger.info(
                "核心记忆块并发懒建冲突（另一请求已完成种子——重读收敛）: keys={}",
                [key.value for key in missing],
            )

    async def _require_block(
        self, session: AsyncSession, block: CoreBlockKey
    ) -> CoreMemoryBlockRow:
        """按块键取块行（人格作用域内——缺失即 NOT_FOUND fail loud）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            block: 块键。

        Returns:
            块行。

        Raises:
            BusinessException: 块行不存在（``NOT_FOUND``——懒建后不应
                出现，属防御面）。
        """
        row = (
            (
                await session.execute(
                    select(CoreMemoryBlockRow).where(
                        CoreMemoryBlockRow.block_key == block.value
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="核心记忆块不存在",
                details={"block": block.value},
            )
        return row

    def _add_revision(
        self,
        session: AsyncSession,
        row: CoreMemoryBlockRow,
        *,
        provenance: CoreMemoryActor,
        source: CoreMemorySource,
        session_id: int | None,
    ) -> None:
        """追加一条修订行（写入后全文 + 当次版本号——只插入不更新）。

        Args:
            session: 注入的异步 Session。
            row: 已完成内容 / 版本变更的持久化块行（主键可取；修订与块
                变更同事务提交）。
            provenance: 修订写入者（owner / agent）。
            source: 修订来源（admin / chat）。
            session_id: 对话内编辑的会话主键（管理面为 None）。
        """
        session.add(
            CoreMemoryRevisionRow(
                block_id=row.id,
                version=row.version,
                content=row.content,
                provenance=provenance.value,
                source=source.value,
                session_id=session_id,
                ts_ms=self._clock(),
            )
        )

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
            await CoreMemoryService._rollback_quietly(session)
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
            logger.opt(exception=True).debug("核心记忆路径会话回滚失败（close 兜底）")


def register_core_memory_tools(
    registry: ToolRegistry,
    service: CoreMemoryService,
    session_id_provider: Callable[[], int | None],
) -> ToolSpecModel:
    """注册 ``edit_core_memory`` 工具（人格自编辑核心记忆的 LLM 工具面）。

    执行语义：参数经 registry 的 args_model 校验物化 → 读取人格上下文
    （缺失即 ``PERSONA_CONTEXT_REQUIRED``——registry 归一 ``ok=False``）
    → 开独立短事务调用 :meth:`CoreMemoryService.agent_edit`（锁定 / 超限
    的可读拒绝由服务返回，不进入异常面）→ 结果以模型可读取的 JSON 形态
    回填工具轮。

    **写入闸不在本函数**：聊天服务按本轮发送者来源分级从工具列表里移除
    本工具（群聊 peer 回合结构性不可见）——本工具只负责「被调用时正确
    执行」。

    Args:
        registry: 工具注册表（聊天注册表装配目标——duty 注册表不注册）。
        service: 核心记忆服务（无状态共享实例）。
        session_id_provider: 当前对话会话主键读取面（聊天服务在工具循环
            期间绑定 ``core_memory_session_id_var``；None = 无对话上下文）。

    Returns:
        已注册的工具规格（调用方按需并入 chat 工具列表）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（参数已经 registry 校验物化——block / content / mode 直读）。"""
        persona_scope = persona_id_var.get()
        if persona_scope is None:
            raise BusinessException(
                code=ErrorCode.PERSONA_CONTEXT_REQUIRED,
                message="edit_core_memory 缺少人格上下文（调用方须先绑定人格上下文）",
            )
        edit = AgentEditModel.model_validate(args)
        async with session_scope() as db_session:
            result = await service.agent_edit(
                db_session, edit, session_id=session_id_provider()
            )
        return result.model_dump(mode="json")

    spec = ToolSpecModel(
        name=CORE_MEMORY_TOOL_NAME,
        description=_CORE_MEMORY_TOOL_DESCRIPTION,
        args_schema=_CORE_MEMORY_SCHEMA,
        args_model=AgentEditModel,
    )
    executor: ToolExecutor = _execute
    registry.register(spec, executor)
    return spec
