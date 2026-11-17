"""search 域客户端测试——Bocha / Zhipu（MockTransport 零真实网络）。

覆盖矩阵：

    - Bocha：请求形状（URL / Bearer 头 / 四键锁定体 {query, freshness,
      summary:false, count} / 请求超时）→ 结果归一（name/url/snippet →
      title/url/snippet + source_provider）；兼容未包裹响应形态（data
      缺失回退整体载荷 + snippet 缺失回退 summary 键）；错误映射
      （非 200 / 传输异常 / 非 JSON / 业务码非 200 / 结构破坏
      LLM_CALL_FAILED）；空结果合法态；用量事件（成功 / 失败恰一事件）。
    - Zhipu：请求形状（URL / Bearer 头 / 五键体 {search_query, search_
      engine, search_intent:false, count, request_id} + query 70 字符
      截断 + request_id 形态）→ 结果归一（title/link/content）；错误
      映射（非 200 / 传输异常 / 非 JSON / search_result 非列表）；
      空结果合法态（search_result 键缺失）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.search.bocha_client import BochaWebSearchClient
from packages.harness.search.search_models import SearchUsageEvent
from packages.harness.search.zhipu_client import ZhipuWebSearchClient

#: Bocha 包裹形态响应（任务书锁定的 {code, data:{webPages:{value}}} 形状）。
_BOCHA_WRAPPED_BODY: dict[str, Any] = {
    "code": 200,
    "log_id": "log-1",
    "msg": None,
    "data": {
        "_type": "SearchResponse",
        "webPages": {
            "webSearchUrl": "https://bochaai.com/search?q=x",
            "value": [
                {
                    "id": "id-1",
                    "name": "博查AI开放平台",
                    "url": "https://open.bochaai.com",
                    "snippet": "给AI用的世界知识搜索引擎",
                    "dateLastCrawled": "2026-11-01T00:00:00Z",
                },
                {
                    "id": "id-2",
                    "name": "第二条结果",
                    "url": "https://example.com/2",
                    "snippet": "第二条摘要",
                    "dateLastCrawled": "2026-11-02T00:00:00Z",
                },
            ],
        },
    },
}


def _bocha_client(
    handler: Any,
    *,
    api_key: str = "test-bocha-key",
    base_url: str = "http://bocha.test",
    timeout_seconds: float = 5.0,
    events: list[SearchUsageEvent] | None = None,
) -> BochaWebSearchClient:
    """构造测试 Bocha 客户端（MockTransport + 可选用量事件收集）。

    Args:
        handler: MockTransport 处理器。
        api_key: 测试密钥。
        base_url: 测试基础 URL。
        timeout_seconds: 请求超时秒数。
        events: 用量事件收集列表（提供时注入发射钩子）。

    Returns:
        BochaWebSearchClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _emit(event: SearchUsageEvent) -> None:
        """事件收集（列表侧断言真源）。"""
        if events is not None:
            events.append(event)

    return BochaWebSearchClient(
        http,
        api_key=api_key,
        base_url=base_url,
        request_timeout_seconds=timeout_seconds,
        provider_name="bocha-main",
        usage_emitter=_emit if events is not None else None,
    )


