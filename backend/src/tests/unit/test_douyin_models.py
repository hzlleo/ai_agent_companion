"""douyin 通道模型测试——会话键语法 / 快照归一矩阵 / 附件与合成语义。

覆盖矩阵：

    - 会话键：构造（空标识按 default 兜底）/ 解析（合法 / 前缀缺失 / 空标识）；
    - 归一：正常文本 / 自身回声剔除 / 系统消息 synthetic / 群聊标记 /
      图片附件（http(s) 保留、data: 剔除、纯图占位文本）/ 缺失时间按采集时刻
      兜底 / 文本与图片皆空跳过 / 无标识跳过 / 批内重复标识保留首次 /
      已处理标识过滤 / 上限截断（保留时间序末尾）。
"""

from __future__ import annotations

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.douyin.douyin_models import (
    DOUYIN_DEFAULT_CONVERSATION_ID,
    DOUYIN_IMAGE_PLACEHOLDER_TEXT,
    DouyinChatSnapshotModel,
    DouyinNormalizeParamsModel,
    build_douyin_chat_id,
    map_douyin_snapshot_to_inbound,
    parse_douyin_chat_id,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.douyin_fakes import make_message_payload, make_snapshot_payload

_FIXED_NOW_MS = 1732000000000


def _normalize(
    snapshot: DouyinChatSnapshotModel,
    *,
    known: frozenset[str] = frozenset(),
    limit: int = 20,
) -> list[InboundMessageModel]:
    """以固定采集时刻归一快照（测试辅助——避免重复构造参数模型）。"""
    return map_douyin_snapshot_to_inbound(
        snapshot,
        params=DouyinNormalizeParamsModel(
            chat_id=build_douyin_chat_id("c-1"),
            known_message_ids=known,
            max_messages=limit,
            now_ms=_FIXED_NOW_MS,
        ),
    )


async def test_chat_id_build_and_parse() -> None:
    """会话键：构造 / 解析往返；空标识按 default 兜底。"""
    assert build_douyin_chat_id("c-1") == "im:c-1"
    assert build_douyin_chat_id("  ") == f"im:{DOUYIN_DEFAULT_CONVERSATION_ID}"
    assert parse_douyin_chat_id("im:c-1") == "c-1"


async def test_chat_id_parse_rejects_bad_grammar() -> None:
    """会话键解析：前缀缺失 / 空标识 → VALIDATION_ERROR（调用方 bug 面）。"""
    with pytest.raises(BusinessException) as missing_prefix:
        parse_douyin_chat_id("c-1")
    assert missing_prefix.value.code is ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as empty_tail:
        parse_douyin_chat_id("im:   ")
    assert empty_tail.value.code is ErrorCode.VALIDATION_ERROR


async def test_normalize_maps_text_message() -> None:
    """归一：文本消息 → 入站模型（通道 / 会话键 / 发送者 / 时间）。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(messages=[make_message_payload(message_id="m-1", text="  在吗  ")])
    )
    inbound = _normalize(snapshot)
    assert len(inbound) == 1
    message = inbound[0]
    assert message.channel is ChannelType.DOUYIN
    assert message.chat_id == "im:c-1"
    assert message.message_id == "m-1"
    assert message.sender_id == "u-1"
    assert message.text == "在吗"
    assert message.ts_ms == 1731000000000
    assert message.is_group_chat is False
    assert message.synthetic is False
    assert message.silent is False


async def test_normalize_skips_self_and_blank_messages() -> None:
    """归一：自身回声 / 无标识 / 文本与图片皆空 → 跳过。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(
            messages=[
                make_message_payload(message_id="m-self", text="我自己的回复", is_self=True),
                make_message_payload(message_id="", text="无标识"),
                make_message_payload(message_id="m-empty", text="   "),
                make_message_payload(message_id="m-ok", text="保留"),
            ]
        )
    )
    inbound = _normalize(snapshot)
    assert [message.message_id for message in inbound] == ["m-ok"]


async def test_normalize_filters_known_ids_and_batch_duplicates() -> None:
    """归一：已处理标识过滤；批内重复标识保留首次出现。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(
            messages=[
                make_message_payload(message_id="m-known", text="已处理"),
                make_message_payload(message_id="m-1", text="第一条"),
                make_message_payload(message_id="m-1", text="重复采集"),
                make_message_payload(message_id="m-2", text="第二条"),
            ]
        )
    )
    inbound = _normalize(snapshot, known=frozenset({"m-known"}))
    assert [(message.message_id, message.text) for message in inbound] == [
        ("m-1", "第一条"),
        ("m-2", "第二条"),
    ]


async def test_normalize_system_and_group_semantics() -> None:
    """归一：系统消息 = synthetic；群会话 = is_group_chat。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(
            messages=[
                make_message_payload(message_id="m-sys", text="对方已开启新会话", is_system=True),
                make_message_payload(message_id="m-group", text="群里见", is_group=True),
            ]
        )
    )
    inbound = _normalize(snapshot)
    assert [message.synthetic for message in inbound] == [True, False]
    assert [message.is_group_chat for message in inbound] == [False, True]


async def test_normalize_images_and_placeholder_text() -> None:
    """归一：图片附件只保留 http(s) 源；纯图片消息归一占位文本。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(
            messages=[
                make_message_payload(
                    message_id="m-img",
                    text="",
                    image_urls=["https://p3.example/img.jpg", "data:image/png;base64,AAA"],
                )
            ]
        )
    )
    inbound = _normalize(snapshot)
    assert len(inbound) == 1
    message = inbound[0]
    assert message.text == DOUYIN_IMAGE_PLACEHOLDER_TEXT
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.kind is ChannelAttachmentKind.IMAGE
    assert attachment.url == "https://p3.example/img.jpg"


async def test_normalize_missing_timestamp_uses_collected_at() -> None:
    """归一：脚本未报告时间 → 按采集时刻兜底（epoch 毫秒）。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(messages=[make_message_payload(message_id="m-1", ts_ms=0)])
    )
    inbound = _normalize(snapshot)
    assert inbound[0].ts_ms == _FIXED_NOW_MS


async def test_normalize_caps_to_newest_messages() -> None:
    """归一：上限截断保留时间序末尾（最新消息优先）。"""
    snapshot = DouyinChatSnapshotModel.model_validate(
        make_snapshot_payload(
            messages=[
                make_message_payload(message_id=f"m-{index}", text=f"第 {index} 条")
                for index in range(5)
            ]
        )
    )
    inbound = _normalize(snapshot, limit=2)
    assert [message.message_id for message in inbound] == ["m-3", "m-4"]


async def test_normalize_rejects_blank_chat_id() -> None:
    """归一参数：空白会话键拒绝（全部落同一会话是接线 bug 面）。"""
    with pytest.raises(ValueError):
        DouyinNormalizeParamsModel(chat_id="   ", now_ms=_FIXED_NOW_MS)


async def test_snapshot_ignores_unknown_wire_fields() -> None:
    """wire 容错：脚本追加字段不影响解析（extra=ignore）。"""
    payload = make_snapshot_payload(messages=[make_message_payload(message_id="m-1")])
    payload["page_title"] = "抖音私信"
    payload["messages"][0]["reactions"] = []
    snapshot = DouyinChatSnapshotModel.model_validate(payload)
    assert snapshot.messages[0].message_id == "m-1"
