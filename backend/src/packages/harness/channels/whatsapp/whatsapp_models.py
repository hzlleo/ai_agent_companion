"""WhatsApp Cloud API 入站通道——webhook 载荷归一 / 去重窗口 / 服务窗（Unit 文件）。

WhatsApp Cloud API 是 **Meta 托管**的消息通道：入站只能经 Meta 的 webhook
推送（``entry[].changes[].value``），出站经 Graph API ``/{version}/
{phone_number_id}/messages``（文本消息，受 24 小时客户服务窗约束）。本模块
只做纯变换与进程内状态（无 HTTP、无路由副作用）：

    载荷 JSON
        ↓ ① 容错解析（多余字段忽略、缺失字段给默认、非法载荷返回空批次）
        ↓ ② 入站归一（``messages[]`` 逐条展平；``statuses[]`` 只计数）
        ↓ ③ 文本回退（text.body；媒体取 caption 或占位标签；位置 / 按钮 /
           交互回复取文本回退）
        ↓ InboundMessageModel（channel=whatsapp、chat_id=wa:<wa_id>、
           synthetic=False、is_group_chat=False、is_owner 按属主清单命中）
        ↓ 去重键（wamid 常驻 FIFO——吸收 Meta 偶发重复推送）
        ↓ 服务窗状态（每会话最近入站毫秒——24 小时客户服务窗判定原料）

语义边界（锁定决策）：

    - **会话键**：``wa:<归一 wa_id>``（wa_id 是国际号码，国家码是号码本身
      的一部分，归一必须保留；归一复用
      :mod:`packages.harness.channels.phone_numbers` 的 WhatsApp 口径）。
      发送者不是人格 ID——人格仍由通道路由器绑定；
    - **仅私聊语义**：WhatsApp Cloud API 的 webhook 不区分群聊
      （``is_group_chat=False``）；群聊多人格路径不适用；
    - **来源分级**：默认 ``peer``（第三方发送者不自动晋升主人），仅当
      归一 wa_id 命中 ``WHATSAPP_OWNER_NUMBERS`` 时 ``is_owner=True``；
    - **时间**：Cloud API 的 ``timestamp`` 是秒级字符串，归一为 epoch
      毫秒；缺失 / 非法 / 非正值回落服务端时钟；
    - **状态事件**：``statuses[]``（sent / delivered / read 回执）不进
      路由、不登记去重，只累计 ``status_events_ignored`` 供观测。
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from typing import Any, Final, Protocol

from loguru import logger
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.phone_numbers import normalize_whatsapp_wa_id
from packages.harness.core import time_utils

__all__ = [
    "WHATSAPP_DEDUP_WINDOW_SIZE",
    "WHATSAPP_SERVICE_WINDOW_MS",
    "WhatsAppButtonModel",
    "WhatsAppContactModel",
    "WhatsAppDedupWindow",
    "WhatsAppInboundRouterFace",
    "WhatsAppInteractiveModel",
    "WhatsAppLocationModel",
    "WhatsAppMediaModel",
    "WhatsAppMessageModel",
    "WhatsAppMetadataModel",
    "WhatsAppParsedBatchModel",
    "WhatsAppParsedMessageModel",
    "WhatsAppProfileModel",
    "WhatsAppServiceWindow",
    "WhatsAppSendMessageIdModel",
    "WhatsAppSendRequestModel",
    "WhatsAppSendResponseModel",
    "WhatsAppStatusModel",
    "WhatsAppTextModel",
    "WhatsAppWebhookPayloadModel",
    "parse_whatsapp_webhook_payload",
    "process_whatsapp_webhook_payload",
    "reset_whatsapp_state",
    "resolve_whatsapp_timestamp_ms",
]

#: wamid 去重窗口容量（已见键上限——超出按 FIFO 淘汰；进程重启清零）。
WHATSAPP_DEDUP_WINDOW_SIZE: Final[int] = 1024

#: 24 小时客户服务窗毫秒（Cloud API 官方：用户最后一条消息起 24 小时内可
#: 发送自由格式消息；超出必须使用模板消息——本通道暂未实现模板发送）。
WHATSAPP_SERVICE_WINDOW_MS: Final[int] = 24 * 60 * 60 * 1000

#: 秒级时间戳下界（低于该值视为非法——回落到服务端时钟）。
_EPOCH_SECONDS_MIN: Final[int] = 1_000_000_000

#: 毫秒级时间戳下界（达到该值按毫秒解释，避免二次放大）。
_EPOCH_MILLIS_MIN: Final[int] = 1_000_000_000_000

#: 媒体 / 结构化消息的文本占位标签（无 caption / 无文本回退时的正文）。
_MEDIA_PLACEHOLDERS: Final[dict[str, str]] = {
    "image": "[图片]",
    "audio": "[语音]",
    "document": "[文件]",
    "sticker": "[贴纸]",
    "video": "[视频]",
}


class WhatsAppInboundRouterFace(Protocol):
    """WhatsApp 入站路由面（生产 ChannelRouter；测试 fake 同形态）。"""

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """处理一条入站消息（调用方承担异常隔离）。

        Args:
            message: 通道中立入站消息。

        Returns:
            投递结果（直调路径不消费，仅保持路由器契约）。
        """
        ...


class WhatsAppProfileModel(BaseModel):
    """WhatsApp 联系人资料（``contacts[].profile``）。

    Attributes:
        name: 联系人显示名（仅观测 / 日志，不参与路由与身份键）。
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="联系人显示名（观测面）")


