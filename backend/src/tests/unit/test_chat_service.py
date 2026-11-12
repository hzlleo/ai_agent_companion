"""harness/chat service + chat_models 测试——组装契约 / 网关直通 / 会话态。

测试策略：假网关（记录组装消息与调用参数——零真实网络）+ 直供人格
记录（不走 DB 的组装面）；create_session 查库路径经共享 SQLite fixture
走真实 PersonaService。组装断言覆盖：正典字段 / 世界书（constant 恒注 /
关键词触发 + scan_depth 窗口 / selective / 大小写 / token 预算截断）/
历史后再注入位置 / purpose 与 persona_id 直通 / 流式聚合与提前终止。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.chat.chat_models import (
    ChatSessionStateModel,
    ChatTurnModel,
    MessageCompositionResult,
)
from packages.harness.chat.service import (
    ChatService,
    compose_messages,
    estimate_text_tokens,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.core.errors import BusinessException
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatResponseModel,
    ChatStreamChunkModel,
)
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)
from packages.harness.persona.dtos import PersonaCreateDTO, PersonaRecordDTO
from packages.harness.persona.service import PersonaService


class FakeGateway:
    """假 LLM 网关——记录调用参数与组装消息；可注入回复 / 故障。"""

    def __init__(
        self,
        *,
        response_content: str = "假回复内容",
        stream_parts: Sequence[str] = ("早", "安", "呀"),
        error: Exception | None = None,
    ) -> None:
        """初始化假网关。

        Args:
            response_content: 非流式回复文本。
            stream_parts: 流式增量块序列。
            error: 故障注入（每次调用抛出该异常）。
        """
        self.response_content = response_content
        self.stream_parts = list(stream_parts)
        self.error = error
        self.chat_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def chat_by_role(
        self,
        role: ModelRole | str,
        messages: Sequence[ChatMessageModel],
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """非流式假调用（记录参数）。"""
        self.chat_calls.append(
            {
                "role": role,
                "messages": list(messages),
                "persona_id": persona_id,
                "purpose": purpose,
                "request_id": request_id,
            }
        )
        if self.error is not None:
            raise self.error
        return ChatResponseModel(content=self.response_content)

    async def stream_by_role(
        self,
        role: ModelRole | str,
        messages: Sequence[ChatMessageModel],
        params: Any = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式假调用（记录参数 + 逐块产出）。"""
        self.stream_calls.append(
            {
                "role": role,
                "messages": list(messages),
                "persona_id": persona_id,
                "purpose": purpose,
                "request_id": request_id,
            }
        )
        if self.error is not None:
            raise self.error
        for part in self.stream_parts:
            yield ChatStreamChunkModel(content=part)
        yield ChatStreamChunkModel(content="", usage=None)


def _card(**overrides: Any) -> CharaCardModel:
    """组装测试人格卡片（chara card 字段可覆写）。"""
    fields: dict[str, Any] = {
        "name": "大肥鱼",
        "description": "一条圆滚滚的橘色胖鱼，最喜欢小鱼干。",
        "personality": "元气、贪吃、爱撒娇，语气软软的。",
        "scenario": "周末午后的鱼缸边，阳光正好。",
        "first_mes": "你回来啦！今天有没有想我？",
        "post_history_instructions": "回复保持简短，多用亲昵语气词。",
        "character_book": None,
    }
    fields.update(overrides)
    return CharaCardModel.model_validate(fields)


def _record(**card_overrides: Any) -> PersonaRecordDTO:
    """直供人格记录（不走 DB——组装面测试形态）。"""
    return PersonaRecordDTO(
        id=7,
        name="feiyu",
        display_name="大肥鱼",
        status="active",
        is_active=True,
        card=_card(**card_overrides),
        created_at=1700000000000,
        updated_at=1700000000000,
    )


async def _make_state(**card_overrides: Any) -> ChatSessionStateModel:
    """直供记录 → 会话状态（测试铺底辅助）。"""
    service = ChatService(gateway=FakeGateway())
    return await service.create_session(persona=_record(**card_overrides))


