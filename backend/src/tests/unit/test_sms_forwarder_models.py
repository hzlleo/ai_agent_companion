"""SmsForwarder 入站归一测试——载荷解析 / 发送者归一 / 属主分级 / 去重窗口。

零真实网络与零真实时钟：时钟为可推进伪时钟；去重窗口逐测试复位。
覆盖：短信默认接收与通知开关、字段别名与缺省、正文优先级、空正文丢弃、
时间戳秒 / 毫秒 / 非法回落、发送者归一与属主命中、会话键形状、
msgid / 内容去重键、窗口 TTL 与 FIFO 淘汰、直调路径去重与异常隔离。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.sms_forwarder.sms_models import (
    SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS,
    SmsForwarderDedupWindow,
    SmsForwarderKind,
    build_sms_forwarder_dedup_key,
    normalize_sms_forwarder_kind,
    normalize_sms_sender,
    parse_owner_numbers,
    parse_sms_forwarder_payload,
    process_sms_forwarder_payload,
    reset_sms_forwarder_state,
    resolve_sms_forwarder_timestamp_ms,
)


class _Clock:
    """可推进的测试时钟（epoch 毫秒）。"""

    def __init__(self, now_ms: int = 1_731_000_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


class RecordingRouter:
    """fake 路由器（记录入站消息；可选编程抛错）。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.received: list[InboundMessageModel] = []
        self._error = error

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        await asyncio.sleep(0)
        self.received.append(message)
        if self._error is not None:
            raise self._error
        return ChannelDeliveryResultModel(
            channel=ChannelType.SMS_FORWARDER,
            chat_id=message.chat_id,
            message_ids=[],
        )


@pytest.fixture(autouse=True)
def _reset_direct_dedup() -> None:
    """每测试复位直调路径的进程内去重窗口。"""
    reset_sms_forwarder_state()


def _payload(**overrides: Any) -> dict[str, Any]:
    """构造转发载荷（测试直供形态）。"""
    payload: dict[str, Any] = {
        "from": "+86 138-0013-8000",
        "content": "验证码 123456",
        "timestamp": 1_731_000_000_000,
        "kind": "SMS",
        "device": "phone-a",
        "sim": "sim-1",
    }
    payload.update(overrides)
    return payload


async def test_sms_payload_routes_with_normalized_chat_key() -> None:
    """短信载荷：发送者 / 会话键归一、`synthetic=False`、默认 peer 落档。"""
    clock = _Clock()
    parsed = parse_sms_forwarder_payload(
        _payload(),
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=clock,
    )
    assert parsed is not None
    inbound = parsed.inbound
    assert inbound.channel is ChannelType.SMS_FORWARDER
    assert inbound.chat_id == "sms:13800138000"
    assert inbound.sender_id == "13800138000"
    assert inbound.text == "验证码 123456"
    assert inbound.ts_ms == 1_731_000_000_000
    assert inbound.synthetic is False
    assert inbound.is_group_chat is False
    assert inbound.is_owner is False
    assert parsed.dedup_key.startswith("content:")
    assert parsed.dedup_ttl_ms == SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS


async def test_msgid_and_seconds_timestamp_are_normalized() -> None:
    """msgid 别名生效（常驻去重键）；秒级时间戳 ×1000 归一。"""
    parsed = parse_sms_forwarder_payload(
        _payload(
            msgid="abc-123",
            timestamp="1731000000",
            content=None,
            message="新短信",
        ),
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=_Clock(),
    )
    assert parsed is not None
    assert parsed.inbound.message_id == "abc-123"
    assert parsed.inbound.text == "新短信"
    assert parsed.inbound.ts_ms == 1_731_000_000_000
    assert parsed.dedup_key == "msgid:abc-123"
    assert parsed.dedup_ttl_ms is None


async def test_message_id_alias_variants_are_accepted() -> None:
    """message_id / messageId 别名与 msgid 同义。"""
    for key in ("message_id", "messageId"):
        parsed = parse_sms_forwarder_payload(
            _payload(**{key: f"id-{key}"}),
            notifications_enabled=False,
            owner_numbers=frozenset(),
            clock=_Clock(),
        )
        assert parsed is not None
        assert parsed.inbound.message_id == f"id-{key}"


async def test_notification_gated_by_flag() -> None:
    """通知默认忽略（不登记去重）；开关开启后正常路由。"""
    payload = _payload(kind="notification", content="应用通知", msgid="n-1")
    clock = _Clock()
    assert (
        parse_sms_forwarder_payload(
            payload,
            notifications_enabled=False,
            owner_numbers=frozenset(),
            clock=clock,
        )
        is None
    )
    parsed = parse_sms_forwarder_payload(
        payload,
        notifications_enabled=True,
        owner_numbers=frozenset(),
        clock=clock,
    )
    assert parsed is not None
    assert parsed.inbound.text == "应用通知"