class WhatsAppContactModel(BaseModel):
    """WhatsApp 联系人条目（``contacts[]``）。

    Attributes:
        wa_id: 联系人 WhatsApp 号码（国际号码，不带加号）。
        profile: 联系人资料。
    """

    model_config = ConfigDict(extra="ignore")

    wa_id: str = Field(default="", description="联系人 WhatsApp 号码")
    profile: WhatsAppProfileModel = Field(
        default_factory=WhatsAppProfileModel, description="联系人资料"
    )


class WhatsAppMetadataModel(BaseModel):
    """webhook value 元数据（``metadata``）。

    Attributes:
        display_phone_number: 企业号码的展示形态。
        phone_number_id: 企业号码 ID（出站 Graph API 路径段）。
    """

    model_config = ConfigDict(extra="ignore")

    display_phone_number: str = Field(default="", description="企业号码展示形态")
    phone_number_id: str = Field(default="", description="企业号码 ID")


class WhatsAppTextModel(BaseModel):
    """文本消息体（``text``）。

    Attributes:
        body: 文本正文。
    """

    model_config = ConfigDict(extra="ignore")

    body: str = Field(default="", description="文本正文")


class WhatsAppMediaModel(BaseModel):
    """媒体消息体（``image`` / ``audio`` / ``document`` / ``sticker`` / ``video``）。

    Attributes:
        id: 媒体 ID（下载需二次调用媒体端点——本通道只消费 caption 文本）。
        mime_type: MIME 类型。
        caption: 媒体说明文本（图片 / 视频 / 文档可能携带）。
        filename: 文件名（文档消息）。
        voice: 是否语音消息（音频消息）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="媒体 ID")
    mime_type: str = Field(default="", description="MIME 类型")
    caption: str = Field(default="", description="媒体说明文本")
    filename: str = Field(default="", description="文件名")
    voice: bool = Field(default=False, description="是否语音消息")


class WhatsAppLocationModel(BaseModel):
    """位置消息体（``location``）。

    Attributes:
        latitude: 纬度（观测面）。
        longitude: 经度（观测面）。
        name: 地点名称（文本回退首选）。
        address: 地点地址（文本回退次选）。
    """

    model_config = ConfigDict(extra="ignore")

    latitude: float | None = Field(default=None, description="纬度")
    longitude: float | None = Field(default=None, description="经度")
    name: str = Field(default="", description="地点名称")
    address: str = Field(default="", description="地点地址")


class WhatsAppButtonModel(BaseModel):
    """模板按钮回执（``button``）。

    Attributes:
        text: 按钮文本（文本回退来源）。
        payload: 按钮负载。
    """

    model_config = ConfigDict(extra="ignore")

    text: str = Field(default="", description="按钮文本")
    payload: str = Field(default="", description="按钮负载")


class WhatsAppReplyModel(BaseModel):
    """交互回复条目（``interactive.button_reply`` / ``list_reply``）。

    Attributes:
        id: 选项 ID。
        title: 选项标题（文本回退来源）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="选项 ID")
    title: str = Field(default="", description="选项标题")


