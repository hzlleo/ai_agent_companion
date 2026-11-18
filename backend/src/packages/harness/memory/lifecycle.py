"""memory 域记忆生命周期——时间衰减 / 使用强化 / 归档判定的纯函数面。

设计定位（记忆生命周期——**纯数学 Unit 模块**，零 I/O、零模型调用）：

    - :class:`LifecycleConfig` —— 生命周期构造级参数（衰减半衰期 /
      强化权重 / 归档阈值 / 最小年龄 / 置顶豁免——构造注入式旋钮，
      **无 env 键**；与 DreamJobConfig 同模式）；
    - :func:`effective_strength` —— 记忆有效强度（时间衰减 × 使用强化
      的复合权重；置顶行免于衰减）；
    - :func:`should_archive` —— 归档判定（不置顶 / 不冻结 / 现行
      active / 年龄超阈值 / 强度低于阈值——五条同时满足才归档）。

数学定义（精确可测——测试以浮点近似断言锁定）::

    age_days = max(0, (now_ms - anchor_ms) / MS_PER_DAY)
    reinforcement = 1 + log1p(access_count) * reinforcement_weight
    ordinary   = importance * 0.5 ** (age_days / decay_half_life_days) * reinforcement
    pinned     = importance * reinforcement
                 （pinned_decay_exempt=True 时不乘衰减因子）

    should_archive ⇔ status == active
                     ∧ not pinned ∧ not frozen
                     ∧ age_days > min_age_days
                     ∧ effective_strength < archive_threshold

降级与豁免语义（锁定决策）：

    - **置顶豁免**：``pinned=True`` 行既免于衰减（强度恒不衰减）也**永不
      归档**（``should_archive`` 硬门）——主人显式置顶是最高优先级意图；
    - **冻结豁免**：``frozen=True`` 行永不归档（且梦境合并面不消费——
      见 memory.dreaming 冻结主题过滤）；冻结不改变强度计算（冻结行
      若出现在读侧展示仍按普通公式呈现强度——展示与判定语义分离，
      判定由 ``should_archive`` 承担）；
    - **仅现行 active 行可归档**：superseded / archived 行不在候选面
      （``should_archive`` 显式校验 status——调用方即使误传也不误归档）；
    - 未来时间戳（``anchor_ms > now_ms``）按 age=0 处理（不奖励未来
      时间——与 dreaming.recency_decay 同口径）。

归档阈值语义：``effective_strength < archive_threshold`` 为严格小于
（阈值边界不归档——阈值是「低于即归档」线）。

读取面强度：``CuratedRecordDTO.effective_strength`` 由服务层在**读时**
以本模块公式计算（读写一致：同一 config 同一下行值恒同强度——无落库
冗余列，见 docs/backend/memory.md 生命周期一节）。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from packages.harness.memory.models import CuratedStatus

__all__ = [
    "MS_PER_DAY",
    "LifecycleConfig",
    "effective_strength",
    "should_archive",
]

#: 一天的毫秒数（epoch 毫秒轴的换算基准——与 dreaming.MS_PER_DAY 同值）。
MS_PER_DAY: int = 86_400_000


class LifecycleConfig(BaseModel):
    """记忆生命周期构造级参数（衰减 / 强化 / 归档判定的旋钮面）。

    全部旋钮经构造函数注入（**无 env 键**——与 DreamJobConfig 同模式；
    默认值与产品基线一致，改动经代码评审而非部署配置）。

    Attributes:
        decay_half_life_days: 时间衰减半衰期（天——记忆距今每过一个
            半衰期，衰减因子减半；指数衰减语义）。
        reinforcement_weight: 使用强化权重（检索命中次数的对数增益
            系数——``1 + log1p(access_count) * weight``）。
        archive_threshold: 归档强度阈值（有效强度严格小于该值且满足
            其余条件的现行记忆由梦境巩固归档）。
        min_age_days: 归档最小年龄（天——年龄不超过该值的记忆不归档，
            防止新记忆在首个巩固周期即被清退）。
        pinned_decay_exempt: 置顶行免衰减开关（默认 True——置顶记忆的
            强度不随时间衰减；False 时置顶行仍按普通公式计算，仅保留
            「不归档」豁免）。
    """

    decay_half_life_days: float = Field(
        default=30.0, gt=0, description="时间衰减半衰期（天）"
    )
    reinforcement_weight: float = Field(
        default=0.25, ge=0, description="使用强化权重（log1p 增益系数）"
    )
    archive_threshold: float = Field(
        default=1.0, ge=0, description="归档强度阈值（严格小于才归档）"
    )
    min_age_days: float = Field(default=7.0, ge=0, description="归档最小年龄（天）")
    pinned_decay_exempt: bool = Field(
        default=True, description="置顶行免衰减开关（默认开）"
    )


def effective_strength(
    importance: int,
    anchor_ms: int,
    access_count: int,
    now_ms: int,
    config: LifecycleConfig,
    *,
    pinned: bool = False,
) -> float:
    """记忆有效强度（时间衰减 × 使用强化的复合权重——纯函数）。

    公式见模块 docstring：普通行按半衰期指数衰减并乘以检索命中强化
    因子；置顶行在 ``config.pinned_decay_exempt=True`` 时不乘衰减因子。
    防御归一：``importance`` 负数按 0、``access_count`` 负数按 0、
    未来锚点按 age=0（不奖励未来时间）。

    Args:
        importance: 重要度（1-10——服务层保证；越界按原值参与计算）。
        anchor_ms: 记忆锚点时间（epoch 毫秒——行创建时刻；衰减年龄
            的起算点）。
        access_count: 检索命中累计次数（写读强化轴）。
        now_ms: 参考当前时间（epoch 毫秒——注入式，测试可控）。
        config: 生命周期参数（半衰期 / 强化权重 / 置顶豁免开关）。
        pinned: 是否置顶行（免衰减判定输入）。

    Returns:
        有效强度（>= 0 的浮点；重要度为 0 时恒 0）。
    """
    reinforcement = 1.0 + math.log1p(max(0, access_count)) * config.reinforcement_weight
    base = max(0, importance) * reinforcement
    if pinned and config.pinned_decay_exempt:
        return float(base)
    age_days = max(0, now_ms - anchor_ms) / MS_PER_DAY
    decay = float(0.5 ** (age_days / config.decay_half_life_days))
    return float(base * decay)


def should_archive(
    *,
    importance: int,
    anchor_ms: int,
    access_count: int,
    pinned: bool,
    frozen: bool,
    status: CuratedStatus | str,
    now_ms: int,
    config: LifecycleConfig,
) -> bool:
    """归档判定（生命周期门矩阵——五条同时满足才归档，纯函数）。

    门矩阵（按序短路，任一不满足即 False）：

        1. ``status == active``（仅现行行可归档——superseded / archived
           不在候选面）；
        2. ``not pinned``（置顶行永不归档——主人显式意图最高优先）；
        3. ``not frozen``（冻结行永不归档）；
        4. ``age_days > config.min_age_days``（新记忆不在首个巩固周期
           被清退；年龄取 ``now_ms - anchor_ms``，未来锚点按 0）；
        5. ``effective_strength(...) < config.archive_threshold``
           （严格小于——阈值边界不归档）。

    Args:
        importance: 重要度（1-10）。
        anchor_ms: 记忆锚点时间（epoch 毫秒——行创建时刻）。
        access_count: 检索命中累计次数。
        pinned: 是否置顶。
        frozen: 是否冻结。
        status: 记忆状态（枚举或字符串值——非 active 一律不归档）。
        now_ms: 参考当前时间（epoch 毫秒——注入式，测试可控）。
        config: 生命周期参数。

    Returns:
        True = 本次巩固应归档该记忆。
    """
    if CuratedStatus(status) != CuratedStatus.ACTIVE:
        return False
    if pinned or frozen:
        return False
    age_days = max(0, now_ms - anchor_ms) / MS_PER_DAY
    if age_days <= config.min_age_days:
        return False
    strength = effective_strength(
        importance,
        anchor_ms,
        access_count,
        now_ms,
        config,
        pinned=pinned,
    )
    return strength < config.archive_threshold
