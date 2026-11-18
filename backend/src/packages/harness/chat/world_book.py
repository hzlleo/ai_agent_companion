"""世界书高级筛选纯函数模块（R2b——检索增强条目的选择算法链）。

从 service.py 抽出为独立 Unit 文件的决策（文档化）：筛选算法是纯函数
契约（可注入 rng / 回合状态、独立测试面），service.py 专注组装编排与
会话循环——两文件各自单一职责。本模块不导入 chat 域其他 Unit
（chat_models / service / prompts），只依赖 persona 域卡片模型——
依赖方向无环。

选择算法链（顺序锁定 + 测试覆盖；每候选按链序过门，任一门拒绝即出局）::

    ① constant 恒注入        constant 条目直接进候选集（免触发；无视
                             分组互斥 / 概率门 / 冷却门；不占 token 预算）
    ② 关键词匹配             最近 scan_depth 条回合文本拼接为扫描面；
                             keys 任一命中 + selective 条目再要求
                             secondary_keys 任一命中；大小写按条目声明
                             （既有语义原样保持）
    ③ 递归扩展（2 跳上限）   书级 recursive_scanning 总开关 + 条目级
                             recursive 逐条目收窄同时开启时生效：已入选
                             条目的正文追加进扫描面再匹配新条目；跳 1
                             （首轮入选者的正文入面）→ 跳 2（跳 1 新入
                             选者的正文入面）→ 停（跳 2 入选者正文不再
                             入面）；visited 集合保证条目至多入选一次、
                             正文至多入面一次（环不可能由构造保证）
    ④ 互斥分组剪枝           非 constant 候选按 include_group 分组，
                             同组只留优先级最高者（insertion_order 升序、
                             同值按条目索引升序——确定性）；constant
                             条目无视分组（不参与也不被排斥）
    ⑤ 概率门（rng）          非 constant 候选 trigger_probability < 1.0
                             时消耗一次 rng.random() < p 判定（=1.0 恒
                             过门不消耗随机数——既有卡片零随机性；=0.0
                             恒拒）；消耗序 = 渲染序（种子 rng 可复现）
    ⑥ 冷却门（回合状态）     非 constant 候选上次注入于第 F 回合、当前
                             第 C 回合时，1 <= C - F <= cooldown_turns
                             即拒绝（注入后 N 个组装回合内不可再注入，
                             第 N+1 回合解禁；=0 恒过门）
    ⑦ token 预算装箱         候选按渲染序累计 estimate_text_tokens；
                             首条超预算即截止（其后非 constant 候选不再
                             评估——既有预算耗尽语义）；constant 不占预算

渲染序（DECIDE 的精确裁定，文档化锁定）：条目恒渲染在 position 决定的
段内（before_char 段在人格正典前 / after_char 段在后——既有段位语义
不变，prompt 模板字节零改动）；段内按 ``(depth 降序, insertion_order
升序, 条目索引升序)`` 排列。``depth`` 语义是「距对话末尾的近因深度」
（0 = 最贴近末尾）：世界书条目渲染于角色定义段（position 段位语义 +
模板字节锁定），故 depth 落位为**段内深度槽**——段是深度分级队列，
depth 0 为段尾槽（紧邻其后渲染的对话历史一侧 = 最贴近末条消息的可用
槽位），值越大越靠段首（越远离对话末尾）；同槽回退 insertion_order。
**兼容证明**：既有卡片全部条目缺省 depth=4（同槽）→ 段内退化为纯
insertion_order 升序 = 既有行为逐字节一致（既有测试零变更即为证明）。

回合状态（functional state-passing——无隐藏变更）：``compose_turn``
为组装调用计数（每次调用 +1，含无书卡片——统一时钟）；选中注入的非
constant 条目记 ``injected_turns[条目索引] = 本次回合号``。冷却状态为
**会话作用域内存态**（ChatSessionStateModel 携带，无持久化 schema 变更
——重启即复位，诚实文档化）；条目索引在会话内稳定（卡片快照会话期
不可变——见 chat_models 模块文档）。

随机性契约：``rng`` 可注入（``random.Random``）；缺省 1.0 概率条目
零消耗——只有显式声明 ``trigger_probability < 1.0`` 的卡片才引入随机
性（测试注入种子 rng 全量可复现）。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, Field

from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookPosition,
    CharaCardModel,
)

__all__ = [
    "RECURSION_HOP_CAP",
    "SelectedEntryModel",
    "WorldBookTurnStateModel",
    "WorldBookSelectionResult",
    "select_world_book_entries",
    "estimate_text_tokens",
]

#: 递归扩展跳数上限（全局：跳 1 + 跳 2；跳 2 入选条目正文不再入面）。
RECURSION_HOP_CAP: Final[int] = 2


# ---------------------------------------------------------------------------
# token 近似估算（无 tokenizer——结构性预算的确定性估算）
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
    """粗估文本 token 数（无 tokenizer 的确定性近似）。

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
# 选择结果 / 回合状态 Model Classes
# ---------------------------------------------------------------------------


