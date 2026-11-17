"""search 域 LLM 工具——``web_search``（duty 工具循环 + chat 工具循环共用）。

工具面契约：

    - 规格 ``web_search``，参数 ``{query: str 1-256}``（与
      :class:`WebSearchToolArgsModel` 对齐——registry 执行前校验物化）；
    - 执行器委托 :class:`~packages.harness.search.search_service.SearchService`
      （预算门禁 / failover 在服务内闭环——工具面保持薄）；人格作用域经
      ``persona_id_var`` 上下文由服务层读取（duty 轮 / 通道路由已绑定）；
    - 结果为紧凑中文文本块（``【N】标题 / 链接 / 摘要`` 逐行 + 空行分
      隔，top N 由条目 ``max_results`` 界定），同时携带 ``count`` 机器
      可读字段；
    - **恒不上抛**：服务异常（预算超限 / 全降级等）经 registry 隔离归
      一为 ``ok=False`` + 人读错误（registry 契约——与 search_memory
      的执行器同语义）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.search.search_models import SearchResponseModel, SearchServiceLike
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import ToolExecutor, ToolSpecModel

__all__ = [
    "WEB_SEARCH_TOOL_NAME",
    "WebSearchToolArgsModel",
    "build_web_search_tool",
    "register_web_search_tool",
    "format_search_results",
]

#: web_search 工具名（LLM 请求 tools 面标识——duty / chat 两循环共用）。
WEB_SEARCH_TOOL_NAME: str = "web_search"

#: web_search 参数 JSON Schema（与 WebSearchToolArgsModel 字段对齐）。
_WEB_SEARCH_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索词（1-256 字符——新闻 / 资料 / 事实核查等联网信息需求）",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
)


class WebSearchToolArgsModel(BaseModel):
    """web_search 参数面（查询词——registry 执行前校验锚点）。

    Attributes:
        query: 搜索词（1-256 字符）。
    """

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=256, description="搜索词")


def format_search_results(response: SearchResponseModel) -> str:
    """搜索响应 → 紧凑中文文本块（LLM 工具反馈面）。

    Args:
        response: 服务归一响应。

    Returns:
        逐结果「【N】标题 / 链接：URL / 摘要：snippet」块（空行分隔）；
        空结果渲染单行「（无搜索结果）」。
    """
    if not response.results:
        return "（无搜索结果）"
    blocks: list[str] = []
    for index, result in enumerate(response.results, start=1):
        lines = [f"【{index}】{result.title}", f"链接：{result.url}"]
        if result.snippet:
            lines.append(f"摘要：{result.snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_web_search_tool(
    service: SearchServiceLike,
) -> tuple[ToolSpecModel, ToolExecutor]:
    """构建 web_search 工具（规格 + 执行器二元组——镜像内置工具工厂范式）。

    Args:
        service: 搜索服务（结构化协议——真实 SearchService / 测试 fake
            均可）。

    Returns:
        ``(spec, executor)`` 二元组（经 ``registry.register`` 装配）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """执行（参数已经 registry 校验物化——query 直读；人格上下文由
        服务层经 persona_id_var 读取）。"""
        response = await service.search(str(args["query"]))
        return {
            "count": len(response.results),
            "provider": response.provider_used,
            "text": format_search_results(response),
        }

    spec = ToolSpecModel(
        name=WEB_SEARCH_TOOL_NAME,
        description="联网搜索最新信息（新闻、资料、事实核查等——返回标题/链接/摘要文本块）。",
        args_schema=_WEB_SEARCH_SCHEMA,
        args_model=WebSearchToolArgsModel,
    )
    return spec, _execute


def register_web_search_tool(registry: ToolRegistry, service: SearchServiceLike) -> None:
    """注册 web_search 工具到目标注册表（duty 注册表 / chat 注册表共用入口）。

    Args:
        registry: 工具注册表（装配目标——duty 分支在内置 + 沙盒 +
            look_at 工具后注册；chat 侧独立注册表仅含本工具）。
        service: 搜索服务（预算 / failover 在服务内闭环）。
    """
    spec, executor = build_web_search_tool(service)
    registry.register(spec, executor)
