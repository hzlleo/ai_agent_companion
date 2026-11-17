"""telegram 通道客户端测试——Bot API 手写客户端（MockTransport 零真实网络）。

覆盖矩阵：

    - 构造期 token 解析（env 间接 + 缺失 fail fast）；
    - getUpdates 长轮询请求形状（timeout 参数 / offset 游标 / 请求超时
      = 轮询超时 + 裕量）+ 空结果形态；
    - sendMessage 快乐路径（body 形状 / 引用回复）+ 4096 分段
      （换行边界 / 硬切 / 引用只挂首段）；
    - getMe 快乐路径与失败映射；
    - 错误映射表：401/403 → CHANNEL_TOKEN_MISSING；非 200 / ok=False /
      传输异常 / JSON 非法 → CHANNEL_API_ERROR；空文本 / 非数字引用 ID
      → VALIDATION_ERROR。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from packages.harness.channels.telegram.tg_client import (
    TG_MESSAGE_MAX_CHARS,
    TelegramClient,
    split_tg_message_text,
)
from packages.harness.core.errors import BusinessException, ErrorCode

#: conftest 硬覆盖的测试专用 token 变量（bot_token_env 缺省指向形态）。
_TEST_TOKEN_ENV = "TELEGRAM_TEST_BOT_TOKEN"


def _ok(result: Any) -> httpx.Response:
    """构造 ok=True 的 Bot API 响应。

    Args:
        result: result 载荷。

    Returns:
        200 JSON 响应。
    """
    return httpx.Response(200, json={"ok": True, "result": result})


def _update(update_id: int, *, text: str | None = "你好", chat_id: int = 10086) -> dict[str, Any]:
    """构造单条 message update 的 wire dict。

    Args:
        update_id: 更新 ID。
        text: 消息文本（None = 非文本消息形态）。
        chat_id: 会话 ID。

    Returns:
        update wire dict。
    """
    message: dict[str, Any] = {
        "message_id": update_id * 10,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 7, "is_bot": False},
        "date": 1731000000,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _client(handler: Any, *, poll_timeout_seconds: int = 25) -> TelegramClient:
    """构造测试客户端（MockTransport + 测试 token env）。

    Args:
        handler: MockTransport 处理器。
        poll_timeout_seconds: 长轮询超时（缺省 25）。

    Returns:
        TelegramClient 实例。
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient(
        http,
        bot_token_env=_TEST_TOKEN_ENV,
        poll_timeout_seconds=poll_timeout_seconds,
    )


class TestConstruction:
    async def test_token_missing_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """构造期 token 缺失：CHANNEL_TOKEN_MISSING + 中文指引（变量名入消息）。"""
        monkeypatch.delenv(_TEST_TOKEN_ENV, raising=False)
        with pytest.raises(BusinessException) as exc_info:
            _client(lambda request: _ok([]))
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
        assert _TEST_TOKEN_ENV in exc_info.value.message
        assert "TELEGRAM_ENABLED" in exc_info.value.message

    async def test_token_whitespace_only_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """构造期 token 纯空白：等同缺失（strip 归一后判空）。"""
        monkeypatch.setenv(_TEST_TOKEN_ENV, "   ")
        with pytest.raises(BusinessException) as exc_info:
            _client(lambda request: _ok([]))
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_method_url_shape(self) -> None:
        """方法 URL：``<api_base>/bot<token>/<method>``（token 在路径段）。"""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return _ok([])

        client = _client(handler)
        await client.get_updates()
        token = os.environ.get(_TEST_TOKEN_ENV, "")
        assert captured["url"] == f"https://api.telegram.org/bot{token}/getUpdates?timeout=25"


