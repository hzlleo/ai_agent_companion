"""chat 域消息变体（swipes）服务——单回合多候选重生成 + 选定提升。

设计边界（锁定决策——docs/backend/chat.md「消息变体」单一真相）：

    - **上下文脊柱**：重生成只回放父回合之前的**正典对话链**（变体不
      进入上下文——每回合针对原始对话脊柱重掷；已选中的变体已就地替换
      正典正文，因此会自然进入后续回合的上下文）；
    - **生成路径**：与 ``ChatService.reply`` 同构的组装链（同一
      :func:`~packages.harness.chat.service.compose_messages`——人格 /
      卡片 / 世界书 / 心情摘要 / 关系进展全量复用，prompt 文本零变更）+
      网关 ``chat_by_role``（chat 角色 / ``purpose="chat"`` / persona_id
      显式直传 / 派生 ``intimacy_level`` 原样透传——端点裁决在 llm 网关）；
      **不装载工具面**（单轮候选生成——工具循环属 reply 的回合语义）；
      关系摘要经既有事实读取面与 ``build_relationship_summary`` 派生，
      读取面未接线时不注入关系块且路由输入为 None、读取失败降级空摘要；
      末位 user 回合沿用组装链的注入防御语义（定界包裹 + 检测提示）：本
      服务经构造注入 ``InjectionGuardSettings``（与 ``ChatService`` 同形
      态），``INJECTION_`` 配置的启用 / 阈值 / 风格 / 提示开关对变体路径
      同样生效；无来源分级信息时标签取 ``UNTRUSTED_LABEL_DEFAULT``，见
      docs/backend/defense.md；
    - **协议面**：每个候选独立经 ``parse_assistant_output`` 解析——可见
      正文落变体行 content，心声 / 情绪增量落 meta（附 ``variant_index``）；
      变体情绪增量**不推进共享 M 态**（swipes 是探索面——只有选定变体经
      select-variant 时推进；文档化边界）；
    - **持久化**：候选生成阶段全部成功后才逐条落库（网关中途失败零残留
      变体行）；变体行 seq = 父回合 seq、``variant_of_id`` 指向父回合、
      ``variant_index`` 从既有最大值续编（不清洗旧候选——历史全部保留）；
    - **选定提升**：变体正文 / 元数据就地替换正典回合（父行幸存且保持
      正典身份），并**唯一一次**把该变体的情绪增量推进 M 态、快照写回
      父行 meta（``selected_variant_id`` 注记）；变体行原样保留（历史
      可回溯——不清洗，文档化决策）。

人格作用域：调用方须先经 ``bind_persona_context`` 绑定目标人格并确认
会话归属（app 层经 ``ChatSessionService.ensure_session``）；``persona``
入参为解析后的人格记录（卡片当前正典用于组装）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.chat.chat_models import ChatSessionStateModel
from packages.harness.chat.injection_guard import build_injection_hint, detect_injection
from packages.harness.chat.injection_models import InjectionGuardSettings
from packages.harness.chat.output_protocol import parse_assistant_output
from packages.harness.chat.prompts import UNTRUSTED_LABEL_DEFAULT
from packages.harness.chat.service import (
    CHAT_PURPOSE,
    FactsReaderFace,
    compose_messages,
    derive_relationship_summary,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.core import time_utils
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.emotion.service import EmotionService
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import ChatMessageRole
from packages.harness.memory.dtos import ChatMessageDTO
from packages.harness.memory.orm import ChatMessageRow
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.relationship import empty_relationship_summary
from packages.harness.persona.relationship_models import (
    DEFAULT_RELATIONSHIP_THRESHOLDS,
    RelationshipSummaryModel,
    RelationshipThresholdsModel,
)
from packages.harness.persona.service import bind_persona_context

__all__ = ["SwipeService"]


class SwipeService:
    """消息变体服务（重生成候选 / 选定提升——无内部状态，Session 由调用方注入）。

    构造注入网关 / 会话持久化服务 / 情绪引擎（测试可全量 fake）；网关
    调用与 ``ChatService.reply`` 同参数面（角色 / 用途 / persona 直传），
    组装链经同一 ``compose_messages`` 纯函数——prompt 字节零变更。
    """

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        session_service: ChatSessionService,
        emotion_service: EmotionService | None = None,
        facts_reader: FactsReaderFace | None = None,
        relationship_thresholds: RelationshipThresholdsModel | None = None,
        injection_settings: InjectionGuardSettings | None = None,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（chat 角色调用面——与 reply 同路径）。
            session_service: 会话持久化服务（变体读写 / 正典读取 / 选定
                替换的唯一通道）。
            emotion_service: 轻情绪引擎（None = 现场构造默认实例——读
                ``EMOTION_`` env；用于组装心情摘要与选定时的 M 态推进）。
            facts_reader: 人物事实读取面（None = 未接线——不注入关系进展
                块、亲密通道路由输入为 None，与既有降级语义一致）。
            relationship_thresholds: 关系进展阈值表（None = 锁定缺省表）。
            injection_settings: 注入防御配置（None = 现场构造默认实例——
                读 ``INJECTION_`` env；与 ``ChatService`` 同形态，变体路径
                的包裹 / 检测 / 提示完全按该配置生效）。
        """
        self._gateway = gateway
        self._session_service = session_service
        self._emotion_service = (
            emotion_service if emotion_service is not None else EmotionService()
        )
        self._facts_reader = facts_reader
        self._relationship_thresholds = (
            relationship_thresholds
            if relationship_thresholds is not None
            else DEFAULT_RELATIONSHIP_THRESHOLDS
        )
        self._injection_settings = (
            injection_settings if injection_settings is not None else InjectionGuardSettings()
        )

    # ------------------------------------------------------------------
    # 重生成
    # ------------------------------------------------------------------

    async def regenerate_variants(
        self,
        db_session: AsyncSession,
        *,
        session_id: int,
        parent_message_id: int,
        persona: PersonaRecordDTO,
        count: int = 4,
    ) -> list[ChatMessageDTO]:
        """为单个 assistant 回合生成 N 条候选变体（落库并返回创建结果）。

        组装上下文 = 父回合之前的正典对话链（seq 升序、变体不参与）+
        当前人格卡片 / 世界书 / 情绪摘要；同一组装结果重复调用网关
        ``count`` 次（同上下文多采样）。全部候选生成成功后才逐条落库；
        任一次网关异常或空正文即整体失败（零变体行残留——用户回合与
        父回合不受影响）。

        Args:
            db_session: 注入的异步 Session（调用方已绑定人格上下文）。
            session_id: 会话主键。
            parent_message_id: 父回合主键（须为 assistant 正典链行）。
            persona: 会话归属人格记录（卡片正典 + 网关 persona_id 面）。
            count: 候选数量（1-8；DTO 边界已校验，本方法防御性复核）。

        Returns:
            新建变体消息 DTO 列表（variant_index 升序续编）。

        Raises:
            BusinessException: 数量越界 / 父回合非 assistant / 父回合为
                变体（``VALIDATION_ERROR``）；父回合不存在（``NOT_FOUND``）；
                模型返回空正文（``LLM_CALL_FAILED``）；网关异常透传；
                并发生成撞变体序号（``INTERNAL_ERROR``）。
        """
        if not 1 <= count <= 8:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="变体生成数量须在 1-8 之间",
                details={"count": count},
            )
        parent = await self._require_canonical_assistant(
            db_session, session_id, parent_message_id
        )
        history = await self._session_service.list_canonical_messages_before(
            db_session, session_id, before_seq=parent.seq
        )
        state = ChatSessionStateModel(
            persona_id=persona.id,
            persona_name=persona.name,
            display_name=persona.display_name,
            card=persona.card,
            session_id=session_id,
        )
        for message in history:
            if message.role == ChatMessageRole.SYSTEM:
                continue
            try:
                state.append_turn(message.role, message.content)
            except ValueError:
                # 历史行正文为空属库内异常形态——跳过不阻断（组装链宽容）。
                logger.debug(
                    "变体组装跳过空正文历史行: session_id={}, message_id={}",
                    session_id,
                    message.id,
                )
        emotion_summary = (
            await self._emotion_service.summary(persona.id)
            if self._emotion_service.enabled
            else None
        )
        relationship: RelationshipSummaryModel | None = None
        if self._facts_reader is not None:
            now_ms = time_utils.now_ms()
            try:
                async with bind_persona_context(persona.id):
                    relationship = await derive_relationship_summary(
                        db_session,
                        self._facts_reader,
                        now_ms=now_ms,
                        thresholds=self._relationship_thresholds,
                    )
            except Exception:  # best-effort：派生失败降级空摘要，不阻断变体生成
                logger.opt(exception=True).warning(
                    "变体重生成的关系进展派生失败（降级为空摘要）: persona_id={}",
                    persona.id,
                )
                relationship = empty_relationship_summary(
                    now_ms=now_ms, thresholds=self._relationship_thresholds
                )
        intimacy_level = relationship.intimacy_level if relationship is not None else None
        injection = self._injection_settings
        last_turn = state.turns[-1] if state.turns else None
        injection_hint: str | None = None
        if (
            injection.enabled
            and last_turn is not None
            and last_turn.role == ChatMessageRole.USER
        ):
            signal = detect_injection(last_turn.content, threshold=injection.threshold)
            if signal.suspected:
                logger.debug(
                    "变体重生成注入信号命中: persona_id={}, score={:.2f}, kinds={}",
                    persona.id,
                    signal.score,
                    [reason.kind.value for reason in signal.reasons],
                )
                if injection.anti_injection_hint:
                    injection_hint = build_injection_hint(
                        injection.counter_style, last_turn.content
                    )
        composition = compose_messages(
            state,
            emotion_summary=emotion_summary,
            relationship=relationship,
            wrap_current_message=injection.enabled,
            current_message_label=UNTRUSTED_LABEL_DEFAULT,
            injection_hint=injection_hint,
        )
        candidates: list[tuple[str, dict[str, Any]]] = []
        for _ in range(count):
            response = await self._gateway.chat_by_role(
                ModelRole.CHAT,
                composition.messages,
                persona_id=str(persona.id),
                purpose=CHAT_PURPOSE,
                intimacy_level=intimacy_level,
            )
            parsed = parse_assistant_output(response.content)
            if not parsed.visible_text:
                raise BusinessException(
                    code=ErrorCode.LLM_CALL_FAILED,
                    message="模型返回空变体回复",
                    details={"persona_id": persona.id, "message_id": parent_message_id},
                )
            candidates.append(
                (
                    parsed.visible_text,
                    {
                        "thought": parsed.thought,
                        "emotion_deltas": dict(parsed.emotion_deltas),
                        # 变体不推进 M 态——快照恒 None，选定后才产生。
                        "emotion_snapshot": None,
                    },
                )
            )
        next_index = await self._session_service.next_variant_index(
            db_session, session_id, parent_message_id
        )
        created: list[ChatMessageDTO] = []
        for offset, (content, meta) in enumerate(candidates):
            index = next_index + offset
            created.append(
                await self._session_service.append_variant(
                    db_session,
                    session_id,
                    parent_message_id=parent_message_id,
                    variant_index=index,
                    content=content,
                    meta={**meta, "variant_index": index},
                )
            )
        logger.info(
            "消息变体已生成: session_id={}, parent_message_id={}, count={}",
            session_id,
            parent_message_id,
            len(created),
        )
        return created

    # ------------------------------------------------------------------
    # 选定提升
    # ------------------------------------------------------------------

    async def select_variant(
        self,
        db_session: AsyncSession,
        *,
        session_id: int,
        parent_message_id: int,
        variant_id: int,
        persona: PersonaRecordDTO,
    ) -> ChatMessageDTO:
        """选定变体并提升为正典回合内容（唯一 M 态推进写点）。

        语义（锁定）：变体可见正文与协议元数据替换父回合行内内容（父行
        幸存、正典链身份不变）；该变体的情绪增量在此刻**唯一一次**推进
        共享 M 态，推进后快照与 ``selected_variant_id`` 注记写回父行
        meta；变体行原样保留（历史可回溯，不清洗）。情绪引擎停用 / 无
        增量时零 M 态写入（快照 None）。

        Args:
            db_session: 注入的异步 Session（调用方已绑定人格上下文）。
            session_id: 会话主键。
            parent_message_id: 父回合主键（须为 assistant 正典链行）。
            variant_id: 待选中的变体主键（须属同一父回合）。
            persona: 会话归属人格记录（M 态推进的 persona 作用域）。

        Returns:
            更新后的正典消息 DTO（content / meta 为选中变体形态）。

        Raises:
            BusinessException: 父回合非 assistant / 为变体
                （``VALIDATION_ERROR``）；父回合 / 变体缺失或变体不属该
                回合（``NOT_FOUND``）。
        """
        parent = await self._require_canonical_assistant(
            db_session, session_id, parent_message_id
        )
        variant = await self._session_service.require_message(
            db_session, session_id, variant_id
        )
        if variant.variant_of_id != parent.id:
            raise BusinessException(
                code=ErrorCode.NOT_FOUND,
                message="变体不存在或不属于目标回合",
                details={"message_id": parent_message_id, "variant_id": variant_id},
            )
        meta: dict[str, Any] = dict(variant.meta or {})
        deltas: dict[str, float] = {}
        raw_deltas = meta.get("emotion_deltas")
        if isinstance(raw_deltas, dict):
            for dimension, value in raw_deltas.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    deltas[str(dimension)] = float(value)
        snapshot: dict[str, float] | None = None
        if deltas and self._emotion_service.enabled:
            advanced = await self._emotion_service.apply_deltas(persona.id, deltas)
            if advanced is not None:
                snapshot = dict(advanced.values)
        selected_meta: dict[str, Any] = {
            "thought": meta.get("thought"),
            "emotion_deltas": deltas,
            "emotion_snapshot": snapshot,
            "variant_index": variant.variant_index,
            "selected_variant_id": variant.id,
        }
        updated = await self._session_service.replace_message(
            db_session,
            session_id,
            parent_message_id,
            content=variant.content,
            meta=selected_meta,
        )
        logger.info(
            "消息变体已选中: session_id={}, parent_message_id={}, variant_id={}",
            session_id,
            parent_message_id,
            variant_id,
        )
        return updated

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _require_canonical_assistant(
        self, db_session: AsyncSession, session_id: int, message_id: int
    ) -> ChatMessageRow:
        """取父回合行并校验「assistant + 正典链」语义（变体不可作父回合）。

        Args:
            db_session: 注入的异步 Session。
            session_id: 会话主键。
            message_id: 消息主键。

        Returns:
            父回合 ORM 行。

        Raises:
            BusinessException: 消息不存在或不属于该会话（``NOT_FOUND``）；
                非 assistant / 本身是变体（``VALIDATION_ERROR``）。
        """
        row = await self._session_service.require_message(db_session, session_id, message_id)
        if row.role != ChatMessageRole.ASSISTANT.value:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="仅 assistant 回合可生成 / 选定变体",
                details={"message_id": message_id, "role": row.role},
            )
        if row.variant_of_id is not None:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="变体不可再生成变体（父回合须为正典链行）",
                details={"message_id": message_id},
            )
        return row
