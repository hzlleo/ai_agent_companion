"""search 域 Zhipu 客户端——智谱 Web Search API（共享 httpx client 注入）。

**wire 契约（本客户端持有文档——按智谱开放平台官方 OpenAPI 规格锁定；
来源 docs.bigmodel.cn ``post /paas/v4/web_search`` OpenAPI 组件 schema）**::

    POST {base_url}/api/paas/v4/web_search   # base_url 缺省 https://open.bigmodel.cn
    Authorization: Bearer <SEARCH_ZHIPU_API_KEY 解析值>
    Content-Type: application/json
    {
        "search_query": str,     # 搜索词（必填；官方 schema maxLength 70——
                                 #   超长时本客户端截断到 70，见下方披露）
        "search_engine": str,    # 引擎码（必填；条目 model 字段——缺省
                                 #   search_std（兜底档）；可配 search_pro /
                                 #   search_pro_sogou / search_pro_quark）
        "search_intent": false,  # 意图识别开关（必填——恒 false：直查）
        "count": int,            # 返回条数（1-50；条目 max_results 1-20）
        "request_id": str        # 请求标识（uuid4 hex——官方建议 UUID，
                                 #   长度 6-64）
    }

    200 →
    {
        "id": str, "created": int, "request_id": str,
        "search_intent": [...],            # 意图识别结果（search_intent=false
                                           #   时为 SEARCH_ALWAYS 档——不消费）
        "search_result": [                 # 搜索结果（空列表为合法态）
            {
                "title": str,              # → title
                "link": str,               # → url
                "content": str,            # → snippet
                "media": str, "icon": str, # 站名 / 站图标（不消费）
                "refer": str, "publish_date": str   # 角标 / 发布时间（不消费）
            }
        ]
    }

    其余 HTTP 状态 / 传输异常 / JSON 非法 → LLM_CALL_FAILED（503——远程
    搜索依赖不可用类别，复用既有码表）；官方错误体形态
    ``{"error": {"code": str, "message": str}}`` 原样进 details。

**路径勘误（相对任务书的差异披露）**：任务书建议 ``/api/paas/v4/tools``
（agent 工具循环端点——响应嵌于 ``choices[].message``）；实际按官方
OpenAPI 规格实现 ``/api/paas/v4/web_search`` 专用端点——任务书描述的
响应形状 ``{"search_result": [{title, link, content, media, icon}]}``
正是该专用端点的响应（两者在任务书内被混写；以官方规格 + 响应形状
双交叉证据取 web_search 端点）。

**query 长度截断披露**：工具面 query 上限 256 字符（本工程统一面），
官方 schema ``search_query`` 上限 70 字符——超长时截断到 70 字符发送
（兜底 provider 的降级语义：宁可截断查询也完成兜底，不 fail）。
"""

from __future__ import annotations

import uuid
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

__all__ = ["ZHIPU_WEB_SEARCH_PATH", "ZhipuWebSearchClient"]

#: Zhipu Web Search API 路径（拼接在 base_url 后）。
ZHIPU_WEB_SEARCH_PATH: Final[str] = "/api/paas/v4/web_search"

#: 官方 ``search_query`` 长度上限（schema maxLength 70——超长截断）。
ZHIPU_QUERY_MAX_CHARS: Final[int] = 70

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class ZhipuWebSearchClient:
    """智谱 Web Search 客户端（共享 ``httpx.AsyncClient`` 注入）。

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
        base_url: str = "https://open.bigmodel.cn",
        search_engine: str = "search_std",
        request_timeout_seconds: float = 15.0,
        provider_name: str = "zhipu",
        usage_emitter: SearchUsageEmitter | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            api_key: 已解析的 API Key 明文（服务层在调用前已对空密钥
                fail loud，此处不再校验）。
            base_url: API 基础 URL（尾斜杠容错；缺省官方端点）。
            search_engine: 搜索引擎码（条目 model 字段；缺省
                ``search_std`` 兜底档）。
            request_timeout_seconds: 单次搜索请求超时秒数。
            provider_name: provider 条目名（结果标记 + 日志面）。
            usage_emitter: 用量发射钩子（None = 不发射——纯传输测试面）。
        """
        self._http = http_client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._search_engine = search_engine
        self._timeout = httpx.Timeout(request_timeout_seconds)
        self._provider_name = provider_name
        self._usage_emitter = usage_emitter

    async def search(self, query: str, *, max_results: int = 8) -> SearchResponseModel:
        """执行网页搜索（POST /api/paas/v4/web_search → 归一结果）。

        Args:
            query: 搜索词（超 70 字符截断——见模块 docstring 披露）。
            max_results: 返回条数（1-20）。

        Returns:
            归一响应（results 按端点返回序；provider_used = 构造期条目名）。

        Raises:
            BusinessException: 传输异常 / 非 200 / JSON 非法 / 结构破坏
                （``LLM_CALL_FAILED``——映射表见模块 docstring）。
        """
        started_ms = now_ms()
        try:
            response = await self._http.post(
                f"{self._base_url}{ZHIPU_WEB_SEARCH_PATH}",
                headers=self._auth_headers(),
                json={
                    "search_query": query[:ZHIPU_QUERY_MAX_CHARS],
                    "search_engine": self._search_engine,
                    "search_intent": False,
                    "count": max_results,
                    "request_id": uuid.uuid4().hex,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed(
                f"Zhipu 搜索传输失败: {type(exc).__name__}: {exc}", status_code=None
            ) from exc
        if response.status_code != 200:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed(
                f"Zhipu 搜索端点返回非 200: {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            await self._emit_usage("error", started_ms, str(ErrorCode.LLM_CALL_FAILED))
            raise self._call_failed("Zhipu 搜索响应非 JSON", status_code=200) from exc
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
        """Zhipu 响应载荷 → 归一结果列表。

        Args:
            payload: 已解析的 JSON 载荷（dict 形态——非法结构抛
                ``LLM_CALL_FAILED``）。

        Returns:
            归一结果列表（``search_result`` 缺失视为空结果——合法态）。

        Raises:
            BusinessException: 结构破坏（``LLM_CALL_FAILED``）。
        """
        if not isinstance(payload, dict):
            raise self._call_failed("Zhipu 搜索响应结构非 object", status_code=200)
        raw_results = payload.get("search_result")
        if raw_results is None:
            return []
        if not isinstance(raw_results, list):
            raise self._call_failed("Zhipu 搜索响应 search_result 非列表", status_code=200)
        results: list[SearchResultModel] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            results.append(
                SearchResultModel(
                    title=str(item.get("title") or ""),
                    url=str(item.get("link") or ""),
                    snippet=truncate_snippet(str(item.get("content") or "")),
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
        details: dict[str, Any] = {
            "service": "zhipu",
            "provider": self._provider_name,
            "search_engine": self._search_engine,
        }
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
