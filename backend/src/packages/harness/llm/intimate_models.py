"""LLM 亲密回复通道 Model Classes——独立的 ``LLM_INTIMATE_*`` 环境面与路由裁决契约。

定位：长期陪伴对话里的亲密表达需要一个**可选的独立模型端点**——主对话
模型可能挂着偏保守的输出策略，运营可以另起一个按亲密语气调过的端点承接
「关系已经走到那一步」的回合，其余回合仍走既有 chat 角色通道。本模块只
承载数据形态与配置面，裁决算法在
:mod:`packages.harness.llm.intimate_router`（模型与逻辑分文件）。

配置面（``LLM_INTIMATE_`` 前缀的独立 ``BaseSettings`` 子类——不经
config 域 ``Settings`` 聚合，与 ``COMPLIANCE_`` / ``INJECTION_`` 同形态）：

    - 通道**默认关闭**：``LLM_INTIMATE_ENABLED=false``，且通道只在本类
      三个必填项（base_url / api_key / model）全部非空时才算配置齐全；
    - 半配置策略（锁定）：全局开关为真但必填项缺任一项时，**整通道视为
      未启用**并回退默认通道；网关于首个裁决时记一条 warning，指出缺失
      的环境变量名——不因半配置抛错阻断回复；
    - 通道标识 ``roles:intimate`` 只在本模块的环境面存在，**不进入**
      config 域 ``ModelRole`` 注册表（注册表角色面维持五角色不变，亲密
      通道不是可被角色绑定引用的注册表条目）。

路由输入只有一项：本轮关系摘要的 ``intimacy_level``（0-3）。关系摘要
缺失（未接线 / 读取降级为空摘要）时输入为 ``None`` 或 0，裁决自然回退
默认通道——亲密通道不自行读库、不猜关系状态。

依赖方向：pydantic / pydantic-settings only（配置面不导入其他 harness
域，防环；注册表条目构建在 router 模块完成）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "INTIMATE_CHANNEL_ENTRY_NAME",
    "INTIMATE_ENV_PREFIX",
    "DEFAULT_INTIMATE_MIN_INTIMACY_LEVEL",
    "IntimateChannelConfig",
    "IntimateRouteReason",
    "IntimateRouteDecisionModel",
]


#: 亲密通道环境变量前缀（``LLM_INTIMATE_*``——配置面单一真源）。
INTIMATE_ENV_PREFIX: str = "LLM_INTIMATE_"

#: 亲密通道在用量埋点里的条目名（与注册表条目名区分，便于用量面板观测）。
INTIMATE_CHANNEL_ENTRY_NAME: str = "llm-intimate-channel"

#: 触发亲密通道的最低亲密等级缺省值（关系摘要 intimacy_level 0-3 同轴）。
DEFAULT_INTIMATE_MIN_INTIMACY_LEVEL: int = 2


class IntimateChannelConfig(BaseSettings):
    """亲密回复通道配置（env_prefix=``LLM_INTIMATE_``——llm 域本地设置）。

    全部字段可选：未配置的字段保持空值，通道按
    :attr:`is_configured` 判定为未启用。``enabled`` 是运维总闸，与配置
    齐全性共同决定通道是否可用（见模块 docstring 半配置策略）。

    Attributes:
        enabled: 是否开启亲密通道路由（默认 False——零行为变更；True 且
            必填项齐全时才可能被路由选中）。
        base_url: 亲密端点 API 基础 URL（OpenAI 兼容端点根路径，含
            ``/v1``；空 = 未配置）。
        api_key: 亲密端点 API Key（SecretStr 存储，repr 不泄露；空 = 未
            配置——本通道不回落全局 ``LLM_API_KEY``，独立端点独立凭据）。
        model: 亲密端点模型名（传给 provider 的 model 参数；空 = 未配置）。
        temperature: 亲密通道缺省采样温度（None = 不注入，沿用调用方参数 /
            端点默认；调用方显式 temperature 优先）。
        min_intimacy_level: 触发亲密通道的最低亲密等级（0-3；默认 2——
            与关系摘要的 intimacy_level 同轴）。
    """

    enabled: bool = Field(default=False, description="是否开启亲密通道路由（默认关闭；需配置齐全）")
    base_url: str = Field(
        default="", max_length=512, description="亲密端点 API 基础 URL（空=未配置）"
    )
    api_key: SecretStr = Field(
        default=SecretStr(""), description="亲密端点 API Key（空=未配置；repr 不泄露）"
    )
    model: str = Field(default="", max_length=256, description="亲密端点模型名（空=未配置）")
    temperature: float | None = Field(
        default=None, ge=0.0, le=2.0, description="亲密通道缺省采样温度（None=不注入）"
    )
    min_intimacy_level: int = Field(
        default=DEFAULT_INTIMATE_MIN_INTIMACY_LEVEL,
        ge=0,
        le=3,
        description="触发亲密通道的最低亲密等级（0-3）",
    )

    model_config = SettingsConfigDict(
        env_prefix=INTIMATE_ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("base_url", "model")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """去除首尾空白（空串语义保持「未配置」——不在这里拒绝空值）。

        Args:
            value: 原始字段值。

        Returns:
            去除首尾空白后的值。
        """
        return value.strip()

    @property
    def is_configured(self) -> bool:
        """通道必填项是否齐全（base_url / api_key / model 三项全部非空）。

        Returns:
            True 表示配置齐全（仍须 ``enabled=True`` 才会被路由选中）。
        """
        return not self.missing_env_keys

    @property
    def missing_env_keys(self) -> tuple[str, ...]:
        """缺失的必填环境变量名（半配置排障面——warning 与文档同源）。

        Returns:
            缺失项对应的 ``LLM_INTIMATE_*`` 变量名元组（按 base_url /
            api_key / model 固定顺序；空元组 = 配置齐全）。
        """
        missing: list[str] = []
        if not self.base_url:
            missing.append(f"{INTIMATE_ENV_PREFIX}BASE_URL")
        if not self.api_key.get_secret_value().strip():
            missing.append(f"{INTIMATE_ENV_PREFIX}API_KEY")
        if not self.model:
            missing.append(f"{INTIMATE_ENV_PREFIX}MODEL")
        return tuple(missing)


class IntimateRouteReason(StrEnum):
    """亲密通道路由裁决原因值域（纯函数产出的可观测结论）。

    - ``disabled``：全局开关关闭（默认形态——零行为变更）；
    - ``incomplete_config``：开关开启但必填项缺失（整通道视为未启用）；
    - ``relationship_unavailable``：本轮没有关系摘要（未接线 / 读取降级）；
    - ``below_threshold``：本轮亲密等级低于配置阈值；
    - ``selected``：本轮走亲密通道。
    """

    DISABLED = "disabled"
    INCOMPLETE_CONFIG = "incomplete_config"
    RELATIONSHIP_UNAVAILABLE = "relationship_unavailable"
    BELOW_THRESHOLD = "below_threshold"
    SELECTED = "selected"


class IntimateRouteDecisionModel(BaseModel):
    """单轮亲密通道路由裁决（纯函数产出——网关据此选择端点，不自行推断）。

    Attributes:
        use_intimate: 本轮是否走亲密通道（唯一执行判据）。
        reason: 裁决原因（可观测结论——日志与测试断言消费）。
        channel_enabled: 全局开关的当前值（排障对照）。
        channel_configured: 必填项是否齐全（排障对照）。
        intimacy_level: 本轮关系摘要的亲密等级（None = 摘要不可用）。
        min_intimacy_level: 生效的触发阈值（配置值）。
    """

    model_config = ConfigDict(frozen=True)

    use_intimate: bool = Field(description="本轮是否走亲密通道")
    reason: IntimateRouteReason = Field(description="裁决原因")
    channel_enabled: bool = Field(description="全局开关当前值")
    channel_configured: bool = Field(description="必填项是否齐全")
    intimacy_level: int | None = Field(
        default=None, ge=0, le=3, description="本轮亲密等级（None=摘要不可用）"
    )
    min_intimacy_level: int = Field(ge=0, le=3, description="生效的触发阈值")