def _book(**entry_overrides: Any) -> CharaBookModel:
    """构造世界书（条目 / scan_depth / token_budget 可覆写）。"""
    fields: dict[str, Any] = {
        "name": "测试世界书",
        "scan_depth": None,
        "token_budget": None,
        "entries": [],
    }
    fields.update(entry_overrides)
    return CharaBookModel.model_validate(fields)


# ---------------------------------------------------------------------------
# compose_messages——组装契约（纯函数直调）
# ---------------------------------------------------------------------------


async def test_compose_system_prompt_contains_card_fields() -> None:
    """system 首块：章程 + 正典四段（name/描述/性格/场景）均在。"""
    state = await _make_state()
    result = compose_messages(state)
    system = result.messages[0]
    assert system.role == ChatMessageRole.SYSTEM
    assert "【会话章程】" in system.content
    assert "你现在扮演「大肥鱼」" in system.content
    assert "一条圆滚滚的橘色胖鱼" in system.content
    assert "元气、贪吃、爱撒娇" in system.content
    assert "周末午后的鱼缸边" in system.content
    assert result.system_prompt == system.content


async def test_compose_constant_entries_always_injected() -> None:
    """constant 条目恒注入（无关键词 / 无历史 / 有预算均注入）。"""
    state = await _make_state(
        character_book=_book(
            token_budget=1,
            entries=[
                CharaBookEntryModel(keys=[], content="世界观保底设定", constant=True),
            ],
        )
    )
    result = compose_messages(state)
    assert "世界观保底设定" in result.system_prompt
    assert result.included_book_entries == 1


async def test_compose_keyword_entry_requires_keyword_in_scan_window() -> None:
    """关键词条目：窗口内命中注入；窗口外 / 无关键词不注入。"""
    state = await _make_state(
        character_book=_book(
            scan_depth=2,
            entries=[CharaBookEntryModel(keys=["公园"], content="公园的傍晚设定")],
        )
    )
    # 历史 4 回合，"公园" 只出现在倒数第 3 回合（scan_depth=2 窗口外）。
    state.append_turn(ChatMessageRole.ASSISTANT, "你回来啦！")
    state.append_turn(ChatMessageRole.USER, "上次我们去公园玩得很开心")
    state.append_turn(ChatMessageRole.ASSISTANT, "是呀，夕阳很好看。")
    state.append_turn(ChatMessageRole.USER, "今天忙什么呢")
    away = compose_messages(state)
    assert "公园的傍晚设定" not in away.system_prompt
    assert away.included_book_entries == 0

    # 关键词出现在最新回合（窗口内）→ 注入。
    state.append_turn(ChatMessageRole.USER, "傍晚想去公园走走")
    near = compose_messages(state)
    assert "公园的傍晚设定" in near.system_prompt
    assert near.included_book_entries == 1


async def test_compose_keyword_entry_absent_keyword_not_injected() -> None:
    """无关键词命中：条目不注入。"""
    state = await _make_state(
        character_book=_book(entries=[CharaBookEntryModel(keys=["图书馆"], content="图书馆设定")])
    )
    state.append_turn(ChatMessageRole.USER, "今天吃了小鱼干")
    result = compose_messages(state)
    assert "图书馆设定" not in result.system_prompt


async def test_compose_selective_entry_needs_secondary_key() -> None:
    """selective 条目：主键命中 + 副键命中才注入；缺副键不注入。"""
    entries = [
        CharaBookEntryModel(
            keys=["晚饭"],
            secondary_keys=["小鱼干"],
            content="晚饭与小鱼干的设定",
            selective=True,
        ),
    ]
    only_primary = await _make_state(character_book=_book(entries=entries))
    only_primary.append_turn(ChatMessageRole.USER, "晚饭吃什么")
    assert "晚饭与小鱼干的设定" not in compose_messages(only_primary).system_prompt

    both_keys = await _make_state(character_book=_book(entries=entries))
    both_keys.append_turn(ChatMessageRole.USER, "晚饭想吃小鱼干")
    assert "晚饭与小鱼干的设定" in compose_messages(both_keys).system_prompt


