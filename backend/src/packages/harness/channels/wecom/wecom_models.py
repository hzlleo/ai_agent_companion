"""wecom 通道 wire Model Classes——企业微信「智能机器人」长连接协议子集（Unit 文件）。

协议依据：企业微信开发者中心《智能机器人长连接》
（https://developer.work.weixin.qq.com/document/path/101463）与《接收消息》
（https://developer.work.weixin.qq.com/document/path/100719）。本文件只声明
本工程消费与产出的字段子集（``extra="ignore"`` 容忍平台加字段），不含任何
I/O——连接生命周期在 ``wecom_client``，路由与投递编排在 ``wecom_runner``。

wire 形态（锁定——单一命令包裹，字段名对齐官方文档）::

    出站（开发者 → 平台）        {"cmd": "aibot_respond_msg", "headers": {"req_id": ...}, "body": {...}}
    入站（平台 → 开发者）        {"cmd": "aibot_msg_callback", "headers": {"req_id": ...}, "body": {...}}
    应答（平台 → 开发者）        {"headers": {"req_id": ...}, "errcode": 0, "errmsg": "ok"[, "body": {...}]}

命令值域（官方文档锁定）：``aibot_subscribe``（订阅鉴权）、``ping``（心跳）、
``aibot_respond_msg``（回复消息回调）、``aibot_respond_welcome_msg``（回复进入
会话事件）、``aibot_send_msg``（主动推送）、``aibot_upload_media_init`` /
``aibot_upload_media_chunk`` / ``aibot_upload_media_finish``（临时素材分片
上传）；入站 ``aibot_msg_callback``（消息回调）与 ``aibot_event_callback``
（事件回调）。应答包裹统一 ``errcode == 0`` 为成功。

凭据语义：订阅只需 **BotID + Secret**（官方文档「获取凭证」一节）——长连接
模式**不使用** ``corp_id`` / ``agent_id`` / access_token 三件套（素材上传同在
长连接命令面完成，不走向 ``/cgi-bin/gettoken`` 的 HTTP 面），因此本工程
``WECOM_`` env 面的最小集不含这三项。

入站归一约定：

    - ``chattype=single`` → 会话键 ``single:<userid>``（一人一会话）；
      ``chattype=group`` → ``group:<chatid>``（一群一会话，发送者经
      ``sender_id`` 承载）——与 QQ 适配器的 chat_id grammar 同构；
    - 群聊 / 单聊判定输出到 ``InboundMessageModel.is_group_chat``（来源分级
      依据：群聊按 peer 落档）；
    - ``msgid`` 是官方文档明确的排重键（「本次回调的唯一性标志，用于事件
      排重」）——本层直接作为 ``message_id`` 与去重键原料；缺失 ``msgid`` 的
      回调不可排重，按不可路由处理；
    - 文本归一：``text.content`` 原样；``voice.content`` 为平台已转写的
      语音文本（可直接进 LLM）；``mixed``（图文混排）逐项拼接（文本项原文、
      图片项占位符）；纯媒体消息（image / file / video）以占位符兜底为文本
      ——``InboundMessageModel.text`` 要求非空，占位符让「用户只发了张图」仍是
      一个可复述的回合（与 QQ 段展平占位符同哲学）；
    - 媒体附件：``image`` / ``mixed`` 内图片 / ``file`` / ``video`` 的
      ``url`` + ``aeskey`` 输出为 ``ChannelMessageAttachment``（``url`` 源；
      video 归 ``file`` 类且 ``media_type="video/mp4"``）。平台对这三类资源
      返回的是**密文**（AES-256-CBC，``aeskey`` 派生 IV）——解密属消费者面，
      通道中立附件不新增平台专有字段：需要字节的消费者按 ``wecom_models``
      的 wire 模型取 ``aeskey`` 自行解密（文档化边界，本切片不下载不落盘）；
    - ``synthetic``：回调携带 ``compat_info`` 表示**非普通用户消息**（智能
      助理调用 / 频道来源）——映射为 ``synthetic=True``（机器来源面，路由
      器据此恒跳过回复，与 ``FLOW_ENABLED`` 无关）；普通用户消息恒 False；
    - ``silent``：长连接协议无免通知投递语义——入站恒 False、出站字段被适配
      器优雅忽略（与 QQ 同约定）。

出站支持面（官方文档「回复消息」「主动推送消息」「上传临时素材」）：

    - 文本 → ``aibot_respond_msg``（``msgtype=markdown``）：回复面列出的文本
      载体是 markdown（纯文本在 markdown 语义下原样呈现），单条 content 上限
      20480 字节 UTF-8——超限按换行边界确定分段（``split_wecom_markdown_text``），
      首段走回复、续段走主动推送；
    - 附件 → 先分片上传取 ``media_id``，再经 ``aibot_send_msg`` 按消息类型
      投递（image / voice / file；sticker 归 image——企微无独立贴纸消息类型）；
      上传会话 30 分钟有效、单片 ≤512KB 明文、≤100 片、临时素材 3 天有效；
    - 回复 vs 推送：``aibot_respond_msg`` 必须透传**触发该会话的消息回调
      req_id**（官方文档：24 小时内可回复该会话），``aibot_send_msg`` 以
      ``chatid`` + ``chat_type`` 寻址（要求用户先给机器人发过消息）。频道中立
      出站模型不携带 req_id，故由 runner 维护每会话最近一次消息回调的回复
      上下文（``WeComReplyContextModel``）；上下文过期或缺失时降级为主动推送。
      附件恒走主动推送：``aibot_respond_msg`` 与单个 req_id 强绑定，多条复用
      同一 req_id 的语义官方未文档化，主动推送面无此不确定性。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms

__all__ = [
    "WECOM_DEFAULT_WS_ENDPOINT",
    "WECOM_CMD_SUBSCRIBE",
    "WECOM_CMD_PING",
    "WECOM_CMD_MSG_CALLBACK",
    "WECOM_CMD_EVENT_CALLBACK",
    "WECOM_CMD_RESPOND_MSG",
    "WECOM_CMD_RESPOND_WELCOME_MSG",
    "WECOM_CMD_SEND_MSG",
    "WECOM_CMD_UPLOAD_MEDIA_INIT",
    "WECOM_CMD_UPLOAD_MEDIA_CHUNK",
    "WECOM_CMD_UPLOAD_MEDIA_FINISH",
    "WECOM_CALLBACK_COMMANDS",
    "WECOM_EVENT_DISCONNECTED",
    "WECOM_EVENT_ENTER_CHAT",
    "WECOM_SINGLE_PREFIX",
    "WECOM_GROUP_PREFIX",
    "WECOM_CHAT_TYPE_SINGLE",
    "WECOM_CHAT_TYPE_GROUP",
    "WECOM_UPLOAD_CHUNK_SIZE_BYTES",
    "WECOM_UPLOAD_MAX_CHUNKS",
    "WECOM_UPLOAD_MIN_SIZE_BYTES",
    "WECOM_MARKDOWN_MAX_BYTES",
    "WECOM_PLACEHOLDER_IMAGE",
    "WECOM_PLACEHOLDER_FILE",
    "WECOM_PLACEHOLDER_VIDEO",
    "WeComMediaKind",
    "WeComSenderModel",
    "WeComTextContentModel",
    "WeComMediaContentModel",
    "WeComVoiceContentModel",
    "WeComMixedItemModel",
    "WeComMixedContentModel",
    "WeComQuoteModel",
    "WeComEventContentModel",
    "WeComCompatInfoModel",
    "WeComCallbackBodyModel",
    "WeComCallbackFrameModel",
    "WeComHeadersModel",
    "WeComAckBodyModel",
    "WeComAckModel",
    "WeComChatTargetModel",
    "WeComReplyContextModel",
    "WeComSubscribeBodyModel",
    "WeComMarkdownContentModel",
    "WeComMarkdownBodyModel",
    "WeComMediaIdModel",
    "WeComMediaBodyModel",
    "WeComUploadInitBodyModel",
    "WeComUploadChunkBodyModel",
    "WeComUploadFinishBodyModel",
    "WeComCommandBody",
    "WeComCommandModel",
    "parse_wecom_chat_id",
    "map_wecom_callback_to_inbound",
    "extract_wecom_attachments",
    "flatten_wecom_callback_text",
    "wecom_media_kind_for_attachment",
    "wecom_upload_size_limit",
    "split_wecom_markdown_text",
    "build_subscribe_command",
    "build_ping_command",
    "build_markdown_reply_command",
    "build_markdown_push_command",
    "build_media_push_command",
    "build_upload_init_command",
    "build_upload_chunk_command",
    "build_upload_finish_command",
]

#: 长连接 WebSocket 端点（官方文档「WebSocket 连接地址」——可在 WECOM_WS_ENDPOINT
#: 覆写，测试 / 代理部署形态使用）。
WECOM_DEFAULT_WS_ENDPOINT: Final[str] = "wss://openws.work.weixin.qq.com"

#: 订阅鉴权命令（连接建立后第一条命令——BotID + Secret 校验）。
WECOM_CMD_SUBSCRIBE: Final[str] = "aibot_subscribe"
#: 应用层心跳命令（官方文档建议 30 秒间隔）。
WECOM_CMD_PING: Final[str] = "ping"
#: 入站消息回调命令。
WECOM_CMD_MSG_CALLBACK: Final[str] = "aibot_msg_callback"
#: 入站事件回调命令。
WECOM_CMD_EVENT_CALLBACK: Final[str] = "aibot_event_callback"
#: 回复消息回调命令（须透传回调 req_id）。
WECOM_CMD_RESPOND_MSG: Final[str] = "aibot_respond_msg"
#: 回复进入会话事件的欢迎语命令（须透传事件 req_id）。
WECOM_CMD_RESPOND_WELCOME_MSG: Final[str] = "aibot_respond_welcome_msg"
#: 主动推送消息命令（chatid + chat_type 寻址）。
WECOM_CMD_SEND_MSG: Final[str] = "aibot_send_msg"
#: 临时素材上传——初始化。
WECOM_CMD_UPLOAD_MEDIA_INIT: Final[str] = "aibot_upload_media_init"
#: 临时素材上传——分片。
WECOM_CMD_UPLOAD_MEDIA_CHUNK: Final[str] = "aibot_upload_media_chunk"
#: 临时素材上传——完成（返回 media_id）。
WECOM_CMD_UPLOAD_MEDIA_FINISH: Final[str] = "aibot_upload_media_finish"

#: 入站回调命令值域（客户端据 cmd 分流到回调消费面而非请求应答面）。
WECOM_CALLBACK_COMMANDS: frozenset[str] = frozenset(
    {WECOM_CMD_MSG_CALLBACK, WECOM_CMD_EVENT_CALLBACK}
)

#: 连接断开事件（新连接完成订阅会踢掉旧连接并下发该事件）。
WECOM_EVENT_DISCONNECTED: Final[str] = "disconnected_event"
#: 进入会话事件（用户当天首次进入单聊）。
WECOM_EVENT_ENTER_CHAT: Final[str] = "enter_chat"

#: 会话键单聊前缀（``single:<userid>``）。
WECOM_SINGLE_PREFIX: Final[str] = "single"
#: 会话键群聊前缀（``group:<chatid>``）。
WECOM_GROUP_PREFIX: Final[str] = "group"

#: 主动推送的会话类型值（官方文档：1 = 单聊 userid、2 = 群聊 chatid）。
WECOM_CHAT_TYPE_SINGLE: Final[int] = 1
#: 主动推送的群聊会话类型值。
WECOM_CHAT_TYPE_GROUP: Final[int] = 2

#: 临时素材单片明文上限（官方文档：单片 ≤512KB，Base64 编码前）。
WECOM_UPLOAD_CHUNK_SIZE_BYTES: Final[int] = 512 * 1024
#: 临时素材分片数上限（官方文档：≤100 片）。
WECOM_UPLOAD_MAX_CHUNKS: Final[int] = 100
#: 临时素材最小体积（官方文档 total_size 下限：5 字节）。
WECOM_UPLOAD_MIN_SIZE_BYTES: Final[int] = 5
#: markdown 消息 content 上限（官方文档：≤20480 字节 UTF-8）。
WECOM_MARKDOWN_MAX_BYTES: Final[int] = 20480

#: 纯媒体入站的文本占位符（``InboundMessageModel.text`` 非空契约的兜底形态）。
WECOM_PLACEHOLDER_IMAGE: Final[str] = "[图片]"
#: 文件入站的文本占位符。
WECOM_PLACEHOLDER_FILE: Final[str] = "[文件]"
#: 视频入站的文本占位符。
WECOM_PLACEHOLDER_VIDEO: Final[str] = "[视频]"

#: 附件 kind → 上传素材类型（官方文档素材 type 值域：file / image / voice / video）。
_UPLOAD_KIND_BY_ATTACHMENT: Final[dict[ChannelAttachmentKind, str]] = {
    ChannelAttachmentKind.IMAGE: "image",
    ChannelAttachmentKind.AUDIO: "voice",
    ChannelAttachmentKind.FILE: "file",
    ChannelAttachmentKind.STICKER: "image",
}

#: 上传素材类型 → 明文大小上限（官方文档：图片 10MB / 语音 2MB / 视频 10MB /
#: 普通文件 20MB）。
_UPLOAD_SIZE_LIMIT_BY_KIND: Final[dict[str, int]] = {
    "image": 10 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}


class WeComMediaKind(StrEnum):
    """上传素材类型值域（官方文档素材 type 字段：file / image / voice / video）。

    Attributes:
        FILE: 普通文件（≤20MB）。
        IMAGE: 图片（≤10MB；png / jpg / jpeg / gif）。
        VOICE: 语音（≤2MB；amr）。
        VIDEO: 视频（≤10MB；mp4）。
    """

    FILE = "file"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"


class WeComSenderModel(BaseModel):
    """回调触发者（wire 字段名 ``from``）。

    Attributes:
        userid: 触发者 userid——机器人创建者为超管时为明文，否则为企业主体下的
            加密 userid（官方文档）
    """

    model_config = ConfigDict(extra="ignore")

    userid: str = Field(default="", description="触发者 userid（明文或加密形态）")


class WeComTextContentModel(BaseModel):
    """文本内容结构体（官方文档「文本」）。

    Attributes:
        content: 文本消息内容。
    """

    model_config = ConfigDict(extra="ignore")

    content: str = Field(default="", description="文本内容")


class WeComMediaContentModel(BaseModel):
    """图片 / 文件 / 视频资源结构体（官方文档对应结构体）。

    Attributes:
        url: 资源下载地址（官方文档：5 分钟内有效）。
        aeskey: 资源解密密钥（每个链接唯一；AES-256-CBC，IV 取前 16 字节）。
    """

    model_config = ConfigDict(extra="ignore")

    url: str = Field(default="", description="资源下载地址（5 分钟有效）")
    aeskey: str | None = Field(default=None, description="资源解密密钥（每个链接唯一）")


class WeComVoiceContentModel(BaseModel):
    """语音内容结构体（官方文档「语音」——平台已完成语音转文本）。

    Attributes:
        content: 语音转写文本。
    """

    model_config = ConfigDict(extra="ignore")

    content: str = Field(default="", description="语音转写文本")


class WeComMixedItemModel(BaseModel):
    """图文混排子项（官方文档「图文混排」的 ``msg_item`` 元素）。

    Attributes:
        msgtype: 子项类型（text / image）。
        text: 文本子项内容。
        image: 图片子项内容。
    """

    model_config = ConfigDict(extra="ignore")

    msgtype: str = Field(default="", description="子项类型（text / image）")
    text: WeComTextContentModel | None = Field(default=None, description="文本子项")
    image: WeComMediaContentModel | None = Field(default=None, description="图片子项")


class WeComMixedContentModel(BaseModel):
    """图文混排内容结构体（官方文档「图文混排」）。

    Attributes:
        msg_item: 子项列表（文本与图片按用户拼接顺序）。
    """

    model_config = ConfigDict(extra="ignore")

    msg_item: list[WeComMixedItemModel] = Field(
        default_factory=list, description="图文混排子项列表"
    )


class WeComQuoteModel(BaseModel):
    """引用内容结构体（官方文档「引用」——用户引用其他消息时携带）。

    Attributes:
        msgtype: 被引用消息类型（text / image / mixed / voice / file / video）。
        text: 被引用文本内容。
        image: 被引用图片内容。
        mixed: 被引用图文混排内容。
        voice: 被引用语音内容。
        file: 被引用文件内容。
        video: 被引用视频内容。
    """

    model_config = ConfigDict(extra="ignore")

    msgtype: str = Field(default="", description="被引用消息类型")
    text: WeComTextContentModel | None = Field(default=None, description="被引用文本")
    image: WeComMediaContentModel | None = Field(default=None, description="被引用图片")
    mixed: WeComMixedContentModel | None = Field(default=None, description="被引用图文混排")
    voice: WeComVoiceContentModel | None = Field(default=None, description="被引用语音")
    file: WeComMediaContentModel | None = Field(default=None, description="被引用文件")
    video: WeComMediaContentModel | None = Field(default=None, description="被引用视频")


class WeComEventContentModel(BaseModel):
    """事件内容结构体（官方文档「事件推送格式」）。

    Attributes:
        eventtype: 事件类型（enter_chat / template_card_event / feedback_event /
            disconnected_event）。
    """

    model_config = ConfigDict(extra="ignore")

    eventtype: str = Field(default="", description="事件类型")


class WeComCompatInfoModel(BaseModel):
    """回调来源附加信息（存在即表示非普通用户消息）。

    Attributes:
        source: 来源（``ai_assistant`` = 智能助理调用 / ``channel`` = 频道）。
    """

    model_config = ConfigDict(extra="ignore")

    source: str = Field(default="", description="来源标识（ai_assistant / channel）")


class WeComCallbackBodyModel(BaseModel):
    """入站回调 body（消息回调与事件回调共用外壳字段）。

    Attributes:
        msgid: 回调唯一标志（官方文档指定的事件排重键）。
        aibotid: 智能机器人 BotID。
        chatid: 会话 ID（仅群聊类型返回；单聊为空）。
        chattype: 会话类型（``single`` 单聊 / ``group`` 群聊）。
        sender: 触发者信息（wire 字段名 ``from``）。
        response_url: 支持主动回复消息的临时 URL（本工程经长连接回复，不消费）。
        msgtype: 消息类型（text / image / mixed / voice / file / video / event）。
        create_time: 事件产生时间戳（epoch 秒——仅事件回调携带）。
        text: 文本内容结构体。
        image: 图片内容结构体。
        mixed: 图文混排内容结构体。
        voice: 语音内容结构体。
        file: 文件内容结构体。
        video: 视频内容结构体。
        quote: 引用内容结构体（用户引用其他消息时携带）。
        event: 事件内容结构体（事件回调携带）。
        compat_info: 来源附加信息（非普通用户消息携带）。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    msgid: str = Field(default="", description="回调唯一标志（排重键）")
    aibotid: str = Field(default="", description="智能机器人 BotID")
    chatid: str | None = Field(default=None, description="会话 ID（仅群聊）")
    chattype: str = Field(default="", description="会话类型（single / group）")
    sender: WeComSenderModel = Field(
        default_factory=WeComSenderModel, alias="from", description="触发者信息"
    )
    response_url: str | None = Field(default=None, description="主动回复临时 URL（不消费）")
    msgtype: str = Field(default="", description="消息类型")
    create_time: int = Field(default=0, description="事件产生时间戳（epoch 秒）")
    text: WeComTextContentModel | None = Field(default=None, description="文本内容")
    image: WeComMediaContentModel | None = Field(default=None, description="图片内容")
    mixed: WeComMixedContentModel | None = Field(default=None, description="图文混排内容")
    voice: WeComVoiceContentModel | None = Field(default=None, description="语音内容")
    file: WeComMediaContentModel | None = Field(default=None, description="文件内容")
    video: WeComMediaContentModel | None = Field(default=None, description="视频内容")
    quote: WeComQuoteModel | None = Field(default=None, description="引用内容")
    event: WeComEventContentModel | None = Field(default=None, description="事件内容")
    compat_info: WeComCompatInfoModel | None = Field(
        default=None, description="来源附加信息（非普通用户消息）"
    )


