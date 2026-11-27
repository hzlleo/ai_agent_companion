"""harness/tools 域——ToolRegistry（注册 / 列举 / 隔离执行）+ 内置工具。

职责链（单一执行入口 :meth:`ToolRegistry.execute`）::

    ToolCallRequestModel / (name, arguments)
        ↓ ① 寻址（未注册工具 → ok=False + error，不上抛）
        ↓ ② 参数校验（args_model 逐工具 Model Class 校验 + 默认值物化；
        ↓     失败 → ok=False + error）
        ↓ ③ 执行器调用（异步；异常捕获隔离 → ok=False + error——
        ↓     **execute 恒不抛异常**）
        ↓ ④ 时延测量（epoch 毫秒差——成功 / 失败路径均计时）

设计决策：

    - **executor 无关**：注册面只依赖 :data:`ToolExecutor` 契约
      （``Callable[[dict], Awaitable[dict]]``）——docker 沙盒执行器
      （代理到沙盒进程，见 ``tools/sandbox.py``）以同一契约接入，
      registry 零改动；
    - **list_specs → OpenAI tool 定义**：列举面直接产出
      :class:`ToolDefinitionModel`（llm 域请求 ``tools`` 面——duty 轮
      经 ``gateway.chat_by_role(..., tools=registry.list_specs())`` 透传）；
    - **内置工具经工厂注册**（依赖构造注入——registry 不持全局状态）：

        - ``get_current_time``——无参数；epoch 毫秒 + 本地格式化时间
          （**naive 本地时间**——与 scheduler 同约定，无时区数据库）；
        - ``search_memory``——``{query, limit?, mode?}``；委托注入的
          检索服务；**人格作用域经 contextvar 调用时读取**
          （persona_id_var——duty 轮已绑定人格上下文则自动携带）；
          检索服务未注入时不注册（工具面按装配形态收缩）；
    - **红线**（内置工具边界）：无网络、无文件系统——全部
      依赖经构造注入；平台写入类工具属 safety 审批面。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

from packages.harness.core.context import persona_id_var
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.llm.llm_models import ToolDefinitionModel, ToolParametersSchema
from packages.harness.memory.retrieval_models import (
    MemorySearchMode,
    MemorySearchQueryDTO,
)
from packages.harness.tools.tool_models import (
    GetCurrentTimeArgsModel,
    MemorySearchFace,
    SearchMemoryArgsModel,
    ToolCallRequestModel,
    ToolExecutor,
    ToolResultModel,
    ToolSpecModel,
)

__all__ = [
    "CURRENT_TIME_TOOL_NAME",
    "SEARCH_MEMORY_TOOL_NAME",
    "ToolRegistry",
    "build_current_time_tool",
    "build_search_memory_tool",
    "register_builtin_tools",
]

#: get_current_time 工具名（LLM 请求 tools 面标识）。
CURRENT_TIME_TOOL_NAME: str = "get_current_time"
#: search_memory 工具名（LLM 请求 tools 面标识）。
SEARCH_MEMORY_TOOL_NAME: str = "search_memory"

#: epoch 毫秒 → 本地格式化时间的输出格式（naive 本地时间——无时区数据库）。
_LOCAL_TIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"

#: 无参数工具的 JSON Schema（空 object——模型不可传参）。
_EMPTY_OBJECT_SCHEMA: ToolParametersSchema = ToolParametersSchema(
    type="object",
    properties={},
)

#: search_memory 参数 JSON Schema（与 SearchMemoryArgsModel 字段对齐）。
_SEARCH_MEMORY_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索文本（关键词或待向量化查询）",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限（1-20，默认 5）",
                "minimum": 1,
                "maximum": 20,
            },
            "mode": {
                "type": "string",
                "description": "检索模式：keyword（默认）/ dense / hybrid",
                "enum": ["keyword", "dense", "hybrid"],
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
)


class _ToolEntry:
    """注册表条目（规格 + 执行器——内部运行态，非传输契约）。

    Attributes:
        spec: 工具规格。
        executor: 执行器（异步——参数 dict → 结果 dict）。
    """

    __slots__ = ("spec", "executor")

    def __init__(self, spec: ToolSpecModel, executor: ToolExecutor) -> None:
        """初始化条目。

        Args:
            spec: 工具规格。
            executor: 执行器。
        """
        self.spec = spec
        self.executor = executor


class ToolRegistry:
    """工具注册表（注册 / OpenAI 定义列举 / 隔离执行；execute 恒不抛）。

    无全局状态：条目经 :meth:`register` 逐个注入（内置工具经
    :func:`register_builtin_tools` 工厂装配）；同名重复注册后写覆盖
    前写（与 ChannelRouter.register_adapter 同语义——热装配覆盖面）。
    """

    def __init__(self) -> None:
        """初始化注册表（空表）。"""
        self._entries: dict[str, _ToolEntry] = {}

    # ------------------------------------------------------------------
    # 注册 / 列举
    # ------------------------------------------------------------------

    def register(self, spec: ToolSpecModel, executor: ToolExecutor) -> None:
        """注册工具（同名后写覆盖前写——装配面语义）。

        Args:
            spec: 工具规格（name 唯一键）。
            executor: 执行器（异步——参数 dict → 结果 dict）。
        """
        replaced = spec.name in self._entries
        self._entries[spec.name] = _ToolEntry(spec, executor)
        if replaced:
            logger.info("工具已覆盖注册: {}", spec.name)
        else:
            logger.info("工具已注册: {}", spec.name)

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名列表（注册序——观测面）。"""
        return list(self._entries)

    def list_specs(self) -> list[ToolDefinitionModel]:
        """列举全部工具的 OpenAI tool 定义（chat 请求 ``tools`` 面）。

        Returns:
            定义列表（注册序；每项对应 wire 形态
            ``{"type": "function", "function": {...}}`` 的模型无关形态——
            序列化包裹见 llm_models.build_chat_request_body）。
        """
        return [
            ToolDefinitionModel(
                name=entry.spec.name,
                description=entry.spec.description,
                parameters=entry.spec.args_schema,
            )
            for entry in self._entries.values()
        ]

    # ------------------------------------------------------------------
    # 执行（恒不抛异常）
    # ------------------------------------------------------------------

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResultModel:
        """执行单次工具调用（寻址 → 校验 → 执行 → 计时；**恒不抛异常**）。

        Args:
            name: 工具名。
            arguments: 调用参数（None = 空参；非 dict 形态由调用方边界
                保证——LLM 响应解析已归一 JSON 对象）。

        Returns:
            执行结果（ok=False + error 携带人读原因——未知工具 / 参数
            校验失败 / 执行器异常三类失败均归一，不上抛）。
        """
        started_ms = now_ms()
        entry = self._entries.get(name)
        if entry is None:
            logger.debug("未注册工具（可用清单仅排障面，不进模型上下文）: name={}, available={}", name, self.tool_names)
            return self._failure(name, started_ms, f"未注册工具: {name}")
        args = arguments if arguments is not None else {}
        validated_args = self._validate_args(entry, args)
        if validated_args is None:
            return self._failure(name, started_ms, f"工具 {name} 参数校验失败")
        try:
            result = await entry.executor(validated_args)
        except BusinessException as exc:
            return self._failure(name, started_ms, f"{exc.code}: {exc.message}")
        except Exception as exc:  # executor 异常隔离——归一 ok=False，不上抛
            logger.opt(exception=True).warning("工具执行器异常（已隔离）: tool={}", name)
            return self._failure(name, started_ms, f"工具执行器异常: {exc}")
        return ToolResultModel(
            tool=name,
            ok=True,
            result=result,
            latency_ms=now_ms() - started_ms,
        )

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """执行单次工具调用（请求模型形态——duty 轮消费面）。

        Args:
            call: 执行请求（工具名 + 参数 dict）。

        Returns:
            执行结果（契约同 :meth:`execute`）。
        """
        return await self.execute(call.name, call.arguments)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _validate_args(self, entry: _ToolEntry, args: dict[str, Any]) -> dict[str, Any] | None:
        """参数校验（args_model 存在时经 Model Class 归一 + 默认值物化）。

        Args:
            entry: 注册表条目。
            args: 原始参数 dict。

        Returns:
            校验通过并物化默认值后的参数 dict；校验失败返回 None
            （错误细节已落日志——结果面收敛为统一失败文案）。
        """
        args_model = entry.spec.args_model
        if args_model is None:
            return args
        try:
            validated: BaseModel = args_model.model_validate(args)
        except (ValidationError, TypeError) as exc:
            # 约束与值类型不相容（如混合类型 Literal + pattern）时 pydantic
            # 抛裸 TypeError；args 已由调用方保证是 dict，故此处只可能来自
            # 约束不兼容——与校验失败同一受控路径。
            logger.warning("工具参数校验失败: tool={}, errors={}", entry.spec.name, str(exc)[:300])
            return None
        return validated.model_dump()

    @staticmethod
    def _failure(name: str, started_ms: int, error: str) -> ToolResultModel:
        """失败结果归一（时延含失败路径耗时）。

        Args:
            name: 工具名。
            started_ms: 执行起始 epoch 毫秒。
            error: 人读失败原因。

        Returns:
            ok=False 的执行结果。
        """
        return ToolResultModel(
            tool=name,
            ok=False,
            error=error,
            latency_ms=now_ms() - started_ms,
        )


