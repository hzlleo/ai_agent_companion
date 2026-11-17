"""搜索 provider 注册表管理 admin 路由——/api/v1/search。

    - GET    /api/v1/search/providers          —— 注册表条目（脱敏）+ 运营子块
    - POST   /api/v1/search/providers          —— 创建条目（原子持久化 + 热重建）
    - PUT    /api/v1/search/providers/{name}   —— 部分更新（name 不可变）
    - DELETE /api/v1/search/providers/{name}   —— 删除（唯一 provider 守卫）
    - GET    /api/v1/search/usage-summary      —— 今日调用数 vs 预算

全局管理面（文档化决策，与 /api/v1/models 同口径）：搜索注册表是进程级
配置真源——**无人格上下文要求**（非 PersonaMixin 数据）。

写路径契约（链路见 harness.config.search_registry 模块 docstring）：
config.yaml 是搜索注册表**唯一真源**——全部突变经「校验 → 原子持久化
（.bak 备份 + 临时文件 + os.replace）→ 热重建（重读磁盘）→ 失败回滚
内存」。密钥不变式：条目只携带 ``api_key_env``（环境变量名）——请求面
拒绝密钥字段（extra=forbid），响应面永不回密钥值。每次突变返回写后
注册表状态（脱敏）。

usage-summary 口径：今日 = epoch 毫秒整除 86_400_000 对齐的 UTC 日桶
（与 SearchService 预算门禁 / usage 域 time_bucket=day 同口径）；按
``purpose=search`` 行计数（含失败行）；per-persona 行按 ``persona_id``
列聚合（usage 域无 persona 分组维度——本端点游标分页遍历后在应用层
聚合，页遍历设安全上限）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from packages.harness.config import search_registry
from packages.harness.config.search_registry import (
    SearchProviderCreateDTO,
    SearchProviderUpdateDTO,
    SearchRegistryAdminService,
    SearchRegistryStateDTO,
)
from packages.harness.core.dtos import ResultDTO
from packages.harness.core.time_utils import now_ms
from packages.harness.usage.dtos import UsageQueryDTO
from packages.harness.usage.models import UsagePurpose
from packages.harness.usage.service import UsageService

__all__ = [
    "router",
    "search_registry_admin_service",
    "SearchUsageSummaryDTO",
    "SearchPersonaUsageDTO",
    "summarize_search_usage",
]

router = APIRouter(prefix="/api/v1/search", tags=["search"])

#: 注册表管理服务实例（写突变经内部 asyncio.Lock 串行化——可安全共享）。
search_registry_admin_service = SearchRegistryAdminService()

#: 用量服务实例（无状态——Session 经 session_scope 每调用获取，可安全共享）。
usage_service = UsageService()

#: usage-summary 分页遍历的页大小（usage 域上限 200）。
_SUMMARY_PAGE_LIMIT: int = 200

#: usage-summary 分页遍历的页数安全上限（防异常膨胀记录集拖死端点）。
_SUMMARY_PAGE_CAP: int = 50


class SearchPersonaUsageDTO(BaseModel):
    """单人格今日搜索用量行（usage-summary 的 persona 明细）。

    Attributes:
        persona_id: 人格 ID（None = 系统调用行——无归属人格的搜索）。
        used: 今日搜索调用数（含失败行）。
        limit: 单人格预算上限（当前生效值）。
    """

    persona_id: str | None = Field(default=None, description="人格 ID（None=系统调用）")
    used: int = Field(default=0, ge=0, description="今日调用数")
    limit: int = Field(default=0, ge=0, description="单人格预算上限")


class SearchUsageSummaryDTO(BaseModel):
    """今日搜索用量总览（调用数 vs 预算——预算门禁的观测面）。

    Attributes:
        day_start_ms: 今日 UTC 日桶起点（epoch 毫秒——与预算门禁同口径）。
        global_used: 今日全人格调用数（含失败行）。
        global_limit: 全局预算上限（当前生效值）。
        persona_usage: per-persona 明细（used 降序；含系统调用行）。
    """

    day_start_ms: int = Field(description="今日 UTC 日桶起点（epoch 毫秒）")
    global_used: int = Field(default=0, ge=0, description="今日全人格调用数")
    global_limit: int = Field(default=0, ge=0, description="全局预算上限")
    persona_usage: list[SearchPersonaUsageDTO] = Field(
        default_factory=list, description="per-persona 明细（used 降序）"
    )


async def summarize_search_usage() -> SearchUsageSummaryDTO:
    """聚合今日（UTC 日桶）purpose=search 用量行 → 总览 DTO。

    游标分页遍历今日行（页大小 200 / 页数上限 50）后应用层聚合：
    全局计数 + persona_id 列分组计数。预算上限读运行时配置当前生效值
    （管理面 CRUD / env 调整后自动跟随）。

    Returns:
        总览 DTO（零记录返回全零 + 空明细）。
    """
    day_start_ms = (now_ms() // 86_400_000) * 86_400_000
    settings = search_registry.runtime_search_config.web_search
    persona_counts: dict[str | None, int] = {}
    global_used = 0
    cursor: str | None = None
    for _ in range(_SUMMARY_PAGE_CAP):
        page = await usage_service.query(
            UsageQueryDTO(
                purpose=UsagePurpose.SEARCH,
                start_ms=day_start_ms,
                cursor=cursor,
                limit=_SUMMARY_PAGE_LIMIT,
            )
        )
        global_used += len(page.items)
        for record in page.items:
            persona_counts[record.persona_id] = (
                persona_counts.get(record.persona_id, 0) + 1
            )
        cursor = page.next_cursor
        if not cursor:
            break
    rows = [
        SearchPersonaUsageDTO(
            persona_id=persona_id,
            used=used,
            limit=settings.daily_budget_per_persona,
        )
        for persona_id, used in persona_counts.items()
    ]
    rows.sort(key=lambda row: (-row.used, row.persona_id or ""))
    return SearchUsageSummaryDTO(
        day_start_ms=day_start_ms,
        global_used=global_used,
        global_limit=settings.daily_budget_global,
        persona_usage=rows,
    )


@router.get("/providers", response_model=ResultDTO[SearchRegistryStateDTO])
async def list_providers() -> ResultDTO[SearchRegistryStateDTO]:
    """读取当前搜索注册表状态（条目脱敏 + failover 链位次 + 运营子块）。

    脱敏契约：条目只回 ``api_key_env`` 环境变量名（密钥值永不出现）；
    ``chain_position`` 为 failover 链位次（列表序即尝试序）。

    Returns:
        注册表状态 DTO（条目声明顺序）。
    """
    return ResultDTO.ok(data=await search_registry_admin_service.state())


@router.post("/providers", response_model=ResultDTO[SearchRegistryStateDTO])
async def create_provider(
    req: SearchProviderCreateDTO,
) -> ResultDTO[SearchRegistryStateDTO]:
    """创建搜索 provider 条目（校验 → 原子持久化 config.yaml → 热重建）。

    Args:
        req: 创建请求（name 全局唯一；kind 值域 bocha/zhipu；base_url
            须 http(s) 或留空按 kind 缺省；api_key_env 可选——只接受
            环境变量名）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目名已存在（``CONFLICT`` → 409）；持久化
            I/O 失败（``INTERNAL_ERROR`` → 500）；热重建失败
            （``CONFIG_INVALID`` → 422，内存已回滚写前快照）。
    """
    state = await search_registry_admin_service.create_provider(req)
    return ResultDTO.ok(data=state, message="搜索 provider 已创建")


@router.put("/providers/{name}", response_model=ResultDTO[SearchRegistryStateDTO])
async def update_provider(
    name: str,
    req: SearchProviderUpdateDTO,
) -> ResultDTO[SearchRegistryStateDTO]:
    """部分更新搜索 provider 条目（仅出现字段生效；name 不可变——请求体
    携带 ``name`` 即 400）。

    Args:
        name: 目标条目名（路径参数；不可变标识）。
        req: 更新请求（逐字段 null 语义见 DTO docstring）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目不存在（``NOT_FOUND`` → 404）；持久化 /
            重建失败（同创建端点错误面）。
    """
    state = await search_registry_admin_service.update_provider(name, req)
    return ResultDTO.ok(data=state, message="搜索 provider 已更新")


@router.delete("/providers/{name}", response_model=ResultDTO[SearchRegistryStateDTO])
async def delete_provider(
    name: str,
) -> ResultDTO[SearchRegistryStateDTO]:
    """删除搜索 provider 条目（唯一 provider 守卫：能力启用时禁止清空）。

    两步操作约定（守卫解除路径）：先经 ``SEARCH_ENABLED=false`` /
    config.yaml 关闭能力，或先创建替代条目，再 DELETE。

    Args:
        name: 目标条目名（路径参数）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目不存在（``NOT_FOUND`` → 404）；能力启用下
            的最后一个条目（``CONFLICT`` → 409，details 携带两步指引）；
            持久化 / 重建失败（同创建端点错误面）。
    """
    state = await search_registry_admin_service.delete_provider(name)
    return ResultDTO.ok(data=state, message="搜索 provider 已删除")


@router.get("/usage-summary", response_model=ResultDTO[SearchUsageSummaryDTO])
async def search_usage_summary() -> ResultDTO[SearchUsageSummaryDTO]:
    """今日搜索用量总览（调用数 vs 预算——预算门禁观测面）。

    口径见模块 docstring：UTC 日桶（与门禁同轴）；计数含失败行；
    persona 明细按记录行 persona_id 聚合（None = 系统调用）。

    Returns:
        总览 DTO（零记录返回全零 + 空明细）。
    """
    return ResultDTO.ok(data=await summarize_search_usage())
