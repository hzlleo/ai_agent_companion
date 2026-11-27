"""LLM 条件路由策略 Model Classes——策略 / 条件 / 时段 / 裁决契约（Unit 文件）。

定位：角色路由从「单一角色 → 固定条目」扩展为「在本轮条件命中时把原角色
改由目标角色条目承接」。配置只声明**条件与目标角色**，求值算法在
:mod:`packages.harness.llm.route_policy`（模型与逻辑分文件）；模型条目本身
仍由 config 域注册表 / 环境面提供，本模块不读库、不读时钟、不发请求。

条件维度（AND 组合，全部满足才命中）：

    - 亲密度区间（``min_intimacy_level`` / ``max_intimacy_level``，0-3）；
    - 关系摘要可用性（``requires_summary`` 三态：仅可用 / 仅不可用 / 不约束）；
    - 时段窗口（``time_window``，IANA 时区 + 跨午夜窗口）；
    - 对话类型（``purposes``——非空时仅当本轮调用用途在集合内才命中；
      用途取调用链的 ``resolved_purpose``，即 ``chat`` / ``duty`` / ``dream``
      等既调用途名）。

依赖方向：pydantic / zoneinfo only（配置面不导入其他 harness 域，防环；
注册表条目与求值器在各自模块消费本契约）。
"""

from __future__ import annotations

from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "INTIMATE_BUILTIN_POLICY_NAME",
    "INTIMATE_BUILTIN_PRIORITY",
    "INTIMATE_TARGET_ROLE",
    "RouteConditionModel",
    "RouteDecisionModel",
    "RouteDecisionReason",
    "RoutePolicyModel",
    "TimeWindowModel",
]

#: 内置亲密策略的策略名（区别于用户策略名，日志与排障引用）。
INTIMATE_BUILTIN_POLICY_NAME: str = "__intimate_builtin"

#: 内置亲密策略的优先级（低于任何用户策略——用户策略可覆盖）。
INTIMATE_BUILTIN_PRIORITY: int = -999

#: 内置亲密策略的目标角色（独立通道条目在解析器中的路由别名）。
INTIMATE_TARGET_ROLE: str = "intimate"


class TimeWindowModel(BaseModel):
    """路由命中时段（IANA 时区 + 每日窗口；``end_hour<=start_hour`` 视为跨午夜）。

    Attributes:
        start_hour: 窗口起始小时（0-23，含）。
        end_hour: 窗口结束小时（0-23，不含；``<= start_hour`` 时跨午夜，
            例如 22→6 表示 22:00-23:59 与 00:00-05:59）。
        timezone: IANA 时区名（装配期经 ``zoneinfo.ZoneInfo`` 校验）。
    """

    model_config = ConfigDict(frozen=True)

    start_hour: int = Field(ge=0, le=23, description="窗口起始小时（0-23，含）")
    end_hour: int = Field(
        ge=0, le=23, description="窗口结束小时（0-23，不含；<= 起始小时视为跨午夜）"
    )
    timezone: str = Field(
        default="UTC", min_length=1, max_length=64, description="IANA 时区名（默认 UTC）"
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """校验时区名为合法 IANA 名（装配期拒绝，不在求值期失败）。

        Args:
            value: 原始时区名。

        Returns:
            去除首尾空白后的时区名。

        Raises:
            ValueError: 时区名不在系统 tzdata 中。
        """
        name = value.strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"非法时区名: {value!r}（须为 IANA 名，如 Asia/Shanghai）") from exc
        return name


