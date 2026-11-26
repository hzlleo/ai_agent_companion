"""GUI 微信通道 Model Classes 测试——会话键 / 设置 / 去重键 / 归一映射。

零真实配置：全局单例读取根 conftest 硬覆盖值；隔离实例经显式字段构造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WECHAT_GUI_DEDUP_WINDOW_MS,
    WechatGuiContactModel,
    WechatGuiMessageModel,
    WechatGuiSettings,
    WechatGuiSnapshotModel,
    build_wechat_gui_chat_id,
    map_wechat_gui_snapshot_to_inbound,
    parse_wechat_gui_chat_id,
    wechat_gui_message_dedup_key,
)
from packages.harness.config.models import ChannelsSettings, Settings, settings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode

#: WechatGuiSettings 全量标量 env 面键（默认值测试清理面）。
_WECHAT_GUI_ENV_KEYS = (
    "WECHAT_GUI_ENABLED",
    "WECHAT_GUI_SANDBOX_URL",
    "WECHAT_GUI_TOKEN",
    "WECHAT_GUI_POLL_INTERVAL_SECONDS",
    "WECHAT_GUI_REQUEST_TIMEOUT_SECONDS",
    "WECHAT_GUI_BACKOFF_BASE_SECONDS",
    "WECHAT_GUI_BACKOFF_MAX_SECONDS",
    "WECHAT_GUI_MAX_MESSAGES_PER_POLL",
    "WECHAT_GUI_CONTACTS_JSON",
    "WECHAT_GUI_VLM_ENTRY",
    "WECHAT_GUI_TITLE_MISMATCH_ACTION",
)


async def test_settings_section_maps_env_and_defaults_off() -> None:
    """channels.wechat_gui 分节与 conftest 硬覆盖对齐（默认关闭）。"""
    gui = settings.channels.wechat_gui
    assert gui.enabled is False
    assert gui.sandbox_url == "http://127.0.0.1:8090"
    assert gui.token.get_secret_value() == ""
    assert gui.poll_interval_seconds == 5.0
    assert gui.request_timeout_seconds == 15.0
    assert gui.backoff_base_seconds == 2.0
    assert gui.backoff_max_seconds == 60.0
    assert gui.max_messages_per_poll == 20
    assert gui.contacts == {}
    assert gui.vlm_entry == ""
    assert gui.title_mismatch_action.value == "skip"


async def test_settings_code_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 面全清时的代码默认值。"""
    for key in _WECHAT_GUI_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = WechatGuiSettings()
    assert default.enabled is False
    assert default.sandbox_url == "http://127.0.0.1:8090"
    assert default.max_messages_per_poll == 20
    assert default.contacts == {}


async def test_settings_env_override_and_contacts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """WECHAT_GUI_* env 覆写 + 联系人 JSON 解析为映射。"""
    monkeypatch.setenv("WECHAT_GUI_ENABLED", "true")
    monkeypatch.setenv("WECHAT_GUI_SANDBOX_URL", "  http://127.0.0.1:8091/  ")
    monkeypatch.setenv("WECHAT_GUI_TOKEN", "secret-token")
    monkeypatch.setenv("WECHAT_GUI_POLL_INTERVAL_SECONDS", "8")
    monkeypatch.setenv("WECHAT_GUI_MAX_MESSAGES_PER_POLL", "50")
    monkeypatch.setenv("WECHAT_GUI_VLM_ENTRY", "  vision-local ")
    monkeypatch.setenv("WECHAT_GUI_TITLE_MISMATCH_ACTION", "fail")
    monkeypatch.setenv(
        "WECHAT_GUI_CONTACTS_JSON",
        '[{"remark_name": "小林", "persona_id": "p-1", "is_owner": true}]',
    )
    gui = WechatGuiSettings()
    assert gui.enabled is True
    assert gui.sandbox_url == "http://127.0.0.1:8091"
    assert gui.token.get_secret_value() == "secret-token"
    assert "secret-token" not in str(gui.token)
    assert gui.poll_interval_seconds == 8.0
    assert gui.max_messages_per_poll == 50
    assert gui.vlm_entry == "vision-local"
    assert gui.title_mismatch_action.value == "fail"
    assert list(gui.contacts) == ["小林"]
    assert gui.contacts["小林"].is_owner is True


async def test_settings_reject_invalid_contacts_and_bounds() -> None:
    """配置面 fail fast：URL / 退避 / 联系人 JSON / 重复联系人。"""
    with pytest.raises(ValidationError):
        WechatGuiSettings(sandbox_url="127.0.0.1:8090")
    with pytest.raises(ValidationError):
        WechatGuiSettings(backoff_base_seconds=10.0, backoff_max_seconds=5.0)
    with pytest.raises(ValidationError):
        WechatGuiSettings(contacts_json="not-json")
    with pytest.raises(ValidationError):
        WechatGuiSettings(contacts_json='{"remark_name": "x"}')
    with pytest.raises(ValidationError):
        WechatGuiSettings(
            contacts_json='[{"remark_name": "x"}, {"remark_name": "x"}]'
        )
    with pytest.raises(ValidationError):
        WechatGuiSettings(contacts_json='[{"remark_name": "   "}]')
    with pytest.raises(ValidationError):
        WechatGuiSettings(poll_interval_seconds=0)
    with pytest.raises(ValidationError):
        WechatGuiSettings(max_messages_per_poll=101)


