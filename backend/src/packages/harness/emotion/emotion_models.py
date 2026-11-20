"""emotion 域 Model Classes——情绪维度枚举 / 情绪状态 / 增量 / 引擎配置与环境开关。

设计定位（轻情绪引擎——纯本地、零外部成本）：

    - :class:`EmotionDimension` —— 8 个轻情绪维度（中文展示名随枚举
      注释；模型输出协议 ``<情绪>维度:+增量</情绪>`` 的值域即本枚举）；
    - :class:`EmotionStateModel` —— 中期（M 层）情绪状态文档（维度值
      0..1 + 最近更新时刻）；持久化形态寄居人格状态表的
      ``ff_persona_states``（layer=M）JSON 文档 ``emotion`` 键下；
    - :class:`EmotionDeltaModel` —— 单轮情绪增量（-1..1；解析器 /
      引擎入口的共同值域载体）；
    - :class:`EmotionConfig` —— 引擎参数（半衰期 / 中性基线 / 摘要
      阈值——构造注入旋钮，测试直调面）；
    - :class:`EmotionSettings` —— 环境开关（``EMOTION_`` 前缀——
      pydantic-settings 直读；物理定义在本模块，config 聚合器接入属
      后续变更，见 docs/backend/emotion.md「装配与开关」）。

不变式：状态值与增量一律按 8 维枚举校验——未知维度在模型边界
丢弃（debug 记录，绝不抛异常：模型输出永远不可信），数值夹取到
合法区间（状态 0..1 / 增量 -1..1）。
"""

from __future__ import annotations

from enum import StrEnum

from loguru import logger
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "EmotionDimension",
    "EmotionStateModel",
    "EmotionDeltaModel",
    "EmotionConfig",
    "EmotionSettings",
    "EMOTION_DOCUMENT_KEY",
    "DIMENSION_LABELS",
    "DEFAULT_NEUTRAL_BASELINE",
    "DEFAULT_DECAY_HALF_LIFE_MS",
    "DEFAULT_SUMMARY_THRESHOLD",
]

#: M 层状态文档内的情绪键（``ff_persona_states.state['emotion']``——与
#: 其他 M 层字段（心情备注 / 关系评价）共存，写入侧保序合并）。
EMOTION_DOCUMENT_KEY: str = "emotion"

#: 中性基线锁定缺省（0.5——8 维全部无偏向；配置可覆写）。
DEFAULT_NEUTRAL_BASELINE: float = 0.5
#: 衰减半衰期锁定缺省（6 小时——跨会话可感但一日内明显回落）。
DEFAULT_DECAY_HALF_LIFE_MS: int = 6 * 60 * 60 * 1000
#: 摘要阈值锁定缺省（0.15——偏离基线的可感幅度）。
DEFAULT_SUMMARY_THRESHOLD: float = 0.15


class EmotionDimension(StrEnum):
    """轻情绪维度值域（8 维——协议值域 + M 态字段真源）。

    维度选择以「陪伴场景可感、TTS / 前端可表达」为原则（后续
    TTS 风格与前端表情消费面按本枚举取值）：

    - ``joy`` 愉悦 / ``affection`` 亲昵 / ``shyness`` 羞怯 /
      ``excitement`` 兴奋 / ``calm`` 平静 / ``sadness`` 低落 /
      ``anxiety`` 不安 / ``annoyance`` 微恼。
    """

    JOY = "joy"
    AFFECTION = "affection"
    SHYNESS = "shyness"
    EXCITEMENT = "excitement"
    CALM = "calm"
    SADNESS = "sadness"
    ANXIETY = "anxiety"
    ANNOYANCE = "annoyance"


#: 维度 → 中文展示名表（摘要行与前端标签的单一真源）。
DIMENSION_LABELS: dict[str, str] = {
    EmotionDimension.JOY.value: "愉悦",
    EmotionDimension.AFFECTION.value: "亲昵",
    EmotionDimension.SHYNESS.value: "羞怯",
    EmotionDimension.EXCITEMENT.value: "兴奋",
    EmotionDimension.CALM.value: "平静",
    EmotionDimension.SADNESS.value: "低落",
    EmotionDimension.ANXIETY.value: "不安",
    EmotionDimension.ANNOYANCE.value: "微恼",
}


def _clamp(value: float, low: float, high: float) -> float:
    """数值夹取（闭区间——模型输出与配置输入的共同边界处理）。

    Args:
        value: 原始数值。
        low: 下界。
        high: 上界。

    Returns:
        夹取后的数值。
    """
    return max(low, min(high, value))


def _normalize_dimension_values(
    values: dict[str, float], *, low: float, high: float, source: str
) -> dict[str, float]:
    """维度字典归一（未知维度丢弃 + 数值夹取；debug 记录丢弃事件）。

    Args:
        values: 原始维度字典。
        low: 数值下界。
        high: 数值上界。
        source: 来源标识（日志用——``state`` / ``delta``）。

    Returns:
        归一后的维度字典（仅合法枚举值键）。
    """
    normalized: dict[str, float] = {}
    for key, value in values.items():
        try:
            dimension = EmotionDimension(key)
        except ValueError:
            logger.debug("情绪{}维度未知（已丢弃）: {!r}", source, key)
            continue
        clamped = _clamp(float(value), low, high)
        if clamped != float(value):
            logger.debug("情绪{}数值越界（已夹取）: {}={} -> {}", source, key, value, clamped)
        normalized[dimension.value] = clamped
    return normalized


