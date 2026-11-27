"""GUI 微信通道会话水位——可见消息序列尾部重叠比对（视图重置语义）。

GUI 侧没有平台消息 ID，同一画面会在每次截图重复出现；判断「本轮哪些消息
是新消息」只能以**当前可见消息序列**相对上一帧的连续性为依据。本模块实现
纯逻辑（零 I/O / 零时钟 / 零配置读取）：给定上一帧与当前帧的消息指纹序列，
按尾部重叠定位「上一帧结束位置」，其后即新消息。

算法（锁定）：

1. 指纹 = ``(sender, is_self, text)``——包含 ``is_self`` 以免自身回声打断
   对齐；``ts_ms`` 不参与（视觉模型读出的时间不可靠，仅作观测字段）。
2. 取使 ``prev[-k:]`` 为 ``cur`` 连续子串的最大 ``k``（1 ≤ k ≤ min 长度）；
   在该 ``k`` 下取**最后一次出现**的结束位置 ``e``，新消息 = ``cur[e+1:]``。
    取最后一次出现是为了「向上滚动看历史」不被误判为新消息。代价（边界，
    文档化）：仅当重叠窗口内基线尾部整段为同一指纹（屏上只看得到这一条 /
    连发多条相同文本）时，新来的同指纹消息不投递（``[好]→[好,好]``、
    ``[好,好]→[好,好,好]``）；部分重叠仍投递新增（``[M1,好]→[M1,好,好]``
    投递一条）。上滚后滚回：无重叠按视图重置、其间新消息不投递；仍有重叠
    时，滚走期间位于屏底的已投递消息会再次入站（重复回复——已知代价）。
3. 无任何 ``k`` 匹配（首帧 / 目标首次出现 / 用户滚动超出可见范围）→
   视为视图重置：只更新基线、不投递。
4. 新增条数超过保留长度 → 视为视图重置（水位无法覆盖这么大跨度，重建
   基线而不倾泻一整屏）。
5. 基线只保留当前帧尾部 ``retention`` 条（``max_messages_per_poll``）。

两相接口（投递与水位更新顺序锁定）：:meth:`WechatGuiWatermark.diff` 只做
比对、不修改状态；调用方在本轮全部投递尝试之后调用
:meth:`WechatGuiWatermark.commit` 落基线——投递失败不回退水位（与逐消息
隔离口径一致，文档写明）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.harness.channels.wechat_gui.wechat_gui_models import WechatGuiMessageModel

__all__ = [
    "WechatGuiWatermark",
    "WechatGuiWatermarkAdvance",
]

#: 消息指纹（发送者 / 是否自身 / 正文——``ts_ms`` 明确不参与）。
_MessageFingerprint = tuple[str, bool, str]


def _fingerprint(message: WechatGuiMessageModel) -> _MessageFingerprint:
    """计算单条消息的对齐指纹（去首尾空白；正文与发送者按界面文本比较）。

    Args:
        message: 解析出的消息。

    Returns:
        对齐指纹元组。
    """
    return (message.sender.strip(), message.is_self, message.text.strip())


def _last_subsequence_end(
    haystack: Sequence[_MessageFingerprint], needle: Sequence[_MessageFingerprint]
) -> int:
    """查找 needle 在 haystack 中最后一次连续出现的结束下标。

    Args:
        haystack: 当前帧指纹序列。
        needle: 上一帧尾部指纹序列（长度 ≥ 1）。

    Returns:
        最后一次出现的结束下标（含）；未出现返回 -1。
    """
    size = len(needle)
    end = -1
    for start in range(len(haystack) - size + 1):
        if list(haystack[start : start + size]) == list(needle):
            end = start + size - 1
    return end


@dataclass(frozen=True)
class WechatGuiWatermarkAdvance:
    """一次水位比对的只读结果（提交前不得丢弃——承载待落基线）。

    Attributes:
        conversation_key: 会话备注名（水位分键）。
        new_messages: 本轮新增消息（当前帧顺序；可能为空）。
        baseline: 提交后应落库的基线指纹（当前帧尾部保留段）。
    """

    conversation_key: str
    new_messages: tuple[WechatGuiMessageModel, ...]
    baseline: tuple[_MessageFingerprint, ...]


class WechatGuiWatermark:
    """会话水位（分会话基线 + 尾部重叠比对——单实例进程内语义）。"""

    def __init__(self, *, retention: int) -> None:
        """初始化水位。

        Args:
            retention: 基线保留条数（= 单次解析消息上限；必须 ≥ 1）。

        Raises:
            ValueError: 保留条数小于 1。
        """
        if retention < 1:
            raise ValueError("retention 必须 >= 1")
        self._retention = retention
        self._baselines: dict[str, tuple[_MessageFingerprint, ...]] = {}

    @property
    def retention(self) -> int:
        """基线保留条数（观测面 / 文档引用）。"""
        return self._retention

    @property
    def tracked_targets(self) -> tuple[str, ...]:
        """已建立基线的会话键（观测面）。"""
        return tuple(self._baselines)

    def reset(self, conversation_key: str) -> None:
        """丢弃指定会话的基线（目标切走 / 停机复位）。

        Args:
            conversation_key: 会话备注名。
        """
        self._baselines.pop(conversation_key, None)

    def diff(
        self, conversation_key: str, messages: Sequence[WechatGuiMessageModel]
    ) -> WechatGuiWatermarkAdvance:
        """比对当前帧与基线，返回新增消息与待落基线（不修改状态）。

        Args:
            conversation_key: 会话备注名。
            messages: 当前可见消息序列（时间序；含自身回声）。

        Returns:
            只读比对结果；调用方在本轮投递尝试之后 :meth:`commit`。
        """
        current = tuple(_fingerprint(message) for message in messages)
        baseline_tail = current[-self._retention :]
        previous = self._baselines.get(conversation_key)
        if not previous or not current:
            return WechatGuiWatermarkAdvance(conversation_key, (), baseline_tail)
        max_k = 0
        best_end = -1
        for size in range(min(len(previous), len(current)), 0, -1):
            end = _last_subsequence_end(current, previous[-size:])
            if end >= 0:
                max_k = size
                best_end = end
                break
        if max_k == 0:
            return WechatGuiWatermarkAdvance(conversation_key, (), baseline_tail)
        new_messages = tuple(messages[best_end + 1 :])
        if len(new_messages) > self._retention:
            return WechatGuiWatermarkAdvance(conversation_key, (), baseline_tail)
        return WechatGuiWatermarkAdvance(
            conversation_key, new_messages, baseline_tail
        )

    def commit(self, advance: WechatGuiWatermarkAdvance) -> None:
        """按比对结果落基线（应在投递尝试全部结束之后调用）。

        Args:
            advance: :meth:`diff` 返回的比对结果。
        """
        self._baselines[advance.conversation_key] = advance.baseline
