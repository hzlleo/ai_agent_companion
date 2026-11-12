"""OpenAI 兼容 chat/completions 异步客户端——llm 域传输层。

锁定设计决策：**不引入 openai SDK**——主 LLM 为远程 OpenAI 兼容端点
（vLLM 0.32+ 等自持推理盒），本客户端按协议手写；HTTP 出站一律经
注入的共享 ``httpx.AsyncClient``（pools.PoolManager 下发——硬规则：
无私建连接）。

职责边界：

    - 非流式 ``chat()``：POST /chat/completions → 归一
      :class:`ChatResponseModel`；
    - 流式 ``stream()``：SSE 行解析 → 异步迭代产出
      :class:`ChatStreamChunkModel`（末块 usage 经 ``stream_options.
      include_usage`` 声明回传）；
    - 重试（tenacity）：仅覆盖**幂等安全失败集**——连接错误 / 5xx /
      408 / 429（429 尊重 Retry-After 头，等待封顶可配）；流式请求仅
      「发起 → 2xx 确认」阶段可重试（首块产出后重放会重复消费，不重试）；
    - 超时：构造注入 ``request_timeout_seconds``（网关侧来自
      ``settings.pools.llm_request_timeout_seconds``）——缺省回落共享
      client 自身超时；
    - 错误映射：全部 HTTP / 状态 / 超时 / 流失败映射 ``BusinessException``
      （复用既有 LLM_* 错误码——本 chunk 零新增）。

错误码映射表：

    ============================  ======================  ==================
    失败类别                       错误码                  可重试
    ============================  ======================  ==================
    连接错误（ConnectError /       LLM_CALL_FAILED          是
    ConnectTimeout）
    HTTP 5xx / 408 / 429           LLM_CALL_FAILED          是（429 尊重
                                                        Retry-After）
    HTTP 401 / 403（鉴权失败）     LLM_API_KEY_MISSING      否
    其余 HTTP 4xx                  LLM_CALL_FAILED          否
    读 / 写 / 池超时               LLM_REQUEST_FAILED       否
    响应 JSON 非法 / choices 空    LLM_CALL_FAILED          否
    SSE data 块非法                LLM_CALL_FAILED          否
    ============================  ======================  ==================
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)
from tenacity.wait import wait_base, wait_exponential

from packages.harness.config.llm_wire import LlmClientConfigModel
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatRequestParams,
    ChatResponseModel,
    ChatStreamChunkModel,
    TokenUsageModel,
    WireChatCompletionResponse,
    WireStreamChunk,
    WireTokenUsage,
    build_chat_request_body,
)

__all__ = ["LlmHttpClient", "LlmRetryableError", "parse_retry_after_seconds"]


#: chat/completions 端点路径（拼接在条目 base_url 之后）。
_CHAT_COMPLETIONS_PATH = "/chat/completions"

#: SSE data 行前缀（OpenAI 兼容流式协议）。
_SSE_DATA_PREFIX = "data:"

#: SSE 流终止标记（data 块载荷）。
_SSE_DONE_MARKER = "[DONE]"

#: 响应体片段截断长度（错误 details 携带量控制）。
_BODY_SNIPPET_LIMIT = 500


class LlmRetryableError(BusinessException):
    """可重试 LLM 传输错误（连接失败 / 5xx / 408 / 429）。

    tenacity 重试谓词按本类型判定；重试耗尽后原样上抛（reraise 语义）。
    网关熔断器对本类失败记 ``record_failure``（端点健康信号）。

    Attributes:
        retry_after_seconds: 429 响应 Retry-After 头解析结果（秒；
            None = 端点未提供）。等待策略优先采用，回落指数退避。
    """

    def __init__(
        self,
        code: str = "LLM_CALL_FAILED",
        details: dict[str, Any] | None = None,
        message: str | None = None,
        headers: dict[str, str] | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """初始化可重试传输错误。

        Args:
            code: 错误码（BusinessException 契约）。
            details: 附加详情。
            message: 面向用户的错误描述。
            headers: 随响应透传的 HTTP 头（429 的 Retry-After 等）。
            retry_after_seconds: Retry-After 解析秒数（None = 未提供）。
        """
        super().__init__(code=code, details=details, message=message, headers=headers)
        self.retry_after_seconds = retry_after_seconds


def parse_retry_after_seconds(raw: str | None) -> float | None:
    """解析 Retry-After 头（秒数或 HTTP-date 形态）。

    Args:
        raw: 头原始值（None / 空 / 无法解析 → None）。

    Returns:
        等待秒数（非负；HTTP-date 换算为距当前的剩余秒数，已过期为 0）；
        无法解析返回 None。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        # 无时区标注的 HTTP-date 语义不确定——放弃解析（不猜测时区）
        return None
    remaining = (target - datetime.now(tz=UTC)).total_seconds()
    return max(0.0, remaining)