async def test_kind_alias_and_unknown_kind() -> None:
    """中文 / 大小写别名归一；未知类型不可路由。"""
    assert normalize_sms_forwarder_kind("短信") is SmsForwarderKind.SMS
    assert normalize_sms_forwarder_kind("Notification") is SmsForwarderKind.NOTIFICATION
    assert normalize_sms_forwarder_kind("email") is None
    assert (
        parse_sms_forwarder_payload(
            _payload(kind="email"),
            notifications_enabled=True,
            owner_numbers=frozenset(),
            clock=_Clock(),
        )
        is None
    )


async def test_missing_fields_have_defaults() -> None:
    """缺失字段给默认：发送者 unknown、kind 按短信、时间戳回落服务端时钟。"""
    clock = _Clock(now_ms=1_731_111_111_111)
    parsed = parse_sms_forwarder_payload(
        {"body": "只有正文"},
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=clock,
    )
    assert parsed is not None
    assert parsed.inbound.chat_id == "sms:unknown"
    assert parsed.inbound.sender_id == "unknown"
    assert parsed.inbound.ts_ms == clock.now_ms
    assert parsed.inbound.text == "只有正文"


async def test_extra_fields_ignored_and_sender_alias() -> None:
    """多余字段忽略；sender / contact 兜底来源均可用。"""
    parsed = parse_sms_forwarder_payload(
        {"sender": "13800138000", "body": "hello", "unknown": {"nested": [1, 2]}},
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=_Clock(),
    )
    assert parsed is not None
    assert parsed.inbound.chat_id == "sms:13800138000"
    parsed_contact = parse_sms_forwarder_payload(
        {"contact": "妈妈", "body": "回家吃饭"},
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=_Clock(),
    )
    assert parsed_contact is not None
    assert parsed_contact.inbound.chat_id == "sms:妈妈"


async def test_body_priority_and_empty_body_dropped() -> None:
    """正文优先级 body > content > message > text；全空丢弃。"""
    parsed = parse_sms_forwarder_payload(
        {"from": "1", "body": "第一", "content": "第二", "message": "第三"},
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=_Clock(),
    )
    assert parsed is not None
    assert parsed.inbound.text == "第一"
    for payload in (
        {"from": "1", "content": "   "},
        {"from": "1"},
        {"from": None, "body": None},
    ):
        assert (
            parse_sms_forwarder_payload(
                payload,
                notifications_enabled=False,
                owner_numbers=frozenset(),
                clock=_Clock(),
            )
            is None
        )


@pytest.mark.parametrize("timestamp", ["not-a-date", -5, 0, 42, None, True])
async def test_invalid_timestamp_falls_back_to_server_clock(timestamp: object) -> None:
    """非法 / 缺失 / 微妙级以下的时间戳回落服务端时钟。"""
    clock = _Clock(now_ms=1_731_222_222_222)
    parsed = parse_sms_forwarder_payload(
        _payload(timestamp=timestamp),
        notifications_enabled=False,
        owner_numbers=frozenset(),
        clock=clock,
    )
    assert parsed is not None
    assert parsed.inbound.ts_ms == clock.now_ms


async def test_timestamp_resolution_matrix() -> None:
    """时间戳解析：毫秒原样 / 秒 ×1000 / 非法回落。"""
    fallback = 1_731_333_333_333
    assert resolve_sms_forwarder_timestamp_ms(1_731_000_000_000, fallback_ms=fallback) == (
        1_731_000_000_000
    )
    assert resolve_sms_forwarder_timestamp_ms("1731000000", fallback_ms=fallback) == (
        1_731_000_000_000
    )
    assert resolve_sms_forwarder_timestamp_ms("1731000000.9", fallback_ms=fallback) == (
        1_731_000_000_000
    )
    assert resolve_sms_forwarder_timestamp_ms("bad", fallback_ms=fallback) == fallback
    assert resolve_sms_forwarder_timestamp_ms(None, fallback_ms=fallback) == fallback


