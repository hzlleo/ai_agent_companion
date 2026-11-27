"""亲密通道装配面——条目构建 / 半配置告警（配置模型与逻辑分文件）。

执行边界（锁定）：

    - 本模块只消费 :class:`~packages.harness.llm.intimate_models.
      IntimateChannelConfig`，**不读数据库、不读系统时钟、不发请求**；
      条目构建保持无 I/O，warning 文案由网关在构造期调用一次；
    - 半配置（开关开启但必填项缺失）视为整通道未启用，网关在构造期
      记录一条 warning（本模块的构建函数保持纯函数，不产生日志副作用）；
    - 已配置且被选中的通道不做静默换道：端点调用失败按网关既有错误
      语义上抛，调用方可见（见 :mod:`packages.harness.llm.gateway`）。

依赖方向：llm → config（``LlmModelEntry`` 类型）+ 本域
``intimate_models``；不消费 pools / usage（网关持有）。
"""

from __future__ import annotations

from loguru import logger

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.llm.intimate_models import (
    INTIMATE_CHANNEL_ENTRY_NAME,
    IntimateChannelConfig,
)

__all__ = [
    "build_intimate_entry",
    "log_incomplete_intimate_config",
]


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
            装配内置策略之前不应构建条目）。
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


def log_incomplete_intimate_config(config: IntimateChannelConfig) -> None:
    """记录半配置 warning（开关开启但必填项缺失——整通道视为未启用）。

    网关在构造期调用一次（策略装配阶段），避免每回合刷屏。

    Args:
        config: 亲密通道配置（缺失项来自 :attr:`missing_env_keys`）。
    """
    logger.warning(
        "亲密通道已开启但配置不完整，整通道视为未启用（回退默认通道）:"
        " missing_env_keys={}, threshold={}",
        list(config.missing_env_keys),
        config.min_intimacy_level,
    )
