"""LLM 注册表管理 admin 路由——/api/v1/models（P2 chunk 1 交付）。

    - GET    /api/v1/models/entries          —— 注册表条目（脱敏）+ 解析状态
    - GET    /api/v1/models/roles            —— 五角色绑定面（bound / unbound）
    - POST   /api/v1/models/entries          —— 创建条目（原子持久化 + 热重建）
    - PUT    /api/v1/models/entries/{name}   —— 部分更新（name 不可变）
    - DELETE /api/v1/models/entries/{name}   —— 删除（角色绑定守卫：先解绑再删除）

全局管理面（文档化决策）：LLM 注册表是进程级配置真源——**无人格上下文
要求**（非 PersonaMixin 数据，不解析目标人格 / 不绑定 persona
contextvar）。

写路径契约（链路见 harness.config.registry_admin 模块 docstring）：
config.yaml 是 LLM 注册表**唯一真源**——全部突变经「校验 → 原子持久化
（.bak 备份 + 临时文件 + os.replace）→ 热重建（重读磁盘）→ 失败回滚
内存」；``.env`` 与 ``extensions_config.json`` 不在本路由触达范围。
密钥不变式：条目只携带 ``api_key_env``（环境变量名）——请求面拒绝
``api_key`` 密钥字段（extra=forbid），响应面永不回密钥值。每次突变
返回写后注册表状态（脱敏）。
"""

from __future__ import annotations

from fastapi import APIRouter

from packages.harness.config.registry_admin import RegistryAdminService
from packages.harness.config.registry_admin_dtos import (
    ModelEntryCreateDTO,
    ModelEntryUpdateDTO,
    RegistryStateDTO,
    RoleBindingDTO,
)
from packages.harness.core.dtos import ResultDTO

__all__ = ["router", "registry_admin_service"]

router = APIRouter(prefix="/api/v1/models", tags=["models"])

#: 注册表管理服务实例（写突变经内部 asyncio.Lock 串行化——可安全共享）。
registry_admin_service = RegistryAdminService()


@router.get("/entries", response_model=ResultDTO[RegistryStateDTO])
async def list_entries() -> ResultDTO[RegistryStateDTO]:
    """读取当前注册表状态（条目脱敏 + 解析状态 + 显式绑定）。

    脱敏契约：条目只回 ``api_key_env`` 环境变量名（密钥值永不出现）；
    ``resolved_roles`` 为解析器当前实际解析到该条目的角色集（显式绑定
    与声明合并的裁决结果）；不含端点健康状态（管理面不探测端点）。

    Returns:
        注册表状态 DTO（条目声明顺序）。
    """
    return ResultDTO.ok(data=await registry_admin_service.state())


@router.get("/roles", response_model=ResultDTO[list[RoleBindingDTO]])
async def list_roles() -> ResultDTO[list[RoleBindingDTO]]:
    """读取五角色绑定面（chat / duty / dream / look_at / embed）。

    Returns:
        角色绑定行列表（角色名字典序；``bound=False`` 即未绑定——
        未绑定角色调用链路将以 ``MODEL_NOT_FOUND`` fail loud）。
    """
    return ResultDTO.ok(data=await registry_admin_service.roles())


@router.post("/entries", response_model=ResultDTO[RegistryStateDTO])
async def create_entry(req: ModelEntryCreateDTO) -> ResultDTO[RegistryStateDTO]:
    """创建模型条目（校验 → 原子持久化 config.yaml → 热重建）。

    Args:
        req: 创建请求（name 全局唯一；base_url 须 http(s)；model 非空；
            api_key_env 可选——只接受环境变量名）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目名已存在（``CONFLICT`` → 409）；持久化
            I/O 失败（``INTERNAL_ERROR`` → 500）；热重建失败
            （``CONFIG_INVALID`` → 422，内存已回滚写前快照）。
    """
    state = await registry_admin_service.create_entry(req)
    return ResultDTO.ok(data=state, message="模型条目已创建")


@router.put("/entries/{name}", response_model=ResultDTO[RegistryStateDTO])
async def update_entry(
    name: str,
    req: ModelEntryUpdateDTO,
) -> ResultDTO[RegistryStateDTO]:
    """部分更新模型条目（仅出现字段生效；name 不可变——请求体携带
    ``name`` 即 400）。

    持久化目标为进程实际加载的 config.yaml（启动期 ``config_dir`` 注入
    与写路径天然一致——见 ``loader.get_loaded_config_yaml_path``）。

    Args:
        name: 目标条目名（路径参数；不可变标识）。
        req: 更新请求（逐字段 null 语义见 DTO docstring——
            ``concurrency`` 显式 null = 重置全局默认）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目不存在（``NOT_FOUND`` → 404）；持久化 /
            重建失败（同创建端点错误面）。
    """
    state = await registry_admin_service.update_entry(name, req)
    return ResultDTO.ok(data=state, message="模型条目已更新")


@router.delete("/entries/{name}", response_model=ResultDTO[RegistryStateDTO])
async def delete_entry(
    name: str,
) -> ResultDTO[RegistryStateDTO]:
    """删除模型条目（角色绑定守卫：仍承接任一角色的条目禁止删除）。

    两步操作约定：先解绑（声明角色经 ``PUT /entries/{name}`` 置
    ``roles: []``；显式 ``role_bindings`` 指向须先在 config.yaml 手工
    移除该绑定——本管理面不提供显式绑定编辑端点），再 DELETE。

    Args:
        name: 目标条目名（路径参数）。

    Returns:
        写后注册表状态（脱敏）。

    Raises:
        BusinessException: 条目不存在（``NOT_FOUND`` → 404）；仍绑定角色
            （``CONFLICT`` → 409，details 携带 ``bound_roles`` 与两步
            指引）；持久化 / 重建失败（同创建端点错误面）。
    """
    state = await registry_admin_service.delete_entry(name)
    return ResultDTO.ok(data=state, message="模型条目已删除")
