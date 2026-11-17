"""配置节管理基座（ConfigSectionAdmin）——config.yaml 写路径的共享抽象基建。

本模块从 LLM 注册表管理服务（:mod:`packages.harness.config.registry_admin`）
抽取的**节无关**写路径机械，供全部 config.yaml 结构化配置节的管理面复用
（现有消费者：LLM 注册表；规划中消费者：搜索 provider 注册表等——每新增
一个配置节管理面不再复制本机械）。

基座持有的机械（全部节无关）：

    - **写串行化**：``asyncio.Lock`` 串行化全部突变（单事件循环内
      校验→持久化→重建为原子窗口，杜绝并发写相互覆盖丢失条目）；
    - **原子持久化**：写前整文件备份（``.bak-<epoch-ms>``）→ 临时文件
      写入 + flush + fsync → ``os.replace`` 原子换名 → 备份轮换（保留
      最新 ``backup_keep`` 份，更旧清理；任一步失败清理临时文件，原文件
      内容不受影响——崩溃窗口内 config.yaml 恒为完整旧版或完整新版）；
    - **重建 + 回滚编排**：持久化成功后经重建钩子重读磁盘（证明落盘
      内容可加载并整体换血运行时）；重建失败 → 经设置钩子把写前快照
      恢复到运行时（仅回滚内存——磁盘保留新内容与 .bak 写前备份，供
      人工恢复，下次成功写自愈覆盖）；
    - **阻塞 I/O 下沉**：文件读写 / 备份 / 换名全部经
      ``run_blocking_ops``（YAML 序列化为纯内存计算，留在事件循环）。

子类缝契约（抽象缝——子类必须实现；``SectionConfigT`` 为配置节
Model Class 类型参数）：

    ======================  =====================================================
    缝                       语义
    ======================  =====================================================
    ``_section_key``         本管理面持有的 config.yaml 顶层键（如 ``llm``）
    ``_current_section``     读取内存运行时当前节（get——快照 / 读改写基底）
    ``_apply_section_to_runtime``  把节整体应用到运行时（set——回滚恢复路径）
    ``_render_section``      节 Model Class → yaml 段 dict（无损回读序列化）
    ``_validate_section``    节级校验钩子（持久化前最后闸门——操作特定的
                             条目级校验在子类突变方法内先行完成）
    ``_rebuild_from_disk``   热重建钩子（重读磁盘并整体换血运行时）
    ``_resolve_yaml_path``   默认持久化目标解析（None 形态的目标文件）
    ======================  =====================================================

可覆写默认缝：``_backup_suffix``（备份文件名后缀——默认 ``.bak``；
轮换契约要求同目录内备份文件名字典序 == 时间序，覆写须保持）。

两种接入形态（文档化决策）：

    - **简单节**：直接消费 :meth:`ConfigSectionAdmin.mutate_section`
      （整体替换语义——调用方给出最终节内容；锁保证持久化 + 重建的
      原子窗口）；
    - **读改写节（条目 CRUD）**：子类自持突变方法（如 LLM 注册表的
      create / update / delete_entry），在 ``async with self._write_lock``
      窗口内完成「读当前节 → 操作特定校验 → 构造新节」后调用继承的
      ``_persist_and_rebuild`` 尾段——构造发生在锁内，杜绝并发读改写
      丢失条目（RegistryAdminService 为参照实现）。

链路顺序（与 LLM 注册表管理面共用同一真相，文档化于
docs/backend/config.md 配置节管理基座一节）::

    校验（节级钩子 _validate_section；条目级校验在子类突变方法内先行）
        ↓ 快照当前节（model_copy(deep=True)——回滚源）
        ↓ 原子持久化（写前备份 → 临时文件 + fsync → os.replace → 轮换）
        ↓ 热重建（_rebuild_from_disk 重读磁盘——证明落盘内容可加载）
        ↓ 重建失败 → _apply_section_to_runtime(写前快照) 回滚内存
          + BusinessException（磁盘保留新内容与 .bak 写前备份）
"""

from __future__ import annotations

import asyncio
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Generic, TypeVar

import yaml
from loguru import logger
from pydantic import BaseModel

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.thread_pool import run_blocking_ops
from packages.harness.core.time_utils import now_ms