class WhatsAppInteractiveModel(BaseModel):
    """交互回复消息体（``interactive``）。

    Attributes:
        type: 交互类型（button_reply / list_reply）。
        button_reply: 按钮回复条目。
        list_reply: 列表回复条目。
    """

    model_config = ConfigDict(extra="ignore")

    type: str = Field(default="", description="交互类型")
    button_reply: WhatsAppReplyModel | None = Field(default=None, description="按钮回复")
    list_reply: WhatsAppReplyModel | None = Field(default=None, description="列表回复")


class WhatsAppStatusModel(BaseModel):
    """消息状态回执（``statuses[]``——只计数，不进路由）。

    Attributes:
        id: 被回执的消息 ID（wamid）。
        status: 状态字面量（sent / delivered / read / failed 等）。
        recipient_id: 接收方号码。
        timestamp: 状态时间戳（秒级字符串）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="被回执的消息 ID")
    status: str = Field(default="", description="状态字面量")
    recipient_id: str = Field(default="", description="接收方号码")
    timestamp: str = Field(default="", description="状态时间戳")


class WhatsAppMessageModel(BaseModel):
    """入站消息（``messages[]``——容错解析面，多余字段忽略）。

    Attributes:
        from_value: 发送者 wa_id（别名 ``from``；国际号码不带加号）。
        id: 消息 ID（wamid——去重首选键）。
        timestamp: 秒级时间戳（数值 / 数值字符串；非法回落服务端时钟）。
        type: 消息类型（text / image / audio / document / sticker / video /
            location / button / interactive 等）。
        text: 文本消息体。
        image: 图片消息体。
        audio: 音频消息体。
        document: 文档消息体。
        sticker: 贴纸消息体。
        video: 视频消息体。
        location: 位置消息体。
        button: 模板按钮回执。
        interactive: 交互回复消息体。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    from_value: str = Field(
        default="",
        validation_alias=AliasChoices("from"),
        description="发送者 wa_id（国际号码）",
    )
    id: str = Field(default="", description="消息 ID（wamid）")
    timestamp: int | float | str | None = Field(
        default=None, description="秒级时间戳（非法回落服务端时钟）"
    )
    type: str = Field(default="", description="消息类型")
    text: WhatsAppTextModel | None = Field(default=None, description="文本消息体")
    image: WhatsAppMediaModel | None = Field(default=None, description="图片消息体")
    audio: WhatsAppMediaModel | None = Field(default=None, description="音频消息体")
    document: WhatsAppMediaModel | None = Field(default=None, description="文档消息体")
    sticker: WhatsAppMediaModel | None = Field(default=None, description="贴纸消息体")
    video: WhatsAppMediaModel | None = Field(default=None, description="视频消息体")
    location: WhatsAppLocationModel | None = Field(default=None, description="位置消息体")
    button: WhatsAppButtonModel | None = Field(default=None, description="按钮回执")
    interactive: WhatsAppInteractiveModel | None = Field(
        default=None, description="交互回复消息体"
    )

    @field_validator("from_value", "id", "type", mode="before")
    @classmethod
    def _coerce_text_field(cls, value: object) -> object:
        """把缺失 / 标量字段归一为文本（缺失字段给默认）。

        Args:
            value: 原始字段值（``None`` / 字符串 / 数字）。

        Returns:
            ``None`` → 空串；数字 → 十进制字符串；其余原样返回。
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return value


class WhatsAppValueModel(BaseModel):
    """webhook change 载荷（``entry[].changes[].value``）。

    Attributes:
        messaging_product: 产品标识（恒 whatsapp）。
        metadata: 企业号码元数据。
        contacts: 联系人条目。
        messages: 入站消息条目。
        statuses: 状态回执条目（只计数）。
    """

    model_config = ConfigDict(extra="ignore")

    messaging_product: str = Field(default="", description="产品标识")
    metadata: WhatsAppMetadataModel = Field(
        default_factory=WhatsAppMetadataModel, description="企业号码元数据"
    )
    contacts: list[WhatsAppContactModel] = Field(
        default_factory=list, description="联系人条目"
    )
    messages: list[WhatsAppMessageModel] = Field(
        default_factory=list, description="入站消息条目"
    )
    statuses: list[WhatsAppStatusModel] = Field(
        default_factory=list, description="状态回执条目"
    )


class WhatsAppChangeModel(BaseModel):
    """webhook change 条目（``entry[].changes[]``）。

    Attributes:
        field: 变更字段名（messages / message_template_status_update 等）。
        value: 变更载荷。
    """

    model_config = ConfigDict(extra="ignore")

    field: str = Field(default="", description="变更字段名")
    value: WhatsAppValueModel | None = Field(default=None, description="变更载荷")


class WhatsAppEntryModel(BaseModel):
    """webhook entry 条目（``entry[]``）。

    Attributes:
        id: 业务账号 ID。
        changes: 变更条目列表。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="业务账号 ID")
    changes: list[WhatsAppChangeModel] = Field(default_factory=list, description="变更条目")


