"""抖音 web 通道测试基建——fake 浏览器调用面 / fake 路由器 / 记录式休眠（共享）。

测试纪律：**零真实网络**（浏览器调用面经 ``DouyinBrowserInvoker`` 注入进程内
fake——不存在真实 sidecar / playwright / socket）、**零真实等待**（休眠注入
记录式 fake，轮询与退避按记录值断言，不按墙钟断言）、**零真实配置**（客户端
/ runner 全部显式参数构造，不读全局 Settings）。

fake 的职责边界：

    - ``FakeDouyinBrowserInvoker``：浏览器工具调用替身——按工具返回脚本化结果
      （``navigate`` 成败可注入；``evaluate`` 按队列返回采集 / 发送载荷）；
    - ``FakeDouyinRouter``：路由器替身——记录入站消息并可注入逐消息异常；
    - ``SleepGate``：休眠替身——记录请求的延时秒数，达到闸门次数后触发
      runner 停机（让 ``run()`` 在有限轮次内正常返回）。
"""

from __future__ import annotations

from typing import Any

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.tools.tool_models import ToolResultModel

__all__ = [
    "FakeDouyinBrowserInvoker",
    "FakeDouyinRouter",
    "SleepGate",
    "make_message_payload",
    "make_snapshot_payload",
]


def make_message_payload(
    *,
    message_id: str,
    text: str = "你好",
    sender_id: str = "u-1",
    ts_ms: int = 1731000000000,
    is_self: bool = False,
    is_system: bool = False,
    is_group: bool = False,
    image_urls: list[str] | None = None,
) -> dict[str, Any]:
    """构造采集脚本单条消息载荷（wire 形态——与脚本返回结构一致）。

    Args:
        message_id: 消息标识。
        text: 消息文本。
        sender_id: 发送者标识。
        ts_ms: 消息时间（epoch 毫秒）。
        is_self: 是否本账号发出。
        is_system: 是否系统消息。
        is_group: 是否群会话。
        image_urls: 图片地址列表（None = 无图）。

    Returns:
        脚本消息载荷 JSON 对象。
    """
    return {
        "message_id": message_id,
        "sender_id": sender_id,
        "text": text,
        "ts_ms": ts_ms,
        "is_self": is_self,
        "is_system": is_system,
        "is_group": is_group,
        "image_urls": list(image_urls or []),
    }


def make_snapshot_payload(
    *,
    conversation_id: str = "c-1",
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造采集脚本完整返回载荷。

    Args:
        conversation_id: 页面报告的会话标识。
        messages: 消息载荷列表（None = 空列表）。

    Returns:
        脚本返回 JSON 对象。
    """
    return {"conversation_id": conversation_id, "messages": list(messages or [])}


class FakeDouyinBrowserInvoker:
    """fake 浏览器调用面（记录调用 / 按队列返回脚本化结果）。

    ``evaluate_queue`` 按调用顺序消费：每项是 ``evaluate`` 工具结果里的
    ``result`` 对象（采集快照 / 发送结果）；队列耗尽后返回空对象。
    """

    def __init__(self) -> None:
        """初始化（空队列 + 全成功形态）。"""
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.evaluate_queue: list[dict[str, Any]] = []
        self.navigate_ok = True
        self.navigate_error = "导航失败（注入）"
        self.evaluate_ok = True
        self.evaluate_error = "页面求值失败（注入）"

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """记录调用并返回脚本化结果（结构类型满足 ``DouyinBrowserInvoker``）。"""
        self.invocations.append((tool, dict(arguments)))
        if tool == "navigate":
            if not self.navigate_ok:
                return ToolResultModel(tool=tool, ok=False, error=self.navigate_error)
            return ToolResultModel(tool=tool, ok=True, result={"url": arguments["url"]})
        if tool == "evaluate":
            if not self.evaluate_ok:
                return ToolResultModel(tool=tool, ok=False, error=self.evaluate_error)
            payload = self.evaluate_queue.pop(0) if self.evaluate_queue else {}
            return ToolResultModel(
                tool=tool,
                ok=True,
                result={"result": payload, "char_count": len(str(payload))},
            )
        return ToolResultModel(tool=tool, ok=True, result={})

    @property
    def evaluate_calls(self) -> list[dict[str, Any]]:
        """evaluate 调用的参数列表（脚本 / 上限断言面）。"""
        return [arguments for tool, arguments in self.invocations if tool == "evaluate"]


class FakeDouyinRouter:
    """fake 通道路由器（记录入站消息 + 可注入逐消息异常）。"""

    def __init__(self) -> None:
        """初始化（空记录 + 无失败注入）。"""
        self.inbound: list[InboundMessageModel] = []
        self.failing_message_ids: set[str] = set()

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """记录入站消息（可注入异常——runner 隔离面断言）。"""
        if message.message_id in self.failing_message_ids:
            raise RuntimeError(f"路由失败（注入）: {message.message_id}")
        self.inbound.append(message)
        return ChannelDeliveryResultModel(
            channel=message.channel, chat_id=message.chat_id, message_ids=[]
        )

    async def send_message(self, message: OutboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 发送面（满足适配器协议的最小实现）。"""
        return ChannelDeliveryResultModel(
            channel=message.channel, chat_id=message.chat_id, message_ids=[]
        )


class SleepGate:
    """记录式休眠替身（记录延时；达到闸门次数后触发 runner 停机）。

    ``run()`` 在闸门触发后于下一轮循环条件处退出——测试因此零真实等待。
    """

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
