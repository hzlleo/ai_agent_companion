"""``core.i18n`` + ``i18n_messages`` 单元测试——消息查找 / locale 解析 / 1:1 对齐锁。

覆盖矩阵：

    - ``get_message``：默认 locale（contextvar）、显式 locale、未知 key
      回退 key、占位符插值、插值失败静默、未知 locale 回退 zh-CN；
    - ``resolve_locale``：显式传入优先 / contextvar / 未知值回退默认；
    - **1:1 对齐锁**：``ErrorCode`` 成员 ↔ ``MESSAGES`` key 双向恰等
      （新增错误码未注册消息即红——回归锁，防消息表漂移）；
    - 每条目覆盖 zh-CN 与 en 两 locale。
"""

from __future__ import annotations

import pytest

from packages.harness.core.context import locale_var
from packages.harness.core.enums import Locale
from packages.harness.core.errors import ErrorCode
from packages.harness.core.i18n import get_message, resolve_locale
from packages.harness.core.i18n_messages import MESSAGES


class TestGetMessage:
    async def test_default_locale_from_contextvar(self) -> None:
        """locale_var 未设置 → 默认 zh-CN。"""
        assert get_message("NOT_FOUND") == "资源不存在"

    async def test_contextvar_locale_respected(self) -> None:
        """locale_var 设置 en → 英文消息。"""
        token = locale_var.set("en")
        try:
            assert get_message("NOT_FOUND") == "Resource not found"
        finally:
            locale_var.reset(token)

    async def test_explicit_locale_overrides_contextvar(self) -> None:
        """显式 locale 参数优先于 contextvar。"""
        token = locale_var.set("en")
        try:
            assert get_message("NOT_FOUND", locale="zh-CN") == "资源不存在"
        finally:
            locale_var.reset(token)

    async def test_unknown_key_returns_key(self) -> None:
        """未知 key 返回 key 本身（不抛异常——日志降级友好）。"""
        assert get_message("NO_SUCH_MESSAGE_KEY") == "NO_SUCH_MESSAGE_KEY"

    async def test_unknown_locale_falls_back_default(self) -> None:
        """未知 locale 回退 zh-CN 条目。"""
        assert get_message("NOT_FOUND", locale="ja-JP") == "资源不存在"

    async def test_placeholder_interpolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """kwargs 占位符插值（临时注入带占位符的测试条目验证机制）。"""
        monkeypatch.setitem(
            MESSAGES,
            "TEST_INTERP_KEY",
            {"zh-CN": "欢迎，{name}（第 {nth} 次）", "en": "Welcome, {name} (#{nth})"},
        )
        assert get_message("TEST_INTERP_KEY", name="小鱼", nth=3) == "欢迎，小鱼（第 3 次）"

    async def test_interpolation_failure_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """占位符与 kwargs 不匹配时静默忽略（文本原样返回）。"""
        monkeypatch.setitem(
            MESSAGES,
            "TEST_INTERP_KEY",
            {"zh-CN": "欢迎，{name}", "en": "Welcome, {name}"},
        )
        assert get_message("TEST_INTERP_KEY", wrong="占位符不匹配") == "欢迎，{name}"

    async def test_message_covers_both_locales(self) -> None:
        """zh-CN 与 en 条目语义抽查（每条目双 locale 由对齐锁覆盖）。"""
        assert MESSAGES["LLM_CALL_FAILED"]["zh-CN"] == "LLM 调用失败，请稍后重试"
        assert MESSAGES["LLM_CALL_FAILED"]["en"] == "LLM call failed, please retry later"


class TestResolveLocale:
    async def test_explicit_none_reads_contextvar(self) -> None:
        """locale=None → 读 locale_var。"""
        token = locale_var.set("en")
        try:
            assert resolve_locale(None) == "en"
        finally:
            locale_var.reset(token)

    async def test_explicit_value_passthrough(self) -> None:
        """显式值直接透传（含未知值——由 get_message 表回退兜底）。"""
        assert resolve_locale("en") == "en"
        assert resolve_locale("fr-FR") == "fr-FR"

    async def test_unknown_contextvar_value_falls_back(self) -> None:
        """contextvar 为未知值 → 回退默认 zh-CN。"""
        token = locale_var.set("de")
        try:
            assert resolve_locale(None) == "zh-CN"
        finally:
            locale_var.reset(token)


class TestErrorCodeMessageAlignment:
    async def test_every_error_code_has_message(self) -> None:
        """正向锁：每个 ErrorCode 成员在 MESSAGES 有条目。"""
        missing = [str(member) for member in ErrorCode if str(member) not in MESSAGES]
        assert missing == [], f"ErrorCode 成员缺消息条目: {missing}"

    async def test_every_message_key_is_error_code(self) -> None:
        """反向锁：MESSAGES 无游离 key（1:1 双向恰等）。"""
        known = {str(member) for member in ErrorCode}
        stray = [key for key in MESSAGES if key not in known]
        assert stray == [], f"MESSAGES 存在非 ErrorCode key: {stray}"

    async def test_every_entry_covers_required_locales(self) -> None:
        """每条目覆盖 zh-CN 与 en（默认回退 + 英文面完整）。"""
        for key, entry in MESSAGES.items():
            assert Locale.ZH_CN in entry, f"{key} 缺 zh-CN 条目"
            assert Locale.EN in entry, f"{key} 缺 en 条目"

    async def test_derived_exception_message_localized(self) -> None:
        """经 errors 注册的 resolver 派生本地化 message（依赖反转链路通）。"""
        from packages.harness.core.errors import BusinessException

        exc = BusinessException(ErrorCode.NOT_FOUND)
        assert exc.message == "资源不存在"

    async def test_derived_message_with_details_interpolation(self) -> None:
        """details 字段经 resolver 插值进消息（CONFIG_INVALID 占位形态）。"""
        from packages.harness.core.errors import BusinessException

        exc = BusinessException(ErrorCode.CONFIG_INVALID, details={"字段": "api_key"})
        # CONFIG_INVALID 消息无命名占位符，details dict 展开不命中占位符 → 原文
        assert exc.message == "配置不合法（详见 details）"
