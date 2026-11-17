"""search 域服务测试——SearchService（failover / 降级冷却 / 预算 / 用量行）。

覆盖矩阵（fake usage face + MockTransport 双 provider 链——零真实网络）：

    - 入参校验（query 越界 / 条数越界 VALIDATION_ERROR）+ 能力未启用
      （SEARCH_DISABLED）+ 无 provider（SEARCH_DISABLED）；
    - failover 链：主 500 → 兜底命中（provider_used = 兜底条目名）；
      列表序即尝试序；成功后清除降级标记；
    - 降级冷却：失败标记在冷却窗口内跳过（直落下一级）；窗口过后
      恢复尝试（时钟注入驱动）；全部降级 → LLM_REQUEST_FAILED
      （details 携带 skipped / attempted / last_error）；
    - 密钥缺失：LLM_API_KEY_MISSING（配置错误不降级不计数）；
    - 预算门禁：persona 档超限（details {used, limit, scope,
      persona_id}）；global 档超限；边界——恰好达到上限的那次调用放行
      （第 limit+1 次拒绝）；无人格上下文跳过 persona 档；usage 面未
      注入零门禁；
    - 用量行：purpose=search / tokens 0/0/0 / model_entry=条目名 /
      provider=kind / 失败尝试行 error_code 携带；预算拒绝不落行；
    - max_results：请求级覆盖 vs 条目缺省。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from packages.harness.config.models import (
    SearchConfig,
    SearchProviderEntry,
    WebSearchSettings,
)
from packages.harness.config.search_registry import (
    apply_search_config_to_runtime,
)
from packages.harness.core.dtos import PageResponse
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.search.search_models import SearchUsageFace
from packages.harness.search.search_service import (
    SEARCH_BUDGET_EXCEEDED_CODE,
    SEARCH_DISABLED_CODE,
    SEARCH_PURPOSE,
    SearchService,
)
from packages.harness.usage.dtos import UsageCreateDTO, UsageQueryDTO, UsageRecordDTO

#: 主 provider（bocha）成功响应体（MockTransport）。
_BOCHA_OK_BODY: dict[str, Any] = {
    "code": 200,
    "data": {"webPages": {"value": [{"name": "博查结果", "url": "https://bocha.test/r", "snippet": "主摘要"}]}},
}

#: 兜底 provider（zhipu）成功响应体（MockTransport）。
_ZHIPU_OK_BODY: dict[str, Any] = {
    "search_result": [{"title": "智谱结果", "link": "https://zhipu.test/r", "content": "兜底摘要"}]
}


class FakeUsageFace:
    """fake usage 消费面——记录创建 + 可注入的今日行计数。"""

    def __init__(self, *, persona_counts: dict[str | None, int] | None = None) -> None:
        """初始化（计数注入 + 创建记录收集）。"""
        self._persona_counts = persona_counts or {}
        self.created: list[UsageCreateDTO] = []
        self.queries: list[UsageQueryDTO] = []

    def set_counts(self, persona_counts: dict[str | None, int]) -> None:
        """注入今日行计数（预算门禁断言真源）。"""
        self._persona_counts = persona_counts

    async def query(self, query: UsageQueryDTO) -> PageResponse[UsageRecordDTO]:
        """按注入计数返回合成记录页（门禁只消费 len）。"""
        self.queries.append(query)
        count = self._persona_counts.get(query.persona_id, 0)
        records = [
            UsageRecordDTO(
                id=index + 1,
                persona_id=query.persona_id,
                ts_ms=query.start_ms or 0,
                model_entry="seed",
                provider="bocha",
                purpose=query.purpose,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=1,
                status="success",
                request_id=None,
                error_code=None,
                created_at=0,
            )
            for index in range(count)
        ]
        return PageResponse.of_cursor(records, page_size=200, next_cursor=None)

    async def create(self, dto: UsageCreateDTO) -> UsageRecordDTO:
        """收集创建 DTO（断言 purpose / tokens / 条目身份）。"""
        self.created.append(dto)
        return UsageRecordDTO(
            id=len(self.created),
            persona_id=dto.persona_id,
            ts_ms=dto.ts_ms,
            model_entry=dto.model_entry,
            provider=dto.provider,
            purpose=dto.purpose,
            input_tokens=dto.input_tokens,
            output_tokens=dto.output_tokens,
            total_tokens=dto.total_tokens,
            latency_ms=dto.latency_ms,
            status=dto.status,
            request_id=dto.request_id,
            error_code=dto.error_code,
            created_at=0,
        )


def _config(
    *,
    enabled: bool = True,
    tool_enabled: bool = True,
    bocha_base: str = "http://bocha.test",
    zhipu_base: str = "http://zhipu.test",
    per_persona: int = 50,
    global_budget: int = 200,
    cooldown_seconds: int = 60,
) -> SearchConfig:
    """构造双 provider 测试配置（bocha 主 + zhipu 兜底）。"""
    return SearchConfig(
        providers=[
            SearchProviderEntry(name="bocha-main", kind="bocha", base_url=bocha_base),
            SearchProviderEntry(name="zhipu-fallback", kind="zhipu", base_url=zhipu_base),
        ],
        web_search=WebSearchSettings(
            enabled=enabled,
            tool_enabled=tool_enabled,
            daily_budget_per_persona=per_persona,
            daily_budget_global=global_budget,
            cooldown_seconds=cooldown_seconds,
        ),
    )


def _service(
    handler: Any,
    *,
    usage_face: SearchUsageFace | None = None,
    clock: Any = None,
) -> SearchService:
    """构造测试服务（MockTransport http client + 注入面）。"""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs: dict[str, Any] = {"http_client": http, "usage_face": usage_face}
    if clock is not None:
        kwargs["clock"] = clock
    return SearchService(**kwargs)


def _handler_by_host(
    *,
    bocha_status: int = 200,
    zhipu_status: int = 200,
) -> Any:
    """按 host 分派的 MockTransport 处理器（双端点独立状态码）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bocha.test":
            if bocha_status != 200:
                return httpx.Response(bocha_status, text="bocha boom")
            return httpx.Response(200, json=_BOCHA_OK_BODY)
        if zhipu_status != 200:
            return httpx.Response(zhipu_status, text="zhipu boom")
        return httpx.Response(200, json=_ZHIPU_OK_BODY)

    return handler


