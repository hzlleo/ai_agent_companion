"""rhythm 域 Model Classes——拟人节律层的配置 / 覆写 / 生效值（Unit 文件）。

模型分层（纯数据——计算全在 :mod:`packages.harness.rhythm.core`）::

    RhythmSettings          env 面配置（env_prefix=``RHYTHM_``——Settings.rhythm 分节）
        ↓ resolve_effective(settings, override)
    EffectiveRhythm         生效值（覆写合并 + 延迟缩放后的解析结果——core 消费）
    PersonaRhythmOverride   人格级覆写（全可选——per-persona 节律解析的原料，
                            心流层按人格注入）
    TypingSimulation        打字模拟结果（duration_ms + chars）
    RhythmGate              回复门（allowed + suggested_delay_ms + 中文 reason——
                            service 层产出 / heartflow 与 GUI 消费）

时区约定（naive 本地墙钟——scheduler 先例）：``bedtime`` / ``wake_time``
按进程本地时区的墙钟语义解析（``datetime.fromtimestamp`` 无 tz 参数，
与 ``Scheduler.register_daily`` 的 ``daily_at`` 同约定）；不引入
zoneinfo / DST 规则。``timezone_note`` 字段承载该约定的运维可见文档
（随 .env.example 生成物自文档化）。

HH:MM 校验本地化说明：config 域的 ``_validate_daily_at_shape`` 不可复用
——config/models.py 经 ``Settings.rhythm`` 聚合本模块（config → rhythm
单向），复用会造成 rhythm → config 环依赖；本模块自持同语义校验
（正则 + strip 后 fullmatch，与 config 域共享规则但物理独立）。

依赖方向：pydantic / 标准库 only（被 config 域聚合——严禁反向 import
config / scheduler / llm 等重依赖域，防环）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "RhythmSettings",
    "PersonaRhythmOverride",
    "EffectiveRhythm",
    "TypingSimulation",
    "RhythmGate",
]

#: 每日时刻 "HH:MM" 合法形态（24 小时制——bedtime / wake_time 共用；
#: 与 config 域 daily_at 同规则，本地自持防 import 环）。
_HHMM_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _validate_hhmm_shape(value: str) -> str:
    """校验 "HH:MM" 时刻形态（strip 后 fullmatch——24 小时制）。

    Args:
        value: 时刻字符串。

    Returns:
        校验通过的 strip 后原值。

    Raises:
        ValueError: 形态非法（装配错误面）。
    """
    stripped = value.strip()
    if not _HHMM_RE.fullmatch(stripped):
        raise ValueError(f"时刻须为 HH:MM 形态（24 小时制）: {value!r}")
    return stripped


class RhythmSettings(BaseSettings):
    """拟人节律层配置（env_prefix=``RHYTHM_``——``Settings.rhythm`` 分节）。

    锁定决策：``enabled=False``（默认）——节律门控 / 延迟是**拟人行为增强面**
    （缺省零行为变化：既有回复链路零延迟零门控），零配置开发可用、测试
    hermetic；``enabled=True`` 时 RhythmService 的门语义生效（心流层
    与 GUI 消费——本节律层仅交付门面与纯函数核心）。

    时区约定：``bedtime`` / ``wake_time`` 为 naive 本地墙钟（scheduler
    ``daily_at`` 先例——无时区数据库；``timezone_note`` 字段为运维可见
    的约定说明锚点）。

    Attributes:
        enabled: 是否启用节律门控（False = RhythmService 门直通——
            allowed=True / 延迟 0）。
        bedtime: 就寝时刻（"HH:MM" 24 小时制——睡眠窗口起点，含端点）。
        wake_time: 醒来时刻（"HH:MM" 24 小时制——睡眠窗口终点，不含端点；
            窗口可跨午夜，如 23:30 → 08:00）。
        timezone_note: 时区约定说明（naive 本地墙钟——文档锚点字段，
            不参与计算）。
        reply_delay_min_ms: 回复延迟下限毫秒（log-uniform 采样域下界；
            ≥1——对数域要求正值）。
        reply_delay_max_ms: 回复延迟上限毫秒（log-uniform 采样域上界；
            不得小于下限）。
        typing_speed_ms_per_char: 打字速度（毫秒/字符——模拟逐字输入）。
        typing_base_ms: 打字基础耗时毫秒（长度无关的起手耗时）。
        burst_probability: 快答概率（0-1——偶尔从快尾 [min, min*2) 采样）。
    """

    enabled: bool = Field(default=False, description="是否启用节律门控（False = 门直通）")
    bedtime: str = Field(default="23:30", description="就寝时刻（HH:MM 24 小时制，含端点）")
    wake_time: str = Field(default="08:00", description="醒来时刻（HH:MM 24 小时制，不含端点）")
    timezone_note: str = Field(
        default="naive-local",
        description="时区约定说明（naive 本地墙钟——scheduler daily_at 同约定；文档锚点不参与计算）",
    )
    reply_delay_min_ms: int = Field(
        default=1500, ge=1, description="回复延迟下限毫秒（log-uniform 采样域下界）"
    )
    reply_delay_max_ms: int = Field(
        default=45000, ge=1, description="回复延迟上限毫秒（log-uniform 采样域上界）"
    )
    typing_speed_ms_per_char: int = Field(default=45, ge=1, description="打字速度（毫秒/字符）")
    typing_base_ms: int = Field(default=800, ge=0, description="打字基础耗时毫秒（起手耗时）")
    burst_probability: float = Field(
        default=0.12, ge=0.0, le=1.0, description="快答概率（快尾采样机会）"
    )

    model_config = SettingsConfigDict(
        env_prefix="RHYTHM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("bedtime", "wake_time")
    @classmethod
    def _validate_hhmm(cls, value: str) -> str:
        """校验时刻形态（共享规则见 :func:`_validate_hhmm_shape`）。

        Args:
            value: 时刻字符串。

        Returns:
            校验通过的原值。

        Raises:
            ValueError: 形态非法（装配错误面）。
        """
        return _validate_hhmm_shape(value)

    @model_validator(mode="after")
    def _validate_delay_bounds(self) -> RhythmSettings:
        """校验延迟边界（max 不得小于 min——采样域有序性）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: reply_delay_max_ms < reply_delay_min_ms（装配错误面）。
        """
        if self.reply_delay_max_ms < self.reply_delay_min_ms:
            raise ValueError(
                f"reply_delay_max_ms({self.reply_delay_max_ms}) 不得小于 "
                f"reply_delay_min_ms({self.reply_delay_min_ms})"
            )
        return self


class PersonaRhythmOverride(BaseModel):
    """人格级节律覆写（全可选——per-persona 节律解析原料）。

    心流层按人格注入（人格记录 → 覆写 → ``resolve_effective`` 合并）；
    本节律层交付模型与解析函数，消费面在后继版本接线。

    Attributes:
        bedtime: 就寝时刻覆写（None = 沿用 RhythmSettings 值）。
        wake_time: 醒来时刻覆写（None = 沿用 RhythmSettings 值）。
        delay_scale: 温度缩放系数（0.5-2.0——回复延迟 min/max 同乘；
            <1 更敏捷 / >1 更慢热；None = 1.0 不缩放）。
    """

    bedtime: str | None = Field(default=None, description="就寝时刻覆写（None = 沿用全局值）")
    wake_time: str | None = Field(default=None, description="醒来时刻覆写（None = 沿用全局值）")
    delay_scale: float | None = Field(
        default=None, ge=0.5, le=2.0, description="延迟温度缩放（0.5-2.0；None = 1.0）"
    )

    @field_validator("bedtime", "wake_time")
    @classmethod
    def _validate_hhmm(cls, value: str | None) -> str | None:
        """校验时刻形态（None 直通——共享规则见 :func:`_validate_hhmm_shape`）。

        Args:
            value: 时刻字符串（None = 未覆写）。

        Returns:
            校验通过的原值（None 原样）。

        Raises:
            ValueError: 形态非法（装配错误面）。
        """
        if value is None:
            return None
        return _validate_hhmm_shape(value)


class EffectiveRhythm(BaseModel):
    """生效节律（resolve_effective 的解析结果——core 纯函数消费面）。

    覆写合并 + 延迟缩放后的终值：时刻字符串保留人读形态，同时物化
    分钟数（``bedtime_minute`` / ``wake_minute``——core 免逐调用重解析）；
    延迟边界为缩放后值（缩放后下限夹逼 ≥1ms——log-uniform 对数域要求）。

    Attributes:
        bedtime: 生效就寝时刻（"HH:MM"）。
        wake_time: 生效醒来时刻（"HH:MM"）。
        bedtime_minute: 就寝时刻的当日分钟数（0-1439）。
        wake_minute: 醒来时刻的当日分钟数（0-1439）。
        reply_delay_min_ms: 生效回复延迟下限毫秒（缩放后）。
        reply_delay_max_ms: 生效回复延迟上限毫秒（缩放后）。
        typing_speed_ms_per_char: 打字速度（毫秒/字符）。
        typing_base_ms: 打字基础耗时毫秒。
        burst_probability: 快答概率（0-1）。
        delay_scale: 生效延迟缩放系数（覆写值或 1.0）。
    """

    bedtime: str = Field(description="生效就寝时刻（HH:MM）")
    wake_time: str = Field(description="生效醒来时刻（HH:MM）")
    bedtime_minute: int = Field(ge=0, le=1439, description="就寝时刻当日分钟数")
    wake_minute: int = Field(ge=0, le=1439, description="醒来时刻当日分钟数")
    reply_delay_min_ms: int = Field(ge=1, description="生效延迟下限毫秒（缩放后）")
    reply_delay_max_ms: int = Field(ge=1, description="生效延迟上限毫秒（缩放后）")
    typing_speed_ms_per_char: int = Field(ge=1, description="打字速度（毫秒/字符）")
    typing_base_ms: int = Field(ge=0, description="打字基础耗时毫秒")
    burst_probability: float = Field(ge=0.0, le=1.0, description="快答概率")
    delay_scale: float = Field(ge=0.5, le=2.0, description="生效延迟缩放系数")


class TypingSimulation(BaseModel):
    """打字模拟结果（typing_duration_ms 的输出契约）。

    Attributes:
        duration_ms: 模拟打字耗时毫秒（base + 每字符速度累计，封顶
            :data:`packages.harness.rhythm.core.TYPING_DURATION_CAP_MS`）。
        chars: 输入文本长度（字符数——观测面）。
    """

    duration_ms: int = Field(ge=0, description="模拟打字耗时毫秒（封顶后）")
    chars: int = Field(ge=0, description="文本字符数")


class RhythmGate(BaseModel):
    """回复门（RhythmService.gate_reply 的输出契约）。

    消费面：心流层（回复调度决策）与 GUI（状态展示）——本节律层
    交付门面与契约，回复链路暂不消费（文档化前向兼容）。

    Attributes:
        allowed: 是否允许即时回复（睡眠窗口内为 False）。
        suggested_delay_ms: 建议延迟毫秒（allowed 时 = 拟人回复延迟采样值；
            不允许时 = 距下次醒来的毫秒数——调用方调度参考；直通形态为 0）。
        reason: 门判定中文说明（观测 / 日志面）。
    """

    allowed: bool = Field(description="是否允许即时回复")
    suggested_delay_ms: int = Field(ge=0, description="建议延迟毫秒（语义随 allowed 切换）")
    reason: str = Field(min_length=1, description="门判定中文说明")
