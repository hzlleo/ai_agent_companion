"""reminders 域 Model Classes——提醒的枚举 / DTO / 投递契约（Unit 文件）。

提醒（持久化提醒——锁定设计）：主人预约的定时提醒落库
（``ff_reminders``），由中央调度器的扫描作业在到期后经通道适配器投递到
属主会话。与「对话中随口答应」的区别是本域**跨重启存活**：状态在库、
投递尝试次数在库、失败原因在库。

值域决策（锁定）：

    - ``status``：pending / delivered / cancelled / failed——单向推进
      （pending → 其余终态），无回退路径；
    - ``source``：manual（管理面创建）/ chat（对话意图捕获）——仅观测面，
      不改变投递语义；
    - ``title`` 1-200 字符（投递文案 ```` 【提醒】{title} ```` 的正文）；
      ``due_at_ms`` 允许过去时刻（补投语义：创建即到期，下次扫描投递）。

投递语义（锁定，与 duty 属主投递同轴）：

    - 无可用通道（未装配路由器 / 未注册 Telegram 适配器 / 属主会话未
      配置）时提醒**保持 pending**，下一次扫描重试（不消耗失败次数）；
    - 通道发送异常才计入 ``delivery_attempts`` 并在达到上限后转 failed
      （阈值见 ``reminders.service.DELIVERY_FAILURE_LIMIT``）。

依赖方向：pydantic / 标准库（无 SQLAlchemy——DTO 层不触 ORM）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.harness.channels.channel_models import OutboundMessageModel

__all__ = [
    "ReminderStatus",
    "ReminderSource",
    "ReminderCreateModel",
    "ReminderRecordDTO",
    "ReminderQueryDTO",
    "ReminderScanResultModel",
    "ReminderBatchCancelRequest",
    "ReminderBatchCancelItemDTO",
    "ReminderBatchCancelResponseDTO",
    "OutboundSenderFace",
]


class ReminderStatus(StrEnum):
    """提醒状态值域（``ff_reminders.status``——单向推进）。"""

    PENDING = "pending"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReminderSource(StrEnum):
    """提醒来源值域（``ff_reminders.source``——观测面）。"""

    MANUAL = "manual"
    CHAT = "chat"


class OutboundSenderFace(Protocol):
    """出站发送面协议（通道适配器的结构化消费契约）。

    结构化类型：Telegram 适配器（``ChannelAdapterProtocol``）天然满足
    （同签名 ``send_message``）；测试注入 fake 同形态——提醒作业因此
    零真实网络可测。
    """

    async def send_message(self, message: OutboundMessageModel) -> object:
        """发送单条出站消息（参数由提醒作业构造）。

        Args:
            message: 出站消息（``OutboundMessageModel`` 实例）。

        Returns:
            通道投递结果（调用方仅消费异常与否）。
        """
        ...


class ReminderCreateModel(BaseModel):
    """提醒创建入参（管理面 API / 对话捕获共用形态）。

    Attributes:
        title: 提醒标题（1-200 字符——首尾空白由服务层裁剪）。
        due_at_ms: 到期时刻（epoch 毫秒；允许过去时刻——补投语义）。
        source: 来源（manual / chat；缺省 manual）。
        meta: 来源附加信息（可空 JSON——不参与业务判定）。
    """

    title: str = Field(min_length=1, max_length=200, description="提醒标题（1-200 字符）")
    due_at_ms: int = Field(ge=0, description="到期时刻（epoch 毫秒）")
    source: ReminderSource = Field(default=ReminderSource.MANUAL, description="来源")
    meta: dict[str, object] | None = Field(default=None, description="来源附加信息")


class ReminderRecordDTO(BaseModel):
    """提醒记录（ORM 行归一——读取 / 投递观测面）。

    Attributes:
        id: 提醒主键。
        persona_id: 所属人格 ID。
        title: 提醒标题。
        due_at_ms: 到期时刻（epoch 毫秒）。
        status: 状态。
        delivery_attempts: 投递失败累计次数。
        last_error: 最近一次投递失败原因（NULL = 无失败）。
        delivered_at_ms: 投递成功时刻（NULL = 未投递）。
        source: 来源。
        meta: 来源附加信息。
        created_at: 落库时间（epoch 毫秒）。
        updated_at: 行更新时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="提醒主键")
    persona_id: str = Field(description="所属人格 ID")
    title: str = Field(description="提醒标题")
    due_at_ms: int = Field(description="到期时刻（epoch 毫秒）")
    status: ReminderStatus = Field(description="状态")
    delivery_attempts: int = Field(description="投递失败累计次数")
    last_error: str | None = Field(default=None, description="最近一次投递失败原因")
    delivered_at_ms: int | None = Field(default=None, description="投递成功时刻（epoch 毫秒）")
    source: ReminderSource = Field(description="来源")
    meta: dict[str, object] | None = Field(default=None, description="来源附加信息")
    created_at: int = Field(description="落库时间（epoch 毫秒）")
    updated_at: int = Field(description="行更新时间（epoch 毫秒）")


class ReminderQueryDTO(BaseModel):
    """提醒列表查询（status 过滤 + 游标分页——id 降序，新提醒在前）。

    Attributes:
        status: 状态过滤（None = 全部）。
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1-200，默认 20）。
    """

    status: ReminderStatus | None = Field(default=None, description="状态过滤")
    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class ReminderScanResultModel(BaseModel):
    """扫描结果（到期待投递提醒快照——调用方投递后回写终态）。

    Attributes:
        scanned: 本轮选中的到期提醒数量（= len(due)）。
        due: 到期待投递提醒列表（id 升序——投递顺序确定）。
    """

    scanned: int = Field(description="本轮选中数量")
    due: list[ReminderRecordDTO] = Field(default_factory=list, description="到期待投递提醒")


class ReminderBatchCancelRequest(BaseModel):
    """批量取消请求（部分成功形态——逐 id 独立判定）。

    Attributes:
        ids: 待取消提醒主键列表（1-100 个且不得重复）。
    """

    ids: list[int] = Field(
        min_length=1, max_length=100, description="待取消提醒主键列表（1-100 个，不得重复）"
    )

    @model_validator(mode="after")
    def _verify_ids_unique(self) -> ReminderBatchCancelRequest:
        """ids 唯一性校验（重复项拒绝——防「先成功后 CONFLICT」畸形结果）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ids 存在重复项。
        """
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("待取消提醒主键列表存在重复项")
        return self


class ReminderBatchCancelItemDTO(BaseModel):
    """批量取消结果单条目（部分成功形态）。

    Attributes:
        id: 提醒主键。
        success: 是否取消成功。
        code: 失败错误码（成功为 None）。
        message: 面向用户的提示信息。
    """

    id: int = Field(description="提醒主键")
    success: bool = Field(description="是否取消成功")
    code: str | None = Field(default=None, description="失败错误码（成功为 None）")
    message: str = Field(default="", description="提示信息")


class ReminderBatchCancelResponseDTO(BaseModel):
    """批量取消响应（部分成功形态——逐 id 结果与提交顺序一致）。

    Attributes:
        total: 提交总数。
        succeeded: 成功数量。
        failed: 失败数量。
        items: 逐 id 结果列表。
    """

    total: int = Field(description="提交总数")
    succeeded: int = Field(description="成功数量")
    failed: int = Field(description="失败数量")
    items: list[ReminderBatchCancelItemDTO] = Field(description="逐 id 结果列表")
