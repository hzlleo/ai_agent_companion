"""SmsForwarder 入站通道——手机端转发载荷的归一 / 去重窗口（Unit 文件）。

手机端 SmsForwarder 应用把短信 / 通知经 HTTP POST 转发到本后端；载荷形态
由手机端模板决定（常见字段 ``from`` / ``contact`` / ``body`` / ``content`` /
``message`` / ``timestamp`` / ``device`` / ``kind`` / ``sim`` / ``msgid``）。
本模块只做纯变换与过滤（无路由副作用、无队列）：

    载荷 JSON
        ↓ ① 容错解析（多余字段忽略、缺失字段给默认、非法载荷返回 None）
        ↓ ② kind 过滤（短信默认接收；notification 由开关决定）
        ↓ ③ 正文归一（空正文丢弃；发送者 / 时间戳 / 消息号归一）
        ↓ InboundMessageModel（channel=sms_forwarder、chat_id=sms:<sender>、
           synthetic=False、is_group_chat=False、is_owner 按属主清单命中）
        ↓ 去重键（msgid 优先；缺省按发送者 + 正文内容哈希，由有界时间窗
           吸收手机端重投）

来源分级：SmsForwarder 的发送者是第三方（手机号 / 联系名），默认按
``peer`` 落档；仅当发送者命中 ``owner_numbers``（属主号码 / 联系名清单）
时 ``is_owner=True``，路由器据此把该会话入站按主人来源处理。发送者本身
**不是人格 ID**——会话键只用 ``sms:<sender>``。
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Final, Protocol

from loguru import logger
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.core import time_utils

__all__ = [
    "SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS",
    "SmsForwarderDedupWindow",
    "SmsForwarderInboundRouterFace",
    "SmsForwarderKind",
    "SmsForwarderParsedMessageModel",
    "SmsForwarderPayloadModel",
    "build_sms_forwarder_dedup_key",
    "normalize_sms_forwarder_kind",
    "normalize_sms_sender",
    "parse_owner_numbers",
    "parse_sms_forwarder_payload",
    "process_sms_forwarder_payload",
    "reset_sms_forwarder_state",
    "resolve_sms_forwarder_timestamp_ms",
]

#: 无 msgid 时的内容去重窗口毫秒（同发送者 + 同正文在该窗口内只路由一次；
#: 窗口按到达时刻计算，吸收手机端重投 / 网络重试）。
SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS: Final[int] = 120_000

#: 去重窗口容量（已见键上限——超出按 FIFO 淘汰；进程重启清零）。
SMS_FORWARDER_DEDUP_WINDOW_SIZE: Final[int] = 1024

#: 手机号分隔符（空格 / 连字符 / 括号 / 点——展示形态归一为纯号码）。
_SENDER_SEPARATORS_RE: Final[re.Pattern[str]] = re.compile(r"[\s\-().]")

#: 秒级时间戳下界（低于该值视为非法——回落到服务端时钟）。
_EPOCH_SECONDS_MIN: Final[int] = 1_000_000_000

#: 毫秒级时间戳下界（达到该值按毫秒解释，避免二次放大）。
_EPOCH_MILLIS_MIN: Final[int] = 1_000_000_000_000


class SmsForwarderKind(StrEnum):
    """转发消息类型值域（短信 / 通知）。

    继承 ``StrEnum`` 保证与字符串比较兼容；手机端模板字段名为 ``kind``。
    """

    SMS = "sms"
    NOTIFICATION = "notification"


#: kind 字面量归一键（大小写不敏感；中文模板值兼容）。
_KIND_ALIASES: Final[dict[str, SmsForwarderKind]] = {
    "": SmsForwarderKind.SMS,
    "sms": SmsForwarderKind.SMS,
    "短信": SmsForwarderKind.SMS,
    "short_message": SmsForwarderKind.SMS,
    "notification": SmsForwarderKind.NOTIFICATION,
    "通知": SmsForwarderKind.NOTIFICATION,
    "notify": SmsForwarderKind.NOTIFICATION,
}

#: 正文候选字段归属顺序（手机端模板差异——首个非空者生效）。
_BODY_FIELDS: Final[tuple[str, ...]] = ("body", "content", "message", "text")


class SmsForwarderInboundRouterFace(Protocol):
    """SmsForwarder 入站路由面（生产 ChannelRouter；测试 fake 同形态）。"""

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """处理一条入站消息（调用方承担异常隔离）。

        Args:
            message: 通道中立入站消息。

        Returns:
            投递结果（直调路径不消费，仅保持路由器契约）。
        """
        ...


class SmsForwarderPayloadModel(BaseModel):
    """SmsForwarder 转发载荷（容错解析面——多余字段忽略）。

    手机端模板字段名不统一：发送者可能落在 ``from`` / ``sender``；正文可能
    落在 ``body`` / ``content`` / ``message`` / ``text``；消息号可能落在
    ``msgid`` / ``message_id`` / ``messageId``。本模型只承载原始值，归一
    规则由 :func:`parse_sms_forwarder_payload` 承担。

    Attributes:
        from_value: 发送者（别名 ``from``；手机号或联系名）。
        sender: 发送者候选（手机端模板使用 ``sender`` 键时的兜底）。
        contact: 联系名（发送者缺失时的兜底来源）。
        body: 正文候选（首选）。
        content: 正文候选。
        message: 正文候选。
        text: 正文候选。
        timestamp: 手机端时间戳（秒 / 毫秒数值或数值字符串；其余形态回落
            服务端时钟）。
        device: 设备标识（仅观测 / 日志，不参与路由）。
        kind: 消息类型（``SMS`` / ``notification`` / 中文别名；缺省按短信）。
        sim: SIM 卡标识（仅观测 / 日志，不参与路由）。
        msgid: 手机端消息号（去重首选键）。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    from_value: str = Field(
        default="",
        validation_alias=AliasChoices("from"),
        description="发送者（手机号 / 联系名）",
    )
    sender: str = Field(default="", description="发送者候选（sender 键模板）")
    contact: str = Field(default="", description="联系名（发送者兜底来源）")
    body: str = Field(default="", description="正文候选")
    content: str = Field(default="", description="正文候选")
    message: str = Field(default="", description="正文候选")
    text: str = Field(default="", description="正文候选")
    timestamp: int | float | str | None = Field(
        default=None, description="手机端时间戳（秒 / 毫秒数值；非法回落服务端时钟）"
    )
    device: str = Field(default="", description="设备标识（观测面）")
    kind: str = Field(default="", description="消息类型（SMS / notification）")
    sim: str = Field(default="", description="SIM 卡标识（观测面）")
    msgid: str = Field(
        default="",
        validation_alias=AliasChoices("msgid", "message_id", "messageId"),
        description="消息号（去重首选键）",
    )

    @field_validator(
        "from_value",
        "sender",
        "contact",
        "body",
        "content",
        "message",
        "text",
        "device",
        "kind",
        "sim",
        "msgid",
        mode="before",
    )
    @classmethod
    def _coerce_text_field(cls, value: object) -> object:
        """把手机端模板的空值 / 标量值归一为文本（缺失字段给默认）。

        Args:
            value: 原始字段值（``None`` / 字符串 / 数字 / 布尔）。

        Returns:
            ``None`` → 空串；数字 / 布尔 → 十进制字符串；其余原样返回。
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return value


class SmsForwarderParsedMessageModel(BaseModel):
    """解析结果（入站消息 + 去重键 + 去重窗口语义）。

    Attributes:
        inbound: 通道中立入站消息（可直接交给路由器）。
        dedup_key: 去重键（``msgid:<id>`` 或 ``content:<sha256>``）。
        dedup_ttl_ms: 去重有效期毫秒（None = 进程内 FIFO 常驻；内容键使用
            时间窗常量）。
    """

    inbound: InboundMessageModel = Field(description="通道中立入站消息")
    dedup_key: str = Field(min_length=1, description="去重键")
    dedup_ttl_ms: int | None = Field(
        default=None, description="去重有效期毫秒（None = 进程内 FIFO 常驻）"
    )


class SmsForwarderDedupWindow:
    """SmsForwarder 去重窗口（msgid 常驻 FIFO / 内容键时间窗）。

    手机端重投与网络重试会产生重复载荷：带 ``msgid`` 的载荷按消息号常驻
    吸收（容量上限内）；缺消息号的载荷按发送者 + 正文内容哈希在
    :data:`SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS` 窗口内吸收。窗口容量按
    FIFO 淘汰最旧键（与 QQ / 企微去重窗同容量语义），进程重启清零。
    """

    def __init__(self, cap: int = SMS_FORWARDER_DEDUP_WINDOW_SIZE) -> None:
        """初始化去重窗口。

        Args:
            cap: 窗口容量（默认 1024；须 ≥ 1）。

        Raises:
            ValueError: ``cap < 1``（零容量令新登记的键即刻淘汰——去重静默
                失效；入口拒绝）。
        """
        if cap < 1:
            raise ValueError("cap 须 ≥ 1")
        self.cap = cap
        self._expiry_by_key: dict[str, int | None] = {}
        self._order: deque[tuple[str, int | None]] = deque()

    def contains(self, key: str, now_ms: int) -> bool:
        """判断去重键是否仍在有效窗口内（只读——过期项就地清理）。

        Args:
            key: 去重键。
            now_ms: 当前时刻（epoch 毫秒）。

        Returns:
            True = 窗口内已登记且未过期。
        """
        if key not in self._expiry_by_key:
            return False
        expires_at = self._expiry_by_key[key]
        if expires_at is not None and now_ms >= expires_at:
            del self._expiry_by_key[key]
            return False
        return True

    def remember(self, key: str, now_ms: int, ttl_ms: int | None = None) -> bool:
        """登记去重键（容量超限按 FIFO 淘汰最旧）。

        Args:
            key: 去重键。
            now_ms: 当前时刻（epoch 毫秒）。
            ttl_ms: 有效期毫秒（None = FIFO 常驻——msgid 语义）。

        Returns:
            首见返回 True；窗口内重复返回 False。
        """
        if self.contains(key, now_ms):
            return False
        expires_at = None if ttl_ms is None else now_ms + max(0, ttl_ms)
        self._expiry_by_key[key] = expires_at
        self._order.append((key, expires_at))
        while len(self._order) > self.cap:
            old_key, old_expiry = self._order.popleft()
            if old_key in self._expiry_by_key and self._expiry_by_key[old_key] == old_expiry:
                del self._expiry_by_key[old_key]
        return True

    def __len__(self) -> int:
        """当前窗口内键数（观测 / 测试面）。"""
        return len(self._expiry_by_key)


def normalize_sms_forwarder_kind(raw: str) -> SmsForwarderKind | None:
    """归一 kind 字面量（大小写 / 中文别名不敏感）。

    Args:
        raw: 原始 kind 值（可能为空串）。

    Returns:
        归一后的消息类型；未知值返回 None（不可路由——确认丢弃）。
    """
    return _KIND_ALIASES.get(raw.strip().lower())


def normalize_sms_sender(raw: str) -> str:
    """归一发送者标识（手机号去分隔符 / 国家码，联系名小写）。

    规则（文档化决策）：

        - 去除空格 / 连字符 / 括号 / 点等展示分隔符；
        - 纯数字号码去掉 ``+86`` / ``0086`` / ``86`` 前缀（中国手机号
          11 位归一）——手机端模板可能带国际码，属主清单与发送者两侧
          使用同一归一键；
        - 其余（联系名）小写化，便于属主清单大小写不敏感比较。

    Args:
        raw: 原始发送者（手机号 / 联系名）。

    Returns:
        归一后的发送者键；空串表示无发送者。
    """
    cleaned = _SENDER_SEPARATORS_RE.sub("", raw.strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if digits.isdigit():
            if len(digits) == 13 and digits.startswith("86"):
                return digits[2:]
            return digits
        return cleaned.lower()
    if cleaned.isdigit():
        if len(cleaned) == 15 and cleaned.startswith("0086"):
            return cleaned[4:]
        if len(cleaned) == 13 and cleaned.startswith("86"):
            return cleaned[2:]
        return cleaned
    return cleaned.lower()


def parse_owner_numbers(raw: str) -> frozenset[str]:
    """解析属主号码 / 联系名清单（逗号分隔；空串 = 空集）。

    Args:
        raw: 配置原值（``SMS_FORWARDER_OWNER_NUMBERS``）。

    Returns:
        归一后的属主键集合（沿用 :func:`normalize_sms_sender` 的归一口径）。
    """
    owners: set[str] = set()
    for item in raw.split(","):
        normalized = normalize_sms_sender(item)
        if normalized:
            owners.add(normalized)
    return frozenset(owners)


def resolve_sms_forwarder_timestamp_ms(
    value: int | float | str | None, *, fallback_ms: int
) -> int:
    """解析手机端时间戳为 epoch 毫秒（非法 / 缺失回落服务端时钟）。

    支持形态：毫秒数值、秒级数值（自动 ×1000）、数值字符串。ISO 文本 /
    负值 / 零值 / 过小数值视为非法（SmsForwarder 模板默认输出数值）。
    秒级下界用于区分秒与毫秒（``1e9`` 秒约 2001 年，``1e12`` 毫秒约
    2001 年）；低于秒级下界的数值不作为时间使用。

    Args:
        value: 原始时间戳（数值 / 数值字符串 / None）。
        fallback_ms: 非法或缺失时的回落时刻（服务端时钟 epoch 毫秒）。

    Returns:
        epoch 毫秒时间戳（恒正）。
    """
    if isinstance(value, bool):
        return fallback_ms
    if isinstance(value, (int, float)):
        candidate = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback_ms
        try:
            candidate = int(text)
        except ValueError:
            try:
                candidate = int(float(text))
            except ValueError:
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


def _resolve_body(payload: SmsForwarderPayloadModel) -> str:
    """按候选优先级取正文（首个非空白字段）。

    Args:
        payload: 容错解析后的载荷。

    Returns:
        去除首尾空白的正文；全空返回空串（调用方按空正文丢弃）。
    """
    for field_name in _BODY_FIELDS:
        candidate = str(getattr(payload, field_name)).strip()
        if candidate:
            return candidate
    return ""


def build_sms_forwarder_dedup_key(*, msgid: str, sender_key: str, text: str) -> str:
    """构造去重键（msgid 优先；缺省按发送者 + 正文内容哈希）。

    Args:
        msgid: 手机端消息号（空 = 内容模式）。
        sender_key: 归一后的发送者键。
        text: 归一后的正文。

    Returns:
        去重键（``msgid:<id>`` 或 ``content:<sha256>``）。
    """
    if msgid:
        return f"msgid:{msgid}"
    digest = hashlib.sha256(f"{sender_key}\x1f{text}".encode()).hexdigest()
    return f"content:{digest}"


def parse_sms_forwarder_payload(
    payload: dict[str, Any],
    *,
    notifications_enabled: bool,
    owner_numbers: frozenset[str],
    clock: Callable[[], int] = time_utils.now_ms,
) -> SmsForwarderParsedMessageModel | None:
    """解析单条 SmsForwarder 转发载荷（不可路由返回 None）。

    纯变换与过滤面：不做去重登记、不触路由。不可路由形态（载荷非法 /
    kind 未知 / 通知关闭 / 空正文）返回 None——调用方确认并忽略，且不
    登记去重（手机端重投仍有意义）。

    Args:
        payload: HTTP 层已保证为 JSON 对象的载荷。
        notifications_enabled: 是否接收 ``kind=notification``（False = 仅
            短信；通知确认丢弃）。
        owner_numbers: 属主号码 / 联系名归一集合（命中则 ``is_owner=True``）。
        clock: 服务端时钟（epoch 毫秒——时间戳缺失 / 非法时回落）。

    Returns:
        解析结果（入站消息 + 去重键 + 去重窗口）；不可路由返回 None。
    """
    try:
        parsed_payload = SmsForwarderPayloadModel.model_validate(payload)
    except ValidationError as exc:
        logger.warning(
            "SmsForwarder 载荷非法（隔离确认）: {}", str(exc)[:200]
        )
        return None
    kind = normalize_sms_forwarder_kind(parsed_payload.kind)
    if kind is None:
        logger.debug(
            "SmsForwarder 未知消息类型（确认并忽略）: kind={!r}", parsed_payload.kind
        )
        return None
    if kind is SmsForwarderKind.NOTIFICATION and not notifications_enabled:
        logger.debug("SmsForwarder 通知转发未启用（确认并忽略）: kind=notification")
        return None
    text = _resolve_body(parsed_payload)
    if not text:
        logger.debug(
            "SmsForwarder 空正文（跳过路由）: device={!r}, sim={!r}",
            parsed_payload.device,
            parsed_payload.sim,
        )
        return None
    sender_key = normalize_sms_sender(
        parsed_payload.from_value or parsed_payload.sender or parsed_payload.contact
    )
    if not sender_key:
        sender_key = "unknown"
    now_ms = clock()
    ts_ms = resolve_sms_forwarder_timestamp_ms(parsed_payload.timestamp, fallback_ms=now_ms)
    msgid = parsed_payload.msgid.strip()
    dedup_key = build_sms_forwarder_dedup_key(
        msgid=msgid, sender_key=sender_key, text=text
    )
    if msgid:
        message_id = msgid
        dedup_ttl_ms: int | None = None
    else:
        message_id = f"sms-{ts_ms}-{dedup_key.removeprefix('content:')[:12]}"
        dedup_ttl_ms = SMS_FORWARDER_CONTENT_DEDUP_WINDOW_MS
    inbound = InboundMessageModel(
        channel=ChannelType.SMS_FORWARDER,
        chat_id=f"sms:{sender_key}",
        message_id=message_id,
        sender_id=sender_key,
        text=text,
        ts_ms=ts_ms,
        synthetic=False,
        is_group_chat=False,
        is_owner=sender_key in owner_numbers,
    )
    return SmsForwarderParsedMessageModel(
        inbound=inbound, dedup_key=dedup_key, dedup_ttl_ms=dedup_ttl_ms
    )


#: 进程级去重窗口单例（webhook 直调路径与 runner 各自独立——本单例只服务
#: 未装配 runner 的嵌入形态；测试经 :func:`reset_sms_forwarder_state` 复位）。
_DEDUP_WINDOW = SmsForwarderDedupWindow()


def reset_sms_forwarder_state() -> None:
    """复位 webhook 直调路径的进程内去重窗口（测试隔离面）。"""
    global _DEDUP_WINDOW
    _DEDUP_WINDOW = SmsForwarderDedupWindow()


async def process_sms_forwarder_payload(
    payload: dict[str, Any],
    router: SmsForwarderInboundRouterFace,
    *,
    notifications_enabled: bool,
    owner_numbers: frozenset[str],
) -> None:
    """处理单条转发载荷（解析 → 去重 → 路由；恒不抛）。

    单事件直调路径（测试 / 未装配 runner 的嵌入形态）：生产端点经
    :class:`packages.harness.channels.sms_forwarder.sms_runner.
    SmsForwarderWebhookRunner` 的有界队列消纳，不在 HTTP 请求内等待路由。
    本函数保留「一次调用完成一条消息」的语义，与 runner 共享解析面与去重
    窗口类型（各自实例：直调路径用模块级窗口，runner 持独立窗口）。

    Args:
        payload: HTTP 层已保证为 JSON 对象的载荷。
        router: 通道路由器（入站消费面——单消息错误隔离由本函数兜底）。
        notifications_enabled: 是否接收通知转发。
        owner_numbers: 属主号码 / 联系名归一集合。
    """
    parsed = parse_sms_forwarder_payload(
        payload,
        notifications_enabled=notifications_enabled,
        owner_numbers=owner_numbers,
    )
    if parsed is None:
        return
    now_ms = time_utils.now_ms()
    if not _DEDUP_WINDOW.remember(parsed.dedup_key, now_ms, parsed.dedup_ttl_ms):
        logger.debug(
            "SmsForwarder 消息重复投递（去重跳过）: key={}", parsed.dedup_key
        )
        return
    try:
        await router.handle_inbound(parsed.inbound)
    except Exception:
        logger.opt(exception=True).error(
            "SmsForwarder 消息路由失败（隔离确认）: chat_id={}, message_id={}",
            parsed.inbound.chat_id,
            parsed.inbound.message_id,
        )
