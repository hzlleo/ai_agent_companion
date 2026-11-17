"""channels 域 Model Classes——通道中立消息 / 上下文 / 投递结果（Unit 文件）。

通道分层（链路顺序文档化于 docs/backend/channels.md）::

    平台 runner（telegram/tg_runner）        平台 wire → 通道中立模型
        ↓ InboundMessageModel
    ChannelRouter（channel_service）         人格绑定 + 每聊天会话 + ChatService.reply
        ↓ OutboundMessageModel
    平台适配器发送面（runner.send_message）  通道中立 → 平台 wire

模型约定：

    - **通道中立**：入站 / 出站消息不携带任何平台专有字段（Telegram 的
      chat 类型 / username 等留在 ``telegram/tg_models`` wire 层）；
    - **ID 一律字符串**：各平台 ID 值域不一（Telegram int、B站 / QQ 复合
      形态），通道层统一 ``str`` 承载（平台适配器负责双向归一）；
    - **时间一律 epoch 毫秒**（``core.time_utils.now_ms``——Telegram wire
      层的 epoch 秒在 runner 映射时 ×1000 归一）；
    - ``ChannelType`` 为 StrEnum 值域（初始为 telegram；多通道扩展在此
      追加成员——QQ 已加入，B站等后续通道同点追加）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from packages.harness.core.time_utils import now_ms

__all__ = [
    "ChannelType",
    "ChannelContextModel",
    "InboundMessageModel",
    "OutboundMessageModel",
    "ChannelDeliveryResultModel",
]


class ChannelType(StrEnum):
    """通道类型值域（telegram / qq；后续通道同点追加）。

    继承 ``StrEnum`` 保证与字符串比较兼容——``ChannelType.TELEGRAM ==
    "telegram"`` 为 ``True``（与 core ``ErrorCode`` 同约定）。
    """

    TELEGRAM = "telegram"
    QQ = "qq"


class ChannelContextModel(BaseModel):
    """通道会话上下文（通道 × 会话二元组）。

    路由器的会话身份键原料（``channel:chat_id``——每聊天一个 ChatService
    会话，见 :class:`packages.harness.channels.channel_service.ChannelRouter`）。

    Attributes:
        channel: 通道类型。
        chat_id: 平台会话 ID（字符串形态）。
    """

    channel: ChannelType = Field(description="通道类型")
    chat_id: str = Field(min_length=1, description="平台会话 ID（字符串形态）")


class InboundMessageModel(BaseModel):
    """入站消息（通道中立形态——平台 runner 产出 / 路由器消费）。

    Attributes:
        channel: 来源通道类型。
        chat_id: 平台会话 ID（字符串形态——路由器按 channel:chat_id
            维度复用会话）。
        message_id: 平台消息 ID（字符串形态——出站回复的引用目标）。
        sender_id: 发送者 ID（字符串形态；平台匿名形态归一 "0"）。
        text: 消息文本（非空——非文本消息由 runner 过滤，不进路由器）。
        ts_ms: 消息时间（epoch 毫秒；平台 wire 的秒级时间戳在映射时归一）。
    """

    channel: ChannelType = Field(description="来源通道类型")
    chat_id: str = Field(min_length=1, description="平台会话 ID（字符串形态）")
    message_id: str = Field(min_length=1, description="平台消息 ID（字符串形态）")
    sender_id: str = Field(min_length=1, description="发送者 ID（字符串形态）")
    text: str = Field(min_length=1, description="消息文本（非空）")
    ts_ms: int = Field(default_factory=now_ms, description="消息时间（epoch 毫秒）")


class OutboundMessageModel(BaseModel):
    """出站消息（路由器产出 / 通道适配器发送面消费）。

    Attributes:
        chat_id: 目标平台会话 ID（字符串形态）。
        text: 回复文本（非空——超长文本由平台客户端按上限分段发送）。
        reply_to_message_id: 引用回复的目标消息 ID（None = 不引用——
            兜底消息不引用，避免级联噪声）。
    """

    chat_id: str = Field(min_length=1, description="目标平台会话 ID（字符串形态）")
    text: str = Field(min_length=1, description="回复文本（非空）")
    reply_to_message_id: str | None = Field(
        default=None, description="引用回复的目标消息 ID（None = 不引用）"
    )


class ChannelDeliveryResultModel(BaseModel):
    """投递结果（通道适配器发送面返回——路由器 / 观测面消费）。

    Attributes:
        channel: 投递通道类型。
        chat_id: 目标平台会话 ID。
        message_ids: 已送达的平台消息 ID 列表（字符串形态；超长文本分段
            发送时多条——空列表 = 未送达任何消息）。
    """

    channel: ChannelType = Field(description="投递通道类型")
    chat_id: str = Field(min_length=1, description="目标平台会话 ID（字符串形态）")
    message_ids: list[str] = Field(default_factory=list, description="已送达消息 ID 列表")
