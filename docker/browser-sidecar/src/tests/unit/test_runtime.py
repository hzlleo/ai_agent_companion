"""sidecar 工具运行时测试——注册 / 列举 / 隔离执行 / 计时。

覆盖矩阵：注册序与同名覆盖；invoke 的未知工具 / 参数校验失败 /
受控失败（BrowserSidecarError）/ 执行器异常四类隔离（恒不抛 + latency
非负）；args_model 默认值物化与 None 透传；注入时钟的 latency 精确断言。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from browser_sidecar.runtime import (
    BrowserSidecarError,
    BrowserSidecarRuntime,
    BrowserSidecarToolSpec,
)
from browser_sidecar.wire_models import InvokeRequestModel


class _EchoArgs(BaseModel):
    """测试工具参数面（校验边界 + 默认值物化断言用）。"""

    model_config = {"extra": "forbid"}

    value: int = Field(ge=0, le=10)
    limit: int = Field(default=5, ge=1, le=20)


def _echo_tool() -> tuple[BrowserSidecarToolSpec, Any]:
    """构造 echo 测试工具（参数校验 + 结果回显）。"""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """回显参数。"""
        return {"echo": args["value"], "limit": args["limit"]}

    spec = BrowserSidecarToolSpec(name="echo", description="回显测试", args_model=_EchoArgs)
    return spec, _execute


def _clock_factory(values: list[int]) -> Any:
    """构造注入时钟（按序吐出 epoch 毫秒——latency 精确断言面）。"""
    iterator = iter(values)

    def _clock() -> int:
        """取下一个时钟值。"""
        return next(iterator)

    return _clock


class TestRegistration:
    async def test_tool_names_in_registration_order(self) -> None:
        """列举：注册序工具名列表。"""
        runtime = BrowserSidecarRuntime()
        spec, executor = _echo_tool()
        runtime.register(spec, executor)
        runtime.register(BrowserSidecarToolSpec(name="raw", description=""), executor)
        assert runtime.tool_names == ["echo", "raw"]

    async def test_same_name_overwrites(self) -> None:
        """同名重复注册：后写覆盖前写（装配面唯一）。"""
        runtime = BrowserSidecarRuntime()
        spec, _ = _echo_tool()

        async def _new_executor(args: dict[str, Any]) -> dict[str, Any]:
            """覆盖后的执行器。"""
            return {"replaced": True}

        runtime.register(spec, _new_executor)
        response = await runtime.invoke(InvokeRequestModel(tool="echo", arguments={"value": 1}))
        assert response.ok is True
        assert response.result == {"replaced": True}


class TestInvokeIsolation:
    async def test_unknown_tool_isolated(self) -> None:
        """未知工具：ok=False + error 携带工具名与可用清单；不上抛。"""
        runtime = BrowserSidecarRuntime()
        spec, executor = _echo_tool()
        runtime.register(spec, executor)
        response = await runtime.invoke(InvokeRequestModel(tool="nope"))
        assert response.ok is False
        assert response.result == {}
        assert "未知工具: nope" in (response.error or "")
        assert "echo" in (response.error or "")
        assert response.latency_ms >= 0

    async def test_args_validation_failure_isolated(self) -> None:
        """参数校验失败（越界 / 未知键）：ok=False；不上抛。"""
        runtime = BrowserSidecarRuntime()
        spec, executor = _echo_tool()
        runtime.register(spec, executor)
        out_of_range = await runtime.invoke(
            InvokeRequestModel(tool="echo", arguments={"value": 99})
        )
        assert out_of_range.ok is False
        assert "参数校验失败" in (out_of_range.error or "")
        unknown_key = await runtime.invoke(
            InvokeRequestModel(tool="echo", arguments={"value": 1, "extra": True})
        )
        assert unknown_key.ok is False

    async def test_args_defaults_materialized(self) -> None:
        """args_model 默认值物化：缺省参数进入执行器（limit=5 形态）。"""
        seen: list[dict[str, Any]] = []

        async def _execute(args: dict[str, Any]) -> dict[str, Any]:
            """记录收到的参数。"""
            seen.append(args)
            return {}

        runtime = BrowserSidecarRuntime()
        runtime.register(BrowserSidecarToolSpec(name="with_defaults", args_model=_EchoArgs), _execute)
        response = await runtime.invoke(
            InvokeRequestModel(tool="with_defaults", arguments={"value": 1})
        )
        assert response.ok is True
        assert seen[0] == {"value": 1, "limit": 5}

    async def test_spec_without_args_model_passes_raw(self) -> None:
        """args_model=None：参数原样透传（不校验）。"""
        seen: list[dict[str, Any]] = []

        async def _execute(args: dict[str, Any]) -> dict[str, Any]:
            """记录收到的参数。"""
            seen.append(args)
            return {"ok": True}

        runtime = BrowserSidecarRuntime()
        runtime.register(BrowserSidecarToolSpec(name="raw"), _execute)
        response = await runtime.invoke(
            InvokeRequestModel(tool="raw", arguments={"anything": [1, 2]})
        )
        assert response.ok is True
        assert seen[0] == {"anything": [1, 2]}

    async def test_controlled_error_isolated(self) -> None:
        """受控失败（BrowserSidecarError）：ok=False + 受控文案透传。"""
        runtime = BrowserSidecarRuntime()

        async def _refuse(args: dict[str, Any]) -> dict[str, Any]:
            """抛受控失败的执行器。"""
            raise BrowserSidecarError("目标域名不在允许清单: evil.example.org")

        runtime.register(BrowserSidecarToolSpec(name="refuse"), _refuse)
        response = await runtime.invoke(InvokeRequestModel(tool="refuse"))
        assert response.ok is False
        assert response.error == "目标域名不在允许清单: evil.example.org"

    async def test_executor_exception_isolated(self) -> None:
        """执行器异常（非受控）：ok=False + 类型名文案；不上抛。"""
        runtime = BrowserSidecarRuntime()

        async def _boom(args: dict[str, Any]) -> dict[str, Any]:
            """抛 RuntimeError 的执行器。"""
            raise RuntimeError("boom")

        runtime.register(BrowserSidecarToolSpec(name="boom"), _boom)
        response = await runtime.invoke(InvokeRequestModel(tool="boom"))
        assert response.ok is False
        assert "工具执行器异常" in (response.error or "")
        assert "RuntimeError" in (response.error or "")


class TestTiming:
    async def test_success_latency_via_injected_clock(self) -> None:
        """成功路径 latency：注入时钟精确断言（epoch 毫秒差）。"""
        runtime = BrowserSidecarRuntime(clock=_clock_factory([1000, 1050]))
        spec, executor = _echo_tool()
        runtime.register(spec, executor)
        response = await runtime.invoke(
            InvokeRequestModel(tool="echo", arguments={"value": 3})
        )
        assert response.ok is True
        assert response.result == {"echo": 3, "limit": 5}
        assert response.error is None
        assert response.latency_ms == 50

    async def test_failure_latency_via_injected_clock(self) -> None:
        """失败路径 latency：注入时钟精确断言（含失败耗时）。"""
        runtime = BrowserSidecarRuntime(clock=_clock_factory([2000, 2027]))
        response = await runtime.invoke(InvokeRequestModel(tool="missing"))
        assert response.ok is False
        assert response.latency_ms == 27

    async def test_success_response_shape(self) -> None:
        """成功响应 wire 形态：ok / result / error=None / latency 字段齐备。"""
        runtime = BrowserSidecarRuntime()
        spec, executor = _echo_tool()
        runtime.register(spec, executor)
        response = await runtime.invoke(
            InvokeRequestModel(tool="echo", arguments={"value": 3, "limit": 2})
        )
        assert response.model_dump() == {
            "ok": True,
            "result": {"echo": 3, "limit": 2},
            "error": None,
            "latency_ms": response.latency_ms,
        }
        assert isinstance(response.latency_ms, int)


class TestSpecValidation:
    async def test_spec_name_bounds(self) -> None:
        """规格工具名边界：空串 / 超 64 字符拒绝（注册面前置）。"""
        with pytest.raises(ValidationError):
            BrowserSidecarToolSpec(name="")
        with pytest.raises(ValidationError):
            BrowserSidecarToolSpec(name="x" * 65)