class TestInputAndEnabledGates:
    async def test_query_over_limit_rejected(self) -> None:
        """query 超 256 字符：VALIDATION_ERROR（本地校验——零网络）。"""
        apply_search_config_to_runtime(_config())
        service = _service(lambda request: httpx.Response(200, json={}))
        with pytest.raises(BusinessException) as exc_info:
            await service.search("长" * 257)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_max_results_out_of_range_rejected(self) -> None:
        """条数覆盖越界（21）：VALIDATION_ERROR。"""
        apply_search_config_to_runtime(_config())
        service = _service(lambda request: httpx.Response(200, json={}))
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q", max_results=21)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_disabled_rejected(self) -> None:
        """能力未启用：SEARCH_DISABLED（清晰中文消息）。"""
        apply_search_config_to_runtime(_config(enabled=False))
        service = _service(lambda request: httpx.Response(200, json={}))
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q")
        assert exc_info.value.code == SEARCH_DISABLED_CODE
        assert "未启用" in exc_info.value.message

    async def test_no_providers_rejected(self) -> None:
        """启用但无 provider：SEARCH_DISABLED（启动校验的运行时防线）。"""
        apply_search_config_to_runtime(
            SearchConfig(web_search=WebSearchSettings(enabled=True))
        )
        service = _service(lambda request: httpx.Response(200, json={}))
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q")
        assert exc_info.value.code == SEARCH_DISABLED_CODE


