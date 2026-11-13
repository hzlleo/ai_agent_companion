"""config.yaml + extensions_config.json 运行时加载器（分层加载 + 热重载地基）。

在 app lifespan（P0-6 装配）最先调用 ``load_all_config()``，将
config.yaml 的 ``llm`` 段（模型注册表 / 角色绑定 / 多模态回退链）与
extensions_config.json 的 ``tools`` 加载到进程内运行时状态：

    - ``runtime_llm_config``   —— :class:`LlmConfig`（llm 段解析结果）
    - ``extensions_config``    —— :class:`ExtensionsConfig`（工具清单）
    - 模型解析器全局索引重建   —— ``rebuild_model_resolver``
    - 多模态回退链记录         —— ``record_multimodal_fallback``

分层加载链（优先级从低到高）::

    代码默认值（Field default）
        ↓
    .env 文件（pydantic-settings，Settings 各分节 env_prefix）
        ↓
    进程环境变量（pydantic-settings 语义：env 优先于 .env）
        ↓
    config.yaml / extensions_config.json（结构化面：llm 模型注册表 /
    工具清单——与 env 面不重叠，互不覆盖）

文件缺失为合法状态（debug 日志，保持默认值——首次部署 / 测试 scratch
CWD）；文件存在但解析失败为 ``CONFIG_INVALID`` fail loud（半生效配置
不可审计）。

热重载地基：``register_reload_callback`` 注册重载后回调（轻量状态重置），
``load_all_config`` 每次完成即通知全部回调——重资源（引擎 / 池）重建由
P0-6 lifespan 装配时按需接线，本模块只提供通知机制。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import yaml
from loguru import logger
from pydantic import ValidationError

from packages.harness.config.model_entry import parse_model_entry
from packages.harness.config.model_resolver import rebuild_model_resolver
from packages.harness.config.models import (
    ExtensionsConfig,
    LlmConfig,
    MultimodalFallbackConfig,
    Settings,
)
from packages.harness.config.multimodal_fallback_loader import record_multimodal_fallback
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.logging import setup_logging

__all__ = [
    "runtime_llm_config",
    "extensions_config",
    "load_config_yaml",
    "load_extensions_config",
    "load_all_config",
    "register_reload_callback",
    "reset_config_runtime_state",
    "setup_logging_from_settings",
    "get_loaded_config_yaml_path",
    "apply_llm_config_to_runtime",
]

#: config.yaml 默认发现路径（CWD 相对——app 层从 backend/ 目录启动；
#: 测试经 ``load_all_config(config_dir=tmp_path)`` 显式注入）。
_CONFIG_YAML_PATH = Path("config.yaml")
#: extensions_config.json 默认发现路径。
_EXTENSIONS_JSON_PATH = Path("extensions_config.json")

#: 配置热重载回调注册表（load_all_config 完成后通知）。
_reload_callbacks: list[Callable[[], None]] = []

# ---------------------------------------------------------------------------
# 运行时状态（loader 单一真源；测试经 reset_config_runtime_state 复位）
# ---------------------------------------------------------------------------

#: llm 段解析结果（模型注册表 + 角色绑定 + 多模态回退链）。
runtime_llm_config: LlmConfig = LlmConfig()

#: extensions_config.json 解析结果（工具清单）。
extensions_config: ExtensionsConfig = ExtensionsConfig()

#: 最近一次成功加载使用的 config.yaml 路径（P2 注册表管理写路径消费——
#: 管理面原子持久化必须落在进程实际加载的同一文件上；None = 尚未加载）。
_loaded_config_yaml_path: Path | None = None


def register_reload_callback(cb: Callable[[], None]) -> None:
    """注册配置热重载回调（在 ``load_all_config`` 完成后调用）。

    Args:
        cb: 无参同步回调。仅做轻量状态重置，避免阻塞；重资源重建应在
            下次使用时惰性完成。
    """
    _reload_callbacks.append(cb)
    logger.debug("已注册配置热重载回调: {}", getattr(cb, "__name__", repr(cb)))


def _notify_reload_callbacks() -> None:
    """通知全部已注册回调：配置已重载。

    单个回调失败仅记录日志，不阻塞后续回调与加载主流程。
    """
    for cb in _reload_callbacks:
        try:
            cb()
        except Exception as exc:  # best-effort: 单回调失败不影响其他回调
            logger.opt(exception=True).warning(
                "配置热重载回调执行失败 (cb={}): {}",
                getattr(cb, "__name__", repr(cb)),
                exc,
            )


def _wrap_config_invalid(source: str, exc: Exception) -> BusinessException:
    """把解析/校验异常包装为 ``CONFIG_INVALID`` 业务异常。

    Args:
        source: 配置来源描述（文件路径等）。
        exc: 原始异常。

    Returns:
        携带来源与原因的 :class:`BusinessException`。
    """
    return BusinessException(
        code=ErrorCode.CONFIG_INVALID,
        details={"source": source, "reason": str(exc)},
        message=f"配置解析失败（{source}）: {exc}",
    )


def load_config_yaml(path: Path | None = None, *, config_dir: Path | None = None) -> LlmConfig:
    """加载 config.yaml，解析 ``llm`` 段并更新运行时状态。

    解析成功后：更新 ``runtime_llm_config``、重建模型解析器索引、记录
    多模态回退链。文件缺失为合法状态（保持默认值）。

    Args:
        path: 显式文件路径（优先）。
        config_dir: 配置目录（``path`` 未给时拼接 ``config.yaml``）；
            两者均未给时用 CWD 相对默认路径。

    Returns:
        解析后的 :class:`LlmConfig`（未加载文件时返回当前运行时值）。

    Raises:
        BusinessException: 文件存在但 YAML 解析失败 / 结构非法 / 模型
            条目校验失败（``CONFIG_INVALID``）。
    """
    global runtime_llm_config, _loaded_config_yaml_path

    yaml_path = path or (config_dir / "config.yaml" if config_dir else _CONFIG_YAML_PATH)
    if not yaml_path.is_file():
        logger.debug("config.yaml 不存在 (path={})，跳过加载", yaml_path)
        # 文件缺失为合法状态：仍记录解析路径——P2 注册表管理写路径在
        # 首次部署（尚无 config.yaml）时经本路径创建该文件。
        _loaded_config_yaml_path = yaml_path
        return runtime_llm_config

    try:
        raw_text = yaml_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except Exception as exc:
        raise _wrap_config_invalid(str(yaml_path), exc) from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _wrap_config_invalid(
            str(yaml_path), ValueError(f"顶层结构非 mapping（实际 {type(data).__name__}）")
        )

    llm_section = data.get("llm")
    if llm_section is None:
        llm_section = {}
    if not isinstance(llm_section, dict):
        raise _wrap_config_invalid(str(yaml_path), ValueError("llm 段结构非 mapping"))

    unknown_top_keys = [key for key in data if key != "llm"]
    if unknown_top_keys:
        logger.warning(
            "config.yaml 含未识别的顶层键（已忽略，当前仅解析 llm 段）: {}", unknown_top_keys
        )

    try:
        config = _parse_llm_section(llm_section)
    except BusinessException:
        raise
    except Exception as exc:
        raise _wrap_config_invalid(str(yaml_path), exc) from exc

    runtime_llm_config = config
    _loaded_config_yaml_path = yaml_path
    rebuild_model_resolver(config.models, config.role_bindings)
    record_multimodal_fallback(config.multimodal_fallback)
    logger.info(
        "config.yaml 已加载: {} 个模型条目, 已绑定角色={}, 回退链(local={}, remote={})",
        len(config.models),
        sorted(config.role_bindings) or "(按条目声明)",
        config.multimodal_fallback.local.name if config.multimodal_fallback.local else "(无)",
        config.multimodal_fallback.remote.name if config.multimodal_fallback.remote else "(无)",
    )
    return config


def _parse_llm_section(llm_section: dict[str, object]) -> LlmConfig:
    """解析 ``llm`` 段 dict 为 :class:`LlmConfig`（条目经归一化入口）。

    Args:
        llm_section: ``llm`` 段原始 dict。

    Returns:
        解析后的 :class:`LlmConfig`。

    Raises:
        BusinessException: 模型条目非法（``CONFIG_INVALID``）。
        ValidationError: 角色绑定等容器字段校验失败。
    """
    raw_models = llm_section.get("models", [])
    if raw_models is None:
        raw_models = []
    if not isinstance(raw_models, list):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={"reason": "llm.models 必须是列表"},
        )
    models = [parse_model_entry(entry) for entry in raw_models]

    raw_bindings = llm_section.get("role_bindings", {})
    if raw_bindings is None:
        raw_bindings = {}
    if not isinstance(raw_bindings, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={"reason": "llm.role_bindings 必须是 mapping"},
        )
    bindings: dict[str, str] = {str(key): str(value) for key, value in raw_bindings.items()}

    raw_fallback = llm_section.get("multimodal_fallback", {})
    if raw_fallback is None:
        raw_fallback = {}
    if not isinstance(raw_fallback, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={"reason": "llm.multimodal_fallback 必须是 mapping"},
        )
    fallback = MultimodalFallbackConfig.model_validate(raw_fallback)

    return LlmConfig(
        models=models,
        role_bindings=bindings,
        multimodal_fallback=fallback,
    )


def load_extensions_config(
    path: Path | None = None, *, config_dir: Path | None = None
) -> ExtensionsConfig:
    """加载 extensions_config.json，解析 ``tools`` 到运行时状态。

    Args:
        path: 显式文件路径（优先）。
        config_dir: 配置目录（``path`` 未给时拼接文件名）；两者均未给时
            用 CWD 相对默认路径。

    Returns:
        解析后的 :class:`ExtensionsConfig`（未加载文件时返回当前运行时值）。

    Raises:
        BusinessException: 文件存在但 JSON 解析失败 / 结构非法
            （``CONFIG_INVALID``）。
    """
    global extensions_config

    json_path = path or (
        config_dir / "extensions_config.json" if config_dir else _EXTENSIONS_JSON_PATH
    )
    if not json_path.is_file():
        logger.debug("extensions_config.json 不存在 (path={})，跳过加载", json_path)
        return extensions_config

    try:
        raw_text = json_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as exc:
        raise _wrap_config_invalid(str(json_path), exc) from exc

    if not isinstance(data, dict):
        raise _wrap_config_invalid(
            str(json_path), ValueError(f"顶层结构非 mapping（实际 {type(data).__name__}）")
        )

    try:
        config = ExtensionsConfig.model_validate(data)
    except ValidationError as exc:
        raise _wrap_config_invalid(str(json_path), exc) from exc

    extensions_config = config
    logger.info("extensions_config.json 已加载: {} 个工具配置", len(config.tools))
    return config


def load_all_config(*, config_dir: Path | None = None) -> tuple[LlmConfig, ExtensionsConfig]:
    """一次性加载 config.yaml + extensions_config.json 并通知重载回调。

    在 app lifespan（P0-6 装配）最先调用（先于 pool_manager.start()）。
    同步是知情决策：启动路径一次性、事件循环尚未服务请求，毫秒级本地
    文件读零影响；重资源热重建由重载回调 / lifespan 接线承担。

    Args:
        config_dir: 配置目录（None = CWD 相对默认路径；测试注入 tmp_path）。

    Returns:
        ``(llm 段配置, 工具清单)`` 二元组。

    Raises:
        BusinessException: 任一文件存在但解析失败（``CONFIG_INVALID``）。
    """
    llm = load_config_yaml(config_dir=config_dir)
    extensions = load_extensions_config(config_dir=config_dir)
    _notify_reload_callbacks()
    return llm, extensions


def reset_config_runtime_state() -> None:
    """复位运行时状态（测试隔离用——llm 段 / 工具清单 / 重载回调全清）。"""
    global runtime_llm_config, extensions_config, _loaded_config_yaml_path
    runtime_llm_config = LlmConfig()
    extensions_config = ExtensionsConfig()
    _loaded_config_yaml_path = None
    rebuild_model_resolver([], {})
    record_multimodal_fallback(None)
    _reload_callbacks.clear()


def get_loaded_config_yaml_path() -> Path:
    """返回进程当前绑定的 config.yaml 路径（注册表管理写路径消费）。

    优先返回最近一次 :func:`load_config_yaml` 实际解析的路径（显式
    ``path`` / ``config_dir`` / CWD 相对默认三者中实际生效者——含文件
    缺失的合法跳过形态）；从未加载过时回落 CWD 相对默认路径。P2 注册表
    管理的原子持久化必须落在进程实际加载的同一文件上——经本访问器取径。

    Returns:
        当前绑定的 :class:`Path`（未加载过 = CWD 相对 ``config.yaml``）。
    """
    if _loaded_config_yaml_path is not None:
        return _loaded_config_yaml_path
    return _CONFIG_YAML_PATH


def apply_llm_config_to_runtime(config: LlmConfig) -> None:
    """将 :class:`LlmConfig` 整体应用到运行时状态（最小公开重建入口）。

    与 :func:`load_config_yaml` 的差异：入参是内存模型而非文件——供
    「已有可信配置对象、需整体换血运行时」的消费面使用（P2 注册表管理
    的**回滚路径**：热重建失败后把写前快照恢复到运行时）。三步与
    ``load_config_yaml`` 成功尾段完全一致（runtime_llm_config 赋值 →
    解析器重建 → 回退链记录），保证两条路径行为不漂移。

    Args:
        config: 待应用的 LlmConfig（模型注册表 + 角色绑定 + 回退链）。
    """
    global runtime_llm_config
    runtime_llm_config = config
    rebuild_model_resolver(config.models, config.role_bindings)
    record_multimodal_fallback(config.multimodal_fallback)


def setup_logging_from_settings(settings: Settings) -> None:
    """Settings.log → core ``setup_logging`` 接线（app lifespan 启动期调用）。

    依赖方向 config → core（合法）；core 不感知 config（chunk 边界），
    由本函数完成 ``LogConfig`` → ``core.logging_models.LoggingConfig``
    的字段映射。

    Args:
        settings: 全局（或隔离）Settings 实例。
    """
    setup_logging(settings.log.to_core_config())
