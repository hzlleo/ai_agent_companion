"""wire 契约 Model Classes 测试——请求 / 响应 / 健康载荷的校验面。

覆盖矩阵：InvokeRequestModel 默认值与 extra 拒绝、工具名长度边界；
InvokeResponseModel 默认终态与 latency 非负；HealthResponseModel 缺省值。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tool_sandbox.wire_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
)


class TestInvokeRequestModel:
    async def test_defaults_materialize(self) -> None:
        """缺省构造：arguments 空 dict（调用方可不传参）。"""
        request = InvokeRequestModel(tool="http_fetch")
        assert request.tool == "http_fetch"
        assert request.arguments == {}

    async def test_extra_fields_forbidden(self) -> None:
        """extra=forbid：未知键拒绝（wire 契约严格面）。"""
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="t", arguments={}, unexpected=1)

    async def test_tool_name_bounds(self) -> None:
        """工具名边界：空串 / 超 64 字符拒绝。"""
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="")
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="x" * 65)

    async def test_arguments_passthrough(self) -> None:
        """arguments 原样承载（JSON 对象任意键值）。"""
        request = InvokeRequestModel(tool="t", arguments={"url": "https://a.example/x"})
        assert request.arguments == {"url": "https://a.example/x"}


class TestInvokeResponseModel:
    async def test_defaults_are_success_terminal(self) -> None:
        """缺省构造：成功终态（ok=True / result 空 / error None / latency 0）。"""
        response = InvokeResponseModel()
        assert response.ok is True
        assert response.result == {}
        assert response.error is None
        assert response.latency_ms == 0

    async def test_failure_terminal_shape(self) -> None:
        """失败终态：ok=False + error 文案 + latency 非负。"""
        response = InvokeResponseModel(ok=False, error="目标域名不在允许清单", latency_ms=7)
        assert response.ok is False
        assert response.error == "目标域名不在允许清单"
        assert response.latency_ms == 7

    async def test_negative_latency_rejected(self) -> None:
        """latency_ms 非负约束：负值拒绝。"""
        with pytest.raises(ValidationError):
            InvokeResponseModel(latency_ms=-1)


class TestHealthResponseModel:
    async def test_default_status_ok(self) -> None:
        """缺省构造：status=ok（存活判定面）。"""
        assert HealthResponseModel().status == "ok"