__all__ = [
    "ConfigSectionAdmin",
    "DEFAULT_BACKUP_KEEP",
    "SectionConfigT",
]

#: 配置节 Model Class 类型参数（基座泛型——绑定 BaseModel，杜绝裸 dict 面）。
SectionConfigT = TypeVar("SectionConfigT", bound=BaseModel)

#: 备份保留份数默认值（轮换上限——超出即清理最旧备份）。
DEFAULT_BACKUP_KEEP: Final[int] = 10

#: 备份文件名后缀默认值（``<文件名>.bak-<epoch-ms>``）。
_DEFAULT_BACKUP_SUFFIX: Final[str] = ".bak"


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


def _prune_backups_blocking(yaml_path: Path, *, backup_suffix: str, backup_keep: int) -> None:
    """备份轮换：保留最新 ``backup_keep`` 份，清理更旧备份（阻塞形态）。

    文件名字典序 == epoch-ms 数值序（当前纪元毫秒恒 13 位定长），
    排序后裁尾即删除最旧。

    Args:
        yaml_path: config.yaml 路径（备份与其同目录同名前缀）。
        backup_suffix: 备份文件名后缀（须与备份路径构造一致）。
        backup_keep: 备份保留份数（轮换上限）。
    """
    backups = sorted(
        path
        for path in yaml_path.parent.glob(f"{yaml_path.name}{backup_suffix}-*")
        if path.is_file()
    )
    for stale in backups[:-backup_keep] if len(backups) > backup_keep else []:
        stale.unlink(missing_ok=True)
        logger.debug("已清理过期配置备份: {}", stale)


def _atomic_persist_blocking(
    yaml_path: Path,
    content: str,
    *,
    backup_suffix: str,
    backup_keep: int,
) -> Path | None:
    """原子持久化 config.yaml（阻塞形态——经线程池下沉）。

    步骤：写前整文件备份（``<名><backup_suffix>-<epoch-ms>``）→ 临时文件
    写入 + flush + fsync → ``os.replace`` 原子换名 → 备份轮换。任一步
    失败时清理临时文件（不留 ``.tmp-*`` 残留），原文件内容不受影响
    （备份先行、换名最后——崩溃窗口内至多多出一份备份，config.yaml
    恒为完整旧版或完整新版）。

    Args:
        yaml_path: 目标 config.yaml 路径。
        content: 完整文件文本。
        backup_suffix: 备份文件名后缀（命名缝的展开值）。
        backup_keep: 备份保留份数（轮换上限）。

    Returns:
        备份文件路径（写前文件存在时）；无既有文件返回 None。

    Raises:
        Exception: 备份 / 写入 / 换名失败的底层异常（调用方统一包装）。
    """
    backup_path: Path | None = None
    if yaml_path.is_file():
        backup_path = yaml_path.with_name(f"{yaml_path.name}{backup_suffix}-{now_ms()}")
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
    _prune_backups_blocking(yaml_path, backup_suffix=backup_suffix, backup_keep=backup_keep)
    return backup_path


def _dump_config_yaml_text(
    section_key: str,
    section: Mapping[str, Any],
    raw_existing: dict[str, Any] | None,
) -> str:
    """渲染完整 config.yaml 文本（保留本节之外的未知顶层键）。

    Args:
        section_key: 本管理面持有的顶层键（其值被整体替换）。
        section: 节的 yaml 段 dict（序列化边界——Model Class 的映射形态）。
        raw_existing: 既有文件的原始顶层 dict（None = 无既有文件）；
            ``section_key`` 键被整体替换，其余键原样保留（向前兼容后续
            顶层配置段）。

    Returns:
        完整 config.yaml 文本（UTF-8 YAML，键序稳定）。
    """
    data: dict[str, Any] = dict(raw_existing) if raw_existing else {}
    data[section_key] = dict(section)
    # 显式 str 标注收口（yaml 无 stub——safe_dump 返回 Any；与既有
    # registry_admin 序列化面同法，零 type:ignore）。
    text: str = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return text


# ---------------------------------------------------------------------------
# 基座
# ---------------------------------------------------------------------------


