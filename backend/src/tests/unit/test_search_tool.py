"""search 域工具测试——web_search 注册 / 执行器格式化 / 异常隔离。

覆盖矩阵：注册面（ToolRegistry 出现 web_search + list_specs 暴露
schema）/ 执行器（fake 服务 → count / provider / text 三键载荷 + 紧凑
中文块格式）/ 空结果渲染 / 参数校验（越界 query → ok=False 不上抛）/
服务异常隔离（预算超限 / 全降级 → ok=False + 人读错误，恒不上抛）。
"""

from __future__ import annotations

from typing import Any

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.search.search_models import (
    SearchResponseModel,
    SearchResultModel,
)
from packages.harness.search.tool import (
    WEB_SEARCH_TOOL_NAME,
    build_web_search_tool,
    format_search_results,
    register_web_search_tool,
)
from packages.harness.tools.registry import ToolRegistry


class FakeSearchService:
    """fake 搜索服务——记录调用 / 可注入异常。"""

    def __init__(self, *, error: BusinessException | None = None) -> None:
        """初始化（响应或异常注入）。"""
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        *,
        persona_id: str | None = None,
        max_results: int | None = None,
    ) -> SearchResponseModel:
        """记录调用并返回固定响应（或抛注入异常）。"""
        self.calls.append({"query": query, "persona_id": persona_id})
        if self.error is not None:
            raise self.error
        return SearchResponseModel(
            results=[
                SearchResultModel(
                    title=f"结果{index}",
                    url=f"https://example.test/{index}",
                    snippet=f"摘要{index}",
                    source_provider="bocha-main",
                )
                for index in range(1, 3)
            ],
            provider_used="bocha-main",
            latency_ms=5,
        )


class TestFormatSearchResults:
    async def test_blocks_with_title_url_snippet(self) -> None:
        """紧凑中文块：【N】标题 / 链接 / 摘要 逐行 + 空行分隔。"""
        response = SearchResponseModel(
            results=[
                SearchResultModel(
                    title="标题一", url="https://a.test", snippet="摘要一", source_provider="p"
                ),
                SearchResultModel(
                    title="标题二", url="https://b.test", snippet="摘要二", source_provider="p"
                ),
            ]
        )
        text = format_search_results(response)
        assert text == (
            "【1】标题一\n链接：https://a.test\n摘要：摘要一\n\n"
            "【2】标题二\n链接：https://b.test\n摘要：摘要二"
        )

    async def test_empty_results_renders_placeholder(self) -> None:
        """空结果：单行占位（模型可读的合法态）。"""
        text = format_search_results(SearchResponseModel(results=[]))
        assert text == "（无搜索结果）"

    async def test_missing_snippet_omitted(self) -> None:
        """无摘要的条目省略摘要行（schema 弹性）。"""
        response = SearchResponseModel(
            results=[
                SearchResultModel(title="无摘要", url="https://c.test", snippet="", source_provider="p")
            ]
        )
        assert format_search_results(response) == "【1】无摘要\n链接：https://c.test"


class TestRegisterAndExecute:
    async def test_register_exposes_tool_in_registry(self) -> None:
        """注册面：registry 出现 web_search + list_specs 携带 schema。"""
        registry = ToolRegistry()
        register_web_search_tool(registry, FakeSearchService())
        assert registry.tool_names == [WEB_SEARCH_TOOL_NAME]
        specs = registry.list_specs()
        assert specs[0].name == WEB_SEARCH_TOOL_NAME
        assert specs[0].parameters.required == ["query"]

    async def test_executor_returns_compact_payload(self) -> None:
        """执行器：count / provider / text 三键载荷（text 为中文块）。"""
        registry = ToolRegistry()
        service = FakeSearchService()
        register_web_search_tool(registry, service)
        result = await registry.execute(WEB_SEARCH_TOOL_NAME, {"query": "查询词"})
        assert result.ok is True
        assert result.error is None
        assert result.result["count"] == 2
        assert result.result["provider"] == "bocha-main"
        assert result.result["text"].startswith("【1】结果1")
        assert service.calls == [{"query": "查询词", "persona_id": None}]

    async def test_build_web_search_tool_returns_spec_pair(self) -> None:
        """工厂二元组：规格 + 执行器（chat 装配面消费形态）。"""
        service = FakeSearchService()
        spec, executor = build_web_search_tool(service)
        assert spec.name == WEB_SEARCH_TOOL_NAME
        payload = await executor({"query": "q"})
        assert payload["count"] == 2

    async def test_invalid_query_args_isolated(self) -> None:
        """参数校验失败：ok=False（空 / 越界 query——不上抛）。"""
        registry = ToolRegistry()
        register_web_search_tool(registry, FakeSearchService())
        for bad_args in ({"query": ""}, {"query": "长" * 257}, {}):
            result = await registry.execute(WEB_SEARCH_TOOL_NAME, bad_args)
            assert result.ok is False
            assert result.error is not None

    async def test_service_budget_error_isolated(self) -> None:
        """服务异常隔离：预算超限 → ok=False + 人读错误（恒不上抛）。"""
        budget_error = BusinessException(
            code="SEARCH_BUDGET_EXCEEDED",
            message="单人格每日搜索预算已用尽: used=50, limit=50",
        )
        registry = ToolRegistry()
        register_web_search_tool(registry, FakeSearchService(error=budget_error))
        result = await registry.execute(WEB_SEARCH_TOOL_NAME, {"query": "q"})
        assert result.ok is False
        assert "预算" in (result.error or "")

    async def test_service_degraded_error_isolated(self) -> None:
        """服务异常隔离：全降级 → ok=False + 错误码（恒不上抛）。"""
        degraded_error = BusinessException(
            code=ErrorCode.LLM_REQUEST_FAILED, message="搜索全部 provider 不可用"
        )
        registry = ToolRegistry()
        register_web_search_tool(registry, FakeSearchService(error=degraded_error))
        result = await registry.execute(WEB_SEARCH_TOOL_NAME, {"query": "q"})
        assert result.ok is False
        assert "provider" in (result.error or "")
