"""ASGI 原始请求替身——有界请求体读取测试（伪 stream 逐条计数消费）。

端点直调形态绕过 httpx 传输层：``ReceiveRecorder`` 逐条投放预置 ASGI
消息并记录被索取次数，"超限即停"用例据此断言后续块未被读取。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.requests import Request


class ReceiveRecorder:
    """ASGI receive 记录器（逐条投放预置消息并计数）。"""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        """初始化记录器。

        Args:
            messages: 预置 ASGI 消息序列（按被索取顺序投放）。
        """
        self._messages = messages
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        """投放下一条 ASGI 消息并累计索取次数。"""
        message = self._messages[self.calls]
        self.calls += 1
        return message


def chunked_messages(chunks: list[bytes]) -> list[dict[str, Any]]:
    """把字节块序列转换为 ASGI ``http.request`` 消息序列。

    Args:
        chunks: 逐块请求体（最后一块 ``more_body=False``）。

    Returns:
        ASGI 消息列表（可交给 :class:`ReceiveRecorder`）。
    """
    return [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]


def raw_request(
    *,
    receive: Any,
    headers: list[tuple[bytes, bytes]],
    path: str,
    app: FastAPI | None = None,
) -> Request:
    """手工构造 ASGI 请求（body 消费面可记录——不经过传输层）。

    Args:
        receive: ASGI receive 可调用对象。
        headers: 原始请求头字节对。
        path: 请求路径。
        app: 可选 FastAPI 应用（写入 ASGI scope 供端点读取 state）。

    Returns:
        Starlette 请求对象。
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("test", 12345),
        "server": ("test", 80),
    }
    if app is not None:
        scope["app"] = app
    return Request(scope, receive)
