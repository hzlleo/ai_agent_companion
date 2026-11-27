"""外部工具扩展装配测试——总开关 / 同名保护 / 构造失败隔离 / 报告。"""

from __future__ import annotations

import httpx
import pytest

from packages.harness.config.models import ExtensionsConfig
from packages.harness.tools.extension_registry import register_extension_tools
from packages.harness.tools.registry import ToolRegistry, register_builtin_tools


def _config(
    *,
    enabled: bool = True,
    allow_override: bool = False,
    name: str = "calendar_query",
    url: str = "http://service.local/query",
    headers: dict[str, str] | None = None,
) -> ExtensionsConfig:
    """构造测试用扩展配置。"""
    return ExtensionsConfig.model_validate(
        {
            "enabled": enabled,
            "allow_override_builtin": allow_override,
            "tools": [
                {
                    "name": name,
                    "description": "测试扩展工具",
                    "parameters": {
                        "type": "object",
                        "properties": {"date": {"type": "string"}},
                        "required": ["date"],
                        "additionalProperties": False,
                    },
                    "kind": "http",
                    "http": {
                        "url": url,
                        "method": "POST",
                        "headers": headers or {},
                        "result_path": "events",
                    },
                }
            ],
        }
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """返回固定成功 JSON。"""
    return httpx.Response(200, json={"events": [{"title": "站会"}]})


async def test_disabled_registers_nothing() -> None:
    """总开关关闭：空报告 + 零注册 + 工厂零调用。"""
    factory_calls: list[bool] = []

    def factory() -> httpx.AsyncClient:
        """记录工厂调用。"""
        factory_calls.append(True)
        return httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))

    registry = ToolRegistry()
    report = register_extension_tools(registry, _config(enabled=False), http_client_factory=factory)
    assert report.registered_count == 0
    assert report.skipped_count == 0
    assert registry.tool_names == []
    assert factory_calls == []


async def test_empty_tools_registers_nothing() -> None:
    """工具列表为空：空报告 + 零注册。"""
    registry = ToolRegistry()
    report = register_extension_tools(
        registry,
        ExtensionsConfig(enabled=True, tools=[]),
        http_client_factory=lambda: httpx.AsyncClient(),
    )
    assert report.registered_count == 0
    assert registry.tool_names == []


async def test_normal_registration_and_execution() -> None:
    """正常注册：报告计数 + registry 可执行（工厂执行期调用）。"""
    factory_calls: list[bool] = []

    def factory() -> httpx.AsyncClient:
        """记录工厂调用并返回 MockTransport 客户端。"""
        factory_calls.append(True)
        return client

    registry = ToolRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    try:
        report = register_extension_tools(
            registry, _config(), http_client_factory=factory
        )
        assert report.registered == ["calendar_query"]
        assert report.registered_count == 1
        assert report.skipped_count == 0
        assert registry.tool_names == ["calendar_query"]
        assert factory_calls == []
        result = await registry.execute("calendar_query", {"date": "2026-01-01"})
    finally:
        await client.aclose()
    assert result.ok is True
    assert result.result["text"] == '[{"title":"站会"}]'
    assert factory_calls == [True]


async def test_builtin_name_conflict_skipped() -> None:
    """同名已注册工具：默认拒绝覆盖（内置执行器保持原样）。"""
    registry = ToolRegistry()
    register_builtin_tools(registry)
    report = register_extension_tools(
        registry,
        _config(name="get_current_time"),
        http_client_factory=lambda: httpx.AsyncClient(),
    )
    assert report.registered == []
    assert report.skipped_count == 1
    assert report.skipped[0].name == "get_current_time"
    result = await registry.execute("get_current_time")
    assert result.ok is True
    assert "epoch_ms" in result.result


async def test_allow_override_registers_extension() -> None:
    """allow_override_builtin=true：同名扩展工具覆盖内置执行器。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    try:
        registry = ToolRegistry()
        register_builtin_tools(registry)
        report = register_extension_tools(
            registry,
            _config(name="get_current_time", allow_override=True),
            http_client_factory=lambda: client,
        )
        assert report.registered == ["get_current_time"]
        result = await registry.execute("get_current_time", {"date": "2026-01-01"})
    finally:
        await client.aclose()
    assert result.ok is True
    assert result.result["text"] == '[{"title":"站会"}]'


async def test_missing_env_skips_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造期 env 缺失：跳过该工具并记录原因，不阻塞其它工具。"""
    monkeypatch.delenv("EXT_REGISTRY_TOKEN", raising=False)
    registry = ToolRegistry()
    report = register_extension_tools(
        registry,
        _config(headers={"Authorization": "{env:EXT_REGISTRY_TOKEN}"}),
        http_client_factory=lambda: httpx.AsyncClient(),
    )
    assert report.registered == []
    assert report.skipped_count == 1
    assert "EXT_REGISTRY_TOKEN" in report.skipped[0].reason
    assert registry.tool_names == []


async def test_partial_failure_keeps_other_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """单工具失败隔离：成功工具照常注册，失败工具进入跳过明细。"""
    monkeypatch.delenv("EXT_PARTIAL_TOKEN", raising=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler))
    try:
        registry = ToolRegistry()
        config = ExtensionsConfig.model_validate(
            {
                "enabled": True,
                "tools": [
                    {
                        "name": "good_tool",
                        "description": "ok",
                        "parameters": {
                            "type": "object",
                            "properties": {"date": {"type": "string"}},
                            "required": ["date"],
                            "additionalProperties": False,
                        },
                        "kind": "http",
                        "http": {"url": "http://service.local/good"},
                    },
                    {
                        "name": "bad_tool",
                        "description": "bad",
                        "parameters": {"type": "object", "properties": {}},
                        "kind": "http",
                        "http": {
                            "url": "http://service.local/bad",
                            "headers": {"Authorization": "{env:EXT_PARTIAL_TOKEN}"},
                        },
                    },
                ],
            }
        )
        report = register_extension_tools(
            registry, config, http_client_factory=lambda: client
        )
    finally:
        await client.aclose()
    assert report.registered == ["good_tool"]
    assert report.skipped_count == 1
    assert report.skipped[0].name == "bad_tool"
    assert registry.tool_names == ["good_tool"]


async def test_undeclared_placeholder_skips_tool() -> None:
    """URL 引用未声明占位：跳过工具（构造期失败隔离）。"""
    registry = ToolRegistry()
    report = register_extension_tools(
        registry,
        _config(url="http://service.local/{missing}/x"),
        http_client_factory=lambda: httpx.AsyncClient(),
    )
    assert report.registered == []
    assert report.skipped_count == 1
    assert "未声明" in report.skipped[0].reason


async def test_invalid_tool_spec_skipped() -> None:
    """工具规格校验失败（空名称）：跳过并记录原因，不阻塞启动。"""
    registry = ToolRegistry()
    report = register_extension_tools(
        registry,
        _config(name=""),
        http_client_factory=lambda: httpx.AsyncClient(),
    )
    assert report.registered == []
    assert report.skipped_count == 1
    assert "规格校验失败" in report.skipped[0].reason