async def test_compose_case_sensitive_matching() -> None:
    """大小写敏感性：case_sensitive 条目不命中大小写不同的扫描文本。"""
    entries = [
        CharaBookEntryModel(keys=["Park"], content="Park 设定", case_sensitive=True),
        CharaBookEntryModel(keys=["zoo"], content="zoo 设定"),
    ]
    state = await _make_state(character_book=_book(entries=entries))
    state.append_turn(ChatMessageRole.USER, "i went to the park and the ZOO today")
    result = compose_messages(state)
    assert "Park 设定" not in result.system_prompt
    assert "zoo 设定" in result.system_prompt


async def test_compose_token_budget_truncates_keyword_entries() -> None:
    """token 预算：超预算关键词条目截断（其后关键词条目不再注入）。"""
    entries = [
        # 每条约 10 token（10 个 CJK 字符）。
        CharaBookEntryModel(keys=["甲"], content="甲条目设定文本共计十字"),
        CharaBookEntryModel(keys=["乙"], content="乙条目设定文本共计十字"),
        CharaBookEntryModel(keys=["丙"], content="丙条目设定文本共计十字"),
    ]
    state = await _make_state(character_book=_book(token_budget=15, entries=entries))
    state.append_turn(ChatMessageRole.USER, "聊聊甲乙丙")
    result = compose_messages(state)
    assert "甲条目设定文本共计十字" in result.system_prompt
    # 乙（10 token）累计 20 > 15 → 截断；丙在乙之后 → 一并不注入。
    assert "乙条目设定文本共计十字" not in result.system_prompt
    assert "丙条目设定文本共计十字" not in result.system_prompt
    assert result.included_book_entries == 1


async def test_compose_entry_position_and_insertion_order() -> None:
    """条目位置与次序：before_char 在人格正典前 / after_char 在后；
    同位置内 insertion_order 升序。"""
    entries = [
        CharaBookEntryModel(keys=["行为"], content="行为补充条目", insertion_order=100),
        CharaBookEntryModel(
            keys=["世界"],
            content="世界观背景条目",
            insertion_order=50,
            position="before_char",
        ),
        CharaBookEntryModel(
            keys=["地理"],
            content="地理背景条目",
            insertion_order=10,
            position="before_char",
        ),
    ]
    state = await _make_state(character_book=_book(entries=entries))
    state.append_turn(ChatMessageRole.USER, "聊聊世界观地理和行为")
    system = compose_messages(state).system_prompt
    assert system.index("地理背景条目") < system.index("世界观背景条目")
    assert system.index("世界观背景条目") < system.index("你现在扮演「大肥鱼」")
    assert system.index("你现在扮演「大肥鱼」") < system.index("行为补充条目")


async def test_compose_post_history_after_last_history_turn() -> None:
    """历史后再注入：位于最后一条历史回合之后（末位 system 块）。"""
    state = await _make_state()
    state.append_turn(ChatMessageRole.ASSISTANT, "你回来啦！")
    state.append_turn(ChatMessageRole.USER, "在吗")
    result = compose_messages(state)
    assert result.post_history_message is not None
    last = result.messages[-1]
    assert last.role == ChatMessageRole.SYSTEM
    assert "回复保持简短，多用亲昵语气词。" in last.content
    # 末块之前是最后的历史回合（user），而非 system 首块。
    prior = result.messages[-2]
    assert prior.role == ChatMessageRole.USER
    assert prior.content == "在吗"


async def test_compose_without_post_history_instructions() -> None:
    """卡片未声明 PHI：无末位 system 块（post_history_message=None）。"""
    state = await _make_state(post_history_instructions="")
    state.append_turn(ChatMessageRole.USER, "在吗")
    result = compose_messages(state)
    assert result.post_history_message is None
    assert result.messages[-1].role == ChatMessageRole.USER


async def test_compose_maps_turns_in_order() -> None:
    """历史回合按时间序映射为传输消息（含开场白 assistant 回合）。"""
    state = await _make_state()
    state.append_turn(ChatMessageRole.ASSISTANT, "开场白")
    state.append_turn(ChatMessageRole.USER, "第一句")
    state.append_turn(ChatMessageRole.ASSISTANT, "第一答")
    state.append_turn(ChatMessageRole.USER, "第二句")
    result = compose_messages(state)
    roles = [message.role for message in result.messages]
    assert roles == [
        ChatMessageRole.SYSTEM,
        ChatMessageRole.ASSISTANT,
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
        ChatMessageRole.USER,
        ChatMessageRole.SYSTEM,  # 历史后再注入
    ]
    assert result.estimated_system_tokens == estimate_text_tokens(result.system_prompt)


