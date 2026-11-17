"""search 域服务——SearchService（provider failover 链 + 预算门禁 + 用量落账）。

调用链（单一入口 :meth:`SearchService.search`，文档化于
docs/backend/search.md）::

    search(query, persona_id=?, max_results=?)
        ↓ ① 入参校验（SearchQueryModel——query 1-256 / 条数 1-20）
        ↓ ② 运行时配置读取（runtime_search_config——CRUD 热重建后自动
        ↓     生效；enabled=false → 清晰业务异常）
        ↓ ③ 预算门禁（先 persona 档后 global 档；按 usage 域
        ↓     purpose=search 今日行计数——含失败尝试行，防故障重试风暴
        ↓     绕过预算；超限 → SEARCH_BUDGET_EXCEEDED，details
        ↓     {used, limit, scope[, persona_id]}；已达上限的第 N+1 次调用
        ↓     拒绝——恰好达到上限的那次调用放行）
        ↓ ④ provider failover 链（providers 列表序即尝试序）：
        ↓     降级冷却中（now < degraded_until）→ 跳过；
        ↓     密钥缺失 → LLM_API_KEY_MISSING（fail loud——配置错误面，
        ↓       不降级不计数）
        ↓     客户端调用（按 kind 构建，per-call 瞬时构造——与 llm 网关
        ↓       同范式，配置热重建天然生效）→ 成功：清除降级标记 +
        ↓       用量行（success）+ 返回；
        ↓     失败 → 标记降级（now + cooldown_seconds）+ 用量行（error）
        ↓       + 落下一级
        ↓ ⑤ 全部 provider 降级 / 失败 → LLM_REQUEST_FAILED（503——details
        ↓     携带跳过 / 尝试明细与末次错误）

预算口径（文档化决策）：

    - **按行计数**：今日 = epoch 毫秒整除 86_400_000 对齐的 UTC 日桶
      （与 usage 域 ``time_bucket=day`` 同口径——跨方言确定性）；计数含
      ``status=error`` 行（provider 故障重试同样消耗预算——防故障风暴）；
      预算拒绝本身不落行（调用未发生，零 provider 成本）；
    - **成本观测（estimated cost）**：usage 行无成本列（表结构不可变——
      本 chunk 按计数预算）；成本经 ``model_entry=条目名`` +
      ``provider=kind`` 维度可观测，单次调用成本估算口径文档化于
      docs/backend/usage.md 的 purpose 表（search 行——按 provider 计价
      页估值为准，代码不硬编码价格）。

人格作用域：``persona_id`` 显式参数优先，缺省读 ``persona_id_var``
上下文（duty 轮 / 通道路由均已绑定则自动携带）；无人格上下文 =
系统调用（persona 档预算跳过，global 档仍生效）。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from loguru import logger

from packages.harness.config import search_registry
from packages.harness.config.model_entry import resolve_api_key
from packages.harness.config.models import SearchProviderEntry
from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.search.bocha_client import BochaWebSearchClient
from packages.harness.search.search_models import (
    SearchBudgetErrorDetails,
    SearchQueryModel,
    SearchResponseModel,
    SearchUsageEmitter,
    SearchUsageEvent,
    SearchUsageFace,
)
from packages.harness.search.zhipu_client import ZhipuWebSearchClient
from packages.harness.usage.dtos import UsageCreateDTO, UsageQueryDTO
from packages.harness.usage.models import UsagePurpose, UsageStatus

__all__ = [
    "SEARCH_PURPOSE",
    "SEARCH_BUDGET_EXCEEDED_CODE",
    "SEARCH_DISABLED_CODE",
    "DAY_BUCKET_MS",
    "SearchService",
]

#: 搜索链路的用量 purpose（usage 域枚举成员——R2a-4 新增）。
SEARCH_PURPOSE: str = UsagePurpose.SEARCH.value

#: 预算超限错误码（字符串字面量形态——core/errors.py 值域不在本变更
#: 允许清单内；http_status 经 get_http_status_for_code 回落 400）。
SEARCH_BUDGET_EXCEEDED_CODE: str = "SEARCH_BUDGET_EXCEEDED"

#: 能力未启用错误码（同上——字符串字面量形态）。
SEARCH_DISABLED_CODE: str = "SEARCH_DISABLED"

#: UTC 日桶宽（毫秒）——预算「今日」边界（floor 对齐，与 usage 域
#: time_bucket=day 同口径）。
DAY_BUCKET_MS: int = 86_400_000

#: 预算计数分页查询的页大小（usage 域上限 200——预算默认 ≤200 时
#: 单页即出）。
_COUNT_PAGE_LIMIT: int = 200

#: 预算计数分页遍历的页数安全上限（防异常膨胀的记录集拖死门禁路径）。
_COUNT_PAGE_CAP: int = 50


def _day_start_ms(now_ms_value: int) -> int:
    """UTC 日桶起点（epoch 毫秒整除对齐——预算「今日」边界）。

    Args:
        now_ms_value: 当前 epoch 毫秒。

    Returns:
        今日 UTC 0 点对应的 epoch 毫秒（floor 对齐）。
    """
    return (now_ms_value // DAY_BUCKET_MS) * DAY_BUCKET_MS


class SearchService:
    """联网搜索编排服务（failover 链 + 预算门禁 + 用量落账）。

    构造注入（测试 / 装配友好）：共享 HTTP client / usage 消费面 /
    时钟均可显式注入；provider 注册表与运营参数在**调用时**读运行时
    真源（``runtime_search_config``——管理面 CRUD 热重建后自动生效，
    与 llm 网关按调用读 resolver 同范式）。降级标记为进程内内存态
    （重启即清零——冷却窗口语义天然复位）。
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        usage_face: SearchUsageFace | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        """初始化服务。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接——
                pools.PoolManager 装配产物 / 测试 MockTransport client）。
            usage_face: usage 域消费面（None = 零预算门禁 + 零落账——
                纯编排测试形态；生产装配注入 UsageService）。
            clock: 时钟注入（epoch 毫秒 callable；测试可控降级冷却窗）。
        """
        self._http = http_client
        self._usage_face = usage_face
        self._clock = clock
        #: 降级标记（条目名 → 降级截止 epoch 毫秒；成功调用即清除）。
        self._degraded_until: dict[str, int] = {}

    async def search(
        self,
        query: str,
        *,
        persona_id: str | None = None,
        max_results: int | None = None,
    ) -> SearchResponseModel:
        """执行一次联网搜索（预算门禁 → failover 链 → 归一结果）。

        Args:
            query: 搜索词（1-256 字符——越界 ``VALIDATION_ERROR``）。
            persona_id: 人格 ID（None = 读 ``persona_id_var`` 上下文；
                无人格上下文 = 系统调用——persona 档预算跳过）。
            max_results: 返回条数覆盖（None = 条目 ``max_results``）。

        Returns:
            归一响应（provider_used = 实际命中的条目名）。

        Raises:
            BusinessException: 入参越界（``VALIDATION_ERROR``）；能力未
                启用（``SEARCH_DISABLED``）；预算超限
                （``SEARCH_BUDGET_EXCEEDED``——details {used, limit,
                scope[, persona_id]}）；密钥缺失（``LLM_API_KEY_MISSING``）；
                全部 provider 降级 / 失败（``LLM_REQUEST_FAILED``）。
        """
        try:
            validated = SearchQueryModel(query=query, max_results=max_results)
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"搜索入参非法: {exc}",
            ) from exc
        config = search_registry.runtime_search_config
        if not config.web_search.enabled:
            raise BusinessException(
                code=SEARCH_DISABLED_CODE,
                message="联网搜索能力未启用（config.yaml search.web_search.enabled=false）",
            )
        if not config.providers:
            raise BusinessException(
                code=SEARCH_DISABLED_CODE,
                message="联网搜索能力已启用但无 provider 条目（config.yaml search 段为空）",
            )
        resolved_persona = persona_id if persona_id is not None else persona_id_var.get()
        current_ms = self._clock()
        day_start = _day_start_ms(current_ms)
        await self._enforce_budgets(resolved_persona, day_start)
        return await self._failover_chain(
            validated.query,
            resolved_persona,
            validated.max_results,
        )

    # ------------------------------------------------------------------
    # 预算门禁
    # ------------------------------------------------------------------

    async def _enforce_budgets(
        self, persona_id: str | None, day_start: int
    ) -> None:
        """预算门禁（persona 档先行——更紧的先报；超限即抛）。

        Args:
            persona_id: 人格 ID（None = 系统调用——跳过 persona 档）。
            day_start: 今日 UTC 日桶起点（epoch 毫秒）。

        Raises:
            BusinessException: 任一档超限（``SEARCH_BUDGET_EXCEEDED``）。
        """
        if self._usage_face is None:
            return
        settings = search_registry.runtime_search_config.web_search
        if persona_id is not None:
            persona_used = await self._count_today(persona_id, day_start)
            if persona_used >= settings.daily_budget_per_persona:
                details = SearchBudgetErrorDetails(
                    used=persona_used,
                    limit=settings.daily_budget_per_persona,
                    scope="persona",
                    persona_id=persona_id,
                )
                raise BusinessException(
                    code=SEARCH_BUDGET_EXCEEDED_CODE,
                    message=(
                        f"单人格每日搜索预算已用尽: used={persona_used}, "
                        f"limit={settings.daily_budget_per_persona}, persona={persona_id}"
                    ),
                    details=details.model_dump(exclude_none=True),
                )
        global_used = await self._count_today(None, day_start)
        if global_used >= settings.daily_budget_global:
            details = SearchBudgetErrorDetails(
                used=global_used,
                limit=settings.daily_budget_global,
                scope="global",
                persona_id=None,
            )
            raise BusinessException(
                code=SEARCH_BUDGET_EXCEEDED_CODE,
                message=f"全局每日搜索预算已用尽: used={global_used}, limit={settings.daily_budget_global}",
                details=details.model_dump(exclude_none=True),
            )

    async def _count_today(self, persona_id: str | None, day_start: int) -> int:
        """计数今日（UTC 日桶）purpose=search 的 usage 行数。

        游标分页遍历 + 提前收束（累计数无上限意义——门禁只需判
        ``used >= limit``；预算默认 ≤200 时单页即出）。分页遍历设页数
        安全上限（超限按已计数返回——门禁语义下多计不误放）。

        Args:
            persona_id: 人格 ID（None = 全人格——global 档口径）。
            day_start: 今日 UTC 日桶起点（epoch 毫秒）。

        Returns:
            今日搜索行数（含失败行；usage 面未注入返回 0）。
        """
        if self._usage_face is None:
            return 0
        total = 0
        cursor: str | None = None
        for _ in range(_COUNT_PAGE_CAP):
            page = await self._usage_face.query(
                UsageQueryDTO(
                    purpose=UsagePurpose.SEARCH,
                    persona_id=persona_id,
                    start_ms=day_start,
                    cursor=cursor,
                    limit=_COUNT_PAGE_LIMIT,
                )
            )
            total += len(page.items)
            cursor = page.next_cursor
            if not cursor:
                break
        return total

    # ------------------------------------------------------------------
    # failover 链
    # ------------------------------------------------------------------

    async def _failover_chain(
        self, query: str, persona_id: str | None, max_results_override: int | None
    ) -> SearchResponseModel:
        """按列表序尝试 provider（降级跳过 + 失败标记 + 全败汇总）。

        Args:
            query: 搜索词。
            persona_id: 人格 ID（用量行归属）。
            max_results_override: 条数覆盖（None = 条目缺省）。

        Returns:
            命中 provider 的归一响应。

        Raises:
            BusinessException: 密钥缺失（``LLM_API_KEY_MISSING``——配置
                错误不降级）；全部降级 / 失败（``LLM_REQUEST_FAILED``）。
        """
        config = search_registry.runtime_search_config
        cooldown_ms = config.web_search.cooldown_seconds * 1000
        skipped: list[str] = []
        attempted: list[str] = []
        last_error: str | None = None
        for entry in config.providers:
            degraded_until = self._degraded_until.get(entry.name)
            if degraded_until is not None and self._clock() < degraded_until:
                skipped.append(entry.name)
                logger.info("搜索 provider 降级冷却中，跳过: provider={}", entry.name)
                continue
            api_key = self._resolve_entry_key(entry)
            client = self._build_client(entry, api_key, persona_id)
            try:
                response = await client.search(
                    query,
                    max_results=max_results_override
                    if max_results_override is not None
                    else entry.max_results,
                )
            except BusinessException as exc:
                self._degraded_until[entry.name] = self._clock() + cooldown_ms
                attempted.append(entry.name)
                last_error = f"{entry.name}: {exc.code} {exc.message}"
                logger.warning(
                    "搜索 provider 尝试失败（标记降级 {}s，落下一级）: provider={}, error={}",
                    config.web_search.cooldown_seconds,
                    entry.name,
                    exc.message,
                )
                continue
            self._degraded_until.pop(entry.name, None)
            return response
        raise BusinessException(
            code=ErrorCode.LLM_REQUEST_FAILED,
            message="搜索全部 provider 不可用（降级冷却或全部尝试失败）",
            details={
                "skipped_degraded": skipped,
                "attempted": attempted,
                "last_error": last_error,
                "cooldown_seconds": config.web_search.cooldown_seconds,
            },
        )

    def _resolve_entry_key(self, entry: SearchProviderEntry) -> str:
        """解析条目密钥（api_key_env 间接——密钥收口于本调用点）。

        Args:
            entry: provider 条目。

        Returns:
            环境变量解析出的密钥明文（repr 不落日志）。

        Raises:
            BusinessException: 条目声明的密钥变量为空 / 未设置
                （``LLM_API_KEY_MISSING``——配置错误面，不降级不计数）。
        """
        api_key = resolve_api_key(entry.api_key_env)
        if not api_key:
            raise BusinessException(
                code=ErrorCode.LLM_API_KEY_MISSING,
                message=(
                    f"搜索 provider 密钥未配置（环境变量 {entry.api_key_env} 为空）: {entry.name}"
                ),
                details={"provider": entry.name, "api_key_env": entry.api_key_env},
            )
        return api_key

    def _build_client(
        self, entry: SearchProviderEntry, api_key: str, persona_id: str | None
    ) -> BochaWebSearchClient | ZhipuWebSearchClient:
        """按条目 kind 瞬时构建 provider 客户端（用量闭包绑定条目身份）。

        Args:
            entry: provider 条目。
            api_key: 已解析密钥明文。
            persona_id: 人格 ID（落进用量行 persona_id 列）。

        Returns:
            对应 kind 的客户端实例。

        Raises:
            BusinessException: 未知 kind（``VALIDATION_ERROR``——条目
                Literal 校验下不可达的防御面）。
        """
        emitter = self._build_usage_emitter(entry, persona_id)
        if entry.kind == "bocha":
            return BochaWebSearchClient(
                self._http,
                api_key=api_key,
                base_url=entry.base_url,
                request_timeout_seconds=entry.timeout_seconds,
                provider_name=entry.name,
                usage_emitter=emitter,
            )
        if entry.kind == "zhipu":
            return ZhipuWebSearchClient(
                self._http,
                api_key=api_key,
                base_url=entry.base_url,
                search_engine=entry.model,
                request_timeout_seconds=entry.timeout_seconds,
                provider_name=entry.name,
                usage_emitter=emitter,
            )
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"未知搜索 provider kind: {entry.kind}",
            details={"provider": entry.name, "kind": entry.kind},
        )

    def _build_usage_emitter(
        self, entry: SearchProviderEntry, persona_id: str | None
    ) -> SearchUsageEmitter | None:
        """构建单次调用的用量发射闭包（条目身份 + 人格上下文绑定）。

        Args:
            entry: provider 条目（name / kind 落进行标识列）。
            persona_id: 人格 ID（None = 系统调用）。

        Returns:
            异步发射器（``SearchUsageEvent`` → usage 行落账；usage 面未
            注入时返回 None——客户端 no-op）。
        """
        if self._usage_face is None:
            return None
        usage_face = self._usage_face

        async def _emit(event: SearchUsageEvent) -> None:
            """事件 → usage 创建 DTO（purpose=search 行）→ 落账。"""
            await usage_face.create(
                UsageCreateDTO(
                    ts_ms=self._clock(),
                    persona_id=persona_id,
                    model_entry=entry.name,
                    provider=entry.kind,
                    purpose=UsagePurpose.SEARCH,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    latency_ms=event.latency_ms,
                    status=UsageStatus(event.status),
                    request_id=None,
                    error_code=event.error_code,
                )
            )

        return _emit