class _WaitRetryAfterFallback(wait_base):
    """等待策略——优先 Retry-After（异常携带），回落指数退避。"""

    def __init__(self, fallback: wait_exponential) -> None:
        """初始化策略。

        Args:
            fallback: 指数退避回落策略（Retry-After 缺失时生效）。
        """
        self._fallback = fallback

    def __call__(self, retry_state: RetryCallState) -> float:
        """计算下一次重试前等待秒数。

        Args:
            retry_state: tenacity 重试状态（outcome 携带上一轮异常）。

        Returns:
            等待秒数（Retry-After 优先；否则指数退避值）。
        """
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, LlmRetryableError) and exc.retry_after_seconds is not None:
                return exc.retry_after_seconds
        return self._fallback(retry_state)


def _map_wire_usage(usage: WireTokenUsage) -> TokenUsageModel:
    """wire 用量 → 域内用量归一。

    Args:
        usage: wire 用量形态。

    Returns:
        :class:`TokenUsageModel` 实例。
    """
    return TokenUsageModel(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


class LlmHttpClient:
    """OpenAI 兼容端点客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源，可按调用瞬时构造（网关按条目逐调用构建，随 wire
    配置热重建自然生效）。

    Attributes:
        wire: wire 配置（base_url / api_key / model 等；config 域下发）。
    """

    def __init__(
        self,
        wire: LlmClientConfigModel,
        http_client: httpx.AsyncClient,
        *,
        request_timeout_seconds: float | None = None,
        retry_attempts: int = 3,
        retry_wait_seconds: float = 0.5,
        retry_after_max_seconds: float = 30.0,
    ) -> None:
        """初始化客户端。

        Args:
            wire: wire 配置（config.llm_wire 构建产物）。
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            request_timeout_seconds: 单请求超时秒数（None = 用共享 client
                自身超时配置）。
            retry_attempts: 重试总次数上限（含首次；下限 1 = 不重试）。
            retry_wait_seconds: 指数退避基数秒（Retry-After 缺失时生效）。
            retry_after_max_seconds: Retry-After 等待封顶秒数（端点声明
                超长冷却时按封顶执行——请求路径不悬挂）。
        """
        self._wire = wire
        self._http = http_client
        self._request_timeout_seconds = request_timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._retry_wait_seconds = retry_wait_seconds
        self._retry_after_max_seconds = retry_after_max_seconds

    @property
    def model(self) -> str:
        """请求模型名（wire 配置）。"""
        return self._wire.model

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
    ) -> ChatResponseModel:
        """非流式聊天补全（POST /chat/completions）。

        Args:
            messages: 聊天消息序列。
            params: 采样参数（None = 端点默认）。

        Returns:
            归一响应（含用量与时延）。

        Raises:
            BusinessException: 连接 / 状态 / 超时 / 解析失败（映射表见
                模块 docstring；可重试失败在重试耗尽后上抛）。
        """
        started_ms = now_ms()
        body = build_chat_request_body(self._wire.model, list(messages), params, stream=False)
        response = await self._post_with_retry(body)
        wire = self._parse_response_json(response)
        latency_ms = now_ms() - started_ms
        return self._to_chat_response(wire, latency_ms=latency_ms)

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """流式聊天补全（SSE 增量迭代）。

        重试仅覆盖「发起请求 → 2xx 确认」阶段；首块产出后的流中断
        **不重试**（重放会导致消费方收到重复增量——非幂等）。

        Args:
            messages: 聊天消息序列。
            params: 采样参数（None = 端点默认）。

        Yields:
            归一增量块（content / finish_reason / 末块 usage）。

        Raises:
            BusinessException: 发起阶段失败（可重试集同 ``chat``）；流中段
                SSE data 块非法（``LLM_CALL_FAILED``，不重试）。
        """
        body = build_chat_request_body(self._wire.model, list(messages), params, stream=True)
        response = await self._open_stream_with_retry(body)
        try:
            async for line in response.aiter_lines():
                payload = self._extract_sse_payload(line)
                if payload is None:
                    continue
                if payload == _SSE_DONE_MARKER:
                    break
                yield self._parse_stream_chunk(payload)
        except BaseException:
            await response.aclose()
            raise
        await response.aclose()

    # ------------------------------------------------------------------
    # 内部——请求与重试
    # ------------------------------------------------------------------

    def _retrying(self) -> AsyncRetrying:
        """构建 tenacity 异步重试器（幂等安全失败集 + 有界次数）。"""
        return AsyncRetrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=_WaitRetryAfterFallback(
                wait_exponential(
                    multiplier=self._retry_wait_seconds,
                    max=max(1.0, self._retry_wait_seconds * 8),
                )
            ),
            retry=retry_if_exception_type(LlmRetryableError),
            reraise=True,
        )

    def _endpoint_url(self) -> str:
        """拼接 chat/completions 端点 URL（base_url 去尾斜杠 + 路径）。"""
        return f"{self._wire.base_url.rstrip('/')}{_CHAT_COMPLETIONS_PATH}"

    def _headers(self) -> dict[str, str]:
        """构建请求头（Bearer 鉴权——免鉴权端点无 Authorization 头）。"""
        headers = {"Content-Type": "application/json"}
        if self._wire.api_key:
            headers["Authorization"] = f"Bearer {self._wire.api_key}"
        return headers

    def _request_timeout(self) -> httpx.Timeout:
        """单请求超时（未配置时取共享 client 的超时对象——语义等效透传默认）。"""
        if self._request_timeout_seconds is not None:
            return httpx.Timeout(self._request_timeout_seconds)
        return self._http.timeout

    async def _post_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        """发起非流式请求（可重试阶段：连接 + 状态码确认）。

        Args:
            body: 请求体。

        Returns:
            2xx 响应（连接 / 5xx / 429 失败经 tenacity 有界重试）。

        Raises:
            BusinessException: 重试耗尽的可重试失败 / 不可重试失败
                （映射表见模块 docstring）。
        """
        async for attempt in self._retrying():
            with attempt:
                try:
                    response = await self._http.post(
                        self._endpoint_url(),
                        json=body,
                        headers=self._headers(),
                        timeout=self._request_timeout(),
                    )
                except httpx.HTTPError as exc:
                    raise self._map_transport_exception(exc) from exc
                self._raise_for_llm_status(response)
                return response
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="重试循环意外退出（不可达路径）",
        )

    async def _open_stream_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        """发起流式请求并确认 2xx（可重试阶段；首块产出前）。

        Args:
            body: 请求体（stream=True 形态）。

        Returns:
            已进入流式态的 2xx 响应（调用方迭代后 ``aclose()`` 关闭）。

        Raises:
            BusinessException: 发起阶段失败（重试耗尽 / 不可重试）。
        """
        async for attempt in self._retrying():
            with attempt:
                request = self._http.build_request(
                    "POST",
                    self._endpoint_url(),
                    json=body,
                    headers=self._headers(),
                    timeout=self._request_timeout(),
                )
                try:
                    response = await self._http.send(request, stream=True)
                except httpx.HTTPError as exc:
                    raise self._map_transport_exception(exc) from exc
                try:
                    if not response.is_success:
                        await response.aread()
                        self._raise_for_llm_status(response)
                except BaseException:
                    await response.aclose()
                    raise
                return response
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="重试循环意外退出（不可达路径）",
        )

    # ------------------------------------------------------------------
    # 内部——异常映射
    # ------------------------------------------------------------------

    def _map_transport_exception(self, exc: httpx.HTTPError) -> BusinessException:
        """httpx 传输异常 → 业务异常（连接可重试 / 超时与传输失败不重试）。

        Args:
            exc: httpx 传输异常（ConnectError / ConnectTimeout / 其余
                TimeoutException / 其他 HTTPError）。

        Returns:
            映射后的业务异常（连接类为 :class:`LlmRetryableError`）。
        """
        if isinstance(exc, httpx.ConnectTimeout):
            return LlmRetryableError(
                code=ErrorCode.LLM_CALL_FAILED,
                details={"reason": "连接 LLM 端点超时", "model": self._wire.model},
            )
        if isinstance(exc, httpx.ConnectError):
            return LlmRetryableError(
                code=ErrorCode.LLM_CALL_FAILED,
                details={"reason": "无法建立到 LLM 端点的连接", "model": self._wire.model},
            )
        if isinstance(exc, httpx.TimeoutException):
            return BusinessException(
                code=ErrorCode.LLM_REQUEST_FAILED,
                details={
                    "reason": "LLM 请求超时",
                    "model": self._wire.model,
                    "timeout_seconds": self._request_timeout_seconds,
                    "timeout_type": type(exc).__name__,
                },
            )
        return BusinessException(
            code=ErrorCode.LLM_CALL_FAILED,
            details={"reason": "LLM 传输失败", "model": self._wire.model, "error": str(exc)},
        )

    def _raise_for_llm_status(self, response: httpx.Response) -> None:
        """HTTP 状态 → 业务异常映射（调用前响应体须已可读）。

        Args:
            response: HTTP 响应（2xx 直接返回；429 携带 Retry-After 解析）。

        Raises:
            LlmRetryableError: 5xx / 408 / 429（幂等安全可重试集）。
            BusinessException: 401 / 403 → ``LLM_API_KEY_MISSING``；其余
                4xx → ``LLM_CALL_FAILED``。
        """
        if response.is_success:
            return
        status = response.status_code
        retry_after = parse_retry_after_seconds(response.headers.get("Retry-After"))
        if retry_after is not None:
            retry_after = min(retry_after, self._retry_after_max_seconds)
        details: dict[str, Any] = {
            "reason": "LLM 端点返回非成功状态",
            "status_code": status,
            "model": self._wire.model,
            "body_snippet": response.text[:_BODY_SNIPPET_LIMIT],
        }
        headers: dict[str, str] | None = None
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after
            headers = {"Retry-After": str(int(retry_after))}
        if status == 429:
            logger.warning(
                "LLM 端点限流（429），Retry-After 解析 {} 秒: model={}",
                retry_after,
                self._wire.model,
            )
        if status >= 500 or status in (408, 429):
            raise LlmRetryableError(
                code=ErrorCode.LLM_CALL_FAILED,
                details=details,
                headers=headers,
                retry_after_seconds=retry_after,
            )
        if status in (401, 403):
            raise BusinessException(
                code=ErrorCode.LLM_API_KEY_MISSING,
                details=details,
            )
        raise BusinessException(code=ErrorCode.LLM_CALL_FAILED, details=details)

    # ------------------------------------------------------------------
    # 内部——响应 / SSE 解析
    # ------------------------------------------------------------------

    def _parse_response_json(self, response: httpx.Response) -> WireChatCompletionResponse:
        """非流式响应体 → wire 响应（JSON 校验）。

        Args:
            response: 2xx 响应。

        Returns:
            wire 响应实例。

        Raises:
            BusinessException: 响应体不是合法的 OpenAI 兼容 JSON
                （``LLM_CALL_FAILED``）。
        """
        try:
            return WireChatCompletionResponse.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                details={
                    "reason": "LLM 响应不是合法的 OpenAI 兼容 JSON",
                    "model": self._wire.model,
                    "parse_error": str(exc)[:300],
                },
            ) from exc

    def _to_chat_response(
        self, wire: WireChatCompletionResponse, *, latency_ms: int
    ) -> ChatResponseModel:
        """wire 响应 → 域内归一响应（choices 空即非法）。

        Args:
            wire: wire 响应。
            latency_ms: 端到端时延毫秒。

        Returns:
            归一响应（首个 choice）。

        Raises:
            BusinessException: choices 为空（``LLM_CALL_FAILED``）。
        """
        if not wire.choices:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                details={
                    "reason": "LLM 响应 choices 为空",
                    "model": self._wire.model,
                    "body_snippet": wire.model_dump_json()[:_BODY_SNIPPET_LIMIT],
                },
            )
        choice = wire.choices[0]
        return ChatResponseModel(
            content=choice.message.content,
            finish_reason=choice.finish_reason,
            model=wire.model or self._wire.model,
            usage=_map_wire_usage(wire.usage) if wire.usage is not None else None,
            latency_ms=latency_ms,
        )

    def _extract_sse_payload(self, line: str) -> str | None:
        """提取 SSE data 载荷（注释 / 空行 / 非数据字段行返回 None）。

        Args:
            line: SSE 单行（已去换行）。

        Returns:
            data 载荷字符串；无关行返回 None。
        """
        text = line.strip()
        if not text or text.startswith(":") or not text.startswith(_SSE_DATA_PREFIX):
            return None
        return text[len(_SSE_DATA_PREFIX) :].strip()

    def _parse_stream_chunk(self, payload: str) -> ChatStreamChunkModel:
        """SSE data 载荷 → 归一增量块。

        Args:
            payload: data 载荷字符串（JSON）。

        Returns:
            归一增量块（choices 空且携带 usage 的 usage-only 末块归一为
            空内容 + usage 块）。

        Raises:
            BusinessException: 载荷不是合法 JSON / 不符合 wire 块形态
                （``LLM_CALL_FAILED``，不重试）。
        """
        try:
            wire = WireStreamChunk.model_validate_json(payload)
        except (ValidationError, ValueError) as exc:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                details={
                    "reason": "LLM SSE data 块不是合法 JSON",
                    "model": self._wire.model,
                    "parse_error": str(exc)[:300],
                    "line_snippet": payload[:_BODY_SNIPPET_LIMIT],
                },
            ) from exc
        if not wire.choices:
            # usage-only 末块（include_usage 声明后部分端点 choices 为空）
            if wire.usage is not None:
                return ChatStreamChunkModel(usage=_map_wire_usage(wire.usage))
            return ChatStreamChunkModel()
        choice = wire.choices[0]
        return ChatStreamChunkModel(
            content=choice.delta.content or "",
            finish_reason=choice.finish_reason,
            usage=_map_wire_usage(wire.usage) if wire.usage is not None else None,
        )