async def test_settings_isolated_construction() -> None:
    """隔离构造：显式字段进聚合容器，其余通道保持缺省关闭。"""
    channels = ChannelsSettings(
        wechat_gui=WechatGuiSettings(
            enabled=True,
            token="t",
            contacts_json='[{"remark_name": "小林"}]',
        )
    )
    isolated = build_isolated_settings(channels=channels)
    assert isolated.channels.wechat_gui.enabled is True
    assert isolated.channels.wechat_gui.token.get_secret_value() == "t"
    assert "小林" in isolated.channels.wechat_gui.contacts
    assert isolated.channels.telegram.enabled is False
    assert isolated.channels.douyin.enabled is False
    assert isinstance(isolated, Settings)


async def test_chat_id_grammar_round_trip_and_rejection() -> None:
    """会话键：构造 / 解析往返；非法前缀与空备注名拒绝。"""
    chat_id = build_wechat_gui_chat_id(" 小林 ")
    assert chat_id == "wechat_gui:小林"
    assert parse_wechat_gui_chat_id(chat_id) == "小林"
    with pytest.raises(ValueError):
        build_wechat_gui_chat_id("   ")
    with pytest.raises(BusinessException) as missing_prefix:
        parse_wechat_gui_chat_id("im:小林")
    assert missing_prefix.value.code is ErrorCode.VALIDATION_ERROR
    with pytest.raises(BusinessException) as empty_suffix:
        parse_wechat_gui_chat_id("wechat_gui:   ")
    assert empty_suffix.value.code is ErrorCode.VALIDATION_ERROR


async def test_dedup_key_uses_text_and_time_window() -> None:
    """去重键：同文本同窗口一致；跨窗口 / 跨文本不同。"""
    message = WechatGuiMessageModel(text="你好", ts_ms=1_000_000)
    key_a = wechat_gui_message_dedup_key("小林", message, captured_at_ms=1_000_000)
    key_b = wechat_gui_message_dedup_key(
        "小林", message.model_copy(), captured_at_ms=1_200_000
    )
    assert key_a == key_b
    key_later = wechat_gui_message_dedup_key(
        "小林",
        message.model_copy(update={"ts_ms": 1_000_000 + WECHAT_GUI_DEDUP_WINDOW_MS}),
        captured_at_ms=1_000_000,
    )
    assert key_later != key_a
    key_other_text = wechat_gui_message_dedup_key(
        "小林", message.model_copy(update={"text": "再见"}), captured_at_ms=1_000_000
    )
    assert key_other_text != key_a
    assert len(key_a) == 64


async def test_map_snapshot_filters_and_maps_fields() -> None:
    """归一映射：自身回声 / 空文本 / 已处理键过滤；来源与通道字段正确。"""
    snapshot = WechatGuiSnapshotModel(
        title="小林",
        messages=[
            WechatGuiMessageModel(sender="我", text="自己发的", is_self=True),
            WechatGuiMessageModel(sender="小林", text=""),
            WechatGuiMessageModel(sender="小林", text="第一条", ts_ms=1_000_000),
            WechatGuiMessageModel(sender="小林", text="第二条", ts_ms=1_100_000),
            WechatGuiMessageModel(sender="小林", text="第二条", ts_ms=1_100_500),
        ],
        screenshot_sha256="sha-1",
        captured_at_ms=1_200_000,
    )
    contact = WechatGuiContactModel(remark_name="小林", is_owner=True)
    inbound = map_wechat_gui_snapshot_to_inbound(
        snapshot,
        contact=contact,
        known_message_ids=frozenset(),
        max_messages=20,
        now_ms=1_200_000,
    )
    assert [message.text for message in inbound] == ["第一条", "第二条"]
    first = inbound[0]
    assert first.channel is ChannelType.WECHAT_GUI
    assert first.chat_id == "wechat_gui:小林"
    assert first.is_owner is True
    assert first.has_user_text is True
    assert first.is_group_chat is False
    assert first.synthetic is False
    assert first.sender_id == "小林"
    assert first.message_id == wechat_gui_message_dedup_key(
        "小林", snapshot.messages[2], captured_at_ms=1_200_000
    )


async def test_map_snapshot_skips_known_and_bounds_tail() -> None:
    """归一映射：已处理键跳过；上限只保留时间序末尾。"""
    messages = [
        WechatGuiMessageModel(sender="小林", text=f"消息{i}", ts_ms=1_000_000 + i)
        for i in range(5)
    ]
    snapshot = WechatGuiSnapshotModel(
        title="小林",
        messages=messages,
        screenshot_sha256="sha",
        captured_at_ms=2_000_000,
    )
    contact = WechatGuiContactModel(remark_name="小林")
    known = frozenset(
        {
            wechat_gui_message_dedup_key("小林", messages[0], captured_at_ms=2_000_000),
            wechat_gui_message_dedup_key("小林", messages[1], captured_at_ms=2_000_000),
        }
    )
    inbound = map_wechat_gui_snapshot_to_inbound(
        snapshot,
        contact=contact,
        known_message_ids=known,
        max_messages=2,
        now_ms=2_000_000,
    )
    assert [message.text for message in inbound] == ["消息3", "消息4"]
    assert all(message.is_owner is False for message in inbound)


async def test_snapshot_and_result_models_validate() -> None:
    """解析结果 / 快照模型：标题空白与置信度越界拒绝。"""
    with pytest.raises(ValidationError):
        WechatGuiSnapshotModel(
            title="  ", messages=[], screenshot_sha256="s", captured_at_ms=1
        )
    with pytest.raises(ValidationError):
        WechatGuiSettings(title_mismatch_action="explode")