class WeComHeadersModel(BaseModel):
    """命令包裹 headers（请求 / 应答均携带）。

    Attributes:
        req_id: 请求唯一标识（平台应答原样透传；回复消息回调时须回填回调的
            req_id）。
    """

    model_config = ConfigDict(extra="ignore")

    req_id: str = Field(default="", description="请求唯一标识")


class WeComCallbackFrameModel(BaseModel):
    """入站回调帧（``aibot_msg_callback`` / ``aibot_event_callback``）。

    Attributes:
        cmd: 回调命令（回调命令值域见 :data:`WECOM_CALLBACK_COMMANDS`）。
        headers: 命令头（``req_id`` = 回复该回调时必须透传的标识）。
        body: 回调 body。
    """

    model_config = ConfigDict(extra="ignore")

    cmd: str = Field(min_length=1, description="回调命令")
    headers: WeComHeadersModel = Field(
        default_factory=WeComHeadersModel, description="命令头（req_id）"
    )
    body: WeComCallbackBodyModel = Field(
        default_factory=WeComCallbackBodyModel, description="回调 body"
    )


class WeComAckBodyModel(BaseModel):
    """应答 body（仅素材上传在应答中回传载荷）。

    Attributes:
        upload_id: 上传会话 ID（``aibot_upload_media_init`` 应答携带）。
        media_id: 临时素材 ID（``aibot_upload_media_finish`` 应答携带）。
        media_kind: 素材类型（wire 字段名 ``type``——完成上传应答携带）。
        created_at: 素材创建时间（epoch 秒字符串——完成上传应答携带）。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    upload_id: str | None = Field(default=None, description="上传会话 ID")
    media_id: str | None = Field(default=None, description="临时素材 ID")
    media_kind: str | None = Field(default=None, alias="type", description="素材类型")
    created_at: str | None = Field(default=None, description="素材创建时间（epoch 秒）")


class WeComAckModel(BaseModel):
    """平台应答包裹（所有出站命令的确认形态）。

    Attributes:
        cmd: 应答回显的命令名（部分场景缺失——缺省空串）。
        headers: 应答头（``req_id`` 透传请求标识）。
        errcode: 错误码（0 = 成功）。
        errmsg: 错误信息（成功为 ``ok``）。
        body: 应答载荷（素材上传命令携带）。
    """

    model_config = ConfigDict(extra="ignore")

    cmd: str = Field(default="", description="应答命令名（可能缺失）")
    headers: WeComHeadersModel = Field(
        default_factory=WeComHeadersModel, description="应答头（req_id 透传）"
    )
    errcode: int = Field(default=0, description="错误码（0 = 成功）")
    errmsg: str = Field(default="", description="错误信息")
    body: WeComAckBodyModel | None = Field(default=None, description="应答载荷")

    @property
    def is_success(self) -> bool:
        """应答是否成功（``errcode == 0``）。

        Returns:
            成功判定。
        """
        return self.errcode == 0


class WeComChatTargetModel(BaseModel):
    """出站目标解析结果（会话键 grammar 拆解）。

    Attributes:
        chat_type: 主动推送的会话类型值（1 = 单聊、2 = 群聊）。
        target_id: 单聊 userid / 群聊 chatid。
        is_group: 是否群聊会话。
    """

    chat_type: int = Field(description="主动推送会话类型（1 单聊 / 2 群聊）")
    target_id: str = Field(min_length=1, description="单聊 userid / 群聊 chatid")
    is_group: bool = Field(description="是否群聊会话")


class WeComReplyContextModel(BaseModel):
    """每会话最近一次消息回调的回复上下文（``aibot_respond_msg`` 寻址原料）。

    Attributes:
        req_id: 该消息回调的 req_id（回复时原样透传）。
        chat_id: 会话键（``single:`` / ``group:`` grammar）。
        ts_ms: 回调到达时刻（epoch 毫秒——回复窗口判定）。
    """

    req_id: str = Field(min_length=1, description="消息回调 req_id（回复时透传）")
    chat_id: str = Field(min_length=1, description="会话键")
    ts_ms: int = Field(ge=0, description="回调到达时刻（epoch 毫秒）")


class WeComSubscribeBodyModel(BaseModel):
    """订阅请求 body（官方文档「订阅请求」）。

    Attributes:
        bot_id: 智能机器人 BotID。
        secret: 长连接专用密钥（与回调 Token / EncodingAESKey 不同）。
    """

    bot_id: str = Field(min_length=1, description="智能机器人 BotID")
    secret: str = Field(min_length=1, description="长连接专用密钥")


class WeComMarkdownContentModel(BaseModel):
    """markdown 消息内容结构体。

    Attributes:
        content: markdown 文本（≤20480 字节 UTF-8）。
    """

    content: str = Field(min_length=1, description="markdown 文本（≤20480 字节）")


class WeComMarkdownBodyModel(BaseModel):
    """markdown 消息体（回复 / 主动推送共用）。

    Attributes:
        chatid: 主动推送目标会话 ID（回复形态省略——由 req_id 寻址）。
        chat_type: 主动推送会话类型（回复形态省略）。
        msgtype: 消息类型（固定 ``markdown``）。
        markdown: markdown 内容。
    """

    model_config = ConfigDict(populate_by_name=True)

    chatid: str | None = Field(default=None, description="主动推送目标会话 ID")
    chat_type: int | None = Field(default=None, description="主动推送会话类型（1 / 2）")
    msgtype: str = Field(default="markdown", description="消息类型（markdown）")
    markdown: WeComMarkdownContentModel = Field(description="markdown 内容")


class WeComMediaIdModel(BaseModel):
    """媒体消息体（image / voice / file 共用 ``media_id`` 载荷）。

    Attributes:
        media_id: 临时素材 ID（上传完成接口返回）。
    """

    media_id: str = Field(min_length=1, description="临时素材 ID")


class WeComMediaBodyModel(BaseModel):
    """附件消息体（主动推送面上的 image / voice / file 消息）。

    Attributes:
        chatid: 目标会话 ID。
        chat_type: 会话类型（1 单聊 / 2 群聊）。
        msgtype: 消息类型（image / voice / file）。
        image: 图片载荷（msgtype=image 时）。
        voice: 语音载荷（msgtype=voice 时）。
        file: 文件载荷（msgtype=file 时）。
    """

    chatid: str = Field(min_length=1, description="目标会话 ID")
    chat_type: int = Field(description="会话类型（1 单聊 / 2 群聊）")
    msgtype: str = Field(min_length=1, description="消息类型（image / voice / file）")
    image: WeComMediaIdModel | None = Field(default=None, description="图片载荷")
    voice: WeComMediaIdModel | None = Field(default=None, description="语音载荷")
    file: WeComMediaIdModel | None = Field(default=None, description="文件载荷")


class WeComUploadInitBodyModel(BaseModel):
    """临时素材上传初始化 body（官方文档「上传初始化」）。

    素材类型字段在 wire 上是 ``type``（Python 侧用 ``media_kind`` 命名——``type``
    是内建名），序列化别名只作用于出站 dump；该 body 只由本工程构造，平台不会
    回传同名结构，因此不做入站别名解析。

    Attributes:
        media_kind: 素材类型（wire 字段名 ``type``——file / image / voice / video）。
        filename: 文件名（≤256 字节）。
        total_size: 文件总大小字节数。
        total_chunks: 分片数量（≤100）。
        md5: 文件 MD5（可选——平台合并后校验完整性）。
    """

    media_kind: str = Field(
        serialization_alias="type", description="素材类型（file / image / voice / video）"
    )
    filename: str = Field(min_length=1, max_length=256, description="文件名（≤256 字节）")
    total_size: int = Field(
        ge=WECOM_UPLOAD_MIN_SIZE_BYTES,
        description="文件总大小（字节——平台下限 5 字节）",
    )
    total_chunks: int = Field(ge=1, le=WECOM_UPLOAD_MAX_CHUNKS, description="分片数量（≤100）")
    md5: str | None = Field(default=None, description="文件 MD5（可选）")


class WeComUploadChunkBodyModel(BaseModel):
    """临时素材分片 body（官方文档「上传分片」）。

    Attributes:
        upload_id: 上传会话 ID。
        chunk_index: 分片序号（从 0 起；官方文档示例为数值形态）。
        base64_data: 分片内容的 base64 编码。
    """

    upload_id: str = Field(min_length=1, description="上传会话 ID")
    chunk_index: int = Field(ge=0, description="分片序号（从 0 起）")
    base64_data: str = Field(min_length=1, description="分片内容 base64")


class WeComUploadFinishBodyModel(BaseModel):
    """临时素材上传完成 body（官方文档「上传结束」）。

    Attributes:
        upload_id: 上传会话 ID。
    """

    upload_id: str = Field(min_length=1, description="上传会话 ID")


#: 出站命令体联合（按命令取值——None = 无 body 命令，如 ``ping``）。
WeComCommandBody = (
    WeComSubscribeBodyModel
    | WeComMarkdownBodyModel
    | WeComMediaBodyModel
    | WeComUploadInitBodyModel
    | WeComUploadChunkBodyModel
    | WeComUploadFinishBodyModel
)


class WeComCommandModel(BaseModel):
    """出站命令包裹（``{"cmd", "headers": {"req_id"}, "body"}``）。

    Attributes:
        cmd: 命令名。
        headers: 命令头（``req_id`` 由客户端生成）。
        body: 命令体（None = 无 body 命令）。
    """

    cmd: str = Field(min_length=1, description="命令名")
    headers: WeComHeadersModel = Field(description="命令头")
    body: WeComCommandBody | None = Field(default=None, description="命令体")


def parse_wecom_chat_id(chat_id: str) -> WeComChatTargetModel:
    """解析会话键 grammar（``single:<userid>`` / ``group:<chatid>``）。

    Args:
        chat_id: 通道中立出站会话 ID。

    Returns:
        会话类型 / 目标 ID / 群聊标记。

    Raises:
        BusinessException: grammar 非法（未知前缀 / 目标 ID 为空——
            ``VALIDATION_ERROR``，调用方 bug 面）。
    """
    prefix, separator, target_id = chat_id.partition(":")
    if not separator or not target_id:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                "企微 chat_id 须为 single:<userid> 或 group:<chatid> 形态: "
                f"{chat_id!r}"
            ),
            details={"chat_id": chat_id},
        )
    if prefix == WECOM_SINGLE_PREFIX:
        return WeComChatTargetModel(
            chat_type=WECOM_CHAT_TYPE_SINGLE, target_id=target_id, is_group=False
        )
    if prefix == WECOM_GROUP_PREFIX:
        return WeComChatTargetModel(
            chat_type=WECOM_CHAT_TYPE_GROUP, target_id=target_id, is_group=True
        )
    raise BusinessException(
        code=ErrorCode.VALIDATION_ERROR,
        message=(
            "企微 chat_id 须为 single:<userid> 或 group:<chatid> 形态: "
            f"{chat_id!r}"
        ),
        details={"chat_id": chat_id},
    )


def _media_attachment(
    kind: ChannelAttachmentKind,
    content: WeComMediaContentModel,
    *,
    media_type: str | None = None,
) -> ChannelMessageAttachment | None:
    """把 wire 媒体结构体转换为通道中立附件（无 url 时跳过）。

    Args:
        kind: 附件类型。
        content: wire 媒体结构体（url 为空 = 平台未给引用，跳过）。
        media_type: MIME 线索（视频等平台未给 MIME 的类别为 None）。

    Returns:
        通道中立附件；``url`` 为空时返回 None。
    """
    if not content.url:
        return None
    return ChannelMessageAttachment(kind=kind, media_type=media_type, url=content.url)


def extract_wecom_attachments(body: WeComCallbackBodyModel) -> list[ChannelMessageAttachment]:
    """提取回调 body 的媒体字段为通道中立附件（url 源——密文由消费者解密）。

    Args:
        body: 回调 body。

    Returns:
        附件列表（无媒体字段时为空——纯文本 / 语音回调形态）；语音不产出附件
        （平台已转写为文本），视频归 ``file`` 类且 ``media_type="video/mp4"``。
    """
    attachments: list[ChannelMessageAttachment] = []
    if body.image is not None:
        attachment = _media_attachment(ChannelAttachmentKind.IMAGE, body.image)
        if attachment is not None:
            attachments.append(attachment)
    if body.mixed is not None:
        for item in body.mixed.msg_item:
            if item.image is None:
                continue
            attachment = _media_attachment(ChannelAttachmentKind.IMAGE, item.image)
            if attachment is not None:
                attachments.append(attachment)
    if body.file is not None:
        attachment = _media_attachment(ChannelAttachmentKind.FILE, body.file)
        if attachment is not None:
            attachments.append(attachment)
    if body.video is not None:
        attachment = _media_attachment(
            ChannelAttachmentKind.FILE, body.video, media_type="video/mp4"
        )
        if attachment is not None:
            attachments.append(attachment)
    return attachments


def flatten_wecom_callback_text(body: WeComCallbackBodyModel) -> str:
    """把回调 body 归一为可路由文本（占位符兜底纯媒体消息）。

    Args:
        body: 回调 body。

    Returns:
        归一文本（text 原文 / 语音转写原文 / 图文混排逐项拼接 / 纯媒体占位符）；
        未知消息类型返回空串（调用方按不可路由处理）。
    """
    if body.msgtype == "text":
        return body.text.content if body.text is not None else ""
    if body.msgtype == "voice":
        return body.voice.content if body.voice is not None else ""
    if body.msgtype == "mixed":
        if body.mixed is None:
            return ""
        parts: list[str] = []
        for item in body.mixed.msg_item:
            if item.msgtype == "text" and item.text is not None:
                parts.append(item.text.content)
            elif item.msgtype == "image":
                parts.append(WECOM_PLACEHOLDER_IMAGE)
        return "".join(parts)
    if body.msgtype == "image":
        return WECOM_PLACEHOLDER_IMAGE
    if body.msgtype == "file":
        return WECOM_PLACEHOLDER_FILE
    if body.msgtype == "video":
        return WECOM_PLACEHOLDER_VIDEO
    return ""


def map_wecom_callback_to_inbound(
    frame: WeComCallbackFrameModel,
    *,
    ts_ms: int | None = None,
) -> InboundMessageModel | None:
    """映射消息回调帧为通道中立入站消息（不可路由形态返回 None）。

    路由门槛：命令必须是 ``aibot_msg_callback``（事件回调不产出人类回合）；
    ``msgid`` 必须非空（官方文档排重键——缺失即无法排重）；会话类型必须是
    ``single`` / ``group``；归一文本必须非空。任一不满足返回 None（调用方
    记日志并跳过）。

    Args:
        frame: 入站回调帧。
        ts_ms: 入站时间（epoch 毫秒；None = 读取系统时钟——测试注入固定值）。

    Returns:
        入站消息（附件 / 群聊标记 / 机器来源标记已归一）；不可路由返回 None。
    """
    if frame.cmd != WECOM_CMD_MSG_CALLBACK:
        return None
    body = frame.body
    if not body.msgid:
        return None
    if body.chattype == WECOM_GROUP_PREFIX:
        if not body.chatid:
            return None
        chat_id = f"{WECOM_GROUP_PREFIX}:{body.chatid}"
        is_group_chat = True
    elif body.chattype == WECOM_SINGLE_PREFIX:
        if not body.sender.userid:
            return None
        chat_id = f"{WECOM_SINGLE_PREFIX}:{body.sender.userid}"
        is_group_chat = False
    else:
        return None
    text = flatten_wecom_callback_text(body)
    if not text:
        return None
    return InboundMessageModel(
        channel=ChannelType.WECOM,
        chat_id=chat_id,
        message_id=body.msgid,
        sender_id=body.sender.userid or "0",
        text=text,
        ts_ms=now_ms() if ts_ms is None else ts_ms,
        attachments=extract_wecom_attachments(body),
        synthetic=body.compat_info is not None,
        is_group_chat=is_group_chat,
    )


def wecom_media_kind_for_attachment(attachment: ChannelMessageAttachment) -> WeComMediaKind:
    """解析出站附件 kind → 临时素材上传类型。

    映射：image / sticker → image（企微无独立贴纸消息类型）；audio → voice；
    file → file。视频以 ``file`` 类载荷承载（上传类型 file）。

    Args:
        attachment: 出站附件。

    Returns:
        上传素材类型。
    """
    return WeComMediaKind(_UPLOAD_KIND_BY_ATTACHMENT[attachment.kind])


def wecom_upload_size_limit(media_kind: WeComMediaKind) -> int:
    """读取上传素材类型的明文大小上限（官方文档素材限制）。

    Args:
        media_kind: 上传素材类型。

    Returns:
        明文字节上限。
    """
    return _UPLOAD_SIZE_LIMIT_BY_KIND[media_kind.value]


def split_wecom_markdown_text(
    text: str, *, max_bytes: int = WECOM_MARKDOWN_MAX_BYTES
) -> list[str]:
    """按平台 content 字节上限切分超长文本（换行边界优先，硬切兜底）。

    切分口径与 Telegram 4096 分段同哲学：窗口内取最后一个换行符作为切点
    （段落不跨语义行），窗口内无换行时按 UTF-8 字节硬切（不产生半个字符——
    逐字符累计字节数）。空文本返回空列表（调用方拒发）。

    Args:
        text: 待发送文本（markdown 原文）。
        max_bytes: 单段字节上限（UTF-8 编码后；缺省官方 20480 字节）。

    Returns:
        分段列表（按原文顺序拼接等于原文；段数 = ceil(总字节 / 上限) 的上界）。
    """
    if not text:
        return []
    encoded_length = len(text.encode("utf-8"))
    if encoded_length <= max_bytes:
        return [text]
    segments: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            segments.append(remaining)
            break
        consumed_bytes = 0
        window_end = 0
        for index, char in enumerate(remaining):
            char_bytes = len(char.encode("utf-8"))
            if consumed_bytes + char_bytes > max_bytes:
                break
            consumed_bytes += char_bytes
            window_end = index + 1
        window = remaining[:window_end]
        newline_index = window.rfind("\n")
        if newline_index > 0:
            segments.append(window[:newline_index])
            remaining = remaining[newline_index + 1 :]
        else:
            segments.append(window)
            remaining = remaining[window_end:]
    return segments


def build_subscribe_command(*, req_id: str, bot_id: str, secret: str) -> WeComCommandModel:
    """构造订阅鉴权命令（连接建立后的第一条命令）。

    Args:
        req_id: 请求唯一标识。
        bot_id: 智能机器人 BotID。
        secret: 长连接专用密钥。

    Returns:
        订阅命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_SUBSCRIBE,
        headers=WeComHeadersModel(req_id=req_id),
        body=WeComSubscribeBodyModel(bot_id=bot_id, secret=secret),
    )