class ConfigSectionAdmin(ABC, Generic[SectionConfigT]):
    """config.yaml 单配置节管理基座（写串行化 + 原子持久化 + 热重建 + 回滚）。

    持有节无关的写路径机械（见模块 docstring）；子类经抽象缝声明所属
    配置节的形态与运行时挂点。无状态读写共享子类运行时；写突变经
    ``asyncio.Lock`` 串行化（校验→持久化→重建原子窗口）。
    """

    def __init__(self, *, backup_keep: int = DEFAULT_BACKUP_KEEP) -> None:
        """初始化基座（写锁 + 备份轮换参数）。

        Args:
            backup_keep: 备份保留份数（轮换上限——超出即清理最旧备份）。
        """
        #: 写锁（单事件循环内串行化全部突变——子类突变方法经
        #: ``async with self._write_lock`` 持有校验→持久化→重建原子窗口）。
        self._write_lock = asyncio.Lock()
        #: 备份保留份数（轮换上限）。
        self._backup_keep = backup_keep

    # ------------------------------------------------------------------
    # 抽象缝（子类实现——节形态与运行时挂点）
    # ------------------------------------------------------------------

    @abstractmethod
    def _section_key(self) -> str:
        """本管理面持有的 config.yaml 顶层键。

        Returns:
            顶层键名（如 ``llm``；持久化时该键值被整体替换，其余顶层键
            原样保留）。
        """
        raise NotImplementedError

    @abstractmethod
    def _current_section(self) -> SectionConfigT:
        """读取内存运行时当前节（get 缝——快照 / 读改写基底）。

        Returns:
            当前运行时节实例（读取真源——子类运行时树 / loader 全局）。
        """
        raise NotImplementedError

    @abstractmethod
    def _apply_section_to_runtime(self, section: SectionConfigT) -> None:
        """把节整体应用到运行时（set 缝——回滚恢复路径）。

        与重建钩子的成功尾段保持一致的三步换血语义（整体替换运行时
        引用 + 一切派生索引），保证持久化成功 → 热重建失败 → 回滚的
        行为不漂移。

        Args:
            section: 待应用的节实例（回滚时为写前快照）。
        """
        raise NotImplementedError

    @abstractmethod
    def _render_section(self, section: SectionConfigT) -> Mapping[str, Any]:
        """节 Model Class → yaml 段 dict（无损回读序列化缝）。

        Args:
            section: 待序列化的节实例。

        Returns:
            可经子类加载面无损回读的段 dict（序列化边界——yaml 段天然
            为 mapping 形态，非公共 API 载荷）。
        """
        raise NotImplementedError

    @abstractmethod
    def _validate_section(self, section: SectionConfigT) -> None:
        """节级校验钩子（持久化前最后闸门）。

        在锁内、任何文件 I/O 之前调用——校验失败即整个突变失败（零
        磁盘副作用）。操作特定的条目级校验（唯一性 / 存在性 / 删除守卫
        等）在子类突变方法内先行完成（错误面携带操作上下文）；本钩子
        只承载跨操作成立的节级不变式，无不变式的节实现为空体。

        Args:
            section: 待校验的新节实例。

        Raises:
            BusinessException: 节级不变式不成立（子类定义错误码——
                通常 ``CONFLICT``）。
        """
        raise NotImplementedError

    @abstractmethod
    def _rebuild_from_disk(self, yaml_path: Path) -> None:
        """热重建钩子（重读磁盘并整体换血运行时）。

        Args:
            yaml_path: 刚持久化的 config.yaml 路径。

        Raises:
            Exception: 落盘内容不可加载（调用方回滚内存后统一包装为
                ``CONFIG_INVALID``）。
        """
        raise NotImplementedError

    @abstractmethod
    def _resolve_yaml_path(self) -> Path:
        """默认持久化目标解析（突变调用未显式给路径时的目标文件）。

        Returns:
            目标 config.yaml 路径（如进程实际加载路径 / 固定配置路径）。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 可覆写默认缝（备份文件命名——轮换契约见 docstring）
    # ------------------------------------------------------------------

    def _backup_suffix(self) -> str:
        """备份文件名后缀（命名缝——默认 ``.bak``）。

        备份路径为 ``<目标文件名><后缀>-<epoch-ms>``；轮换按文件名
        字典序 == epoch-ms 数值序裁尾（当前纪元毫秒恒 13 位定长）——
        覆写后缀须保持该序（避免破坏轮换正确性）。

        Returns:
            备份文件名后缀（含前导点）。
        """
        return _DEFAULT_BACKUP_SUFFIX

    # ------------------------------------------------------------------
    # 机械（节无关——子类继承消费）
    # ------------------------------------------------------------------

    def _render_file_text(
        self, section: SectionConfigT, raw_existing: dict[str, Any] | None
    ) -> str:
        """渲染完整 config.yaml 文本（节缝 + 未知顶层键保留）。

        Args:
            section: 新的节实例（落盘的真源）。
            raw_existing: 既有文件的原始顶层 dict（None = 无既有文件）。

        Returns:
            完整 config.yaml 文本（UTF-8 YAML，键序稳定）。
        """
        return _dump_config_yaml_text(
            self._section_key(), self._render_section(section), raw_existing
        )

    async def mutate_section(
        self, new_section: SectionConfigT, *, yaml_path: Path | None = None
    ) -> None:
        """通用突变入口（整体替换语义——锁内 校验→快照→持久化→热重建）。

        适用于调用方直接给出最终节内容的简单配置节。读改写（条目 CRUD）
        流程不得使用本入口在锁外构造新节（并发下会丢失条目）——应如
        RegistryAdminService 在子类突变方法的锁窗口内构造后调用
        ``_persist_and_rebuild``。

        Args:
            new_section: 新的节实例（整体替换本节）。
            yaml_path: 目标 config.yaml（None = ``_resolve_yaml_path``）。

        Raises:
            BusinessException: 节级校验失败（子类错误码）；既有文件不可
                解析（``CONFIG_INVALID``）；持久化 I/O 失败
                （``INTERNAL_ERROR``——临时文件已清理，原文件不变）；
                热重建失败（``CONFIG_INVALID``——内存已回滚写前快照）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            snapshot = self._current_section().model_copy(deep=True)
            await self._persist_and_rebuild(new_section, snapshot=snapshot, yaml_path=path)

    async def _persist_and_rebuild(
        self, new_section: SectionConfigT, *, snapshot: SectionConfigT, yaml_path: Path
    ) -> None:
        """原子持久化 + 热重建 + 失败回滚（全部写突变的共享尾段）。

        Args:
            new_section: 待落盘的新节实例。
            snapshot: 写前运行时快照（重建失败回滚源——调用方在锁内
                构造）。
            yaml_path: 目标 config.yaml 路径。

        Raises:
            BusinessException: 节级校验失败（子类错误码）；既有文件不可
                解析（``CONFIG_INVALID``）；持久化 I/O 失败
                （``INTERNAL_ERROR``——临时文件已清理，原文件不变）；
                热重建失败（``CONFIG_INVALID``——内存已回滚写前快照，
                磁盘保留新内容与 .bak 写前备份）。
        """
        self._validate_section(new_section)
        raw_existing = await run_blocking_ops(_read_raw_yaml_blocking, yaml_path)
        content = self._render_file_text(new_section, raw_existing)
        try:
            backup_path = await run_blocking_ops(
                _atomic_persist_blocking,
                yaml_path,
                content,
                backup_suffix=self._backup_suffix(),
                backup_keep=self._backup_keep,
            )
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
            self._rebuild_from_disk(yaml_path)
        except Exception as exc:
            self._apply_section_to_runtime(snapshot)
            logger.opt(exception=True).error(
                "持久化后热重建失败，内存配置节已回滚写前快照: path={}", yaml_path
            )
            raise BusinessException(
                code=ErrorCode.CONFIG_INVALID,
                message=(
                    "config.yaml 持久化后热重建失败，内存配置节已回滚写前快照"
                    f"（磁盘保留新内容，写前备份: {backup_path.name if backup_path is not None else '无'}）"
                ),
                details={
                    "path": str(yaml_path),
                    "reason": str(exc),
                    "rollback": "in-memory",
                    "backup": backup_path.name if backup_path is not None else None,
                },
            ) from exc