class SelectedEntryModel(BaseModel):
    """已入选条目（选择算法链的输出契约——渲染序平铺列表元素）。

    Attributes:
        index: 条目在世界书 ``entries`` 列表中的索引（会话内稳定——
            卡片快照不可变；组装层据此回查条目模型渲染）。
        content: 条目正文（冗余承便于纯函数测试直断言）。
        section: 插入段（= 条目 ``position``——before/after char 段）。
        depth: 深度槽位（0-20；渲染排序首选键的镜像）。
        insertion_order: 插入顺序（同槽排序回退键）。
    """

    index: int = Field(ge=0, description="条目索引（世界书 entries 内）")
    content: str = Field(description="条目正文")
    section: CharaBookPosition = Field(description="插入段（条目 position）")
    depth: int = Field(ge=0, le=20, description="深度槽位")
    insertion_order: int = Field(ge=0, le=9999, description="插入顺序")


class WorldBookTurnStateModel(BaseModel):
    """世界书回合状态（冷却判定的时钟 + 注入追踪——会话作用域内存态）。

    功能式状态传递：选择函数**不变更**入参状态——复制后推进，经结果
    模型返回给会话层回存（无隐藏变更；纯函数可重复求值）。

    Attributes:
        compose_turn: 组装调用计数（每次选择 +1；含无书卡片——统一
            时钟；快径回复不组装、不推进）。
        injected_turns: 注入追踪 ``{条目索引: 注入回合号}``（仅非
            constant 注入条目记录；冷却门据此判定）。
    """

    compose_turn: int = Field(default=0, ge=0, description="组装调用计数")
    injected_turns: dict[int, int] = Field(
        default_factory=dict, description="注入追踪 {条目索引: 注入回合号}"
    )


class WorldBookSelectionResult(BaseModel):
    """选择结果（已入选条目平铺列表 + 推进后的回合状态）。

    Attributes:
        entries: 渲染序平铺列表（before_char 段在前 / after_char 段在
            后；段内 depth 降序 → insertion_order 升序 → 索引升序）。
        included: 实际注入条目数（constant + 触发过门者）。
        turn_state: 推进后的回合状态（会话层回存面——冷却 bookkeeping）。
    """

    entries: list[SelectedEntryModel] = Field(default_factory=list, description="渲染序入选列表")
    included: int = Field(default=0, ge=0, description="实际注入条目数")
    turn_state: WorldBookTurnStateModel = Field(
        default_factory=WorldBookTurnStateModel, description="推进后的回合状态"
    )


# ---------------------------------------------------------------------------
# 匹配原语（既有语义原样保持）
# ---------------------------------------------------------------------------