async def test_owner_numbers_match_phone_and_contact_name() -> None:
    """属主清单命中（国际码归一 / 联系名）时才按 owner 落档。"""
    owners = parse_owner_numbers("+86 138-0013-8000, 妈妈 ,")
    assert owners == frozenset({"13800138000", "妈妈"})
    phone_parsed = parse_sms_forwarder_payload(
        _payload(),
        notifications_enabled=False,
        owner_numbers=owners,
        clock=_Clock(),
    )
    assert phone_parsed is not None
    assert phone_parsed.inbound.is_owner is True
    contact_parsed = parse_sms_forwarder_payload(
        {"contact": "妈妈", "body": "到家了"},
        notifications_enabled=False,
        owner_numbers=owners,
        clock=_Clock(),
    )
    assert contact_parsed is not None
    assert contact_parsed.inbound.is_owner is True
    peer_parsed = parse_sms_forwarder_payload(
        {"contact": "同事", "body": "开会"},
        notifications_enabled=False,
        owner_numbers=owners,
        clock=_Clock(),
    )
    assert peer_parsed is not None
    assert peer_parsed.inbound.is_owner is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+86 138-0013-8000", "13800138000"),
        ("0086 13800138000", "13800138000"),
        ("86 13800138000", "13800138000"),
        ("13800138000", "13800138000"),
        ("  妈妈  ", "妈妈"),
        ("", ""),
    ],
)
async def test_normalize_sms_sender_matrix(raw: str, expected: str) -> None:
    """发送者归一：号码去分隔符 / 国家码；联系名小写。"""
    assert normalize_sms_sender(raw) == expected


async def test_dedup_key_shapes() -> None:
    """msgid 键与内容键形态（内容键含发送者 + 正文哈希）。"""
    assert build_sms_forwarder_dedup_key(msgid="m1", sender_key="1", text="x") == "msgid:m1"
    content_key = build_sms_forwarder_dedup_key(msgid="", sender_key="1", text="x")
    assert content_key.startswith("content:")
    assert content_key == build_sms_forwarder_dedup_key(msgid="", sender_key="1", text="x")


async def test_dedup_window_content_ttl_expiry() -> None:
    """内容键在 TTL 内重复吸收，过期后允许重新登记。"""
    window = SmsForwarderDedupWindow(cap=8)
    clock = _Clock()
    assert window.remember("content:a", clock.now_ms, 1000) is True
    assert window.contains("content:a", clock.now_ms) is True
    assert window.remember("content:a", clock.now_ms, 1000) is False
    clock.advance_ms(1000)
    assert window.contains("content:a", clock.now_ms) is False
    assert window.remember("content:a", clock.now_ms, 1000) is True


async def test_dedup_window_msgid_fifo_eviction() -> None:
    """msgid 常驻键按容量 FIFO 淘汰；淘汰后可重新登记。"""
    window = SmsForwarderDedupWindow(cap=2)
    clock = _Clock()
    assert window.remember("msgid:1", clock.now_ms) is True
    assert window.remember("msgid:2", clock.now_ms) is True
    assert window.remember("msgid:3", clock.now_ms) is True
    assert window.contains("msgid:1", clock.now_ms) is False
    assert window.contains("msgid:2", clock.now_ms) is True
    assert window.remember("msgid:1", clock.now_ms) is True
    assert len(window) == 2


async def test_dedup_window_cap_below_one_rejected() -> None:
    """零容量窗口拒绝构造（去重静默失效面）。"""
    with pytest.raises(ValueError):
        SmsForwarderDedupWindow(cap=0)


async def test_direct_path_routes_once_and_dedups() -> None:
    """直调路径：首次路由，重投被去重吸收。"""
    router = RecordingRouter()
    payload = _payload(msgid="direct-1")
    await process_sms_forwarder_payload(
        payload, router, notifications_enabled=False, owner_numbers=frozenset()
    )
    await process_sms_forwarder_payload(
        payload, router, notifications_enabled=False, owner_numbers=frozenset()
    )
    assert len(router.received) == 1


async def test_direct_path_unroutable_does_not_consume_dedup() -> None:
    """通知关闭 / 空正文不登记去重——随后同 msgid 的可路由消息照常路由。"""
    router = RecordingRouter()
    payload = _payload(kind="notification", msgid="same-id")
    await process_sms_forwarder_payload(
        payload, router, notifications_enabled=False, owner_numbers=frozenset()
    )
    assert router.received == []
    routable = _payload(kind="SMS", msgid="same-id")
    await process_sms_forwarder_payload(
        routable, router, notifications_enabled=True, owner_numbers=frozenset()
    )
    assert len(router.received) == 1


async def test_direct_path_router_exception_isolated() -> None:
    """路由器抛错被兜底捕获（恒不抛——HTTP 层确认语义）。"""
    router = RecordingRouter(error=RuntimeError("boom"))
    await process_sms_forwarder_payload(
        _payload(msgid="err-1"),
        router,
        notifications_enabled=False,
        owner_numbers=frozenset(),
    )
    assert len(router.received) == 1
