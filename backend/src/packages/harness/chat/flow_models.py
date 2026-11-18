"""chat 域心流门控 Model Classes——判定输入 / 决策 / 会话节拍状态（Unit 文件）。

心流门控决定「这条入站消息现在该不该开口」：聊天生成前的判定层把
节律层（作息 / 拟人延迟）、会话消息密度、未读积压与关系摘要折算成一条
结构化决策——立即回复 / 延迟回复 / 连发合并 / 不回复。状态模型只承载
数据，判定矩阵与合并规则在 :mod:`packages.harness.chat.flow_gate`。

时间字段一律 epoch 毫秒（``now_ms`` 由调用方注入——模型层不读系统时钟）。
默认配置 ``enabled=False``：门控关闭时回复链路与门控引入前完全一致
（既有单聊 / 群聊行为零变化）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.persona.relationship_models import RelationshipSummaryModel

__all__ = [
    "FLOW_ARRIVAL_HISTORY_LIMIT",
    "FLOW_BUFFERED_TEXT_LIMIT",
    "FlowGateSettings",
    "FlowVerdict",
    "FlowReason",
    "FlowRhythmStateModel",
    "FlowChatStateModel",
    "FlowGateInputModel",
    "FlowGateDecisionModel",
    "MergedBurstModel",
]

#: 判定面保留的最近入站时刻条数上限（密度 / 连发窗口计算原料——超出丢弃最老）。
FLOW_ARRIVAL_HISTORY_LIMIT: int = 32

#: 延迟缓冲保留的待回复文本条数上限（合并阶段再按 ``merge_max_messages`` 裁剪）。
FLOW_BUFFERED_TEXT_LIMIT: int = 32


class FlowGateSettings(BaseSettings):
    """心流门控配置（env_prefix=``FLOW_``——``Settings.flow`` 分节）。

    默认关闭：门控关闭时 ``ChannelRouter`` 直接按既有路径回复（不排程、
    不合并、不查询关系摘要）；开启后所有阈值由本分节控制，缺省值按
    「正常社交节奏」调校（短连发合并、长积压延迟、深夜静默）。

    Attributes:
        enabled: 是否启用门控（False = 直通——回复链路与引入前完全一致）。
        burst_window_ms: 连发窗口毫秒（该窗口内达到 ``burst_count`` 条即
            触发合并——窗口外消息不参与连发计数）。
        burst_count: 连发合并阈值（窗口内达到该条数转合并）。
        merge_quiet_ms: 合并静默毫秒（最后一次入站之后等待该时长再回复，
            让后续连发进入同一批）。
        merge_max_messages: 单次合并回复承载的消息条数上限（超出保留
            最近的条目，丢弃最老并记日志）。
        min_reply_gap_ms: 两条回复之间的最小间隔毫秒（短于该间隔的追发
            转延迟，避免刷屏式应答）。
        backlog_defer_threshold: 未读积压阈值（积压达到该条数转延迟回复）。
        backlog_defer_step_ms: 积压每超出阈值一条追加的延迟毫秒。
        backlog_defer_max_ms: 积压延迟上限毫秒（防止无限推迟）。
        pending_grace_ms: 已排程延迟任务的宽限毫秒（排程时刻超过该宽限
            仍未执行视为任务丢失，后续入站重新排程）。
    """

    enabled: bool = Field(default=False, description="是否启用心流门控（False = 直通）")
    burst_window_ms: int = Field(
        default=4000, ge=0, description="连发窗口毫秒（窗口内计数）"
    )
    burst_count: int = Field(default=3, ge=2, le=20, description="连发合并阈值（窗口内条数）")
    merge_quiet_ms: int = Field(
        default=2500, ge=0, description="合并静默毫秒（最后一次入站后的等待时长）"
    )
    merge_max_messages: int = Field(
        default=5, ge=1, le=20, description="单次合并承载的消息条数上限"
    )
    min_reply_gap_ms: int = Field(
        default=1200, ge=0, description="两条回复之间的最小间隔毫秒"
    )
    backlog_defer_threshold: int = Field(
        default=6, ge=1, description="未读积压转延迟的条数阈值"
    )
    backlog_defer_step_ms: int = Field(
        default=8000, ge=0, description="积压每超出阈值一条追加的延迟毫秒"
    )
    backlog_defer_max_ms: int = Field(
        default=90000, ge=0, description="积压延迟上限毫秒"
    )
    pending_grace_ms: int = Field(
        default=60000, ge=0, description="延迟任务丢失判定宽限毫秒"
    )

    model_config = SettingsConfigDict(
        env_prefix="FLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class FlowVerdict(StrEnum):
    """门控裁决值域。

    - ``REPLY``：立即进入聊天生成与投递；
    - ``DEFER``：延迟回复（按 ``delay_ms`` 排程，复用中央调度器）；
    - ``MERGE``：连发合并（延迟到静默窗口结束后以合并文本回复一次）；
    - ``SKIP``：不回复（机器事件 / 无回复诉求的入站）。
    """

    REPLY = "reply"
    DEFER = "defer"
    MERGE = "merge"
    SKIP = "skip"


class FlowReason(StrEnum):
    """门控理由值域（观测 / 日志面——判定矩阵的稳定标识）。

    - ``DISABLED``：门控未启用（直通形态）；
    - ``SYNTHETIC_EVENT``：机器生成事件（不要求人类回合式回复）；
    - ``SLEEP_WINDOW``：处于睡眠窗口（静默至醒来）；
    - ``BURST_MERGE``：连发达到阈值（合并为一次回复）；
    - ``DENSITY_PACING``：回复间隔过密（按最小间隔延迟）；
    - ``BACKLOG_DEFER``：未读积压过高（延迟消化）；
    - ``RELATIONSHIP_PRIORITY``：亲近 / 亲密关系优先应答（跳过密度与
      积压延迟）；
    - ``RHYTHM_DELAY``：节律层拟人延迟（清醒时段的正常节拍）；
    - ``NORMAL``：无特殊信号，立即回复。
    """

    DISABLED = "disabled"
    SYNTHETIC_EVENT = "synthetic_event"
    SLEEP_WINDOW = "sleep_window"
    BURST_MERGE = "burst_merge"
    DENSITY_PACING = "density_pacing"
    BACKLOG_DEFER = "backlog_defer"
    RELATIONSHIP_PRIORITY = "relationship_priority"
    RHYTHM_DELAY = "rhythm_delay"
    NORMAL = "normal"


class FlowRhythmStateModel(BaseModel):
    """节律层现状（门控输入的节律快照——由 RhythmService 门判定产出）。

    Attributes:
        allowed: 节律层是否允许即时回复（睡眠窗口内为 False）。
        suggested_delay_ms: 节律层建议延迟毫秒（睡眠窗口 = 距醒来；清醒 =
            拟人延迟采样值；节律层停用 = 0）。
    """

    allowed: bool = Field(description="节律层是否允许即时回复")
    suggested_delay_ms: int = Field(ge=0, description="节律层建议延迟毫秒")


class FlowChatStateModel(BaseModel):
    """单个会话的心流节拍状态（内存态——路由器按会话身份键持有）。

    Attributes:
        recent_arrival_ms: 最近入站时刻列表（时间序——密度 / 连发窗口
            计算原料；长度受 :data:`FLOW_ARRIVAL_HISTORY_LIMIT` 约束）。
        backlog_count: 上次成功回复以来的未读条数（回复成功后清零）。
        buffered_texts: 待回复缓冲文本（延迟 / 合并路径累积；条数受
            :data:`FLOW_BUFFERED_TEXT_LIMIT` 约束）。
        pending: 是否已有排程中的延迟任务（True 时新入站只入缓冲，不
            重复排程）。
        pending_due_ms: 已排程任务的计划执行时刻（epoch 毫秒；None = 无
            排程——宽限判定原料）。
    """

    recent_arrival_ms: list[int] = Field(
        default_factory=list, description="最近入站时刻列表（时间序）"
    )
    backlog_count: int = Field(default=0, ge=0, description="未读积压条数")
    buffered_texts: list[str] = Field(default_factory=list, description="待回复缓冲文本")
    pending: bool = Field(default=False, description="是否存在排程中的延迟任务")
    pending_due_ms: int | None = Field(
        default=None, description="排程任务计划执行时刻（epoch 毫秒；None = 无排程）"
    )


class FlowGateInputModel(BaseModel):
    """门控判定输入（纯函数消费面——全部字段由调用方注入）。

    Attributes:
        now_ms: 判定时刻（epoch 毫秒）。
        is_synthetic: 入站是否为机器生成事件（``InboundMessageModel.synthetic``）。
        rhythm: 节律层现状快照。
        relationship: 关系进展摘要（None = 读取面未接线——不参与判定）。
        state: 会话心流节拍状态（入站计数与积压）。
    """

    now_ms: int = Field(ge=0, description="判定时刻（epoch 毫秒）")
    is_synthetic: bool = Field(default=False, description="入站是否为机器生成事件")
    rhythm: FlowRhythmStateModel = Field(description="节律层现状快照")
    relationship: RelationshipSummaryModel | None = Field(
        default=None, description="关系进展摘要（None = 不参与判定）"
    )
    state: FlowChatStateModel = Field(description="会话心流节拍状态")


class FlowGateDecisionModel(BaseModel):
    """门控裁决（结构化输出——链路消费与日志观测面）。

    Attributes:
        verdict: 裁决（立即回复 / 延迟 / 合并 / 不回复）。
        delay_ms: 延迟毫秒（REPLY / SKIP 恒 0；DEFER / MERGE 为排程延迟）。
        reason: 理由枚举（判定矩阵的稳定标识）。
    """

    verdict: FlowVerdict = Field(description="门控裁决")
    delay_ms: int = Field(ge=0, description="延迟毫秒（REPLY / SKIP 恒 0）")
    reason: FlowReason = Field(description="判定理由枚举")


class MergedBurstModel(BaseModel):
    """连发合并结果（缓冲裁剪后的待回复文本）。

    Attributes:
        text: 合并后的回复输入（按入站顺序以换行连接）。
        used_count: 实际纳入的消息条数。
        dropped_count: 超出上限被丢弃的最老消息条数。
    """

    text: str = Field(description="合并后的回复输入文本")
    used_count: int = Field(ge=0, description="实际纳入的消息条数")
    dropped_count: int = Field(ge=0, description="被丢弃的最老消息条数")
