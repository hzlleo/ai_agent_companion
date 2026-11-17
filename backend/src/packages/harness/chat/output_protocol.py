"""chat 域输出协议解析器（模型自述标签 → 可见正文 / 心声 / 情绪增量）。

协议语法（与 prompts.py 的 ``OUTPUT_PROTOCOL_TEMPLATE`` 指令文本同源，
文档化于 docs/backend/chat.md「输出协议」一节）::

    [<心声>一两句内心活动</心声>][<情绪>维度:+增量,维度:增量,...</情绪>]正文

解析语义（纯函数 Unit——无 I/O / 无日志输出副作用以外的状态）：

    - **只解析回复最前面的标签区**：``<心声>`` / ``<情绪>`` 两个标签
      均可选、次序容忍、各至多生效一次（同种标签重复出现时首个生效，
      后续重复标签连同其内容一并消费丢弃——debug 记录，不静默）；
    - **情绪维度**：按 :class:`~packages.harness.emotion.emotion_models.
      EmotionDimension` 值域校验；未知维度丢弃（debug 记录，绝不抛
      异常）；增量解析为 float 后夹取到 ``[-1, 1]``；同维度重复条目
      后者覆盖（确定性规则）；
    - **优雅降级**：标签不成对 / 已知开标签未闭合 / 标签区后无正文等
      不合规输出一律回落 ``visible_text = 原始文本``、``thought=None``、
      ``emotion_deltas={}``——解析器从不因模型不合规而抛异常；
    - **可见正文** = 标签区之后的剩余文本（strip）；纯标签回复得到空串
      （调用方按既有「模型返回空回复」语义处理）。

流式边界（``StreamTagStripper``——best-effort 前缀剥离）：

    - ``stream_reply`` 不跑完整协议（不做 M 态推进 / 不落 meta——流式
      通道是操作员可见面，边界文档化于 chat/service 模块文档）；
    - 剥离器只处理**流的起始前缀**：已完整出现的前导标签区被扣留并
      丢弃，判定为普通正文后一次性放行并转入直通；未知标签 / 未闭合
      标签原样放行（流结束时扣留区原样交还剩余内容——不吞文本）；
    - 剥离器不跨会话共享、不持有 I/O——纯状态机，单元测试直调。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger
from pydantic import BaseModel, Field

from packages.harness.emotion.emotion_models import EmotionDimension

__all__ = [
    "ParsedAssistantOutputModel",
    "StreamTagStripper",
    "parse_assistant_output",
    "strip_leading_protocol_prefix",
]

#: 心声标签对（开 / 闭）。
THOUGHT_TAG_OPEN: str = "<心声>"
THOUGHT_TAG_CLOSE: str = "</心声>"
#: 情绪标签对（开 / 闭）。
EMOTION_TAG_OPEN: str = "<情绪>"
EMOTION_TAG_CLOSE: str = "</情绪>"

#: 已知标签对表（扫描器唯一真源——新增标签只改此处）。
_KNOWN_TAGS: tuple[tuple[str, str], ...] = (
    (THOUGHT_TAG_OPEN, THOUGHT_TAG_CLOSE),
    (EMOTION_TAG_OPEN, EMOTION_TAG_CLOSE),
)

#: 情绪条目分隔符（半角 / 全角逗号、分号、空白）。
_EMOTION_ENTRY_SEPARATOR = re.compile(r"[,，;；\s]+")


class ParsedAssistantOutputModel(BaseModel):
    """输出协议解析结果（可见正文 + 内心独白 + 情绪增量）。

    Attributes:
        visible_text: 可见正文（标签区之后的剩余文本——strip 后的
            形态；发给通道 / TTS / 一切对方可见面）。
        thought: 内心独白（``<心声>`` 内容；未声明为 None——只进持久化
            meta 与会话详情，永不进入通道回复）。
        emotion_deltas: 情绪增量（维度值 → 增量；已按枚举校验并夹取到
            ``[-1, 1]``；未知维度已丢弃）。
    """

    visible_text: str = Field(description="可见正文（对方可见面）")
    thought: str | None = Field(default=None, description="内心独白（永不展示给对方）")
    emotion_deltas: dict[str, float] = Field(
        default_factory=dict, description="情绪增量（维度 → -1..1；未知维度已丢弃）"
    )


class _ScanStatus(Enum):
    """前导标签区扫描状态（解析 / 流式剥离共用的内部判定）。"""

    #: 标签区已结束（其后为正文或空）——可放行。
    COMPLETE = "complete"
    #: 缓冲区在标签区内部截断（可能还有后续标签 / 未闭合开标签）——等待更多输入。
    PENDING = "pending"


@dataclass
class _LeadingScan:
    """前导标签区扫描结果（内部结构——非对外契约）。

    Attributes:
        consumed: 已消费的字符数（标签区 + 其间空白）。
        status: 扫描状态（COMPLETE = 其后为正文；PENDING = 输入截断在
            标签区内）。
        thought: 首个 ``<心声>`` 内容（未出现为 None）。
        emotion_deltas: 情绪增量（已校验 / 夹取）。
        notes: 丢弃事件（重复标签 / 未知维度 / 非法增量——debug 记录原语）。
    """

    consumed: int
    status: _ScanStatus
    thought: str | None = None
    emotion_deltas: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _parse_emotion_payload(payload: str) -> tuple[dict[str, float], list[str]]:
    """解析 ``<情绪>`` 内容为增量字典（未知维度 / 非法数值丢弃并记录）。

    Args:
        payload: 标签内容（``joy:+0.5, shyness:0.2`` 形态）。

    Returns:
        ``(增量字典, 丢弃说明列表)`` 二元组；空 / 全非法内容返回空字典
        （调用方视为「有情绪标签但无有效条目」——不改变可见正文）。
    """
    deltas: dict[str, float] = {}
    notes: list[str] = []
    for entry in _EMOTION_ENTRY_SEPARATOR.split(payload.strip()):
        if not entry:
            continue
        # 半角 / 全角冒号均为合法分隔（模型输出常见混用）。
        name, sep, raw_value = entry.replace("：", ":").partition(":")
        if not sep or not name or not raw_value:
            notes.append(f"情绪条目形态非法（已忽略）: {entry!r}")
            continue
        try:
            dimension = EmotionDimension(name.strip())
        except ValueError:
            notes.append(f"未知情绪维度（已丢弃）: {name!r}")
            continue
        try:
            parsed = float(raw_value.strip())
        except ValueError:
            notes.append(f"情绪增量非数值（已忽略）: {entry!r}")
            continue
        clamped = max(-1.0, min(1.0, parsed))
        if clamped != parsed:
            notes.append(f"情绪增量越界（已夹取）: {entry!r} -> {clamped}")
        if dimension.value in deltas:
            notes.append(f"情绪维度重复条目（后者覆盖）: {dimension.value}")
        deltas[dimension.value] = clamped
    return deltas, notes


def _scan_leading_tags(text: str) -> _LeadingScan:
    """扫描前导标签区（解析器与流式剥离器共用的确定性单遍扫描）。

    扫描规则：跳过空白 → 命中已知开标签则要求配对闭标签（未配对即
    PENDING——full 解析面由调用方决定降级）→ 重复标签消费丢弃（首个
    生效）→ 命中未知 ``<`` 或普通字符即 COMPLETE。

    Args:
        text: 待扫描文本（完整回复 / 流式缓冲区）。

    Returns:
        扫描结果（消费长度 + 状态 + 标签内容 + 丢弃说明）。
    """
    cursor = 0
    result = _LeadingScan(consumed=0, status=_ScanStatus.COMPLETE)
    seen: set[str] = set()
    length = len(text)
    while True:
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            result.status = _ScanStatus.PENDING
            result.consumed = cursor
            return result
        matched = False
        for opener, closer in _KNOWN_TAGS:
            if not text.startswith(opener, cursor):
                continue
            matched = True
            close_index = text.find(closer, cursor + len(opener))
            if close_index < 0:
                result.status = _ScanStatus.PENDING
                result.consumed = cursor
                return result
            payload = text[cursor + len(opener) : close_index]
            if opener in seen:
                result.notes.append(f"重复标签（已消费丢弃）: {opener}{payload}{closer}")
            elif opener == THOUGHT_TAG_OPEN:
                seen.add(opener)
                stripped = payload.strip()
                result.thought = stripped if stripped else None
            else:
                seen.add(opener)
                deltas, notes = _parse_emotion_payload(payload)
                result.emotion_deltas = deltas
                result.notes.extend(notes)
            cursor = close_index + len(closer)
            break
        if matched:
            continue
        rest = text[cursor:]
        if rest.startswith("<") and any(
            opener.startswith(rest) for opener, _ in _KNOWN_TAGS
        ):
            # 缓冲区截断在已知开标签的中途（如 ``<心``）——等待更多输入。
            result.status = _ScanStatus.PENDING
            result.consumed = cursor
            return result
        result.status = _ScanStatus.COMPLETE
        result.consumed = cursor
        return result


def _log_scan_notes(notes: list[str]) -> None:
    """把扫描丢弃说明写入 debug 日志（协议宽容面——不抛异常）。

    Args:
        notes: 丢弃说明列表（空列表零输出）。
    """
    for note in notes:
        logger.debug("输出协议解析提示: {}", note)


def parse_assistant_output(raw_text: str) -> ParsedAssistantOutputModel:
    """解析模型原始输出（前导标签区 → 可见正文 / 心声 / 情绪增量）。

    优雅降级断言：标签不成对 / 已知开标签未闭合 / 输入截断在标签区
    内部且残留非空白内容时，整体回落原文（``thought=None``、
    ``emotion_deltas={}``）——绝不抛异常。

    Args:
        raw_text: 模型原始回复文本。

    Returns:
        解析结果（可见正文剥离前导标签区；不合规输出原样可见）。
    """
    scan = _scan_leading_tags(raw_text)
    _log_scan_notes(scan.notes)
    remainder = raw_text[scan.consumed :]
    if scan.status is _ScanStatus.PENDING and remainder.strip():
        # 未闭合开标签 / 截断在标签区内部：整体降级（原文可见）。
        return ParsedAssistantOutputModel(visible_text=raw_text.strip())
    return ParsedAssistantOutputModel(
        visible_text=remainder.strip(),
        thought=scan.thought,
        emotion_deltas=dict(scan.emotion_deltas),
    )


def strip_leading_protocol_prefix(text: str) -> str:
    """一次性剥离文本前导标签区（非流式直调面——best-effort）。

    与 :func:`parse_assistant_output` 同扫描语义；尾部未闭合标签区
    原样保留（不吞文本）。

    Args:
        text: 完整文本。

    Returns:
        剥离前导标签区后的文本（strip）；标签区未闭合时为原文本 strip。
    """
    scan = _scan_leading_tags(text)
    remainder = text[scan.consumed :]
    return remainder.strip()


class StreamTagStripper:
    """流式前缀标签剥离器（best-effort——只处理流起始标签区）。

    状态机语义：缓冲区在标签区判定未完成前扣留输出；判定为普通正文
    （或未知标签）后**一次性放行剩余缓冲并转入直通**（后续块零改写）；
    流结束（:meth:`flush`）时扣留区按 best-effort 交还（未闭合标签的
    残余文本原样输出——绝不静默吞字）。实例不跨流复用。
    """

    def __init__(self) -> None:
        """初始化剥离器（空缓冲、非直通态）。"""
        self._buffer: str = ""
        self._passthrough: bool = False

    def feed(self, chunk: str) -> str:
        """喂入流式增量块，返回可放行文本（可能为空串——扣留中）。

        Args:
            chunk: 网关增量文本块。

        Returns:
            当前可安全放行的文本（空串 = 前缀判定未完成）。
        """
        if self._passthrough:
            return chunk
        if not chunk:
            return ""
        self._buffer += chunk
        scan = _scan_leading_tags(self._buffer)
        if scan.status is _ScanStatus.PENDING:
            return ""
        _log_scan_notes(scan.notes)
        emitted = self._buffer[scan.consumed :]
        self._buffer = ""
        self._passthrough = True
        return emitted

    def flush(self) -> str:
        """流结束：交还剩余扣留文本（best-effort 前缀剥离）。

        Returns:
            尚扣留的文本（已消费标签区不交还；未闭合的已知开标签本身
            剥离、其后内容原样交还——绝不静默吞字，也不向操作员泄漏
            半截标签标记）。
        """
        if self._passthrough or not self._buffer:
            self._buffer = ""
            return ""
        scan = _scan_leading_tags(self._buffer)
        _log_scan_notes(scan.notes)
        remainder = self._buffer[scan.consumed :]
        for opener, _ in _KNOWN_TAGS:
            if remainder.startswith(opener):
                remainder = remainder[len(opener) :]
                break
        self._buffer = ""
        self._passthrough = True
        return remainder
