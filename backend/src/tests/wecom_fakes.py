"""企微长连接测试基建——fake 传输连接 / fake 客户端 / 伪时钟（共享 fake 资源）。

测试纪律：**零真实网络**（连接面经 ``connector`` 注入进程内 fake；不存在任何
真实 socket / DNS / TLS）、**零真实等待**（``sleep`` 注入记录式 fake，退避与
心跳节奏按记录值断言，不按墙钟时间断言）、**零真实配置**（客户端 / runner 全部
显式参数构造，不读全局 Settings）。

fake 的职责边界：

    - ``FakeWeComConnection``：wire 级替身——记录出站命令（JSON 文本），按命令
      自动应答（默认全成功，可按命令注入错误码 / 静默丢应答 / 注入断连）；
    - ``FakeWeComConnector``：连接工厂替身——按脚本返回连接实例（支持「前 N 次
      连接失败」的重连退避场景）；
    - ``RecordingSleep``：休眠替身——记录请求的延时秒数后立即返回（可选闸门
      逐步放行，用于心跳 / 退避的确定性推进）；
    - ``FakeWeComClient``：客户端替身——runner 侧测试用，记录发送 / 上传调用并
      可主动注入回调帧。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from packages.harness.channels.wecom.wecom_models import (
    WECOM_CMD_MSG_CALLBACK,
    WECOM_CMD_SUBSCRIBE,
    WECOM_CMD_UPLOAD_MEDIA_FINISH,
    WECOM_CMD_UPLOAD_MEDIA_INIT,
    WeComCallbackFrameModel,
    WeComChatTargetModel,
    WeComMediaKind,
)

__all__ = [
    "FakeConnectionClosed",
    "FakeWeComConnection",
    "FakeWeComConnector",
    "RecordingSleep",
    "FakeWeComClient",
    "FakeRouter",
    "make_ack",
    "make_callback_frame",
    "make_event_frame",
]


class FakeConnectionClosed(ConnectionError):
    """fake 连接已关闭（模拟 websockets 断连异常——收帧方按连接失效处理）。"""


def make_ack(req_id: str, *, errcode: int = 0, errmsg: str = "ok", body: dict[str, Any] | None = None) -> str:
    """构造平台应答帧（JSON 文本）。

    Args:
        req_id: 请求标识（应答原样透传）。
        errcode: 错误码（0 = 成功）。
        errmsg: 错误信息。
        body: 应答载荷（素材上传场景）。

    Returns:
        JSON 文本帧。
    """
    payload: dict[str, Any] = {"headers": {"req_id": req_id}, "errcode": errcode, "errmsg": errmsg}
    if body is not None:
        payload["body"] = body
    return json.dumps(payload, ensure_ascii=False)


def make_callback_frame(
    body: dict[str, Any], *, cmd: str = WECOM_CMD_MSG_CALLBACK, req_id: str = "cb-req-1"
) -> str:
    """构造回调帧（默认消息回调）。

    Args:
        body: 回调 body（wire 字段名）。
        cmd: 回调命令。
        req_id: 回调请求标识。

    Returns:
        JSON 文本帧。
    """
    return json.dumps({"cmd": cmd, "headers": {"req_id": req_id}, "body": body}, ensure_ascii=False)


def make_event_frame(
    eventtype: str, *, req_id: str = "ev-req-1", msgid: str = "ev-msg-1"
) -> str:
    """构造事件回调帧。

    Args:
        eventtype: 事件类型。
        req_id: 回调请求标识。
        msgid: 事件唯一标志。

    Returns:
        JSON 文本帧。
    """
    body: dict[str, Any] = {
        "msgid": msgid,
        "create_time": 1700000000,
        "aibotid": "test-bot",
        "msgtype": "event",
        "event": {"eventtype": eventtype},
    }
    return make_callback_frame(body, cmd="aibot_event_callback", req_id=req_id)


class FakeWeComConnection:
    """长连接传输面替身（记录出站命令 + 脚本化入站帧）。

    Attributes:
        sent_payloads: 收到的出站命令原文（JSON 文本，按发送顺序）。
        sent_commands: 收到的出站命令解析结果（含 cmd / req_id / body）。
        closed: 是否已关闭（关闭后 ``recv`` 立即抛断连异常）。
        auto_ack: 是否自动应答（False = 测试手工 push 应答）。
        subscribe_errcode: 订阅应答的错误码（非 0 = 订阅被拒场景）。
        silent_commands: 不应答的命令值集合（应答超时场景）。
        command_errcodes: 按命令指定的错误码（平台拒绝场景）。
        upload_init_without_body: 初始化应答缺 body（缺字段路径）。
        upload_finish_without_body: 完成应答缺 body（缺字段路径）。
    """

    def __init__(self, *, auto_ack: bool = True) -> None:
        """初始化 fake 连接。

        Args:
            auto_ack: 是否自动应答出站命令。
        """
        self.sent_payloads: list[str] = []
        self.sent_commands: list[dict[str, Any]] = []
        self.closed = False
        self.auto_ack = auto_ack
        self.subscribe_errcode = 0
        self.silent_commands: set[str] = set()
        self.command_errcodes: dict[str, int] = {}
        self.upload_init_without_body = False
        self.upload_finish_without_body = False
        self.close_calls = 0
        self._inbound: asyncio.Queue[str | BaseException] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        """发送一帧（记录 + 按需自动应答）。

        Args:
            payload: JSON 文本帧。

        Raises:
            FakeConnectionClosed: 连接已关闭。
        """
        if self.closed:
            raise FakeConnectionClosed("fake 连接已关闭")
        self.sent_payloads.append(payload)
        command = json.loads(payload)
        self.sent_commands.append(command)
        if self.auto_ack:
            self._auto_ack(command)

    async def recv(self) -> str | bytes:
        """接收一帧（等待脚本化入站帧）。

        Returns:
            入站帧。

        Raises:
            FakeConnectionClosed: 连接已关闭 / 注入的断连异常。
        """
        if self.closed:
            raise FakeConnectionClosed("fake 连接已关闭")
        item = await self._inbound.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        """关闭连接（幂等——重复关闭仅计数）。"""
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        self._inbound.put_nowait(FakeConnectionClosed("fake 连接已关闭"))

    # ------------------------------------------------------------------
    # 脚本化入站
    # ------------------------------------------------------------------

    def push(self, payload: str) -> None:
        """注入一帧入站载荷。

        Args:
            payload: JSON 文本帧。
        """
        self._inbound.put_nowait(payload)

    def push_error(self, exc: BaseException) -> None:
        """注入一个收帧异常（模拟断连）。

        Args:
            exc: 异常实例。
        """
        self._inbound.put_nowait(exc)

    def commands_for(self, cmd: str) -> list[dict[str, Any]]:
        """按命令名筛选出站命令记录。

        Args:
            cmd: 命令名。

        Returns:
            匹配的命令列表（按发送顺序）。
        """
        return [command for command in self.sent_commands if command.get("cmd") == cmd]

    def _auto_ack(self, command: dict[str, Any]) -> None:
        """按命令自动生成应答（默认全成功）。

        Args:
            command: 出站命令解析结果。
        """
        cmd = command.get("cmd")
        req_id = command.get("headers", {}).get("req_id", "")
        if cmd in self.silent_commands:
            return
        errcode = self.command_errcodes.get(cmd, 0)
        if cmd == WECOM_CMD_SUBSCRIBE and self.subscribe_errcode:
            errcode = self.subscribe_errcode
        body: dict[str, Any] | None = None
        if errcode == 0 and cmd == WECOM_CMD_UPLOAD_MEDIA_INIT and not self.upload_init_without_body:
            body = {"upload_id": "fake-upload-id"}
        elif errcode == 0 and cmd == WECOM_CMD_UPLOAD_MEDIA_FINISH and not self.upload_finish_without_body:
            body = {"type": "file", "media_id": "fake-media-id"}
        self._inbound.put_nowait(make_ack(req_id, errcode=errcode, body=body))


class FakeWeComConnector:
    """连接工厂替身（按脚本返回连接 / 前 N 次失败）。

    Attributes:
        attempts: 已发起的连接次数。
        connections: 已产出的连接实例（按顺序）。
    """

    def __init__(
        self,
        *,
        failures_before_success: int = 0,
        auto_ack: bool = True,
    ) -> None:
        """初始化连接工厂。

        Args:
            failures_before_success: 首次成功前的失败次数（重连退避场景）。
            auto_ack: 产出的连接是否自动应答。
        """
        self.attempts = 0
        self.connections: list[FakeWeComConnection] = []
        self._failures_left = failures_before_success
        self._auto_ack = auto_ack

    async def __call__(self, endpoint: str, open_timeout_seconds: float) -> FakeWeComConnection:
        """建立连接（按脚本失败 / 成功）。

        Args:
            endpoint: 端点（记录面）。
            open_timeout_seconds: 握手超时（记录面）。

        Returns:
            新的 fake 连接。

        Raises:
            OSError: 脚本失败窗口内的握手失败。
        """
        self.attempts += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            raise OSError("fake 握手失败")
        connection = FakeWeComConnection(auto_ack=self._auto_ack)
        self.connections.append(connection)
        return connection


class RecordingSleep:
    """休眠替身（记录延时后返回；闸门模式按次放行）。

    闸门是**一次一放行**的许可队列（``release_once`` 放行一次等待），因此
    心跳节拍与退避重连可以按「记录 → 放行 → 再记录」确定性推进，既不真实
    等待也不自旋。

    Attributes:
        delays: 已请求的延时秒数（按调用顺序）。
        gated: 是否启用闸门（True = 每次调用等待一次 ``release_once``）。
    """

    def __init__(self, *, gated: bool = False) -> None:
        """初始化休眠替身。

        Args:
            gated: 是否启用闸门（确定性推进心跳 / 退避）。
        """
        self.delays: list[float] = []
        self.gated = gated
        self._permits: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        """记录延时并（闸门模式下）等待一次放行。

        Args:
            delay: 请求的延时秒数。
        """
        self.delays.append(delay)
        if self.gated:
            await self._permits.get()

    def release_once(self) -> None:
        """放行一次等待（许可计数 +1）。"""
        self._permits.put_nowait(None)


class FakeRouter:
    """通道路由器替身（记录入站消息——runner 侧路由形状断言用）。

    Attributes:
        received: 收到的入站消息（按处理顺序）。
        fail_next: 下一条消息处理时抛异常（隔离路径）。
        hold: 处理闸门（非 None 时每条消息先等待该事件——队列过载场景）。
    """

    def __init__(self) -> None:
        """初始化 fake 路由器。"""
        self.received: list[Any] = []
        self.fail_next = False
        self.hold: asyncio.Event | None = None

    async def handle_inbound(self, message: Any) -> Any:
        """记录入站消息（可选闸门 / 可选抛错）。

        Args:
            message: 入站消息。

        Returns:
            空字符串（真实路由器返回投递结果——测试不消费）。
        """
        if self.hold is not None:
            await self.hold.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fake 路由器注入失败")
        self.received.append(message)
        return ""


class FakeWeComClient:
    """长连接客户端替身（runner 侧——记录发送 / 上传调用）。

    Attributes:
        markdown_replies: 回复调用记录（req_id + content）。
        markdown_pushes: 主动推送记录（目标 + content）。
        media_pushes: 附件推送记录（目标 + 素材类型 + media_id）。
        uploads: 上传调用记录（字节数 + 素材类型 + 文件名）。
        uploaded_content: 上传的原始字节（按顺序）。
        run_started: ``run`` 是否进入。
        stop_requested: 是否收到停机请求。
        upload_error: 上传时抛出的业务异常（失败路径注入）。
        media_id: 上传返回值。
    """

    def __init__(self) -> None:
        """初始化 fake 客户端。"""
        self.markdown_replies: list[tuple[str, str]] = []
        self.markdown_pushes: list[tuple[WeComChatTargetModel, str]] = []
        self.media_pushes: list[tuple[WeComChatTargetModel, WeComMediaKind, str]] = []
        self.uploads: list[tuple[int, WeComMediaKind, str]] = []
        self.uploaded_content: list[bytes] = []
        self.run_started = False
        self.stop_requested = False
        self.upload_error: Exception | None = None
        self.media_id = "fake-media-id"
        self.handler: Callable[[WeComCallbackFrameModel], Awaitable[None]] | None = None
        self._stop_event = asyncio.Event()

    def set_callback_handler(
        self, handler: Callable[[WeComCallbackFrameModel], Awaitable[None]]
    ) -> None:
        """登记回调处理器（runner 装配面）。

        Args:
            handler: 回调处理器。
        """
        self.handler = handler

    def request_stop(self) -> None:
        """记录停机请求并放行 ``run``。"""
        self.stop_requested = True
        self._stop_event.set()

    async def run(self) -> None:
        """模拟 supervisor（等待停机请求后返回）。"""
        self.run_started = True
        await self._stop_event.wait()

    async def send_markdown_reply(self, *, callback_req_id: str, content: str) -> str:
        """记录回复调用。

        Args:
            callback_req_id: 回调 req_id。
            content: markdown 文本。

        Returns:
            固定应答标记。
        """
        self.markdown_replies.append((callback_req_id, content))
        return "ack"

    async def send_markdown_push(self, *, target: WeComChatTargetModel, content: str) -> str:
        """记录主动推送调用。

        Args:
            target: 会话目标。
            content: markdown 文本。

        Returns:
            固定应答标记。
        """
        self.markdown_pushes.append((target, content))
        return "ack"

    async def send_media_push(
        self,
        *,
        target: WeComChatTargetModel,
        media_kind: WeComMediaKind,
        media_id: str,
    ) -> str:
        """记录附件推送调用。

        Args:
            target: 会话目标。
            media_kind: 素材类型。
            media_id: 素材 ID。

        Returns:
            固定应答标记。
        """
        self.media_pushes.append((target, media_kind, media_id))
        return "ack"

    async def upload_media(
        self, content: bytes, *, media_kind: WeComMediaKind, filename: str
    ) -> str:
        """记录上传调用（可注入失败）。

        Args:
            content: 明文字节。
            media_kind: 素材类型。
            filename: 文件名。

        Returns:
            配置的素材 ID。

        Raises:
            Exception: 注入的上传失败。
        """
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((len(content), media_kind, filename))
        self.uploaded_content.append(content)
        return self.media_id

    async def emit_frame(self, frame: WeComCallbackFrameModel) -> None:
        """驱动已登记的回调处理器（runner 入站注入）。

        Args:
            frame: 回调帧。
        """
        assert self.handler is not None, "runner 尚未登记回调处理器"
        await self.handler(frame)
