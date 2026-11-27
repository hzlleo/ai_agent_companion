"""harness/tools 域——外部工具扩展装配（管理员配置 → ToolRegistry）。

职责链（app lifespan → duty / chat 工具面）::

    config.loader.extensions_config（启动期已解析的本地配置）
        ↓ register_extension_tools（总开关 / 同名冲突 / 构造期失败隔离）
        ↓ build_extension_tool（参数 schema 编译 + {env:} 解析）
        ↓ ToolRegistry.register（与内置 / 沙盒 / 浏览器工具同一注册表）

设计决策：

    - **零噪声默认态**：总开关关闭 / 工具列表为空时直接返回空报告，不
      注册、不告警（默认部署不因示例配置产生日志噪声）；
    - **同名保护**：默认拒绝覆盖任何已注册工具（内置 ``get_current_time`` /
      ``search_memory``，以及沙盒 / 浏览器 / 搜索工具）；管理员显式设置
      ``allow_override_builtin=true`` 才放行；
    - **单工具失败隔离**：构造期失败（环境变量缺失 / 占位未声明）只跳过
      该工具并记 warning，其它工具照常注册——扩展配置不阻塞启动；
    - **信任边界**：端点由管理员在本地文件定义，本层不做私网地址判定。
"""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from packages.harness.config.models import ExtensionsConfig
from packages.harness.core.errors import BusinessException
from packages.harness.tools.extension_tools import (
    HttpClientFactory,
    ToolClock,
    build_extension_tool,
)
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.tool_models import (
    ExtensionRegistrationReportModel,
    ExtensionSkippedToolModel,
)

__all__ = ["register_extension_tools"]


def register_extension_tools(
    registry: ToolRegistry,
    config: ExtensionsConfig,
    *,
    http_client_factory: HttpClientFactory,
    clock: ToolClock | None = None,
) -> ExtensionRegistrationReportModel:
    """把管理员声明的外部 HTTP 工具装配进注册表（失败逐工具隔离）。

    Args:
        registry: 工具注册表（装配目标）。
        config: 外部工具扩展配置（总开关 / 同名覆盖开关 / 工具列表）。
        http_client_factory: 共享 HTTP 客户端工厂（执行期调用）。
        clock: 时钟注入面（None = core.now_ms）。

    Returns:
        装配报告（成功注册与跳过明细——空报告 = 关闭 / 空列表）。
    """
    if not config.enabled or not config.tools:
        return ExtensionRegistrationReportModel()

    registered: list[str] = []
    skipped: list[ExtensionSkippedToolModel] = []
    existing = set(registry.tool_names)
    for tool in config.tools:
        if not config.allow_override_builtin and tool.name in existing:
            logger.warning(
                "外部工具与已注册工具同名，拒绝覆盖: tool={}（allow_override_builtin=false）",
                tool.name,
            )
            skipped.append(
                ExtensionSkippedToolModel(
                    name=tool.name,
                    reason="同名工具已注册（allow_override_builtin=false）",
                )
            )
            continue
        target = tool.http
        if target is None:
            skipped.append(
                ExtensionSkippedToolModel(name=tool.name, reason="缺少 http 目标")
            )
            continue
        try:
            spec, executor = build_extension_tool(
                tool,
                target,
                http_client_factory=http_client_factory,
                clock=clock,
            )
        except BusinessException as exc:
            logger.warning(
                "外部工具装配失败，已跳过: tool={}, reason={}",
                tool.name,
                exc.message,
            )
            skipped.append(
                ExtensionSkippedToolModel(name=tool.name, reason=exc.message)
            )
            continue
        except ValidationError as exc:
            logger.warning(
                "外部工具规格校验失败，已跳过: tool={}, errors={}",
                tool.name,
                str(exc)[:200],
            )
            skipped.append(
                ExtensionSkippedToolModel(name=tool.name, reason="工具规格校验失败")
            )
            continue
        registry.register(spec, executor)
        existing.add(tool.name)
        registered.append(tool.name)

    logger.info(
        "外部工具扩展装配完成: registered={}, skipped={}",
        len(registered),
        len(skipped),
    )
    return ExtensionRegistrationReportModel(registered=registered, skipped=skipped)