class TestFailoverChain:
    async def test_primary_success_no_fallback(self) -> None:
        """主 provider 成功：直接命中（provider_used = 主条目名）。"""
        apply_search_config_to_runtime(_config())
        service = _service(_handler_by_host())
        result = await service.search("q")
        assert result.provider_used == "bocha-main"
        assert result.results[0].title == "博查结果"

    async def test_primary_failure_falls_through(self) -> None:
        """主 500 → 兜底命中（failover——provider_used = 兜底条目名）。"""
        apply_search_config_to_runtime(_config())
        service = _service(_handler_by_host(bocha_status=500))
        result = await service.search("q")
        assert result.provider_used == "zhipu-fallback"
        assert result.results[0].title == "智谱结果"

    async def test_degraded_cooldown_honored_within_window(self) -> None:
        """降级冷却：窗口内主 provider 被跳过（零主端点请求——兜底直命中）。"""
        apply_search_config_to_runtime(_config())
        bocha_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "bocha.test":
                bocha_calls.append(str(request.url))
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=_ZHIPU_OK_BODY)

        current_ms = 1_000_000

        def clock() -> int:
            """注入时钟（首调用后固定——冷却窗口内）。"""
            return current_ms

        service = _service(handler, clock=clock)
        first = await service.search("q")
        assert first.provider_used == "zhipu-fallback"
        assert len(bocha_calls) == 1
        second = await service.search("q")
        assert second.provider_used == "zhipu-fallback"
        # 冷却窗口内主 provider 被跳过——bocha 端点零新增请求
        assert len(bocha_calls) == 1

    async def test_degraded_recovers_after_window(self) -> None:
        """冷却窗口过后恢复尝试（时钟推进——主 provider 复活命中）。"""
        apply_search_config_to_runtime(_config())
        current_ms = 1_000_000
        bocha_seen: list[int] = []

        def clock() -> int:
            """注入时钟（外部推进）。"""
            return current_ms

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "bocha.test":
                # 首次失败（标记降级）；窗口过后复活成功
                if not bocha_seen:
                    bocha_seen.append(1)
                    return httpx.Response(500, text="boom")
                bocha_seen.append(2)
                return httpx.Response(200, json=_BOCHA_OK_BODY)
            return httpx.Response(200, json=_ZHIPU_OK_BODY)

        service = _service(handler, clock=clock)
        first = await service.search("q")
        assert first.provider_used == "zhipu-fallback"
        current_ms += 60_000 + 1  # 冷却 60s 窗口过后
        second = await service.search("q")
        assert second.provider_used == "bocha-main"

    async def test_all_degraded_raises_request_failed(self) -> None:
        """全部降级（冷却窗口内全跳过）：LLM_REQUEST_FAILED + skipped 明细。"""
        apply_search_config_to_runtime(_config())
        current_ms = 1_000_000

        def clock() -> int:
            """注入时钟（固定——窗口内）。"""
            return current_ms

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="all boom")

        service = _service(handler, clock=clock)
        with pytest.raises(BusinessException):
            await service.search("q")  # 双双失败标记降级
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q")  # 全部跳过
        assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
        assert exc_info.value.details is not None
        assert sorted(exc_info.value.details["skipped_degraded"]) == [
            "bocha-main",
            "zhipu-fallback",
        ]

    async def test_all_attempted_failure_raises_request_failed(self) -> None:
        """全部尝试失败（无降级预标记）：LLM_REQUEST_FAILED + attempted 明细。"""
        apply_search_config_to_runtime(_config())
        service = _service(_handler_by_host(bocha_status=500, zhipu_status=503))
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q")
        assert exc_info.value.code == ErrorCode.LLM_REQUEST_FAILED
        assert exc_info.value.details is not None
        assert exc_info.value.details["attempted"] == ["bocha-main", "zhipu-fallback"]
        assert exc_info.value.details["last_error"] is not None

    async def test_missing_api_key_fails_loud_no_degrade(self) -> None:
        """密钥缺失：LLM_API_KEY_MISSING（配置错误——不降级不落用量行）。"""
        apply_search_config_to_runtime(
            SearchConfig(
                providers=[
                    SearchProviderEntry(
                        name="keyless", kind="bocha", base_url="http://bocha.test", api_key_env="UNSET_KEY_VAR"
                    )
                ],
                web_search=WebSearchSettings(enabled=True),
            )
        )
        usage = FakeUsageFace()
        service = _service(lambda request: httpx.Response(200, json={}), usage_face=usage)
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q")
        assert exc_info.value.code == ErrorCode.LLM_API_KEY_MISSING
        assert "UNSET_KEY_VAR" in exc_info.value.message
        assert usage.created == []

    async def test_max_results_override_and_entry_default(self) -> None:
        """条数：请求级覆盖生效 / 缺省用条目 max_results。"""
        apply_search_config_to_runtime(_config())
        captured: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=_BOCHA_OK_BODY)

        service = _service(handler)
        await service.search("q", max_results=3)
        await service.search("q")
        assert [body["count"] for body in captured] == [3, 8]


