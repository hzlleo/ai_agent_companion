"""亲密通道路由纯函数——配置裁决 / 条目构建 / 采样参数合并（模型与逻辑分文件）。

执行边界（锁定）：

    - 本模块只消费 :class:`~packages.harness.llm.intimate_models.
      IntimateChannelConfig` 与本轮 ``intimacy_level``，**不读数据库、
      不读系统时钟、不发请求**——关系摘要由 chat 域派生后作为输入传入；
    - 判定顺序固定为 开关 → 配置齐全 → 关系摘要可用 → 阈值，先命中者
      给出裁决原因（与文档「路由矩阵」一致）；
    - 半配置（开关开启但必填项缺失）视为整通道未启用，warning 由网关在
      首个裁决时记录（本模块保持纯函数，不产生日志副作用）；
    - 已配置且被选中的通道不做静默换道：端点调用失败按网关既有错误
      语义上抛，调用方可见（见 :mod:`packages.harness.llm.gateway`）。

依赖方向：llm → config（``LlmModelEntry`` 类型）+ 本域
``intimate_models`` / ``llm_models``；不消费 pools / usage（网关持有）。
"""

from __future__ import annotations

from loguru import logger

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.intimate_models import (
    INTIMATE_CHANNEL_ENTRY_NAME,
    IntimateChannelConfig,
    IntimateRouteDecisionModel,
    IntimateRouteReason,
)
from packages.harness.llm.llm_models import ChatRequestParams

__all__ = [
    "evaluate_intimate_route",
    "build_intimate_entry",
    "build_intimate_request_params",
    "log_incomplete_intimate_config",
]


def evaluate_intimate_route(
    config: IntimateChannelConfig,
    *,
    intimacy_level: int | None,
) -> IntimateRouteDecisionModel:
    """裁决本轮是否走亲密通道（确定性纯函数——无 I/O、无日志）。

    判定顺序（先命中者即结论）：

        1. ``enabled=False`` → ``disabled``（默认形态，零行为变更）；
        2. 必填项缺失 → ``incomplete_config``（整通道视为未启用）；
        3. 本轮无关系摘要（``intimacy_level is None``）→
           ``relationship_unavailable``（未接线 / 读取降级）；
        4. 等级低于 ``min_intimacy_level`` → ``below_threshold``；
        5. 其余 → ``selected``。

    Args:
        config: 亲密通道配置（含开关、阈值与端点字段）。
        intimacy_level: 本轮关系摘要的亲密等级（0-3；None = 摘要不可用）。

    Returns:
        裁决结果（执行判据 ``use_intimate`` + 原因 + 排障对照字段）。

    Raises:
        ValueError: ``intimacy_level`` 越出 0-3（调用方编程错误——关系摘要
            的等级轴与配置阈值同域，越界即契约破坏）。
    """
    if intimacy_level is not None and not 0 <= intimacy_level <= 3:
        raise ValueError(f"intimacy_level 须在 0-3 之间: {intimacy_level!r}")

    configured = config.is_configured
    threshold = config.min_intimacy_level

    def _decision(*, use_intimate: bool, reason: IntimateRouteReason) -> IntimateRouteDecisionModel:
        """组装裁决结果（统一字段填充——原因与判据由调用分支给出）。"""
        return IntimateRouteDecisionModel(
            use_intimate=use_intimate,
            reason=reason,
            channel_enabled=config.enabled,
            channel_configured=configured,
            intimacy_level=intimacy_level,
            min_intimacy_level=threshold,
        )

    if not config.enabled:
        return _decision(use_intimate=False, reason=IntimateRouteReason.DISABLED)
    if not configured:
        return _decision(use_intimate=False, reason=IntimateRouteReason.INCOMPLETE_CONFIG)
    if intimacy_level is None:
        return _decision(use_intimate=False, reason=IntimateRouteReason.RELATIONSHIP_UNAVAILABLE)
    if intimacy_level < threshold:
        return _decision(use_intimate=False, reason=IntimateRouteReason.BELOW_THRESHOLD)
    return _decision(use_intimate=True, reason=IntimateRouteReason.SELECTED)


def build_intimate_entry(config: IntimateChannelConfig) -> LlmModelEntry:
    """由已配置完整的亲密通道构建模型条目（池门控 / 用量归属的条目键）。

    条目只承载寻址与观测字段：``api_key_env`` 留空（密钥不经环境变量再次
    解析——调用方用 :meth:`IntimateChannelConfig.api_key` 的值直接注入
    wire 配置），并发上限沿用全局默认，多模态关闭（对话通道不消费图像
    分块）。

    Args:
        config: 亲密通道配置（必须已通过 :attr:`is_configured` 校验）。

    Returns:
        供网关池门控与用量事件使用的模型条目。

    Raises:
        BusinessException: 配置不齐全（``INTERNAL_ERROR``——调用方在
            裁决为 ``selected`` 之前不应构建条目）。
    """
    if not config.is_configured:
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message="亲密通道配置不完整，不能构建模型条目",
            details={"missing_env_keys": list(config.missing_env_keys)},
        )
    return LlmModelEntry(
        name=INTIMATE_CHANNEL_ENTRY_NAME,
        provider="openai_compatible",
        base_url=config.base_url,
        api_key_env="",
        model=config.model,
    )


def build_intimate_request_params(
    config: IntimateChannelConfig,
    params: ChatRequestParams | None,
) -> ChatRequestParams | None:
    """合并亲密通道缺省采样温度与调用方参数（调用方显式值优先）。

    规则：通道未配置 ``temperature`` 时原样返回调用方参数；调用方显式
    设置了 ``temperature`` 时原样返回（合规重试的温度提升走这条路径）；
    调用方未设置温度时用通道缺省温度补位（其余采样字段保持调用方值）。

    Args:
        config: 亲密通道配置（提供缺省温度）。
        params: 调用方采样参数（None = 全默认）。

    Returns:
        合并后的采样参数（None = 通道与调用方都未设置温度——端点默认）。
    """
    if config.temperature is None:
        return params
    if params is None:
        return ChatRequestParams(temperature=config.temperature)
    if params.temperature is not None:
        return params
    return params.model_copy(update={"temperature": config.temperature})


def log_incomplete_intimate_config(config: IntimateChannelConfig) -> None:
    """记录半配置 warning（开关开启但必填项缺失——整通道视为未启用）。

    幂等由调用方控制（网关只在本进程首次命中时调用），避免每回合刷屏。

    Args:
        config: 亲密通道配置（缺失项来自 :attr:`missing_env_keys`）。
    """
    logger.warning(
        "亲密通道已开启但配置不完整，整通道视为未启用（回退默认通道）:"
        " missing_env_keys={}, threshold={}",
        list(config.missing_env_keys),
        config.min_intimacy_level,
    )
