"""GUI 微信会话水位测试——尾部重叠 / 末次出现 / 视图重置 / 保留长度。

纯逻辑用例：零网络 / 零时钟 / 零配置；消息模型经显式字段构造。
"""

from __future__ import annotations

import pytest

from packages.harness.channels.wechat_gui.wechat_gui_models import WechatGuiMessageModel
from packages.harness.channels.wechat_gui.wechat_gui_watermark import (
    WechatGuiWatermark,
    WechatGuiWatermarkAdvance,
)
from tests.wechat_gui_fakes import make_message


def _texts(messages: tuple[WechatGuiMessageModel, ...]) -> list[str]:
    """提取消息正文（断言面）。"""
    return [message.text for message in messages]


async def test_first_frame_baseline_only() -> None:
    """首帧（无 prev）：只建基线，零投递。"""
    watermark = WechatGuiWatermark(retention=10)
    advance = watermark.diff("小林", [make_message(text="你好")])
    assert advance.new_messages == ()
    watermark.commit(advance)
    assert watermark.tracked_targets == ("小林",)


async def test_append_one_and_three_messages() -> None:
    """追加 1 / 3 条：各投递对应数量。"""
    watermark = WechatGuiWatermark(retention=10)
    base = [make_message(text="消息0")]
    watermark.commit(watermark.diff("小林", base))
    one = watermark.diff(
        "小林", [*base, make_message(text="消息1")]
    )
    assert _texts(one.new_messages) == ["消息1"]
    watermark.commit(one)
    three = watermark.diff(
        "小林",
        [
            *base,
            make_message(text="消息1"),
            make_message(text="消息2"),
            make_message(text="消息3"),
        ],
    )
    assert _texts(three.new_messages) == ["消息2", "消息3"]


async def test_duplicate_text_tail_delivers_one() -> None:
    """重复文本尾随：prev=[X,好]、cur=[X,好,好] 只投递 1 条新增。"""
    watermark = WechatGuiWatermark(retention=10)
    prev = [make_message(text="X"), make_message(text="好")]
    watermark.commit(watermark.diff("小林", prev))
    advance = watermark.diff(
        "小林", [*prev, make_message(text="好")]
    )
    assert _texts(advance.new_messages) == ["好"]


async def test_scroll_up_history_delivers_nothing() -> None:
    """向上滚动看历史：prev=[A,B]、cur=[B,A,B] 零投递。"""
    watermark = WechatGuiWatermark(retention=10)
    a = make_message(text="A")
    b = make_message(text="B")
    watermark.commit(watermark.diff("小林", [a, b]))
    advance = watermark.diff("小林", [b, a, b])
    assert advance.new_messages == ()


async def test_last_occurrence_prevents_false_new_messages() -> None:
    """末次出现定位：prev=[A]、cur=[A,B,A] 末次 A 之后为空——零投递。"""
    watermark = WechatGuiWatermark(retention=10)
    watermark.commit(watermark.diff("小林", [make_message(text="A")]))
    advance = watermark.diff(
        "小林",
        [make_message(text="A"), make_message(text="B"), make_message(text="A")],
    )
    assert advance.new_messages == ()


async def test_identical_fingerprint_tail_is_swallowed() -> None:
    """同指纹尾随代价：prev=[好]、cur=[好,好] 零投递（末次已落在屏上）。"""
    watermark = WechatGuiWatermark(retention=10)
    watermark.commit(watermark.diff("小林", [make_message(text="好")]))
    advance = watermark.diff(
        "小林", [make_message(text="好"), make_message(text="好")]
    )
    assert advance.new_messages == ()


async def test_target_first_frame_delivers_nothing() -> None:
    """目标切换后的首帧：新目标无基线——只建基线、零投递。"""
    watermark = WechatGuiWatermark(retention=10)
    message = make_message(text="阿青消息", sender="阿青")
    watermark.commit(watermark.diff("小林", [message]))
    advance = watermark.diff("阿青", [message])
    assert advance.new_messages == ()
    watermark.commit(advance)
    assert set(watermark.tracked_targets) == {"小林", "阿青"}


async def test_new_messages_beyond_retention_resets() -> None:
    """新增条数超过保留长度：视为视图重置（零投递）并重建基线。"""
    watermark = WechatGuiWatermark(retention=2)
    head = make_message(text="A")
    watermark.commit(watermark.diff("小林", [head]))
    overflow = watermark.diff(
        "小林",
        [
            head,
            make_message(text="B"),
            make_message(text="C"),
            make_message(text="D"),
        ],
    )
    assert overflow.new_messages == ()
    watermark.commit(overflow)
    follow_up = watermark.diff(
        "小林",
        [
            make_message(text="C"),
            make_message(text="D"),
            make_message(text="E"),
        ],
    )
    assert _texts(follow_up.new_messages) == ["E"]


async def test_unmatched_view_resets_baseline() -> None:
    """无任何 k 匹配（滚动超出可见范围）：零投递并重建基线。"""
    watermark = WechatGuiWatermark(retention=10)
    watermark.commit(watermark.diff("小林", [make_message(text="A")]))
    advance = watermark.diff("小林", [make_message(text="Z")])
    assert advance.new_messages == ()
    watermark.commit(advance)
    after = watermark.diff(
        "小林", [make_message(text="Z"), make_message(text="Y")]
    )
    assert _texts(after.new_messages) == ["Y"]


async def test_self_echo_participates_in_alignment() -> None:
    """is_self 参与对齐：自身回声之后的新消息仍能定位。"""
    watermark = WechatGuiWatermark(retention=10)
    peer = make_message(text="A")
    echo = make_message(text="我发的", sender="我", is_self=True)
    watermark.commit(watermark.diff("小林", [peer, echo]))
    advance = watermark.diff(
        "小林",
        [peer, echo, make_message(text="B")],
    )
    assert _texts(advance.new_messages) == ["B"]


async def test_ts_ms_does_not_affect_alignment() -> None:
    """ts_ms 仅作观测：同一可见文本的时间漂移仍对齐为零新增。"""
    watermark = WechatGuiWatermark(retention=10)
    watermark.commit(
        watermark.diff("小林", [make_message(text="你好", ts_ms=1_000)])
    )
    advance = watermark.diff(
        "小林", [make_message(text="你好", ts_ms=9_999_999)]
    )
    assert advance.new_messages == ()


async def test_reset_and_commit_are_explicit() -> None:
    """reset 丢弃基线；commit 落基线（diff 本身不修改状态）。"""
    watermark = WechatGuiWatermark(retention=10)
    pending = watermark.diff("小林", [make_message(text="你好")])
    assert isinstance(pending, WechatGuiWatermarkAdvance)
    assert watermark.tracked_targets == ()
    watermark.commit(pending)
    assert watermark.tracked_targets == ("小林",)
    assert watermark.retention == 10
    watermark.reset("小林")
    assert watermark.tracked_targets == ()


async def test_retention_must_be_positive() -> None:
    """保留长度不变式：小于 1 拒绝。"""
    with pytest.raises(ValueError):
        WechatGuiWatermark(retention=0)
