"""LLM 注册表管理服务——config.yaml ``llm`` 段写路径（/api/v1/models 管理面后端）。

本模块是 :class:`packages.harness.config.section_admin.ConfigSectionAdmin`
共享基座的 **LLM 专属子类**：基座持有节无关机械（写锁串行化 / 原子持久化
+ 备份轮换 / 热重建 + 失败回滚编排——见该模块 docstring 的缝契约），
本模块只承载 LLM 域语义：

    - 条目 CRUD（唯一性 409 / 存在性 404 / 删除角色绑定守卫 409——
      操作特定的条目级校验在各突变方法的锁窗口内先行完成）；
    - 角色绑定面（五角色读取 + 解析状态合并）；
    - 读取脱敏（条目只回 ``api_key_env`` 变量名，永不回密钥值）；
    - ``llm`` 段序列化（Model Class ↔ yaml 段 dict 无损回读）。

LLM 子类的缝实现一览::

    ``_section_key``               → ``"llm"``
    ``_current_section``           → ``loader.runtime_llm_config``
    ``_apply_section_to_runtime``  → ``loader.apply_llm_config_to_runtime``
    ``_render_section``            → ``_llm_section``（本模块）
    ``_validate_section``          → 空体（见该方法 docstring 的域决策）
    ``_rebuild_from_disk``         → ``loader.load_config_yaml(path=...)``
    ``_resolve_yaml_path``         → ``loader.get_loaded_config_yaml_path()``

链路顺序（单一真相，文档化于 docs/backend/llm.md 注册表管理一节）::

    校验（DTO 层：base_url http(s) / 非空 / extra=forbid；
          服务层：条目名唯一 409 / 存在性 404 / 删除角色绑定守卫 409）
        ↓ 构造新 LlmConfig（不可变重建——绝不原地修改运行时对象）
        ↓ 原子持久化（基座：写前备份 config.yaml.bak-<epoch-ms> → 临时文件
          写入 + fsync → os.replace 原子换名；失败清理临时文件）
        ↓ 热重建（load_config_yaml 重读磁盘——证明落盘内容可加载，
          并整体换血运行时：runtime_llm_config + 解析器 + 回退链）
        ↓ 重建失败 → apply_llm_config_to_runtime(写前快照) 回滚内存
          注册表 + BusinessException（磁盘保留新内容与 .bak 写前备份）

设计决策（文档化）：

    - **config.yaml 是 LLM 注册表唯一真源**：本服务只读写 config.yaml
      的 ``llm`` 段（其余顶层键原样保留）；``.env`` 与
      ``extensions_config.json`` 永不被触达；
    - **密钥不变式**：注册表只携带 ``api_key_env``（环境变量名）——
      写路径不接受也不产出任何密钥值；
    - **持久化目标 = 进程实际加载的文件**：路径经
      ``loader.get_loaded_config_yaml_path()`` 解析（启动期
      ``config_dir`` 注入与写路径天然一致）；
    - **备份轮换**：每次成功持久化生成 ``config.yaml.bak-<epoch-ms>``
      （写前内容整文件备份），保留最新 ``REGISTRY_BACKUP_KEEP`` 份，
      更旧的自动清理（轮换策略：按文件名字典序 == epoch-ms 数值序）；
    - **回滚语义**：仅回滚**内存**注册表（磁盘保留新内容——写已成功，
      失败在重建；.bak 保留写前内容供人工恢复，下次成功写自愈覆盖）；
    - **写串行化**：``asyncio.Lock`` 串行化全部突变（单事件循环内
      校验→持久化→重建为原子窗口，杜绝并发写相互覆盖丢失条目）；
    - **阻塞 I/O 下沉**：文件读写 / 备份 / 换名全部经
      ``run_blocking_ops``（基座持有；YAML 序列化为纯内存计算，留在
      事件循环）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from loguru import logger

from packages.harness.config import loader as config_loader
from packages.harness.config.loader import (
    apply_llm_config_to_runtime,
    get_loaded_config_yaml_path,
    load_config_yaml,
)
from packages.harness.config.model_entry import SUPPORTED_MODEL_ROLES, LlmModelEntry
from packages.harness.config.model_resolver import ModelResolver, get_model_resolver
from packages.harness.config.models import LlmConfig, MultimodalFallbackConfig
from packages.harness.config.registry_admin_dtos import (
    ModelEntryCreateDTO,
    ModelEntryMaskedDTO,
    ModelEntryUpdateDTO,
    RegistryStateDTO,
    RoleBindingDTO,
)
from packages.harness.config.section_admin import (
    DEFAULT_BACKUP_KEEP,
    ConfigSectionAdmin,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "RegistryAdminService",
    "REGISTRY_BACKUP_KEEP",
]


#: 备份保留份数（轮换上限——超出即清理最旧备份；值单一真源为基座默认）。
REGISTRY_BACKUP_KEEP: Final[int] = DEFAULT_BACKUP_KEEP


# ---------------------------------------------------------------------------
# YAML 序列化（llm 段 → config.yaml 文本；与 parse_model_entry 输入键对齐）
# ---------------------------------------------------------------------------


def _entry_to_section(entry: LlmModelEntry) -> dict[str, Any]:
    """单条目 → llm.models 列表项 dict（键名与 parse_model_entry 对齐）。

    Args:
        entry: 模型条目。

    Returns:
        可经 :func:`parse_model_entry` 无损回读的 dict（concurrency 为
        None 时省略——回落全局默认语义）。
    """
    payload: dict[str, Any] = {
        "name": entry.name,
        "provider": entry.provider,
        "base_url": entry.base_url,
        "api_key_env": entry.api_key_env,
        "model": entry.model,
        "roles": [role.value for role in entry.roles],
        "multimodal": entry.multimodal,
    }
    if entry.concurrency is not None:
        payload["concurrency"] = entry.concurrency
    return payload


def _fallback_to_section(fallback: MultimodalFallbackConfig) -> dict[str, Any]:
    """多模态回退链 → llm.multimodal_fallback 段 dict。

    Args:
        fallback: 回退链配置。

    Returns:
        可经 ``MultimodalFallbackConfig.model_validate`` 无损回读的 dict
        （未配置级为 None）。
    """
    return {
        "local": _entry_to_section(fallback.local) if fallback.local is not None else None,
        "remote": _entry_to_section(fallback.remote) if fallback.remote is not None else None,
    }


def _llm_section(config: LlmConfig) -> dict[str, Any]:
    """LlmConfig → ``llm`` 段 dict（注册表 / 显式绑定 / 回退链全量）。

    Args:
        config: 待序列化的 LlmConfig。

    Returns:
        可经 loader ``_parse_llm_section`` 无损回读的段 dict。
    """
    return {
        "models": [_entry_to_section(entry) for entry in config.models],
        "role_bindings": dict(config.role_bindings),
        "multimodal_fallback": _fallback_to_section(config.multimodal_fallback),
    }


# ---------------------------------------------------------------------------
# 服务（ConfigSectionAdmin 的 LLM 子类）
# ---------------------------------------------------------------------------


class RegistryAdminService(ConfigSectionAdmin[LlmConfig]):
    """LLM 注册表管理服务（读脱敏 / 写原子持久化 + 热重建 + 失败回滚）。

    无状态读写共享 loader 运行时（``runtime_llm_config`` / 全局解析器）；
    写突变经基座 ``asyncio.Lock`` 串行化（校验→持久化→重建原子窗口）。
    """

    def __init__(self) -> None:
        """初始化服务（基座写锁 + LLM 注册表备份轮换参数）。"""
        super().__init__(backup_keep=REGISTRY_BACKUP_KEEP)

    # ------------------------------------------------------------------
    # 基座缝实现（llm 段形态与 loader 运行时挂点）
    # ------------------------------------------------------------------

    def _section_key(self) -> str:
        """本管理面持有的 config.yaml 顶层键（LLM 注册表 = ``llm``）。"""
        return "llm"

    def _current_section(self) -> LlmConfig:
        """读取内存运行时当前节（loader ``runtime_llm_config`` 全局真源）。"""
        return config_loader.runtime_llm_config

    def _apply_section_to_runtime(self, section: LlmConfig) -> None:
        """把节整体应用到运行时（loader 最小公开重建入口——回滚路径）。"""
        apply_llm_config_to_runtime(section)

    def _render_section(self, section: LlmConfig) -> dict[str, Any]:
        """LlmConfig → ``llm`` 段 dict（无损回读序列化）。"""
        return _llm_section(section)

    def _validate_section(self, section: LlmConfig) -> None:
        """节级校验钩子——LLM 域决策：空体。

        条目级校验（唯一性 409 / 存在性 404 / 删除守卫 409）在各突变
        方法的锁窗口内先行完成（错误面携带操作上下文）；跨操作的节级
        不变式（重名条目 / 悬挂显式绑定）在本域为**告警容错**语义
        （解析器 ``rebuild`` 告警并保持先注册者，见 model_resolver），
        不构成拒绝持久化的闸门——故本钩子无额外校验。
        """
        return None

    def _rebuild_from_disk(self, yaml_path: Path) -> None:
        """热重建钩子（load_config_yaml 重读磁盘——整体换血运行时）。"""
        load_config_yaml(path=yaml_path)

    def _resolve_yaml_path(self) -> Path:
        """默认持久化目标（进程实际加载的 config.yaml——写路径一致性）。"""
        return get_loaded_config_yaml_path()

    # ------------------------------------------------------------------
    # 读取面（脱敏）
    # ------------------------------------------------------------------

    def _mask_entry(self, entry: LlmModelEntry, resolver: ModelResolver) -> ModelEntryMaskedDTO:
        """单条目 → 脱敏 DTO（含解析状态——解析到本条目的角色集）。

        Args:
            entry: 模型条目。
            resolver: 全局模型解析器（解析状态真源）。

        Returns:
            脱敏条目 DTO（只携带 api_key_env 变量名，永不携带密钥值）。
        """
        resolved_roles = sorted(
            role
            for role in SUPPORTED_MODEL_ROLES
            if (bound := resolver.resolve_optional(role)) is not None and bound.name == entry.name
        )
        return ModelEntryMaskedDTO(
            name=entry.name,
            provider=entry.provider,
            base_url=entry.base_url,
            api_key_env=entry.api_key_env,
            model=entry.model,
            roles=[role.value for role in entry.roles],
            resolved_roles=resolved_roles,
            concurrency=entry.concurrency,
            multimodal=entry.multimodal,
        )

    async def state(self) -> RegistryStateDTO:
        """读取当前注册表状态（脱敏 + 解析状态 + 显式绑定）。

        Returns:
            注册表状态 DTO（条目声明顺序）。
        """
        current = self._current_section()
        resolver = get_model_resolver()
        return RegistryStateDTO(
            entries=[self._mask_entry(entry, resolver) for entry in current.models],
            role_bindings=dict(current.role_bindings),
        )

    async def roles(self) -> list[RoleBindingDTO]:
        """读取五角色绑定面（chat / duty / dream / look_at / embed）。

        Returns:
            角色绑定行列表（角色名字典序；bound = 显式绑定或条目声明
            任一命中）。
        """
        resolver = get_model_resolver()
        bindings: list[RoleBindingDTO] = []
        for role in sorted(SUPPORTED_MODEL_ROLES):
            entry = resolver.resolve_optional(role)
            bindings.append(
                RoleBindingDTO(
                    role=role,
                    bound=entry is not None,
                    entry_name=entry.name if entry is not None else None,
                )
            )
        return bindings

    # ------------------------------------------------------------------
    # 写入面（原子持久化 + 热重建 + 失败回滚——机械在基座）
    # ------------------------------------------------------------------

    def _roles_bound_to(self, name: str) -> set[str]:
        """解析当前绑定到指定条目的角色集（显式绑定 + 条目声明合并）。

        Args:
            name: 条目名。

        Returns:
            角色名集合（删除守卫的判据真源——与解析器裁决一致）。
        """
        resolver = get_model_resolver()
        return {
            role
            for role in SUPPORTED_MODEL_ROLES
            if (bound := resolver.resolve_optional(role)) is not None and bound.name == name
        }

    async def create_entry(
        self, req: ModelEntryCreateDTO, *, yaml_path: Path | None = None
    ) -> RegistryStateDTO:
        """创建模型条目（唯一性校验 → 原子持久化 → 热重建）。

        Args:
            req: 创建请求（DTO 层已完成 http(s) / 非空 / 角色值域校验）。
            yaml_path: 目标 config.yaml（None = 进程实际加载路径）。

        Returns:
            写后注册表状态（脱敏）。

        Raises:
            BusinessException: 条目名已存在（``CONFLICT`` → 409）；持久化
                I/O 失败（``INTERNAL_ERROR`` → 500）；热重建失败
                （``CONFIG_INVALID`` → 422，内存已回滚写前快照）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            if any(entry.name == req.name for entry in current.models):
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message=f"模型条目名已存在: {req.name}",
                    details={"name": req.name},
                )
            entry = LlmModelEntry(
                name=req.name,
                provider=req.provider,
                base_url=req.base_url,
                api_key_env=req.api_key_env,
                model=req.model,
                roles=list(req.roles),
                concurrency=req.concurrency,
                multimodal=req.multimodal,
            )
            new_config = LlmConfig(
                models=[*current.models, entry],
                role_bindings=dict(current.role_bindings),
                multimodal_fallback=current.multimodal_fallback.model_copy(deep=True),
            )
            logger.info("注册表新增条目: name={}, path={}", req.name, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            # 状态快照在写锁内读取并返回——锁外读会与下一次写突变的
            # 校验→持久化窗口交错（读到半更新视图）
            return await self.state()

    async def update_entry(
        self, name: str, req: ModelEntryUpdateDTO, *, yaml_path: Path | None = None
    ) -> RegistryStateDTO:
        """部分更新模型条目（name 不可变；仅出现字段生效）。

        Args:
            name: 目标条目名（路径参数）。
            req: 更新请求（逐字段 null 语义见 DTO docstring）。
            yaml_path: 目标 config.yaml（None = 进程实际加载路径）。

        Returns:
            写后注册表状态（脱敏）。

        Raises:
            BusinessException: 条目不存在（``NOT_FOUND`` → 404）；持久化
                I/O 失败（``INTERNAL_ERROR``）；热重建失败
                （``CONFIG_INVALID``，内存已回滚写前快照）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            entry = next((item for item in current.models if item.name == name), None)
            if entry is None:
                raise BusinessException(
                    code=ErrorCode.NOT_FOUND,
                    message=f"模型条目不存在: {name}",
                    details={"name": name, "known_entries": [item.name for item in current.models]},
                )
            # 全量重校验构造（model_copy(update=) 绕过校验——不用）：
            # DTO 已校验提供值，merge 后经 model_validate 复核条目契约。
            merged: dict[str, Any] = dict(entry.model_dump())
            if req.provider is not None:
                merged["provider"] = req.provider
            if req.base_url is not None:
                merged["base_url"] = req.base_url
            if req.api_key_env is not None:
                merged["api_key_env"] = req.api_key_env
            if req.model is not None:
                merged["model"] = req.model
            if req.roles is not None:
                merged["roles"] = [role.value for role in req.roles]
            if req.multimodal is not None:
                merged["multimodal"] = req.multimodal
            if "concurrency" in req.model_fields_set:
                merged["concurrency"] = req.concurrency
            updated = LlmModelEntry.model_validate(merged)
            new_config = LlmConfig(
                models=[updated if item.name == name else item for item in current.models],
                role_bindings=dict(current.role_bindings),
                multimodal_fallback=current.multimodal_fallback.model_copy(deep=True),
            )
            logger.info("注册表更新条目: name={}, path={}", name, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            # 状态快照在写锁内读取并返回——锁外读会与下一次写突变的
            # 校验→持久化窗口交错（读到半更新视图）
            return await self.state()

    async def delete_entry(self, name: str, *, yaml_path: Path | None = None) -> RegistryStateDTO:
        """删除模型条目（角色绑定守卫：仍承接任一角色的条目禁止删除）。

        两步操作约定（删除守卫的解除路径）：条目**声明**的角色经
        ``PUT /entries/{name}`` 置 ``roles: []`` 解绑；**显式
        role_bindings** 指向的条目须先在 config.yaml 手工移除该绑定
        （本管理面不提供显式绑定编辑端点）。

        Args:
            name: 目标条目名（路径参数）。
            yaml_path: 目标 config.yaml（None = 进程实际加载路径）。

        Returns:
            写后注册表状态（脱敏）。

        Raises:
            BusinessException: 条目不存在（``NOT_FOUND`` → 404）；条目仍
                绑定角色（``CONFLICT`` → 409，details 携带 bound_roles 与
                两步指引）；持久化 I/O 失败（``INTERNAL_ERROR``）；热重建
                失败（``CONFIG_INVALID``，内存已回滚写前快照）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            entry = next((item for item in current.models if item.name == name), None)
            if entry is None:
                raise BusinessException(
                    code=ErrorCode.NOT_FOUND,
                    message=f"模型条目不存在: {name}",
                    details={"name": name, "known_entries": [item.name for item in current.models]},
                )
            bound_roles = self._roles_bound_to(name)
            if bound_roles:
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message=f"模型条目仍绑定角色，禁止删除（先解绑再删除）: {name}",
                    details={
                        "name": name,
                        "bound_roles": sorted(bound_roles),
                        "fix": (
                            "两步操作：PUT /api/v1/models/entries/{name} 将 roles 置空"
                            "（条目声明角色）；显式 role_bindings 指向的条目需先在"
                            " config.yaml 移除该绑定"
                        ),
                    },
                )
            new_config = LlmConfig(
                models=[item for item in current.models if item.name != name],
                role_bindings=dict(current.role_bindings),
                multimodal_fallback=current.multimodal_fallback.model_copy(deep=True),
            )
            logger.info("注册表删除条目: name={}, path={}", name, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            # 状态快照在写锁内读取并返回——锁外读会与下一次写突变的
            # 校验→持久化窗口交错（读到半更新视图）
            return await self.state()
