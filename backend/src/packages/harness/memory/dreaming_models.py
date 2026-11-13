"""memory 域梦境整理 DTO——夜间梦境巩固任务的传输契约（Unit 文件，P1 chunk 3）。

契约分层：

    - :class:`DreamRunStatus`        —— 运行状态值域（running / success /
      failed——运行日志行的生命周期轴）；
    - :class:`DreamDecisionAction`   —— 逐事件决策动作值域（promoted /
      skipped / merged——语义见 :class:`DreamDecisionModel`）；
    - :class:`DreamJobConfig`        —— 梦境任务构造级参数（窗口 / 衰减
      半衰期 / 分数门 / 候选上限 / 主题相关度加成 / 日记开关——**构造
      注入式旋钮**，与 llm 网关 retry 旋钮同模式；本 chunk 不落 config
      域，迁移到 env 面属后续 chunk——文档化决策）；
    - :class:`DreamRunStatsModel`    —— 运行统计（候选 / 过门 / 晋升 /
      并入 / untrusted 跳过 / 提案丢弃计数——落 DreamRunRow.stats JSON）；
    - :class:`DreamDecisionModel`    —— 逐事件决策记录（落
      DreamRunRow.decisions JSON；既往成功运行中的 promoted / merged
      决策构成「已晋升事件集」——后续运行的跳过依据）；
    - :class:`DreamProposedItemModel` —— LLM 合并提案条目（梦境模型
      严格 JSON 数组输出的元素契约——校验失败即丢弃计数）；
    - :class:`DreamRunRecordDTO`     —— 运行记录读取面（ORM 行归一）；
    - :class:`DreamRunQueryDTO`      —— 运行日志游标分页查询面；
    - :class:`DreamTriggerRequestDTO` —— 手工触发请求体（persona_id 可选
      + cfg 参数覆写）；
    - :data:`DreamTriggerResultDTO`  —— 触发响应面（DreamRunRecordDTO
      别名——触发即返回运行记录本身）。

时间语义：全部 epoch 毫秒；候选窗口**左闭右开**
（``now - window_hours <= ts_ms < now``——与情景事件查询约定一致）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from packages.harness.memory.models import CuratedKind

__all__ = [
    "DreamRunStatus",
    "DreamDecisionAction",
    "DreamJobConfig",
    "DreamRunStatsModel",
    "DreamDecisionModel",
    "DreamProposedItemModel",
    "DreamRunRecordDTO",
    "DreamRunQueryDTO",
    "DreamTriggerRequestDTO",
    "DreamTriggerResultDTO",
]


class DreamRunStatus(StrEnum):
    """梦境运行状态值域（ff_dream_runs.status）。

    - ``running``：运行中（行创建即落库——进程崩溃可能遗留终态缺失行，
      P3 调度器负责收敛孤儿 running 行，文档化决策）；
    - ``success``：成功（合并 + 日记全部完成或按规则跳过）；
    - ``failed``：失败（LLM 输出不可解析 / 调用失败 / 日记生成失败——
      fail-closed 语义见 dreaming 模块文档）。
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class DreamDecisionAction(StrEnum):
    """逐事件决策动作值域（DreamDecisionModel.action）。

    - ``promoted``：事件内容经 LLM 蒸馏进入**新起链**的策展主题
      （upsert 落 version=1——事件被晋升为记忆）；
    - ``merged``：事件内容并入**既有 active 主题**（upsert 推进
      supersede 链，version > 1——事件并入既有记忆）；
    - ``skipped``：未参与晋升（低于分数线 / 既往已晋升 / 超出候选
      上限 / 提案未收录或被丢弃）。
    """

    PROMOTED = "promoted"
    SKIPPED = "skipped"
    MERGED = "merged"


