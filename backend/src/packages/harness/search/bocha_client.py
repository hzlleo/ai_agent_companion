"""search 域 Bocha 客户端——博查 Web Search API（共享 httpx client 注入）。

**wire 契约（本客户端持有文档——按官方开放平台 open.bochaai.com 首页
curl 示例与任务 DTO 研究锁定）**::

    POST {base_url}/v1/web-search           # base_url 缺省 https://api.bochaai.com
    Authorization: Bearer <SEARCH_BOCHA_API_KEY 解析值>
    Content-Type: application/json
    {
        "query": str,        # 搜索词（必填）
        "freshness": str,    # 时间范围过滤——本客户端恒 "noLimit"（不限）
        "summary": false,    # 关闭 AI 摘要（省成本 / 低延迟——仅取 snippet；
                             #   字段名按官方 curl 示例为 summary，bool 型）
        "count": int         # 返回条数（1-20，条目 max_results / 请求覆盖）
    }

    200 →
    {
        "code": 200,                      # 业务码（非 200 → LLM_CALL_FAILED）
        "log_id": str, "msg": null,
        "data": {                          # 兼容未包裹形态（官方首页示例
            "webPages": {                  # 直接给 SearchResponse——本客户端
                "value": [                 # 对 data 缺失时回退整体载荷）
                    {
                        "name": str,       # → title
                        "url": str,        # → url
                        "snippet": str,    # → snippet（缺失时回退 summary 键）
                        "dateLastCrawled": str   # 抓取时间（本工程不消费）
                    }
                ]
            }
        }
    }

    其余 HTTP 状态 / 传输异常 / JSON 非法 / 结构破坏 → LLM_CALL_FAILED
    （503——远程搜索服务与 LLM 端点同属「远程依赖不可用」类别，复用
    既有码表，core/errors.py 不动）。

**形态不确定性披露（do-not-guess-silently）**：官方文档站为飞书 wiki
（纯 fetch 不可渲染）；上表请求字段名（query / freshness / summary /
count）与响应包裹（code/data/webPages.value[].name/url/snippet）按
open.bochaai.com 首页 curl 示例 + 任务方给定的响应形状交叉锁定——
dateLastCrawled 键按任务方形状实现但不消费；若部署实测响应为首页
示例的未包裹形态（webPages 直接在顶层 + datePublished 键），本客户端
的 data 回退 + snippet→summary 回退已兼容。偏差以 MockTransport 测试
锁定的本契约为准，部署侧实测后如需修正属契约变更（同步本 docstring
+ 测试 + docs/backend/search.md）。
"""

from __future__ import annotations

from typing import Any, Final, Literal

import httpx
from loguru import logger

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.search.search_models import (
    SearchResponseModel,
    SearchResultModel,
    SearchUsageEmitter,
    SearchUsageEvent,
    truncate_snippet,
)

__all__ = ["BOCHA_WEB_SEARCH_PATH", "BochaWebSearchClient"]

#: Bocha Web Search API 路径（拼接在 base_url 后）。
BOCHA_WEB_SEARCH_PATH: Final[str] = "/v1/web-search"

