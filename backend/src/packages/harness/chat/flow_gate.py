"""chat 域心流门控纯函数——判定矩阵 / 连发合并 / 缓冲裁剪。

全纯函数（零 I/O / 零全局态 / 不读系统时钟）：判定时刻、会话节拍状态、
节律层快照与关系摘要全部由调用方注入；封装层（``ChannelRouter``）负责
把裁决映射到中央调度器排程与聊天生成。门控关闭（``enabled=False``）时
判定恒为立即回复，回复链路与引入前完全一致。

判定矩阵（顺序锁定——先命中者产出结论）::

    ① 门控未启用              → REPLY / DISABLED / 0
    ② 机器生成事件            → SKIP  / SYNTHETIC_EVENT / 0
    ③ 睡眠窗口（节律不允许）  → DEFER / SLEEP_WINDOW / 距醒来毫秒
    ④ 连发达到阈值            → MERGE / BURST_MERGE / 静默窗口剩余毫秒
    ⑤ 回复间隔过密（且关系非亲近 / 亲密）
                              → DEFER / DENSITY_PACING / 补齐最小间隔毫秒
    ⑥ 未读积压达到阈值（且关系非亲近 / 亲密）
                              → DEFER / BACKLOG_DEFER / 阶梯延迟（封顶）
    ⑦ 关系亲近 / 亲密且本应因密度或积压延迟
                              → REPLY / RELATIONSHIP_PRIORITY / 0
    ⑧ 节律允许但建议延迟 > 0  → DEFER / RHYTHM_DELAY / 节律建议延迟
    ⑨ 其余                    → REPLY / NORMAL / 0

关系摘要的角色（锁定解释）：``close`` / ``intimate`` 阶段的人先于积压与
刷屏节流被回应——亲近关系里「秒回」本身就是关系信号；睡眠窗口仍然优先
于关系（人在睡，关系再近也不叫醒）。陌生人 / 熟悉阶段照常受密度与积压
节流。

连发合并语义（锁定）：缓冲超过 ``merge_max_messages`` 时保留**最近**的
条目（对话恢复力优先——最新几句才承载当前诉求），丢弃最老条目并返回
丢弃条数供日志面记录；合并文本按入站顺序以换行连接。延迟任务执行前
若仍有新的入站落在静默窗口内，调用方按 :func:`remaining_quiet_ms` 重排，
不在本模块内等待。
"""

from __future__ import annotations

from packages.harness.chat.flow_models import (
    FLOW_ARRIVAL_HISTORY_LIMIT,
    FLOW_BUFFERED_TEXT_LIMIT,
    FlowChatStateModel,
    FlowGateDecisionModel,
    FlowGateInputModel,
    FlowGateSettings,
    FlowReason,
    FlowRhythmStateModel,
    FlowVerdict,
    MergedBurstModel,
)
from packages.harness.persona.relationship_models import RelationshipStage

__all__ = [
    "RELATIONSHIP_PRIORITY_STAGES",
    "evaluate_flow_gate",
    "record_inbound",
    "merge_buffered_texts",
    "remaining_quiet_ms",
    "mark_pending",
    "clear_pending",
    "clear_backlog",
    "rhythm_state_from_gate",
]

#: 触发「关系优先应答」的关系阶段集合（跳过密度与积压节流，睡眠窗口除外）。
RELATIONSHIP_PRIORITY_STAGES: tuple[RelationshipStage, ...] = (
    RelationshipStage.CLOSE,
    RelationshipStage.INTIMATE,
)


