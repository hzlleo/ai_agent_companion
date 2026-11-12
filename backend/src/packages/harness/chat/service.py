"""chat 域服务——人格感知消息组装 + 会话循环（compose → gateway → 回合）。

组装链（单一真相，文档化于 docs/backend/chat.md）::

    compose_messages(state)
        ↓ ① prompts.render_charter()                      会话章程帧
        ↓ ② 世界书 before_char 条目块                      检索增强（前段）
        ↓ ③ prompts.render_persona_system_prompt(card)     人格正典（覆写感知）
        ↓ ④ 世界书 after_char 条目块                       检索增强（后段）
        ↓ ⑤ 历史回合 → ChatMessageModel 序列                （含刚追加的用户回合）
        ↓ ⑥ prompts.render_post_history_message(card)      历史后再注入（末块）

世界书筛选语义（chara card v2 规格）：

    - ``constant=True`` 条目恒注入（世界观保底——**不占 token 预算**）；
    - 关键词条目对「最近 ``scan_depth`` 条回合」做关键词匹配
      （``keys`` 任一命中；``selective`` 再要求 ``secondary_keys`` 任一
      命中；大小写按条目 ``case_sensitive``）；
    - 关键词条目按 ``(position, insertion_order)`` 升序累计
      ``estimate_text_tokens``，超出 ``token_budget`` 即截断（其后关键
      字条目不再注入——预算耗尽语义）；
    - ``recursive_scanning`` 标志读取但 P0 未启用（P1 记忆层扩展点）。

调用链（网关面）：``chat_by_role / stream_by_role``，role=chat、
purpose="chat"、persona_id 显式参数直传（网关用量面直接携带；显式参数
优先于上下文变量——与 ``bind_persona_context`` 语义等价，选直传因其
可测性与可读性）。

P0 内存态：会话历史仅存活于会话状态对象（持久化随 P1 记忆层落地，
见 chat_models 模块 docstring）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.chat.chat_models import (
    ChatSessionStateModel,
    ChatTurnModel,
    MessageCompositionResult,
)
from packages.harness.chat.prompts import (
    WORLD_BOOK_SECTION_SEPARATOR,
    render_charter,
    render_first_greeting,
    render_persona_system_prompt,
    render_post_history_message,
    render_world_book_block,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatResponseModel,
)
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaBookPosition,
    CharaCardModel,
)
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService

__all__ = [
    "ChatService",
    "compose_messages",
    "estimate_text_tokens",
]

#: 用途常量——chat 会话调用恒以 chat 用途落用量账（usage 域枚举校验）。
CHAT_PURPOSE: str = "chat"


# ---------------------------------------------------------------------------
# token 近似估算（P0 无 tokenizer——结构性预算的确定性估算）
# ---------------------------------------------------------------------------


def _is_cjk_char(ch: str) -> bool:
    """判断字符是否属于 CJK 表意 / 注音 / 全角区段（1 字 ≈ 1 token）。

    Args:
        ch: 单字符。

    Returns:
        属于 CJK 相关区段返回 True。
    """
    code = ord(ch)
    return (
        0x3000 <= code <= 0x30FF  # CJK 标点 + 假名
        or 0x3400 <= code <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # CJK 统一表意
        or 0xF900 <= code <= 0xFAFF  # CJK 兼容表意
        or 0xFF00 <= code <= 0xFFEF  # 全角形式
    )


def estimate_text_tokens(text: str) -> int:
    """粗估文本 token 数（P0 无 tokenizer 的确定性近似）。

    估算规则（本工程约定）：CJK 区段字符按 1 token/字计；其余字符按
    4 字符 ≈ 1 token 计（拉丁 BPE 经验值）。世界书 ``token_budget``
    与组装结果观测均消费本估算——同一文本恒同值（无随机性）。

    Args:
        text: 待估文本。

    Returns:
        近似 token 数（非空文本至少 1）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk_char(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# ---------------------------------------------------------------------------
# 世界书筛选（纯函数）
# ---------------------------------------------------------------------------


def _build_scan_text(book: CharaBookModel, turns: Sequence[ChatTurnModel]) -> str:
    """构建关键词扫描文本（最近 ``scan_depth`` 条回合的内容拼接）。

    Args:
        book: 世界书（``scan_depth`` 为 None 时扫描全部回合）。
        turns: 会话回合序列（时间序）。

    Returns:
        扫描文本（回合内容以换行拼接；空历史为空串）。
    """
    window = turns if book.scan_depth is None else turns[-book.scan_depth :]
    return "\n".join(turn.content for turn in window)


def _entry_matches(entry: CharaBookEntryModel, scan_text: str) -> bool:
    """单条目关键词触发判定（primary + selective secondary）。

    Args:
        entry: 世界书条目（大小写敏感性按条目声明）。
        scan_text: 扫描文本。

    Returns:
        是否触发（``keys`` 任一命中；selective 条目再要求
        ``secondary_keys`` 任一命中）。
    """
    haystack = scan_text if entry.case_sensitive else scan_text.lower()
    primary_hit = any(
        (key if entry.case_sensitive else key.lower()) in haystack for key in entry.keys if key
    )
    if not primary_hit:
        return False
    if entry.selective:
        return any(
            (key if entry.case_sensitive else key.lower()) in haystack
            for key in entry.secondary_keys
            if key
        )
    return True


def _select_world_book_entries(
    card: CharaCardModel, turns: Sequence[ChatTurnModel]
) -> tuple[list[CharaBookEntryModel], list[CharaBookEntryModel], int]:
    """筛选应注入的世界书条目（constant 恒注入 + 关键词触发 + 预算截断）。

    Args:
        card: 人格卡片（无世界书时返回空二元组）。
        turns: 会话回合序列（关键词扫描窗口原料）。

    Returns:
        ``(before_char 条目, after_char 条目, 注入总数)`` 三元组（条目按
        ``(position, insertion_order)`` 升序稳定排列）。
    """
    book = card.character_book
    if book is None or not book.entries:
        return [], [], 0

    scan_text = _build_scan_text(book, turns)
    # 注入次序：before_char 段在前（背景 / 世界观），after_char 段在后
    # （行为补充）；同段内 insertion_order 升序（值越大越靠后）。
    ordered = sorted(
        book.entries,
        key=lambda entry: (
            0 if entry.position == CharaBookPosition.BEFORE_CHAR else 1,
            entry.insertion_order,
        ),
    )

    before: list[CharaBookEntryModel] = []
    after: list[CharaBookEntryModel] = []
    included = 0
    remaining = book.token_budget
    budget_exhausted = False
    for entry in ordered:
        if entry.constant:
            # 恒注入条目不占 token 预算（世界观保底——预算只约束触发条目）
            target = before if entry.position == CharaBookPosition.BEFORE_CHAR else after
            target.append(entry)
            included += 1
            continue
        if budget_exhausted or not _entry_matches(entry, scan_text):
            continue
        cost = estimate_text_tokens(entry.content)
        if remaining is not None and cost > remaining:
            # 预算截断：首条超预算条目即截止，其后关键词条目不再注入
            budget_exhausted = True
            continue
        if remaining is not None:
            remaining -= cost
        target = before if entry.position == CharaBookPosition.BEFORE_CHAR else after
        target.append(entry)
        included += 1
    return before, after, included


# ---------------------------------------------------------------------------
# 消息组装（纯函数——测试直调面）
# ---------------------------------------------------------------------------


def compose_messages(state: ChatSessionStateModel) -> MessageCompositionResult:
    """组装完整请求消息序列（纯函数——无 I/O / 无网关调用）。

    消息序列结构::

        [system]  章程 + 世界书 before 段 + 人格正典 + 世界书 after 段
        [user/assistant ...]  全部历史回合（含刚追加的用户回合）
        [system]  历史后再注入（卡片声明 post_history_instructions 时；
                  位于**最后一条历史回合之后**——deep anti-drift 位置）

    Args:
        state: 会话状态（卡片快照 + 回合列表）。

    Returns:
        组装结果（消息序列 + 观测元数据）。
    """
    before_entries, after_entries, included = _select_world_book_entries(state.card, state.turns)
    system_parts: list[str] = [render_charter()]
    before_block = render_world_book_block(before_entries)
    if before_block:
        system_parts.append(before_block)
    system_parts.append(render_persona_system_prompt(state.card))
    after_block = render_world_book_block(after_entries)
    if after_block:
        system_parts.append(after_block)
    system_prompt = WORLD_BOOK_SECTION_SEPARATOR.join(system_parts)

    messages: list[ChatMessageModel] = [
        ChatMessageModel(role=ChatMessageRole.SYSTEM, content=system_prompt)
    ]
    messages.extend(ChatMessageModel(role=turn.role, content=turn.content) for turn in state.turns)
    post_history_message: str | None = None
    if state.card.post_history_instructions:
        post_history_message = render_post_history_message(state.card)
        messages.append(ChatMessageModel(role=ChatMessageRole.SYSTEM, content=post_history_message))
    return MessageCompositionResult(
        messages=messages,
        system_prompt=system_prompt,
        post_history_message=post_history_message,
        included_book_entries=included,
        estimated_system_tokens=estimate_text_tokens(system_prompt),
    )


# ---------------------------------------------------------------------------
# 会话服务
# ---------------------------------------------------------------------------


class ChatService:
    """人格感知聊天会话服务（组装 + 网关调用 + 会话内存态维护）。

    构造注入（测试 / 装配友好）：网关与人格服务均可显式注入；测试注入
    fake 网关记录组装消息断言组装契约（零真实网络）。
    """

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        persona_service: PersonaService | None = None,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（chat 角色调用面）。
            persona_service: 人格注册表服务（激活人格查找；None = 现场
                构造无状态默认实例）。
        """
        self._gateway = gateway
        self._persona_service = persona_service if persona_service is not None else PersonaService()

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        persona: PersonaRecordDTO | None = None,
        db_session: AsyncSession | None = None,
    ) -> ChatSessionStateModel:
        """创建聊天会话（快照人格正典 + 绑定 persona 轴）。

        Args:
            persona: 显式人格记录（CLI / 测试直供形态；优先于查库）。
            db_session: 数据库会话（``persona`` 未给时用于查找当前激活
                人格）。

        Returns:
            新建的 :class:`ChatSessionStateModel`（空回合列表）。

        Raises:
            BusinessException: 既未提供人格也未提供数据库会话
                （``INTERNAL_ERROR``）；当前无激活人格（``NOT_FOUND``）。
        """
        record = persona
        if record is None:
            if db_session is None:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="创建聊天会话需要显式人格或数据库会话（二者至少其一）",
                )
            record = await self._persona_service.get_active_persona(db_session)
            if record is None:
                raise BusinessException(
                    code=ErrorCode.NOT_FOUND,
                    message="当前没有激活人格，请先创建并激活一个人格再开始对话",
                )
        state = ChatSessionStateModel(
            persona_id=record.id,
            persona_name=record.name,
            display_name=record.display_name,
            card=record.card,
        )
        logger.info("聊天会话已创建: persona_id={}, persona_name={}", record.id, record.name)
        return state

    def opening(self, state: ChatSessionStateModel) -> str:
        """生成会话开场白（首条人格消息——正典 ``first_mes`` 优先）。

        开场白同时追加为首个 assistant 回合（进入会话历史——后续组装
        作为人格自己的先发言回放）。

        Args:
            state: 会话状态（空回合时生成开场；已有回合时返回末条
                assistant 回合内容——幂等入口）。

        Returns:
            开场白文本（卡片 ``first_mes`` 为空时回落 prompts 兜底模板）。
        """
        for turn in reversed(state.turns):
            if turn.role == ChatMessageRole.ASSISTANT:
                return turn.content
        greeting = render_first_greeting(state.card)
        state.append_turn(ChatMessageRole.ASSISTANT, greeting)
        return greeting

    # ------------------------------------------------------------------
    # 对话循环
    # ------------------------------------------------------------------

    async def reply(self, state: ChatSessionStateModel, user_text: str) -> str:
        """单轮回复（非流式）：追加用户回合 → 组装 → 网关 chat_by_role。

        失败语义：网关异常原样上抛（用户回合保留在历史——调用方决定重试
        或提示；半成品回合不入历史的策略见 ``stream_reply``）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。

        Returns:
            人格回复文本（已追加为 assistant 回合）。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；模型返回
                空回复（``LLM_CALL_FAILED``）；网关链路异常（透传）。
        """
        self._append_user_turn(state, user_text)
        result = compose_messages(state)
        response: ChatResponseModel = await self._gateway.chat_by_role(
            ModelRole.CHAT,
            result.messages,
            persona_id=str(state.persona_id),
            purpose=CHAT_PURPOSE,
        )
        content = response.content.strip()
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型返回空回复",
                details={"persona_id": state.persona_id},
            )
        state.append_turn(ChatMessageRole.ASSISTANT, content)
        return content

    async def stream_reply(
        self, state: ChatSessionStateModel, user_text: str
    ) -> AsyncIterator[str]:
        """单轮流式回复：追加用户回合 → 组装 → 网关 stream_by_role。

        增量语义：逐块 ``yield chunk.content``（空增量块跳过）；流自然
        结束后聚合全文追加为 assistant 回合。消费方提前终止
        （``GeneratorExit``）时**不追加助手回合**——半截回复不入历史
        （文档化决策；用户回合保留，重试即重新生成）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。

        Yields:
            回复增量文本块。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；流结束但
                聚合全文为空（``LLM_CALL_FAILED``）；网关链路异常（透传）。
        """
        self._append_user_turn(state, user_text)
        result = compose_messages(state)
        parts: list[str] = []
        async for chunk in self._gateway.stream_by_role(
            ModelRole.CHAT,
            result.messages,
            persona_id=str(state.persona_id),
            purpose=CHAT_PURPOSE,
        ):
            if not chunk.content:
                continue
            parts.append(chunk.content)
            yield chunk.content
        content = "".join(parts).strip()
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型流式回复为空",
                details={"persona_id": state.persona_id},
            )
        state.append_turn(ChatMessageRole.ASSISTANT, content)

    # ------------------------------------------------------------------
    # 会话观测 / 维护
    # ------------------------------------------------------------------

    def history(self, state: ChatSessionStateModel) -> list[ChatTurnModel]:
        """读取会话回合列表（时间序副本）。

        Args:
            state: 会话状态。

        Returns:
            回合列表副本（外部修改不影响会话态）。
        """
        return list(state.turns)

    def clear_history(self, state: ChatSessionStateModel) -> None:
        """清空会话回合（/clear 命令面——人格绑定与卡片快照保留）。

        Args:
            state: 会话状态。
        """
        state.clear_turns()
        logger.info("会话历史已清空: persona_id={}", state.persona_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _append_user_turn(self, state: ChatSessionStateModel, user_text: str) -> None:
        """追加用户回合（空白输入拒绝——边界校验）。

        Args:
            state: 会话状态。
            user_text: 用户输入。

        Raises:
            BusinessException: 输入为空 / 纯空白（``VALIDATION_ERROR``）。
        """
        try:
            state.append_turn(ChatMessageRole.USER, user_text)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="用户输入不能为空",
            ) from exc