def build_ping_command(*, req_id: str) -> WeComCommandModel:
    """构造应用层心跳命令（官方文档建议 30 秒间隔）。

    Args:
        req_id: 请求唯一标识。

    Returns:
        心跳命令包裹（无 body）。
    """
    return WeComCommandModel(cmd=WECOM_CMD_PING, headers=WeComHeadersModel(req_id=req_id))


def build_markdown_reply_command(*, callback_req_id: str, content: str) -> WeComCommandModel:
    """构造回复消息回调的 markdown 命令（帧头回抄回调 req_id）。

    响应帧头携带 callback req_id 供平台归位，命令自身不再生成请求号。

    Args:
        callback_req_id: 触发回复的消息回调 req_id（官方文档要求原样回抄）。
        content: markdown 文本。

    Returns:
        回复命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_RESPOND_MSG,
        headers=WeComHeadersModel(req_id=callback_req_id),
        body=WeComMarkdownBodyModel(markdown=WeComMarkdownContentModel(content=content)),
    )


def build_markdown_push_command(
    *, req_id: str, target: WeComChatTargetModel, content: str
) -> WeComCommandModel:
    """构造主动推送的 markdown 命令（chatid + chat_type 寻址）。

    Args:
        req_id: 请求唯一标识。
        target: 解析后的会话目标。
        content: markdown 文本。

    Returns:
        推送命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_SEND_MSG,
        headers=WeComHeadersModel(req_id=req_id),
        body=WeComMarkdownBodyModel(
            chatid=target.target_id,
            chat_type=target.chat_type,
            markdown=WeComMarkdownContentModel(content=content),
        ),
    )


