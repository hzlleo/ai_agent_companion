"""harness/tools 域——原生工具调用 registry（Model Class Unit 文件）。

本模块是 tools 域的 Unit 文件（Model Classes 优先于裸 dict）：

    - :class:`ToolSpecModel`       —— 工具规格（name / description /
      参数 JSON schema + 可选参数校验 Model Class）；
    - :class:`ToolResultModel`     —— 工具执行结果（ok / result JSON /
      error / 时延——executor 异常隔离的归一终态）；
    - :class:`ToolCallRequestModel` —— 执行请求（工具名 + 参数 dict——
      duty 轮把 LLM 响应的 :class:`ToolCallModel` 归一为本形态）；
    - 内置工具参数 Model Class：:class:`GetCurrentTimeArgsModel` /
      :class:`SearchMemoryArgsModel`（args 校验面——registry 执行前
      逐工具校验）；
    - :class:`MemorySearchFace`    —— 记忆检索服务注入面协议
      （结构化类型——真实 MemorySearchService 天然满足）。

执行器契约（registry 消费）：``Callable[[dict], Awaitable[dict]]``——
异步可调用、参数 dict 入 / 结果 dict 出；**无网络、无文件系统**（依赖
一律构造注入——本 chunk 内置工具零外部副作用；docker 沙盒执行器属
后续 chunk，registry API 保持 executor 无关）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.memory.retrieval_models import (
    MemorySearchQueryDTO,
    MemorySearchResultDTO,
)

__all__ = [
    "ToolExecutor",
    "ToolSpecModel",
    "ToolResultModel",
    "ToolCallRequestModel",
    "GetCurrentTimeArgsModel",
    "SearchMemoryArgsModel",
    "MemorySearchFace",
]

#: 工具执行器契约——参数 dict → 结果 dict（异步；异常由 registry 隔离）。
ToolExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolSpecModel(BaseModel):
    """工具规格（注册面——name / description / 参数 schema + 校验模型）。

    Attributes:
        name: 工具名（registry 内唯一——重复注册后写覆盖前写）。
        description: 工具描述（进入 LLM 请求 tools 面——告知模型何时调用）。
        args_schema: 参数 JSON Schema（OpenAI 兼容 ``function.parameters``
            形态）。
        args_model: 参数校验 Model Class（None = 不校验——registry 执行前
            经 ``model_validate`` 归一参数并物化默认值；校验失败 →
            ok=False 结果，不上抛）。
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str = Field(min_length=1, max_length=64, description="工具名（唯一标识）")
    description: str = Field(default="", description="工具描述（模型调用时机说明）")
    args_schema: ToolParametersSchema = Field(
        default_factory=ToolParametersSchema, description="参数 JSON Schema"
    )
    args_model: type[BaseModel] | None = Field(
        default=None, description="参数校验 Model Class（None = 不校验）"
    )


class ToolResultModel(BaseModel):
    """工具执行结果（registry 归一终态——恒不抛异常的执行契约）。

    Attributes:
        tool: 工具名（echo 回调用面）。
        ok: 是否成功（False = 未知工具 / 参数校验失败 / 执行器异常——
            三类失败均归一为本形态，不上抛）。
        result: 结果 JSON（成功时为执行器返回 dict；失败恒空 dict）。
        error: 失败原因（成功为 None；人读中文字符串）。
        latency_ms: 单次执行时延（epoch 毫秒差；含失败路径耗时）。
    """

    tool: str = Field(description="工具名（echo）")
    ok: bool = Field(default=True, description="是否成功")
    result: dict[str, Any] = Field(default_factory=dict, description="结果 JSON")
    error: str | None = Field(default=None, description="失败原因（成功为 None）")
    latency_ms: int = Field(default=0, ge=0, description="单次执行时延（毫秒）")


class ToolCallRequestModel(BaseModel):
    """工具执行请求（duty 轮 → registry 的调用归一形态）。

    Attributes:
        name: 工具名。
        arguments: 调用参数（JSON 对象——LLM 响应已解析 dict）。
    """

    name: str = Field(min_length=1, description="工具名")
    arguments: dict[str, Any] = Field(default_factory=dict, description="调用参数")


class GetCurrentTimeArgsModel(BaseModel):
    """get_current_time 参数面（无参数——空 schema 的校验锚点）。"""

    model_config = {"extra": "forbid"}


class SearchMemoryArgsModel(BaseModel):
    """search_memory 参数面（查询 / 条数上限 / 检索模式）。

    Attributes:
        query: 检索文本（keyword 模式 = 关键词；dense / hybrid = 待
            向量化查询文本）。
        limit: 返回条数上限（1-20，默认 5）。
        mode: 检索模式（默认 keyword——SQLite 回退存储零向量依赖；
            dense / hybrid 经注入的检索服务向量化）。
    """

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=512, description="检索文本")
    limit: int = Field(default=5, ge=1, le=20, description="返回条数上限（1-20）")
    mode: Literal["keyword", "dense", "hybrid"] = Field(
        default="keyword", description="检索模式（默认 keyword）"
    )


class MemorySearchFace(Protocol):
    """记忆检索服务注入面协议（search_memory 工具的依赖契约）。

    结构化类型：真实
    :class:`packages.harness.memory.retrieval.MemorySearchService` 天然
    满足（同签名 ``search``）；测试注入 fake 服务同形态。
    """

    async def search(self, query: MemorySearchQueryDTO) -> list[MemorySearchResultDTO]:
        """执行检索（人格隔离谓词由查询 DTO 的 persona_id 承载）。

        Args:
            query: 检索查询 DTO。

        Returns:
            结果 DTO 列表。
        """
        ...
