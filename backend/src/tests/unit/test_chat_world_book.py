"""harness/chat world_book 纯选择函数测试——R2b 高级触发算法链。

测试策略：纯函数直调（零 DB / 零网关 / 零 I/O）；rng 注入种子实例
（概率门可复现）、回合状态功能式传递（入参不变更断言）。覆盖面：
constant 恒注入 / 关键词窗口 / selective 副键 / 递归 2 跳上限与环
安全 / 互斥分组确定性 / 概率门三态 / 冷却门阻塞-解禁 / token 预算
截止 / 深度槽渲染序 / 状态累积回传 / 缺省字段 = 既有行为（兼容证明）。
"""

from __future__ import annotations

import random

from packages.harness.chat.world_book import (
    RECURSION_HOP_CAP,
    WorldBookTurnStateModel,
    select_world_book_entries,
)
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaCardModel,
)


def _card(book: CharaBookModel | None = None) -> CharaCardModel:
    """构造测试卡片（世界书可缺省）。"""
    return CharaCardModel(name="大肥鱼", character_book=book)


def _book(**overrides: object) -> CharaBookModel:
    """构造世界书（条目 / scan_depth / token_budget 可覆写）。"""
    fields: dict[str, object] = {
        "name": "测试世界书",
        "scan_depth": None,
        "token_budget": None,
        "entries": [],
    }
    fields.update(overrides)
    return CharaBookModel.model_validate(fields)


def _select(
    book: CharaBookModel,
    history: list[str],
    *,
    rng: random.Random | None = None,
    turn_state: WorldBookTurnStateModel | None = None,
) -> tuple[list[str], WorldBookTurnStateModel]:
    """选择直调辅助：返回 (入选条目正文列表, 推进后回合状态)。"""
    result = select_world_book_entries(
        _card(book), history, rng=rng, turn_state=turn_state
    )
    return [selected.content for selected in result.entries], result.turn_state


# ---------------------------------------------------------------------------
# 既有语义保持（兼容证明的纯函数面）
# ---------------------------------------------------------------------------


async def test_constant_entries_always_selected_without_budget() -> None:
    """constant 恒注入：无历史 / 预算 1 / 概率 0 / 冷却 50 均不拦。"""
    book = _book(
        token_budget=1,
        entries=[
            CharaBookEntryModel(
                keys=[],
                content="世界观保底",
                constant=True,
                trigger_probability=0.0,
                cooldown_turns=50,
            ),
        ],
    )
    contents, _ = _select(book, [])
    assert contents == ["世界观保底"]


async def test_keyword_match_respects_scan_depth_window() -> None:
    """关键词窗口：仅最近 scan_depth 条回合参与扫描。"""
    book = _book(
        scan_depth=2,
        entries=[CharaBookEntryModel(keys=["公园"], content="公园设定")],
    )
    contents, _ = _select(book, ["上次去公园玩了", "今天忙什么呢", "在吗"])
    assert contents == []
    contents, _ = _select(book, ["今天忙什么呢", "在吗", "想去公园走走"])
    assert contents == ["公园设定"]


async def test_selective_entry_requires_secondary_within_surface() -> None:
    """selective：主键 + 副键须同在扫描面命中。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["晚饭"],
                secondary_keys=["小鱼干"],
                content="晚饭设定",
                selective=True,
            ),
        ],
    )
    contents, _ = _select(book, ["晚饭吃什么"])
    assert contents == []
    contents, _ = _select(book, ["晚饭想吃小鱼干"])
    assert contents == ["晚饭设定"]


async def test_defaults_render_order_equals_legacy_insertion_order() -> None:
    """兼容证明：缺省 depth=4 同槽 → 段内纯 insertion_order 升序。"""
    book = _book(
        entries=[
            CharaBookEntryModel(keys=["行为"], content="行为条目", insertion_order=100),
            CharaBookEntryModel(
                keys=["世界"], content="世界观条目", insertion_order=50, position="before_char"
            ),
            CharaBookEntryModel(
                keys=["地理"], content="地理条目", insertion_order=10, position="before_char"
            ),
            CharaBookEntryModel(keys=["气候"], content="气候条目", insertion_order=50),
        ],
    )
    contents, _ = _select(book, ["聊聊世界地理气候行为"])
    assert contents == ["地理条目", "世界观条目", "气候条目", "行为条目"]


# ---------------------------------------------------------------------------
# 递归扩展（2 跳上限 + 环安全）
# ---------------------------------------------------------------------------


async def test_recursive_expansion_two_hop_cap() -> None:
    """递归 2 跳上限：跳 2 入选条目（C）正文不再入面（D 不被触发）。"""
    book = _book(
        recursive_scanning=True,
        entries=[
            CharaBookEntryModel(keys=["苹果"], content="甲提到香蕉", recursive=True),
            CharaBookEntryModel(keys=["香蕉"], content="乙提到樱桃", recursive=True),
            CharaBookEntryModel(keys=["樱桃"], content="丙提到榴莲", recursive=True),
            CharaBookEntryModel(keys=["榴莲"], content="丁不应入选"),
        ],
    )
    contents, _ = _select(book, ["今天买了苹果"])
    assert contents == ["甲提到香蕉", "乙提到樱桃", "丙提到榴莲"]
    assert RECURSION_HOP_CAP == 2


async def test_recursive_cycle_terminates_by_visited_set() -> None:
    """环安全：A⇄B 互指内容关键词——visited 集合保证终止、不重复入选。"""
    book = _book(
        recursive_scanning=True,
        entries=[
            CharaBookEntryModel(keys=["甲"], content="内容含乙", recursive=True),
            CharaBookEntryModel(keys=["乙"], content="内容含甲", recursive=True),
        ],
    )
    contents, _ = _select(book, ["开头说甲"])
    assert contents == ["内容含乙", "内容含甲"]


async def test_recursive_requires_entry_level_flag() -> None:
    """逐条目收窄：同组跳 1 入选但 recursive=False 的条目正文不入面。"""
    book = _book(
        recursive_scanning=True,
        entries=[
            CharaBookEntryModel(keys=["苹果"], content="提到香蕉但不递归"),
            CharaBookEntryModel(keys=["香蕉"], content="不应入选"),
        ],
    )
    contents, _ = _select(book, ["今天买了苹果"])
    assert contents == ["提到香蕉但不递归"]


# ---------------------------------------------------------------------------
# 互斥分组（确定性优先级）
# ---------------------------------------------------------------------------


async def test_group_mutual_exclusion_keeps_highest_priority() -> None:
    """同组两条命中：只注入 insertion_order 更小者（升序优先）。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["问候"], content="问候变体甲", insertion_order=10, include_group="greet"
            ),
            CharaBookEntryModel(
                keys=["问候"], content="问候变体乙", insertion_order=5, include_group="greet"
            ),
        ],
    )
    contents, _ = _select(book, ["打个问候"])
    assert contents == ["问候变体乙"]