#: Bocha 搜索的时间范围过滤值（不限——锁定 noLimit）。
BOCHA_FRESHNESS_NO_LIMIT: Final[str] = "noLimit"

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class BochaWebSearchClient:
    """博查 Web Search 客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源。请求超时从构造参数读取（条目 ``timeout_seconds``
    经服务层传入）。每次尝试经 ``usage_emitter`` 恰发一事件（成功 /
    失败均发——发射异常仅告警不阻断）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = "https://api.bochaai.com",
        request_timeout_seconds: float = 15.0,
        provider_name: str = "bocha",
        usage_emitter: SearchUsageEmitter | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            api_key: 已解析的 API Key 明文（空 = 免鉴权形态——服务层
                在调用前已对空密钥 fail loud，此处不再校验）。
            base_url: API 基础 URL（尾斜杠容错；缺省官方端点）。
            request_timeout_seconds: 单次搜索请求超时秒数。
            provider_name: provider 条目名（结果标记 + 日志面）。
            usage_emitter: 用量发射钩子（None = 不发射——纯传输测试面）。
        """
        self._http = http_client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_seconds)
        self._provider_name = provider_name
        self._usage_emitter = usage_emitter

    async def search(self, query: str, *, max_results: int = 8) -> SearchResponseModel:
        """执行网页搜索（POST /v1/web-search → 归一结果）。

        Args:
            query: 搜索词（1-256 字符——服务层已校验）。
            max_results: 返回条数（1-20）。

        Returns:
            归一响应（results 按端点返回序；provider_used = 构造期条目名）。

        Raises:
            BusinessException: 传输异常 / 非 200 / JSON 非法 / 业务码非
                200 / 结构破坏（``LLM_CALL_FAILED``——映射表见模块
                docstring）。
        """
        started_ms = now_ms()
        try:
            response = await self._http.post(
                f"{self._base_url}{BOCHA_WEB_SEARCH_PATH}",
                headers=self._auth_headers(),
                json={
                    "query": query,
                    "freshness": BOCHA_FRESHNESS_NO_LIMIT,
                    "summary": False,
                    "count": max_results,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed(
                f"Bocha 搜索传输失败: {type(exc).__name__}: {exc}", status_code=None
            ) from exc
        if response.status_code != 200:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed(
                f"Bocha 搜索端点返回非 200: {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed("Bocha 搜索响应非 JSON", status_code=200) from exc
        results = self._map_results(payload)
        latency_ms = now_ms() - started_ms
        await self._emit_usage("success", started_ms, None, latency_ms=latency_ms)
        return SearchResponseModel(
            results=results,
            provider_used=self._provider_name,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """构造鉴权头（Bearer——密钥仅进入请求头，永不落日志 / 响应面）。"""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _map_results(self, payload: Any) -> list[SearchResultModel]:
        """Bocha 响应载荷 → 归一结果列表（data 包裹与未包裹双形态兼容）。

        Args:
            payload: 已解析的 JSON 载荷（dict 形态——非法结构抛
                ``LLM_CALL_FAILED``）。

        Returns:
            归一结果列表（空结果为合法态——count 0）。

        Raises:
            BusinessException: 业务码非 200 / 结构破坏
                （``LLM_CALL_FAILED``）。
        """
        if not isinstance(payload, dict):
            raise self._call_failed("Bocha 搜索响应结构非 object", status_code=200)
        if "code" in payload and payload.get("code") != 200:
            raise self._call_failed(
                f"Bocha 搜索业务码非 200: {payload.get('code')}",
                status_code=200,
                body=str(payload.get("msg") or ""),
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        web_pages = data.get("webPages") if isinstance(data, dict) else None
        raw_values = web_pages.get("value") if isinstance(web_pages, dict) else None
        if raw_values is None:
            raise self._call_failed("Bocha 搜索响应缺 webPages.value", status_code=200)
        if not isinstance(raw_values, list):
            raise self._call_failed("Bocha 搜索响应 webPages.value 非列表", status_code=200)
        results: list[SearchResultModel] = []
        for item in raw_values:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or item.get("summary") or "")
            results.append(
                SearchResultModel(
                    title=str(item.get("name") or ""),
                    url=str(item.get("url") or ""),
                    snippet=truncate_snippet(snippet),
                    source_provider=self._provider_name,
                )
            )
        return results

    def _call_failed(
        self, reason: str, *, status_code: int | None, body: str | None = None
    ) -> BusinessException:
        """构造传输失败业务异常（details 携带状态码与响应体片段）。

        Args:
            reason: 人读失败原因（一行）。
            status_code: HTTP 状态码（传输异常为 None）。
            body: 响应体片段（截断到 300 字符；无则省略）。

        Returns:
            ``LLM_CALL_FAILED`` 业务异常实例。
        """
        details: dict[str, Any] = {"service": "bocha", "provider": self._provider_name}
        if status_code is not None:
            details["status_code"] = status_code
        if body:
            details["body_snippet"] = body[:_BODY_SNIPPET_LIMIT]
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            message=f"{reason}（provider={self._provider_name}）",
            details=details,
        )

    async def _emit_usage(
        self,
        status: Literal["success", "error"],
        started_ms: int,
        error_code: str | None,
        *,
        latency_ms: int = 0,
    ) -> None:
        """发射单次尝试用量事件（钩子未注入时 no-op；异常仅告警）。

        Args:
            status: 尝试结果（success / error）。
            started_ms: 尝试起始 epoch 毫秒（错误路径时延按此刻计）。
            error_code: 失败错误码（成功为 None）。
            latency_ms: 已知时延（错误路径 0 → 按起始毫秒差补齐）。
        """
        if self._usage_emitter is None:
            return
        event = SearchUsageEvent(
            status=status,
            latency_ms=latency_ms if latency_ms else now_ms() - started_ms,
            error_code=error_code,
        )
        try:
            await self._usage_emitter(event)
        except Exception:
            logger.opt(exception=True).warning(
                "搜索用量事件发射失败（不阻断主调用）: provider={}", self._provider_name
            )