def evaluate_flow_gate(
    request: FlowGateInputModel, settings: FlowGateSettings
) -> FlowGateDecisionModel:
    """按锁定判定矩阵产出单条入站的门控裁决。

    判定输入显式区分「当前消息」与「之前的历史」：``prior_arrivals_ms``
    不含当前消息（路由器先取历史 → 门控 → 再记录当前）；不存在
    「末条是否为当前消息」的隐式推断。

    Args:
        request: 判定输入（时刻 / 节律快照 / 关系摘要 / 当前消息之前的
            入站历史与积压）。
        settings: 门控配置（阈值与开关——调用方注入，模型层不读环境）。

    Returns:
        :class:`~packages.harness.chat.flow_models.FlowGateDecisionModel`
        （裁决 + 延迟毫秒 + 理由枚举）。
    """
    if not settings.enabled:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.REPLY, delay_ms=0, reason=FlowReason.DISABLED
        )
    if request.is_synthetic:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.SKIP, delay_ms=0, reason=FlowReason.SYNTHETIC_EVENT
        )
    if not request.rhythm.allowed:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.DEFER,
            delay_ms=request.rhythm.suggested_delay_ms,
            reason=FlowReason.SLEEP_WINDOW,
        )
    window_start = request.now_ms - settings.burst_window_ms
    # prior_arrivals_ms 显式不含当前消息：窗口内历史 + 当前消息本身才构成
    # 连发计数（阈值语义与「当前已入历史」的旧形态一致，无分支推断）。
    recent_prior = [
        ts
        for ts in request.prior_arrivals_ms
        if window_start <= ts <= request.now_ms
    ]
    if len(recent_prior) + 1 >= settings.burst_count:
        # 静默窗口从当前消息到达时刻起算（当前恒为最新入站）。
        return FlowGateDecisionModel(
            verdict=FlowVerdict.MERGE,
            delay_ms=settings.merge_quiet_ms,
            reason=FlowReason.BURST_MERGE,
        )
    density_wait = 0
    if recent_prior and settings.min_reply_gap_ms > 0:
        # 密度判定锚点 = 当前消息之前最近一条入站（显式历史末条）。
        elapsed = request.now_ms - recent_prior[-1]
        if elapsed < settings.min_reply_gap_ms:
            density_wait = settings.min_reply_gap_ms - elapsed
    backlog_wait = 0
    if request.backlog_count >= settings.backlog_defer_threshold:
        overflow = request.backlog_count - settings.backlog_defer_threshold + 1
        backlog_wait = min(
            settings.backlog_defer_max_ms, settings.backlog_defer_step_ms * overflow
        )
    relationship_priority = (
        request.relationship is not None
        and request.relationship.stage in RELATIONSHIP_PRIORITY_STAGES
    )
    if relationship_priority and (density_wait > 0 or backlog_wait > 0):
        return FlowGateDecisionModel(
            verdict=FlowVerdict.REPLY,
            delay_ms=0,
            reason=FlowReason.RELATIONSHIP_PRIORITY,
        )
    if density_wait > 0:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.DEFER,
            delay_ms=density_wait,
            reason=FlowReason.DENSITY_PACING,
        )
    if backlog_wait > 0:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.DEFER,
            delay_ms=backlog_wait,
            reason=FlowReason.BACKLOG_DEFER,
        )
    if request.rhythm.suggested_delay_ms > 0:
        return FlowGateDecisionModel(
            verdict=FlowVerdict.DEFER,
            delay_ms=request.rhythm.suggested_delay_ms,
            reason=FlowReason.RHYTHM_DELAY,
        )
    return FlowGateDecisionModel(
        verdict=FlowVerdict.REPLY, delay_ms=0, reason=FlowReason.NORMAL
    )


def record_inbound(
    state: FlowChatStateModel, text: str, now_ms: int, *, append_text: bool = True
) -> FlowChatStateModel:
    """记录一条入站消息（到达时刻 + 积压递增 + 待回复缓冲追加）。

    纯函数：返回新状态，不改写入参。到达时刻保留最近
    :data:`~packages.harness.chat.flow_models.FLOW_ARRIVAL_HISTORY_LIMIT`
    条，缓冲保留最近
    :data:`~packages.harness.chat.flow_models.FLOW_BUFFERED_TEXT_LIMIT` 条
    （合并阶段的正式裁剪由 :func:`merge_buffered_texts` 按配置上限执行）。

    仅附件入站（``text=""``）以 ``append_text=False`` 记录：到达时刻与
    积压照常递增（连发窗口 / 节律判定不变），空文本不进入待回复缓冲——
    合并文本不会因空条目产生孤立换行，纯媒体批次也不会被误判为有待
    回复内容。

    Args:
        state: 既有会话节拍状态。
        text: 入站文本（非空原文；append_text=False 时仅作占位，不追加）。
        now_ms: 入站时刻（epoch 毫秒——调用方注入）。
        append_text: 是否把 ``text`` 追加进待回复缓冲（仅附件入站传
            False）。

    Returns:
        追加后的新状态（积压 +1，``pending`` / ``pending_due_ms`` 保持；
        append_text=False 时缓冲原样保留）。
    """
    arrivals = [*state.recent_arrival_ms, now_ms][-FLOW_ARRIVAL_HISTORY_LIMIT:]
    buffered = (
        [*state.buffered_texts, text][-FLOW_BUFFERED_TEXT_LIMIT:]
        if append_text
        else list(state.buffered_texts)
    )
    return state.model_copy(
        update={
            "recent_arrival_ms": arrivals,
            "backlog_count": state.backlog_count + 1,
            "buffered_texts": buffered,
        }
    )