async def test_group_tie_breaks_by_entry_index() -> None:
    """同组同 insertion_order：条目索引小者胜（确定性平局裁定）。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["问候"], content="先声明条目", insertion_order=7, include_group="g"
            ),
            CharaBookEntryModel(
                keys=["问候"], content="后声明条目", insertion_order=7, include_group="g"
            ),
        ],
    )
    contents, _ = _select(book, ["打个问候"])
    assert contents == ["先声明条目"]


async def test_constant_entry_ignores_group_exclusion() -> None:
    """constant 无视分组：同组 constant + 命中条目互不排斥（双双注入）。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=[], content="保底条目", constant=True, include_group="g"
            ),
            CharaBookEntryModel(keys=["话题"], content="触发条目", include_group="g"),
        ],
    )
    contents, _ = _select(book, ["聊话题"])
    assert contents == ["保底条目", "触发条目"]


# ---------------------------------------------------------------------------
# 概率门（rng 可注入）
# ---------------------------------------------------------------------------


async def test_probability_zero_never_injects() -> None:
    """概率 0：恒拒（仍消耗一次 rng——消耗序 = 渲染序）。"""
    rng = random.Random(1)
    book = _book(
        entries=[CharaBookEntryModel(keys=["天气"], content="天气条目", trigger_probability=0.0)],
    )
    contents, _ = _select(book, ["今天天气如何"], rng=rng)
    assert contents == []


async def test_probability_one_always_injects_without_consuming_rng() -> None:
    """概率 1（缺省）：恒过门且零 rng 消耗——既有卡片零随机性。"""
    rng = random.Random(7)
    book = _book(
        entries=[CharaBookEntryModel(keys=["天气"], content="天气条目")],
    )
    contents, _ = _select(book, ["今天天气如何"], rng=rng)
    assert contents == ["天气条目"]
    assert rng.random() == random.Random(7).random()