async def test_estimate_text_tokens_cjk_and_latin() -> None:
    """token 估算：CJK 1 字 1 token；拉丁 4 字符 1 token。"""
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("你好世界") == 4
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("你好abcd") == 3
    assert estimate_text_tokens("你好abc") == 3  # 2 CJK + ceil(3/4)=1


# ---------------------------------------------------------------------------
# ChatService——网关直通 / 会话态
# ---------------------------------------------------------------------------


async def test_reply_passes_role_purpose_and_persona_id() -> None:
    """网关直通：role=chat、purpose=chat、persona_id 字符串直传。"""
    gateway = FakeGateway()
    service = ChatService(gateway=gateway)
    state = await service.create_session(persona=_record())
    content = await service.reply(state, "在吗")
    assert content == "假回复内容"
    assert len(gateway.chat_calls) == 1
    call = gateway.chat_calls[0]
    assert call["role"] in (ModelRole.CHAT, "chat")
    assert str(call["role"]) == "chat"
    assert call["purpose"] == "chat"
    assert call["persona_id"] == "7"
    composed = call["messages"]
    assert composed[0].role == ChatMessageRole.SYSTEM
    assert composed[-1].content is not None
    # 用户回合与助手回合均已入历史。
    assert [turn.role for turn in state.turns] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
    ]


async def test_reply_empty_input_rejected() -> None:
    """空输入边界：空白用户文本拒绝（VALIDATION_ERROR）。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record())
    with pytest.raises(BusinessException) as exc_info:
        await service.reply(state, "   ")
    assert exc_info.value.code == "VALIDATION_ERROR"


async def test_reply_empty_model_response_rejected() -> None:
    """模型空回复：LLM_CALL_FAILED（不入助手回合）。"""
    gateway = FakeGateway(response_content="   ")
    service = ChatService(gateway=gateway)
    state = await service.create_session(persona=_record())
    with pytest.raises(BusinessException) as exc_info:
        await service.reply(state, "在吗")
    assert exc_info.value.code == "LLM_CALL_FAILED"
    assert [turn.role for turn in state.turns] == [ChatMessageRole.USER]


async def test_stream_reply_aggregates_and_appends_turn() -> None:
    """流式聚合：逐块产出 + 全文入助手回合 + 参数直通。"""
    gateway = FakeGateway(stream_parts=("早", "安", "呀"))
    service = ChatService(gateway=gateway)
    state = await service.create_session(persona=_record())
    tokens: list[str] = []
    async for token in service.stream_reply(state, "在吗"):
        tokens.append(token)
    assert tokens == ["早", "安", "呀"]
    assert state.turns[-1].role == ChatMessageRole.ASSISTANT
    assert state.turns[-1].content == "早安呀"
    call = gateway.stream_calls[0]
    assert call["purpose"] == "chat"
    assert call["persona_id"] == "7"


async def test_stream_reply_early_termination_keeps_history_clean() -> None:
    """提前终止：半截回复不入历史（仅保留用户回合）。"""
    gateway = FakeGateway(stream_parts=("早", "安", "呀"))
    service = ChatService(gateway=gateway)
    state = await service.create_session(persona=_record())
    async for token in service.stream_reply(state, "在吗"):
        assert token == "早"
        break
    assert [turn.role for turn in state.turns] == [ChatMessageRole.USER]
    assert state.turns[0].content == "在吗"


async def test_stream_reply_empty_stream_rejected() -> None:
    """空流：聚合为空 → LLM_CALL_FAILED。"""
    gateway = FakeGateway(stream_parts=("", ""))
    service = ChatService(gateway=gateway)
    state = await service.create_session(persona=_record())
    with pytest.raises(BusinessException) as exc_info:
        async for _token in service.stream_reply(state, "在吗"):
            pass
    assert exc_info.value.code == "LLM_CALL_FAILED"


async def test_opening_uses_first_mes_and_appends_turn() -> None:
    """开场白：卡片 first_mes 优先 + 追加为首个 assistant 回合。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record())
    greeting = service.opening(state)
    assert greeting == "你回来啦！今天有没有想我？"
    assert state.turns[0].role == ChatMessageRole.ASSISTANT
    assert state.turns[0].content == greeting


