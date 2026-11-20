"""qq 通道 webhook 处理器——OneBot v11 事件上报 → 通道路由器（恒不抛）。

入站链路（与 Telegram 拉取式长轮询相反的**推送式**形态）::

    NapCat（部署机，配置 HTTP POST 上报到本后端）
        ↓ POST /api/v1/channels/qq/webhook（app/api/channels_qq）
        ↓ token 校验（webhook_token_env——配置时恒时比较）
    process_webhook_event(payload, router)     ← 本模块
        ① Ob11EventModel 容错解析（非法载荷 = 确认并忽略——不进 HTTP 错误面）
        ② 大类过滤：仅 ``post_type=message`` 进路由；notice / request /
           meta_event 确认并忽略（debug 日志——HTTP 上报方对非 200 会
           重试，忽略类事件必须 200 确认防重投风暴）
        ③ message_id 去重（进程内有界 FIFO 窗口——tg_runner 同语义；
           NapCat 重启 / 网络重试会重投事件）
        ④ 消息展平（段数组 / CQ 码字符串双形态 → 纯文本；非文本段
           占位符化，见 ``_SEGMENT_PLACEHOLDERS``）
        ⑤ v2 附件提取（``extract_message_attachments``——仅段数组形态；
           image / record 段 → ``ChannelMessageAttachment``，NapCat
           提供的引用形态承载：``data.url`` → url 源、``data.file`` 的
           ``base64://`` 前缀 → base64 源、其余（``file://`` URL / 平台
           引用）→ path 源；CQ 码字符串形态维持占位符降级——附件提取
           边界文档化于 docs/backend/channels.md「协议 v2」）
        ⑥ InboundMessageModel 构造（chat_id grammar：``private:<user_id>``
           / ``group:<group_id>``；epoch 秒 ×1000 归一；attachments 搭载）
        ↓ ChannelRouter.handle_inbound（恒不抛——单消息错误隔离铁律）

仅附件入站（纯图片 / 纯表情，无用户字面文本）：占位符文本是人格可见面
的降级形态，不代表用户说了话——只要消息含非文本内容（CQ 码或非 text
段，两种 wire 形态一致），就归一为 ``text=""`` 投递（附件照常搭载，
路由器跳过生成并保留表情收录）；既无用户文本也无任何非文本内容时才
丢弃。

隔离铁律：**本处理器恒不抛异常**——任何单事件失败（载荷非法 / 字段
缺失 / 路由器内部异常）只进日志，向 HTTP 层返回确认语义（调用方
据此恒 200；OneBot 上报方对非 200 会重试，确认即防重投）。

去重状态：进程内模块级单例（QQ 通道无 runner 对象可挂载——与
tg_runner 实例态去重的差异点）；测试经 ``reset_qq_webhook_state``
逐测试复位。
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Final

from loguru import logger
from pydantic import ValidationError

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    InboundMessageModel,
)
from packages.harness.channels.channel_service import ChannelRouter
from packages.harness.channels.qq.qq_models import Ob11EventModel, Ob11MessageSegmentModel
from packages.harness.core.time_utils import now_ms

__all__ = [
    "QqMessageDedupWindow",
    "flatten_message_text",
    "extract_message_attachments",
    "parse_webhook_event",
    "process_webhook_event",
    "reset_qq_webhook_state",
]

#: 去重窗口容量（message_id 已见集合上限——超出按 FIFO 淘汰；tg_runner
#: 同语义同容量：只兜底「NapCat 重启 / 网络重试」窗口内的重投）。
_SEEN_MESSAGE_CAP: Final[int] = 1024

#: 非文本消息段 → 占位符映射表（常见 OneBot v11 段类型；人格可见文本
#: 面的非文本内容降级形态——文档化决策）。
_SEGMENT_PLACEHOLDERS: Final[dict[str, str]] = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "at": "[提及]",
    "reply": "[引用回复]",
    "share": "[分享]",
    "music": "[音乐]",
    "red_packet": "[红包]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "forward": "[合并转发]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
}

#: 未知段类型的兜底占位符（实现扩展段类型——信息保真降级）。
_UNKNOWN_PLACEHOLDER: Final[str] = "[其它消息]"

#: CQ 码形态（``[CQ:type,key=value,...]``——字符串形态消息的内嵌码）。
_CQ_CODE_RE: Final[re.Pattern[str]] = re.compile(r"\[CQ:(?P<type>[A-Za-z0-9_-]+)[^\]]*\]")


class QqMessageDedupWindow:
    """message_id 去重窗口（set + deque FIFO 淘汰——tg_runner 同语义）。

    Attributes:
        cap: 窗口容量（已见 ID 上限；超出按 FIFO 淘汰最旧）。
    """

    def __init__(self, cap: int = _SEEN_MESSAGE_CAP) -> None:
        """初始化去重窗口。

        Args:
            cap: 窗口容量（默认 1024；须 ≥ 1）。

        Raises:
            ValueError: ``cap < 1``（零容量会令新登记的 ID 即刻淘汰——
                去重静默失效；入口拒绝）。
        """
        if cap < 1:
            raise ValueError("cap 须 ≥ 1")
        self.cap = cap
        self._seen: set[int] = set()
        self._order: deque[int] = deque()

    def contains(self, message_id: int) -> bool:
        """判断 message_id 是否已在窗口内（只读——不登记）。

        Args:
            message_id: 消息 ID（OneBot v11 int 形态）。

        Returns:
            窗口内已登记返回 True。
        """
        return message_id in self._seen

    def remember(self, message_id: int) -> bool:
        """登记 message_id（窗口超容按 FIFO 淘汰最旧）。

        Args:
            message_id: 消息 ID（OneBot v11 int 形态——可为负值）。

        Returns:
            首见返回 True；重复返回 False。
        """
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._order.append(message_id)
        while len(self._order) > self.cap:
            self._seen.discard(self._order.popleft())
        return True

    def __len__(self) -> int:
        """当前窗口内 ID 数（观测面）。"""
        return len(self._seen)


#: 进程级去重窗口单例（QQ 通道无 runner 实例可挂载——模块级承载；
#: 测试经 :func:`reset_qq_webhook_state` 复位）。
_DEDUP_WINDOW = QqMessageDedupWindow()


def reset_qq_webhook_state() -> None:
    """复位 webhook 处理器进程内状态（测试隔离面——逐测试调用）。

    重建去重窗口单例（清空已见 message_id 集合）。
    """
    global _DEDUP_WINDOW
    _DEDUP_WINDOW = QqMessageDedupWindow()


def _placeholder_for(segment_type: str) -> str:
    """解析段类型占位符（text 类型归一空串——CQ 字符串内嵌形态）。

    Args:
        segment_type: 消息段类型（OneBot v11 段类型字符串）。

    Returns:
        占位符文本（映射表命中值；text → 空串；未知 → ``[其它消息]``）。
    """
    if segment_type == "text":
        return ""
    return _SEGMENT_PLACEHOLDERS.get(segment_type, _UNKNOWN_PLACEHOLDER)


def flatten_message_text(message: list[Ob11MessageSegmentModel] | str) -> str:
    """展平 OneBot v11 消息内容为纯文本（段数组 / CQ 码字符串双形态）。

    规则（文档化决策）：

        - 段数组形态：``text`` 段取 ``data.text`` 依次**无缝拼接**（多
          text 段是同一消息的连续切片——无缝拼接还原原文）；非文本段
          按占位符表降级（如 image → ``[图片]``）；
        - CQ 码字符串形态：码外的字面文本保留；``[CQ:type,...]`` 码按
          同一占位符表降级（``[CQ:text,...]`` 罕见——归一空串）。

    Args:
        message: OneBot v11 消息内容（段数组或 CQ 码字符串）。

    Returns:
        展平后的纯文本（非文本段占位符化；仅附件入站的空文本归一由
        :func:`process_webhook_event` 承担）。
    """
    if isinstance(message, str):
        return _CQ_CODE_RE.sub(lambda match: _placeholder_for(match.group("type")), message)
    parts: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
        else:
            parts.append(_placeholder_for(segment.type))
    return "".join(parts)


def _has_user_text(message: list[Ob11MessageSegmentModel] | str) -> bool:
    """消息是否携带可回复的用户字面文本（纯媒体为 False）。

    仅附件入站（纯图片 / 纯表情）归一为空文本投递：占位符文本只服务
    混合消息的人格可见面，不被误当作「用户说了话」。CQ 码字符串形态
    剥掉码后的字面文本参与判定。

    Args:
        message: OneBot v11 消息内容（段数组或 CQ 码字符串）。

    Returns:
        True = 存在非空白用户字面文本。
    """
    if isinstance(message, str):
        return bool(_CQ_CODE_RE.sub("", message).strip())
    return any(
        segment.type == "text" and str(segment.data.get("text", "")).strip()
        for segment in message
    )


def _has_non_text_content(message: list[Ob11MessageSegmentModel] | str) -> bool:
    """消息是否含非文本内容（CQ 码 / 非 text 段——纯媒体形态判定）。

    与 :func:`_has_user_text` 成对使用：无用户字面文本但含非文本内容时
    归一为 ``text=""`` 投递（占位符不冒充用户话语，路由器据此跳过生成）；
    两者皆无才是真正无可投递内容的消息。

    Args:
        message: OneBot v11 消息内容（段数组或 CQ 码字符串）。

    Returns:
        True = CQ 码字符串形态存在 ``[CQ:type,...]`` 码，或段数组形态
        存在 ``type != "text"`` 的段。
    """
    if isinstance(message, str):
        return _CQ_CODE_RE.search(message) is not None
    return any(segment.type != "text" for segment in message)


def _attachment_from_segment(segment: Ob11MessageSegmentModel) -> ChannelMessageAttachment | None:
    """单一媒体段 → 通道中立附件（无可用引用形态返回 None）。

    引用形态优先序（NapCat 提供形态——docs/backend/channels.md「协议
    v2」同源）：``data.url``（可下载 URL）→ url 源；``data.file`` 为
    ``base64://`` 前缀 → base64 源（载荷剥前缀）；其余 ``data.file``
    （``file://`` URL / 平台文件引用）→ path 源原样承载；两者皆缺 →
    None（无可用引用——该段不产附件）。

    Args:
        segment: 媒体段（image / record；其余类型返回 None）。

    Returns:
        附件（image 段 → image 类、record 段 → audio 类）；无可用引用
        或非媒体段返回 None。
    """
    if segment.type == "image":
        kind = ChannelAttachmentKind.IMAGE
    elif segment.type == "record":
        kind = ChannelAttachmentKind.AUDIO
    else:
        return None
    url = segment.data.get("url")
    file_value = segment.data.get("file")
    if isinstance(url, str) and url:
        return ChannelMessageAttachment(kind=kind, url=url)
    if isinstance(file_value, str) and file_value:
        if file_value.startswith("base64://"):
            return ChannelMessageAttachment(kind=kind, base64=file_value[len("base64://") :])
        return ChannelMessageAttachment(kind=kind, path=file_value)
    return None


def extract_message_attachments(
    message: list[Ob11MessageSegmentModel] | str,
) -> list[ChannelMessageAttachment]:
    """提取段数组消息的媒体附件（v2——CQ 码字符串形态维持占位符降级）。

    边界（文档化决策）：仅**段数组形态**提取（NapCat HTTP POST 上报
    缺省 message_format=array；CQ 码字符串形态的参数解析转义复杂度
    不抵增益——维持 v1 占位符降级，附件提取不生效）；无可用引用的
    媒体段（url / file 键皆缺）跳过。

    Args:
        message: OneBot v11 消息内容（段数组或 CQ 码字符串）。

    Returns:
        附件列表（段序保持；CQ 字符串 / 无媒体段 / 无引用形态为空）。
    """
    if isinstance(message, str):
        return []
    attachments: list[ChannelMessageAttachment] = []
    for segment in message:
        attachment = _attachment_from_segment(segment)
        if attachment is not None:
            attachments.append(attachment)
    return attachments


def parse_webhook_event(payload: dict[str, Any]) -> InboundMessageModel | None:
    """解析单条 webhook 事件为通道中立入站消息（不可路由返回 None）。

    解析面只做纯变换与过滤（无去重登记 / 无路由副作用）：去重与排队由
    调用方决定——webhook 端点经 :class:`~packages.harness.channels.qq.
    qq_runner.QqWebhookRunner` 登记去重并入有界队列；单事件直调路径
    :func:`process_webhook_event` 亦复用同一解析面。

    Args:
        payload: OneBot v11 事件上报载荷（JSON 对象——HTTP 层已保证形态）。

    Returns:
        可路由的入站消息；载荷非法 / 非消息大类 / 消息类型不支持 / 无用户
        文本且无非文本内容 / 会话目标缺失时返回 None（调用方确认并忽略，
        且不登记去重）。
    """
    try:
        event = Ob11EventModel.model_validate(payload)
    except ValidationError as exc:
        logger.warning("QQ webhook 载荷不是合法的 OneBot 事件（隔离确认）: {}", str(exc)[:200])
        return None
    if event.post_type != "message":
        logger.debug(
            "QQ webhook 非消息事件（确认并忽略）: post_type={!r}", event.post_type or "(缺失)"
        )
        return None
    if event.message_type not in ("private", "group"):
        logger.debug(
            "QQ webhook 消息类型不支持（确认并忽略）: message_type={!r}",
            event.message_type or "(缺失)",
        )
        return None
    has_non_text_content = _has_non_text_content(event.message)
    text = flatten_message_text(event.message)
    attachments = extract_message_attachments(event.message)
    if not _has_user_text(event.message) and has_non_text_content:
        # 占位文本不算用户文本：纯媒体（两种 wire 形态一致）以空文本
        # 投递——路由器跳过生成，无可提取附件时按 no-op 收录。
        text = ""
    if not text.strip() and not has_non_text_content:
        logger.debug(
            "QQ webhook 消息无用户文本且无非文本内容（跳过路由）: message_id={}",
            event.message_id,
        )
        return None
    if event.message_type == "group":
        if event.group_id is None or event.group_id < 1:
            logger.warning(
                "QQ 群消息缺少合法 group_id（隔离确认）: message_id={}", event.message_id
            )
            return None
        chat_id = f"group:{event.group_id}"
    else:
        if event.user_id < 1:
            logger.warning(
                "QQ 私聊消息缺少合法 user_id（隔离确认）: message_id={}", event.message_id
            )
            return None
        chat_id = f"private:{event.user_id}"
    sender_id = str(event.user_id) if event.user_id >= 1 else "0"
    ts_ms = event.time * 1000 if event.time > 0 else now_ms()
    return InboundMessageModel(
        channel=ChannelType.QQ,
        chat_id=chat_id,
        message_id=str(event.message_id),
        sender_id=sender_id,
        text=text,
        ts_ms=ts_ms,
        attachments=attachments,
        is_group_chat=chat_id.startswith("group:"),
    )


async def process_webhook_event(payload: dict[str, Any], router: ChannelRouter) -> None:
    """处理单条 webhook 事件（解析 → 去重 → 路由；恒不抛）。

    单事件直调路径（测试 / 嵌入式调用面）：生产端点经
    :class:`~packages.harness.channels.qq.qq_runner.QqWebhookRunner` 的
    有界队列消纳，不在 HTTP 请求内等待路由；本函数保留「一次调用完成
    一条事件」的语义，与 runner 共享解析面与去重窗口。

    Args:
        payload: OneBot v11 事件上报载荷（JSON 对象——HTTP 层已保证形态）。
        router: 通道路由器（入站消费面——``handle_inbound`` 恒不抛的
            契约由路由器承担；本函数再做一层兜底捕获）。
    """
    inbound = parse_webhook_event(payload)
    if inbound is None:
        return
    message_id = int(inbound.message_id)
    if not _DEDUP_WINDOW.remember(message_id):
        logger.debug("QQ webhook 消息重复投递（去重跳过）: message_id={}", message_id)
        return
    try:
        await router.handle_inbound(inbound)
    except Exception:
        logger.opt(exception=True).error(
            "QQ webhook 消息路由失败（隔离确认）: message_id={}", message_id
        )