def merge_buffered_texts(
    texts: list[str], settings: FlowGateSettings
) -> MergedBurstModel:
    """按上限裁剪并连接待回复缓冲文本（保留最近条目）。

    Args:
        texts: 待回复缓冲文本（入站顺序）。
        settings: 门控配置（``merge_max_messages`` 为保留上限）。

    Returns:
        :class:`~packages.harness.chat.flow_models.MergedBurstModel`
        （合并文本 + 实际条数 + 丢弃条数）；空缓冲返回空串与零计数。
    """
    cap = settings.merge_max_messages
    kept = texts[-cap:] if texts else []
    dropped = max(0, len(texts) - len(kept))
    return MergedBurstModel(
        text="\n".join(kept), used_count=len(kept), dropped_count=dropped
    )


def remaining_quiet_ms(
    state: FlowChatStateModel, now_ms: int, settings: FlowGateSettings
) -> int:
    """合并静默窗口的剩余毫秒（新入站后重排延迟任务用）。

    Args:
        state: 会话节拍状态（取最后一次到达时刻）。
        now_ms: 当前时刻（epoch 毫秒）。
        settings: 门控配置（``merge_quiet_ms`` 为静默窗口）。

    Returns:
        剩余毫秒；无入站记录或窗口已满返回 0。
    """
    if not state.recent_arrival_ms:
        return 0
    last = state.recent_arrival_ms[-1]
    return max(0, settings.merge_quiet_ms - (now_ms - last))


def mark_pending(
    state: FlowChatStateModel, *, due_ms: int
) -> FlowChatStateModel:
    """标记延迟任务已排程（``pending`` + 计划执行时刻）。

    Args:
        state: 会话节拍状态。
        due_ms: 计划执行时刻（epoch 毫秒）。

    Returns:
        标记后的新状态。
    """
    return state.model_copy(update={"pending": True, "pending_due_ms": due_ms})


def clear_pending(state: FlowChatStateModel) -> FlowChatStateModel:
    """清除延迟任务标记（任务执行或丢失重排时调用）。

    Args:
        state: 会话节拍状态。

    Returns:
        清除 ``pending`` 与计划执行时刻后的新状态。
    """
    return state.model_copy(update={"pending": False, "pending_due_ms": None})


def clear_backlog(state: FlowChatStateModel) -> FlowChatStateModel:
    """清空未读积压与待回复缓冲（回复成功后调用）。

    Args:
        state: 会话节拍状态。

    Returns:
        积压与缓冲清零后的新状态（到达时刻历史保留——密度判定仍需要
        最近节拍样本）。
    """
    return state.model_copy(update={"backlog_count": 0, "buffered_texts": []})


def rhythm_state_from_gate(
    *, allowed: bool, suggested_delay_ms: int
) -> FlowRhythmStateModel:
    """把节律门结果折算为门控输入的节律快照。

    Args:
        allowed: 节律门是否允许即时回复。
        suggested_delay_ms: 节律门建议延迟毫秒（非负）。

    Returns:
        :class:`~packages.harness.chat.flow_models.FlowRhythmStateModel`。
    """
    return FlowRhythmStateModel(
        allowed=allowed, suggested_delay_ms=max(0, suggested_delay_ms)
    )
