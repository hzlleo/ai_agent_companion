"""harness/tools registry 测试——注册 / 列举 / 隔离执行 + 内置工具。

覆盖面：list_specs 的 OpenAI 定义形态；同名覆盖注册；execute 的未知
工具 / 参数校验失败 / 执行器异常三类隔离（恒不抛 + 时延非负）；内置
get_current_time（注入时钟 + naive 本地格式化）；内置 search_memory
（fake 检索服务——人格 contextvar 作用域 / 缺失 / 非数值 / 参数默认值
物化 / 结果序列化）；register_builtin_tools 的装配矩阵（无检索服务 →
工具面收缩）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from packages.harness.core.context import bind_context
from packages.harness.memory.retrieval_models import (
    MemoryRefType,
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)
from packages.harness.tools.registry import (
    CURRENT_TIME_TOOL_NAME,
    SEARCH_MEMORY_TOOL_NAME,
    ToolRegistry,
    build_current_time_tool,
    build_search_memory_tool,
    register_builtin_tools,
)
from packages.harness.tools.tool_models import (
    GetCurrentTimeArgsModel,
    SearchMemoryArgsModel,
    ToolSpecModel,
)


class _EchoArgs(BaseModel):
    """测试工具参数面（校验边界断言用）。"""

    model_config = {"extra": "forbid"}

    value: int = Field(ge=0, le=10)


def _echo_tool() -> tuple[ToolSpecModel, Any]:
    """构造 echo 测试工具（参数校验 + 结果回显）。"""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """回显参数。"""
        return {"echo": args["value"]}

    spec = ToolSpecModel(
        name="echo",
        description="回显测试",
        args_schema={"type": "object", "properties": {"value": {"type": "integer"}}},
        args_model=_EchoArgs,
    )
    return spec, _execute


class FakeSearchService:
    """fake 记忆检索服务（记录查询；返回单条假结果）。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化（fail=True 时 search 抛异常——隔离路径用）。"""
        self.queries: list[MemorySearchQueryDTO] = []
        self.fail = fail

    async def search(self, query: MemorySearchQueryDTO) -> list[MemorySearchResultDTO]:
        """记录查询并返回单条固定结果。"""
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("检索服务不可用（测试注入）")
        return [
            MemorySearchResultDTO(
                ref_type=MemoryRefType.EPISODE,
                ref_id=1,
                kind="chat",
                provenance="owner",
                ts_ms=1000,
                content="用户：今天心情不错",
                score=1.5,
            )
        ]


# ---------------------------------------------------------------------------
# 注册 / 列举
# ---------------------------------------------------------------------------


async def test_list_specs_renders_openai_definition_shape() -> None:
    """list_specs：OpenAI tool 定义形态（name / description / parameters）。"""
    registry = ToolRegistry()
    spec, executor = _echo_tool()
    registry.register(spec, executor)
    definitions = registry.list_specs()
    assert len(definitions) == 1
    assert definitions[0].name == "echo"
    assert definitions[0].description == "回显测试"
    assert definitions[0].parameters.model_dump(exclude_none=True) == {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": [],
    }
    assert registry.tool_names == ["echo"]


async def test_register_same_name_overwrites() -> None:
    """同名重复注册：后写覆盖前写（工具面唯一）。"""
    registry = ToolRegistry()
    spec, executor = _echo_tool()
    registry.register(spec, executor)

    async def _new_executor(args: dict[str, Any]) -> dict[str, Any]:
        """覆盖后的执行器。"""
        return {"replaced": True}

    registry.register(spec, _new_executor)
    assert registry.tool_names == ["echo"]
    result = await registry.execute("echo", {"value": 1})
    assert result.ok is True
    assert result.result == {"replaced": True}


# ---------------------------------------------------------------------------
# execute 隔离（恒不抛）
# ---------------------------------------------------------------------------


async def test_execute_unknown_tool_isolated() -> None:
    """未知工具：ok=False + error 仅携带工具名（不回显可用清单）；不上抛。"""
    registry = ToolRegistry()
    spec, executor = _echo_tool()
    registry.register(spec, executor)
    result = await registry.execute("nope")
    assert result.ok is False
    assert result.tool == "nope"
    assert (result.error or "").startswith("未注册工具: " + "nope")
    assert "可用" not in (result.error or "")
    assert "echo" not in (result.error or "")
    assert result.result == {}
    assert result.latency_ms >= 0


async def test_execute_args_validation_failure_isolated() -> None:
    """参数校验失败（越界 / 未知键）：ok=False；不上抛。"""
    registry = ToolRegistry()
    spec, executor = _echo_tool()
    registry.register(spec, executor)
    out_of_range = await registry.execute("echo", {"value": 99})
    assert out_of_range.ok is False
    assert "参数校验失败" in (out_of_range.error or "")
    unknown_key = await registry.execute("echo", {"value": 1, "extra": True})
    assert unknown_key.ok is False


async def test_execute_args_defaults_materialized() -> None:
    """args_model 默认值物化：缺省参数进入执行器（limit=5 形态）。"""
    seen: list[dict[str, Any]] = []

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """记录收到的参数。"""
        seen.append(args)
        return {}

    spec = ToolSpecModel(
        name="with_defaults",
        description="默认值物化",
        args_model=SearchMemoryArgsModel,
    )
    registry = ToolRegistry()
    registry.register(spec, _execute)
    result = await registry.execute("with_defaults", {"query": "近况"})
    assert result.ok is True
    assert seen[0] == {"query": "近况", "limit": 5, "mode": "keyword"}


async def test_execute_spec_without_args_model_passes_raw() -> None:
    """args_model=None：参数原样透传（不校验）。"""
    seen: list[dict[str, Any]] = []

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """记录收到的参数。"""
        seen.append(args)
        return {"ok": True}

    spec = ToolSpecModel(name="raw", description="原样透传")
    registry = ToolRegistry()
    registry.register(spec, _execute)
    result = await registry.execute("raw", {"anything": [1, 2]})
    assert result.ok is True
    assert seen[0] == {"anything": [1, 2]}


async def test_execute_executor_exception_isolated() -> None:
    """执行器异常（含 BusinessException 形态）：ok=False + error 文案。"""
    registry = ToolRegistry()

    async def _boom(args: dict[str, Any]) -> dict[str, Any]:
        """抛 RuntimeError 的执行器。"""
        raise RuntimeError("boom")

    async def _business_boom(args: dict[str, Any]) -> dict[str, Any]:
        """抛 BusinessException 的执行器。"""
        from packages.harness.core.errors import BusinessException, ErrorCode

        raise BusinessException(code=ErrorCode.VALIDATION_ERROR, message="参数面拒绝")

    registry.register(ToolSpecModel(name="boom", description=""), _boom)
    registry.register(ToolSpecModel(name="biz_boom", description=""), _business_boom)
    runtime_result = await registry.execute("boom")
    assert runtime_result.ok is False
    assert "工具执行器异常" in (runtime_result.error or "")
    biz_result = await registry.execute("biz_boom")
    assert biz_result.ok is False
    assert "VALIDATION_ERROR" in (biz_result.error or "")
    assert "参数面拒绝" in (biz_result.error or "")


async def test_execute_none_arguments_treated_as_empty() -> None:
    """arguments=None：空参形态（无参工具直接执行）。"""
    registry = ToolRegistry()
    spec, executor = build_current_time_tool(clock=lambda: 1_700_000_000_000)
    registry.register(spec, executor)
    result = await registry.execute(CURRENT_TIME_TOOL_NAME)
    assert result.ok is True


async def test_execute_success_carries_result_and_timing() -> None:
    """成功路径：result 回显 + latency_ms 非负 + error None。"""
    registry = ToolRegistry()
    spec, executor = _echo_tool()
    registry.register(spec, executor)
    result = await registry.execute("echo", {"value": 3})
    assert result.ok is True
    assert result.tool == "echo"
    assert result.result == {"echo": 3}
    assert result.error is None
    assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# 内置 get_current_time
# ---------------------------------------------------------------------------


async def test_current_time_tool_with_injectable_clock() -> None:
    """get_current_time：注入时钟 → epoch_ms 精确 + naive 本地格式化。"""
    fixed_ms = 1_760_000_000_123
    spec, executor = build_current_time_tool(clock=lambda: fixed_ms)
    result = await executor({})
    from datetime import datetime

    expected_local = datetime.fromtimestamp(fixed_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    assert result == {"epoch_ms": fixed_ms, "local_time": expected_local}
    assert spec.name == CURRENT_TIME_TOOL_NAME
    assert spec.args_model is GetCurrentTimeArgsModel
    assert spec.args_schema.model_dump(exclude_none=True) == {
        "type": "object",
        "properties": {},
        "required": [],
    }


async def test_current_time_tool_rejects_arguments() -> None:
    """get_current_time 收到参数：校验失败（无参工具红线）。"""
    registry = ToolRegistry()
    register_builtin_tools(registry)
    result = await registry.execute(CURRENT_TIME_TOOL_NAME, {"unexpected": 1})
    assert result.ok is False


# ---------------------------------------------------------------------------
# 内置 search_memory
# ---------------------------------------------------------------------------


async def test_search_memory_tool_delegates_with_persona_contextvar() -> None:
    """search_memory：人格 contextvar 作用域 + 参数透传 + 结果序列化。"""
    service = FakeSearchService()
    spec, executor = build_search_memory_tool(service)
    async with bind_context(persona_id="701"):
        result = await executor({"query": "主人 近况", "limit": 2, "mode": "keyword"})
    assert result["count"] == 1
    assert result["results"][0]["content"] == "用户：今天心情不错"
    assert len(service.queries) == 1
    query = service.queries[0]
    assert query.persona_id == 701
    assert query.query == "主人 近况"
    assert query.limit == 2
    assert query.mode.value == "keyword"
    assert spec.name == SEARCH_MEMORY_TOOL_NAME
    assert spec.args_model is SearchMemoryArgsModel
    schema = spec.args_schema.model_dump(exclude_none=True)
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "limit", "mode"}


async def test_search_memory_tool_defaults_applied() -> None:
    """search_memory 缺省参数：limit=5 + mode=keyword（registry 校验物化）。"""
    service = FakeSearchService()
    registry = ToolRegistry()
    spec, executor = build_search_memory_tool(service)
    registry.register(spec, executor)
    async with bind_context(persona_id="701"):
        result = await registry.execute(SEARCH_MEMORY_TOOL_NAME, {"query": "日记"})
    assert result.ok is True
    assert service.queries[0].limit == 5
    assert service.queries[0].mode.value == "keyword"


async def test_search_memory_tool_missing_persona_context() -> None:
    """人格上下文缺失：ok=False（PERSONA_CONTEXT_REQUIRED 隔离文案）。"""
    service = FakeSearchService()
    registry = ToolRegistry()
    spec, executor = build_search_memory_tool(service)
    registry.register(spec, executor)
    result = await registry.execute(SEARCH_MEMORY_TOOL_NAME, {"query": "日记"})
    assert result.ok is False
    assert "人格上下文" in (result.error or "")


async def test_search_memory_tool_non_numeric_persona() -> None:
    """人格上下文非数值：ok=False（索引 BigInt 隔离谓词面）。"""
    service = FakeSearchService()
    registry = ToolRegistry()
    spec, executor = build_search_memory_tool(service)
    registry.register(spec, executor)
    async with bind_context(persona_id="not-a-number"):
        result = await registry.execute(SEARCH_MEMORY_TOOL_NAME, {"query": "日记"})
    assert result.ok is False
    assert "非数值" in (result.error or "")


async def test_search_memory_tool_service_failure_isolated() -> None:
    """检索服务故障：ok=False（executor 异常隔离）。"""
    service = FakeSearchService(fail=True)
    registry = ToolRegistry()
    spec, executor = build_search_memory_tool(service)
    registry.register(spec, executor)
    async with bind_context(persona_id="701"):
        result = await registry.execute(SEARCH_MEMORY_TOOL_NAME, {"query": "日记"})
    assert result.ok is False
    assert "工具执行器异常" in (result.error or "")


async def test_register_builtin_tools_matrix() -> None:
    """装配矩阵：无检索服务 → 仅 get_current_time；注入 → 双工具。"""
    bare = ToolRegistry()
    register_builtin_tools(bare)
    assert bare.tool_names == [CURRENT_TIME_TOOL_NAME]
    full = ToolRegistry()
    register_builtin_tools(full, search_service=FakeSearchService())
    assert full.tool_names == [CURRENT_TIME_TOOL_NAME, SEARCH_MEMORY_TOOL_NAME]