def _entry_matches(entry: CharaBookEntryModel, scan_text: str) -> bool:
    """单条目关键词触发判定（primary + selective secondary）。

    Args:
        entry: 世界书条目（大小写敏感性按条目声明）。
        scan_text: 扫描文本（历史窗口或含递归正文的扩展面）。

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


# ---------------------------------------------------------------------------
# 选择算法链（纯函数）
# ---------------------------------------------------------------------------


def select_world_book_entries(
    card: CharaCardModel,
    recent_history_texts: Sequence[str],
    *,
    rng: random.Random | None = None,
    turn_state: WorldBookTurnStateModel | None = None,
) -> WorldBookSelectionResult:
    """筛选应注入的世界书条目（算法链见模块文档；纯函数）。

    纯函数契约：不变更入参（回合状态复制后推进、经结果返回）；可注入
    rng（缺省 1.0 概率条目零消耗）与回合状态（缺省零态）；同输入同
    rng 序列恒同输出。

    Args:
        card: 人格卡片（无世界书 / 空条目集时返回空列表 + 推进后回合
            状态——统一时钟含无书卡片）。
        recent_history_texts: 会话回合文本序列（时间序；书级
            ``scan_depth`` 窗口切片在本函数内完成）。
        rng: 随机源（None = 现场构造非种子实例——生产熵源；测试注入
            种子实例复现概率门行为）。
        turn_state: 会话当前回合状态（None = 零态首回合；入参不变更）。

    Returns:
        选择结果（渲染序入选列表 + 注入数 + 推进后回合状态）。
    """
    state = (
        turn_state.model_copy(deep=True)
        if turn_state is not None
        else WorldBookTurnStateModel()
    )
    state.compose_turn += 1
    book = card.character_book
    if book is None or not book.entries:
        return WorldBookSelectionResult(entries=[], included=0, turn_state=state)

    bound_rng = rng if rng is not None else random.Random()

    # ② 关键词匹配的扫描面：scan_depth 窗口切片（None = 全部历史）。
    window = (
        list(recent_history_texts)
        if book.scan_depth is None
        else list(recent_history_texts[-book.scan_depth :])
    )
    scan_parts: list[str] = ["\n".join(window)]

    # ① constant 恒注入 + ② 关键词首轮匹配（visited 集合 = selected 键集）。
    selected: dict[int, CharaBookEntryModel] = {}
    for index, entry in enumerate(book.entries):
        if entry.constant or _entry_matches(entry, scan_parts[0]):
            selected[index] = entry

    # ③ 递归扩展（2 跳上限）：frontier = 上一轮新入选且 recursive 的条目；
    # 其正文追加进扫描面；跳 2 入选者不进 frontier（其正文不再入面）。
    if book.recursive_scanning:
        frontier = [index for index, entry in selected.items() if entry.recursive]
        for _ in range(RECURSION_HOP_CAP):
            if not frontier:
                break
            scan_parts.append("\n".join(book.entries[index].content for index in frontier))
            extended_surface = "\n".join(scan_parts)
            newly_matched: list[int] = []
            for index, entry in enumerate(book.entries):
                if index in selected or entry.constant:
                    continue
                if _entry_matches(entry, extended_surface):
                    selected[index] = entry
                    newly_matched.append(index)
            frontier = [index for index in newly_matched if book.entries[index].recursive]

    # ④ 互斥分组剪枝：非 constant 候选同组只留 (insertion_order, 索引)
    # 升序优先级最高者（确定性）；constant 无视分组恒保留。
    group_winner: dict[str, int] = {}
    for index, entry in selected.items():
        if entry.constant or entry.include_group is None:
            continue
        group = entry.include_group
        incumbent = group_winner.get(group)
        if incumbent is None or (entry.insertion_order, index) < (
            book.entries[incumbent].insertion_order,
            incumbent,
        ):
            group_winner[group] = index
    pruned: dict[int, CharaBookEntryModel] = {
        index: entry
        for index, entry in selected.items()
        if entry.constant
        or entry.include_group is None
        or group_winner[entry.include_group] == index
    }

    # 渲染序（段位 → 深度槽降序 → insertion_order → 索引）：
    # 兼容证明——缺省 depth=4 同槽时退化为既有 (position, insertion_order) 序。
    render_ordered = sorted(
        pruned.items(),
        key=lambda item: (
            0 if item[1].position == CharaBookPosition.BEFORE_CHAR else 1,
            -item[1].depth,
            item[1].insertion_order,
            item[0],
        ),
    )

    # ⑤⑥⑦ 渲染序走门：概率门 → 冷却门 → token 预算装箱（链序锁定）。
    emitted: list[SelectedEntryModel] = []
    remaining = book.token_budget
    budget_exhausted = False
    for index, entry in render_ordered:
        if entry.constant:
            # constant 不占预算、不过概率 / 冷却门（恒注入语义）。
            emitted.append(_to_selected(index, entry))
            continue
        if budget_exhausted:
            # 预算耗尽：其后非 constant 候选不再评估（既有截止语义）。
            continue
        if entry.trigger_probability < 1.0 and bound_rng.random() >= entry.trigger_probability:
            continue
        fired_turn = state.injected_turns.get(index)
        elapsed = state.compose_turn - fired_turn if fired_turn is not None else None
        if elapsed is not None and 1 <= elapsed <= entry.cooldown_turns:
            continue
        cost = estimate_text_tokens(entry.content)
        if remaining is not None and cost > remaining:
            budget_exhausted = True
            continue
        if remaining is not None:
            remaining -= cost
        emitted.append(_to_selected(index, entry))
        state.injected_turns[index] = state.compose_turn

    return WorldBookSelectionResult(
        entries=emitted,
        included=len(emitted),
        turn_state=state,
    )


def _to_selected(index: int, entry: CharaBookEntryModel) -> SelectedEntryModel:
    """条目模型 → 已入选契约（选择输出的纯映射）。

    Args:
        index: 条目索引（世界书 entries 内）。
        entry: 条目模型。

    Returns:
        已入选条目契约实例。
    """
    return SelectedEntryModel(
        index=index,
        content=entry.content,
        section=entry.position,
        depth=entry.depth,
        insertion_order=entry.insertion_order,
    )
