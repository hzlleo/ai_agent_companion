"""harness/persona chara card 模型测试——v2 规格校验 + 序列化往返。

纯单元（零 DB / 零配置）：卡片是人格 L 层正典，校验面（非空 name /
insertion_order 界 / selective 副键一致性）在此锁定。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookModel,
    CharaBookPosition,
    CharaCardModel,
)


def _card(**overrides: object) -> CharaCardModel:
    """构造最小合法卡片（name 必填，其余缺省）。"""
    payload: dict[str, object] = {"name": "大肥鱼"}
    payload.update(overrides)
    return CharaCardModel.model_validate(payload)


# ---------------------------------------------------------------------------
# 基础卡片
# ---------------------------------------------------------------------------


async def test_card_requires_non_empty_name() -> None:
    """非空 name 校验：空串 / 缺失均拒绝。"""
    with pytest.raises(ValidationError):
        CharaCardModel(name="")
    with pytest.raises(ValidationError):
        CharaCardModel.model_validate({})  # name 缺失（运行期校验目标）


async def test_card_defaults_and_full_fields() -> None:
    """缺省字段全空串 + 全字段赋值往返。"""
    minimal = _card()
    assert minimal.description == ""
    assert minimal.personality == ""
    assert minimal.first_mes == ""
    assert minimal.character_book is None

    full = _card(
        description="一条会说话的胖鱼",
        personality="温柔、爱操心、有点啰嗦",
        scenario="住在主人书房的鱼缸里",
        first_mes="你回来啦！今天过得怎么样？",
        mes_example="<START>\n{{user}}: 我累了\n{{char}}: 那就歇会儿，我陪你。",
        system_prompt="你必须始终以大肥鱼的口吻回复。",
        post_history_instructions="保持回复简短，两三句以内。",
        character_book=CharaBookModel(
            name="feiyu-world",
            entries=[
                CharaBookEntryModel(
                    keys=["鱼缸", "书房"],
                    content="大肥鱼住在主人书房的圆形鱼缸里。",
                    insertion_order=10,
                )
            ],
        ),
    )
    assert full.character_book is not None
    assert full.character_book.entries[0].keys == ["鱼缸", "书房"]
    assert full.character_book.entries[0].insertion_order == 10
    # JSON 往返（落库形态 ↔ 读取形态）
    roundtrip = CharaCardModel.model_validate(full.model_dump())
    assert roundtrip == full


async def test_entry_insertion_order_bounds() -> None:
    """insertion_order 界校验：负数 / 超上界（9999）拒绝，边界值通过。"""
    with pytest.raises(ValidationError):
        CharaBookEntryModel(keys=["k"], insertion_order=-1)
    with pytest.raises(ValidationError):
        CharaBookEntryModel(keys=["k"], insertion_order=10_000)
    assert CharaBookEntryModel(keys=["k"], insertion_order=0).insertion_order == 0
    assert CharaBookEntryModel(keys=["k"], insertion_order=9999).insertion_order == 9999


async def test_entry_defaults() -> None:
    """条目缺省：constant / case_sensitive / selective False + after_char。"""
    entry = CharaBookEntryModel(keys=["k"], content="正文")
    assert entry.constant is False
    assert entry.case_sensitive is False
    assert entry.selective is False
    assert entry.position == CharaBookPosition.AFTER_CHAR
    assert entry.secondary_keys == []


async def test_entry_selective_secondary_keys_rule() -> None:
    """规格一致性：非选择性条目携带 secondary_keys 拒绝；选择性携带通过。"""
    with pytest.raises(ValidationError, match="secondary_keys"):
        CharaBookEntryModel(keys=["k"], secondary_keys=["sk"], selective=False)
    selective = CharaBookEntryModel(
        keys=["旅行"], secondary_keys=["计划"], selective=True, case_sensitive=True
    )
    assert selective.selective is True
    assert selective.secondary_keys == ["计划"]


async def test_entry_position_values() -> None:
    """position 值域：before_char / after_char 均可显式指定。"""
    before = CharaBookEntryModel(keys=["k"], position=CharaBookPosition.BEFORE_CHAR)
    assert before.position == "before_char"
    after = CharaBookEntryModel(keys=["k"], position=CharaBookPosition.AFTER_CHAR)
    assert after.position == "after_char"


async def test_book_fields_roundtrip() -> None:
    """世界书字段（扫描深度 / token 预算 / 递归扫描）+ 多条目往返。"""
    book = CharaBookModel(
        name="world",
        description="世界观设定",
        scan_depth=4,
        token_budget=800,
        recursive_scanning=True,
        entries=[
            CharaBookEntryModel(keys=["a"], content="A", insertion_order=0),
            CharaBookEntryModel(keys=["b"], content="B", insertion_order=1, constant=True),
        ],
    )
    roundtrip = CharaBookModel.model_validate(book.model_dump())
    assert roundtrip == book
    assert roundtrip.entries[1].constant is True
    # 深度 / 预算下界校验（ge=1）
    with pytest.raises(ValidationError):
        CharaBookModel(scan_depth=0)
    with pytest.raises(ValidationError):
        CharaBookModel(token_budget=0)
