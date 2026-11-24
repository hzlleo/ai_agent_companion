"""搜索 provider 注册表管理——config.yaml ``search`` 段读写 + /api/v1/search 管理面后端。

本模块是 :class:`packages.harness.config.section_admin.ConfigSectionAdmin`
共享基座的**搜索域子类**（该基座的第二个接入者——接入形态遵循
基座 docstring 的「读改写节（条目 CRUD）」文档化决策：条目级校验在
突变方法的锁窗口内先行完成，构造新节后调用继承的
``_persist_and_rebuild`` 尾段）。承载内容：

    - 运行时真源 :data:`runtime_search_config`（loader 语义镜像——
      ``search`` 段不在 loader 解析面内（loader 仅解析 ``llm`` 段；
      ``search`` 属其白名单已知段、不告警；其余顶层键告警忽略），
      本模块自持加载 / 应用 / 复位三件套；
      app lifespan 启动序列经 :func:`load_search_config` 装配）；
    - 条目 CRUD（唯一名 409 / 存在性 404 / 删除唯一 provider 守卫 409
      ——能力启用时删除最后一个条目会破坏「启用即至少一条目」的
      启动校验不变式，故拒绝并给出两步指引）；
    - 读取脱敏（条目只回 ``api_key_env`` 变量名，永不回密钥值）；
    - ``search`` 段序列化（Model Class ↔ yaml 段 dict 无损回读——
      ``web_search`` 子块按当前生效值整体落盘，见
      :class:`packages.harness.config.models.WebSearchSettings` 的
      「yaml 最高层固化」文档）。

搜索子类的缝实现一览::

    ``_section_key``               → ``"search"``
    ``_current_section``           → ``runtime_search_config``（本模块）
    ``_apply_section_to_runtime``  → ``apply_search_config_to_runtime``
    ``_render_section``            → ``_search_section``（本模块）
    ``_validate_section``          → 条目名唯一（节级跨操作不变式）
    ``_rebuild_from_disk``         → ``_rebuild_runtime_from_yaml``（重读磁盘）
    ``_resolve_yaml_path``         → 本模块加载路径回落 loader 加载路径

链路顺序（与 LLM 注册表管理面共用同一真相，文档化于
docs/backend/search.md 配置节管理一节）::

    校验（DTO 层：kind 值域 / base_url http(s) / extra=forbid；
          服务层：条目名唯一 409 / 存在性 404 / 删除唯一 provider 守卫 409）
        ↓ 构造新 SearchConfig（不可变重建——绝不原地修改运行时对象）
        ↓ 原子持久化（基座：写前备份 config.yaml.bak-<epoch-ms> → 临时文件
          写入 + fsync → os.replace 原子换名；失败清理临时文件）
        ↓ 热重建（重读磁盘解析 search 段——证明落盘内容可加载，
          并整体换血运行时）
        ↓ 重建失败 → apply_search_config_to_runtime(写前快照) 回滚内存
          + BusinessException（磁盘保留新内容与 .bak 写前备份）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.harness.config import loader as config_loader
from packages.harness.config.models import (
    SEARCH_PROVIDER_KINDS,
    SearchConfig,
    SearchProviderEntry,
    WebSearchSettings,
)
from packages.harness.config.section_admin import (
    DEFAULT_BACKUP_KEEP,
    ConfigSectionAdmin,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "SEARCH_REGISTRY_BACKUP_KEEP",
    "SearchProviderCreateDTO",
    "SearchProviderUpdateDTO",
    "SearchProviderMaskedDTO",
    "SearchRegistryStateDTO",
    "SearchRegistryAdminService",
    "runtime_search_config",
    "load_search_config",
    "apply_search_config_to_runtime",
    "reset_search_runtime_state",
    "get_loaded_search_yaml_path",
]

#: 备份保留份数（轮换上限——值单一真源为基座默认）。
SEARCH_REGISTRY_BACKUP_KEEP: Final[int] = DEFAULT_BACKUP_KEEP

# ---------------------------------------------------------------------------
# 运行时状态（本模块单一真源；测试经 reset_search_runtime_state 复位）
# ---------------------------------------------------------------------------

#: ``search`` 段解析结果（provider 注册表 + web_search 子块）。
runtime_search_config: SearchConfig = SearchConfig()

#: 最近一次成功加载使用的 config.yaml 路径（管理面写路径消费——原子
#: 持久化必须落在进程实际加载的同一文件上；None = 尚未加载，回落
#: loader 的加载路径——同一 config.yaml 文件）。
_loaded_search_yaml_path: Path | None = None


def get_loaded_search_yaml_path() -> Path:
    """返回搜索域当前绑定的 config.yaml 路径。

    解析序：本模块最近一次 :func:`load_search_config` 实际解析的路径 →
    loader 的 :func:`~packages.harness.config.loader.get_loaded_config_yaml_path`
    （同一文件——搜索段与 llm 段共存于一份 config.yaml）→ CWD 相对
    ``config.yaml``（loader 回落语义）。

    Returns:
        当前绑定的 :class:`Path`。
    """
    if _loaded_search_yaml_path is not None:
        return _loaded_search_yaml_path
    return config_loader.get_loaded_config_yaml_path()


def load_search_config(
    path: Path | None = None, *, config_dir: Path | None = None
) -> SearchConfig:
    """加载 config.yaml 的 ``search`` 段并更新运行时状态。

    与 loader ``llm`` 段语义对齐：文件缺失为合法状态（保持默认值——
    零配置开发 / 测试 scratch CWD）；文件存在但解析失败为
    ``CONFIG_INVALID`` fail loud。``web_search`` 子块缺省键经
    :class:`WebSearchSettings` 的 BaseSettings 构造从 SEARCH_* env /
    代码默认回填（yaml 键优先——分层加载链）。

    Args:
        path: 显式文件路径（优先）。
        config_dir: 配置目录（``path`` 未给时拼接 ``config.yaml``）；
            两者均未给时用搜索域当前绑定路径（含 loader 回落）。

    Returns:
        解析后的 :class:`SearchConfig`（未加载文件时返回当前运行时值）。

    Raises:
        BusinessException: 文件存在但 YAML 解析失败 / ``search`` 段结构
            非法 / 条目校验失败（``CONFIG_INVALID``）。
    """
    global runtime_search_config, _loaded_search_yaml_path

    yaml_path = path or (
        config_dir / "config.yaml" if config_dir else get_loaded_search_yaml_path()
    )
    if not yaml_path.is_file():
        logger.debug("config.yaml 不存在 (path={})，搜索段保持默认值", yaml_path)
        _loaded_search_yaml_path = yaml_path
        return runtime_search_config

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=f"config.yaml 解析失败（搜索段加载）: {exc}",
            details={"source": str(yaml_path), "reason": str(exc)},
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=f"config.yaml 顶层结构非 mapping（搜索段加载）: {yaml_path}",
            details={"source": str(yaml_path), "got": type(data).__name__},
        )

    section = data.get(config_loader.SEARCH_SECTION_KEY)
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message="config.yaml 的 search 段结构非 mapping",
            details={"source": str(yaml_path)},
        )
    try:
        config = SearchConfig.model_validate(section)
    except Exception as exc:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=f"search 段解析失败: {exc}",
            details={"source": str(yaml_path), "reason": str(exc)},
        ) from exc

    runtime_search_config = config
    _loaded_search_yaml_path = yaml_path
    logger.info(
        "config.yaml search 段已加载: {} 个 provider, enabled={}, tool_enabled={}",
        len(config.providers),
        config.web_search.enabled,
        config.web_search.tool_enabled,
    )
    return config


def apply_search_config_to_runtime(config: SearchConfig) -> None:
    """将 :class:`SearchConfig` 整体应用到运行时（管理面回滚路径）。"""
    global runtime_search_config
    runtime_search_config = config


def reset_search_runtime_state() -> None:
    """复位搜索运行时状态（测试隔离用——段 / 加载路径全清）。"""
    global runtime_search_config, _loaded_search_yaml_path
    runtime_search_config = SearchConfig()
    _loaded_search_yaml_path = None


def _rebuild_runtime_from_yaml(yaml_path: Path) -> None:
    """热重建钩子实现：重读磁盘 ``search`` 段 → 整体换血运行时。

    Args:
        yaml_path: 刚持久化的 config.yaml 路径。

    Raises:
        Exception: 落盘内容不可解析（调用方回滚内存后统一包装
            ``CONFIG_INVALID``）。
    """
    global runtime_search_config, _loaded_search_yaml_path
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    section = (data or {}).get(config_loader.SEARCH_SECTION_KEY) or {}
    runtime_search_config = SearchConfig.model_validate(section)
    _loaded_search_yaml_path = yaml_path


# ---------------------------------------------------------------------------
# YAML 序列化（search 段 → yaml 段 dict；与 SearchConfig.model_validate 回读对齐）
# ---------------------------------------------------------------------------


def _provider_to_section(entry: SearchProviderEntry) -> dict[str, Any]:
    """单 provider 条目 → ``search.providers`` 列表项 dict。

    Args:
        entry: provider 条目。

    Returns:
        可经 :class:`SearchProviderEntry` 无损回读的 dict（kind 缺省已
        物化的值原样保留——显式值与缺省值落盘同形）。
    """
    return {
        "name": entry.name,
        "kind": entry.kind,
        "base_url": entry.base_url,
        "api_key_env": entry.api_key_env,
        "model": entry.model,
        "max_results": entry.max_results,
        "timeout_seconds": entry.timeout_seconds,
    }


def _search_section(config: SearchConfig) -> dict[str, Any]:
    """SearchConfig → ``search`` 段 dict（providers + web_search 全量）。

    Args:
        config: 待序列化的 SearchConfig。

    Returns:
        可经 :func:`load_search_config` 无损回读的段 dict（``web_search``
        按当前生效值整体落盘——env 已合并值固化，见模块 docstring）。
    """
    return {
        "providers": [_provider_to_section(entry) for entry in config.providers],
        "web_search": dict(config.web_search.model_dump()),
    }


# ---------------------------------------------------------------------------
# 管理面 DTO（/api/v1/search 传输契约——密钥不变式：请求面 extra=forbid
# 拒绝任何未声明键，响应面只回 api_key_env 变量名）
# ---------------------------------------------------------------------------


def _validate_http_base_url(value: str) -> str:
    """校验 base_url 为 http(s) URL（管理面写路径唯一 scheme 值域）。

    Args:
        value: 原始字段值。

    Returns:
        去除首尾空白后的 URL。

    Raises:
        ValueError: scheme 非 http/https 或缺少 host。
    """
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"base_url 必须为 http(s) URL（实际: {value!r}）")
    return stripped


class SearchProviderCreateDTO(BaseModel):
    """provider 条目创建请求（写面）。

    Attributes:
        name: 条目唯一标识（非空；唯一性校验在服务层——冲突 409）。
        kind: provider 类型（bocha / zhipu）。
        base_url: API 基础 URL（http(s)；空 = 按 kind 缺省官方端点）。
        api_key_env: API Key 环境变量名（空 = 按 kind 缺省；**只接受
            变量名，永不接受密钥明文**）。
        model: provider 专属模型 / 引擎标识（空 = 按 kind 缺省）。
        max_results: 单次搜索返回条数上限（1-20，默认 8）。
        timeout_seconds: 单次请求超时秒数（默认 15）。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str = Field(min_length=1, max_length=64, description="条目唯一标识")
    kind: str = Field(description="provider 类型（bocha/zhipu）")
    base_url: str = Field(default="", max_length=512, description="API 基础 URL（空=按 kind 缺省）")
    api_key_env: str = Field(
        default="", max_length=128, description="API Key 环境变量名（空=按 kind 缺省）"
    )
    model: str = Field(
        default="", max_length=128, description="provider 专属模型/引擎标识（空=按 kind 缺省）"
    )
    max_results: int = Field(default=8, ge=1, le=20, description="返回条数上限（1-20）")
    timeout_seconds: float = Field(default=15.0, gt=0, le=120, description="单次请求超时（秒）")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """name 去空白 + 拒绝空值。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的条目名。

        Raises:
            ValueError: 值为空 / 纯空白。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("条目名不能为空")
        return stripped

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        """kind 值域校验（bocha / zhipu）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 kind。

        Raises:
            ValueError: 未知 kind。
        """
        stripped = value.strip().lower()
        if stripped not in SEARCH_PROVIDER_KINDS:
            raise ValueError(f"未知搜索 provider kind: {value!r}（合法值: {list(SEARCH_PROVIDER_KINDS)}）")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """base_url 提供 http(s) 校验（空串透传——按 kind 缺省语义）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 URL 或空串。

        Raises:
            ValueError: 提供了非 http(s) URL。
        """
        if not value.strip():
            return ""
        return _validate_http_base_url(value)

    @field_validator("api_key_env", "model")
    @classmethod
    def _strip_optional(cls, value: str) -> str:
        """可选字符串字段去空白（空串合法——按 kind 缺省语义）。

        Args:
            value: 原始字段值。

        Returns:
            去除首尾空白后的值。
        """
        return value.strip()


class SearchProviderUpdateDTO(BaseModel):
    """provider 条目部分更新请求（``name`` 不可变——字段不存在，请求体
    携带即经 ``extra="forbid"`` 拒绝 400）。

    字段 null 语义（逐字段文档化）：全部字段 null / 缺席 = 不变
    （空串语义字段——``base_url`` / ``api_key_env`` / ``model``——
    置空传 ``""``：重置为按 kind 缺省值）。

    Attributes:
        kind: 新 provider 类型（None = 不变）。
        base_url: 新 API 基础 URL（None = 不变；"" = 按 kind 缺省）。
        api_key_env: 新 API Key 环境变量名（None = 不变；"" = 按 kind 缺省）。
        model: 新模型 / 引擎标识（None = 不变；"" = 按 kind 缺省）。
        max_results: 新返回条数上限（None = 不变）。
        timeout_seconds: 新请求超时秒数（None = 不变）。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    kind: str | None = Field(default=None, description="新 provider 类型（None=不变）")
    base_url: str | None = Field(
        default=None, max_length=512, description="新 API 基础 URL（None=不变；空串=按 kind 缺省）"
    )
    api_key_env: str | None = Field(
        default=None, max_length=128, description="新 API Key 环境变量名（None=不变；空串=按 kind 缺省）"
    )
    model: str | None = Field(
        default=None, max_length=128, description="新模型/引擎标识（None=不变；空串=按 kind 缺省）"
    )
    max_results: int | None = Field(
        default=None, ge=1, le=20, description="新返回条数上限（None=不变）"
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0, le=120, description="新请求超时秒数（None=不变）"
    )

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str | None) -> str | None:
        """kind 提供时值域校验（None 透传——不变语义）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 kind 或 None。

        Raises:
            ValueError: 提供了未知 kind。
        """
        if value is None:
            return None
        stripped = value.strip().lower()
        if stripped not in SEARCH_PROVIDER_KINDS:
            raise ValueError(f"未知搜索 provider kind: {value!r}（合法值: {list(SEARCH_PROVIDER_KINDS)}）")
        return stripped

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        """base_url 提供时校验 http(s)（None / 空串透传）。

        Args:
            value: 原始字段值。

        Returns:
            校验通过的 URL、空串或 None。

        Raises:
            ValueError: 提供了非 http(s) URL。
        """
        if value is None or not value.strip():
            return value
        return _validate_http_base_url(value)

    @field_validator("api_key_env", "model")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        """可选字符串字段去空白（None 透传——不变语义）。

        Args:
            value: 原始字段值。

        Returns:
            去除首尾空白后的值或 None。
        """
        return value if value is None else value.strip()