class WhatsAppWebhookPayloadModel(BaseModel):
    """WhatsApp webhook 推送载荷（容错解析面——多余字段忽略）。

    Attributes:
        object: 推送对象标识（恒 whatsapp_business_account）。
        entry: 业务账号条目列表。
    """

    model_config = ConfigDict(extra="ignore")

    object: str = Field(default="", description="推送对象标识")
    entry: list[WhatsAppEntryModel] = Field(default_factory=list, description="业务账号条目")


class WhatsAppParsedMessageModel(BaseModel):
    """单条入站消息解析结果（入站消息 + 去重键）。

    Attributes:
        inbound: 通道中立入站消息（可直接交给路由器）。
        dedup_key: 去重键（``wamid:<id>``）。
    """

    inbound: InboundMessageModel = Field(description="通道中立入站消息")
    dedup_key: str = Field(min_length=1, description="去重键")


class WhatsAppParsedBatchModel(BaseModel):
    """单次 webhook 载荷解析结果（可含多条消息 + 状态事件计数）。

    Attributes:
        messages: 可路由入站消息列表（空 = 无可路由消息）。
        status_events_ignored: 被忽略的状态回执条数（观测面）。
    """

    messages: list[WhatsAppParsedMessageModel] = Field(
        default_factory=list, description="可路由入站消息列表"
    )
    status_events_ignored: int = Field(default=0, description="被忽略的状态回执条数")


