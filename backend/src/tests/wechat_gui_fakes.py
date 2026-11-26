"""GUI 微信通道测试基建——fake 沙盒客户端 / 视觉解析器 / 路由器 / 休眠闸。

测试纪律：零真实网络（沙盒客户端经注入 fake，HTTP 契约在
``test_wechat_gui_client`` 内用 MockTransport 覆盖）、零真实等待（休眠注入
记录式 fake，轮询与退避按记录值断言）、零真实配置（runner / adapter 全部
显式参数构造，不读全局 Settings）。
"""

from __future__ import annotations

import base64
from typing import Any

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    VlmReadResultModel,
    WechatGuiMessageModel,
    WechatGuiOpenChatResultModel,
    WechatGuiScreenshotModel,
)

__all__ = [
    "FakeWechatGuiClient",
    "FakeWechatGuiReader",
    "FakeWechatGuiRouter",
    "SleepGate",
    "make_message",
    "make_open_chat_result",
    "make_read_result",
    "make_screenshot",
]


def make_screenshot(*, sha: str, width: int = 1280, height: int = 800) -> WechatGuiScreenshotModel:
    """构造截图结果（sha 决定变化检测分支）。

    Args:
        sha: 截图 sha256。
        width: 宽度（像素）。
        height: 高度（像素）。

    Returns:
        截图模型（base64 为固定 PNG 占位字节）。
    """
    return WechatGuiScreenshotModel(
        image_base64=base64.b64encode(f"png-{sha}".encode()).decode("ascii"),
        sha256=sha,
        width=width,
        height=height,
    )


def make_open_chat_result(*, remark_name: str = "小林") -> WechatGuiOpenChatResultModel:
    """构造 open_chat 工具结果。"""
    return WechatGuiOpenChatResultModel(
        remark_name=remark_name,
        title_image_base64=base64.b64encode(b"title").decode("ascii"),
        title_sha256="title-sha",
        full_image_base64=base64.b64encode(b"full").decode("ascii"),
        full_sha256="full-sha",
    )


def make_message(
    *,
    text: str = "你好",
    sender: str = "小林",
    ts_ms: int = 1731000000000,
    is_self: bool = False,
) -> WechatGuiMessageModel:
    """构造单条解析消息。"""
    return WechatGuiMessageModel(sender=sender, text=text, ts_ms=ts_ms, is_self=is_self)


def make_read_result(
    *,
    title: str = "小林",
    messages: list[WechatGuiMessageModel] | None = None,
    confidence: float = 0.9,
) -> VlmReadResultModel:
    """构造视觉解析结果。"""
    return VlmReadResultModel(
        title=title,
        messages=list(messages or []),
        confidence=confidence,
        raw_json={},
    )


class FakeWechatGuiClient:
    """记录式沙盒客户端（截图 / 打开会话 / 输入 / 按键 + 失败注入）。"""

    def __init__(
        self,
        *,
        screenshots: list[WechatGuiScreenshotModel] | None = None,
        read_result: VlmReadResultModel | None = None,
    ) -> None:
        self.has_token = True
        self.open_calls: list[str] = []
        self.screenshot_calls = 0
        self.type_calls: list[str] = []
        self.key_calls: list[str] = []
        self._screenshots = list(screenshots or [make_screenshot(sha="sha-1")])
        self._open_result = make_open_chat_result()
        self._read_result = read_result
        self.fail_open_chat: Exception | None = None
        self.fail_screenshot: Exception | None = None
        self.fail_type_text: Exception | None = None
        self.fail_press_key: Exception | None = None

    async def probe_health(self) -> bool:
        """fake 健康探测（恒 True）。"""
        return True

    async def open_chat(self, remark_name: str) -> WechatGuiOpenChatResultModel:
        """记录打开请求并返回脚本化截图。"""
        self.open_calls.append(remark_name)
        if self.fail_open_chat is not None:
            raise self.fail_open_chat
        return self._open_result

    async def screenshot(self) -> WechatGuiScreenshotModel:
        """按队列返回截图（队列耗尽后复述最后一项）。"""
        self.screenshot_calls += 1
        if self.fail_screenshot is not None:
            raise self.fail_screenshot
        if len(self._screenshots) > 1:
            return self._screenshots.pop(0)
        return self._screenshots[0]

    async def type_text(self, text: str) -> None:
        """记录输入文本。"""
        self.type_calls.append(text)
        if self.fail_type_text is not None:
            raise self.fail_type_text

    async def press_key(self, key: str) -> None:
        """记录按键表达式。"""
        self.key_calls.append(key)
        if self.fail_press_key is not None:
            raise self.fail_press_key


class FakeWechatGuiReader:
    """记录式视觉解析器（按队列返回结果 / 可注入异常）。"""

    def __init__(self, results: list[VlmReadResultModel] | None = None) -> None:
        self.calls: list[str] = []
        self._results = list(results or [])
        self.error: Exception | None = None

    async def read(self, image_base64: str, *, request_id: str | None = None) -> VlmReadResultModel:
        """记录截图载荷并返回脚本化结果。"""
        self.calls.append(image_base64)
        if self.error is not None:
            raise self.error
        if self._results:
            return self._results.pop(0)
        return make_read_result()


class FakeWechatGuiRouter:
    """fake 通道路由器（记录入站消息 + 可注入逐消息异常）。"""

    def __init__(self) -> None:
        self.inbound: list[InboundMessageModel] = []
        self.failing_message_ids: set[str] = set()

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """记录入站消息（可注入异常——runner 隔离面断言）。"""
        if message.message_id in self.failing_message_ids:
            raise RuntimeError(f"路由失败（注入）: {message.message_id}")
        self.inbound.append(message)
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECHAT_GUI, chat_id=message.chat_id, message_ids=[]
        )

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（满足适配器协议的最小实现）。"""
        return ChannelDeliveryResultModel(
            channel=ChannelType.WECHAT_GUI, chat_id=message.chat_id, message_ids=[]
        )


class SleepGate:
    """记录式休眠替身（记录延时；达到闸门次数后触发 runner 停机）。"""

    def __init__(self, *, stop_after: int) -> None:
        """初始化闸门。

        Args:
            stop_after: 第几次休眠后停机（1 起）。
        """
        self._stop_after = stop_after
        self.runner: Any = None
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """记录延时并在闸门次数处请求停机。"""
        self.delays.append(seconds)
        if len(self.delays) >= self._stop_after and self.runner is not None:
            self.runner.request_stop()