async def test_opening_fallback_when_first_mes_empty() -> None:
    """开场白兜底：first_mes 为空 → prompts 兜底模板（含称呼名）。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record(first_mes=""))
    greeting = service.opening(state)
    assert "大肥鱼" in greeting
    assert "今天想聊点什么" in greeting


async def test_opening_idempotent_with_existing_assistant_turn() -> None:
    """幂等入口：已有 assistant 回合时返回其内容（不重复追加）。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record())
    first = service.opening(state)
    again = service.opening(state)
    assert again == first
    assert len(state.turns) == 1


async def test_history_returns_copy_and_clear_resets() -> None:
    """历史读取副本 + clear 清空（人格绑定保留）。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record())
    service.opening(state)
    turns = service.history(state)
    assert len(turns) == 1
    turns.clear()
    assert len(state.turns) == 1  # 副本——外部修改不影响会话态
    service.clear_history(state)
    assert service.history(state) == []
    assert state.persona_id == 7


async def test_state_caps_sliding_window_and_turn_truncation() -> None:
    """结构性上限：回合数滑动窗口 + 单回合字符截断。"""
    service = ChatService(gateway=FakeGateway())
    state = await service.create_session(persona=_record())
    state.max_history_turns = 3
    for index in range(5):
        state.append_turn(ChatMessageRole.USER, f"第{index}句")
    assert len(state.turns) == 3
    assert state.turns[0].content == "第2句"

    state.max_turn_chars = 4
    truncated = state.append_turn(ChatMessageRole.USER, "这是一段超长的用户输入")
    assert truncated.content == "这是一段"
    with pytest.raises(ValueError, match="不能为空"):
        state.append_turn(ChatMessageRole.USER, "   ")


async def test_create_session_requires_persona_or_db() -> None:
    """既无显式人格也无数据库会话：INTERNAL_ERROR fail loud。"""
    service = ChatService(gateway=FakeGateway())
    with pytest.raises(BusinessException) as exc_info:
        await service.create_session()
    assert exc_info.value.code == "INTERNAL_ERROR"


async def test_create_session_no_active_persona(db_session: AsyncSession) -> None:
    """查库路径无激活人格：NOT_FOUND（空库真实路径）。"""
    service = ChatService(gateway=FakeGateway(), persona_service=PersonaService())
    with pytest.raises(BusinessException) as exc_info:
        await service.create_session(db_session=db_session)
    assert exc_info.value.code == "NOT_FOUND"
    assert "激活" in exc_info.value.message


async def test_create_session_from_active_persona(db_session: AsyncSession) -> None:
    """查库路径：激活人格 → 会话绑定其正典快照。"""
    persona_service = PersonaService()
    created = await persona_service.create(
        db_session,
        PersonaCreateDTO.model_validate(
            {
                "name": "chat-target",
                "status": "active",
                "card": {
                    "name": "小夜",
                    "description": "安静的夜猫子",
                    "personality": "温柔",
                    "first_mes": "晚上好呀。",
                },
            }
        ),
    )
    await persona_service.activate(db_session, created.id)

    service = ChatService(gateway=FakeGateway(), persona_service=persona_service)
    state = await service.create_session(db_session=db_session)
    assert state.persona_id == created.id
    assert state.persona_name == "chat-target"
    assert state.card.name == "小夜"
    assert state.display_name == "小夜"
    assert state.turns == []


async def test_message_composition_result_contract() -> None:
    """组装结果契约面：字段齐备（观测元数据）。"""
    state = await _make_state(
        character_book=_book(entries=[CharaBookEntryModel(content="保底设定", constant=True)])
    )
    state.append_turn(ChatMessageRole.USER, "在吗")
    result = compose_messages(state)
    assert isinstance(result, MessageCompositionResult)
    assert result.included_book_entries == 1
    assert result.estimated_system_tokens > 0
    assert all(isinstance(turn, ChatTurnModel) for turn in state.turns)
