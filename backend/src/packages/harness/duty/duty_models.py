"""harness/duty 域 Model Classes——值班轮的传输契约（Unit 文件）。

契约分层：

    - :class:`DutyStatus`           —— 值班轮终态值域（success / failed /
      error——fail-closed 语义见 duty_service 模块文档）；
    - :class:`DutyOwnerDelivery`    —— 属主投递终态值域（sent / failed /
      skipped_* / disabled——报告路由观测轴）；
    - :class:`DutyRunConfigModel`   —— 单轮运行参数（工具步数上限 +
      任务书覆写——Settings.duty 装配映射面）；
    - :class:`DutyStepModel`        —— 单个工具调用 / 结果条目（steps
      轴的元素——调用声明 + registry 归一结果）；
    - :class:`DutyReportModel`      —— 值班轮报告（调度器 / 日志消费面；
      情景层落库映射为 memory 域 DutyReportPayloadModel）。

时间语义：全部 epoch 毫秒。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from packages.harness.tools.tool_models import ToolCallRequestModel, ToolResultModel

__all__ = [
    "DutyStatus",
    "DutyOwnerDelivery",
    "DutyRunConfigModel",
    "DutyStepModel",
    "DutyReportModel",
]


class DutyStatus(StrEnum):
    """值班轮终态值域。

    - ``success``：最终报告产出（工具循环自然收束或步数上限强制收尾）；
    - ``failed``：轮次完成但无可用最终报告（模型返回空内容——业务失败，
      情景层仍落 failed 事件）；
    - ``error``：轮次异常中止（网关 / 工具风暴等异常——fail-closed：
      情景层落 error 事件 + 报告中止文案，**恒不上抛调用方**）。
    """

    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"


class DutyOwnerDelivery(StrEnum):
    """属主投递终态值域（报告路由观测轴——duty 轮返回面携带）。

    - ``sent``：经 Telegram 适配器送达属主会话；
    - ``failed``：适配器投递失败（best-effort——仅日志，不影响轮次状态）；
    - ``skipped_no_channel``：通道未装配（无路由器 / 无 Telegram 适配器）
      ——报告仅落记忆；
    - ``skipped_no_owner``：属主会话 ID 未配置——报告仅落记忆；
    - ``disabled``：report_to_owner 关闭——报告仅落记忆。
    """

    SENT = "sent"
    FAILED = "failed"
    SKIPPED_NO_CHANNEL = "skipped_no_channel"
    SKIPPED_NO_OWNER = "skipped_no_owner"
    DISABLED = "disabled"


class DutyRunConfigModel(BaseModel):
    """值班轮运行参数（Settings.duty 的运行时映射——装配 / 测试注入面）。

    Attributes:
        max_tool_steps: 工具调用轮数上限（超出 → 强制最终报告指令，
            不再携带工具面；1-64）。
        charter_text: 值班任务书覆写（None = 使用 duty_prompts 正典模板；
            非空整体替换——装配面经 Settings.duty.charter_text 注入）。
    """

    max_tool_steps: int = Field(
        default=8, ge=1, le=64, description="工具调用轮数上限（超出强制收尾）"
    )
    charter_text: str | None = Field(default=None, description="值班任务书覆写（None = 正典模板）")


class DutyStepModel(BaseModel):
    """单个工具调用 / 结果条目（报告 steps 轴元素）。

    Attributes:
        call: 调用声明（工具名 + 参数——LLM 响应归一形态）。
        result: 执行结果（registry 归一终态——ok / result / error / 时延）。
    """

    call: ToolCallRequestModel = Field(description="调用声明（工具名 + 参数）")
    result: ToolResultModel = Field(description="执行结果（registry 归一终态）")


class DutyReportModel(BaseModel):
    """值班轮报告（轮次终态——调度器 / 日志消费面）。

    情景层映射：落库为 ``kind=duty`` 事件（载荷
    ``memory.models.DutyReportPayloadModel``——title / summary=最终报告 /
    details=本模型结构化投影；见 duty_service）。

    Attributes:
        persona: 人格作用域 ID（字符串形态——上下文轴）。
        persona_name: 人格展示名（报告标题 / 声线变量）。
        started_ts_ms: 轮次发起时间（epoch 毫秒）。
        finished_ts_ms: 轮次收束时间（epoch 毫秒）。
        steps: 工具调用 / 结果条目列表（时间序）。
        final_report: 最终报告文本（error 路径为中止文案）。
        status: 轮次终态。
        error: 失败 / 异常原因（success 为 None）。
        owner_delivery: 属主投递终态（观测轴——见 DutyOwnerDelivery）。
    """

    persona: str = Field(description="人格作用域 ID（字符串形态）")
    persona_name: str = Field(default="", description="人格展示名")
    started_ts_ms: int = Field(description="轮次发起时间（epoch 毫秒）")
    finished_ts_ms: int = Field(description="轮次收束时间（epoch 毫秒）")
    steps: list[DutyStepModel] = Field(default_factory=list, description="工具调用条目")
    final_report: str = Field(default="", description="最终报告文本")
    status: DutyStatus = Field(description="轮次终态")
    error: str | None = Field(default=None, description="失败 / 异常原因")
    owner_delivery: DutyOwnerDelivery = Field(
        default=DutyOwnerDelivery.DISABLED, description="属主投递终态"
    )
