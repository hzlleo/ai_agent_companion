"""harness/search 域 Model Classes——查询 / 结果 / 响应 / 用量事件（Unit 文件）。

契约分层：

    - :class:`SearchQueryModel`   —— 单次搜索查询（query 1-256 字符 +
      可选条数覆盖——registry 工具面 / 服务面共用校验锚点）；
    - :class:`SearchResultModel`  —— 单条结果（title / url / snippet /
      source_provider——两 provider 响应归一后的统一形态）；
    - :class:`SearchResponseModel`—— 单次搜索响应（结果列表 + 命中
      provider + 客户端侧时延）；
    - :class:`SearchUsageEvent` / :data:`SearchUsageEmitter`——客户端
      用量发射钩子（每次 provider 尝试恰发一事件——成功 / 失败均落；
      服务层闭包补 ts / persona / 条目身份后落 usage 行）；
    - :class:`SearchUsageFace`    —— usage 域消费面协议（结构化类型——
      真实 :class:`~packages.harness.usage.service.UsageService` 的
      ``query`` / ``create`` 天然满足）。

预算错误形态（服务层 details 面）：``SEARCH_BUDGET_EXCEEDED`` 的
details 恒为 ``{"used": int, "limit": int, "scope": "persona" | "global"}``
（persona 档附 ``persona_id``）——:class:`SearchBudgetErrorDetails`
是该 dict 的类型化真源。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from packages.harness.core.dtos import PageResponse
from packages.harness.usage.dtos import (
    UsageCreateDTO,
    UsageQueryDTO,
    UsageRecordDTO,
)

__all__ = [
    "SearchQueryModel",
    "SearchResultModel",
    "SearchResponseModel",
    "SearchUsageEvent",
    "SearchUsageEmitter",
    "SearchBudgetErrorDetails",
    "SearchUsageFace",
    "SearchServiceLike",
    "truncate_snippet",
]

#: 单条结果摘要的服务层截断上限（防超长 snippet 撑爆工具反馈面）。
_SNIPPET_MAX_CHARS: int = 500


class SearchQueryModel(BaseModel):
    """单次搜索查询（服务 / 工具面共用的入参校验锚点）。

    Attributes:
        query: 搜索词（1-256 字符——空白拒绝）。
        max_results: 返回条数覆盖（None = 条目 ``max_results`` 生效；
            1-20）。
    """

    model_config = {"extra": "forbid", "str_strip_whitespace": False}

    query: str = Field(min_length=1, max_length=256, description="搜索词（1-256 字符）")
    max_results: int | None = Field(
        default=None, ge=1, le=20, description="返回条数覆盖（None=条目缺省）"
    )


class SearchResultModel(BaseModel):
    """单条搜索结果（两 provider 响应归一后的统一形态）。

    Attributes:
        title: 结果标题。
        url: 结果链接。
        snippet: 内容摘要（服务层截断到 500 字符）。
        source_provider: 命中 provider 的条目名（注册表条目标识）。
    """

    title: str = Field(default="", description="结果标题")
    url: str = Field(default="", description="结果链接")
    snippet: str = Field(default="", description="内容摘要（截断到 500 字符）")
    source_provider: str = Field(default="", description="命中 provider 条目名")


class SearchResponseModel(BaseModel):
    """单次搜索响应（服务面返回 / 工具面消费）。

    Attributes:
        results: 结果列表（provider 返回序；条数 ≤ 请求上限）。
        provider_used: 命中 provider 的条目名（failover 链实际服务者）。
        latency_ms: 客户端侧往返时延（epoch 毫秒差）。
    """

    results: list[SearchResultModel] = Field(default_factory=list, description="结果列表")
    provider_used: str = Field(default="", description="命中 provider 条目名")
    latency_ms: int = Field(default=0, ge=0, description="客户端侧时延（毫秒）")


class SearchUsageEvent(BaseModel):
    """单次 provider 尝试的用量事件（客户端 → 服务层发射钩子载荷）。

    每次尝试恰发一事件（成功 / 失败均发——与 llm 网关「成功 / 失败
    路径均落账」同语义）；条目身份（name / kind）与人格上下文由服务层
    闭包补充，客户端只携带自身可观测面。

    Attributes:
        status: 尝试结果（success / error）。
        latency_ms: 本次尝试端到端时延（毫秒，含失败耗时）。
        error_code: 失败错误码（成功为 None）。
    """

    status: Literal["success", "error"] = Field(description="尝试结果")
    latency_ms: int = Field(default=0, ge=0, description="本次尝试时延（毫秒）")
    error_code: str | None = Field(default=None, description="失败错误码（成功为 None）")


#: 用量发射钩子契约——客户端每次尝试调用一次（异常由服务层闭包兜底）。
SearchUsageEmitter = Callable[[SearchUsageEvent], Awaitable[None]]


class SearchBudgetErrorDetails(BaseModel):
    """预算超限错误 details 面的类型化真源（SEARCH_BUDGET_EXCEEDED）。

    Attributes:
        used: 今日已用搜索调用数（含失败尝试行）。
        limit: 生效预算上限。
        scope: 预算档位（persona = 单人格档 / global = 全局档）。
        persona_id: 人格 ID（仅 scope=persona 携带）。
    """

    used: int = Field(ge=0, description="今日已用调用数")
    limit: int = Field(ge=1, description="生效预算上限")
    scope: Literal["persona", "global"] = Field(description="预算档位")
    persona_id: str | None = Field(default=None, description="人格 ID（persona 档携带）")


class SearchUsageFace(Protocol):
    """usage 域消费面协议（预算计数 + 用量落账的双方法契约）。

    结构化类型：真实
    :class:`~packages.harness.usage.service.UsageService` 天然满足（同签名
    ``query`` / ``create``）；测试注入 fake 同形态。
    """

    async def query(self, query: UsageQueryDTO) -> PageResponse[UsageRecordDTO]:
        """游标分页查询（预算计数的行来源）。

        Args:
            query: 查询 DTO（过滤面 + 游标 + 页大小）。

        Returns:
            分页响应（``next_cursor`` 非空表示还有更早记录）。
        """
        ...

    async def create(self, dto: UsageCreateDTO) -> UsageRecordDTO:
        """单条用量落账（purpose=search 行的写入口）。

        Args:
            dto: 创建 DTO。

        Returns:
            落库后的记录 DTO（含主键）。
        """
        ...


def truncate_snippet(snippet: str) -> str:
    """截断结果摘要到 500 字符（防超长 snippet 撑爆工具反馈面）。

    Args:
        snippet: 原始摘要文本。

    Returns:
        截断后的摘要（超长时截到 500 字符，无省略号后缀——机器消费面）。
    """
    return snippet[:_SNIPPET_MAX_CHARS]


class SearchServiceLike(Protocol):
    """搜索服务注入面协议（web_search 工具的依赖契约）。

    结构化类型：真实
    :class:`~packages.harness.search.search_service.SearchService` 天然
    满足（同签名 ``search``）；测试注入 fake 服务同形态。
    """

    async def search(
        self,
        query: str,
        *,
        persona_id: str | None = None,
        max_results: int | None = None,
    ) -> SearchResponseModel:
        """执行一次联网搜索（预算门禁 + failover 在实现内闭环）。

        Args:
            query: 搜索词。
            persona_id: 人格 ID（None = 读上下文变量）。
            max_results: 返回条数覆盖（None = 条目缺省）。

        Returns:
            归一响应。
        """
        ...
