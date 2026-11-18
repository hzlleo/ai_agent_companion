"""抖音 web 通道 Settings 测试——DOUYIN_ env 面 / 默认值 / 不变式 / 隔离构造。

零真实配置：全局单例读取根 conftest 硬覆盖值；隔离实例经显式字段构造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.harness.config.models import (
    ChannelsSettings,
    DouyinChannelSettings,
    Settings,
    settings,
)
from packages.harness.config.settings import build_isolated_settings

#: DouyinChannelSettings 全量 env 面键（S3 硬覆盖一致性 + 默认值测试清理面）。
_DOUYIN_ENV_KEYS = (
    "DOUYIN_ENABLED",
    "DOUYIN_CHAT_URL",
    "DOUYIN_CONVERSATION_ID",
    "DOUYIN_POLL_INTERVAL_SECONDS",
    "DOUYIN_MAX_MESSAGES_PER_POLL",
    "DOUYIN_BACKOFF_BASE_SECONDS",
    "DOUYIN_BACKOFF_MAX_SECONDS",
)


async def test_settings_channels_douyin_section_maps_env() -> None:
    """channels.douyin 分节与 DOUYIN_* env 面对齐（conftest 硬覆盖值）。"""
    douyin = settings.channels.douyin
    assert douyin.enabled is False
    assert douyin.chat_url == ""
    assert douyin.conversation_id == ""
    assert douyin.poll_interval_seconds == 5.0
    assert douyin.max_messages_per_poll == 20
    assert douyin.backoff_base_seconds == 2.0
    assert douyin.backoff_max_seconds == 60.0


async def test_douyin_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """DouyinChannelSettings 代码默认值（env 面全清——零 env 依赖形态）。"""
    for key in _DOUYIN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = DouyinChannelSettings()
    assert default.enabled is False
    assert default.chat_url == ""
    assert default.conversation_id == ""
    assert default.poll_interval_seconds == 5.0
    assert default.max_messages_per_poll == 20


async def test_douyin_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOUYIN_* env 逐键覆写（字段名 → env 名映射契约 + chat_url 裁剪）。"""
    monkeypatch.setenv("DOUYIN_ENABLED", "true")
    monkeypatch.setenv("DOUYIN_CHAT_URL", "  https://www.douyin.com/im  ")
    monkeypatch.setenv("DOUYIN_CONVERSATION_ID", "c-prod")
    monkeypatch.setenv("DOUYIN_POLL_INTERVAL_SECONDS", "8")
    monkeypatch.setenv("DOUYIN_MAX_MESSAGES_PER_POLL", "50")
    monkeypatch.setenv("DOUYIN_BACKOFF_BASE_SECONDS", "3")
    monkeypatch.setenv("DOUYIN_BACKOFF_MAX_SECONDS", "30")
    douyin = DouyinChannelSettings()
    assert douyin.enabled is True
    assert douyin.chat_url == "https://www.douyin.com/im"
    assert douyin.conversation_id == "c-prod"
    assert douyin.poll_interval_seconds == 8.0
    assert douyin.max_messages_per_poll == 50
    assert douyin.backoff_base_seconds == 3.0
    assert douyin.backoff_max_seconds == 30.0


async def test_douyin_settings_rejects_bad_chat_url() -> None:
    """chat_url 校验：非 http/https 拒绝（空串 = 不导航，合法）。"""
    assert DouyinChannelSettings(chat_url="").chat_url == ""
    with pytest.raises(ValidationError):
        DouyinChannelSettings(chat_url="www.douyin.com/im")


async def test_douyin_settings_rejects_bad_bounds() -> None:
    """不变式：退避封顶不得小于基数；轮询间隔必须 > 0；采集上限 1-100。"""
    with pytest.raises(ValidationError):
        DouyinChannelSettings(backoff_base_seconds=10.0, backoff_max_seconds=5.0)
    with pytest.raises(ValidationError):
        DouyinChannelSettings(poll_interval_seconds=0)
    with pytest.raises(ValidationError):
        DouyinChannelSettings(max_messages_per_poll=0)
    with pytest.raises(ValidationError):
        DouyinChannelSettings(max_messages_per_poll=101)


async def test_douyin_settings_isolated_construction() -> None:
    """隔离构造：显式字段构造 + 聚合容器承载 + 嵌套 env 覆写形态。"""
    channels = ChannelsSettings(
        douyin=DouyinChannelSettings(
            enabled=True, chat_url="https://www.douyin.com/im", conversation_id="c-1"
        )
    )
    isolated = build_isolated_settings(channels=channels)
    assert isolated.channels.douyin.enabled is True
    assert isolated.channels.douyin.conversation_id == "c-1"
    # 其余通道保持缺省关闭（单通道启用不连带装配）
    assert isolated.channels.telegram.enabled is False
    assert isolated.channels.qq.enabled is False
    assert isolated.channels.wecom.enabled is False
    assert isinstance(isolated, Settings)