def build_media_push_command(
    *,
    req_id: str,
    target: WeComChatTargetModel,
    media_kind: WeComMediaKind,
    media_id: str,
) -> WeComCommandModel:
    """构造附件主动推送命令（image / voice / file 三类消息）。

    Args:
        req_id: 请求唯一标识。
        target: 解析后的会话目标。
        media_kind: 上传素材类型（image / voice / file；video 以 file 消息承载）。
        media_id: 上传完成返回的临时素材 ID。

    Returns:
        附件推送命令包裹。

    Raises:
        BusinessException: 素材类型无对应消息体（``video``——企微附件投递不产出
            视频消息，调用方不会传；防御性拒绝 ``VALIDATION_ERROR``）。
    """
    media = WeComMediaIdModel(media_id=media_id)
    if media_kind is WeComMediaKind.IMAGE:
        body = WeComMediaBodyModel(
            chatid=target.target_id,
            chat_type=target.chat_type,
            msgtype="image",
            image=media,
        )
    elif media_kind is WeComMediaKind.VOICE:
        body = WeComMediaBodyModel(
            chatid=target.target_id,
            chat_type=target.chat_type,
            msgtype="voice",
            voice=media,
        )
    elif media_kind is WeComMediaKind.FILE:
        body = WeComMediaBodyModel(
            chatid=target.target_id,
            chat_type=target.chat_type,
            msgtype="file",
            file=media,
        )
    else:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"企微附件投递无对应消息体: {media_kind.value}",
            details={"media_kind": media_kind.value},
        )
    return WeComCommandModel(
        cmd=WECOM_CMD_SEND_MSG,
        headers=WeComHeadersModel(req_id=req_id),
        body=body,
    )


