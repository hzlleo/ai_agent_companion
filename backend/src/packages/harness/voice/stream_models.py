"""voice 域语音流式通道的传输面契约（injected transport——零真实网络依赖）。

设计决策（锁定）：

    - **传输注入**：流式 ASR / TTS 客户端只依赖本模块的
      :class:`VoiceStreamTransportFace`，真实 WebSocket 实现见
      :mod:`packages.harness.voice.stream_transport`（仅 app 装配层 / 探测
      脚本消费）。测试用内存 fake 传输即可覆盖全部协议与状态机，客户端
      模块导入期不触达任何网络。
    - **帧模型而非裸 dict**：传输面载荷统一为
      :class:`VoiceStreamFrameModel`（文本 / 二进制判别联合），客户端把
      文本帧 JSON 解析为各自的事件模型后再分派——公开面零裸 dict。
    - **关闭码语义**（RFC 6455 子集，客户端主动关闭时使用）：
      ``1000`` 正常收尾（会话完成 / 客户端取消后干净下线）、``1001``
      对端离开（服务重启 / 会话迁移）、``1002`` 协议错误（事件形态不符
      契约）、``1008`` 策略违规（鉴权 / 参数被服务端拒绝）、``1011``
      服务端内部错误。客户端收到对端 ``1002`` 及以上码即按失败处理并
      上抛可回退的业务异常（调用方回退批量主链）。

传输实现契约（部署侧 / 测试 fake 据此实现）:

    - ``send`` 发送一帧（文本帧承载 JSON 事件；二进制帧承载 PCM16 分片）。
    - ``receive`` 返回下一帧；对端正常关闭返回 ``None``（不抛异常——由
      客户端映射为「中途断流」）。
    - ``ping`` 发送协议层保活（WebSocket ping 控制帧；对端实现可忽略）。
    - ``close`` 幂等关闭（重复调用安全返回）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "STREAM_CLOSE_NORMAL",
    "STREAM_CLOSE_GOING_AWAY",
    "STREAM_CLOSE_PROTOCOL_ERROR",
    "STREAM_CLOSE_POLICY_VIOLATION",
    "STREAM_CLOSE_INTERNAL_ERROR",
    "VoiceStreamFrameModel",
    "VoiceStreamTransportFace",
    "VoiceStreamConnector",
    "build_text_frame",
    "build_binary_frame",
]

#: 正常收尾关闭码。
STREAM_CLOSE_NORMAL: Final[int] = 1000
#: 对端离开关闭码（服务重启 / 会话迁移）。
STREAM_CLOSE_GOING_AWAY: Final[int] = 1001
#: 协议错误关闭码（事件形态不符契约）。
STREAM_CLOSE_PROTOCOL_ERROR: Final[int] = 1002
#: 策略违规关闭码（鉴权 / 参数被服务端拒绝）。
STREAM_CLOSE_POLICY_VIOLATION: Final[int] = 1008
#: 服务端内部错误关闭码。
STREAM_CLOSE_INTERNAL_ERROR: Final[int] = 1011


class VoiceStreamFrameModel(BaseModel):
    """流式通道的传输帧（文本 / 二进制判别联合形态）。

    载荷二选一（``kind`` 为判别键）：文本帧只能携带 ``text``，二进制帧
    只能携带 ``data``——两者同时出现或同时缺失在构造期拒绝（契约破坏
    而非运行期分支）。

    Attributes:
        kind: 帧类型（``text`` / ``binary``）。
        text: 文本帧载荷（JSON 事件字符串；二进制帧为 None）。
        data: 二进制帧载荷（PCM16 分片；文本帧为 None）。
    """

    kind: Literal["text", "binary"] = Field(description="帧类型")
    text: str | None = Field(default=None, description="文本帧载荷（JSON 事件）")
    data: bytes | None = Field(default=None, description="二进制帧载荷（PCM16 分片）")

    @model_validator(mode="after")
    def _validate_payload(self) -> VoiceStreamFrameModel:
        """校验载荷与帧类型一致（二选一，不允许空帧）。

        Returns:
            校验通过的自身实例。

        Raises:
            ValueError: 文本帧缺 text / 携带 data，或二进制帧缺 data /
                携带 text。
        """
        if self.kind == "text":
            if self.text is None:
                raise ValueError("文本帧必须携带 text 载荷")
            if self.data is not None:
                raise ValueError("文本帧不得携带二进制 data 载荷")
            return self
        if self.data is None:
            raise ValueError("二进制帧必须携带 data 载荷")
        if self.text is not None:
            raise ValueError("二进制帧不得携带 text 载荷")
        return self


def build_text_frame(payload: str) -> VoiceStreamFrameModel:
    """构造文本帧（空载荷在构造期拒绝）。

    Args:
        payload: 已序列化的 JSON 事件字符串。

    Returns:
        文本帧模型。

    Raises:
        ValueError: 载荷为空串。
    """
    if not payload:
        raise ValueError("文本帧载荷不能为空")
    return VoiceStreamFrameModel(kind="text", text=payload)


def build_binary_frame(payload: bytes) -> VoiceStreamFrameModel:
    """构造二进制帧（空载荷在构造期拒绝）。

    Args:
        payload: 二进制分片（PCM16 字节）。

    Returns:
        二进制帧模型。

    Raises:
        ValueError: 载荷为空字节。
    """
    if not payload:
        raise ValueError("二进制帧载荷不能为空")
    return VoiceStreamFrameModel(kind="binary", data=payload)


class VoiceStreamTransportFace(Protocol):
    """流式通道传输面（生产 = WebSocket 适配；测试 = 内存双工 fake）。

    实现只承担「帧收发 + 保活 + 关闭」，不含任何协议语义——事件编解码与
    状态推进全部在客户端（可脱离网络确定性测试）。
    """

    async def send(self, frame: VoiceStreamFrameModel) -> None:
        """发送一帧。

        Args:
            frame: 待发送帧。

        Raises:
            异常: 传输自身失败（由客户端归一为业务异常）。
        """
        ...

    async def receive(self) -> VoiceStreamFrameModel | None:
        """接收下一帧（None = 对端已关闭）。

        Returns:
            下一帧；对端正常关闭返回 None。
        """
        ...

    async def ping(self) -> None:
        """发送协议层保活（实现可忽略对端应答）。"""
        ...

    @property
    def last_close_code(self) -> int | None:
        """最近一次对端关闭码（None = 尚未关闭）。

        Returns:
            RFC 6455 关闭码；对端以正常帧收尾（未升级为关闭）时为 None。
        """
        ...

    async def close(self, code: int, reason: str) -> None:
        """关闭传输（幂等——已关闭实现应安全返回）。

        Args:
            code: 关闭码（取值语义见模块 docstring）。
            reason: 关闭原因文本。
        """
        ...


#: 传输连接工厂契约——每次调用建立一条新连接（客户端按会话 / 分句新建）。
VoiceStreamConnector = Callable[[], Awaitable[VoiceStreamTransportFace]]
