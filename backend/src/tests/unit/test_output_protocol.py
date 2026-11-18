"""output_protocol 解析器测试——标签语法 / 宽容降级 / 流式前缀剥离。

测试策略：纯函数直调（无 I/O / 无网关）；覆盖完整矩阵——心声单标签 /
情绪单标签 / 双标签（两种次序）/ 无标签 / 形态残缺 / 未闭合 / 未知
维度 / 越界夹取 / 重复标签 / 纯标签空正文；流式剥离器覆盖标签前缀扣留、
未知标签直通、未闭合 flush 交还、直通后零改写。
"""

from __future__ import annotations

from packages.harness.chat.output_protocol import (
    StreamTagStripper,
    parse_assistant_output,
    strip_leading_protocol_prefix,
)

# ---------------------------------------------------------------------------
# 非流式解析面
# ---------------------------------------------------------------------------


async def test_parse_thought_only() -> None:
    """只有心声标签：可见正文剥离标签，thought 提取。"""
    parsed = parse_assistant_output("<心声>他今天又迟到了，会不会有麻烦。</心声>你来啦，快坐。")
    assert parsed.visible_text == "你来啦，快坐。"
    assert parsed.thought == "他今天又迟到了，会不会有麻烦。"
    assert parsed.emotion_deltas == {}


async def test_parse_emotion_only() -> None:
    """只有情绪标签：增量解析 + 正文剥离。"""
    parsed = parse_assistant_output("<情绪>affection:+0.3, shyness:0.2</情绪>嗯……你回来啦。")
    assert parsed.visible_text == "嗯……你回来啦。"
    assert parsed.thought is None
    assert parsed.emotion_deltas == {"affection": 0.3, "shyness": 0.2}


async def test_parse_both_tags_in_protocol_order() -> None:
    """双标签（协议声明次序）：心声 + 情绪 + 正文全部正确切分。"""
    parsed = parse_assistant_output(
        "<心声>想多问一句，又怕他嫌烦。</心声><情绪>anxiety:+0.2</情绪>今天还顺利吗？"
    )
    assert parsed.visible_text == "今天还顺利吗？"
    assert parsed.thought == "想多问一句，又怕他嫌烦。"
    assert parsed.emotion_deltas == {"anxiety": 0.2}


async def test_parse_both_tags_order_tolerant() -> None:
    """双标签（逆序）：情绪在前、心声在后同样正确切分（次序容忍）。"""
    parsed = parse_assistant_output("<情绪>joy:+0.4</情绪><心声>他心情不错。</心声>嘿嘿。")
    assert parsed.visible_text == "嘿嘿。"
    assert parsed.thought == "他心情不错。"
    assert parsed.emotion_deltas == {"joy": 0.4}


async def test_parse_no_tags_passthrough() -> None:
    """无标签（不合规模型）：原文可见、零情绪。"""
    parsed = parse_assistant_output("  今天好累呀，早点休息。  ")
    assert parsed.visible_text == "今天好累呀，早点休息。"
    assert parsed.thought is None
    assert parsed.emotion_deltas == {}


async def test_parse_unclosed_thought_degrades_to_raw() -> None:
    """心声未闭合：整体降级（原文可见——绝不吞正文）。"""
    raw = "<心声>还没说完的念头，正文"
    parsed = parse_assistant_output(raw)
    assert parsed.visible_text == raw
    assert parsed.thought is None
    assert parsed.emotion_deltas == {}


async def test_parse_partial_tag_prefix_degrades_to_raw() -> None:
    """标签区截断在开标签中途（``<心``）：降级原文。"""
    parsed = parse_assistant_output("<心")
    assert parsed.visible_text == "<心"
    assert parsed.thought is None


async def test_parse_malformed_tag_body_keeps_visible_text() -> None:
    """情绪标签内容全非法：标签消费、可见正文正常（不抛异常）。"""
    parsed = parse_assistant_output("<情绪>乱写的东西</情绪>你好。")
    assert parsed.visible_text == "你好。"
    assert parsed.emotion_deltas == {}


async def test_parse_unknown_dimension_dropped() -> None:
    """未知情绪维度：丢弃（debug 记录），合法维度保留。"""
    parsed = parse_assistant_output("<情绪>melancholy:0.5, joy:0.2</情绪>嗯。")
    assert parsed.visible_text == "嗯。"
    assert parsed.emotion_deltas == {"joy": 0.2}


