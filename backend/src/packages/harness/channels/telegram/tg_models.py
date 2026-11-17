"""telegram 通道 wire Model Classes——Bot API 子集（Unit 文件）。

Telegram Bot API 响应对象的**严格类型化子集**（本工程消费面：
getUpdates 长轮询 / sendMessage / getMe 启动校验）。锁定设计决策：
不引入 aiogram / python-telegram-bot——手写客户端（见 ``tg_client``），
wire 模型只声明消费到的字段（``extra="ignore"`` 容忍平台加字段）。

wire 归一约定：

    - ``from`` 是 Python 关键字——wire 字段 ``from`` 经 alias 映射到
      ``from_user``（``populate_by_name=True`` 支持两种入参形态）；
    - Telegram ``date`` 为 **epoch 秒**——runner 映射 InboundMessageModel
      时 ×1000 归一为工程统一的 epoch 毫秒；
    - ``TgResponseModel[T]`` 为泛型响应包裹（``ok`` / ``result`` /
      ``error_code`` / ``description``）——按端点参数化
      （``TgResponseModel[list[TgUpdateModel]]`` 等）。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TgChatModel",
    "TgUserModel",
    "TgMessageModel",
    "TgUpdateModel",
    "TgResponseModel",
    "TgSendMessageResult",
    "TgBotInfoModel",
]

_T = TypeVar("_T")


class TgChatModel(BaseModel):
    """Bot API ``chat`` 对象（子集）。

    Attributes:
        id: 会话 ID（数值——私聊 / 群 / 频道；通道层归一为字符串形态）。
        type: 会话类型（``private`` / ``group`` / ``supergroup`` /
            ``channel``）。
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="会话 ID")
    type: str = Field(default="", description="会话类型")


class TgUserModel(BaseModel):
    """Bot API ``from`` 用户对象（子集）。

    Attributes:
        id: 用户 ID（数值；通道层归一为字符串形态）。
        is_bot: 是否 bot 账号。
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="用户 ID")
    is_bot: bool = Field(default=False, description="是否 bot 账号")


class TgMessageModel(BaseModel):
    """Bot API ``message`` 对象（子集）。

    Attributes:
        message_id: 消息 ID（会话内唯一；数值）。
        chat: 所属会话。
        from_user: 发送者（``from`` 别名；频道帖子等形态可为 None）。
        text: 文本内容（非文本消息——图片 / 贴纸等——为 None；runner
            过滤不进路由器）。
        date: 发送时间（**epoch 秒**——wire 原生形态）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    message_id: int = Field(description="消息 ID")
    chat: TgChatModel = Field(description="所属会话")
    from_user: TgUserModel | None = Field(default=None, alias="from", description="发送者")
    text: str | None = Field(default=None, description="文本内容（非文本消息为 None）")
    date: int = Field(default=0, description="发送时间（epoch 秒）")


class TgUpdateModel(BaseModel):
    """Bot API ``update`` 对象（子集）。

    Attributes:
        update_id: 更新 ID（单调递增——offset 游标与去重的键）。
        message: 新消息（edited_message / callback_query 等其它更新形态
            为 None——runner 跳过，offset 仍照常推进确认）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    update_id: int = Field(description="更新 ID（单调递增）")
    message: TgMessageModel | None = Field(
        default=None, description="新消息（其它更新形态为 None）"
    )


class TgResponseModel(BaseModel, Generic[_T]):
    """Bot API 响应包裹（泛型——按端点参数化 result 类型）。

    Attributes:
        ok: 调用是否成功（False 时 description / error_code 携带原因）。
        result: 载荷（按端点参数化——getUpdates 为 update 列表、
            sendMessage 为 message 对象、getMe 为 bot 用户对象）。
        error_code: 平台错误码（ok=False 时；401/403 = token 无效）。
        description: 平台错误描述（ok=False 时）。
    """

    model_config = ConfigDict(extra="ignore")

    ok: bool = Field(description="调用是否成功")
    result: _T | None = Field(default=None, description="载荷（按端点参数化）")
    error_code: int | None = Field(default=None, description="平台错误码")
    description: str | None = Field(default=None, description="平台错误描述")


class TgSendMessageResult(BaseModel):
    """Bot API ``sendMessage`` 的 result 对象（子集）。

    Attributes:
        message_id: 已送达消息 ID（数值——投递结果归一为字符串形态）。
        chat: 目标会话。
    """

    model_config = ConfigDict(extra="ignore")

    message_id: int = Field(description="已送达消息 ID")
    chat: TgChatModel = Field(description="目标会话")


class TgBotInfoModel(BaseModel):
    """Bot API ``getMe`` 的 result 对象（子集——启动校验消费）。

    Attributes:
        id: bot 用户 ID。
        is_bot: 恒 True（bot 账号面）。
        username: bot 用户名（不含 @；可能缺失）。
        first_name: 显示名。
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="bot 用户 ID")
    is_bot: bool = Field(default=True, description="是否 bot 账号")
    username: str | None = Field(default=None, description="bot 用户名（不含 @）")
    first_name: str = Field(default="", description="显示名")
