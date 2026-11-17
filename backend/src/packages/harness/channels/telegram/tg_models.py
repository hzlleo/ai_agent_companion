"""telegram 通道 wire Model Classes——Bot API 子集（Unit 文件）。

Telegram Bot API 响应对象的**严格类型化子集**（本工程消费面：
getUpdates 长轮询 / sendMessage / getMe 启动校验 / v2 媒体入站与发送）。
锁定设计决策：不引入 aiogram / python-telegram-bot——手写客户端（见
``tg_client``），wire 模型只声明消费到的字段（``extra="ignore"`` 容忍
平台加字段）。

wire 归一约定：

    - ``from`` 是 Python 关键字——wire 字段 ``from`` 经 alias 映射到
      ``from_user``（``populate_by_name=True`` 支持两种入参形态）；
    - Telegram ``date`` 为 **epoch 秒**——runner 映射 InboundMessageModel
      时 ×1000 归一为工程统一的 epoch 毫秒；
    - ``TgResponseModel[T]`` 为泛型响应包裹（``ok`` / ``result`` /
      ``error_code`` / ``description``）——按端点参数化
      （``TgResponseModel[list[TgUpdateModel]]`` 等）。

v2 媒体子集（入站方向）：``message`` 的媒体字段（photo / voice / audio /
document / sticker）与 ``caption``——Bot API 语义：**文本与媒体互斥**
（一条 message 要么 ``text`` 要么媒体字段 + 可选 ``caption``）；runner
映射入站时 caption 兜底为文本（见 ``tg_runner``）。媒体对象只声明
``file_id`` 与消费到的线索字段（MIME / 宽高 / 文件名——真实媒体拉取
需 getFile + 文件 URL 二段流程，deferred 到 R2d）。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TgChatModel",
    "TgUserModel",
    "TgPhotoSizeModel",
    "TgVoiceModel",
    "TgAudioModel",
    "TgDocumentModel",
    "TgStickerModel",
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


class TgPhotoSizeModel(BaseModel):
    """Bot API ``photo`` 数组元素（图片尺寸形态——子集）。

    一条图片消息的 ``photo`` 为尺寸数组（缩略图 → 原图）；runner 提取
    附件时取**最大尺寸**（宽 × 高最大者——无尺寸时取末位兜底）。

    Attributes:
        file_id: 平台文件 ID（v2 入站附件以 ``tg-file://{file_id}``
            伪方案承载——见 channel_models）。
        width: 宽（像素——最大尺寸选取依据）。
        height: 高（像素——最大尺寸选取依据）。
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="平台文件 ID")
    width: int = Field(default=0, description="宽（像素）")
    height: int = Field(default=0, description="高（像素）")


class TgVoiceModel(BaseModel):
    """Bot API ``voice`` 对象（语音消息——子集）。

    Attributes:
        file_id: 平台文件 ID。
        mime_type: MIME 类型（如 ``audio/ogg``——附件 media_type 线索）。
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="平台文件 ID")
    mime_type: str | None = Field(default=None, description="MIME 类型")


class TgAudioModel(BaseModel):
    """Bot API ``audio`` 对象（音乐文件消息——子集）。

    Attributes:
        file_id: 平台文件 ID。
        mime_type: MIME 类型（如 ``audio/mpeg``——附件 media_type 线索）。
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="平台文件 ID")
    mime_type: str | None = Field(default=None, description="MIME 类型")


class TgDocumentModel(BaseModel):
    """Bot API ``document`` 对象（通用文件消息——子集）。

    Attributes:
        file_id: 平台文件 ID。
        file_name: 文件名（平台携带时——观测面）。
        mime_type: MIME 类型（附件 media_type 线索）。
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="平台文件 ID")
    file_name: str | None = Field(default=None, description="文件名")
    mime_type: str | None = Field(default=None, description="MIME 类型")


class TgStickerModel(BaseModel):
    """Bot API ``sticker`` 对象（贴纸消息——子集）。

    Attributes:
        file_id: 平台文件 ID（贴纸重发 / 引用凭据——R2d 贴纸工作消费面）。
    """

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="平台文件 ID")


class TgMessageModel(BaseModel):
    """Bot API ``message`` 对象（子集）。

    Attributes:
        message_id: 消息 ID（会话内唯一；数值）。
        chat: 所属会话。
        from_user: 发送者（``from`` 别名；频道帖子等形态可为 None）。
        text: 文本内容（**与媒体字段互斥**——纯文本消息形态；媒体消息
            为 None，v2 由 caption 兜底为文本）。
        caption: 媒体说明文本（图片 / 语音 / 文件等媒体消息携带；纯
            文本消息为 None——v2 runner 以其兜底为入站文本）。
        photo: 图片尺寸数组（图片消息形态；None = 非图片消息）。
        voice: 语音消息对象（None = 非语音消息）。
        audio: 音乐文件消息对象（None = 非音频文件消息）。
        document: 通用文件消息对象（None = 非文件消息）。
        sticker: 贴纸消息对象（None = 非贴纸消息）。
        date: 发送时间（**epoch 秒**——wire 原生形态）。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    message_id: int = Field(description="消息 ID")
    chat: TgChatModel = Field(description="所属会话")
    from_user: TgUserModel | None = Field(default=None, alias="from", description="发送者")
    text: str | None = Field(default=None, description="文本内容（媒体消息为 None）")
    caption: str | None = Field(default=None, description="媒体说明文本（纯文本消息为 None）")
    photo: list[TgPhotoSizeModel] | None = Field(
        default=None, description="图片尺寸数组（图片消息形态）"
    )
    voice: TgVoiceModel | None = Field(default=None, description="语音消息对象")
    audio: TgAudioModel | None = Field(default=None, description="音乐文件消息对象")
    document: TgDocumentModel | None = Field(default=None, description="通用文件消息对象")
    sticker: TgStickerModel | None = Field(default=None, description="贴纸消息对象")
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