class DreamJobConfig(BaseModel):
    """梦境任务构造级参数（构造注入旋钮——与 llm 网关 retry 旋钮同模式）。

    本 chunk 不修改 config 域（锁定决策）：全部旋钮经构造函数 / 请求体
    覆写注入；向 Settings env 面的迁移属后续 chunk（届时本类保留为
    纯配置形态，与 SeekDBStoreConfig 的装配映射同模式）。

    Attributes:
        window_hours: 候选窗口宽度（小时；窗口 ``[now - window_hours,
            now)`` 左闭右开）。
        recency_half_life_days: 时间衰减半衰期（天——事件距今每过
            一个半衰期，recency 衰减一半；指数衰减语义）。
        min_score: 梦境分晋级阈值（score >= min_score 才入 LLM 候选）。
        max_candidates: 单次运行候选事件上限（按分数降序截断）。
        topic_relevance_limit: 主题相关度判定的检索 top-N（每个既有
            active 主题经存储 keyword 检索取 top-N 事件构成命中集）。
        boost: 主题相关度加成系数（命中既有主题检索的事件得分乘
            ``1 + boost``）。
        diary_enabled: 日记生成开关（关闭则跳过日记阶段）。
        diary_importance: 日记事件的 important 度（kind=dream 落库）。
    """

    window_hours: int = Field(default=24, ge=1, le=24 * 30, description="候选窗口宽度（小时）")
    recency_half_life_days: float = Field(default=30.0, gt=0, description="时间衰减半衰期（天）")
    min_score: float = Field(default=1.0, ge=0, description="梦境分晋级阈值")
    max_candidates: int = Field(default=50, ge=1, le=500, description="单次候选事件上限")
    topic_relevance_limit: int = Field(default=3, ge=1, le=50, description="主题相关度检索 top-N")
    boost: float = Field(default=0.5, ge=0, le=10, description="主题相关度加成系数")
    diary_enabled: bool = Field(default=True, description="日记生成开关")
    diary_importance: int = Field(default=5, ge=1, le=10, description="日记事件重要度")


class DreamRunStatsModel(BaseModel):
    """梦境运行统计（ff_dream_runs.stats JSON 的类型化形态）。

    计数口径（文档化——与决策动作对齐）：

        - ``candidates``：窗口内**可信**（非 untrusted）事件数（untrusted
          在打分前即被硬排除——只计入 skipped_untrusted）；
        - ``gated``：通过规则门进入 LLM 合并的事件数（分数达标 + 未被
          既往晋升排除 + 上限截断后）；
        - ``promoted``：决策动作 = promoted 的事件数（晋升进新起链主题）；
        - ``superseded``：决策动作 = merged 的事件数（并入既有 active
          主题链）；
        - ``skipped_untrusted``：窗口内被硬排除的 untrusted 事件数
          （永不晋升、永不进入任何梦境 prompt——provenance 硬门）；
        - ``failed``：LLM 提案中被校验丢弃的条目数（merged_from 引用
          未知事件 / 字段校验失败——逐条丢弃计数，不整体失败）。

    Attributes:
        candidates: 可信候选事件数。
        gated: 过门事件数。
        promoted: 晋升事件数（新起链）。
        superseded: 并入事件数（推进既有链）。
        skipped_untrusted: untrusted 硬排除计数。
        failed: 提案丢弃计数。
    """

    candidates: int = Field(default=0, ge=0, description="可信候选事件数")
    gated: int = Field(default=0, ge=0, description="过门事件数")
    promoted: int = Field(default=0, ge=0, description="晋升事件数（新起链）")
    superseded: int = Field(default=0, ge=0, description="并入事件数（推进既有链）")
    skipped_untrusted: int = Field(default=0, ge=0, description="untrusted 硬排除计数")
    failed: int = Field(default=0, ge=0, description="提案丢弃计数")


class DreamDecisionModel(BaseModel):
    """逐事件决策记录（ff_dream_runs.decisions JSON 的元素形态）。

    决策覆盖口径：**全部可信候选事件**（窗口内非 untrusted 事件——
    无论过门与否每人一条；untrusted 事件不可见、不产生决策）。
    既往成功运行中 action ∈ {promoted, merged} 的 episode_id 集合构成
    后续运行的「已晋升」跳过依据。

    Attributes:
        episode_id: 情景事件主键（决策主体）。
        topic: 关联策展主题键（纯跳过决策为空串）。
        kind: 关联策展类型（纯跳过决策为 None）。
        action: 决策动作（promoted / skipped / merged）。
        reason: 人读决策理由（跳过原因 / 晋升去向说明）。
    """

    episode_id: int = Field(description="情景事件主键")
    topic: str = Field(default="", description="关联策展主题键（纯跳过为空串）")
    kind: CuratedKind | None = Field(default=None, description="关联策展类型（纯跳过为 None）")
    action: DreamDecisionAction = Field(description="决策动作")
    reason: str = Field(default="", description="决策理由")