class WhatsAppSendMessageIdModel(BaseModel):
    """出站响应中的消息凭据条目（``messages[]``）。

    Attributes:
        id: 平台分配的消息 ID（wamid）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="平台分配的消息 ID")


class WhatsAppSendResponseModel(BaseModel):
    """出站发送响应（Graph API ``/messages`` 成功形态子集）。

    Attributes:
        messaging_product: 产品标识（恒 whatsapp）。
        contacts: 接收方联系人条目。
        messages: 已发送消息凭据条目。
    """

    model_config = ConfigDict(extra="ignore")

    messaging_product: str = Field(default="", description="产品标识")
    contacts: list[WhatsAppContactModel] = Field(
        default_factory=list, description="接收方联系人条目"
    )
    messages: list[WhatsAppSendMessageIdModel] = Field(
        default_factory=list, description="已发送消息凭据条目"
    )


class WhatsAppSendRequestModel(BaseModel):
    """出站文本消息请求体（Graph API ``/messages`` 文本形态）。

    Attributes:
        messaging_product: 产品标识（恒 whatsapp）。
        recipient_type: 接收方类型（individual）。
        to: 目标 WhatsApp 号码（归一后的国际号码——国家码保留）。
        type: 消息类型（text）。
        text: 文本消息体。
    """

    model_config = ConfigDict(extra="ignore")

    messaging_product: str = Field(default="whatsapp", description="产品标识")
    recipient_type: str = Field(default="individual", description="接收方类型")
    to: str = Field(min_length=1, description="目标 WhatsApp 号码（含国家码）")
    type: str = Field(default="text", description="消息类型")
    text: WhatsAppTextModel = Field(description="文本消息体")


class WhatsAppDedupWindow:
    """WhatsApp wamid 去重窗口（进程内有界 FIFO，容量超限淘汰最旧）。

    本通道端点对已接收推送恒回 200（Meta 仅对非 2xx 重投），去重窗因此
    不是重投防线，而是吸收 Meta 偶发重复推送（同 wamid 重复到达）的
    幂等面。窗口是进程内状态（多 worker 部署各自独立），进程重启清零；
    容量上限内常驻（wamid 全局唯一，无需时间窗）。
    """

    def __init__(self, cap: int = WHATSAPP_DEDUP_WINDOW_SIZE) -> None:
        """初始化去重窗口。

        Args:
            cap: 窗口容量（默认 1024；须 ≥ 1）。

        Raises:
            ValueError: ``cap < 1``（零容量令新登记键即刻淘汰——去重静默失效）。
        """
        if cap < 1:
            raise ValueError("cap 须 ≥ 1")
        self.cap = cap
        self._keys: set[str] = set()
        self._order: deque[str] = deque()

    def contains(self, key: str) -> bool:
        """判断去重键是否已登记（只读）。

        Args:
            key: 去重键（``wamid:<id>``）。

        Returns:
            True = 窗口内已登记。
        """
        return key in self._keys

    def remember(self, key: str) -> bool:
        """登记去重键（容量超限按 FIFO 淘汰最旧）。

        Args:
            key: 去重键（``wamid:<id>``）。

        Returns:
            首见返回 True；窗口内重复返回 False。
        """
        if key in self._keys:
            return False
        self._keys.add(key)
        self._order.append(key)
        while len(self._order) > self.cap:
            oldest = self._order.popleft()
            self._keys.discard(oldest)
        return True

    def __len__(self) -> int:
        """当前窗口内键数（观测 / 测试面）。"""
        return len(self._keys)


class WhatsAppServiceWindow:
    """24 小时客户服务窗状态（进程内、单实例限制——文档化边界）。

    Cloud API 只允许在用户最后一条消息后的 24 小时内发送自由格式消息；
    超出窗口必须使用模板消息（本通道暂未实现模板发送）。本类记录每个
    会话最近一条入站消息的 epoch 毫秒，供出站适配器在发送前判定窗口。

    进程内状态：多 worker / 多实例部署各自独立（重启清零；负载均衡后
    某实例未见过入站时会把出站判为窗口外）。需要严格窗口语义时保持
    ``SERVER_WORKERS=1`` 并单实例部署（与去重窗同一边界）。
    """

    def __init__(self, window_ms: int = WHATSAPP_SERVICE_WINDOW_MS) -> None:
        """初始化服务窗状态。

        Args:
            window_ms: 窗口毫秒（默认 24 小时）。
        """
        self.window_ms = window_ms
        self._last_inbound: dict[str, int] = {}

    def record_inbound(self, wa_id: str, inbound_ms: int) -> None:
        """登记会话最近入站时刻（取较新值——乱序重投不回退窗口）。

        Args:
            wa_id: 归一后的 WhatsApp 号码（会话键原料）。
            inbound_ms: 该条入站消息的 epoch 毫秒。
        """
        if not wa_id:
            return
        previous = self._last_inbound.get(wa_id)
        if previous is not None and previous >= inbound_ms:
            return
        self._last_inbound[wa_id] = inbound_ms

    def last_inbound_ms(self, wa_id: str) -> int | None:
        """读取会话最近入站时刻（未见过返回 None）。

        Args:
            wa_id: 归一后的 WhatsApp 号码。

        Returns:
            epoch 毫秒；该会话从未收到入站消息时为 None。
        """
        return self._last_inbound.get(wa_id)

    def is_within_window(self, wa_id: str, now_ms: int) -> bool:
        """判定当前时刻是否仍在 24 小时客户服务窗内。

        Args:
            wa_id: 归一后的 WhatsApp 号码。
            now_ms: 当前时刻（epoch 毫秒）。

        Returns:
            True = 窗口内（含边界）；从未收到入站消息 / 已超窗返回 False。
        """
        last = self.last_inbound_ms(wa_id)
        if last is None:
            return False
        return now_ms - last <= self.window_ms


def resolve_whatsapp_timestamp_ms(
    value: int | float | str | None, *, fallback_ms: int
) -> int:
    """解析 Cloud API 时间戳为 epoch 毫秒（非法 / 缺失回落服务端时钟）。

    支持形态：秒级数值 / 数值字符串（×1000）、毫秒数值（原样）。负值 /
    零值 / 过小数值 / 非数值文本 / 非有限浮点（``NaN`` / ``Infinity`` /
    字符串 ``"nan"`` / ``"inf"``）视为非法（回落 ``fallback_ms``）。

    Args:
        value: 原始时间戳（数值 / 数值字符串 / None）。
        fallback_ms: 非法或缺失时的回落时刻（服务端时钟 epoch 毫秒）。

    Returns:
        epoch 毫秒时间戳（恒正）。
    """
    if isinstance(value, bool):
        return fallback_ms
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return fallback_ms
        try:
            candidate = int(value)
        except (OverflowError, ValueError):
            return fallback_ms
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback_ms
        try:
            candidate = int(text)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return fallback_ms
            if not math.isfinite(parsed):
                return fallback_ms
            try:
                candidate = int(parsed)
            except (OverflowError, ValueError):
                return fallback_ms
    else:
        return fallback_ms
    if candidate <= 0:
        return fallback_ms
    if candidate >= _EPOCH_MILLIS_MIN:
        return candidate
    if candidate >= _EPOCH_SECONDS_MIN:
        return candidate * 1000
    return fallback_ms


def _resolve_message_text(message: WhatsAppMessageModel) -> str:
    """按消息类型取文本正文（无文本回退返回空串——调用方按空正文丢弃）。

    Args:
        message: 容错解析后的入站消息。

    Returns:
        去除首尾空白的正文；不可路由类型返回空串。
    """
    message_type = message.type.strip().lower()
    if message_type == "text":
        return (message.text.body if message.text is not None else "").strip()
    if message_type in _MEDIA_PLACEHOLDERS:
        media = {
            "image": message.image,
            "audio": message.audio,
            "document": message.document,
            "sticker": message.sticker,
            "video": message.video,
        }[message_type]
        caption = (media.caption if media is not None else "").strip()
        return caption or _MEDIA_PLACEHOLDERS[message_type]
    if message_type == "location" and message.location is not None:
        location = message.location
        return (location.name or location.address).strip() or "[位置]"
    if message_type == "button" and message.button is not None:
        return message.button.text.strip() or "[按钮]"
    if message_type == "interactive" and message.interactive is not None:
        interactive = message.interactive
        for reply in (interactive.button_reply, interactive.list_reply):
            if reply is not None and reply.title.strip():
                return reply.title.strip()
        return "[交互回复]"
    return ""


def _contact_names_by_wa_id(
    value: WhatsAppValueModel,
) -> dict[str, str]:
    """构造归一 wa_id → 联系人显示名映射（仅日志观测面）。

    Args:
        value: webhook change 载荷。

    Returns:
        归一键到显示名的映射（空名 / 空键条目跳过）。
    """
    names: dict[str, str] = {}
    for contact in value.contacts:
        normalized = normalize_whatsapp_wa_id(contact.wa_id)
        name = contact.profile.name.strip()
        if normalized and name:
            names[normalized] = name
    return names


def _parse_message(
    message: WhatsAppMessageModel,
    *,
    contact_names: dict[str, str],
    owner_numbers: frozenset[str],
    fallback_ms: int,
) -> WhatsAppParsedMessageModel | None:
    """归一单条入站消息（不可路由返回 None）。

    Args:
        message: 容错解析后的入站消息。
        contact_names: 归一 wa_id → 联系人显示名（日志观测面）。
        owner_numbers: 属主号码归一集合（命中则 ``is_owner=True``）。
        fallback_ms: 时间戳缺失 / 非法时的服务端时刻。

    Returns:
        解析结果；缺 wamid / 缺发送者 / 空正文 / 不可路由类型返回 None。
    """
    wamid = message.id.strip()
    if not wamid:
        logger.debug("WhatsApp 消息缺少 wamid（确认并忽略）: type={!r}", message.type)
        return None
    sender_key = normalize_whatsapp_wa_id(message.from_value)
    if not sender_key:
        logger.debug("WhatsApp 消息缺少发送者（确认并忽略）: wamid={}", wamid)
        return None
    text = _resolve_message_text(message)
    if not text:
        logger.debug(
            "WhatsApp 空正文 / 不可路由类型（跳过路由）: wamid={}, type={!r}",
            wamid,
            message.type,
        )
        return None
    ts_ms = resolve_whatsapp_timestamp_ms(message.timestamp, fallback_ms=fallback_ms)
    contact_name = contact_names.get(sender_key, "")
    if contact_name:
        logger.debug(
            "WhatsApp 入站消息归一: wa_id={}, contact={!r}, type={!r}",
            sender_key,
            contact_name,
            message.type,
        )
    inbound = InboundMessageModel(
        channel=ChannelType.WHATSAPP,
        chat_id=f"wa:{sender_key}",
        message_id=wamid,
        sender_id=sender_key,
        text=text,
        ts_ms=ts_ms,
        synthetic=False,
        is_group_chat=False,
        is_owner=sender_key in owner_numbers,
    )
    return WhatsAppParsedMessageModel(inbound=inbound, dedup_key=f"wamid:{wamid}")


def parse_whatsapp_webhook_payload(
    payload: dict[str, Any],
    *,
    owner_numbers: frozenset[str],
    clock: Callable[[], int] = time_utils.now_ms,
) -> WhatsAppParsedBatchModel:
    """解析一次 WhatsApp webhook 推送（纯变换——不做去重登记、不触路由）。

    空批次表示无可路由消息（载荷非法 / 仅状态回执 / 全部不可路由）；
    调用方确认并忽略，且不登记去重（无可路由内容，无需占窗口）。

    Args:
        payload: HTTP 层已保证为 JSON 对象的推送载荷。
        owner_numbers: 属主号码归一集合（命中则 ``is_owner=True``）。
        clock: 服务端时钟（epoch 毫秒——时间戳缺失 / 非法时回落）。

    Returns:
        解析批次（可路由消息列表 + 被忽略的状态回执计数）。
    """
    try:
        wire = WhatsAppWebhookPayloadModel.model_validate(payload)
    except ValidationError as exc:
        logger.warning("WhatsApp 载荷非法（隔离确认）: {}", str(exc)[:200])
        return WhatsAppParsedBatchModel()
    fallback_ms = clock()
    status_events_ignored = 0
    messages: list[WhatsAppParsedMessageModel] = []
    for entry in wire.entry:
        for change in entry.changes:
            value = change.value
            if value is None:
                continue
            status_events_ignored += len(value.statuses)
            contact_names = _contact_names_by_wa_id(value)
            for message in value.messages:
                parsed = _parse_message(
                    message,
                    contact_names=contact_names,
                    owner_numbers=owner_numbers,
                    fallback_ms=fallback_ms,
                )
                if parsed is not None:
                    messages.append(parsed)
    return WhatsAppParsedBatchModel(
        messages=messages, status_events_ignored=status_events_ignored
    )


#: 进程级去重窗口单例（webhook 直调路径与 runner 各自独立——本单例只服务
#: 未装配 runner 的嵌入形态；测试经 :func:`reset_whatsapp_state` 复位）。
_DEDUP_WINDOW = WhatsAppDedupWindow()


def reset_whatsapp_state() -> None:
    """复位 webhook 直调路径的进程内去重窗口（测试隔离面）。"""
    global _DEDUP_WINDOW
    _DEDUP_WINDOW = WhatsAppDedupWindow()


async def process_whatsapp_webhook_payload(
    payload: dict[str, Any],
    router: WhatsAppInboundRouterFace,
    *,
    owner_numbers: frozenset[str],
    clock: Callable[[], int] = time_utils.now_ms,
) -> None:
    """处理一次 WhatsApp 推送（解析 → 去重 → 路由；恒不抛）。

    单事件直调路径（测试 / 未装配 runner 的嵌入形态）：生产端点经
    :class:`packages.harness.channels.whatsapp.whatsapp_runner.
    WhatsAppWebhookRunner` 的有界队列消纳，不在 HTTP 请求内等待路由。
    本函数保留「一次调用完成本批消息」的语义，与 runner 共享解析面与
    去重窗口类型（各自实例：直调路径用模块级窗口，runner 持独立窗口）。

    Args:
        payload: HTTP 层已保证为 JSON 对象的推送载荷。
        router: 通道路由器（入站消费面——单消息错误隔离由本函数兜底）。
        owner_numbers: 属主号码归一集合。
        clock: 服务端时钟（时间戳回落）。
    """
    batch = parse_whatsapp_webhook_payload(
        payload, owner_numbers=owner_numbers, clock=clock
    )
    if batch.status_events_ignored:
        logger.debug(
            "WhatsApp 状态回执忽略（仅观测）: count={}", batch.status_events_ignored
        )
    for parsed in batch.messages:
        if not _DEDUP_WINDOW.remember(parsed.dedup_key):
            logger.debug(
                "WhatsApp 消息重复投递（去重跳过）: key={}", parsed.dedup_key
            )
            continue
        try:
            await router.handle_inbound(parsed.inbound)
        except Exception:
            logger.opt(exception=True).error(
                "WhatsApp 消息路由失败（隔离确认）: chat_id={}, message_id={}",
                parsed.inbound.chat_id,
                parsed.inbound.message_id,
            )

