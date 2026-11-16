"""LLM 注册表管理服务——config.yaml 写路径（P2 chunk 1 管理面后端）。

链路顺序（单一真相，文档化于 docs/backend/llm.md 注册表管理一节）::

    校验（DTO 层：base_url http(s) / 非空 / extra=forbid；
          服务层：条目名唯一 409 / 存在性 404 / 删除角色绑定守卫 409）
        ↓ 构造新 LlmConfig（不可变重建——绝不原地修改运行时对象）
        ↓ 原子持久化（写前备份 config.yaml.bak-<epoch-ms> → 临时文件
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
      ``run_blocking_ops``（YAML 序列化为纯内存计算，留在事件循环）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Final

import yaml
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
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.core.time_utils import now_ms

__all__ = [
    "RegistryAdminService",
    "REGISTRY_BACKUP_KEEP",
    "render_config_yaml_text",
]


#: 备份保留份数（轮换上限——超出即清理最旧备份）。
REGISTRY_BACKUP_KEEP: Final[int] = 10


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


def render_config_yaml_text(
    config: LlmConfig, raw_existing: dict[str, Any] | None
) -> str:
    """渲染完整 config.yaml 文本（保留 ``llm`` 段之外的未知顶层键）。

    Args:
        config: 新的 LlmConfig（落盘的 ``llm`` 段真源）。
        raw_existing: 既有文件的原始顶层 dict（None = 无既有文件）；
            其 ``llm`` 键被整体替换，其余键原样保留（向前兼容后续
            顶层配置段）。

    Returns:
        完整 config.yaml 文本（UTF-8 YAML，键序稳定）。
    """
    data: dict[str, Any] = dict(raw_existing) if raw_existing else {}
    data["llm"] = _llm_section(config)
    text: str = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return text


# ---------------------------------------------------------------------------
# 阻塞 I/O（经 run_blocking_ops 下沉；全部同步函数）
# ---------------------------------------------------------------------------


def _read_raw_yaml_blocking(yaml_path: Path) -> dict[str, Any] | None:
    """读取既有 config.yaml 顶层 dict（阻塞形态——经线程池下沉）。

    Args:
        yaml_path: config.yaml 路径。

    Returns:
        顶层 dict（文件缺失 = None；空文件 = 空 dict）。

    Raises:
        BusinessException: 文件存在但不可解析 / 顶层非 mapping
            （``CONFIG_INVALID``——fail loud，绝不覆盖不可审计的既有文件）。
    """
    if not yaml_path.is_file():
        return None
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=f"既有 config.yaml 不可解析，拒绝覆写: {yaml_path}",
            details={"path": str(yaml_path), "reason": str(exc)},
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=f"既有 config.yaml 顶层结构非 mapping，拒绝覆写: {yaml_path}",
            details={"path": str(yaml_path), "got": type(data).__name__},
        )
    return data


def _prune_backups_blocking(yaml_path: Path) -> None:
    """备份轮换：保留最新 ``REGISTRY_BACKUP_KEEP`` 份，清理更旧备份。

    文件名字典序 == epoch-ms 数值序（当前纪元毫秒恒 13 位定长），
    排序后裁尾即删除最旧。

    Args:
        yaml_path: config.yaml 路径（备份与其同目录同名前缀）。
    """
    backups = sorted(
        path for path in yaml_path.parent.glob(f"{yaml_path.name}.bak-*") if path.is_file()
    )
    for stale in backups[:-REGISTRY_BACKUP_KEEP] if len(backups) > REGISTRY_BACKUP_KEEP else []:
        stale.unlink(missing_ok=True)
        logger.debug("已清理过期注册表备份: {}", stale)


def _atomic_persist_blocking(yaml_path: Path, content: str) -> Path | None:
    """原子持久化 config.yaml（阻塞形态——经线程池下沉）。

    步骤：写前整文件备份（``.bak-<epoch-ms>``）→ 临时文件写入 + flush
    + fsync → ``os.replace`` 原子换名 → 备份轮换。任一步失败时清理
    临时文件（不留 ``.tmp-*`` 残留），原文件内容不受影响（备份先行、
    换名最后——崩溃窗口内至多多出一份备份，config.yaml 恒为完整旧版
    或完整新版）。

    Args:
        yaml_path: 目标 config.yaml 路径。
        content: 完整文件文本。

    Returns:
        备份文件路径（写前文件存在时）；无既有文件返回 None。

    Raises:
        Exception: 备份 / 写入 / 换名失败的底层异常（调用方统一包装）。
    """
    backup_path: Path | None = None
    if yaml_path.is_file():
        backup_path = yaml_path.with_name(f"{yaml_path.name}.bak-{now_ms()}")
        shutil.copy2(yaml_path, backup_path)
    tmp_path = yaml_path.with_name(f"{yaml_path.name}.tmp-{now_ms()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, yaml_path)
    _prune_backups_blocking(yaml_path)
    return backup_path


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class RegistryAdminService:
    """LLM 注册表管理服务（读脱敏 / 写原子持久化 + 热重建 + 失败回滚）。

    无状态读写共享 loader 运行时（``runtime_llm_config`` / 全局解析器）；
    写突变经 ``asyncio.Lock`` 串行化（校验→持久化→重建原子窗口）。
    """

    def __init__(self) -> None:
        """初始化服务（写锁——单事件循环内串行化全部突变）。"""
        self._write_lock = asyncio.Lock()

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
        current = config_loader.runtime_llm_config
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
    # 写入面（原子持久化 + 热重建 + 失败回滚）
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
            path = yaml_path if yaml_path is not None else get_loaded_config_yaml_path()
            current = config_loader.runtime_llm_config
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
            path = yaml_path if yaml_path is not None else get_loaded_config_yaml_path()
            current = config_loader.runtime_llm_config
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
            path = yaml_path if yaml_path is not None else get_loaded_config_yaml_path()
            current = config_loader.runtime_llm_config
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
        return await self.state()

    async def _persist_and_rebuild(
        self, new_config: LlmConfig, *, snapshot: LlmConfig, yaml_path: Path
    ) -> None:
        """原子持久化 + 热重建 + 失败回滚（全部写突变的共享尾段）。

        Args:
            new_config: 待落盘的新 LlmConfig。
            snapshot: 写前运行时快照（重建失败回滚源）。
            yaml_path: 目标 config.yaml 路径。

        Raises:
            BusinessException: 既有文件不可解析（``CONFIG_INVALID``）；
                持久化 I/O 失败（``INTERNAL_ERROR``——临时文件已清理，
                原文件不变）；热重建失败（``CONFIG_INVALID``——内存已
                回滚写前快照，磁盘保留新内容与 .bak 写前备份）。
        """
        raw_existing = await run_blocking_ops(_read_raw_yaml_blocking, yaml_path)
        content = render_config_yaml_text(new_config, raw_existing)
        try:
            backup_path = await run_blocking_ops(_atomic_persist_blocking, yaml_path, content)
        except BusinessException:
            raise
        except Exception as exc:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"config.yaml 原子持久化失败（临时文件已清理，原文件不变）: {yaml_path}",
                details={"path": str(yaml_path), "reason": str(exc)},
            ) from exc
        logger.info(
            "config.yaml 已原子持久化: path={}, bytes={}, backup={}",
            yaml_path,
            len(content),
            backup_path.name if backup_path is not None else "(首次创建，无写前备份)",
        )
        try:
            load_config_yaml(path=yaml_path)
        except Exception as exc:
            apply_llm_config_to_runtime(snapshot)
            logger.opt(exception=True).error(
                "持久化后热重建失败，内存注册表已回滚写前快照: path={}", yaml_path
            )
            raise BusinessException(
                code=ErrorCode.CONFIG_INVALID,
                message=(
                    "config.yaml 持久化后热重建失败，内存注册表已回滚写前快照"
                    f"（磁盘保留新内容，写前备份: {backup_path.name if backup_path is not None else '无'}）"
                ),
                details={
                    "path": str(yaml_path),
                    "reason": str(exc),
                    "rollback": "in-memory",
                    "backup": backup_path.name if backup_path is not None else None,
                },
            ) from exc