class DreamProposedItemModel(BaseModel):
    """LLM 合并提案条目（梦境模型输出的严格 JSON 数组元素契约）。

    校验语义（dreaming 编排逐条校验，失败丢弃并计数 failed）：
    kind 枚举 / importance 边界 / topic 非空由本 Model Class 边界强制；
    ``merged_from ⊆ 候选事件 id 集`` 由编排校验（需要候选上下文）；
    空来源列表视为无效提案（晋升内容必须有事件背书）。

    Attributes:
        topic: 主题键（并入既有主题时沿用其 topic）。
        kind: 策展类型（memory / user_note / relationship）。
        content: 蒸馏后的记忆正文。
        importance: 重要度（1-10）。
        merged_from: 来源候选事件主键列表（至少一条）。
    """

    topic: str = Field(min_length=1, max_length=255, description="主题键")
    kind: CuratedKind = Field(description="策展类型")
    content: str = Field(min_length=1, description="蒸馏后的记忆正文")
    importance: int = Field(ge=1, le=10, description="重要度（1-10）")
    merged_from: list[int] = Field(min_length=1, description="来源候选事件主键列表（至少一条）")


class DreamRunRecordDTO(BaseModel):
    """梦境运行记录读取 DTO（ORM 行归一）。

    Attributes:
        id: 运行主键。
        persona_id: 所属人格 ID。
        status: 运行状态（running / success / failed）。
        started_ms: 开始时间（epoch 毫秒）。
        finished_ms: 结束时间（epoch 毫秒；运行中为 None）。
        stats: 运行统计。
        decisions: 逐事件决策列表。
        diary_episode_id: 日记事件主键（未生成 / 失败为 None）。
        error: 失败原因（成功为 None）。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = {"from_attributes": True, "protected_namespaces": ()}

    id: int = Field(description="运行主键")
    persona_id: str = Field(description="所属人格 ID")
    status: DreamRunStatus = Field(description="运行状态")
    started_ms: int = Field(description="开始时间（epoch 毫秒）")
    finished_ms: int | None = Field(default=None, description="结束时间（运行中为 None）")
    stats: DreamRunStatsModel = Field(description="运行统计")
    decisions: list[DreamDecisionModel] = Field(default_factory=list, description="逐事件决策")
    diary_episode_id: int | None = Field(default=None, description="日记事件主键")
    error: str | None = Field(default=None, description="失败原因（成功为 None）")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class DreamRunQueryDTO(BaseModel):
    """梦境运行日志查询 DTO（游标分页——id 降序，新运行在前）。

    Attributes:
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class DreamTriggerRequestDTO(BaseModel):
    """手工触发梦境整理请求体（POST /api/v1/memories/dream/trigger）。

    与检索请求体同构：``persona_id`` 可选（缺省当前激活人格，无激活
    404）；``cfg`` 为构造级参数覆写（嵌套部分覆写——仅出现字段生效，
    其余取 DreamJobConfig 默认值）。

    Attributes:
        persona_id: 目标人格主键（None = 当前激活人格）。
        cfg: 梦境任务参数覆写（缺省全默认——DreamJobConfig 各字段）。
    """

    persona_id: int | None = Field(
        default=None, ge=1, description="目标人格主键（None = 当前激活人格）"
    )
    cfg: DreamJobConfig = Field(
        default_factory=DreamJobConfig, description="任务参数覆写（缺省全默认）"
    )


#: 触发响应面——触发即返回运行记录本身（手工触发的产物就是一份运行日志；
#: 响应 data 即 DreamRunRecordDTO，别名仅为请求 / 响应对命名完整）。
DreamTriggerResultDTO = DreamRunRecordDTO