async def test_parse_out_of_range_delta_clamped() -> None:
    """增量越界：夹取到 [-1, 1]（两个方向）。"""
    parsed = parse_assistant_output("<情绪>joy:+3.5, sadness:-2</情绪>没事的。")
    assert parsed.emotion_deltas == {"joy": 1.0, "sadness": -1.0}


async def test_parse_duplicate_tag_first_wins() -> None:
    """同种标签重复：首个生效，重复标签连带内容被消费丢弃（不泄漏）。"""
    parsed = parse_assistant_output("<心声>第一句</心声><心声>第二句</心声>正文。")
    assert parsed.thought == "第一句"
    assert parsed.visible_text == "正文。"
    assert "<心声>" not in parsed.visible_text


async def test_parse_tags_only_yields_empty_visible_text() -> None:
    """纯标签回复：可见正文为空串（调用方按空回复语义处理）。"""
    parsed = parse_assistant_output("<心声>只有一个念头。</心声>")
    assert parsed.visible_text == ""
    assert parsed.thought == "只有一个念头。"


async def test_parse_full_width_and_space_separators() -> None:
    """全角冒号 / 中文逗号分隔的情绪条目可解析。"""
    parsed = parse_assistant_output("<情绪>calm：0.1，excitement:0.2</情绪>好啦。")
    assert parsed.emotion_deltas == {"calm": 0.1, "excitement": 0.2}


async def test_parse_unknown_leading_tag_passthrough() -> None:
    """未知前导标签（非协议标签）：不消费、整体可见。"""
    parsed = parse_assistant_output("<div>你好</div>")
    assert parsed.visible_text == "<div>你好</div>"
    assert parsed.thought is None


async def test_strip_leading_protocol_prefix_helper() -> None:
    """一次性剥离辅助：标签区剥离；未闭合标签原样保留。"""
    assert strip_leading_protocol_prefix("<心声>x</心声><情绪>joy:0.1</情绪>正文") == "正文"
    assert strip_leading_protocol_prefix("<心声>未闭合") == "<心声>未闭合"


# ---------------------------------------------------------------------------
# 流式剥离器
# ---------------------------------------------------------------------------


async def test_stream_stripper_plain_text_passthrough_immediately() -> None:
    """普通正文：首块即放行并转入直通（零延迟改写）。"""
    stripper = StreamTagStripper()
    assert stripper.feed("今天") == "今天"
    assert stripper.feed("好累") == "好累"
    assert stripper.flush() == ""


async def test_stream_stripper_holds_and_drops_complete_prefix() -> None:
    """标签前缀跨块到达：扣留至完整后丢弃，正文一次性放行。"""
    stripper = StreamTagStripper()
    assert stripper.feed("<心声>想他") == ""
    assert stripper.feed("了。</心声><情绪>") == ""
    assert stripper.feed("joy:0.3</情绪>") == ""
    assert stripper.feed("我在呢。") == "我在呢。"
    assert stripper.feed("怎么了？") == "怎么了？"
    assert stripper.flush() == ""


async def test_stream_stripper_unclosed_tag_flush_returns_remainder() -> None:
    """未闭合标签：流结束 flush 交还标签区之后的残余原文（不吞字）。"""
    stripper = StreamTagStripper()
    assert stripper.feed("<心声>没闭合") == ""
    assert stripper.flush() == "没闭合"


async def test_stream_stripper_unknown_tag_passthrough() -> None:
    """未知标签起始：直接放行（不扣留）。"""
    stripper = StreamTagStripper()
    assert stripper.feed("<div>") == "<div>"
    assert stripper.feed("正文") == "正文"


async def test_stream_stripper_partial_opener_waits_for_more() -> None:
    """截断在开标签中途（``<心``）：继续等待后续块。"""
    stripper = StreamTagStripper()
    assert stripper.feed("<心") == ""
    assert stripper.feed("声>x</心声>正文") == "正文"


async def test_stream_stripper_tags_only_flush_empty() -> None:
    """整条流只有标签：flush 返回空串（不泄漏标签）。"""
    stripper = StreamTagStripper()
    assert stripper.feed("<心声>只有念头</心声>") == ""
    assert stripper.flush() == ""