class TestBudgetGates:
    async def test_persona_over_budget_rejected_with_details(self) -> None:
        """persona 档超限：SEARCH_BUDGET_EXCEEDED + details {used, limit, scope, persona_id}。"""
        apply_search_config_to_runtime(_config(per_persona=2))
        usage = FakeUsageFace(persona_counts={"7": 2})
        service = _service(_handler_by_host(), usage_face=usage)
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q", persona_id="7")
        assert exc_info.value.code == SEARCH_BUDGET_EXCEEDED_CODE
        assert exc_info.value.details == {
            "used": 2,
            "limit": 2,
            "scope": "persona",
            "persona_id": "7",
        }

    async def test_global_over_budget_rejected_with_details(self) -> None:
        """global 档超限：SEARCH_BUDGET_EXCEEDED + details {used, limit, scope=global}。"""
        apply_search_config_to_runtime(_config(global_budget=5))
        usage = FakeUsageFace(persona_counts={None: 5})
        service = _service(_handler_by_host(), usage_face=usage)
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q", persona_id="7")
        assert exc_info.value.details == {"used": 5, "limit": 5, "scope": "global"}

    async def test_boundary_exactly_at_limit_passes(self) -> None:
        """边界：已用 limit-1 → 本次（恰好第 limit 次）放行；下一次拒绝。"""
        apply_search_config_to_runtime(_config(per_persona=2))
        usage = FakeUsageFace(persona_counts={"7": 1})
        service = _service(_handler_by_host(), usage_face=usage)
        result = await service.search("q", persona_id="7")
        assert result.provider_used == "bocha-main"
        usage.set_counts({"7": 2})
        with pytest.raises(BusinessException) as exc_info:
            await service.search("q", persona_id="7")
        assert exc_info.value.code == SEARCH_BUDGET_EXCEEDED_CODE
        assert exc_info.value.details is not None
        assert exc_info.value.details["used"] == 2

    async def test_persona_scope_none_skips_persona_gate(self) -> None:
        """无人格上下文（系统调用）：persona 档跳过，global 档仍生效。"""
        apply_search_config_to_runtime(_config(per_persona=1, global_budget=100))
        usage = FakeUsageFace(persona_counts={"7": 5})
        service = _service(_handler_by_host(), usage_face=usage)
        result = await service.search("q", persona_id=None)
        assert result.provider_used == "bocha-main"

    async def test_no_usage_face_zero_gate(self) -> None:
        """usage 面未注入：零门禁 + 零落账（纯编排形态）。"""
        apply_search_config_to_runtime(_config())
        service = _service(_handler_by_host(), usage_face=None)
        result = await service.search("q")
        assert result.provider_used == "bocha-main"

    async def test_budget_rejection_writes_no_usage_row(self) -> None:
        """预算拒绝不落行（调用未发生——零 provider 成本）。"""
        apply_search_config_to_runtime(_config(per_persona=1))
        usage = FakeUsageFace(persona_counts={"7": 1})
        service = _service(_handler_by_host(), usage_face=usage)
        with pytest.raises(BusinessException):
            await service.search("q", persona_id="7")
        assert usage.created == []


class TestUsageRows:
    async def test_success_row_shape(self) -> None:
        """成功行：purpose=search / tokens 0/0/0 / model_entry=条目名 / provider=kind。"""
        apply_search_config_to_runtime(_config())
        usage = FakeUsageFace()
        service = _service(_handler_by_host(), usage_face=usage)
        await service.search("q", persona_id="7")
        assert len(usage.created) == 1
        row = usage.created[0]
        assert row.purpose.value == SEARCH_PURPOSE
        assert row.purpose.value == "search"
        assert (row.input_tokens, row.output_tokens, row.total_tokens) == (0, 0, 0)
        assert row.model_entry == "bocha-main"
        assert row.provider == "bocha"
        assert row.persona_id == "7"
        assert row.status.value == "success"
        assert row.error_code is None
        assert row.latency_ms >= 0

    async def test_error_row_on_failed_attempt(self) -> None:
        """失败尝试行：error 状态 + error_code 携带（含失败行入预算口径）。"""
        apply_search_config_to_runtime(_config())
        usage = FakeUsageFace()
        service = _service(_handler_by_host(bocha_status=500), usage_face=usage)
        result = await service.search("q", persona_id="7")
        assert result.provider_used == "zhipu-fallback"
        assert len(usage.created) == 2
        error_row, success_row = usage.created
        assert error_row.model_entry == "bocha-main"
        assert error_row.status.value == "error"
        assert error_row.error_code == str(ErrorCode.LLM_CALL_FAILED)
        assert success_row.model_entry == "zhipu-fallback"
        assert success_row.provider == "zhipu"
        assert success_row.status.value == "success"

    async def test_persona_context_var_respected(self) -> None:
        """人格上下文变量：显式参数缺席时读 persona_id_var（绑定即携带）。"""
        from packages.harness.core.context import persona_id_var

        apply_search_config_to_runtime(_config())
        usage = FakeUsageFace()
        service = _service(_handler_by_host(), usage_face=usage)
        persona_id_var.set("9")
        try:
            await service.search("q")
        finally:
            persona_id_var.set(None)
        assert usage.created[0].persona_id == "9"

    async def test_query_uses_purpose_search_and_day_window(self) -> None:
        """预算计数查询：purpose=search 过滤 + 今日 UTC 日桶起点。"""
        from packages.harness.core.time_utils import now_ms

        apply_search_config_to_runtime(_config())
        usage = FakeUsageFace()
        service = _service(_handler_by_host(), usage_face=usage)
        await service.search("q", persona_id="7")
        queries = [q for q in usage.queries if q.persona_id == "7"]
        assert len(queries) == 1
        assert queries[0].purpose.value == "search"
        expected_day_start = (now_ms() // 86_400_000) * 86_400_000
        assert queries[0].start_ms == expected_day_start