async def test_probability_half_with_seeded_rng_draws_in_render_order() -> None:
    """概率 0.5 + 种子 rng：按渲染序逐条消耗——首条过门、次条被拒。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["天气"], content="天气条目", insertion_order=1, trigger_probability=0.5
            ),
            CharaBookEntryModel(
                keys=["天气"], content="气候条目", insertion_order=2, trigger_probability=0.5
            ),
        ],
    )
    # 种子 1 的前两次 draw：0.134…（<0.5 过门）、0.847…（≥0.5 拒）。
    probe = random.Random(1)
    first_draw, second_draw = probe.random(), probe.random()
    assert first_draw < 0.5 <= second_draw
    contents, _ = _select(book, ["今天天气如何"], rng=random.Random(1))
    assert contents == ["天气条目"]


# ---------------------------------------------------------------------------
# 冷却门（回合状态——功能式传递）
# ---------------------------------------------------------------------------


async def test_cooldown_blocks_then_re_allows() -> None:
    """冷却 2 回合：第 1 回合注入 → 第 2/3 回合阻塞 → 第 4 回合解禁。"""
    book = _book(
        entries=[CharaBookEntryModel(keys=["问候"], content="问候条目", cooldown_turns=2)],
    )
    first_contents, state = _select(book, ["打个问候"])
    assert first_contents == ["问候条目"]
    assert state.injected_turns == {0: 1}

    blocked_1, state = _select(book, ["打个问候"], turn_state=state)
    assert blocked_1 == []
    blocked_2, state = _select(book, ["打个问候"], turn_state=state)
    assert blocked_2 == []
    # 解禁后重新注入并刷新注入回合号。
    allowed, state = _select(book, ["打个问候"], turn_state=state)
    assert allowed == ["问候条目"]
    assert state.injected_turns == {0: 4}


async def test_cooldown_zero_never_blocks() -> None:
    """冷却 0（缺省）：连续回合恒可注入。"""
    book = _book(
        entries=[CharaBookEntryModel(keys=["问候"], content="问候条目")],
    )
    contents, state = _select(book, ["打个问候"])
    assert contents == ["问候条目"]
    contents, state = _select(book, ["打个问候"], turn_state=state)
    assert contents == ["问候条目"]
    assert state.injected_turns == {0: 2}


async def test_turn_state_passing_is_functional_no_input_mutation() -> None:
    """功能式状态传递：入参回合状态不变更（副本推进经结果回传）。"""
    book = _book(
        entries=[CharaBookEntryModel(keys=["问候"], content="问候条目", cooldown_turns=5)],
    )
    initial = WorldBookTurnStateModel(compose_turn=9, injected_turns={0: 3})
    _, advanced = _select(book, ["打个问候"], turn_state=initial)
    assert initial.compose_turn == 9
    assert initial.injected_turns == {0: 3}
    assert advanced.compose_turn == 10
    assert advanced.injected_turns == {0: 10}


async def test_compose_turn_ticks_without_book() -> None:
    """统一时钟：无世界书 / 空条目集卡片同样推进回合计数（口径一致）。"""
    bookless = select_world_book_entries(_card(None), [])
    assert bookless.entries == []
    assert bookless.turn_state.compose_turn == 1
    again = select_world_book_entries(_card(None), [], turn_state=bookless.turn_state)
    assert again.turn_state.compose_turn == 2
    empty_book = select_world_book_entries(_card(_book()), [], turn_state=again.turn_state)
    assert empty_book.entries == []
    assert empty_book.turn_state.compose_turn == 3


# ---------------------------------------------------------------------------
# token 预算装箱（渲染序累计 + 截止语义）
# ---------------------------------------------------------------------------


async def test_token_budget_first_overflow_cuts_rest() -> None:
    """预算截止：首条超预算即截止，其后非 constant 候选不再评估。"""
    book = _book(
        token_budget=15,
        entries=[
            CharaBookEntryModel(keys=["甲"], content="甲条目设定文本共计十字"),
            CharaBookEntryModel(keys=["乙"], content="乙条目设定文本共计十字"),
            CharaBookEntryModel(keys=["丙"], content="丙条目设定文本共计十字"),
        ],
    )
    contents, state = _select(book, ["聊聊甲乙丙"])
    assert contents == ["甲条目设定文本共计十字"]
    # 预算截断的条目未注入 → 不记冷却（未消耗注入回合）。
    assert state.injected_turns == {0: 1}


async def test_constant_entries_do_not_consume_budget() -> None:
    """constant 不占预算：预算耗尽后 constant 仍然注入。"""
    book = _book(
        token_budget=5,
        entries=[
            CharaBookEntryModel(keys=["甲"], content="甲条目设定文本共计十字"),
            CharaBookEntryModel(keys=[], content="世界观保底", constant=True),
        ],
    )
    contents, _ = _select(book, ["聊聊甲"])
    assert "甲条目设定文本共计十字" not in contents
    assert "世界观保底" in contents


# ---------------------------------------------------------------------------
# 深度槽渲染序（DECIDE 裁定的锁定测试）
# ---------------------------------------------------------------------------


async def test_depth_slots_render_deeper_first_depth_zero_last() -> None:
    """深度槽：段内 depth 降序（深者靠段首）；同槽 insertion_order 升序。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["话题"], content="深度零晚槽", depth=0, insertion_order=900
            ),
            CharaBookEntryModel(
                keys=["话题"], content="深度九早槽", depth=9, insertion_order=100
            ),
            CharaBookEntryModel(
                keys=["话题"], content="深度零早序", depth=0, insertion_order=50
            ),
        ],
    )
    contents, _ = _select(book, ["聊话题"])
    assert contents == ["深度九早槽", "深度零早序", "深度零晚槽"]


async def test_depth_ordering_scoped_within_position_sections() -> None:
    """深度槽段内生效：before_char 深槽仍整体先于 after_char 浅槽渲染。"""
    book = _book(
        entries=[
            CharaBookEntryModel(
                keys=["话题"], content="后段浅槽", depth=0, position="after_char"
            ),
            CharaBookEntryModel(
                keys=["话题"], content="前段深槽", depth=20, position="before_char"
            ),
        ],
    )
    contents, _ = _select(book, ["聊话题"])
    assert contents == ["前段深槽", "后段浅槽"]