class TestGetUpdates:
    async def test_request_shape_timeout_and_offset(self) -> None:
        """长轮询请求形状：timeout 参数 + offset 游标 + 请求超时含裕量。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            captured["timeout"] = request.extensions.get("timeout")
            return _ok([_update(5), _update(6)])

        client = _client(handler, poll_timeout_seconds=25)
        updates = await client.get_updates(offset=6)
        assert captured["params"] == {"timeout": "25", "offset": "6"}
        assert captured["timeout"]["read"] == pytest.approx(25 + 5)
        assert [update.update_id for update in updates] == [5, 6]
        first = updates[0]
        assert first.message is not None
        assert first.message.from_user is not None
        assert first.message.from_user.id == 7
        assert first.message.text == "你好"

    async def test_first_poll_has_no_offset_param(self) -> None:
        """首次拉取（offset=None）：不携带 offset 查询参数。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return _ok([])

        client = _client(handler)
        assert await client.get_updates() == []
        assert captured["params"] == {"timeout": "25"}

    async def test_wire_from_alias_parses(self) -> None:
        """wire ``from`` 关键字字段经别名解析为 from_user。"""
        client = _client(lambda request: _ok([_update(1)]))
        updates = await client.get_updates()
        assert updates[0].message is not None
        assert updates[0].message.from_user is not None
        assert updates[0].message.from_user.id == 7

    async def test_non_200_maps_api_error(self) -> None:
        """非 200 状态：CHANNEL_API_ERROR（details 携带方法与状态码）。"""
        client = _client(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["method"] == "getUpdates"
        assert exc_info.value.details["status_code"] == 500

    async def test_401_maps_token_missing(self) -> None:
        """401 状态：CHANNEL_TOKEN_MISSING（token 无效——启动校验消费）。"""
        client = _client(
            lambda request: httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_ok_false_body_maps_api_error(self) -> None:
        """200 + ok=False 载荷：CHANNEL_API_ERROR（平台语义失败面）。"""
        client = _client(
            lambda request: httpx.Response(
                200, json={"ok": False, "error_code": 400, "description": "Bad request"}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["description"] == "Bad request"

    async def test_ok_false_with_401_body_maps_token_missing(self) -> None:
        """200 + ok=False + error_code=401：CHANNEL_TOKEN_MISSING。"""
        client = _client(
            lambda request: httpx.Response(
                200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        )
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_transport_error_maps_api_error(self) -> None:
        """httpx 传输异常：CHANNEL_API_ERROR（连接 / 超时统一面）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["method"] == "getUpdates"

    async def test_malformed_json_maps_api_error(self) -> None:
        """响应非合法 JSON：CHANNEL_API_ERROR。"""
        client = _client(lambda request: httpx.Response(200, text="not-json"))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_updates()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


class TestSendMessage:
    async def test_happy_path_body_shape(self) -> None:
        """快乐路径：POST body（chat_id / text / reply_to）+ 单段结果。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": 900, "chat": {"id": 10086, "type": "private"}})

        client = _client(handler)
        results = await client.send_message(chat_id="10086", text="你好", reply_to_message_id="42")
        assert bodies == [{"chat_id": "10086", "text": "你好", "reply_to_message_id": 42}]
        assert len(results) == 1
        assert results[0].message_id == 900
        assert results[0].chat.id == 10086

    async def test_no_reply_to_omits_field(self) -> None:
        """无引用：body 不携带 reply_to_message_id。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": 1, "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        await client.send_message(chat_id=10086, text="hi")
        assert bodies == [{"chat_id": 10086, "text": "hi"}]

    async def test_split_at_newline_boundary(self) -> None:
        """4096 分段：换行边界优先（段落不跨语义行）。"""
        sent_texts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
            return _ok({"message_id": len(sent_texts), "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        line_a = "甲" * 3000
        line_b = "乙" * 3000
        text = f"{line_a}\n{line_b}"
        results = await client.send_message(chat_id=1, text=text)
        assert len(results) == 2
        assert sent_texts == [line_a, line_b]

    async def test_hard_split_without_newline(self) -> None:
        """4096 分段：无换行硬切（窗口内无边界即按上限切）。"""
        sent_texts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
            return _ok({"message_id": len(sent_texts), "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        text = "x" * (TG_MESSAGE_MAX_CHARS + 100)
        results = await client.send_message(chat_id=1, text=text)
        assert [len(part) for part in sent_texts] == [TG_MESSAGE_MAX_CHARS, 100]
        assert "".join(sent_texts) == text
        assert len(results) == 2

    async def test_reply_to_only_on_first_part(self) -> None:
        """4096 分段：引用回复只挂首段（续文不引用）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": len(bodies), "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        await client.send_message(chat_id=1, text="a" * 5000, reply_to_message_id="42")
        assert len(bodies) == 2
        assert bodies[0]["reply_to_message_id"] == 42
        assert "reply_to_message_id" not in bodies[1]

    async def test_empty_text_rejected(self) -> None:
        """空文本：VALIDATION_ERROR（调用方 bug 面）。"""
        client = _client(lambda request: _ok(None))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_message(chat_id=1, text="  ")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_non_numeric_reply_to_rejected(self) -> None:
        """非数字引用 ID：VALIDATION_ERROR（Telegram 消息 ID 形态契约）。"""
        client = _client(lambda request: _ok(None))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_message(chat_id=1, text="hi", reply_to_message_id="abc")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_send_failure_maps_api_error(self) -> None:
        """send_message 非 200：CHANNEL_API_ERROR（method=sendMessage）。"""
        client = _client(lambda request: httpx.Response(502, text="bad gateway"))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_message(chat_id=1, text="hi")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["method"] == "sendMessage"


class TestGetMe:
    async def test_happy_path(self) -> None:
        """getMe 快乐路径：bot 信息归一（username / id）。"""
        client = _client(
            lambda request: _ok(
                {"id": 42, "is_bot": True, "username": "companion_bot", "first_name": "C"}
            )
        )
        bot = await client.get_me()
        assert bot.id == 42
        assert bot.is_bot is True
        assert bot.username == "companion_bot"

    async def test_failure_maps_api_error(self) -> None:
        """getMe 失败：CHANNEL_API_ERROR（runner 启动校验退避面）。"""
        client = _client(lambda request: httpx.Response(503, text="unavailable"))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_me()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_result_missing_maps_api_error(self) -> None:
        """ok=True 但 result 缺失：CHANNEL_API_ERROR（响应不完整面）。"""
        client = _client(lambda request: httpx.Response(200, json={"ok": True}))
        with pytest.raises(BusinessException) as exc_info:
            await client.get_me()
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR


class TestSplitFunction:
    async def test_short_text_single_part(self) -> None:
        """短文本：单段原样返回。"""
        assert split_tg_message_text("hello") == ["hello"]
        assert split_tg_message_text("x" * TG_MESSAGE_MAX_CHARS) == ["x" * TG_MESSAGE_MAX_CHARS]

    async def test_max_chars_below_one_rejected(self) -> None:
        """max_chars < 1：入口 ValueError（防零上限切分死循环）。"""
        for invalid in (0, -1):
            with pytest.raises(ValueError, match="max_chars 须 ≥ 1"):
                split_tg_message_text("任意文本", max_chars=invalid)

    async def test_boundary_newline_at_window_start_uses_hard_cut(self) -> None:
        """窗口首字符即换行：硬切（防零长段 / 死循环边界）。"""
        text = "\n" + "y" * (TG_MESSAGE_MAX_CHARS + 10)
        parts = split_tg_message_text(text)
        assert all(len(part) > 0 for part in parts)
        assert len(parts[0]) == TG_MESSAGE_MAX_CHARS
        assert "".join(parts) == text


class TestSendMessageV2:
    async def test_disable_notification_true_in_body(self) -> None:
        """silent=True：各段请求体携带 disable_notification: true。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": 1, "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        await client.send_message(chat_id=1, text="hi", disable_notification=True)
        assert bodies == [{"chat_id": 1, "text": "hi", "disable_notification": True}]

    async def test_disable_notification_false_omitted(self) -> None:
        """disable_notification=None / False：请求体省略该字段（v1 wire 形态保持）。"""
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return _ok({"message_id": 1, "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        await client.send_message(chat_id=1, text="hi", disable_notification=False)
        await client.send_message(chat_id=1, text="hi", disable_notification=None)
        assert bodies == [
            {"chat_id": 1, "text": "hi"},
            {"chat_id": 1, "text": "hi"},
        ]


class TestMediaSendV2:
    async def test_send_media_ref_json_shape(self) -> None:
        """媒体引用形态：方法 URL + JSON 体（chat_id / photo=URL / caption）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return _ok({"message_id": 901, "chat": {"id": 10086, "type": "private"}})

        client = _client(handler)
        result = await client.send_media_ref(
            chat_id="10086",
            api_method="sendPhoto",
            media_field="photo",
            media_ref="https://example.com/cat.jpg",
            caption="看这只猫",
            disable_notification=True,
        )
        assert captured["url"].endswith("/sendPhoto")
        assert captured["body"] == {
            "chat_id": "10086",
            "photo": "https://example.com/cat.jpg",
            "caption": "看这只猫",
            "disable_notification": True,
        }
        assert result.message_id == 901

    async def test_send_media_ref_omits_optional_fields(self) -> None:
        """媒体引用形态：caption / disable_notification 缺省省略（最小 wire）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return _ok({"message_id": 1, "chat": {"id": 1, "type": "private"}})

        client = _client(handler)
        await client.send_media_ref(
            chat_id=1, api_method="sendSticker", media_field="sticker", media_ref="CAACAgIAAx0"
        )
        assert captured["body"] == {"chat_id": 1, "sticker": "CAACAgIAAx0"}

    async def test_send_media_ref_empty_ref_rejected(self) -> None:
        """空媒体引用：VALIDATION_ERROR（调用方 bug 面）。"""
        client = _client(lambda request: _ok(None))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_media_ref(
                chat_id=1, api_method="sendPhoto", media_field="photo", media_ref="  "
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_send_media_upload_multipart_shape(self) -> None:
        """媒体上传形态：multipart 表单（文件字段名 / 文件名 / MIME /
        chat_id / caption / disable_notification）。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            return _ok({"message_id": 902, "chat": {"id": 10086, "type": "private"}})

        client = _client(handler)
        result = await client.send_media_upload(
            chat_id="10086",
            api_method="sendPhoto",
            media_field="photo",
            content=b"\xff\xd8fake-jpeg-bytes",
            filename="image.jpeg",
            content_type="image/jpeg",
            caption="随手拍",
            disable_notification=True,
        )
        assert captured["content_type"].startswith("multipart/form-data")
        body = captured["body"]
        assert b'name="photo"' in body
        assert b'filename="image.jpeg"' in body
        assert b"\xff\xd8fake-jpeg-bytes" in body
        assert b'name="chat_id"' in body
        assert b"10086" in body
        assert b'name="caption"' in body
        assert "随手拍".encode() in body
        assert b'name="disable_notification"' in body
        assert result.message_id == 902

    async def test_send_media_upload_empty_content_rejected(self) -> None:
        """空上传内容：VALIDATION_ERROR（调用方 bug 面）。"""
        client = _client(lambda request: _ok(None))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_media_upload(
                chat_id=1,
                api_method="sendPhoto",
                media_field="photo",
                content=b"",
                filename="image.jpeg",
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_send_media_upload_empty_filename_rejected(self) -> None:
        """空上传文件名：VALIDATION_ERROR（调用方 bug 面）。"""
        client = _client(lambda request: _ok(None))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_media_upload(
                chat_id=1,
                api_method="sendDocument",
                media_field="document",
                content=b"pdf-bytes",
                filename="  ",
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_media_failure_maps_api_error_with_method(self) -> None:
        """媒体发送非 200：CHANNEL_API_ERROR（details 携带媒体方法名）。"""
        client = _client(lambda request: httpx.Response(502, text="bad gateway"))
        with pytest.raises(BusinessException) as exc_info:
            await client.send_media_ref(
                chat_id=1, api_method="sendVoice", media_field="voice", media_ref="https://x/v.ogg"
            )
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["method"] == "sendVoice"

    async def test_media_transport_error_maps_api_error(self) -> None:
        """媒体发送传输异常：CHANNEL_API_ERROR（统一传输错误面）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _client(handler)
        with pytest.raises(BusinessException) as exc_info:
            await client.send_media_upload(
                chat_id=1,
                api_method="sendPhoto",
                media_field="photo",
                content=b"jpg",
                filename="image.jpeg",
            )
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details["method"] == "sendPhoto"
