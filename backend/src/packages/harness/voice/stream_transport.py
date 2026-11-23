"""voice 域流式通道的真实 WebSocket 传输（websockets 依赖，仅装配层 / 探测脚本消费）。

本模块是 :mod:`packages.harness.voice.stream_models` 传输面协议的生产实现：
harness 客户端（ASR / TTS 流式）只依赖协议，不 import 本模块——网络栈
依赖因此被隔离在装配层（``app/api/realtime_voice.py``）与部署探测脚本
（``scripts/probe_voice_streaming.py``）内。

依赖口径（锁定决策）：``websockets`` 随 ``uvicorn[standard]``（FastAPI 服务
运行时）进入本工程运行时环境，``pixi.lock`` 已锁定版本；本模块直接使用，
**不新增工程依赖**。缺包时导入即失败（fail loud——部署环境不完整），
不设静默降级分支。

协议层行为：

    - 连接建立有打开超时（``open_timeout`` 与调用方 ``wait_for`` 双保险）；
      关闭握手有界（``close_timeout`` 缺省 = ``min(open_timeout, 1s)``，
      对端不回关闭帧时也在一秒量级内返回）；
    - ``websockets`` 协议异常（升级被拒 / 非法 URI / 连接已关闭等）统一
      归一为 ``OSError``——客户端既有「``OSError`` → ``LLM_CALL_FAILED``」
      映射据此生效；``receive`` 的对端关闭仍是 ``None`` 语义（不抛）；
    - 文本帧 / 二进制帧双向原样透传（不做任何 JSON 解析）；
    - 对端关闭（正常或异常）统一映射为 ``receive`` 返回 None——客户端据
      「是否已收到收尾事件」判定中途断流并回退批量主链；
    - ``ping`` 走 WebSocket 控制帧（控制帧不占用数据通道，对端无需回业务
      消息）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, cast

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from packages.harness.voice.stream_models import (
    VoiceStreamConnector,
    VoiceStreamFrameModel,
    VoiceStreamTransportFace,
    build_binary_frame,
    build_text_frame,
)

__all__ = ["WebSocketStreamTransport", "make_websocket_connector"]

#: 单帧上限（字节；None = 不限制——TTS 分片可能远大于 WebSocket 缺省 1 MiB）。
_MAX_FRAME_BYTES: Final[int | None] = None
#: 关闭握手缺省预算上限（秒——对端不回关闭帧时仍须有界返回）。
_DEFAULT_CLOSE_TIMEOUT_SECONDS: Final[float] = 1.0


def _close_code(exc: ConnectionClosed) -> int | None:
    """取关闭帧携带的 RFC 6455 关闭码（对端优先，其次本端）。

    Args:
        exc: websockets 关闭异常。

    Returns:
        关闭码；两端均无关闭帧载荷时为 None。
    """
    if exc.rcvd is not None:
        return exc.rcvd.code
    if exc.sent is not None:
        return exc.sent.code
    return None


class WebSocketStreamTransport:
    """``websockets`` 客户端连接 → 流式传输面适配。

    Args:
        connection: 已建立的 websockets 客户端连接（生命周期归本类）。
    """

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._closed = False
        self._last_close_code: int | None = None

    @property
    def last_close_code(self) -> int | None:
        """最近一次对端关闭码（None = 对端未以关闭帧终止）。

        Returns:
            RFC 6455 关闭码。
        """
        return self._last_close_code

    async def send(self, frame: VoiceStreamFrameModel) -> None:
        """发送一帧（文本帧发字符串 / 二进制帧发字节）。

        Args:
            frame: 待发送帧（载荷二选一已由模型校验）。

        Raises:
            OSError: 连接已关闭 / 协议异常（客户端归一为业务异常）。
        """
        try:
            if frame.kind == "text":
                await self._connection.send(cast("str", frame.text))
                return
            await self._connection.send(cast("bytes", frame.data))
        except WebSocketException as exc:
            if isinstance(exc, ConnectionClosed):
                self._closed = True
                self._last_close_code = _close_code(exc)
            raise OSError(str(exc)) from exc

    async def receive(self) -> VoiceStreamFrameModel | None:
        """接收下一帧（对端关闭返回 None）。

        Returns:
            文本 / 二进制帧；对端关闭或连接已关闭返回 None。
        """
        if self._closed:
            return None
        try:
            payload = await self._connection.recv()
        except ConnectionClosed as exc:
            self._closed = True
            self._last_close_code = _close_code(exc)
            return None
        if isinstance(payload, str):
            return build_text_frame(payload)
        return build_binary_frame(bytes(payload))

    async def ping(self) -> None:
        """发送 WebSocket ping 控制帧（对端无需回业务消息）。"""
        if self._closed:
            return
        try:
            await self._connection.ping()
        except ConnectionClosed as exc:
            self._closed = True
            self._last_close_code = _close_code(exc)

    async def close(self, code: int, reason: str) -> None:
        """关闭连接（幂等——已关闭直接返回）。

        Args:
            code: 关闭码。
            reason: 关闭原因文本（服务端日志面）。
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._connection.close(code=code, reason=reason)
        except ConnectionClosed:
            return


def make_websocket_connector(
    url: str, *, open_timeout_seconds: float, close_timeout_seconds: float | None = None
) -> VoiceStreamConnector:
    """构造绑定端点的连接工厂（每次调用建立一条新连接）。

    Args:
        url: WebSocket 端点（``ws://`` / ``wss://``——部署契约见各流式客户端
            模块 docstring）。
        open_timeout_seconds: 打开超时秒数（连接 + 升级握手）。
        close_timeout_seconds: 关闭握手预算秒数（None = ``min(open_timeout,
            1.0)``；测试可注入更小值）。

    Returns:
        可重复调用的异步连接工厂（返回已完成升级的传输面）。

    Raises:
        TimeoutError: 打开超时（由调用方映射为业务异常）。
        OSError: 连接被拒 / 升级被拒 / 协议异常 / 网络不可达。
    """
    close_timeout = (
        close_timeout_seconds
        if close_timeout_seconds is not None
        else min(open_timeout_seconds, _DEFAULT_CLOSE_TIMEOUT_SECONDS)
    )

    async def _connect() -> VoiceStreamTransportFace:
        """建立一条连接并包装为传输面（打开 / 关闭握手均硬约束）。"""
        pending: Any = connect(
            url,
            max_size=_MAX_FRAME_BYTES,
            open_timeout=open_timeout_seconds,
            close_timeout=close_timeout,
        )
        try:
            connection = cast(
                "ClientConnection",
                await asyncio.wait_for(pending, timeout=open_timeout_seconds),
            )
        except WebSocketException as exc:
            raise OSError(str(exc)) from exc
        return WebSocketStreamTransport(connection)

    return _connect
