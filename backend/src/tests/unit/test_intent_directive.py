"""intent DIRECTIVE 快径测试——前缀捕获 / 副作用接线 / 失败退化。

覆盖面：matcher 前缀表命中（记住 / 记得 / 以后都 / 以后 / 别再 / 不要）+
正文剥离（引导 / 句末标点）+ 非句首 / 过短正文不接管；服务层 sink 成功
→「已记住」确认文案 / sink 返回 False → 附和文案 / sink 抛异常 →
附和文案且不抛 / 未接线 sink → 附和文案；既有四类命中行为零变更。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

import packages.harness.memory.orm as memory_orm  # noqa: F401  模型注册副作用导入（共享建表）
from packages.harness.intent.intent_models import IntentKind, PersonaNaming
from packages.harness.intent.intent_texts import (
    DIRECTIVE_ACK_TEXT,
    DIRECTIVE_CONFIRMED_TEXT,
)
from packages.harness.intent.matcher import DIRECTIVE_PREFIXES, match_intent
from packages.harness.intent.service import IntentShortcutService
from packages.harness.memory.models import MemoryProvenance
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.directives_models import DirectiveCreateDTO
from packages.harness.persona.service import bind_persona_context

#: 分钟级固定时钟（epoch 毫秒——确定性轴）。
NOW_MS = 1_000

#: 指令捕获测试的人格上下文轴（PersonaMixin 无外键——任意字符串即隔离轴）。
PERSONA_CONTEXT = "intent-directive-persona"


class _Naming:
    """人格命名面 fake（PersonaNaming 结构满足）。"""

    persona_name = "feiyu"
    display_name = "大肥鱼"


def _naming() -> PersonaNaming:
    """返回人格命名 fake（结构化契约类型）。"""
    return _Naming()


async def test_matcher_captures_remember_prefix() -> None:
    """记住：X —— 前缀剥离 + 正文进 directive_text（附和文案先行）。"""
    match = match_intent("记住：以后都用简体中文", "大肥鱼", NOW_MS)
    assert match.handled is True
    assert match.kind is IntentKind.DIRECTIVE
    assert match.directive_text == "以后都用简体中文"
    assert match.response_text == DIRECTIVE_ACK_TEXT


async def test_matcher_captures_all_prefixes() -> None:
    """全前缀表命中：每种前缀剥离后正文可取（正文 < 2 不接管）。"""
    cases = {
        "记得每天提醒我喝水": "每天提醒我喝水",
        "以后都叫我小鱼干": "叫我小鱼干",
        "以后早点睡": "早点睡",
        "别再熬夜了": "熬夜了",
        "不要太晚回复我。": "太晚回复我",
    }
    for text, expected in cases.items():
        match = match_intent(text, "大肥鱼", NOW_MS)
        assert match.kind is IntentKind.DIRECTIVE, text
        assert match.directive_text == expected, text
    assert "记住" in DIRECTIVE_PREFIXES


async def test_matcher_ignores_non_prefix_and_short_body() -> None:
    """非句首「记得」/ 纯前缀无正文：不接管（文本照常走 LLM）。"""
    assert match_intent("你还记得吗", "大肥鱼", NOW_MS).handled is False
    assert match_intent("记得", "大肥鱼", NOW_MS).handled is False
    assert match_intent("记住。", "大肥鱼", NOW_MS).handled is False


async def test_matcher_existing_kinds_unchanged() -> None:
    """既有四类命中行为零变更（时间问答 / 问候）。"""
    time_match = match_intent("现在几点", "大肥鱼", NOW_MS)
    assert time_match.kind is IntentKind.TIME_QUERY
    greeting = match_intent("你好", "大肥鱼", NOW_MS)
    assert greeting.kind is IntentKind.GREETING


async def test_service_sink_success_returns_confirmation() -> None:
    """sink 成功：回复替换为「已记住：正文」确认文案。"""
    captured: list[str] = []

    async def _sink(text: str) -> bool:
        captured.append(text)
        return True

    service = IntentShortcutService(directive_sink=_sink)
    match = await service.try_shortcut("记住：以后都用简体中文", _naming(), NOW_MS)
    assert captured == ["以后都用简体中文"]
    assert match.response_text == DIRECTIVE_CONFIRMED_TEXT.format(directive="以后都用简体中文")


async def test_service_sink_false_returns_ack() -> None:
    """sink 返回 False（落库失败）：保持附和文案——不承诺「记住」。"""
    async def _sink(text: str) -> bool:
        return False

    service = IntentShortcutService(directive_sink=_sink)
    match = await service.try_shortcut("别再熬夜了", _naming(), NOW_MS)
    assert match.response_text == DIRECTIVE_ACK_TEXT


async def test_service_sink_exception_degrades() -> None:
    """sink 抛异常：快径恒不抛——退化为附和文案。"""
    async def _sink(text: str) -> bool:
        raise RuntimeError("落库失败（测试注入）")

    service = IntentShortcutService(directive_sink=_sink)
    match = await service.try_shortcut("以后都叫我小鱼干", _naming(), NOW_MS)
    assert match.handled is True
    assert match.response_text == DIRECTIVE_ACK_TEXT


async def test_service_without_sink_returns_ack() -> None:
    """未接线 sink：DIRECTIVE 命中只回附和文案（无副作用形态）。"""
    service = IntentShortcutService()
    match = await service.try_shortcut("记住：以后都用简体中文", _naming(), NOW_MS)
    assert match.kind is IntentKind.DIRECTIVE
    assert match.response_text == DIRECTIVE_ACK_TEXT


async def test_service_non_directive_never_calls_sink() -> None:
    """非 DIRECTIVE 命中：sink 零调用（副作用只属于指令捕获面）。"""
    calls: list[str] = []

    async def _sink(text: str) -> bool:
        calls.append(text)
        return True

    service = IntentShortcutService(directive_sink=_sink)
    match = await service.try_shortcut("现在几点", _naming(), NOW_MS)
    assert match.kind is IntentKind.TIME_QUERY
    assert calls == []


async def test_service_unmatched_text_unchanged() -> None:
    """未命中文本：handled=False + 空文案（照常流入 LLM）。"""
    service = IntentShortcutService()
    match = await service.try_shortcut("今天的工作有点难", _naming(), NOW_MS)
    assert match.handled is False
    assert match.directive_text is None


async def _sink_via_directives_service(db_session: AsyncSession, text: str) -> bool:
    """把指令正文经真实长期指令服务落库（装配闭包同构形态）。

    Args:
        db_session: 共享测试 Session（人格上下文由调用路径绑定）。
        text: 指令正文（matcher 前缀剥离后的内容）。

    Returns:
        True 表示已落库。
    """
    async with bind_persona_context(PERSONA_CONTEXT):
        await DirectivesService().create(db_session, DirectiveCreateDTO(content=text))
    return True


async def test_peer_shortcut_skips_directive_creation_and_still_replies(
    db_session: AsyncSession,
) -> None:
    """群聊 peer「记住…」：不创建长期指令，仍以快径附和文案回复。"""
    service = IntentShortcutService(
        directive_sink=lambda text: _sink_via_directives_service(db_session, text)
    )
    match = await service.try_shortcut(
        "记住：以后都用简体中文",
        _naming(),
        NOW_MS,
        sender_provenance=MemoryProvenance.PEER,
    )
    assert match.handled is True
    assert match.kind is IntentKind.DIRECTIVE
    assert match.response_text == DIRECTIVE_ACK_TEXT
    async with bind_persona_context(PERSONA_CONTEXT):
        assert await DirectivesService().list_active(db_session) == []


async def test_dm_owner_shortcut_creates_directive(
    db_session: AsyncSession,
) -> None:
    """私聊 owner「记住…」：长期指令成功落库并回「已记住」确认。"""
    service = IntentShortcutService(
        directive_sink=lambda text: _sink_via_directives_service(db_session, text)
    )
    match = await service.try_shortcut(
        "记住：以后都用简体中文",
        _naming(),
        NOW_MS,
        sender_provenance=MemoryProvenance.OWNER,
    )
    assert match.handled is True
    assert match.response_text == DIRECTIVE_CONFIRMED_TEXT.format(directive="以后都用简体中文")
    async with bind_persona_context(PERSONA_CONTEXT):
        active = await DirectivesService().list_active(db_session)
    assert [record.content for record in active] == ["以后都用简体中文"]
