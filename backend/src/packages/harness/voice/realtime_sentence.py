"""realtime voice 域句切——助手文本按句切分（标点 / 长度上限）。

用途：流式生成的助手文本经本模块切成「可独立合成的句子」，逐句送 TTS
并分片下行。切分语义（锁定）：

    - **句末标点即边界**：``。！？!?；;…`` 与换行视为句末；紧随其后的
      收尾符号（引号 / 括号 / 书名号）并入本句（保留标点语义，不把
      ``。”`` 拆散）；
    - **长度上限**：单句超过 ``max_chars`` 时优先在停顿标点（``，,、：:
      空格``）处切分，找不到合适停顿点则硬切——保证 TTS 单次请求不越界；
    - **空句不产出**：纯空白片段丢弃；行首空白不进入句首；
    - **流式缓冲**：:class:`SentenceAccumulator` 累积增量、产出完整句；
      未闭合的长句超过上限时按同一规则提前切出（避免长时间无音频）。

纯函数 + 无 I/O：同一输入序列在任何调用节奏下产出同一句列表。
"""

from __future__ import annotations

from packages.harness.voice.realtime_models import SentenceBufferModel

__all__ = [
    "SENTENCE_TERMINATORS",
    "split_assistant_sentences",
    "SentenceAccumulator",
]

#: 句末标点（含换行——人格回复的换行即语气停顿）。
SENTENCE_TERMINATORS: frozenset[str] = frozenset("。！？!?；;…\n")

#: 句末标点后允许并入本句的收尾符号。
_CLOSERS: frozenset[str] = frozenset("”’」』）)】》〉\"")

#: 长度超限时的优先切分点（停顿标点 / 空格）。
_PAUSE_MARKS: frozenset[str] = frozenset("，,、：: ")


def _preferred_cut_index(window: str) -> int | None:
    """在窗口内寻找优先切分点（停顿标点，位置须过半个窗口）。

    Args:
        window: 待切分文本（长度等于上限）。

    Returns:
        停顿标点之后的位置（可切分下标）；无合适停顿点返回 None。
    """
    minimum = max(1, len(window) // 2)
    for index in range(len(window) - 1, minimum - 1, -1):
        if window[index] in _PAUSE_MARKS:
            return index + 1
    return None


def _chunk_overlong(sentence: str, max_chars: int) -> list[str]:
    """把超长句切为不超过上限的片段（优先停顿点，兜底硬切）。

    Args:
        sentence: 待切文本（非空）。
        max_chars: 单句字符上限（>0）。

    Returns:
        片段列表（按序拼接等于原文；末段可能短于上限）。
    """
    pieces: list[str] = []
    remainder = sentence
    while len(remainder) > max_chars:
        window = remainder[:max_chars]
        cut = _preferred_cut_index(window)
        if cut is None:
            cut = max_chars
        piece = remainder[:cut].strip()
        if piece:
            pieces.append(piece)
        remainder = remainder[cut:]
    tail = remainder.strip()
    if tail:
        pieces.append(tail)
    return pieces


def split_assistant_sentences(text: str, *, max_chars: int) -> list[str]:
    """把整段助手文本切为可合成句子列表。

    Args:
        text: 助手回复全文。
        max_chars: 单句字符上限（>0）。

    Returns:
        句子列表（时间序；空输入返回空列表）。

    Raises:
        ValueError: ``max_chars`` 非正。
    """
    if max_chars <= 0:
        raise ValueError("单句字符上限必须为正")
    sentences: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        buffer.append(char)
        if char not in SENTENCE_TERMINATORS:
            index += 1
            continue
        index += 1
        while index < length and (text[index] in _CLOSERS or text[index] in SENTENCE_TERMINATORS):
            buffer.append(text[index])
            index += 1
        candidate = "".join(buffer).strip()
        buffer.clear()
        if candidate:
            sentences.extend(_chunk_overlong(candidate, max_chars))
    tail = "".join(buffer).strip()
    if tail:
        sentences.extend(_chunk_overlong(tail, max_chars))
    return sentences


class SentenceAccumulator:
    """流式句切累积器（增量喂入 → 完整句产出）。

    Args:
        max_chars: 单句字符上限（>0）。
    """

    def __init__(self, max_chars: int) -> None:
        if max_chars <= 0:
            raise ValueError("单句字符上限必须为正")
        self._max_chars = max_chars
        self._buffer = ""
        self._emitted = 0

    @property
    def state(self) -> SentenceBufferModel:
        """当前缓冲状态（诊断面）。"""
        return SentenceBufferModel(pending_chars=len(self._buffer), emitted_sentences=self._emitted)

    def feed(self, delta: str) -> list[str]:
        """喂入文本增量并产出新成句。

        Args:
            delta: 文本增量（空串直接返回空列表）。

        Returns:
            本次产出的句子列表（按序）。
        """
        if not delta:
            return []
        self._buffer += delta
        sentences: list[str] = []
        while True:
            boundary = self._find_boundary(self._buffer)
            if boundary is None:
                break
            head, self._buffer = self._buffer[:boundary], self._buffer[boundary:]
            sentence = head.strip()
            if sentence:
                sentences.extend(_chunk_overlong(sentence, self._max_chars))
        while len(self._buffer) > self._max_chars:
            window = self._buffer[: self._max_chars]
            cut = _preferred_cut_index(window)
            if cut is None:
                cut = self._max_chars
            piece = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:]
            if piece:
                sentences.append(piece)
        self._emitted += len(sentences)
        return sentences

    def flush(self) -> list[str]:
        """冲刷未闭合缓冲（生成结束——剩余文本作为末句产出）。

        Returns:
            剩余句列表（缓冲纯空白时为空）。
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        if not remainder:
            return []
        sentences = _chunk_overlong(remainder, self._max_chars)
        self._emitted += len(sentences)
        return sentences

    def _find_boundary(self, buffer: str) -> int | None:
        """定位最早句末边界（含紧随的收尾符号与重复句末标点）。

        Args:
            buffer: 当前缓冲文本。

        Returns:
            可切分下标（边界之后）；无边界返回 None。
        """
        for index, char in enumerate(buffer):
            if char not in SENTENCE_TERMINATORS:
                continue
            cursor = index + 1
            while cursor < len(buffer) and (
                buffer[cursor] in _CLOSERS or buffer[cursor] in SENTENCE_TERMINATORS
            ):
                cursor += 1
            return cursor
        return None
