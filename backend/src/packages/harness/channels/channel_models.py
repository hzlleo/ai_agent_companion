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

协议 v2（附件 / 静默 / 合成事件——additive 扩展，v1 形态缺省保持）：

    - **附件**：``ChannelMessageAttachment`` 承载媒体附件（image / audio /
      file / sticker 四类）——出站与入站消息均携带（``attachments`` 列表，
      缺省空 = v1 纯文本形态）；附件**从不持有原始字节**（``base64`` 字段
      承载编码载荷，容量指引 ≤10MB——见
      :data:`ATTACHMENT_BASE64_SIZE_GUIDE_BYTES`）；
    - **静默**：出站 ``silent=True`` 请求平台免通知投递（Telegram
      disable_notification；QQ 无通知概念——适配器优雅忽略）；入站
      ``silent`` 为平台上报的无通知上下文标记位（预留）；
    - **合成事件**：入站 ``synthetic=True`` 标记机器生成事件面（heartflow /
      mood 事件上浮——R2e 消费面），非人类消息；
    - **逐附件投递结果**：``ChannelDeliveryResultModel.attachment_results``
      承载每附件的独立送达结局（部分成功可见——适配器聚合不抛）；文本与
      附件按「先文本后附件」顺序逐平台消息投递；
    - **流式降级（锁定契约）**：v2 附件**不参与流式**——流式回复恒为纯
      文本（网关工具面已强制文本流；通道面从未流式附件，v2 将其显式化为
      契约：附件到达适配器后整投递，由适配器聚合结果）。

消费者边界（诚实边界——文档化于 docs/backend/channels.md「协议 v2」）：
v2 只交付 wire（模型 + 适配器 + 测试）；入站附件由 ``InboundMessageModel``
承载供**未来消费者**（R2d 贴纸 / 语音、R2e 合成事件）消费——当前
ChatService 回复循环仍为纯文本（回合内容不落附件，零 schema 变更），
duty 属主投递亦维持纯文本。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, Field, model_validator

from packages.harness.core.time_utils import now_ms

__all__ = [
    "ATTACHMENT_BASE64_SIZE_GUIDE_BYTES",
    "ChannelType",
    "ChannelContextModel",
    "ChannelAttachmentKind",
    "ChannelMessageAttachment",
    "InboundMessageModel",
    "OutboundMessageModel",
    "AttachmentDeliveryResult",
    "ChannelDeliveryResultModel",
]

#: 附件 base64 载荷容量指引（字节——解码后原始媒体上限指引 10MB）。
#: 非硬校验（平台各有硬上限且由平台侧强制）；超出指引的载荷应改用
#: ``url`` 源（平台侧拉取）或分片策略——通道层只做文档化指引。
ATTACHMENT_BASE64_SIZE_GUIDE_BYTES: Final[int] = 10 * 1024 * 1024


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


class ChannelAttachmentKind(StrEnum):
    """附件类型值域（image / audio / file / sticker）。

    继承 ``StrEnum`` 保证与字符串比较兼容（与 ``ChannelType`` 同约定）。
    平台适配器按 kind 映射平台专有发送面（Telegram sendPhoto /
    sendVoice / sendAudio / sendDocument / sendSticker；QQ image / record
    消息段），映射表文档化于 docs/backend/channels.md「协议 v2」。
    """

    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    STICKER = "sticker"


