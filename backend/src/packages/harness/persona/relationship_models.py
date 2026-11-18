"""persona 域关系进展 Model Classes——阶段 / 摘要 / 事件 / 阈值表的传输契约。

关系进展摘要（relationship summary）是人格对「我们之间走到哪一步」的
**派生视图**：它不新增存储、不迁移表结构，只把人物事实层里**关系类**
（``PersonaFactCategory.RELATIONSHIP``）的现行事实聚合为一份可注入的
状态摘要。人物事实回答「对方是什么样的人」，本摘要回答「我们的关系处
在什么阶段、最近发生了什么、有哪些说好的边界」——两者同源，后者是
前者的确定性再加工。

证据来源与选取规则（锁定）：

    - 唯一来源是 ``ff_persona_facts`` 现行（``status=active``）关系类事实
      ——读取面是既有 :meth:`~packages.harness.persona.facts.FactsService.
      list_facts`（类别 + 状态过滤的公开读面），不新增查询路径、不复制
      存储；该读面按置信度降序返回，单次扫描上限即这一口径下的前 N 条
      （超出部分不参与本摘要——关系类事实量级远低于上限）；
    - 事实表本身没有独立的「重要度」列，``importance`` 由置信度
      ``confidence`` 线性映射（``confidence × importance_scale``，缺省
      ``×10``）——置信度是事实层里唯一的强度信号，映射系数集中在本模块
      的阈值表中；
    - 事件时间取事实行的 ``updated_at``（最近一次确认 / 更正时刻——
      同身份元数据刷新会推进该值），作为时间衰减的参考轴；
    - 边界约定不单设字段：关系类事实正文命中
      :data:`RELATIONSHIP_BOUNDARY_MARKERS` 中任一短语（「不要」「说好」
      「边界」这类明确约定措辞）即进入摘要的 ``boundaries`` 列表——
      规则确定性、可复核，宁窄勿宽。

得分与阶段（公式与阈值表文档化——实现见 ``persona.relationship``）::

    score = Σ importance × decay(now_ms - updated_at)
    decay 按事实年龄分档（≤7 天 1.0 / ≤30 天 0.75 / ≤90 天 0.5 /
    ≤365 天 0.25 / 更早 0.1）——新鲜的关系变化权重最高，陈旧事实逐步
    淡出，避免一条多年不动的称呼把阶段永久钉死；
    stage / intimacy_level 由 score 对阈值阶梯派生（stranger < 2.0 ≤
    familiar < 10.0 ≤ close < 24.0 ≤ intimate），intimacy_level 夹取
    0-3；空事实即 stranger / 0。

人格侧等级上限（决策与边界文档化）：预设 ``intimacy_level`` 是**生成期
过滤组件成熟度**的输入，不落在人格记录 / 卡片里，运行期聊天上下文拿不到
那个数值。因此本摘要**只输出事实派生值**，不做「不得越过人格侧等级」的
二次夹取；构建函数仍保留 ``intimacy_ceiling`` 形参供未来接线（拿到上限
时按 0-3 夹取，阶段随夹取后的等级回落），当前聊天链路传 ``None``。

依赖方向：pydantic / 标准库 + 人物事实值域枚举（DTO 层不触 ORM / 不触
服务）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.harness.persona.facts_models import PersonaFactCategory

__all__ = [
    "RELATIONSHIP_FACT_CATEGORY",
    "RELATIONSHIP_STAGE_FAMILIAR_MIN_SCORE",
    "RELATIONSHIP_STAGE_CLOSE_MIN_SCORE",
    "RELATIONSHIP_STAGE_INTIMATE_MIN_SCORE",
    "RELATIONSHIP_MAX_INTIMACY_LEVEL",
    "RELATIONSHIP_RECENT_EVENT_LIMIT",
    "RELATIONSHIP_EVENT_SCAN_LIMIT",
    "RELATIONSHIP_EVENT_SUMMARY_MAX_CHARS",
    "RELATIONSHIP_BOUNDARY_LIMIT",
    "RELATIONSHIP_IMPORTANCE_SCALE",
    "RELATIONSHIP_BLOCK_MAX_CHARS",
    "RELATIONSHIP_BOUNDARY_MARKERS",
    "DEFAULT_RELATIONSHIP_DECAY_BANDS",
    "DEFAULT_RELATIONSHIP_THRESHOLDS",
    "RelationshipStage",
    "RELATIONSHIP_STAGE_LABELS",
    "RelationshipDecayBandModel",
    "RelationshipEventModel",
    "RelationshipSummaryModel",
    "RelationshipThresholdsModel",
]

#: 关系进展摘要的唯一证据类别（人物事实层的封闭值域成员——见模块文档）。
RELATIONSHIP_FACT_CATEGORY: PersonaFactCategory = PersonaFactCategory.RELATIONSHIP

#: 阶段「熟悉」的得分下限（含边界——score 达到即进入该段）。
RELATIONSHIP_STAGE_FAMILIAR_MIN_SCORE: float = 2.0

#: 阶段「亲近」的得分下限。
RELATIONSHIP_STAGE_CLOSE_MIN_SCORE: float = 10.0

#: 阶段「亲密」的得分下限。
RELATIONSHIP_STAGE_INTIMATE_MIN_SCORE: float = 24.0

#: 亲密等级上界（0-3——与成熟度分层的等级轴同一值域）。
RELATIONSHIP_MAX_INTIMACY_LEVEL: int = 3

#: 摘要携带的最近事件条数上限（按时间倒序取最新——渲染面同帽）。
RELATIONSHIP_RECENT_EVENT_LIMIT: int = 5

#: 单次派生扫描的事实条数上限（分页读数上限——关系类事实量级远低于此，
#: 超出的历史事实不参与本摘要）。
RELATIONSHIP_EVENT_SCAN_LIMIT: int = 200

#: 单条事件摘要的字符上限（正文按字符截断——注入面逐行预算）。
RELATIONSHIP_EVENT_SUMMARY_MAX_CHARS: int = 80

#: 边界约定条数上限（按时间倒序取最新）。
RELATIONSHIP_BOUNDARY_LIMIT: int = 3

#: 置信度到重要度的线性映射系数（confidence 0-1 × 系数 = importance 0-10）。
RELATIONSHIP_IMPORTANCE_SCALE: float = 10.0

#: 关系进展块的硬字符帽（模板 + 阶段行 + 事件行 + 边界行的合计上界）。
RELATIONSHIP_BLOCK_MAX_CHARS: int = 1200

#: 边界约定识别短语（正文包含任一短语即视为边界——宁窄勿宽，见模块文档）。
RELATIONSHIP_BOUNDARY_MARKERS: tuple[str, ...] = (
    "不要",
    "不许",
    "不准",
    "不可以",
    "不能",
    "别再",
    "别忘",
    "说好",
    "约定",
    "边界",
    "约法",
)


class RelationshipStage(StrEnum):
    """关系阶段值域（摘要派生结果——非存储列）。

    - ``stranger``：初识（尚无关系类事实，或得分未达熟悉下限）；
    - ``familiar``：熟悉（关系类事实开始累积）；
    - ``close``：亲近（关系变化密集或近期信号强）；
    - ``intimate``：亲密（长期高置信关系事实的稳定沉淀）。
    """

    STRANGER = "stranger"
    FAMILIAR = "familiar"
    CLOSE = "close"
    INTIMATE = "intimate"


#: 阶段 → 中文称谓（提示词阶段行与文档的单一真源）。
RELATIONSHIP_STAGE_LABELS: dict[RelationshipStage, str] = {
    RelationshipStage.STRANGER: "初识",
    RelationshipStage.FAMILIAR: "熟悉",
    RelationshipStage.CLOSE: "亲近",
    RelationshipStage.INTIMATE: "亲密",
}


class RelationshipDecayBandModel(BaseModel):
    """时间衰减分档（事实年龄 → 权重；阈值表成员）。

    Attributes:
        age_days_max: 该档覆盖的最大事实年龄（天，含边界）；``None`` 表示
            最后一档无上限（只能出现在最后一项——校验强制）。
        weight: 该档的衰减权重（0-1 之间；年龄越大权重越低——校验强制
            非递增）。
    """

    model_config = ConfigDict(frozen=True)

    age_days_max: int | None = Field(
        default=None, ge=0, description="该档最大事实年龄（天，含边界；None = 无上限）"
    )
    weight: float = Field(gt=0, le=1, description="衰减权重（0-1）")


#: 缺省衰减分档表（锁定——年龄越大权重越低，最后一档无上限）。
DEFAULT_RELATIONSHIP_DECAY_BANDS: tuple[RelationshipDecayBandModel, ...] = (
    RelationshipDecayBandModel(age_days_max=7, weight=1.0),
    RelationshipDecayBandModel(age_days_max=30, weight=0.75),
    RelationshipDecayBandModel(age_days_max=90, weight=0.5),
    RelationshipDecayBandModel(age_days_max=365, weight=0.25),
    RelationshipDecayBandModel(age_days_max=None, weight=0.1),
)


class RelationshipThresholdsModel(BaseModel):
    """关系进展派生阈值表（锁定常量集中地——禁止在逻辑里散落魔数）。

    构造注入面：:class:`~packages.harness.chat.service.ChatService` 可注入
    本模型替换缺省表（测试 / 未来运营调参）；缺省值即
    :data:`DEFAULT_RELATIONSHIP_THRESHOLDS`（不新增 env 键）。

    校验不变式（装配错误即 fail loud）：阶段下限严格递增；衰减分档年龄
    严格递增、权重非递增、只有最后一档可以无上限；扫描上限不超过人物
    事实读面的分页上界（200）。

    Attributes:
        stage_familiar_min_score: 「熟悉」得分下限。
        stage_close_min_score: 「亲近」得分下限。
        stage_intimate_min_score: 「亲密」得分下限。
        max_intimacy_level: 亲密等级上界（0-3 夹取）。
        recent_event_limit: 摘要携带的最近事件条数上限。
        event_scan_limit: 单次派生扫描的事实条数上限（≤200）。
        event_summary_max_chars: 单条事件摘要字符上限。
        boundary_limit: 边界约定条数上限。
        importance_scale: 置信度到重要度的映射系数。
        decay_bands: 时间衰减分档表（按年龄升序）。
    """

    model_config = ConfigDict(frozen=True)

    stage_familiar_min_score: float = Field(
        default=RELATIONSHIP_STAGE_FAMILIAR_MIN_SCORE, gt=0, description="「熟悉」得分下限"
    )
    stage_close_min_score: float = Field(
        default=RELATIONSHIP_STAGE_CLOSE_MIN_SCORE, gt=0, description="「亲近」得分下限"
    )
    stage_intimate_min_score: float = Field(
        default=RELATIONSHIP_STAGE_INTIMATE_MIN_SCORE, gt=0, description="「亲密」得分下限"
    )
    max_intimacy_level: int = Field(
        default=RELATIONSHIP_MAX_INTIMACY_LEVEL, ge=0, le=3, description="亲密等级上界（0-3）"
    )
    recent_event_limit: int = Field(
        default=RELATIONSHIP_RECENT_EVENT_LIMIT, ge=1, le=50, description="最近事件条数上限"
    )
    event_scan_limit: int = Field(
        default=RELATIONSHIP_EVENT_SCAN_LIMIT, ge=1, le=200, description="事实扫描条数上限"
    )
    event_summary_max_chars: int = Field(
        default=RELATIONSHIP_EVENT_SUMMARY_MAX_CHARS, ge=16, le=500, description="事件摘要字符上限"
    )
    boundary_limit: int = Field(
        default=RELATIONSHIP_BOUNDARY_LIMIT, ge=0, le=20, description="边界约定条数上限"
    )
    importance_scale: float = Field(
        default=RELATIONSHIP_IMPORTANCE_SCALE, gt=0, description="置信度到重要度的映射系数"
    )
    decay_bands: tuple[RelationshipDecayBandModel, ...] = Field(
        default=DEFAULT_RELATIONSHIP_DECAY_BANDS, min_length=1, description="时间衰减分档表"
    )

    @model_validator(mode="after")
    def _validate_threshold_ladder(self) -> RelationshipThresholdsModel:
        """校验阈值阶梯与衰减分档的结构不变式。

        Returns:
            校验通过的模型自身。

        Raises:
            ValueError: 阶段下限非严格递增 / 衰减档年龄非严格递增 / 权重
                递增 / 非末档声明了无上限。
        """
        if not (
            self.stage_familiar_min_score
            < self.stage_close_min_score
            < self.stage_intimate_min_score
        ):
            raise ValueError("关系阶段得分下限必须严格递增（熟悉 < 亲近 < 亲密）")
        previous_age = -1
        previous_weight = 1.0
        for index, band in enumerate(self.decay_bands):
            is_last = index == len(self.decay_bands) - 1
            if band.age_days_max is None:
                if not is_last:
                    raise ValueError("只有最后一档衰减分档可以声明无上限（age_days_max=None）")
            elif band.age_days_max <= previous_age:
                raise ValueError("衰减分档的年龄上限必须严格递增")
            else:
                previous_age = band.age_days_max
            if band.weight > previous_weight:
                raise ValueError("衰减分档的权重必须随年龄非递增")
            previous_weight = band.weight
        return self


#: 缺省阈值表实例（不可变——ChatService 未注入时的锁定默认值）。
DEFAULT_RELATIONSHIP_THRESHOLDS: RelationshipThresholdsModel = RelationshipThresholdsModel()


class RelationshipEventModel(BaseModel):
    """关系进展事件（单条关系类事实的注入摘要——派生视图，非存储行）。

    Attributes:
        fact_id: 来源事实主键（排障回查锚点）。
        summary: 事实正文摘要（空白折叠 + 字符截断——注入行预算约束）。
        importance: 重要度（置信度 × 映射系数；0-10）。
        ts_ms: 事件时间（来源事实行 ``updated_at``——最近确认 / 更正时刻，
            epoch 毫秒；时间衰减的参考轴）。
    """

    model_config = ConfigDict(frozen=True)

    fact_id: int = Field(ge=1, description="来源事实主键")
    summary: str = Field(min_length=1, max_length=500, description="事实正文摘要（截断后）")
    importance: float = Field(ge=0, le=10, description="重要度（置信度映射；0-10）")
    ts_ms: int = Field(ge=0, description="事件时间（epoch 毫秒）")


class RelationshipSummaryModel(BaseModel):
    """关系进展摘要（compose 注入面的完整状态——派生视图，非 wire DTO）。

    Attributes:
        stage: 关系阶段（得分阶梯派生 + 等级夹取后回填）。
        intimacy_level: 亲密等级（0-3；与成熟度分层同轴）。
        score: 原始关系得分（衰减加权和——夹取不回写本字段）。
        event_count: 参与派生的事实条数（含未进入最近事件列表的条目）。
        recent_events: 最近事件摘要（时间倒序——渲染面再按预算截断）。
        boundaries: 边界约定摘要（关系类事实正文命中边界短语者）。
        generated_at_ms: 摘要生成时刻（调用方注入的 epoch 毫秒——模块内
            不读系统时钟）。
    """

    model_config = ConfigDict(frozen=True)

    stage: RelationshipStage = Field(description="关系阶段")
    intimacy_level: int = Field(ge=0, le=3, description="亲密等级（0-3）")
    score: float = Field(ge=0, description="原始关系得分（衰减加权和）")
    event_count: int = Field(ge=0, description="参与派生的事实条数")
    recent_events: list[RelationshipEventModel] = Field(
        default_factory=list, description="最近事件摘要（时间倒序）"
    )
    boundaries: list[str] = Field(default_factory=list, description="边界约定摘要")
    generated_at_ms: int = Field(ge=0, description="摘要生成时刻（epoch 毫秒）")
