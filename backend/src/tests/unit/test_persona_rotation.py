"""群聊多人格选择纯函数测试——轮转 / 冷却 / 重复抑制 / 兜底。

覆盖：未启用与单聊空选择；空候选；轮转优先上一轮未发言者；上一轮全员
发言时允许重复；冷却排除与全冷却兜底（选最久未发言者）；上限截断与
去重；主人格非特权（仅主键序稳定参考）。
"""

from __future__ import annotations

from packages.harness.chat.persona_rotation import select_group_responders
from packages.harness.chat.persona_rotation_models import (
    GroupPersonaSettings,
    GroupResponderCandidateModel,
    GroupResponderStateModel,
    GroupSelectionReason,
)

_BASE_MS = 1_700_000_000_000


def _candidate(
    persona_id: int,
    *,
    last_reply_ms: int | None = None,
    is_primary: bool = False,
) -> GroupResponderCandidateModel:
    """构造应答人格候选。"""
    return GroupResponderCandidateModel(
        persona_id=persona_id, is_primary=is_primary, last_reply_ms=last_reply_ms
    )


async def test_disabled_and_private_messages_select_nothing() -> None:
    """未启用 / 单聊：空选择（调用方走既有单人格路径）。"""
    candidates = [_candidate(1), _candidate(2)]
    disabled = select_group_responders(
        candidates,
        GroupResponderStateModel(),
        GroupPersonaSettings(enabled=False),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert disabled.responder_persona_ids == []
    assert disabled.reason == GroupSelectionReason.DISABLED
    private = select_group_responders(
        candidates,
        GroupResponderStateModel(),
        GroupPersonaSettings(enabled=True),
        now_ms=_BASE_MS,
        is_group_chat=False,
    )
    assert private.responder_persona_ids == []
    assert private.reason == GroupSelectionReason.NOT_GROUP


async def test_empty_candidates_select_nothing() -> None:
    """无启用人格：空选择。"""
    selection = select_group_responders(
        [],
        GroupResponderStateModel(),
        GroupPersonaSettings(enabled=True),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == []
    assert selection.reason == GroupSelectionReason.NO_CANDIDATES


async def test_rotation_prefers_previous_non_responder() -> None:
    """轮转：上一轮发言者让位，优先未发言人格。"""
    selection = select_group_responders(
        [_candidate(1, is_primary=True), _candidate(2)],
        GroupResponderStateModel(last_responder_ids=[1]),
        GroupPersonaSettings(enabled=True, max_responders=1),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [2]
    assert selection.reason == GroupSelectionReason.ROTATION


async def test_repeat_fallback_when_all_available_already_spoke() -> None:
    """可用者都刚发过言：允许重复并标明理由。"""
    selection = select_group_responders(
        [_candidate(1), _candidate(2)],
        GroupResponderStateModel(last_responder_ids=[1, 2]),
        GroupPersonaSettings(enabled=True, max_responders=1),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [1]
    assert selection.reason == GroupSelectionReason.REPEAT_FALLBACK


async def test_cooldown_excludes_recent_responder() -> None:
    """冷却内人格被排除，其余候选可应答。"""
    selection = select_group_responders(
        [
            _candidate(1, last_reply_ms=_BASE_MS - 1000, is_primary=True),
            _candidate(2),
        ],
        GroupResponderStateModel(),
        GroupPersonaSettings(enabled=True, max_responders=1, cooldown_ms=60_000),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [2]
    assert selection.cooling_persona_ids == [1]


async def test_all_cooling_falls_back_to_oldest_reply() -> None:
    """全部冷却：选最近应答时刻最早者兜底（保证群里有回应）。"""
    selection = select_group_responders(
        [
            _candidate(1, last_reply_ms=_BASE_MS - 5000),
            _candidate(2, last_reply_ms=_BASE_MS - 1000),
        ],
        GroupResponderStateModel(),
        GroupPersonaSettings(enabled=True, max_responders=1, cooldown_ms=60_000),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [1]
    assert selection.reason == GroupSelectionReason.ALL_COOLING_FALLBACK


async def test_multiple_responders_respect_cap_and_order() -> None:
    """多人格接续：上限截断且按确定性顺序返回。"""
    selection = select_group_responders(
        [_candidate(3), _candidate(1), _candidate(2)],
        GroupResponderStateModel(last_responder_ids=[2]),
        GroupPersonaSettings(enabled=True, max_responders=2),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [1, 3]


async def test_cooldown_zero_disables_exclusion() -> None:
    """冷却 0：不做冷却排除（只保留轮转与重复抑制）。"""
    selection = select_group_responders(
        [_candidate(1, last_reply_ms=_BASE_MS - 1, is_primary=True), _candidate(2)],
        GroupResponderStateModel(last_responder_ids=[1]),
        GroupPersonaSettings(enabled=True, max_responders=1, cooldown_ms=0),
        now_ms=_BASE_MS,
        is_group_chat=True,
    )
    assert selection.responder_persona_ids == [2]
    assert selection.cooling_persona_ids == []