def build_upload_init_command(
    *,
    req_id: str,
    media_kind: WeComMediaKind,
    filename: str,
    total_size: int,
    total_chunks: int,
    md5: str | None = None,
) -> WeComCommandModel:
    """构造临时素材上传初始化命令。

    Args:
        req_id: 请求唯一标识。
        media_kind: 素材类型。
        filename: 文件名。
        total_size: 文件总大小（字节）。
        total_chunks: 分片数量。
        md5: 文件 MD5（可选）。

    Returns:
        上传初始化命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_UPLOAD_MEDIA_INIT,
        headers=WeComHeadersModel(req_id=req_id),
        body=WeComUploadInitBodyModel(
            media_kind=media_kind.value,
            filename=filename,
            total_size=total_size,
            total_chunks=total_chunks,
            md5=md5,
        ),
    )


def build_upload_chunk_command(
    *, req_id: str, upload_id: str, chunk_index: int, base64_data: str
) -> WeComCommandModel:
    """构造临时素材分片上传命令。

    Args:
        req_id: 请求唯一标识。
        upload_id: 上传会话 ID。
        chunk_index: 分片序号（从 0 起）。
        base64_data: 分片内容 base64。

    Returns:
        分片上传命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_UPLOAD_MEDIA_CHUNK,
        headers=WeComHeadersModel(req_id=req_id),
        body=WeComUploadChunkBodyModel(
            upload_id=upload_id, chunk_index=chunk_index, base64_data=base64_data
        ),
    )


def build_upload_finish_command(*, req_id: str, upload_id: str) -> WeComCommandModel:
    """构造临时素材上传完成命令。

    Args:
        req_id: 请求唯一标识。
        upload_id: 上传会话 ID。

    Returns:
        上传完成命令包裹。
    """
    return WeComCommandModel(
        cmd=WECOM_CMD_UPLOAD_MEDIA_FINISH,
        headers=WeComHeadersModel(req_id=req_id),
        body=WeComUploadFinishBodyModel(upload_id=upload_id),
    )