class ChannelMessageAttachment(BaseModel):
    """通道中立消息附件（v2——入站 / 出站共用形态）。

    从不持有原始字节：媒体内容经三种**互斥源**之一承载（``url`` /
    ``base64`` / ``path`` 恰其一——``model_validator`` 强制；空串等同
    未提供）。``base64`` 载荷容量指引 ≤10MB（
    :data:`ATTACHMENT_BASE64_SIZE_GUIDE_BYTES`——非硬校验，见其 docstring）。

    平台引用约定（``path`` 伪方案——入站方向）：Telegram 入站附件以
    ``path="tg-file://{file_id}"`` 伪方案承载平台文件引用（真实媒体
    拉取需 Bot API getFile + 文件 URL 二段流程，deferred 到 R2d 贴纸 /
    语音工作；v2 只交付引用 wire）；出站方向 ``path`` 仅接受同伪方案
    形态（file_id 重发——适配器剥离方案前缀后作为 file_id 透传平台）。

    Attributes:
        kind: 附件类型（image / audio / file / sticker）。
        media_type: MIME 类型（如 ``image/jpeg`` / ``audio/ogg``；None =
            平台未提供线索——Telegram 入站 photo 无 MIME 线索、QQ 段
            不携带 MIME）。
        url: 媒体 URL 源（平台可拉取的 http(s) 地址）。
        base64: base64 编码载荷源（适配器解码后按 multipart / 平台
            编码形态上传——容量指引见模块 docstring）。
        path: 平台引用 / 本地路径源（入站：``tg-file://`` 伪方案 /
            NapCat ``file://``；出站：仅 ``tg-file://`` file_id 重发）。
        caption: 附件说明文本（平台支持时随附件投递——Telegram
            sendPhoto / sendVoice / sendAudio / sendDocument 的 caption
            参数；sendSticker 无 caption 参数、QQ 段无 caption 概念，
            均优雅丢弃并文档化）。
    """

    kind: ChannelAttachmentKind = Field(description="附件类型")
    media_type: str | None = Field(default=None, description="MIME 类型（None = 无线索）")
    url: str | None = Field(default=None, description="媒体 URL 源")
    base64: str | None = Field(default=None, description="base64 编码载荷源")
    path: str | None = Field(default=None, description="平台引用 / 本地路径源")
    caption: str | None = Field(default=None, description="附件说明文本")

    @model_validator(mode="after")
    def _require_single_source(self) -> Self:
        """校验媒体源互斥唯一（url / base64 / path 恰其一）。

        Returns:
            校验通过的自身（链式返回）。

        Raises:
            ValueError: 零源（三者皆缺）或多源（多于一个非空）——附件
                必须恰好声明一个媒体源（空串等同未提供）。
        """
        sources = [
            name
            for name, value in (("url", self.url), ("base64", self.base64), ("path", self.path))
            if value
        ]
        if len(sources) != 1:
            raise ValueError(
                f"附件必须恰好声明一个媒体源（url / base64 / path）：当前 {len(sources)} 个（{', '.join(sources) or '无'}）"
            )
        return self


class InboundMessageModel(BaseModel):
    """入站消息（通道中立形态——平台 runner 产出 / 路由器消费）。

    Attributes:
        channel: 来源通道类型。
        chat_id: 平台会话 ID（字符串形态——路由器按 channel:chat_id
            维度复用会话）。
        message_id: 平台消息 ID（字符串形态——出站回复的引用目标）。
        sender_id: 发送者 ID（字符串形态；平台匿名形态归一 "0"）。
        text: 消息文本（非空——v2 语义：Telegram 媒体消息以 caption 兜底
            为文本；纯媒体无 caption 由 runner 过滤，不进路由器）。
        ts_ms: 消息时间（epoch 毫秒；平台 wire 的秒级时间戳在映射时归一）。
        attachments: 消息附件（v2——缺省空列表 = v1 纯文本形态；由平台
            runner / webhook 提取，供未来消费者（R2d 贴纸 / 语音）消费，
            当前回复循环纯文本）。
        synthetic: 是否机器生成事件（v2——True = heartflow / mood 等
            事件上浮面而非人类消息；缺省 False = 人类消息）。
        silent: 是否无通知上下文（v2——平台上报「该消息在免通知语境
            送达」的标记位；缺省 False）。
    """

    channel: ChannelType = Field(description="来源通道类型")
    chat_id: str = Field(min_length=1, description="平台会话 ID（字符串形态）")
    message_id: str = Field(min_length=1, description="平台消息 ID（字符串形态）")
    sender_id: str = Field(min_length=1, description="发送者 ID（字符串形态）")
    text: str = Field(min_length=1, description="消息文本（非空）")
    ts_ms: int = Field(default_factory=now_ms, description="消息时间（epoch 毫秒）")
    attachments: list[ChannelMessageAttachment] = Field(
        default_factory=list, description="消息附件（缺省空 = v1 纯文本形态）"
    )
    synthetic: bool = Field(
        default=False, description="是否机器生成事件（True = 事件上浮面非人类消息）"
    )
    silent: bool = Field(default=False, description="是否无通知上下文（平台上报标记位）")


