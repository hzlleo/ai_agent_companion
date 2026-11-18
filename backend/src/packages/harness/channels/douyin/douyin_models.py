"""douyin 通道 Model Classes——抖音 web 会话 wire 子集 / 入站归一 / 会话键语法。

抖音没有面向个人号的官方消息 API（开放平台的能力面向企业号与视频评论），
本工程的抖音接入走 **web 会话底座**：由浏览器 sidecar 持有已登录的抖音
页面（``BROWSER_SIDECAR_PROFILE`` 的持久化 context），通道层经
``evaluate`` 工具在页面上下文读取会话 DOM / 写入并发送消息。本模块只承载
**通道层语义**（wire 解析模型 / 通道中立归一 / 会话键），DOM 选择器与 JS
表达式在 ``douyin_scripts``，浏览器协议在 ``douyin_client``。

链路与字段说明见 docs/backend/channels.md「抖音 web 通道」一节。

会话键语法（锁定）：``im:<conversation_id>``——与 QQ 的
``private:<uid>`` / 企微的 ``single:<userid>`` 同构的「通道内目标前缀 +
平台标识」形态；路由器会话身份键因此为
``channel:douyin:im:<conversation_id>``（通道 × 会话天然隔离）。
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import BaseModel, Field, model_validator

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "DOUYIN_CHAT_ID_PREFIX",
    "DOUYIN_DEFAULT_CONVERSATION_ID",
    "DOUYIN_IMAGE_PLACEHOLDER_TEXT",
    "DouyinWebMessageModel",
    "DouyinChatSnapshotModel",
    "DouyinNormalizeParamsModel",
    "build_douyin_chat_id",
    "parse_douyin_chat_id",
    "map_douyin_snapshot_to_inbound",
]

#: 会话键前缀（出站 chat_id 的 grammar 段——与 ``parse_douyin_chat_id`` 对称）。
DOUYIN_CHAT_ID_PREFIX: Final[str] = "im:"

#: 页面未报告会话标识且配置未指定时的兜底会话标识（单会话部署形态）。
DOUYIN_DEFAULT_CONVERSATION_ID: Final[str] = "default"

#: 纯图片消息的归一文本（人格可感知附件存在——与 QQ ``[图片]`` 占位符同语义）。
DOUYIN_IMAGE_PLACEHOLDER_TEXT: Final[str] = "[图片]"


class DouyinWebMessageModel(BaseModel):
    """页面脚本上报的单条会话消息（wire 子集——DOM 采集面产物）。

    字段全部来自 ``douyin_scripts`` 的采集脚本返回值；平台的真实 DOM 结构
    随时间变化，脚本按「候选选择器 + ``data-*`` 属性 + 文本哈希兜底」提取，
    因此本模型对缺省值宽容（``extra="ignore"`` 容忍脚本追加字段）。

    Attributes:
        message_id: 消息标识（``data-message-id`` / ``data-id``；缺失时脚本
            以「发送者 + 时间 + 文本」哈希兜底——去重键与入站 message_id）。
        sender_id: 发送者标识（``data-sender-id`` / ``data-uid``；缺失为
            ``unknown``——不会与主人格来源分级冲突，私聊一律 owner）。
        text: 消息文本（空白已由脚本裁剪）。
        ts_ms: 消息时间（epoch 毫秒；脚本把秒级时间戳归一，缺失为 0——
            归一函数按采集时刻兜底）。
        is_self: 是否本账号发出的消息（脚本按自身气泡标记判定；True 的
            消息不进入路由——自己的回复回声无需再喂给模型）。
        is_system: 是否系统消息（平台提示 / 助手播报——归一为
            ``synthetic=True`` 的机器事件面）。
        is_group: 是否群会话（归一为 ``is_group_chat``——群聊按 peer 落档）。
        image_urls: 消息内图片地址（``http(s)`` 源；``data:`` 内联图由脚本
            过滤——通道附件只承载可回取的 URL）。
    """

    model_config = {"extra": "ignore"}

    message_id: str = Field(default="", description="消息标识（去重键 + 入站 message_id）")
    sender_id: str = Field(default="unknown", description="发送者标识（缺失为 unknown）")
    text: str = Field(default="", description="消息文本（脚本已裁剪空白）")
    ts_ms: int = Field(default=0, ge=0, description="消息时间（epoch 毫秒；0 = 未报告）")
    is_self: bool = Field(default=False, description="是否本账号发出（不进路由）")
    is_system: bool = Field(default=False, description="是否系统消息（归一为 synthetic）")
    is_group: bool = Field(default=False, description="是否群会话（归一为 is_group_chat）")
    image_urls: list[str] = Field(default_factory=list, description="消息内图片地址")


class DouyinChatSnapshotModel(BaseModel):
    """会话快照（页面脚本一次采集的完整返回——客户端解析模型）。

    Attributes:
        conversation_id: 页面报告的会话标识（空串 = 页面未报告——归一函数
            按配置兜底）。
        messages: 采集到的消息（脚本已按时间序裁剪到请求上限）。
    """

    model_config = {"extra": "ignore"}

    conversation_id: str = Field(default="", description="页面报告的会话标识（空 = 未报告）")
    messages: list[DouyinWebMessageModel] = Field(
        default_factory=list, description="采集到的消息（时间序）"
    )


def build_douyin_chat_id(conversation_id: str) -> str:
    """构造通道会话键（``im:<conversation_id>``）。

    Args:
        conversation_id: 页面 / 配置提供的会话标识（空串按兜底标识处理）。

    Returns:
        通道出站 / 入站共用的 chat_id 字符串。
    """
    normalized = conversation_id.strip() or DOUYIN_DEFAULT_CONVERSATION_ID
    return f"{DOUYIN_CHAT_ID_PREFIX}{normalized}"


def parse_douyin_chat_id(chat_id: str) -> str:
    """解析通道会话键为平台会话标识（出站 grammar 校验）。

    Args:
        chat_id: 通道中立出站消息的 chat_id。

    Returns:
        平台会话标识（``im:`` 之后的原值）。

    Raises:
        BusinessException: 前缀缺失 / 后缀为空（``VALIDATION_ERROR``——
            调用方 bug 面，不进重试）。入站方向由本层产出合法键，
            因此该异常只可能来自出站构造面。
    """
    if not chat_id.startswith(DOUYIN_CHAT_ID_PREFIX):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"抖音通道 chat_id 必须以 {DOUYIN_CHAT_ID_PREFIX!r} 开头: {chat_id!r}",
        )
    conversation_id = chat_id[len(DOUYIN_CHAT_ID_PREFIX) :].strip()
    if not conversation_id:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"抖音通道 chat_id 缺少会话标识: {chat_id!r}",
        )
    return conversation_id


def _image_attachments(image_urls: list[str]) -> list[ChannelMessageAttachment]:
    """把页面报告的图片地址转为通道附件（仅 http(s) 源——附件恰一源校验）。

    Args:
        image_urls: 脚本报告的图片地址列表。

    Returns:
        图片附件列表（非 http(s) 地址跳过——``data:`` 内联图不承载）。
    """
    attachments: list[ChannelMessageAttachment] = []
    for url in image_urls:
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            continue
        attachments.append(
            ChannelMessageAttachment(kind=ChannelAttachmentKind.IMAGE, url=cleaned)
        )
    return attachments


class DouyinNormalizeParamsModel(BaseModel):
    """归一入参（内部辅助模型——避免裸 dict 传参）。

    Attributes:
        chat_id: 目标会话键（入站 / 出站同形态）。
        known_message_ids: 已处理消息标识集合（去重面）。
        max_messages: 单次归一的消息上限（取时间序末尾——最新优先保留）。
        now_ms: 采集时刻（缺失时间的兜底值；由调用方注入）。
    """

    chat_id: str = Field(min_length=1, description="目标会话键")
    known_message_ids: frozenset[str] = Field(
        default_factory=frozenset, description="已处理消息标识集合"
    )
    max_messages: int = Field(default=20, ge=1, le=100, description="单次归一消息上限")
    now_ms: int = Field(ge=0, description="采集时刻（缺失时间兜底——epoch 毫秒）")

    @model_validator(mode="after")
    def _require_chat_id(self) -> Self:
        """校验会话键非空白（空键会让路由器全部落同一会话）。

        Returns:
            校验通过的自身。

        Raises:
            ValueError: chat_id 为纯空白。
        """
        if not self.chat_id.strip():
            raise ValueError("chat_id 不得为空白")
        return self


def map_douyin_snapshot_to_inbound(
    snapshot: DouyinChatSnapshotModel,
    *,
    params: DouyinNormalizeParamsModel,
) -> list[InboundMessageModel]:
    """把会话快照归一为入站消息列表（去重 / 自身与系统消息语义）。

    过滤顺序（先剔除再截断，避免上限被无效条目占满）：自身回声
    （``is_self``）→ 无标识（无法去重）→ 已处理标识（``known_message_ids``）
    → 文本与图片皆空；同批次内重复标识保留首次出现。随后按时间序取末尾
    ``max_messages`` 条（DOM 时间序，最新消息优先保留）。

    系统消息保留为 ``synthetic=True`` 入站（心流门控据此不回复），不计入
    人类回合。

    Args:
        snapshot: 页面采集快照。
        params: 归一入参（会话键 / 去重集合 / 上限 / 采集时刻）。

    Returns:
        通道中立入站消息列表（时间序）。
    """
    selected: list[DouyinWebMessageModel] = []
    seen: set[str] = set()
    for message in snapshot.messages:
        message_id = message.message_id.strip()
        if not message_id or message_id in seen:
            continue
        if message.is_self or message_id in params.known_message_ids:
            continue
        attachments = _image_attachments(message.image_urls)
        text = message.text.strip() or (
            DOUYIN_IMAGE_PLACEHOLDER_TEXT if attachments else ""
        )
        if not text:
            continue
        seen.add(message_id)
        selected.append(
            message.model_copy(update={"message_id": message_id, "text": text})
        )
    bounded = selected[-params.max_messages :]
    return [
        InboundMessageModel(
            channel=ChannelType.DOUYIN,
            chat_id=params.chat_id,
            message_id=message.message_id,
            sender_id=message.sender_id.strip() or "unknown",
            text=message.text,
            ts_ms=message.ts_ms or params.now_ms,
            attachments=_image_attachments(message.image_urls),
            synthetic=message.is_system,
            silent=False,
            is_group_chat=message.is_group,
        )
        for message in bounded
    ]
