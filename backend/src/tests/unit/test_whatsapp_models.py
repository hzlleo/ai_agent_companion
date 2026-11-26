"""WhatsApp 入站归一测试——载荷解析 / 文本回退 / 属主分级 / 去重 / 服务窗。

零真实网络与零真实时钟：时钟为可推进伪时钟；去重窗口逐测试复位。
覆盖：文本 / 媒体 caption / 媒体占位 / 位置 / 按钮 / 交互回复、状态回执
计数、缺字段丢弃、时间戳秒级与非法回落、多 entry 展平、属主命中、服务窗
边界、去重窗口 FIFO、直调路径去重与异常隔离。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.phone_numbers import parse_whatsapp_owner_numbers
from packages.harness.channels.whatsapp.whatsapp_models import (
    WHATSAPP_SERVICE_WINDOW_MS,
    WhatsAppDedupWindow,
    WhatsAppServiceWindow,
    parse_whatsapp_webhook_payload,
    process_whatsapp_webhook_payload,
    reset_whatsapp_state,
    resolve_whatsapp_timestamp_ms,
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
            channel=ChannelType.WHATSAPP,
            chat_id=message.chat_id,
            message_ids=[],
        )


@pytest.fixture(autouse=True)
def _reset_direct_dedup() -> None:
    """每测试复位直调路径的进程内去重窗口。"""
    reset_whatsapp_state()


def _message(
    *,
    wamid: str = "wamid.TEST-1",
    sender: str = "15551234567",
    timestamp: str | int | None = "1731000000",
    message_type: str = "text",
    **overrides: Any,
) -> dict[str, Any]:
    """构造单条 messages[] 条目（测试直供形态）。"""
    body: dict[str, Any] = {
        "from": sender,
        "id": wamid,
        "timestamp": timestamp,
        "type": message_type,
    }
    if message_type == "text":
        body["text"] = {"body": "你好"}
    body.update(overrides)
    return body


def _payload(
    *,
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    phone_number_id: str = "123456",
) -> dict[str, Any]:
    """构造单 entry / 单 change 的 webhook 载荷。"""
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
        "contacts": contacts or [{"wa_id": "15551234567", "profile": {"name": "Alice"}}],
        "messages": messages if messages is not None else [_message()],
    }
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "biz-1", "changes": [{"field": "messages", "value": value}]}],
    }


async def test_text_payload_routes_with_normalized_chat_key() -> None:
    """文本载荷：会话键 / 发送者 / 秒级时间戳归一，默认 peer 落档。"""
    clock = _Clock()
    batch = parse_whatsapp_webhook_payload(
        _payload(), owner_numbers=frozenset(), clock=clock
    )
    assert batch.status_events_ignored == 0
    assert len(batch.messages) == 1
    inbound = batch.messages[0].inbound
    assert inbound.channel is ChannelType.WHATSAPP
    assert inbound.chat_id == "wa:15551234567"
    assert inbound.sender_id == "15551234567"
    assert inbound.message_id == "wamid.TEST-1"
    assert inbound.text == "你好"
    assert inbound.ts_ms == 1_731_000_000_000
    assert inbound.synthetic is False
    assert inbound.is_group_chat is False
    assert inbound.is_owner is False
    assert batch.messages[0].dedup_key == "wamid:wamid.TEST-1"


async def test_owner_numbers_mark_owner_provenance() -> None:
    """属主清单命中（展示形态差异）时 is_owner=True。"""
    batch = parse_whatsapp_webhook_payload(
        _payload(),
        owner_numbers=frozenset({"15551234567"}),
        clock=_Clock(),
    )
    assert batch.messages[0].inbound.is_owner is True


async def test_owner_numbers_normalize_equivalent_forms() -> None:
    """属主清单与发送者两侧共用归一函数（清单边界归一后命中）。"""
    batch = parse_whatsapp_webhook_payload(
        _payload(messages=[_message(sender="15551234567")]),
        owner_numbers=frozenset({"15551234567"}),
        clock=_Clock(),
    )
    assert batch.messages[0].inbound.is_owner is True


async def test_whatsapp_wa_id_preserves_country_code_for_chat_key_and_owner() -> None:
    """中国号码保留国家码：入站键含 86 数字、展示形态属主清单命中 owner。"""
    batch = parse_whatsapp_webhook_payload(
        _payload(messages=[_message(sender="8613800138000")]),
        owner_numbers=parse_whatsapp_owner_numbers("+86 138 0013 8000"),
        clock=_Clock(),
    )
    assert len(batch.messages) == 1
    inbound = batch.messages[0].inbound
    assert inbound.chat_id == "wa:8613800138000"
    assert inbound.sender_id == "8613800138000"
    assert inbound.is_owner is True

    non_china = parse_whatsapp_webhook_payload(
        _payload(messages=[_message(wamid="wamid.US-1", sender="15551234567")]),
        owner_numbers=parse_whatsapp_owner_numbers("15551234567"),
        clock=_Clock(),
    )
    assert non_china.messages[0].inbound.chat_id == "wa:15551234567"
    assert non_china.messages[0].inbound.is_owner is True


@pytest.mark.parametrize(
    ("message_type", "overrides", "expected", "expected_has_user_text"),
    [
        ("text", {}, "你好", True),
        ("image", {"image": {"caption": "看图"}}, "看图", True),
        ("document", {"document": {"caption": "合同", "filename": "a.pdf"}}, "合同", True),
        ("video", {"video": {"caption": "短片"}}, "短片", True),
        ("image", {"image": {}}, "[图片]", False),
        ("audio", {"audio": {"voice": True}}, "[语音]", False),
        ("document", {"document": {}}, "[文件]", False),
        ("sticker", {"sticker": {}}, "[贴纸]", False),
        ("video", {"video": {}}, "[视频]", False),
        ("location", {"location": {"name": "家"}}, "家", True),
        ("location", {"location": {"address": "上海市"}}, "上海市", True),
        ("location", {"location": {}}, "[位置]", True),
        ("button", {"button": {"text": "确认"}}, "确认", True),
        ("button", {"button": {}}, "[按钮]", True),
        (
            "interactive",
            {"interactive": {"type": "button_reply", "button_reply": {"title": "选项甲"}}},
            "选项甲",
            True,
        ),
        (
            "interactive",
            {"interactive": {"type": "list_reply", "list_reply": {"title": "选项乙"}}},
            "选项乙",
            True,
        ),
        ("interactive", {"interactive": {"type": "button_reply"}}, "[交互回复]", True),
    ],
)
async def test_message_type_text_fallback_matrix(
    message_type: str,
    overrides: dict[str, Any],
    expected: str,
    expected_has_user_text: bool,
) -> None:
    """媒体 / 位置 / 按钮 / 交互回复的文本回退与 has_user_text 映射。

    纯媒体占位（无 caption）映射 ``has_user_text=False``；caption / 文本 /
    结构化文本回退映射 True——路由器按该字段跳过纯媒体生成。
    """
    message = _message(message_type=message_type, **overrides)
    batch = parse_whatsapp_webhook_payload(
        _payload(messages=[message]), owner_numbers=frozenset(), clock=_Clock()
    )
    assert len(batch.messages) == 1
    assert batch.messages[0].inbound.text == expected
    assert batch.messages[0].inbound.has_user_text is expected_has_user_text


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (_message(wamid=""), "缺 wamid"),
        (_message(sender=""), "缺发送者"),
        (_message(message_type="text", text={"body": "   "}), "空正文"),
        (_message(message_type="reaction", emoji="👍"), "不可路由类型"),
    ],
)
async def test_unroutable_messages_dropped(message: dict[str, Any], reason: str) -> None:
    """缺 wamid / 缺发送者 / 空正文 / 不可路由类型：丢弃且不登记。"""
    batch = parse_whatsapp_webhook_payload(
        _payload(messages=[message]), owner_numbers=frozenset(), clock=_Clock()
    )
    assert batch.messages == [], reason


async def test_statuses_ignored_and_counted() -> None:
    """statuses[]：不进路由，只累计 status_events_ignored。"""
    batch = parse_whatsapp_webhook_payload(
        _payload(
            messages=[],
            statuses=[
                {"id": "wamid.A", "status": "delivered", "recipient_id": "15551234567"},
                {"id": "wamid.B", "status": "read", "recipient_id": "15551234567"},
            ],
        ),
        owner_numbers=frozenset(),
        clock=_Clock(),
    )
    assert batch.messages == []
    assert batch.status_events_ignored == 2


async def test_multiple_entries_and_messages_flattened() -> None:
    """多 entry / 多 change / 多 messages 全量展平。"""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "biz-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                _message(wamid="wamid.A", sender="15551234567"),
                                _message(wamid="wamid.B", sender="15557654321"),
                            ],
                            "statuses": [{"id": "wamid.C", "status": "sent"}],
                        },
                    }
                ],
            },
            {
                "id": "biz-2",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [_message(wamid="wamid.D", sender="15550001111")]
                        },
                    }
                ],
            },
        ],
    }
    batch = parse_whatsapp_webhook_payload(
        payload, owner_numbers=frozenset(), clock=_Clock()
    )
    assert [item.inbound.message_id for item in batch.messages] == [
        "wamid.A",
        "wamid.B",
        "wamid.D",
    ]
    assert batch.status_events_ignored == 1


async def test_malformed_payload_returns_empty_batch() -> None:
    """非法载荷（entry 非列表）：隔离为空批次（不抛）。"""
    batch = parse_whatsapp_webhook_payload(
        {"entry": "not-a-list"}, owner_numbers=frozenset(), clock=_Clock()
    )
    assert batch.messages == []
    assert batch.status_events_ignored == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1731000000", 1_731_000_000_000),
        (1_731_000_000, 1_731_000_000_000),
        (1_731_000_000_000, 1_731_000_000_000),
        (" 1731000000 ", 1_731_000_000_000),
        (None, 1_700_000_000_000),
        ("", 1_700_000_000_000),
        ("abc", 1_700_000_000_000),
        (0, 1_700_000_000_000),
        (-5, 1_700_000_000_000),
        (True, 1_700_000_000_000),
        (math.nan, 1_700_000_000_000),
        (math.inf, 1_700_000_000_000),
        ("inf", 1_700_000_000_000),
    ],
)
async def test_timestamp_resolution_matrix(raw: Any, expected: int) -> None:
    """时间戳矩阵：秒级 ×1000 / 毫秒原样 / 非法与非有限值回落服务端时钟。"""
    assert resolve_whatsapp_timestamp_ms(raw, fallback_ms=1_700_000_000_000) == expected


async def test_missing_timestamp_falls_back_to_server_clock() -> None:
    """载荷时间戳缺失：入站 ts_ms 回落注入的服务端时钟。"""
    clock = _Clock(now_ms=1_700_000_123_000)
    batch = parse_whatsapp_webhook_payload(
        _payload(messages=[_message(timestamp=None)]),
        owner_numbers=frozenset(),
        clock=clock,
    )
    assert batch.messages[0].inbound.ts_ms == 1_700_000_123_000


async def test_service_window_boundaries() -> None:
    """服务窗：未见过入站拒绝；记录后 24h 内（含边界）放行；超窗拒绝。"""
    window = WhatsAppServiceWindow()
    assert window.is_within_window("15551234567", 1_700_000_000_000) is False
    window.record_inbound("15551234567", 1_700_000_000_000)
    assert window.last_inbound_ms("15551234567") == 1_700_000_000_000
    assert window.is_within_window(
        "15551234567", 1_700_000_000_000 + WHATSAPP_SERVICE_WINDOW_MS
    ) is True
    assert window.is_within_window(
        "15551234567", 1_700_000_000_000 + WHATSAPP_SERVICE_WINDOW_MS + 1
    ) is False
    assert window.is_within_window("19990000000", 1_700_000_000_000) is False


async def test_service_window_ignores_older_timestamps() -> None:
    """乱序到达不回退窗口（取较新的入站时刻）。"""
    window = WhatsAppServiceWindow()
    window.record_inbound("15551234567", 1_700_000_500_000)
    window.record_inbound("15551234567", 1_700_000_000_000)
    assert window.last_inbound_ms("15551234567") == 1_700_000_500_000


async def test_dedup_window_fifo_capacity() -> None:
    """去重窗口 FIFO：容量超限淘汰最旧键。"""
    window = WhatsAppDedupWindow(cap=2)
    assert window.remember("wamid:A") is True
    assert window.remember("wamid:B") is True
    assert window.remember("wamid:A") is False
    assert window.remember("wamid:C") is True
    assert window.contains("wamid:A") is False
    assert window.contains("wamid:B") is True
    assert window.contains("wamid:C") is True


async def test_dedup_window_rejects_zero_capacity() -> None:
    """零容量窗口拒绝（去重会静默失效）。"""
    with pytest.raises(ValueError, match="cap 须 ≥ 1"):
        WhatsAppDedupWindow(cap=0)


async def test_direct_process_path_dedups_and_isolates_errors() -> None:
    """直调路径：重复 wamid 只路由一次；路由异常被隔离（恒不抛）。"""
    router = RecordingRouter(error=RuntimeError("路由失败（测试注入）"))
    await process_whatsapp_webhook_payload(
        _payload(), router, owner_numbers=frozenset(), clock=_Clock()
    )
    await process_whatsapp_webhook_payload(
        _payload(), router, owner_numbers=frozenset(), clock=_Clock()
    )
    assert len(router.received) == 1
    assert router.received[0].chat_id == "wa:15551234567"