class EmotionStateModel(BaseModel):
    """情绪状态文档（M 层——维度值 0..1 + 最近更新时刻）。

    Attributes:
        values: 维度值字典（仅 8 维枚举键；0..1 夹取；未出现的维度
            语义上等于中性基线——引擎按 :data:`DEFAULT_NEUTRAL_BASELINE`
            处理）。
        updated_at_ms: 最近更新时刻（epoch 毫秒——衰减计算的时间锚）。
    """

    values: dict[str, float] = Field(
        default_factory=dict, description="维度值（0..1；未出现维度按中性基线处理）"
    )
    updated_at_ms: int = Field(ge=0, description="最近更新时刻（epoch 毫秒——衰减锚点）")

    @field_validator("values", mode="after")
    @classmethod
    def _validate_values(cls, values: dict[str, float]) -> dict[str, float]:
        """维度值边界校验（未知维度丢弃 + 0..1 夹取——绝不抛异常）。

        Args:
            values: 原始维度值字典。

        Returns:
            归一后的维度值字典。
        """
        return _normalize_dimension_values(values, low=0.0, high=1.0, source="状态")


class EmotionDeltaModel(BaseModel):
    """情绪增量文档（单轮——维度增量 -1..1）。

    Attributes:
        values: 维度增量字典（仅 8 维枚举键；-1..1 夹取；正 = 增强、
            负 = 减弱）。
    """

    values: dict[str, float] = Field(
        default_factory=dict, description="维度增量（-1..1；正=增强 / 负=减弱）"
    )

    @field_validator("values", mode="after")
    @classmethod
    def _validate_values(cls, values: dict[str, float]) -> dict[str, float]:
        """维度增量边界校验（未知维度丢弃 + -1..1 夹取——绝不抛异常）。

        Args:
            values: 原始增量字典。

        Returns:
            归一后的增量字典。
        """
        return _normalize_dimension_values(values, low=-1.0, high=1.0, source="增量")


class EmotionConfig(BaseModel):
    """情绪引擎参数（构造注入旋钮——纯函数面直调 / 测试可控）。

    Attributes:
        decay_half_life_ms: 衰减半衰期（毫秒——偏离量按指数半衰期向
            中性基线回落；默认 6 小时）。
        neutral_baseline: 中性基线（0..1——全部维度无偏向的参考值；
            默认 0.5）。
        summary_threshold: 摘要阈值（偏离基线的可感幅度；默认 0.15——
            低于阈值的维度不进摘要 / 视为中性）。
    """

    decay_half_life_ms: int = Field(
        default=DEFAULT_DECAY_HALF_LIFE_MS,
        gt=0,
        description="衰减半衰期（毫秒——指数半衰期回落）",
    )
    neutral_baseline: float = Field(
        default=DEFAULT_NEUTRAL_BASELINE,
        ge=0.0,
        le=1.0,
        description="中性基线（0..1）",
    )
    summary_threshold: float = Field(
        default=DEFAULT_SUMMARY_THRESHOLD,
        gt=0.0,
        le=1.0,
        description="摘要阈值（偏离基线的可感幅度）",
    )


class EmotionSettings(BaseSettings):
    """情绪引擎环境开关（``EMOTION_`` env 前缀——pydantic-settings 直读）。

    锁定决策：``enabled=True``（默认）——与 intent 快径同类纯本地
    零成本特性：协议块只做输出格式指令，不额外消耗 token 预算以外的
    外部资源；不合规模型优雅降级（协议标签缺失 → 原文可见 + 零状态
    变化），故默认开启、需要时经 ``EMOTION_ENABLED=false`` 关闭。

    Attributes:
        enabled: 是否启用情绪引擎（False = 不读不写 M 态情绪、不注入
            心情块——回复链路行为与引擎接入前一致；协议标签仍被解析并
            以 meta 持久化，仅状态推进停用）。
        decay_half_life_ms: 衰减半衰期覆写（默认 6 小时）。
        neutral_baseline: 中性基线覆写（默认 0.5）。
        summary_threshold: 摘要阈值覆写（默认 0.15）。
    """

    enabled: bool = Field(default=True, description="是否启用情绪引擎（默认开启——纯本地零成本）")
    decay_half_life_ms: int = Field(
        default=DEFAULT_DECAY_HALF_LIFE_MS, gt=0, description="衰减半衰期（毫秒）"
    )
    neutral_baseline: float = Field(
        default=DEFAULT_NEUTRAL_BASELINE, ge=0.0, le=1.0, description="中性基线（0..1）"
    )
    summary_threshold: float = Field(
        default=DEFAULT_SUMMARY_THRESHOLD, gt=0.0, le=1.0, description="摘要阈值"
    )

    model_config = SettingsConfigDict(
        env_prefix="EMOTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def to_config(self) -> EmotionConfig:
        """环境参数 → 引擎配置（服务构造注入面）。

        Returns:
            与当前环境参数一致的 :class:`EmotionConfig`。
        """
        return EmotionConfig(
            decay_half_life_ms=self.decay_half_life_ms,
            neutral_baseline=self.neutral_baseline,
            summary_threshold=self.summary_threshold,
        )