class RouteConditionModel(BaseModel):
    """条件路由的命中条件（各维度 AND 组合；全不约束即恒命中）。

    Attributes:
        min_intimacy_level: 最低亲密等级（0-3；None = 不约束下沿）。
        max_intimacy_level: 最高亲密等级（0-3；None = 不约束上沿；
            与低沿同时出现时必须不小于低沿）。
        time_window: 时段窗口（None = 不约束时段）。
        requires_summary: 关系摘要可用性（True = 仅当摘要可用；False =
            仅当摘要不可用；None = 不约束）。
        purposes: 对话类型白名单（调用用途名，如 chat / duty / dream；
            非空时仅当本轮用途在集合内才命中，空 = 不约束）。
    """

    model_config = ConfigDict(frozen=True)

    min_intimacy_level: int | None = Field(
        default=None, ge=0, le=3, description="最低亲密等级（None=不约束下沿）"
    )
    max_intimacy_level: int | None = Field(
        default=None, ge=0, le=3, description="最高亲密等级（None=不约束上沿）"
    )
    time_window: TimeWindowModel | None = Field(
        default=None, description="时段窗口（None=不约束时段）"
    )
    requires_summary: bool | None = Field(
        default=None, description="摘要可用性（True=仅可用/False=仅不可用/None=不约束）"
    )
    purposes: tuple[str, ...] = Field(
        default=(), description="对话类型白名单（本轮调用用途名；空=不约束）"
    )

    @field_validator("purposes")
    @classmethod
    def _normalize_purposes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """归一用途名（去空白；空项视为配置错误）。

        Args:
            value: 原始用途名元组。

        Returns:
            去除首尾空白后的用途名元组。

        Raises:
            ValueError: 含空用途名。
        """
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("purposes 不能包含空用途名")
        return normalized

    @model_validator(mode="after")
    def _validate_intimacy_range(self) -> RouteConditionModel:
        """校验亲密度区间上下沿一致（下沿不得高于上沿）。

        Returns:
            自身（校验通过）。

        Raises:
            ValueError: ``max_intimacy_level < min_intimacy_level``。
        """
        if (
            self.min_intimacy_level is not None
            and self.max_intimacy_level is not None
            and self.max_intimacy_level < self.min_intimacy_level
        ):
            raise ValueError(
                "max_intimacy_level 不得小于 min_intimacy_level:"
                f" {self.min_intimacy_level} -> {self.max_intimacy_level}"
            )
        return self


class RoutePolicyModel(BaseModel):
    """单条条件路由策略（原角色 → 目标角色的条件化改道）。

    Attributes:
        name: 策略名（非空；日志与错误 details 引用）。
        source_role: 原角色（调用方声明的角色名）。
        target_role: 目标角色（命中后承接调用的解析器角色名，不得等于原角色）。
        priority: 优先级（降序求值；同优先级按声明序）。
        condition: 命中条件（AND 组合）。
        fallback_to_source: 目标角色未绑定条目时的回退语义（True = 回落原
            角色；False = 抛 ``MODEL_NOT_FOUND``）。
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="策略名（日志与错误 details 引用）")
    source_role: str = Field(min_length=1, description="原角色名")
    target_role: str = Field(min_length=1, description="目标角色名（不得等于原角色）")
    priority: int = Field(default=0, description="优先级（降序；同优先级按声明序）")
    condition: RouteConditionModel = Field(description="命中条件（AND 组合）")
    fallback_to_source: bool = Field(
        default=True, description="目标未绑定时回落原角色（False=抛 MODEL_NOT_FOUND）"
    )

    @field_validator("name", "source_role", "target_role")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        """去除首尾空白并拒绝空值（策略关键字段非空契约）。

        Args:
            value: 原始字段值。

        Returns:
            去除空白后的值。

        Raises:
            ValueError: 值为空 / 纯空白。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("策略关键字段（name/source_role/target_role）不能为空")
        return stripped

    @model_validator(mode="after")
    def _validate_distinct_roles(self) -> RoutePolicyModel:
        """校验目标角色不同于原角色（同角色改道无意义且掩盖配置错误）。

        Returns:
            自身（校验通过）。

        Raises:
            ValueError: ``target_role == source_role``。
        """
        if self.target_role == self.source_role:
            raise ValueError(
                f"target_role 不得等于 source_role: {self.source_role!r}"
            )
        return self


class RouteDecisionReason(StrEnum):
    """条件路由裁决原因值域（纯函数产出的可观测结论）。

    - ``direct``：无策略命中，按原角色直连；
    - ``routed``：策略命中且目标角色已绑定，改由目标角色承接；
    - ``fallback_unbound``：策略命中但目标角色未绑定，按策略回落原角色；
    - ``fallback_error``：策略命中但目标解析异常，按策略回落原角色。
    """

    DIRECT = "direct"
    ROUTED = "routed"
    FALLBACK_UNBOUND = "fallback_unbound"
    FALLBACK_ERROR = "fallback_error"


class RouteDecisionModel(BaseModel):
    """单轮条件路由裁决（纯函数产出——网关据此选择生效角色）。

    Attributes:
        effective_role: 本轮实际生效的角色名（直连 = 原角色）。
        policy_name: 命中的策略名（None = 无策略命中）。
        reason: 裁决原因。
    """

    model_config = ConfigDict(frozen=True)

    effective_role: str = Field(min_length=1, description="本轮实际生效的角色名")
    policy_name: str | None = Field(default=None, description="命中的策略名（None=未命中）")
    reason: RouteDecisionReason = Field(description="裁决原因")
