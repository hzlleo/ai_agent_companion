"""harness/tools 沙盒注册测试——register_sandbox_tool 代理回路 + 规格面。

覆盖矩阵：代理执行器回路（fake 客户端 ok → registry ok=True；ok=False →
INTERNAL_ERROR 归一）；本地参数校验（fail-fast——客户端零调用）与默认值
物化（timeout 默认 10 经 wire）；http_fetch 规格的 LLM 呈现面（名称 /
schema 形态 / args_model）；HttpFetchArgsModel 校验矩阵；wire 契约模型
基础面（与沙盒树镜像的同一组断言——漂移门禁的行为锚点）。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.sandbox import register_sandbox_tool
from packages.harness.tools.sandbox_models import (
    HTTP_FETCH_TOOL_NAME,
    HealthResponseModel,
    HttpFetchArgsModel,
    InvokeRequestModel,
    InvokeResponseModel,
    build_http_fetch_spec,
)
from packages.harness.tools.tool_models import ToolResultModel


class FakeSandboxClient:
    """fake 沙盒客户端（记录调用；可配置成败终态——SandboxClientFace 满足）。"""

    def __init__(self, *, fail: bool = False, error: str = "目标域名不在允许清单") -> None:
        """初始化（fail=True 时 invoke 返回 ok=False 终态）。"""
        self.fail = fail
        self.error = error
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.probed = False
        self.has_token = True

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """记录调用并返回配置的终态。"""
        self.invocations.append((tool, dict(arguments)))
        if self.fail:
            return ToolResultModel(tool=tool, ok=False, error=self.error)
        return ToolResultModel(tool=tool, ok=True, result={"status_code": 200, "body": "ok"})

    async def probe_health(self) -> bool:
        """fake 探测（恒成功——注册决策由 app wiring 测试覆盖）。"""
        self.probed = True
        return True


def _registered_registry(client: FakeSandboxClient) -> ToolRegistry:
    """构造已注册 http_fetch 沙盒代理工具的注册表。"""
    registry = ToolRegistry()
    register_sandbox_tool(registry, client, build_http_fetch_spec())
    return registry


class TestRegisterSandboxTool:
    async def test_proxy_round_trip_success(self) -> None:
        """代理回路（成功）：registry.execute → fake invoke → ok=True 结果透传。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            HTTP_FETCH_TOOL_NAME, {"url": "https://example.com/x"}
        )
        assert result.ok is True
        assert result.tool == HTTP_FETCH_TOOL_NAME
        assert result.result == {"status_code": 200, "body": "ok"}
        # wire 面：参数经本地默认值物化后透传（timeout 默认 10）
        assert client.invocations == [
            (HTTP_FETCH_TOOL_NAME, {"url": "https://example.com/x", "timeout_seconds": 10})
        ]

    async def test_proxy_failure_maps_to_business_error(self) -> None:
        """代理回路（失败）：fake ok=False → INTERNAL_ERROR 归一（registry 隔离）。"""
        client = FakeSandboxClient(fail=True, error="目标域名不在允许清单: evil.example.org")
        registry = _registered_registry(client)
        result = await registry.execute(
            HTTP_FETCH_TOOL_NAME, {"url": "https://evil.example.org/x"}
        )
        assert result.ok is False
        assert result.result == {}
        assert "INTERNAL_ERROR" in (result.error or "")
        assert "沙盒工具 http_fetch 执行失败" in (result.error or "")
        assert "允许清单" in (result.error or "")

    async def test_local_validation_fail_fast(self) -> None:
        """本地参数校验：非法 URL（ftp）在 registry 层拒绝——客户端零调用。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            HTTP_FETCH_TOOL_NAME, {"url": "ftp://example.com/file"}
        )
        assert result.ok is False
        assert "参数校验失败" in (result.error or "")
        assert client.invocations == []

    async def test_local_timeout_bounds_enforced(self) -> None:
        """本地校验：timeout 越界（31）在 registry 层拒绝。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        result = await registry.execute(
            HTTP_FETCH_TOOL_NAME,
            {"url": "https://example.com/x", "timeout_seconds": 31},
        )
        assert result.ok is False
        assert client.invocations == []

    async def test_spec_listed_for_llm(self) -> None:
        """LLM 呈现面：list_specs 携带 http_fetch 定义（duty 工具清单消费）。"""
        client = FakeSandboxClient()
        registry = _registered_registry(client)
        definitions = registry.list_specs()
        http_fetch = next(d for d in definitions if d.name == HTTP_FETCH_TOOL_NAME)
        assert http_fetch.description
        schema = http_fetch.parameters.model_dump(exclude_none=True)
        assert schema["required"] == ["url"]
        assert set(schema["properties"]) == {"url", "timeout_seconds"}
        assert schema["properties"]["timeout_seconds"]["maximum"] == 30


class TestHttpFetchArgsModel:
    async def test_valid_and_default(self) -> None:
        """合法参数：url 直读 + timeout 默认 10。"""
        args = HttpFetchArgsModel.model_validate({"url": "https://example.com/page"})
        assert args.timeout_seconds == 10

    async def test_scheme_rejected(self) -> None:
        """协议面：ftp / 无协议拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "ftp://example.com/f"})
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "example.com/page"})

    async def test_missing_host_rejected(self) -> None:
        """主机面：http:// 无主机拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate({"url": "http://"})

    async def test_timeout_bounds(self) -> None:
        """超时边界：1 / 30 合法；0 / 31 拒绝。"""
        assert HttpFetchArgsModel.model_validate(
            {"url": "https://example.com", "timeout_seconds": 1}
        ).timeout_seconds == 1
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate(
                {"url": "https://example.com", "timeout_seconds": 0}
            )
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate(
                {"url": "https://example.com", "timeout_seconds": 31}
            )

    async def test_extra_fields_rejected(self) -> None:
        """extra=forbid：未知参数拒绝。"""
        with pytest.raises(ValidationError):
            HttpFetchArgsModel.model_validate(
                {"url": "https://example.com", "method": "POST"}
            )

    async def test_spec_shape(self) -> None:
        """规格形态：name / args_model 锚点。"""
        spec = build_http_fetch_spec()
        assert spec.name == HTTP_FETCH_TOOL_NAME
        assert spec.args_model is HttpFetchArgsModel


class TestWireModels:
    async def test_invoke_request_defaults_and_extra_forbid(self) -> None:
        """请求模型：arguments 默认空 dict；未知键拒绝。"""
        request = InvokeRequestModel(tool="http_fetch")
        assert request.arguments == {}
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="t", arguments={}, unexpected=1)

    async def test_invoke_request_tool_bounds(self) -> None:
        """请求模型：工具名空串 / 超 64 拒绝。"""
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="")
        with pytest.raises(ValidationError):
            InvokeRequestModel(tool="x" * 65)

    async def test_invoke_response_terminal_shapes(self) -> None:
        """响应模型：成功 / 失败终态形态 + latency 非负。"""
        success = InvokeResponseModel()
        assert (success.ok, success.result, success.error, success.latency_ms) == (
            True,
            {},
            None,
            0,
        )
        failure = InvokeResponseModel(ok=False, error="失败", latency_ms=7)
        assert failure.ok is False and failure.latency_ms == 7
        with pytest.raises(ValidationError):
            InvokeResponseModel(latency_ms=-1)

    async def test_health_response_default(self) -> None:
        """健康模型：status 缺省 ok。"""
        assert HealthResponseModel().status == "ok"
