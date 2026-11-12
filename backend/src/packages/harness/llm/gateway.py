"""LLM 网关——角色路由 / 池门控 / 用量埋点的模型调用门面。

调用链（单一真相，文档化于 docs/backend/llm.md）::

    chat_by_role(role, messages, params)
        ↓ ① config.model_resolver.resolve(role)          —— 角色解析
        ↓ ② config.llm_wire.build_llm_wire_config(entry) —— wire 构建（密钥收口）
        ↓ ③ pools.entry_gate(entry)                       —— 熔断预检 + 信号量
        ↓ ④ llm.client.chat / stream                     —— 传输（tenacity 重试）
        ↓ ⑤ usage 发射（成功 / 失败路径均落账）            —— 注入式记录器

设计决策：

    - **llm 不硬依赖 usage 域**：网关持有注入的异步记录器
      (:data:`UsageRecorder`)；默认接线经 :func:`wire_default_usage_recorder`
      （P0-6 app 装配调用，函数体内惰性 import usage 域——模块导入期
      零耦合）；
    - **client 按调用瞬时构建**（零连接资源）：wire 配置随 config 热重建
      自然生效，无缓存失效问题；
    - **熔断结果上报**：client 调用成功 → ``record_success``；失败 →
      ``record_failure``（pools llm_pool 文档化的网关调用点契约）；
    - **用量发射恒不阻断主调用**：记录器异常仅告警（用量是观测面，
      不是业务面）；
    - **上下文轴**：persona_id / request_id 显式参数优先，缺省读
      core.context 上下文变量（请求中间件已绑定则自动携带）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from loguru import logger

from packages.harness.config.llm_wire import build_llm_wire_config
from packages.harness.config.model_entry import LlmModelEntry, ModelRole
from packages.harness.config.model_resolver import ModelResolver, get_model_resolver
from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.context import persona_id_var, request_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.client import LlmHttpClient
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatRequestParams,
    ChatResponseModel,
    ChatStreamChunkModel,
    LlmUsageEvent,
    TokenUsageModel,
)
from packages.harness.pools.llm_pool import LlmEntryPool
from packages.harness.pools.manager import PoolManager, get_pool_manager

__all__ = ["LlmGateway", "UsageRecorder", "wire_default_usage_recorder"]


#: 用量记录器契约——网关 → usage 域的注入式异步回调。
UsageRecorder = Callable[[LlmUsageEvent], Awaitable[None]]


def _role_value(role: ModelRole | str) -> str:
    """角色归一为字符串值（ModelRole 成员 / 角色名兼容）。"""
    return role.value if isinstance(role, ModelRole) else str(role)


class LlmGateway:
    """角色路由 LLM 网关（门面——编排 config / pools / client / usage）。

    构造注入（测试 / 装配友好）：resolver / pool_manager / settings /
    usage_recorder 均可显式注入；缺省分别回落全局单例（resolver 与
    pool_manager 为调用时读取——app lifespan 装配后生效）。
    """

    def __init__(
        self,
        *,
        resolver: ModelResolver | None = None,
        pool_manager: PoolManager | None = None,
        settings: Settings | None = None,
        usage_recorder: UsageRecorder | None = None,
        retry_attempts: int = 3,
        retry_wait_seconds: float = 0.5,
        retry_after_max_seconds: float = 30.0,
    ) -> None:
        """初始化网关。

        Args:
            resolver: 模型解析器（None = 全局单例，调用时读取）。
            pool_manager: 池管理器（None = 全局单例，调用时读取）。
            settings: Settings（None = config 全局单例；仅用其 llm 兜底
                密钥与 llm_request_timeout_seconds）。
            usage_recorder: 用量记录器（None = 未接线——零记录，P0-6
                装配时经 wire_default_usage_recorder 绑定）。
            retry_attempts: 传输重试总次数上限（含首次）。
            retry_wait_seconds: 指数退避基数秒。
            retry_after_max_seconds: Retry-After 等待封顶秒数。
        """
        self._resolver = resolver
        self._pool_manager = pool_manager
        self._settings = settings
        self._usage_recorder = usage_recorder
        self._retry_attempts = retry_attempts
        self._retry_wait_seconds = retry_wait_seconds
        self._retry_after_max_seconds = retry_after_max_seconds

    # ------------------------------------------------------------------
    # 角色门面（chat / stream）
    # ------------------------------------------------------------------

    async def chat_by_role(
        self,
        role: ModelRole | str,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """按角色非流式调用（角色解析 → 条目直连链）。

        Args:
            role: 模型角色（chat / duty / dream / look_at / embed）。
            messages: 聊天消息序列。
            params: 采样参数（None = 端点默认）。
            persona_id: 人格 ID（None = 读请求上下文；无人格上下文则
                用量记录 persona_id 为空——系统调用）。
            purpose: 用量用途覆盖（None = 角色名）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            归一响应。

        Raises:
            BusinessException: 角色未绑定（``MODEL_NOT_FOUND``）；条目
                熔断中（``LLM_REQUEST_FAILED``）；传输失败（见 client
                映射表）。
        """
        entry = self._resolve_role(role)
        return await self.chat_for_entry(
            entry,
            messages,
            params,
            persona_id=persona_id,
            purpose=purpose if purpose is not None else _role_value(role),
            request_id=request_id,
        )

    async def stream_by_role(
        self,
        role: ModelRole | str,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
        *,
        persona_id: str | None = None,
        purpose: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """按角色流式调用（异步生成器——角色解析在首个迭代步发生）。

        Args:
            role: 模型角色。
            messages: 聊天消息序列。
            params: 采样参数（None = 端点默认）。
            persona_id: 人格 ID（None = 读请求上下文）。
            purpose: 用量用途覆盖（None = 角色名）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Yields:
            归一增量块（末块携带 usage 时被网关聚合进用量记录）。

        Raises:
            BusinessException: 角色未绑定 / 熔断中 / 传输失败（首个迭代
                步抛出——异步生成器语义）。
        """
        entry = self._resolve_role(role)
        resolved_purpose = purpose if purpose is not None else _role_value(role)
        async for chunk in self._stream_for_entry(
            entry,
            messages,
            params,
            persona_id=persona_id,
            purpose=resolved_purpose,
            request_id=request_id,
        ):
            yield chunk

    # ------------------------------------------------------------------
    # 条目直连（look_at 回退链 / 显式条目调用）
    # ------------------------------------------------------------------

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
    ) -> ChatResponseModel:
        """按显式条目非流式调用（跳过角色解析；回退链消费面）。

        全链：wire 构建 → entry_gate（熔断 + 信号量）→ client.chat →
        用量发射（成功 / 失败均落账）。

        Args:
            entry: 模型条目（注册表条目或回退链条目）。
            messages: 聊天消息序列。
            params: 采样参数（None = 端点默认）。
            persona_id: 人格 ID（None = 读请求上下文）。
            purpose: 用量用途（chat / duty / dream / look_at / embed /
                tts / asr / tool；usage 域枚举校验）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            归一响应。

        Raises:
            BusinessException: 条目熔断中（``LLM_REQUEST_FAILED``，details
                含冷却剩余毫秒）；传输失败（见 client 映射表）。
        """
        resolved_persona = persona_id if persona_id is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        client = self._build_client(entry)
        entry_pool = self._require_entry_pool()
        breaker = entry_pool.get_entry_breaker(entry)
        started_ms = now_ms()
        try:
            async with entry_pool.entry_gate(entry):
                try:
                    response = await client.chat(messages, params)
                except BusinessException:
                    breaker.record_failure()
                    raise
                breaker.record_success()
            await self._emit_usage(
                entry=entry,
                purpose=purpose,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                status="success",
                usage=response.usage,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                error_code=None,
            )
            return response
        except BusinessException as exc:
            await self._emit_usage(
                entry=entry,
                purpose=purpose,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                status="error",
                usage=None,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                error_code=str(exc.code),
            )
            raise

    # ------------------------------------------------------------------
    # 内部——解析 / 装配
    # ------------------------------------------------------------------

    def _resolve_role(self, role: ModelRole | str) -> LlmModelEntry:
        """角色解析（缺省全局解析器——loader 热重建后自动生效）。

        Raises:
            BusinessException: 角色未绑定（``MODEL_NOT_FOUND``）。
        """
        resolver = self._resolver if self._resolver is not None else get_model_resolver()
        return resolver.resolve(role)

    def _require_pool_manager(self) -> PoolManager:
        """取池管理器（注入优先；缺省全局单例；未装配 fail loud）。"""
        manager = self._pool_manager if self._pool_manager is not None else get_pool_manager()
        if manager is None or not manager.started:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="公共池未初始化，请先在 lifespan startup 中调用 pool_manager.start()",
            )
        return manager

    def _require_entry_pool(self) -> LlmEntryPool:
        """取 LLM 条目池（信号量 + 熔断器注册表）。"""
        return self._require_pool_manager().get_llm_entry_pool()

    def _build_client(self, entry: LlmModelEntry) -> LlmHttpClient:
        """按条目瞬时构建传输 client（共享 HTTP 池 + wire 配置）。

        密钥兜底：条目声明 ``api_key_env`` 但变量未设置时，回落
        Settings.llm.api_key（env 面 LLM_API_KEY）；Settings 缺省用
        config 全局单例。
        """
        settings = self._settings if self._settings is not None else global_settings
        fallback_key = settings.llm.api_key.get_secret_value() or None
        wire = build_llm_wire_config(entry, fallback_api_key=fallback_key)
        timeout_seconds = settings.pools.llm_request_timeout_seconds
        return LlmHttpClient(
            wire,
            self._require_pool_manager().get_http_client(),
            request_timeout_seconds=timeout_seconds,
            retry_attempts=self._retry_attempts,
            retry_wait_seconds=self._retry_wait_seconds,
            retry_after_max_seconds=self._retry_after_max_seconds,
        )

    async def _stream_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None,
        *,
        persona_id: str | None,
        purpose: str,
        request_id: str | None,
    ) -> AsyncIterator[ChatStreamChunkModel]:
        """按条目流式调用（内部生成器——门控 / 熔断 / 用量聚合一体）。

        用量聚合：末块携带 usage（include_usage 声明）时以其为准；
        其余以零用量落账（调用计数 + 时延仍有观测价值）。消费方提前
        终止（GeneratorExit）不落账——无终态结果（文档化决策）。
        """
        resolved_persona = persona_id if persona_id is not None else persona_id_var.get()
        resolved_request_id = request_id if request_id is not None else request_id_var.get()
        client = self._build_client(entry)
        entry_pool = self._require_entry_pool()
        breaker = entry_pool.get_entry_breaker(entry)
        started_ms = now_ms()
        aggregated_usage: TokenUsageModel | None = None
        try:
            async with entry_pool.entry_gate(entry):
                try:
                    async for chunk in client.stream(messages, params):
                        if chunk.usage is not None:
                            aggregated_usage = chunk.usage
                        yield chunk
                except BusinessException:
                    breaker.record_failure()
                    raise
                breaker.record_success()
            await self._emit_usage(
                entry=entry,
                purpose=purpose,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                status="success",
                usage=aggregated_usage,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                error_code=None,
            )
        except BusinessException as exc:
            await self._emit_usage(
                entry=entry,
                purpose=purpose,
                persona_id=resolved_persona,
                request_id=resolved_request_id,
                status="error",
                usage=aggregated_usage,
                ts_ms=started_ms,
                latency_ms=now_ms() - started_ms,
                error_code=str(exc.code),
            )
            raise
        except GeneratorExit:
            logger.info(
                "流式调用被消费方提前终止，用量不落账: entry={}, purpose={}",
                entry.name,
                purpose,
            )
            raise

    # ------------------------------------------------------------------
    # 内部——用量发射
    # ------------------------------------------------------------------

    async def _emit_usage(
        self,
        *,
        entry: LlmModelEntry,
        purpose: str,
        persona_id: str | None,
        request_id: str | None,
        status: str,
        usage: TokenUsageModel | None,
        ts_ms: int,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        """发射用量事件（未接线时 no-op；记录器异常仅告警不阻断）。

        Args:
            entry: 模型条目。
            purpose: 调用用途。
            persona_id: 人格 ID（None = 系统调用）。
            request_id: 请求追踪 ID。
            status: 结果（success / error）。
            usage: 用量（None = 零用量落账——错误路径 / 端点未回传）。
            ts_ms: 调用发起时间（epoch 毫秒）。
            latency_ms: 端到端时延毫秒。
            error_code: 失败错误码（成功为 None）。
        """
        recorder = self._usage_recorder
        if recorder is None:
            return
        tokens = usage if usage is not None else TokenUsageModel()
        event = LlmUsageEvent(
            ts_ms=ts_ms,
            persona_id=persona_id,
            purpose=purpose,
            model_entry=entry.name,
            provider=entry.provider,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            total_tokens=tokens.total_tokens,
            latency_ms=latency_ms,
            status=status,
            request_id=request_id,
            error_code=error_code,
        )
        try:
            await recorder(event)
        except Exception:
            logger.opt(exception=True).warning(
                "用量记录发射失败（不阻断主调用）: entry={}, purpose={}, status={}",
                entry.name,
                purpose,
                status,
            )


def wire_default_usage_recorder() -> UsageRecorder:
    """装配默认用量记录器（绑定 usage 域服务——P0-6 app 装配调用）。

    llm 域不硬依赖 usage 域：本函数是唯一接线点，函数体内惰性 import
    （调用时才解析 usage 模块——llm 模块导入期零耦合）。装配形态::

        gateway = LlmGateway(usage_recorder=wire_default_usage_recorder())

    Returns:
        将 :class:`LlmUsageEvent` 映射为 ``UsageCreateDTO`` 落库的异步
        记录器。
    """
    from packages.harness.usage.dtos import UsageCreateDTO
    from packages.harness.usage.models import UsagePurpose, UsageStatus
    from packages.harness.usage.service import UsageService

    service = UsageService()

    async def _record(event: LlmUsageEvent) -> None:
        """事件 → usage 域创建 DTO（值域枚举化）→ 落库。"""
        await service.create(
            UsageCreateDTO(
                ts_ms=event.ts_ms,
                persona_id=event.persona_id,
                model_entry=event.model_entry,
                provider=event.provider,
                purpose=UsagePurpose(event.purpose),
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                total_tokens=event.total_tokens,
                latency_ms=event.latency_ms,
                status=UsageStatus(event.status),
                request_id=event.request_id,
                error_code=event.error_code,
            )
        )

    return _record
