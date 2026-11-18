"""chat 域群聊多人格选择纯函数——轮转 / 冷却 / 重复抑制。

确定性选择算法（零 I/O / 不读系统时钟；``now_ms`` 与应答记忆由调用方
注入）。规则顺序（锁定）::

    ① 未启用 / 单聊 → 空选择（调用方走既有单人格路径）
    ② 无候选        → 空选择
    ③ 冷却过滤      最近应答距今 < cooldown_ms 的人格排除
    ④ 排序          （上一轮未发言者优先，其次人格主键升序）
    ⑤ 取前 max_responders 个（去重——同一轮内互斥，一人至多应答一次）
    ⑥ 兜底          全部处于冷却时选最近应答时刻最早者（保证群里有回应）

选择结果交给调用方逐人格生成回复；每个应答人格拥有自己的持久化会话
（见 :mod:`packages.harness.chat.persona_rotation_models` 的归属说明）。
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.harness.chat.persona_rotation_models import (
    GroupPersonaSelectionModel,
    GroupPersonaSettings,
    GroupResponderCandidateModel,
    GroupResponderStateModel,
    GroupSelectionReason,
)

__all__ = ["select_group_responders"]


def select_group_responders(
    candidates: Sequence[GroupResponderCandidateModel],
    state: GroupResponderStateModel,
    settings: GroupPersonaSettings,
    *,
    now_ms: int,
    is_group_chat: bool,
) -> GroupPersonaSelectionModel:
    """选择本轮群聊应答人格（冷却 + 轮转 + 重复抑制，确定性）。

    Args:
        candidates: 启用人格候选（含各自最近应答时刻；顺序不参与决策）。
        state: 本群聊应答记忆（上一轮应答者——重复抑制原料）。
        settings: 群聊多人格配置（开关 / 上限 / 冷却）。
        now_ms: 当前时刻（epoch 毫秒——冷却判定参考）。
        is_group_chat: 是否群聊消息（单聊恒空选择）。

    Returns:
        :class:`~packages.harness.chat.persona_rotation_models.
        GroupPersonaSelectionModel`（应答人格主键列表 + 理由 + 冷却排除集）。
    """
    if not settings.enabled:
        return GroupPersonaSelectionModel(reason=GroupSelectionReason.DISABLED)
    if not is_group_chat:
        return GroupPersonaSelectionModel(reason=GroupSelectionReason.NOT_GROUP)
    if not candidates:
        return GroupPersonaSelectionModel(reason=GroupSelectionReason.NO_CANDIDATES)
    cooling: list[int] = []
    available: list[GroupResponderCandidateModel] = []
    for candidate in candidates:
        last = candidate.last_reply_ms
        if settings.cooldown_ms > 0 and last is not None and now_ms - last < settings.cooldown_ms:
            cooling.append(candidate.persona_id)
            continue
        available.append(candidate)
    if not available:
        ordered_cooling = sorted(
            candidates,
            key=lambda item: (
                item.last_reply_ms if item.last_reply_ms is not None else -1,
                item.persona_id,
            ),
        )
        selected = [item.persona_id for item in ordered_cooling[: settings.max_responders]]
        return GroupPersonaSelectionModel(
            responder_persona_ids=_dedupe(selected),
            reason=GroupSelectionReason.ALL_COOLING_FALLBACK,
            cooling_persona_ids=sorted(set(cooling)),
        )
    previous = set(state.last_responder_ids)
    available.sort(
        key=lambda item: (
            0 if item.persona_id not in previous else 1,
            0 if item.is_primary else 1,
            item.persona_id,
        )
    )
    selected = [item.persona_id for item in available[: settings.max_responders]]
    reason = (
        GroupSelectionReason.ROTATION
        if any(persona_id not in previous for persona_id in selected)
        else GroupSelectionReason.REPEAT_FALLBACK
    )
    return GroupPersonaSelectionModel(
        responder_persona_ids=_dedupe(selected),
        reason=reason,
        cooling_persona_ids=sorted(set(cooling)),
    )


def _dedupe(persona_ids: Sequence[int]) -> list[int]:
    """按出现序去重人格主键（同一轮内互斥约束）。

    Args:
        persona_ids: 候选人答人格主键序列。

    Returns:
        去重后的主键列表（保持首次出现顺序）。
    """
    seen: set[int] = set()
    result: list[int] = []
    for persona_id in persona_ids:
        if persona_id in seen:
            continue
        seen.add(persona_id)
        result.append(persona_id)
    return result
