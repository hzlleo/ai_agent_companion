"""chat 域服务——人格感知消息组装 + 会话循环（compose → gateway → 回合）。

组装链（单一真相，文档化于 docs/backend/chat.md）::

    compose_messages(state)
        ↓ ① prompts.render_charter()                      会话章程帧
        ↓ ② 世界书 before_char 条目块                      检索增强（前段）
        ↓ ③ prompts.render_persona_system_prompt(card)     人格正典（覆写感知）
        ↓ ④ 非中性情绪摘要块（render_emotion_state_block——R2b 轻情绪引擎；
             无状态 / 全中性跳过）
        ↓ ⑤ 长期指令块（render_directives_block——R2b D7-14；现行指令
             注入，20 条 / 2000 字符帽截断；无指令跳过）
        ↓ ⑥ 人物事实块（render_facts_block——R2c；【关于对方】现行高置信
             事实注入，12 条 / 1500 字符帽由读取面裁剪；无事实跳过）
        ↓ ⑦ 相关技能块（render_skills_block——R2c；【相关技能】仅当命中
             本轮用户文本时注入，2 条 / 2000 字符帽；无命中跳过）
        ↓ ⑧ prompts.render_output_protocol()               输出格式协议（心声 /
             情绪标签指令——R2b）
        ↓ ⑨ 世界书 after_char 条目块                       检索增强（后段）
        ↓ ⑩ 历史回合 → ChatMessageModel 序列                （含刚追加的用户回合）
        ↓ ⑪ prompts.render_post_history_message(card)      历史后再注入（末块）

世界书筛选语义（chara card v2 规格 + R2b 高级触发——算法链全文见
``world_book`` 模块文档，此处为消费面摘要）：

    - ``constant=True`` 条目恒注入（世界观保底——**不占 token 预算**、
      无视分组互斥 / 概率门 / 冷却门）；
    - 关键词条目对「最近 ``scan_depth`` 条回合」做关键词匹配
      （``keys`` 任一命中；``selective`` 再要求 ``secondary_keys`` 任一
      命中；大小写按条目 ``case_sensitive``）；
    - 递归扩展（书级 ``recursive_scanning`` 总开关 × 条目级
      ``recursive`` 收窄，2 跳上限 + visited 集合环安全）；
    - 互斥分组（``include_group`` 同组非 constant 命中条目只注入
      优先级最高者）→ 概率门（``trigger_probability``，rng 可注入）→
      冷却门（``cooldown_turns``，会话作用域回合状态——功能式传递，
      组装结果携带推进态由本服务回存）→ token 预算装箱（既有截止
      语义）；
    - 渲染序：``position`` 决定段位（既有）；段内 ``(depth 降序,
      insertion_order 升序, 索引升序)``——缺省 depth=4 同槽退化为既有
      纯 insertion_order 序（兼容证明：既有测试零变更）。

调用链（网关面）：``chat_by_role / stream_by_role``，role=chat、
purpose="chat"、persona_id 显式参数直传（网关用量面直接携带；显式参数
优先于上下文变量——与 ``bind_persona_context`` 语义等价，选直传因其
可测性与可读性）。

可选工具循环（R2a-4——构造注入 ``tools`` + ``tool_executor`` 后，
``reply`` 走有界工具循环；缺省 ``tools=None`` 保持既有零工具形态）：

    - 循环机械与 duty 域同构（``duty_service._tool_loop`` 镜像）：
      gateway ``chat_by_role(chat, messages, tools=specs)`` →
      ``tool_calls`` 非空则 assistant 轮回放 + 逐调用经 ``tool_executor``
      （:class:`ToolExecutorFace`——``ToolRegistry.execute_call`` 结构
      满足）执行 + tool 轮反馈（``tool_call_id`` 配对）；``tool_calls``
      空则 content 即最终回复；
    - 步数上限 :data:`CHAT_TOOL_STEP_CAP`（锁定 5——chat 单轮收敛快于
      duty 巡检）；超限追加收尾指令（system）+ **无工具面**最终调用
      （final-report forcing）；
    - **持久化不变**：会话回合只落用户回合 + 最终 assistant 文本
      （工具步是循环内部消息——不进入 ``state.turns`` / 不落
      ``ff_chat_messages``；resume / 后续组装不可见工具中间态，
      文档化决策）；
    - 流式 ``stream_reply`` 恒 content-only（网关面 tools+stream 已
      拒绝——本域不组合二者）。

输出协议 + 轻情绪引擎（R2b D7-2 / D7-7——模块文档锁定边界）：

    - ``reply`` 的模型原始输出经 ``output_protocol.parse_assistant_output``
      解析：``<心声>`` / ``<情绪>`` 前导标签被剥离，**返回 / 落回合 /
      进历史 / 发通道的只有可见正文**；心声与情绪增量只进回合 meta
      （``ff_chat_messages.meta``）与会话详情 API；
    - 情绪引擎仅在「持久化会话绑定且已落库」时激活（M 态存储面依赖
      数据库——纯内存形态零附加 I/O）；激活时 reply 在组装前读 M 态
      产出摘要行注入心情块，回复后按 ``<情绪>`` 增量推进并持久化 M 态，
      同时把推进后快照写入回合 meta；
    - ``stream_reply`` **不跑完整协议**（流式通道是操作员可见面——
      既有 content-only 契约不变）：仅经 ``StreamTagStripper`` 对流的
      起始前缀做 best-effort 标签剥离（未闭合标签原样放行），
      心声 / 情绪**不落 meta、不推进 M 态**；
    - 不合规模型（无标签 / 标签残缺）优雅降级：原文可见、零状态变化
      ——协议面永不因模型输出抛异常。

长期指令注入（R2b D7-14——构造注入 ``directive_service`` 后）：

    - compose 链在心情块之后、输出格式协议之前注入【长期指令】块
      （``render_directives_block``——现行指令按写入倒序，20 条 / 2000
      字符双帽截断 + 截断说明行）；
    - 注入面与情绪引擎同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）——纯内存形态零
      附加 I/O，既有测试面零变更；读取失败不阻断回复（日志 + 空块，
      文档化：常备指令是增强面而非回复前提）。

人物事实注入（R2c——构造注入 ``facts_reader`` 后）：

    - compose 链在长期指令块之后、输出格式协议之前注入【关于对方】块
      （``render_facts_block``——现行高置信事实按类别标签逐行列出，
      12 条 / 1500 字符帽由 ``FactsService.top_facts_for_compose`` 裁剪）；
    - 注入面与长期指令同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）；读取失败不阻断
      回复（日志 + 空块——画像注入是增强面而非回复前提），无现行事实时
      整块跳过。

相关技能注入（R2c——构造注入 ``skills_reader`` 后）：

    - compose 链在人物事实块之后、输出格式协议之前注入【相关技能】块
      （``render_skills_block``——仅当命中**本轮用户文本**时注入；命中
      由 ``SkillsService.match_for_compose`` 按技能触发短语的字面包含
      判定，条数 / 字符帽由服务与渲染函数分别施加）；
    - 注入面与人物事实同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）；读取失败不阻断
      回复（日志 + 空块——技能是操作增强面而非回复前提），无命中时整块
      跳过；命中后的使用计数写入由服务层 fire-and-forget 调度（回复
      链路不等待、不因计数失败而失败）。

意图快径前钩（R2a-6——构造注入 ``intent_shortcut`` 后，``reply`` 前端
先试快径；缺省 ``intent_shortcut=None`` 保持既有零快径形态）：

    - 匹配链：``reply`` 前端调用 ``intent_shortcut.try_shortcut``（
      :class:`IntentShortcutFace`——intent 域 ``IntentShortcutService``
      结构满足）；命中（``handled=True``）时**完全跳过网关调用**——
      罐头回复直接落回合；
    - 拟真延迟：命中后经 ``intent_shortcut.realism_delay``（800-1500ms
      随机——短区间「顺手秒回」语义，不走节律门完整延迟；决策文档化
      于 harness/intent/service）；
    - **持久化不变式**：快径回合照常落用户回合 + 罐头 assistant 回合
      （``state.turns`` + ``ff_chat_messages``——记忆 / 梦境可见该轮，
      文档化决策：快径对话是真实对话史的一部分）；
    - 注入面（通道专属）：app 装配仅在**通道路径**的 ChatService 工厂
      注入快径面（CLI / duty 不注入——行为零变化；意图快径是会话性
      能，duty 是无头计划任务，见 docs/backend/intent.md 装配一节）；
    - 流式 ``stream_reply`` 不走快径（CLI 消费面——通道回复全量走
      ``reply``，零分叉）。

会话持久化（**可选绑定，缺省关闭**）：

    - 构造注入 ``session_service``（memory 域 ``ChatSessionService``）
      后，``create_session(db_session=...)`` 同步落 ``ff_chat_sessions``
      行并在状态上携带 ``session_id``；
    - 绑定 + 已落库时，``reply / stream_reply`` 的用户与助手回合经
      ``session_scope`` 逐条落 ``ff_chat_messages``（seq 递增；开场白
      不落库——正典确定性回放，resume 后幂等重现）；
    - ``resume_session(session_id)`` 全量读取持久化会话消息，经
      ``max_history_turns`` 窗口截断后回放进内存态（保留最新回合；
      卡片按会话归属人格重取当前正典）；
    - 未绑定持久化服务（纯内存形态）时行为与既有完全一致——既有测试面
      零变更。

情景落库（对话回合 → 情景层管道——**可选绑定，缺省关闭**）：

    - 构造注入 ``episode_sink``（memory 域的 chat 回合情景落库面）后，
      ``reply / stream_reply`` 的每个**已落库**回合随即写入 kind=chat
      情景事件（user 回合 provenance=owner / assistant 回合
      provenance=agent）——检索索引 / 夜间梦境 / 生命周期巩固由此获得
      真实对话数据；
    - 时序锁定：消息行提交**之后**才写情景（消息是事实源，情景是派生
      镜像）；未落库会话（纯内存态）零写入；
    - 失败语义：sink 恒不抛（实现吞没），回复链路不受影响——fire-and-
      forget 分发与失败日志见 ``harness.memory.episode_sink``；
    - 未注入 sink（CLI / 单元测试 / 未装配记忆服务的形态）行为与既有
      完全一致。
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final, Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.chat.chat_models import (
    ChatSessionStateModel,
    ChatTurnModel,
    MessageCompositionResult,
)
from packages.harness.chat.output_protocol import (
    ParsedAssistantOutputModel,
    StreamTagStripper,
    parse_assistant_output,
)
from packages.harness.chat.prompts import (
    WORLD_BOOK_SECTION_SEPARATOR,
    render_charter,
    render_directives_block,
    render_emotion_state_block,
    render_facts_block,
    render_first_greeting,
    render_output_protocol,
    render_persona_system_prompt,
    render_post_history_message,
    render_skills_block,
    render_world_book_block,
)
from packages.harness.chat.world_book import (
    WorldBookSelectionResult,
    estimate_text_tokens,
    select_world_book_entries,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import session_scope
from packages.harness.emotion.service import EmotionService
from packages.harness.intent.intent_models import IntentMatchModel, PersonaNaming
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatResponseModel,
    ToolDefinitionModel,
)
from packages.harness.memory.episode_sink import EpisodeSinkFace
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookPosition,
)
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills_models import SkillRecordDTO
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)

__all__ = [
    "ChatService",
    "ToolExecutorFace",
    "IntentShortcutFace",
    "CHAT_TOOL_STEP_CAP",
    "compose_messages",
    "estimate_text_tokens",
]

#: 用途常量——chat 会话调用恒以 chat 用途落用量账（usage 域枚举校验）。
CHAT_PURPOSE: str = "chat"

#: chat 工具循环步数上限（锁定决策：5——chat 单轮回复的工具面收敛快于
#: duty 巡检的 8 步；超限触发收尾指令 + 无工具面最终调用）。
CHAT_TOOL_STEP_CAP: Final[int] = 5

#: 收尾指令（步数上限触发——system 追加块，本轮不再携带工具面）。
#: 就地定义说明：chat/prompts.py 不在本变更允许清单内（prompt Unit
#: 文件迁移属后续独立变更）；文本经测试锁定。
_TOOL_LOOP_FINAL_INSTRUCTION: Final[str] = (
    "【收尾指令】\n工具调用步数已用尽。请立即基于已有信息直接回复用户，不再调用任何工具。"
)


# ---------------------------------------------------------------------------
# 工具结果反馈渲染（chat 本地——duty render_tool_result 的 chat 侧对应物）
# ---------------------------------------------------------------------------


def _render_tool_result_content(result: ToolResultModel) -> str:
    """工具执行结果 → tool 角色消息正文。

    优先取执行器预格式化 ``text`` 块（web_search 的紧凑中文结果块）；
    其余结果整体紧凑 JSON（与 duty ``render_tool_result`` 同信息量——
    ok / result / error / 时延全量反馈，失败形态原样供模型自行调整）。

    Args:
        result: 执行结果（registry 归一终态）。

    Returns:
        tool 消息正文文本。
    """
    if result.ok:
        preformatted = result.result.get("text")
        if isinstance(preformatted, str) and preformatted:
            return preformatted
    return json.dumps(result.model_dump(), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 消息组装（纯函数——测试直调面）
# ---------------------------------------------------------------------------


def _split_sections(
    book_entries: Sequence[CharaBookEntryModel], selection: WorldBookSelectionResult
) -> tuple[list[CharaBookEntryModel], list[CharaBookEntryModel]]:
    """选择结果 → before/after 两段条目模型列表（按入选索引回查）。

    Args:
        book_entries: 世界书条目列表（索引回查源——卡片快照会话期
            不可变，索引即稳定身份）。
        selection: 选择结果（渲染序平铺列表）。

    Returns:
        ``(before_char 段, after_char 段)`` 二元组（各自保持渲染序）。
    """
    before: list[CharaBookEntryModel] = []
    after: list[CharaBookEntryModel] = []
    for selected in selection.entries:
        target = before if selected.section == CharaBookPosition.BEFORE_CHAR else after
        target.append(book_entries[selected.index])
    return before, after


def compose_messages(
    state: ChatSessionStateModel,
    *,
    rng: random.Random | None = None,
    emotion_summary: str | None = None,
    directives: Sequence[str] | None = None,
    facts: Sequence[str] | None = None,
    skills: Sequence[SkillRecordDTO] | None = None,
) -> MessageCompositionResult:
    """组装完整请求消息序列（纯函数——无 I/O / 无网关调用 / 无会话回写）。

    消息序列结构::

        [system]  章程 + 世界书 before 段 + 人格正典 + 心情状态块 +
                  长期指令块 + 人物事实块 + 相关技能块 + 输出格式协议块 +
                  世界书 after 段
        [user/assistant ...]  全部历史回合（含刚追加的用户回合）
        [system]  历史后再注入（卡片声明 post_history_instructions 时；
                  位于**最后一条历史回合之后**——deep anti-drift 位置）

    心情状态块仅在 ``emotion_summary`` 非空时注入（非中性情绪——由
    ChatService 经 emotion 域读取 M 态产出）；长期指令块仅在
    ``directives`` 有有效条目时注入（现行指令——由 ChatService 经
    DirectivesService 读取；20 条 / 2000 字符帽由
    ``render_directives_block`` 施加）；人物事实块仅在 ``facts`` 有有效
    条目时注入（【关于对方】——由 ChatService 经事实读取面提供；条数 /
    字符帽在读取面施加）；相关技能块仅在 ``skills`` 有命中条目时注入
    （【相关技能】——由 ChatService 经技能读取面按本轮用户文本匹配；
    条数帽在读取面、字符帽在 ``render_skills_block`` 施加）；输出格式
    协议块恒注入（心声 / 情绪标签指令——解析面见
    ``chat/output_protocol``）。

    世界书选择经 ``world_book.select_world_book_entries``（算法链与
    渲染序裁定见该模块文档）；推进后的回合状态（冷却时钟 + 注入
    追踪）随结果携带——**本函数不回写会话态**（功能式：调用方
    ``ChatService`` 回存；直调测试忽略即丢弃）。

    Args:
        state: 会话状态（卡片快照 + 回合列表 + 世界书回合状态）。
        rng: 概率门随机源（None = 现场构造非种子实例；仅卡片声明
            ``trigger_probability < 1.0`` 时消耗——缺省卡片零随机性）。
        emotion_summary: 非中性情绪摘要行（None / 空白 = 不注入心情块；
            M 态读取与衰减在调用方——本函数保持纯函数无 I/O）。
        directives: 现行长期指令正文列表（None / 空 = 不注入长期指令块；
            读取在调用方——本函数保持纯函数无 I/O）。
        facts: 现行人物事实注入行列表（``【类别标签】正文``——None / 空 =
            不注入人物事实块；读取与裁剪在调用方——本函数保持纯函数无
            I/O）。
        skills: 命中本轮用户文本的技能记录列表（None / 空 = 不注入相关
            技能块；匹配与条数帽在调用方、字符帽在 ``render_skills_block``
            ——本函数保持纯函数无 I/O）。

    Returns:
        组装结果（消息序列 + 观测元数据 + 推进后的世界书回合状态）。
    """
    selection = select_world_book_entries(
        state.card,
        [turn.content for turn in state.turns],
        rng=rng,
        turn_state=state.world_book_state,
    )
    book = state.card.character_book
    before_entries: list[CharaBookEntryModel] = []
    after_entries: list[CharaBookEntryModel] = []
    if book is not None:
        before_entries, after_entries = _split_sections(book.entries, selection)
    system_parts: list[str] = [render_charter()]
    before_block = render_world_book_block(before_entries)
    if before_block:
        system_parts.append(before_block)
    system_parts.append(render_persona_system_prompt(state.card))
    emotion_block = render_emotion_state_block(emotion_summary)
    if emotion_block:
        system_parts.append(emotion_block)
    directives_block = render_directives_block(directives or [])
    if directives_block:
        system_parts.append(directives_block)
    facts_block = render_facts_block(facts or [])
    if facts_block:
        system_parts.append(facts_block)
    skills_block = render_skills_block(skills or [])
    if skills_block:
        system_parts.append(skills_block)
    system_parts.append(render_output_protocol())
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
        included_book_entries=selection.included,
        estimated_system_tokens=estimate_text_tokens(system_prompt),
        world_book_state=selection.turn_state,
        emotion_summary=emotion_summary if emotion_block else None,
    )


# ---------------------------------------------------------------------------
# 会话服务
# ---------------------------------------------------------------------------


class ToolExecutorFace(Protocol):
    """chat 工具执行面协议（duty 工具注册表的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.tools.registry.ToolRegistry`
    天然满足（同签名 ``execute_call``——恒不抛异常的隔离执行契约）；
    测试注入 fake 同形态。
    """

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """执行单次工具调用（异常隔离——归一 ``ok=False`` 结果，不上抛）。

        Args:
            call: 执行请求（工具名 + 参数 dict）。

        Returns:
            执行结果（ok / result / error / 时延）。
        """
        ...


class IntentShortcutFace(Protocol):
    """chat 意图快径面协议（intent 域服务的结构化消费契约）。

    结构化类型：真实
    :class:`~packages.harness.intent.service.IntentShortcutService` 天然
    满足（同签名 ``try_shortcut`` / ``realism_delay``）；测试注入 fake
    同形态。注入面为**通道路径专属**（app 装配决策——CLI / duty 不注入，
    见模块 docstring「意图快径前钩」一节）。
    """

    async def try_shortcut(
        self, text: str, persona: PersonaNaming, now_ms: int | None = None
    ) -> IntentMatchModel:
        """尝试意图快径匹配（恒不抛异常——内部异常归一未命中）。

        Args:
            text: 用户消息原文。
            persona: 人格命名面（会话状态同形——称呼提取原料）。
            now_ms: 判定时刻（epoch 毫秒；None = 服务注入时钟）。

        Returns:
            匹配结果（``handled=False`` = 未命中——照常走 LLM）。
        """
        ...

    async def realism_delay(self) -> None:
        """快径拟真延迟（800-1500ms 随机 + 休眠面等待——注入式可测）。

        Raises:
            Exception: 休眠面异常（透传——注入面契约）。
        """
        ...


class FactsReaderFace(Protocol):
    """compose 人物事实读取面协议（persona 域事实服务的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.facts.FactsService`
    天然满足（``top_facts_for_compose``——现行高置信事实的注入行读取，
    条数 / 字符帽在实现侧施加）；测试注入 fake 同形态。注入面与长期指令
    同轴：仅在持久化会话已落库时读取（见模块 docstring「人物事实注入」）。
    """

    async def top_facts_for_compose(self, session: AsyncSession) -> list[str]:
        """读取 compose 注入行（现行高置信事实——实现侧裁剪）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            注入行列表（``【类别标签】正文``；空列表 = 无现行事实）。
        """
        ...


class SkillsReaderFace(Protocol):
    """compose 技能读取面协议（persona 域技能服务的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.skills.SkillsService`
    天然满足（``match_for_compose``——按本轮用户文本匹配现行技能，条数帽
    在实现侧施加；命中后的使用计数写入由实现侧 fire-and-forget 调度）；
    测试注入 fake 同形态。注入面与人物事实同轴：仅在持久化会话已落库时
    读取（见模块 docstring「相关技能注入」）。
    """

    async def match_for_compose(
        self, session: AsyncSession, user_text: str
    ) -> list[SkillRecordDTO]:
        """匹配本轮用户文本可用的技能（实现侧裁剪与强化）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            user_text: 本轮用户输入原文。

        Returns:
            命中技能记录列表（最多两条；空列表 = 无命中）。
        """
        ...


class ChatService:
    """人格感知聊天会话服务（组装 + 网关调用 + 会话内存态维护 + 可选持久化）。

    构造注入（测试 / 装配友好）：网关 / 人格服务 / 会话持久化服务 /
    LLM 工具面均可显式注入；测试注入 fake 网关记录组装消息断言组装契约
    （零真实网络）。``session_service=None``（缺省）即纯内存形态——
    持久化可选。``tools=None``（缺省）即零工具形态——既有 reply 行为
    全量保持（CLI 不传工具面——零变更）。
    """

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        persona_service: PersonaService | None = None,
        session_service: ChatSessionService | None = None,
        tools: Sequence[ToolSpecModel] | None = None,
        tool_executor: ToolExecutorFace | None = None,
        intent_shortcut: IntentShortcutFace | None = None,
        emotion_service: EmotionService | None = None,
        directive_service: DirectivesService | None = None,
        facts_reader: FactsReaderFace | None = None,
        skills_reader: SkillsReaderFace | None = None,
        episode_sink: EpisodeSinkFace | None = None,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（chat 角色调用面）。
            persona_service: 人格注册表服务（主人格解析；None = 现场
                构造无状态默认实例）。
            session_service: 会话持久化服务（None = 纯内存态——兼容既有
                形态；绑定后 create_session(db_session=...) 落库、reply /
                stream_reply 逐回合持久化、resume_session 可用）。
            tools: LLM 工具规格列表（None / 空 = 零工具形态——既有
                行为；非空时 ``reply`` 走有界工具循环，与
                ``tool_executor`` 成对注入）。
            tool_executor: 工具执行面（``ToolRegistry`` 结构满足；
                与 ``tools`` 成对注入——缺一即装配错误 fail loud）。
            intent_shortcut: 意图快径面（None = 零快径形态——既有行为；
                注入后 ``reply`` 前端先试快径，命中即罐头回复 + 跳过
                网关——通道装配专属注入面，见模块 docstring）。
            emotion_service: 轻情绪引擎服务（None = 现场构造默认实例——
                读 ``EMOTION_`` env；M 态读写仅在持久化会话绑定且已落库
                时发生——纯内存形态零附加 I/O，见模块 docstring）。
            directive_service: 长期指令服务（None = 不注入长期指令块——
                行为与既有完全一致；注入后持久化会话回合在 compose 前
                读取现行指令——纯内存形态零附加 I/O，见模块 docstring）。
            facts_reader: 人物事实读取面（None = 不注入人物事实块——行为
                与既有完全一致；注入后持久化会话回合在 compose 前读取
                现行高置信事实——纯内存形态零附加 I/O，见模块
                docstring「人物事实注入」）。
            skills_reader: 技能读取面（None = 不注入相关技能块——行为与
                既有完全一致；注入后持久化会话回合在 compose 前按本轮
                用户文本匹配现行技能——纯内存形态零附加 I/O，见模块
                docstring「相关技能注入」）。
            episode_sink: chat 回合情景落库面（None = 零情景写入——CLI /
                未装配记忆服务的形态行为与既有完全一致；注入后每回合
                消息落库即写入 kind=chat 情景事件——fire-and-forget 语义
                与失败吞没见 ``_record_episode``）。

        Raises:
            BusinessException: ``tools`` 与 ``tool_executor`` 未成对注入
                （``INTERNAL_ERROR``——装配面编程错误）。
        """
        self._gateway = gateway
        self._persona_service = persona_service if persona_service is not None else PersonaService()
        self._session_service = session_service
        #: 意图快径面绑定（None = 零快径形态——reply 前钩判定面）。
        self._intent_shortcut = intent_shortcut
        #: 轻情绪引擎绑定（默认实例读 EMOTION_ env——停用时零读写）。
        self._emotion_service = emotion_service if emotion_service is not None else EmotionService()
        #: 长期指令服务绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._directive_service = directive_service
        #: 人物事实读取面绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._facts_reader = facts_reader
        #: 技能读取面绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._skills_reader = skills_reader
        #: 情景落库面绑定（None = 零情景写入——记忆管道未接线形态）。
        self._episode_sink = episode_sink
        bound_tools = list(tools) if tools else []
        if bound_tools and tool_executor is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="chat 工具面装配错误：注入 tools 须同时注入 tool_executor",
            )
        if not bound_tools and tool_executor is not None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="chat 工具面装配错误：注入 tool_executor 须同时注入 tools",
            )
        #: 工具面绑定（None = 零工具形态；成对原子绑定——定义列表 +
        #: 执行面，杜绝单侧注入）。定义面经 ToolSpecModel →
        #: ToolDefinitionModel 一次转换（name / description / parameters
        #: 三键直映——args_model 校验留在 executor 侧 registry）。
        self._tools_binding: tuple[list[ToolDefinitionModel], ToolExecutorFace] | None = None
        if bound_tools and tool_executor is not None:
            self._tools_binding = (
                [
                    ToolDefinitionModel(
                        name=spec.name,
                        description=spec.description,
                        parameters=spec.args_schema,
                    )
                    for spec in bound_tools
                ],
                tool_executor,
            )

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        persona: PersonaRecordDTO | None = None,
        db_session: AsyncSession | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """创建聊天会话（快照人格正典 + 绑定 persona 轴；持久化可选）。

        持久化条件：``session_service`` 已绑定 **且** ``db_session`` 已
        提供（二者缺一即纯内存态）；落库经
        ``bind_persona_context`` 绑定人格上下文（PersonaMixin 自动注入）。

        Args:
            persona: 显式人格记录（CLI / 测试直供形态；优先于查库）。
            db_session: 数据库会话（``persona`` 未给时用于解析当前
                主人格；持久化服务绑定时兼作会话落库通道）。
            title: 持久化会话标题（None / 空 = 缺省「未命名会话」）。

        Returns:
            新建的 :class:`ChatSessionStateModel`（空回合列表；持久化
            时携带 ``session_id``）。

        Raises:
            BusinessException: 既未提供人格也未提供数据库会话
                （``INTERNAL_ERROR``）；当前无主人格（``NOT_FOUND``——
                服务层清晰中文错误直传，指引先 set-primary）。
        """
        record = persona
        if record is None:
            if db_session is None:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="创建聊天会话需要显式人格或数据库会话（二者至少其一）",
                )
            record = await self._persona_service.get_primary_persona(db_session)
        state = ChatSessionStateModel(
            persona_id=record.id,
            persona_name=record.name,
            display_name=record.display_name,
            card=record.card,
        )
        if self._session_service is not None and db_session is not None:
            async with bind_persona_context(record.id):
                summary = await self._session_service.create_session(db_session, title=title)
            state.session_id = summary.id
            logger.info("聊天会话已落库: persona_id={}, session_id={}", record.id, summary.id)
        logger.info("聊天会话已创建: persona_id={}, persona_name={}", record.id, record.name)
        return state

    async def resume_session(
        self, session_id: int, *, db_session: AsyncSession
    ) -> ChatSessionStateModel:
        """恢复持久化会话到内存态（全量读取、窗口截断后回放；卡片重取当前正典）。

        回放语义：持久化消息**全量读取**（append-only 完整记录不丢失），
        但仅回放**最近 ``max_history_turns`` 条**进内存态 ``turns``——
        与 ``append_turn`` 的滑动窗口同口径（内存窗口是上下文组装面，
        超窗历史不再进入后续 compose；持久化行不受影响）。

        人格作用域契约：会话行经 PersonaMixin 自动过滤——**调用方须先
        绑定目标人格上下文**（``bind_persona_context``）；他人格会话
        不可见（``NOT_FOUND``）。

        Args:
            session_id: 持久化会话主键（ff_chat_sessions.id）。
            db_session: 数据库会话（会话读取 + 人格正典重取）。

        Returns:
            恢复后的 :class:`ChatSessionStateModel`（携带 session_id +
            窗口截断后的回放回合列表——保留最新回合）。

        Raises:
            BusinessException: 持久化服务未绑定（``INTERNAL_ERROR``）；
                会话不存在 / 他人格会话 / 会话归属人格已删除
                （``NOT_FOUND``）。
        """
        if self._session_service is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="会话持久化服务未绑定，无法恢复会话",
            )
        detail = await self._session_service.get_session(db_session, session_id)
        persona = await self._persona_service.get(db_session, int(detail.persona_id))
        state = ChatSessionStateModel(
            persona_id=persona.id,
            persona_name=persona.name,
            display_name=persona.display_name,
            card=persona.card,
            session_id=detail.id,
        )
        replay_window = detail.messages[-state.max_history_turns :]
        for message in replay_window:
            state.turns.append(
                ChatTurnModel(role=message.role, content=message.content, ts_ms=message.ts_ms)
            )
        logger.info(
            "聊天会话已恢复: session_id={}, persona_id={}, persisted={}, replayed={}",
            detail.id,
            persona.id,
            len(detail.messages),
            len(replay_window),
        )
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

    async def reply(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> str:
        """单轮回复（非流式）：追加用户回合 → 组装 → 网关 chat_by_role。

        意图快径分派（R2a-6——``intent_shortcut`` 绑定时）：``reply``
        前端先试快径（``try_shortcut``）；命中（``handled=True``）时
        追加用户回合 → 拟真延迟（``realism_delay``——800-1500ms 随机）
        → 罐头文本追加为 assistant 回合（持久化语义与正常回复**完全
        同构**——用户 + 助手回合均落库，记忆 / 梦境可见；网关调用完全
        跳过）；未命中照常走组装 / 工具循环 / 网关链路。快径面自身
        恒不抛异常（未命中兜底——见 :class:`IntentShortcutFace`）。

        工具面分派：``tools`` 绑定时走有界工具循环（步数上限 + 收尾
        forcing——见模块 docstring「可选工具循环」；工具步为循环内部
        消息，不进入会话回合 / 持久化）；未绑定为既有单次调用形态。

        失败语义：网关异常原样上抛（用户回合保留在历史——调用方决定
        重试或提示）。重试语义（文档化决策）：调用方对 ``reply`` 的任何
        重试都是**完整重发**——用户消息再次进入会话历史；持久化绑定且
        已落库时同样再落一行用户消息（append-only 日志不静默丢行）。
        显式的「重生成 / 复用上一条用户输入」API 有意不在本方法提供
        （推迟至后续 swipes 重试面特性）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。
            sender_provenance: 发送者来源分级（私聊 owner / 群聊 peer——
                情景落档按真实层级；缺省 owner 保持既有语义）。

        Returns:
            **可见正文**（已追加为 assistant 回合；持久化绑定且已落库时
            用户 + 助手回合均落 ``ff_chat_messages``——仅最终 assistant
            可见文本，工具中间态与协议标签均不落库正文；心声 / 情绪
            增量只进回合 meta；快径命中时为罐头文本，网关零调用）。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；模型返回
                空回复（``LLM_CALL_FAILED``）；网关链路异常（透传）；
                持久化写入失败（透传——append-only 日志不静默丢行）。
        """
        if self._intent_shortcut is not None:
            match = await self._intent_shortcut.try_shortcut(user_text, state)
            if match.handled:
                logger.debug(
                    "意图快径命中（跳过网关）: persona={}, kind={}, confidence={:.2f}",
                    state.persona_name,
                    match.kind.value if match.kind is not None else "none",
                    match.confidence,
                )
                return await self._reply_via_shortcut(
                    state,
                    user_text,
                    match.response_text,
                    self._intent_shortcut,
                    sender_provenance=sender_provenance,
                )
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        emotion_summary = await self._current_emotion_summary(state)
        directives = await self._current_directives(state)
        facts = await self._current_facts(state)
        skills = await self._current_skills(state, user_text)
        result = compose_messages(
            state,
            emotion_summary=emotion_summary,
            directives=directives,
            facts=facts,
            skills=skills,
        )
        # 功能式回合状态回存：组装纯函数不回写会话态，冷却时钟推进由
        # 本服务消费后落回（快径路径不组装、不推进——时钟只度量真实
        # 组装回合）。
        state.world_book_state = result.world_book_state
        if self._tools_binding is not None:
            raw_content = await self._tool_loop(result.messages, state, self._tools_binding)
        else:
            response: ChatResponseModel = await self._gateway.chat_by_role(
                ModelRole.CHAT,
                result.messages,
                persona_id=str(state.persona_id),
                purpose=CHAT_PURPOSE,
            )
            raw_content = response.content
        parsed = parse_assistant_output(raw_content)
        content = parsed.visible_text
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型返回空回复",
                details={"persona_id": state.persona_id},
            )
        meta = await self._emotion_round_meta(state, parsed)
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, content)
        await self._persist_turn(state, assistant_turn, meta=meta)
        await self._record_episode(state, assistant_turn)
        return content

    async def stream_reply(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> AsyncIterator[str]:
        """单轮流式回复：追加用户回合 → 组装 → 网关 stream_by_role。

        增量语义：逐块经 ``StreamTagStripper`` 做**起始前缀** best-effort
        标签剥离后 yield（空增量块跳过；未知 / 未闭合标签原样放行）；
        流自然结束后聚合已放行文本追加为 assistant 回合。本路径**不跑
        完整输出协议**：心声 / 情绪增量不落 meta、不推进 M 态（流式
        通道是操作员可见面——边界文档化于模块 docstring）。消费方提前终止
        （``GeneratorExit``）时**不追加助手回合**——半截回复不入历史
        （文档化决策；持久化面同语义——半截助手回合不落库）。重试语义
        同 ``reply``：调用方重试是完整重发——用户回合已落库时再落一行
        （append-only；显式重生成 / 复用 API 推迟至后续 swipes 特性）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。

        Yields:
            回复增量文本块。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；流结束但
                聚合全文为空（``LLM_CALL_FAILED``）；网关链路异常（透传）；
                持久化写入失败（透传）。
        """
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        emotion_summary = await self._current_emotion_summary(state)
        directives = await self._current_directives(state)
        facts = await self._current_facts(state)
        skills = await self._current_skills(state, user_text)
        result = compose_messages(
            state,
            emotion_summary=emotion_summary,
            directives=directives,
            facts=facts,
            skills=skills,
        )
        # 功能式回合状态回存（与 reply 同构——冷却时钟只度量真实组装回合）。
        state.world_book_state = result.world_book_state
        stripper = StreamTagStripper()
        parts: list[str] = []
        async for chunk in self._gateway.stream_by_role(
            ModelRole.CHAT,
            result.messages,
            persona_id=str(state.persona_id),
            purpose=CHAT_PURPOSE,
        ):
            if not chunk.content:
                continue
            emitted = stripper.feed(chunk.content)
            if not emitted:
                continue
            parts.append(emitted)
            yield emitted
        tail = stripper.flush()
        if tail:
            parts.append(tail)
            yield tail
        content = "".join(parts).strip()
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型流式回复为空",
                details={"persona_id": state.persona_id},
            )
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, content)
        await self._persist_turn(state, assistant_turn)
        await self._record_episode(state, assistant_turn)

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

    async def _tool_loop(
        self,
        messages: Sequence[ChatMessageModel],
        state: ChatSessionStateModel,
        binding: tuple[list[ToolDefinitionModel], ToolExecutorFace],
    ) -> str:
        """有界工具循环（返回最终回复文本；机械镜像 duty_service._tool_loop）。

        循环消息为本地副本（组装结果 + assistant 工具调用轮回放 + tool
        反馈轮）——**不进入 ``state.turns`` / 持久化**（仅最终 assistant
        文本由 ``reply`` 落回合）。

        Args:
            messages: 组装结果消息序列（system + 历史 + 历史后再注入）。
            state: 会话状态（persona_id 归属面）。
            binding: 工具面绑定（定义列表 + 执行面——reply 分派传入）。

        Returns:
            最终回复文本（空串 = 模型未产出——调用方归
            ``LLM_CALL_FAILED``）。

        Raises:
            Exception: 网关链路异常（原样上抛——与零工具形态同失败语义；
            ``tool_executor.execute_call`` 自身恒不抛）。
        """
        definitions, executor = binding
        loop_messages: list[ChatMessageModel] = list(messages)
        persona_id = str(state.persona_id)
        for _ in range(CHAT_TOOL_STEP_CAP):
            response = await self._gateway.chat_by_role(
                ModelRole.CHAT,
                loop_messages,
                tools=definitions,
                persona_id=persona_id,
                purpose=CHAT_PURPOSE,
            )
            if not response.tool_calls:
                return response.content.strip()
            loop_messages.append(
                ChatMessageModel(
                    role=ChatMessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                result = await executor.execute_call(
                    ToolCallRequestModel(name=call.name, arguments=call.arguments)
                )
                loop_messages.append(
                    ChatMessageModel(
                        role=ChatMessageRole.TOOL,
                        content=_render_tool_result_content(result),
                        tool_call_id=call.id,
                    )
                )
        # 步数上限触发：收尾指令 + 无工具面最终调用（final-report forcing）。
        loop_messages.append(
            ChatMessageModel(role=ChatMessageRole.SYSTEM, content=_TOOL_LOOP_FINAL_INSTRUCTION)
        )
        final_response = await self._gateway.chat_by_role(
            ModelRole.CHAT,
            loop_messages,
            persona_id=persona_id,
            purpose=CHAT_PURPOSE,
        )
        return final_response.content.strip()

    async def _reply_via_shortcut(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        canned_text: str,
        shortcut: IntentShortcutFace,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> str:
        """快径回复落回合（用户回合 → 拟真延迟 → 罐头助手回合；零网关）。

        持久化语义与 ``reply`` 正常路径**完全同构**（``_append_user_turn``
        空白校验 + ``_persist_turn`` 逐回合落库）——快径对话是真实对话
        史的一部分（记忆 / 梦境消费面可见）。拟真延迟位于用户回合落库
        之后、助手回合落库之前（时间戳语义：用户消息到达时刻 → 人格
        「打字」耗时 → 回复产出时刻）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝——与正常路径同校验）。
            canned_text: 罐头回复文本（快径匹配层产出——非空契约）。
            shortcut: 快径面（拟真延迟消费——调用方已判非 None）。

        Returns:
            罐头回复文本（已追加为 assistant 回合）。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``——与正常
                路径同边界）；持久化写入失败（透传——append-only 日志
                不静默丢行）。
        """
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        await shortcut.realism_delay()
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, canned_text)
        await self._persist_turn(state, assistant_turn)
        await self._record_episode(state, assistant_turn)
        return assistant_turn.content

    def _append_user_turn(self, state: ChatSessionStateModel, user_text: str) -> ChatTurnModel:
        """追加用户回合（空白输入拒绝——边界校验）。

        Args:
            state: 会话状态。
            user_text: 用户输入。

        Returns:
            已追加的 :class:`ChatTurnModel`（持久化面消费）。

        Raises:
            BusinessException: 输入为空 / 纯空白（``VALIDATION_ERROR``）。
        """
        try:
            return state.append_turn(ChatMessageRole.USER, user_text)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="用户输入不能为空",
            ) from exc

    async def _persist_turn(
        self,
        state: ChatSessionStateModel,
        turn: ChatTurnModel,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """持久化单回合（持久化绑定 + 会话已落库时；否则 no-op）。

        经 ``session_scope`` 独立短事务写入（CLI / API 入口零 Session
        透传负担）；人格上下文按会话归属人格绑定（消息行非 PersonaMixin
        ——上下文为会话行更新面服务）。失败原样上抛（append-only 日志
        不静默丢行——文档化决策）。

        Args:
            state: 会话状态（``session_id`` 为 None 时 no-op）。
            turn: 待落库回合。
            meta: 回合元数据（心声 / 情绪增量 / 情绪快照；None = 无
                元数据——既有调用方零变更）。
        """
        if self._session_service is None or state.session_id is None:
            return
        async with session_scope() as db_session, bind_persona_context(state.persona_id):
            await self._session_service.append_message(
                db_session,
                state.session_id,
                role=turn.role,
                content=turn.content,
                ts_ms=turn.ts_ms,
                meta=meta,
            )

    async def _record_episode(
        self,
        state: ChatSessionStateModel,
        turn: ChatTurnModel,
        user_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> None:
        """把已落库回合并入情景层（sink 未接线 / 会话未落库时 no-op）。

        时序：**消息行提交之后**调用（回合持久化是情景镜像的前提）；
        ``session_id`` 为 None（纯内存态）时零写入——情景事件必须可回溯
        到会话。失败仅日志：sink 契约恒不抛，本方法仍显式兜底（回复链路
        的失败路径不应包含记忆管道面）。

        Args:
            state: 会话状态（``session_id`` / ``persona_id`` 为事件归属面）。
            turn: 已落库回合（角色 / 正文 / 时间——事件载荷来源）。
        """
        if self._episode_sink is None or state.session_id is None:
            return
        try:
            await self._episode_sink.record_chat_turn(
                state.persona_id,
                state.session_id,
                turn.role.value,
                turn.content,
                turn.ts_ms,
                user_provenance=user_provenance,
            )
        except Exception:  # best-effort: 记忆管道面不进回复失败路径（显式兜底）
            logger.opt(exception=True).warning(
                "chat 回合情景落库失败（已吞没——不影响回复）: session_id={}",
                state.session_id,
            )

    def _emotion_runtime_active(self, state: ChatSessionStateModel) -> bool:
        """情绪引擎运行判定（持久化会话已落库 + 引擎启用）。

        M 态存储面依赖数据库会话——纯内存形态 / 未落库会话一律停机
        （零附加 I/O，既有纯内存测试面零变更）。

        Args:
            state: 会话状态。

        Returns:
            True = 本次回复可读 / 写 M 态情绪。
        """
        return (
            self._session_service is not None
            and state.session_id is not None
            and self._emotion_service.enabled
        )

    async def _current_emotion_summary(self, state: ChatSessionStateModel) -> str | None:
        """读取当前情绪摘要行（compose 心情块注入面——非中性才非空）。

        Args:
            state: 会话状态。

        Returns:
            中文摘要行；引擎未激活 / 无状态 / 全中性时为 None。
        """
        if not self._emotion_runtime_active(state):
            return None
        return await self._emotion_service.summary(state.persona_id)

    async def _current_directives(self, state: ChatSessionStateModel) -> list[str]:
        """读取现行长期指令（compose 长期指令块注入面）。

        运行判定：持久化会话已落库 + 服务已接线（与情绪引擎同轴——M 态 /
        指令一律依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复
        （日志 + 空列表——常备指令是增强面）。

        Args:
            state: 会话状态。

        Returns:
            现行指令正文列表（新写入在前）；未接线 / 未落库 / 读取失败
            时为空列表。
        """
        if (
            self._directive_service is None
            or self._session_service is None
            or state.session_id is None
        ):
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._directive_service.list_active_texts(db_session)
        except Exception:  # best-effort：指令读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "长期指令读取失败（本轮不注入长期指令块）: persona_id={}", state.persona_id
            )
            return []

    async def _current_facts(self, state: ChatSessionStateModel) -> list[str]:
        """读取现行人物事实注入行（compose 人物事实块注入面）。

        运行判定：持久化会话已落库 + 读取面已接线（与长期指令同轴——画像
        依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复（日志 +
        空列表——画像是增强面）。

        Args:
            state: 会话状态。

        Returns:
            注入行列表（置信度降序、读取面已裁剪）；未接线 / 未落库 /
            读取失败时为空列表。
        """
        if self._facts_reader is None or self._session_service is None or state.session_id is None:
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._facts_reader.top_facts_for_compose(db_session)
        except Exception:  # best-effort：事实读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "人物事实读取失败（本轮不注入人物事实块）: persona_id={}", state.persona_id
            )
            return []

    async def _current_skills(
        self, state: ChatSessionStateModel, user_text: str
    ) -> list[SkillRecordDTO]:
        """匹配本轮可用的技能（compose 相关技能块注入面）。

        运行判定：持久化会话已落库 + 读取面已接线（与人物事实同轴——
        技能依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复
        （日志 + 空列表——技能是操作增强面）。服务质量面自身已按条数帽
        裁剪，命中计数写入由其 fire-and-forget 调度（本方法不等待）。

        Args:
            state: 会话状态。
            user_text: 本轮用户输入原文（匹配面输入——只有命中当前输入
                的技能才会注入）。

        Returns:
            命中技能记录列表；未接线 / 未落库 / 读取失败时为空列表。
        """
        if self._skills_reader is None or self._session_service is None or state.session_id is None:
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._skills_reader.match_for_compose(db_session, user_text)
        except Exception:  # best-effort：技能读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "技能匹配失败（本轮不注入相关技能块）: persona_id={}", state.persona_id
            )
            return []

    async def _emotion_round_meta(
        self, state: ChatSessionStateModel, parsed: ParsedAssistantOutputModel
    ) -> dict[str, Any] | None:
        """构建本轮回合元数据（心声 + 情绪增量 + 推进后快照）。

        引擎未激活时仍保留心声 / 增量留档（观测面），快照为 None；
        无心声且无增量时返回 None（零 meta 写入——既有行形态不变）。

        Args:
            state: 会话状态。
            parsed: 输出协议解析结果。

        Returns:
            回合元数据字典；无任何元数据时为 None。
        """
        if parsed.thought is None and not parsed.emotion_deltas:
            return None
        snapshot: dict[str, float] | None = None
        if self._emotion_runtime_active(state) and parsed.emotion_deltas:
            advanced = await self._emotion_service.apply_deltas(
                state.persona_id, parsed.emotion_deltas
            )
            if advanced is not None:
                snapshot = dict(advanced.values)
        return {
            "thought": parsed.thought,
            "emotion_deltas": dict(parsed.emotion_deltas),
            "emotion_snapshot": snapshot,
        }
