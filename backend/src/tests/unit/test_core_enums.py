"""``core.enums`` 单元测试——Locale / TaskStatus 值域与集合真源。"""

from __future__ import annotations

from packages.harness.core.enums import (
    ACTIVE_TASK_STATUSES,
    SUPPORTED_LOCALES,
    Locale,
    TaskStatus,
)


class TestLocale:
    async def test_values(self) -> None:
        """Locale 值域：zh-CN / en。"""
        assert Locale.ZH_CN == "zh-CN"
        assert Locale.EN == "en"

    async def test_str_enum_semantics(self) -> None:
        """StrEnum 语义：成员即字符串。"""
        assert isinstance(Locale.ZH_CN, str)
        assert str(Locale.EN) == "en"

    async def test_supported_locales_frozenset(self) -> None:
        """SUPPORTED_LOCALES 恰含两成员（i18n 回退判定的真源）。"""
        assert frozenset({"zh-CN", "en"}) == SUPPORTED_LOCALES


class TestTaskStatus:
    async def test_values(self) -> None:
        """TaskStatus 五态值域。"""
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"

    async def test_active_statuses_set(self) -> None:
        """活跃态集合恰含 pending/running（停机 drain 打捞面真源）。"""
        assert frozenset({"pending", "running"}) == ACTIVE_TASK_STATUSES

    async def test_membership_by_plain_string(self) -> None:
        """裸字符串成员判定（StrEnum 哈希与 str 一致）。"""
        assert "pending" in ACTIVE_TASK_STATUSES
        assert "completed" not in ACTIVE_TASK_STATUSES