def _zhipu_client(
    handler: Any,
    *,
    api_key: str = "test-zhipu-key",
    base_url: str = "http://zhipu.test",
    timeout_seconds: float = 6.0,
    search_engine: str = "search_std",
    events: list[SearchUsageEvent] | None = None,
) -> ZhipuWebSearchClient:
    """构造测试 Zhipu 客户端（MockTransport + 可选用量事件收集）。

    Args:
        handler: MockTransport 处理器。
        api_key: 测试密钥。
        base_url: 测试基础 URL。
        timeout_seconds: 请求超时秒数。
        search_engine: 引擎码。
        events: 用量事件收集列表（提供时注入发射钩子）。

    Returns:
        ZhipuWebSearchClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _emit(event: SearchUsageEvent) -> None:
        """事件收集（列表侧断言真源）。"""
        if events is not None:
            events.append(event)

    return ZhipuWebSearchClient(
        http,
        api_key=api_key,
        base_url=base_url,
        search_engine=search_engine,
        request_timeout_seconds=timeout_seconds,
        provider_name="zhipu-fallback",
        usage_emitter=_emit if events is not None else None,
    )


class TestBochaClient:
    async def test_request_shape_locked(self) -> None:
        """请求形状锁定：URL + Bearer + 四键体 + 超时透传。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["json"] = json.loads(request.content)
            captured["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, json=_BOCHA_WRAPPED_BODY)

        client = _bocha_client(handler)
        result = await client.search("博查AI", max_results=8)
        assert captured["url"] == "http://bocha.test/v1/web-search"
        assert captured["auth"] == "Bearer test-bocha-key"
        assert captured["json"] == {
            "query": "博查AI",
            "freshness": "noLimit",
            "summary": False,
            "count": 8,
        }
        assert captured["timeout"]["read"] == pytest.approx(5.0)
        assert result.provider_used == "bocha-main"
        assert result.latency_ms >= 0
        assert [item.title for item in result.results] == ["博查AI开放平台", "第二条结果"]
        assert result.results[0].url == "https://open.bochaai.com"
        assert result.results[0].snippet == "给AI用的世界知识搜索引擎"
        assert result.results[0].source_provider == "bocha-main"

    async def test_unwrapped_body_and_summary_fallback(self) -> None:
        """兼容未包裹形态：data 缺失回退整体载荷 + snippet 回退 summary。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "_type": "SearchResponse",
                    "webPages": {
                        "value": [
                            {
                                "name": "新版形态",
                                "url": "https://example.com/new",
                                "summary": "新版摘要字段",
                                "datePublished": "2026-11-01",
                            }
                        ]
                    },
                },
            )

        client = _bocha_client(handler)
        result = await client.search("q")
        assert len(result.results) == 1
        assert result.results[0].title == "新版形态"
        assert result.results[0].snippet == "新版摘要字段"

    async def test_empty_results_is_legal(self) -> None:
        """空结果合法态：webPages.value 为空列表 → results 空列表。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})

        client = _bocha_client(handler)
        result = await client.search("q")
        assert result.results == []

    async def test_business_code_non_200_maps_call_failed(self) -> None:
        """业务码非 200：LLM_CALL_FAILED（details 携带业务码与 msg 片段）。"""
        client = _bocha_client(
            lambda request: httpx.Response(200, json={"code": 401, "msg": "鉴权失败"})
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert "401" in exc_info.value.message

    async def test_non_200_maps_call_failed(self) -> None:
        """HTTP 非 200：LLM_CALL_FAILED（details 携带状态码）。"""
        client = _bocha_client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["status_code"] == 500
        assert exc_info.value.details["service"] == "bocha"

    async def test_transport_error_maps_call_failed(self) -> None:
        """传输异常：LLM_CALL_FAILED。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _bocha_client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_non_json_maps_call_failed(self) -> None:
        """非 JSON 响应：LLM_CALL_FAILED。"""
        client = _bocha_client(lambda request: httpx.Response(200, text="not-json"))
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_missing_web_pages_maps_call_failed(self) -> None:
        """结构破坏（缺 webPages）：LLM_CALL_FAILED。"""
        client = _bocha_client(lambda request: httpx.Response(200, json={"code": 200, "data": {}}))
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert "webPages" in exc_info.value.message

    async def test_usage_events_success_and_error(self) -> None:
        """用量事件：成功恰一事件（success/无错误码）；失败恰一事件
        （error/LLM_CALL_FAILED + 真实时延）。"""
        events: list[SearchUsageEvent] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["query"] == "失败查询":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=_BOCHA_WRAPPED_BODY)

        client = _bocha_client(handler, events=events)
        await client.search("正常查询")
        with pytest.raises(BusinessException):
            await client.search("失败查询")
        assert len(events) == 2
        assert events[0].status == "success"
        assert events[0].error_code is None
        assert events[0].latency_ms >= 0
        assert events[1].status == "error"
        assert events[1].error_code == str(ErrorCode.LLM_CALL_FAILED)
        assert events[1].latency_ms >= 0

    async def test_emitter_exception_does_not_break_call(self) -> None:
        """发射钩子异常不阻断主调用（观测面非业务面）。"""

        async def broken_emit(event: SearchUsageEvent) -> None:
            """恒抛异常的发射钩子。"""
            raise RuntimeError("emitter broken")

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_BOCHA_WRAPPED_BODY))
        )
        client = BochaWebSearchClient(
            http, api_key="k", usage_emitter=broken_emit, provider_name="p"
        )
        result = await client.search("q")
        assert result.results  # 主调用不受发射失败影响


class TestZhipuClient:
    async def test_request_shape_locked(self) -> None:
        """请求形状锁定：URL + Bearer + 五键体 + request_id 形态 + 超时。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["json"] = json.loads(request.content)
            captured["timeout"] = request.extensions.get("timeout")
            return httpx.Response(
                200,
                json={
                    "id": "job-1",
                    "created": 1763000000,
                    "request_id": "req-1",
                    "search_result": [
                        {
                            "title": "智谱开放平台",
                            "link": "https://bigmodel.cn",
                            "content": "一站式大模型开发平台",
                            "media": "智谱",
                            "icon": "https://bigmodel.cn/icon.png",
                            "refer": "1",
                            "publish_date": "2026-11-01",
                        }
                    ],
                },
            )

        client = _zhipu_client(handler)
        result = await client.search("智谱", max_results=5)
        assert captured["url"] == "http://zhipu.test/api/paas/v4/web_search"
        assert captured["auth"] == "Bearer test-zhipu-key"
        assert captured["json"]["search_query"] == "智谱"
        assert captured["json"]["search_engine"] == "search_std"
        assert captured["json"]["search_intent"] is False
        assert captured["json"]["count"] == 5
        request_id = captured["json"]["request_id"]
        assert isinstance(request_id, str) and 6 <= len(request_id) <= 64
        assert captured["timeout"]["read"] == pytest.approx(6.0)
        assert result.provider_used == "zhipu-fallback"
        assert len(result.results) == 1
        assert result.results[0].title == "智谱开放平台"
        assert result.results[0].url == "https://bigmodel.cn"
        assert result.results[0].snippet == "一站式大模型开发平台"
        assert result.results[0].source_provider == "zhipu-fallback"

    async def test_long_query_truncated_to_70(self) -> None:
        """超长 query 截断到 70 字符（官方 schema maxLength——兜底降级语义）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"search_result": []})

        client = _zhipu_client(handler)
        await client.search("长" * 100)
        assert captured["json"]["search_query"] == "长" * 70

    async def test_search_engine_passthrough(self) -> None:
        """引擎码透传（条目 model 字段——search_pro 形态）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"search_result": []})

        client = _zhipu_client(handler, search_engine="search_pro")
        await client.search("q")
        assert captured["json"]["search_engine"] == "search_pro"

    async def test_missing_search_result_is_empty(self) -> None:
        """search_result 键缺失：空结果合法态。"""
        client = _zhipu_client(
            lambda request: httpx.Response(200, json={"id": "job", "created": 1})
        )
        result = await client.search("q")
        assert result.results == []

    async def test_non_200_maps_call_failed(self) -> None:
        """HTTP 非 200：LLM_CALL_FAILED（details 携带引擎码与状态码）。"""
        client = _zhipu_client(lambda request: httpx.Response(429, text="rate limited"))
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["status_code"] == 429
        assert exc_info.value.details["search_engine"] == "search_std"

    async def test_transport_error_maps_call_failed(self) -> None:
        """传输异常：LLM_CALL_FAILED。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = _zhipu_client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_non_json_maps_call_failed(self) -> None:
        """非 JSON 响应：LLM_CALL_FAILED。"""
        client = _zhipu_client(lambda request: httpx.Response(200, text="garbage"))
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_search_result_non_list_maps_call_failed(self) -> None:
        """结构破坏（search_result 非列表）：LLM_CALL_FAILED。"""
        client = _zhipu_client(
            lambda request: httpx.Response(200, json={"search_result": "oops"})
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.search("q")
        assert exc_info.value.code == ErrorCode.LLM_CALL_FAILED

    async def test_usage_events_success_and_error(self) -> None:
        """用量事件：成功 / 失败路径各恰一事件。"""
        events: list[SearchUsageEvent] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["search_query"] == "失败查询":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"search_result": []})

        client = _zhipu_client(handler, events=events)
        await client.search("正常查询")
        with pytest.raises(BusinessException):
            await client.search("失败查询")
        assert [(event.status, event.error_code) for event in events] == [
            ("success", None),
            ("error", str(ErrorCode.LLM_CALL_FAILED)),
        ]
