"""``core.errors`` 单元测试——BusinessException / ErrorCode / 状态码映射。

覆盖矩阵：

    - 构造：code 枚举 / 字符串字面量 / details / message 覆盖 / headers；
    - message 派生链：显式 message → resolver 回调 → ``str(code)`` 回退
      （monkeypatch 控制 resolver，保证与模块导入顺序无关的确定性）；
    - ``http_status`` 属性 + ``get_http_status_for_code``：枚举成员 /
      字符串 / 未知码默认 400；
    - ``ErrorCode`` StrEnum 语义：``==`` 字符串比较 / ``__str__`` / 值域。
"""

from __future__ import annotations

from typing import Any

import pytest

import packages.harness.core.errors as errors_module
from packages.harness.core.errors import (
    BusinessException,
    ErrorCode,
    get_http_status_for_code,
    register_message_resolver,
)


class TestBusinessExceptionConstruction:
    async def test_default_code_is_internal_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺省构造：code=INTERNAL_ERROR；resolver 未注册时 message 回退 code 字符串。

        monkeypatch 显式置空 resolver——全量运行时 ``core.i18n`` 已在收集期
        注册全局回调，此处锁定「未注册」分支的确定性。
        """
        monkeypatch.setattr(errors_module, "_message_resolver", None)
        exc = BusinessException()
        assert exc.code == "INTERNAL_ERROR"
        assert exc.message == "INTERNAL_ERROR"

    async def test_code_as_enum_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """code 传 ErrorCode 枚举成员——StrEnum 兼容字符串比较。"""
        monkeypatch.setattr(errors_module, "_message_resolver", None)
        exc = BusinessException(ErrorCode.NOT_FOUND)
        assert exc.code == ErrorCode.NOT_FOUND
        assert exc.code == "NOT_FOUND"

    async def test_code_as_string_literal(self) -> None:
        """code 传字符串字面量（向后兼容形态）。"""
        exc = BusinessException("VALIDATION_ERROR")
        assert exc.code == "VALIDATION_ERROR"

    async def test_details_carried(self) -> None:
        """details 附加详情透传保留。"""
        exc = BusinessException(ErrorCode.NOT_FOUND, details={"persona_id": "p1"})
        assert exc.details == {"persona_id": "p1"}

    async def test_details_defaults_none(self) -> None:
        """details 缺省 None。"""
        exc = BusinessException()
        assert exc.details is None

    async def test_explicit_message_overrides_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 message 优先于 resolver 回调派生。"""

        def _resolver(code: str, details: dict[str, Any] | None) -> str:
            return "resolver-derived"

        monkeypatch.setattr(errors_module, "_message_resolver", _resolver)
        exc = BusinessException(ErrorCode.INTERNAL_ERROR, message="显式覆盖")
        assert exc.message == "显式覆盖"

    async def test_resolver_derived_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未显式传 message 时经 resolver 回调派生（含 details 插值）。"""

        def _resolver(code: str, details: dict[str, Any] | None) -> str:
            return f"msg:{code}:{details}"

        monkeypatch.setattr(errors_module, "_message_resolver", _resolver)
        exc = BusinessException(ErrorCode.NOT_FOUND, details={"k": "v"})
        assert exc.message == "msg:NOT_FOUND:{'k': 'v'}"

    async def test_headers_carried(self) -> None:
        """headers 额外响应头透传保留。"""
        exc = BusinessException(ErrorCode.RATE_LIMITED, headers={"Retry-After": "5"})
        assert exc.headers == {"Retry-After": "5"}

    async def test_headers_defaults_none(self) -> None:
        """headers 缺省 None。"""
        exc = BusinessException()
        assert exc.headers is None

    async def test_exception_args_carry_message(self) -> None:
        """``super().__init__(message)``——``str(exc)`` 即 message。"""
        exc = BusinessException(ErrorCode.CONFLICT, message="状态冲突")
        assert str(exc) == "状态冲突"

    async def test_repr_format(self) -> None:
        """``__repr__`` 调试友好形态（枚举 code 呈 Enum repr，字符串 code 呈裸值）。"""
        exc_str = BusinessException("NOT_FOUND", message="未找到")
        assert repr(exc_str) == "BusinessException(code='NOT_FOUND', message='未找到')"
        exc_enum = BusinessException(ErrorCode.NOT_FOUND, message="未找到")
        assert "ErrorCode.NOT_FOUND" in repr(exc_enum)
        assert "message='未找到'" in repr(exc_enum)


class TestBusinessExceptionHttpStatus:
    async def test_http_status_from_enum(self) -> None:
        """枚举成员 → 携带的 http_status。"""
        exc = BusinessException(ErrorCode.UNAUTHORIZED)
        assert exc.http_status == 401

    async def test_http_status_from_known_string(self) -> None:
        """已知字符串字面量 → 按值查枚举。"""
        exc = BusinessException("TOKEN_EXPIRED")
        assert exc.http_status == 401

    async def test_http_status_unknown_code_defaults_400(self) -> None:
        """未知 code → 默认 400。"""
        exc = BusinessException("TOTALLY_UNKNOWN_CODE")
        assert exc.http_status == 400


class TestGetHttpStatusForCode:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (ErrorCode.INTERNAL_ERROR, 500),
            (ErrorCode.BAD_REQUEST, 400),
            (ErrorCode.VALIDATION_ERROR, 422),
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.METHOD_NOT_ALLOWED, 405),
            (ErrorCode.CONFLICT, 409),
            (ErrorCode.UNSUPPORTED_MEDIA_TYPE, 415),
            (ErrorCode.RATE_LIMITED, 429),
            (ErrorCode.UNAUTHORIZED, 401),
            (ErrorCode.FORBIDDEN, 403),
            (ErrorCode.TOKEN_EXPIRED, 401),
            (ErrorCode.TOKEN_INVALID, 401),
            (ErrorCode.CONFIG_INVALID, 422),
            (ErrorCode.LLM_CALL_FAILED, 503),
            (ErrorCode.LLM_PROVIDER_NOT_SUPPORTED, 422),
            (ErrorCode.LLM_API_KEY_MISSING, 500),
            (ErrorCode.LLM_REQUEST_FAILED, 503),
            (ErrorCode.MODEL_NOT_FOUND, 404),
        ],
    )
    async def test_all_members_map_expected_status(self, code: ErrorCode, expected: int) -> None:
        """全枚举成员 → http_status 逐成员断言。"""
        assert get_http_status_for_code(code) == expected

    async def test_string_literal_lookup(self) -> None:
        """字符串字面量按值查表。"""
        assert get_http_status_for_code("CONFLICT") == 409

    async def test_unknown_string_defaults_400(self) -> None:
        """未知字符串默认 400。"""
        assert get_http_status_for_code("NO_SUCH_CODE") == 400


class TestErrorCodeEnum:
    async def test_strenum_string_equality(self) -> None:
        """``ErrorCode.X == "X"`` 为 True（StrEnum 兼容契约）。"""
        assert ErrorCode.INTERNAL_ERROR == "INTERNAL_ERROR"
        assert ErrorCode.NOT_FOUND == "NOT_FOUND"

    async def test_str_returns_plain_value(self) -> None:
        """``__str__`` 输出裸值（非 ``EnumClass.MEMBER`` 形态）。"""
        assert str(ErrorCode.RATE_LIMITED) == "RATE_LIMITED"

    async def test_member_is_str_instance(self) -> None:
        """枚举成员 isinstance str（StrEnum 语义）。"""
        assert isinstance(ErrorCode.NOT_FOUND, str)

    async def test_value_equals_code(self) -> None:
        """成员 value 与 code 字符串一致（预构建索引的口径基础）。"""
        for member in ErrorCode:
            assert member.value == str(member)


class TestRegisterMessageResolver:
    async def test_register_then_derive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """注册 resolver 后构造异常走回调派生。"""
        calls: list[tuple[str, dict[str, Any] | None]] = []

        def _resolver(code: str, details: dict[str, Any] | None) -> str:
            calls.append((code, details))
            return f"resolved-{code}"

        monkeypatch.setattr(errors_module, "_message_resolver", _resolver)
        exc = BusinessException(ErrorCode.VALIDATION_ERROR, details={"f": "x"})
        assert exc.message == "resolved-VALIDATION_ERROR"
        assert calls == [("VALIDATION_ERROR", {"f": "x"})]

    async def test_register_message_resolver_sets_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``register_message_resolver`` 写入模块级回调（经原函数验证）。"""

        def _sentinel(code: str, details: dict[str, Any] | None) -> str:
            return "sentinel"

        monkeypatch.setattr(errors_module, "_message_resolver", None)
        register_message_resolver(_sentinel)
        exc = BusinessException(ErrorCode.CONFIG_INVALID)
        assert exc.message == "sentinel"