class SearchProviderMaskedDTO(BaseModel):
    """provider 条目读取面（脱敏）——只携带 ``api_key_env`` 环境变量名，
    **永不携带密钥值**（密钥仅经进程环境变量进入，调用链解析点读取）。

    Attributes:
        name: 条目唯一标识。
        kind: provider 类型（bocha / zhipu）。
        base_url: API 基础 URL。
        api_key_env: API Key 环境变量名。
        model: provider 专属模型 / 引擎标识。
        max_results: 单次搜索返回条数上限。
        timeout_seconds: 单次请求超时秒数。
        chain_position: failover 链位次（1 起——列表序即尝试序）。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(description="条目唯一标识")
    kind: str = Field(description="provider 类型")
    base_url: str = Field(description="API 基础 URL")
    api_key_env: str = Field(description="API Key 环境变量名（永不携带密钥值）")
    model: str = Field(description="provider 专属模型/引擎标识")
    max_results: int = Field(description="返回条数上限")
    timeout_seconds: float = Field(description="单次请求超时（秒）")
    chain_position: int = Field(ge=1, description="failover 链位次（1 起）")


class SearchRegistryStateDTO(BaseModel):
    """搜索注册表状态（读取面 + 每次写操作的返回形态）。

    Attributes:
        providers: 当前 provider 条目（脱敏，声明顺序 = failover 尝试序）。
        web_search: 运营子块当前生效值（开关 / 预算 / 冷却——观测面）。
    """

    providers: list[SearchProviderMaskedDTO] = Field(
        default_factory=list, description="provider 条目（脱敏）"
    )
    web_search: WebSearchSettings = Field(
        default_factory=WebSearchSettings, description="运营子块当前生效值"
    )


# ---------------------------------------------------------------------------
# 服务（ConfigSectionAdmin 的搜索子类）
# ---------------------------------------------------------------------------


class SearchRegistryAdminService(ConfigSectionAdmin[SearchConfig]):
    """搜索 provider 注册表管理服务（读脱敏 / 写原子持久化 + 热重建 + 失败回滚）。

    无状态读写共享本模块运行时（:data:`runtime_search_config`）；写突变
    经基座 ``asyncio.Lock`` 串行化（校验→持久化→重建原子窗口）。
    """

    def __init__(self) -> None:
        """初始化服务（基座写锁 + 搜索注册表备份轮换参数）。"""
        super().__init__(backup_keep=SEARCH_REGISTRY_BACKUP_KEEP)

    # ------------------------------------------------------------------
    # 基座缝实现（search 段形态与运行时挂点）
    # ------------------------------------------------------------------

    def _section_key(self) -> str:
        """本管理面持有的 config.yaml 顶层键（搜索注册表 = ``search``）。"""
        return config_loader.SEARCH_SECTION_KEY

    def _current_section(self) -> SearchConfig:
        """读取内存运行时当前节（本模块 ``runtime_search_config`` 真源）。"""
        return runtime_search_config

    def _apply_section_to_runtime(self, section: SearchConfig) -> None:
        """把节整体应用到运行时（回滚路径——整体引用替换）。"""
        apply_search_config_to_runtime(section)

    def _render_section(self, section: SearchConfig) -> dict[str, Any]:
        """SearchConfig → ``search`` 段 dict（无损回读序列化）。"""
        return _search_section(section)

    def _validate_section(self, section: SearchConfig) -> None:
        """节级校验钩子——条目名唯一（跨操作节级不变式）。

        操作特定的条目级校验（唯一性 409 / 存在性 404 / 删除守卫 409）
        在各突变方法的锁窗口内先行完成（错误面携带操作上下文）；本
        钩子兜底「读改写窗口外的直接 mutate_section 调用」形态。

        Args:
            section: 待校验的新节实例。

        Raises:
            BusinessException: 条目重名（``CONFLICT``）。
        """
        names = [entry.name for entry in section.providers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise BusinessException(
                code=ErrorCode.CONFLICT,
                message=f"搜索 provider 条目名重复: {duplicates}",
                details={"duplicates": duplicates},
            )

    def _rebuild_from_disk(self, yaml_path: Path) -> None:
        """热重建钩子（重读磁盘 search 段——整体换血运行时）。"""
        _rebuild_runtime_from_yaml(yaml_path)

    def _resolve_yaml_path(self) -> Path:
        """默认持久化目标（搜索域绑定路径——与加载路径一致）。"""
        return get_loaded_search_yaml_path()

    # ------------------------------------------------------------------
    # 读取面（脱敏）
    # ------------------------------------------------------------------

    def _mask_entry(self, entry: SearchProviderEntry, position: int) -> SearchProviderMaskedDTO:
        """单条目 → 脱敏 DTO（含 failover 链位次——绑定状态观测面）。

        Args:
            entry: provider 条目。
            position: failover 链位次（1 起）。

        Returns:
            脱敏条目 DTO（只携带 api_key_env 变量名，永不携带密钥值）。
        """
        return SearchProviderMaskedDTO(
            name=entry.name,
            kind=entry.kind,
            base_url=entry.base_url,
            api_key_env=entry.api_key_env,
            model=entry.model,
            max_results=entry.max_results,
            timeout_seconds=entry.timeout_seconds,
            chain_position=position,
        )

    async def state(self) -> SearchRegistryStateDTO:
        """读取当前注册表状态（脱敏条目 + 运营子块生效值）。

        Returns:
            注册表状态 DTO（条目声明顺序 = failover 尝试序）。
        """
        current = self._current_section()
        return SearchRegistryStateDTO(
            providers=[
                self._mask_entry(entry, position)
                for position, entry in enumerate(current.providers, start=1)
            ],
            web_search=current.web_search.model_copy(deep=True),
        )

    # ------------------------------------------------------------------
    # 写入面（原子持久化 + 热重建 + 失败回滚——机械在基座）
    # ------------------------------------------------------------------

    async def create_provider(
        self, req: SearchProviderCreateDTO, *, yaml_path: Path | None = None
    ) -> SearchRegistryStateDTO:
        """创建 provider 条目（唯一性校验 → 原子持久化 → 热重建）。

        Args:
            req: 创建请求（DTO 层已完成 kind 值域 / http(s) 校验）。
            yaml_path: 目标 config.yaml（None = 搜索域绑定路径）。

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
            if any(entry.name == req.name for entry in current.providers):
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message=f"搜索 provider 条目名已存在: {req.name}",
                    details={"name": req.name},
                )
            # 经 model_validate 构造（dict 入参——kind 由 DTO 值域校验 +
            # 条目 Literal 校验双重保证；避免 str→Literal 构造器窄化摩擦）。
            entry = SearchProviderEntry.model_validate(
                {
                    "name": req.name,
                    "kind": req.kind,
                    "base_url": req.base_url,
                    "api_key_env": req.api_key_env,
                    "model": req.model,
                    "max_results": req.max_results,
                    "timeout_seconds": req.timeout_seconds,
                }
            )
            new_config = SearchConfig(
                providers=[*current.providers, entry],
                web_search=current.web_search.model_copy(deep=True),
            )
            logger.info("搜索注册表新增条目: name={}, kind={}, path={}", req.name, req.kind, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            return await self.state()

    async def update_provider(
        self, name: str, req: SearchProviderUpdateDTO, *, yaml_path: Path | None = None
    ) -> SearchRegistryStateDTO:
        """部分更新 provider 条目（name 不可变；仅出现字段生效）。

        Args:
            name: 目标条目名（路径参数）。
            req: 更新请求（逐字段 null 语义见 DTO docstring）。
            yaml_path: 目标 config.yaml（None = 搜索域绑定路径）。

        Returns:
            写后注册表状态（脱敏）。

        Raises:
            BusinessException: 条目不存在（``NOT_FOUND`` → 404）；持久化 /
                重建失败（同创建端点错误面）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            entry = next(
                (item for item in current.providers if item.name == name), None
            )
            if entry is None:
                raise BusinessException(
                    code=ErrorCode.NOT_FOUND,
                    message=f"搜索 provider 条目不存在: {name}",
                    details={
                        "name": name,
                        "known_providers": [item.name for item in current.providers],
                    },
                )
            # 全量重校验构造（model_copy(update=) 绕过校验——不用；与 LLM
            # 注册表 update_entry 同范式：merge 后经 model_validate 复核
            # 条目契约——kind 改变时按 kind 缺省物化空值缝）。
            merged: dict[str, Any] = dict(entry.model_dump())
            if req.kind is not None:
                merged["kind"] = req.kind
            if req.base_url is not None:
                merged["base_url"] = req.base_url
            if req.api_key_env is not None:
                merged["api_key_env"] = req.api_key_env
            if req.model is not None:
                merged["model"] = req.model
            if req.max_results is not None:
                merged["max_results"] = req.max_results
            if req.timeout_seconds is not None:
                merged["timeout_seconds"] = req.timeout_seconds
            updated = SearchProviderEntry.model_validate(merged)
            new_config = SearchConfig(
                providers=[
                    updated if item.name == name else item for item in current.providers
                ],
                web_search=current.web_search.model_copy(deep=True),
            )
            logger.info("搜索注册表更新条目: name={}, path={}", name, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            return await self.state()

    async def delete_provider(
        self, name: str, *, yaml_path: Path | None = None
    ) -> SearchRegistryStateDTO:
        """删除 provider 条目（唯一 provider 守卫：能力启用时禁止清空）。

        守卫语义：``web_search.enabled=true`` 时删除最后一个条目会破坏
        「启用即至少一个 provider」的启动校验不变式（启动期 fail
        fast）——拒绝 409 并给出两步指引（先关能力或先补条目）；
        能力关闭时允许清空注册表（条目为空 + enabled=false 是合法
        零配置形态）。

        Args:
            name: 目标条目名（路径参数）。
            yaml_path: 目标 config.yaml（None = 搜索域绑定路径）。

        Returns:
            写后注册表状态（脱敏）。

        Raises:
            BusinessException: 条目不存在（``NOT_FOUND`` → 404）；能力
                启用下的最后一个条目（``CONFLICT`` → 409，details 携带
                两步指引）；持久化 / 重建失败（同创建端点错误面）。
        """
        async with self._write_lock:
            path = yaml_path if yaml_path is not None else self._resolve_yaml_path()
            current = self._current_section()
            entry = next(
                (item for item in current.providers if item.name == name), None
            )
            if entry is None:
                raise BusinessException(
                    code=ErrorCode.NOT_FOUND,
                    message=f"搜索 provider 条目不存在: {name}",
                    details={
                        "name": name,
                        "known_providers": [item.name for item in current.providers],
                    },
                )
            if current.web_search.enabled and len(current.providers) == 1:
                raise BusinessException(
                    code=ErrorCode.CONFLICT,
                    message=(
                        f"删除被拒：{name} 是能力启用状态下唯一的搜索 provider"
                        "（启用即至少一个条目——启动校验不变式）"
                    ),
                    details={
                        "name": name,
                        "provider_count": len(current.providers),
                        "web_search_enabled": True,
                        "fix": (
                            "两步操作：PUT /api/v1/search/providers/{name} 前先经"
                            " config.yaml / SEARCH_ENABLED=false 关闭能力，或先创建"
                            " 替代 provider 条目再删除"
                        ),
                    },
                )
            new_config = SearchConfig(
                providers=[item for item in current.providers if item.name != name],
                web_search=current.web_search.model_copy(deep=True),
            )
            logger.info("搜索注册表删除条目: name={}, path={}", name, path)
            await self._persist_and_rebuild(
                new_config, snapshot=current.model_copy(deep=True), yaml_path=path
            )
            return await self.state()
