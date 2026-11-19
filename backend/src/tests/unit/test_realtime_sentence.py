"""实时语音句切测试——标点 / 收尾符号 / 长度上限 / 流式累积 / 冲刷。

锁定语义：句末标点保留在句子内、收尾引号不拆散、超长句优先在停顿标点处
切分（找不到则硬切到上限）、空片段不产出、流式累积与整段切分同规则。
"""

from __future__ import annotations

import pytest

from packages.harness.voice.realtime_sentence import (
    SentenceAccumulator,
    split_assistant_sentences,
)


async def test_split_on_sentence_terminators_keeps_punctuation() -> None:
    """句末标点与收尾引号并入本句（不拆散“。”）。"""
    text = "今天降温了。“记得穿外套。”早点休息！"
    assert split_assistant_sentences(text, max_chars=80) == [
        "今天降温了。",
        "“记得穿外套。”",
        "早点休息！",
    ]


async def test_split_handles_newline_and_repeated_terminators() -> None:
    """换行与重复标点都构成边界（问号感叹号连写不丢字）。"""
    text = "真的吗？！\n好啊……那就这样。"
    sentences = split_assistant_sentences(text, max_chars=80)
    assert sentences[0] == "真的吗？！"
    assert sentences[1] == "好啊……"
    assert sentences[2] == "那就这样。"


async def test_split_long_sentence_prefers_pause_points() -> None:
    """超长句优先在停顿标点切分，且每段不超过上限。"""
    text = "第一段很长很长很长，第二段也很长很长很长，第三段同样很长很长。"
    sentences = split_assistant_sentences(text, max_chars=30)
    assert sentences
    assert all(len(sentence) <= 30 for sentence in sentences)
    assert "".join(sentences).replace(" ", "") == text
    assert any(sentence.endswith("，") for sentence in sentences)


async def test_split_long_sentence_without_pause_is_hard_cut() -> None:
    """无停顿标点的超长串按上限硬切（不丢字符）。"""
    text = "啊" * 50
    sentences = split_assistant_sentences(text, max_chars=20)
    assert [len(sentence) for sentence in sentences] == [20, 20, 10]


async def test_split_drops_blank_fragments_and_keeps_unicode() -> None:
    """纯空白片段丢弃；中英文混合标点均识别。"""
    text = "   \n 你好! Hello?\n\n"
    assert split_assistant_sentences(text, max_chars=80) == ["你好!", "Hello?"]


async def test_split_empty_text_returns_empty_list() -> None:
    """空输入 / 纯空白 → 空列表（不产出空句）。"""
    assert split_assistant_sentences("", max_chars=20) == []
    assert split_assistant_sentences("   ", max_chars=20) == []


async def test_split_rejects_non_positive_limit() -> None:
    """上限非法 → ValueError（调用方 bug 面）。"""
    with pytest.raises(ValueError):
        split_assistant_sentences("你好", max_chars=0)


async def test_accumulator_emits_sentences_across_deltas() -> None:
    """流式增量跨块拼出完整句（同块多句也不合并）。"""
    accumulator = SentenceAccumulator(max_chars=80)
    assert accumulator.feed("你好") == []
    assert accumulator.state.pending_chars == 2
    first = accumulator.feed("呀。今天")
    assert first == ["你好呀。"]
    second = accumulator.feed("怎么样？")
    assert second == ["今天怎么样？"]
    assert accumulator.state.emitted_sentences == 2
    assert accumulator.state.pending_chars == 0


async def test_accumulator_caps_unterminated_buffer() -> None:
    """未闭合长句超过上限时提前切出（避免长时间无音频），且不超限。"""
    accumulator = SentenceAccumulator(max_chars=10)
    emitted = accumulator.feed("一二三四五六七八九十十一十二")
    assert emitted
    assert all(len(sentence) <= 10 for sentence in emitted)
    assert accumulator.state.pending_chars <= 10


async def test_accumulator_flush_returns_tail_and_clears() -> None:
    """冲刷返回未闭合尾句；再次冲刷为空。"""
    accumulator = SentenceAccumulator(max_chars=80)
    accumulator.feed("没有句号的尾巴")
    assert accumulator.flush() == ["没有句号的尾巴"]
    assert accumulator.flush() == []


async def test_accumulator_ignores_empty_delta_and_rejects_bad_limit() -> None:
    """空增量零产出；非法上限构造期拒绝。"""
    accumulator = SentenceAccumulator(max_chars=20)
    assert accumulator.feed("") == []
    with pytest.raises(ValueError):
        SentenceAccumulator(max_chars=0)
