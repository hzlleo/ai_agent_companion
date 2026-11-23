"""真实 WebSocket 传输测试——回环服务端（本机 127.0.0.1，零外网依赖）。

覆盖：文本帧 / 二进制帧双向透传、协议层保活、关闭（对端关闭码与 ``None``
返回语义）、连接被拒映射。回环监听端口由内核分配（``port=0``），不依赖
固定端口与外部网络。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from collections.abc import AsyncIterator

import pytest
from websockets.asyncio.server import ServerConnection, serve

from packages.harness.voice.stream_models import (
    STREAM_CLOSE_NORMAL,
    VoiceStreamFrameModel,
    build_binary_frame,
    build_text_frame,
)
from packages.harness.voice.stream_transport import make_websocket_connector


@pytest.fixture
async def echo_server() -> AsyncIterator[str]:
    """回环 echo 服务端点 fixture（文本 / 二进制原样回显）。

    Yields:
        ``ws://127.0.0.1:<port>/stream`` 端点。
    """

    async def _handler(connection: ServerConnection) -> None:
        async for message in connection:
            await connection.send(message)

    async with serve(_handler, "127.0.0.1", 0) as server:
        sockets = server.sockets
        assert sockets
        port = sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/stream"


async def test_text_and_binary_frames_round_trip(echo_server: str) -> None:
    """文本 / 二进制帧经真实 WebSocket 双向透传（字节级一致）。"""
    connector = make_websocket_connector(echo_server, open_timeout_seconds=2.0)
    transport = await connector()
    try:
        await transport.send(build_text_frame('{"type":"ping"}'))
        reply = await asyncio.wait_for(transport.receive(), timeout=2.0)
        assert isinstance(reply, VoiceStreamFrameModel)
        assert reply.kind == "text"
        assert reply.text == '{"type":"ping"}'

        await transport.send(build_binary_frame(b"\x01\x02\x03"))
        binary = await asyncio.wait_for(transport.receive(), timeout=2.0)
        assert isinstance(binary, VoiceStreamFrameModel)
        assert binary.kind == "binary"
        assert binary.data == b"\x01\x02\x03"
    finally:
        await transport.close(STREAM_CLOSE_NORMAL, "test_end")


async def test_ping_keeps_connection_usable(echo_server: str) -> None:
    """协议层保活不破坏数据通道（ping 后仍可收发）。"""
    connector = make_websocket_connector(echo_server, open_timeout_seconds=2.0)
    transport = await connector()
    try:
        await transport.ping()
        await transport.send(build_text_frame("after-ping"))
        reply = await asyncio.wait_for(transport.receive(), timeout=2.0)
        assert reply is not None
        assert reply.text == "after-ping"
    finally:
        await transport.close(STREAM_CLOSE_NORMAL, "test_end")


async def test_client_close_then_receive_returns_none(echo_server: str) -> None:
    """客户端关闭后 ``receive`` 返回 None（幂等关闭安全返回）。"""
    connector = make_websocket_connector(echo_server, open_timeout_seconds=2.0)
    transport = await connector()
    await transport.close(STREAM_CLOSE_NORMAL, "bye")
    await transport.close(STREAM_CLOSE_NORMAL, "bye_again")
    assert await transport.receive() is None
    await transport.ping()


async def test_server_close_surfaces_close_code() -> None:
    """对端关闭：``receive`` 返回 None 且关闭码可观测（语义面）。"""

    async def _handler(connection: ServerConnection) -> None:
        await connection.close(code=1001, reason="restart")

    async with serve(_handler, "127.0.0.1", 0) as server:
        sockets = server.sockets
        assert sockets
        port = sockets[0].getsockname()[1]
        connector = make_websocket_connector(
            f"ws://127.0.0.1:{port}/stream", open_timeout_seconds=2.0
        )
        transport = await connector()
        assert await asyncio.wait_for(transport.receive(), timeout=2.0) is None
        assert transport.last_close_code == 1001


#: RFC 6455 握手接受键的固定 GUID。
_WS_ACCEPT_GUID: str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@contextlib.asynccontextmanager
async def _silent_ws_peer() -> AsyncIterator[str]:
    """只完成 HTTP 升级、从不回关闭帧的对端（关闭握手超时观测面）。

    Yields:
        ``ws://127.0.0.1:<port>/stream`` 端点。
    """
    release = asyncio.Event()

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(
            hashlib.sha1(key + _WS_ACCEPT_GUID.encode("ascii")).digest()
        )
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        await writer.drain()
        try:
            await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    sockets = server.sockets
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}/stream"
    finally:
        release.set()
        server.close()
        await server.wait_closed()


async def test_close_is_bounded_when_peer_ignores_close_handshake() -> None:
    """对端不回关闭帧：close 在有界预算内返回（缺省 = min(open, 1s)）。"""
    async with _silent_ws_peer() as url:
        connector = make_websocket_connector(url, open_timeout_seconds=0.2)
        transport = await connector()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await transport.close(STREAM_CLOSE_NORMAL, "bye")
        assert loop.time() - started < 0.5
        assert await transport.receive() is None


async def test_close_timeout_is_injectable_for_tests() -> None:
    """关闭预算可注入更小值（不改变生产缺省上限）。"""
    async with _silent_ws_peer() as url:
        connector = make_websocket_connector(
            url, open_timeout_seconds=2.0, close_timeout_seconds=0.05
        )
        transport = await connector()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await transport.close(STREAM_CLOSE_NORMAL, "bye")
        assert loop.time() - started < 0.5


async def test_connection_refused_raises_os_error() -> None:
    """端口未监听：连接被拒上抛 OSError（客户端映射为业务异常）。"""
    connector = make_websocket_connector(
        "ws://127.0.0.1:1/stream", open_timeout_seconds=1.0
    )
    with pytest.raises(OSError):
        await connector()