class OutboundMessageModel(BaseModel):
    """出站消息（路由器产出 / 通道适配器发送面消费）。

    Attributes:
        chat_id: 目标平台会话 ID（字符串形态）。
        text: 回复文本（非空——超长文本由平台客户端按上限分段发送）。
        reply_to_message_id: 引用回复的目标消息 ID（None = 不引用——
            兜底消息不引用，避免级联噪声；v2 语义：引用只挂文本消息，
            附件投递不携带引用）。
        attachments: 出站附件（v2——缺省空列表 = v1 纯文本形态；适配器
            按「先文本后附件」顺序投递并聚合逐附件结果；**不参与流式**
            ——流式回复恒为纯文本，见模块 docstring「协议 v2」）。
        silent: 请求免通知投递（v2——True 时平台支持面投递为免通知
            消息：Telegram disable_notification；QQ 无通知概念，适配器
            优雅忽略；缺省 False）。
    """

    chat_id: str = Field(min_length=1, description="目标平台会话 ID（字符串形态）")
    text: str = Field(min_length=1, description="回复文本（非空）")
    reply_to_message_id: str | None = Field(
        default=None, description="引用回复的目标消息 ID（None = 不引用）"
    )
    attachments: list[ChannelMessageAttachment] = Field(
        default_factory=list, description="出站附件（缺省空 = v1 纯文本形态）"
    )
    silent: bool = Field(default=False, description="请求免通知投递（平台不支持时优雅忽略）")


class AttachmentDeliveryResult(BaseModel):
    """单附件投递结果（v2——适配器聚合面）。

    部分成功语义：单附件失败**不抛异常**（``ok=False`` + ``error`` 承载
    失败原因；文本与其余附件照常投递）——适配器按附件序聚合全部结局；
    整条消息的文本段失败仍按 v1 语义上抛（路由器兜底隔离）。

    Attributes:
        index: 附件序（对应 ``OutboundMessageModel.attachments`` 下标——
            0 起）。
        ok: 该附件是否送达。
        error: 失败原因（ok=False 时承载；ok=True 为 None）。
        provider_message_id: 承载该附件的平台消息 ID（字符串形态；None =
            送达成功但平台未回传凭据，或该附件未投递）。
    """

    index: int = Field(ge=0, description="附件序（attachments 下标，0 起）")
    ok: bool = Field(description="该附件是否送达")
    error: str | None = Field(default=None, description="失败原因（ok=False 时承载）")
    provider_message_id: str | None = Field(
        default=None, description="承载该附件的平台消息 ID（None = 无凭据 / 未投递）"
    )


class ChannelDeliveryResultModel(BaseModel):
    """投递结果（通道适配器发送面返回——路由器 / 观测面消费）。

    Attributes:
        channel: 投递通道类型。
        chat_id: 目标平台会话 ID。
        message_ids: 已送达的平台消息 ID 列表（字符串形态；v2 语义：文本
            分段 + 送达成功附件的平台消息一并计入；空列表 = 未送达任何
            消息）。
        attachment_results: 逐附件投递结果（v2——缺省空列表 = v1 纯文本
            形态；按附件序对齐 ``OutboundMessageModel.attachments``）。
    """

    channel: ChannelType = Field(description="投递通道类型")
    chat_id: str = Field(min_length=1, description="目标平台会话 ID（字符串形态）")
    message_ids: list[str] = Field(
        default_factory=list,
        description="已送达消息 ID 列表（文本分段 + 成功附件的平台消息）",
    )
    attachment_results: list[AttachmentDeliveryResult] = Field(
        default_factory=list, description="逐附件投递结果（缺省空 = v1 纯文本形态）"
    )