# =========================================================================== []
# 内置工具（工厂注册——依赖构造注入）
# =========================================================================== []


def build_current_time_tool(
    *, clock: Callable[[], int] | None = None
) -> tuple[ToolSpecModel, ToolExecutor]:
    """构建 get_current_time 工具（epoch 毫秒 + naive 本地格式化时间）。

    Args:
        clock: 时钟注入（epoch 毫秒；None = core.now_ms——测试可控）。

    Returns:
        ``(spec, executor)`` 二元组（经 ``registry.register`` 装配）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（无参数；时钟读取 + 本地格式化）。"""
        current_ms = clock() if clock is not None else now_ms()
        local_time = datetime.fromtimestamp(current_ms / 1000).strftime(_LOCAL_TIME_FORMAT)
        return {"epoch_ms": current_ms, "local_time": local_time}

    spec = ToolSpecModel(
        name=CURRENT_TIME_TOOL_NAME,
        description="获取当前时间（epoch 毫秒 + 本地格式化时间）。",
        args_schema=_EMPTY_OBJECT_SCHEMA,
        args_model=GetCurrentTimeArgsModel,
    )
    return spec, _execute


def build_search_memory_tool(
    search_service: MemorySearchFace,
) -> tuple[ToolSpecModel, ToolExecutor]:
    """构建 search_memory 工具（委托注入的检索服务；人格作用域 contextvar）。

    人格作用域契约：执行时读 ``persona_id_var``（duty 轮绑定的人格上下文
    自动携带）；缺失 / 非数值（索引面为 BigInt）→ BusinessException →
    registry 归一 ok=False。

    Args:
        search_service: 记忆检索服务（结构化协议——真实
            MemorySearchService / 测试 fake 均可）。

    Returns:
        ``(spec, executor)`` 二元组（经 ``registry.register`` 装配）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（参数已经 registry 校验物化——query / limit / mode 直读）。"""
        persona_scope = persona_id_var.get()
        if persona_scope is None:
            raise BusinessException(
                code=ErrorCode.PERSONA_CONTEXT_REQUIRED,
                message="search_memory 缺少人格上下文（调用方须先绑定人格上下文）",
            )
        try:
            numeric_persona = int(persona_scope)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="人格上下文非数值主键，无法进入记忆检索隔离谓词",
                details={"persona_id": persona_scope},
            ) from exc
        query = MemorySearchQueryDTO(
            persona_id=numeric_persona,
            query=str(args["query"]),
            mode=MemorySearchMode(str(args.get("mode", "keyword"))),
            limit=int(args.get("limit", 5)),
        )
        results = await search_service.search(query)
        return {
            "count": len(results),
            "results": [result.model_dump() for result in results],
        }

    spec = ToolSpecModel(
        name=SEARCH_MEMORY_TOOL_NAME,
        description="检索当前人格的记忆（对话 / 日记 / 备忘——按相关度返回片段）。",
        args_schema=_SEARCH_MEMORY_SCHEMA,
        args_model=SearchMemoryArgsModel,
    )
    return spec, _execute


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    search_service: MemorySearchFace | None = None,
    clock: Callable[[], int] | None = None,
) -> None:
    """注册内置工具集（依赖构造注入——按可用性收缩工具面）。

    装配矩阵（文档化决策）：``get_current_time`` 恒注册（零依赖）；
    ``search_memory`` 仅在检索服务注入时注册（未注入 = 该工具不在
    LLM 工具面出现——模型无从调用，duty 轮照常运行）。

    Args:
        registry: 工具注册表（装配目标）。
        search_service: 记忆检索服务（None = 不注册 search_memory）。
        clock: 时钟注入（None = core.now_ms）。
    """
    time_spec, time_executor = build_current_time_tool(clock=clock)
    registry.register(time_spec, time_executor)
    if search_service is not None:
        search_spec, search_executor = build_search_memory_tool(search_service)
        registry.register(search_spec, search_executor)
