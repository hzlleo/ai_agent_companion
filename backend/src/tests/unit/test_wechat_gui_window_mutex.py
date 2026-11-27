"""GUI 微信单窗口互斥测试——runner 轮询与出站适配器共享锁 / 共享目标。

零网络 / 零真实等待：单窗口 fake 客户端记录「输入 / 回车时的当前会话」，
编排钩子在视觉解析期间让另一侧尝试抢窗口，以实证锁的持有与释放边界。
"""

from __future__ import annotations

import asyncio
from typing import Any

from packages.harness.channels.channel_models import OutboundMessageModel
from packages.harness.channels.wechat_gui.wechat_gui_adapter import WechatGuiChannelAdapter
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WechatGuiContactModel,
    WechatGuiWindowState,
)
from packages.harness.channels.wechat_gui.wechat_gui_runner import WechatGuiChannelRunner
from tests.wechat_gui_fakes import (
    FakeGuiWindow,
    FakeWechatGuiRouter,
    FakeWechatGuiWindowClient,
    FakeWechatGuiWindowReader,
    make_message,
)

_NOW_MS = 1_731_000_000_000


def _runner(
    *,
    client: FakeWechatGuiWindowClient,
    reader: FakeWechatGuiWindowReader,
    router: Any,
    gui_lock: asyncio.Lock,
    window_state: WechatGuiWindowState,
) -> WechatGuiChannelRunner:
    """构造与出站适配器共享锁 / 目标状态的 runner。"""
    return WechatGuiChannelRunner(
        client=client,
        reader=reader,
        router=router,
        contacts={
            "小林": WechatGuiContactModel(remark_name="小林"),
            "阿青": WechatGuiContactModel(remark_name="阿青"),
        },
        clock=lambda: _NOW_MS,
        gui_lock=gui_lock,
        window_state=window_state,
    )


def _adapter(
    *,
    client: FakeWechatGuiWindowClient,
    reader: FakeWechatGuiWindowReader,
    gui_lock: asyncio.Lock,
    window_state: WechatGuiWindowState,
) -> WechatGuiChannelAdapter:
    """构造与 runner 共享锁 / 目标状态的出站适配器。"""
    return WechatGuiChannelAdapter(
        client=client,
        reader=reader,
        gui_lock=gui_lock,
        window_state=window_state,
    )


async def test_outbound_send_holds_lock_against_runner_switch() -> None:
    """出站校验 → 输入 → 回车全程持锁：轮询等锁，出站后新增消息不丢。"""
    window = FakeGuiWindow()
    peer_head = make_message(text="阿青旧消息", sender="阿青")
    peer_new = make_message(text="阿青新消息", sender="阿青")
    messages: list[Any] = [peer_head]
    client = FakeWechatGuiWindowClient(window, messages_by_chat={"阿青": messages})
    router = FakeWechatGuiRouter()
    gui_lock = asyncio.Lock()
    window_state = WechatGuiWindowState()
    reader = FakeWechatGuiWindowReader(client, messages_by_chat={"阿青": messages})
    runner = _runner(
        client=client, reader=reader, router=router, gui_lock=gui_lock, window_state=window_state
    )
    adapter = _adapter(
        client=client, reader=reader, gui_lock=gui_lock, window_state=window_state
    )
    # 先建阿青的水位基线（首帧零投递）。
    await runner._poll_target("阿青", WechatGuiContactModel(remark_name="阿青"))
    messages.append(peer_new)
    spawned: list[asyncio.Task[None]] = []

    async def runner_switches_to_other_chat() -> None:
        spawned.append(
            asyncio.create_task(
                runner._poll_target("阿青", WechatGuiContactModel(remark_name="阿青"))
            )
        )
        await asyncio.sleep(0.02)

    reader.arm_hook(runner_switches_to_other_chat)
    await adapter.send_message(
        OutboundMessageModel(chat_id="wechat_gui:小林", text="晚安")
    )
    for task in spawned:
        await asyncio.wait_for(task, timeout=2.0)
    assert window.sent_in == ["小林"]
    assert window.typed == [("小林", "晚安")]
    assert [message.text for message in router.inbound] == ["阿青新消息"]


async def test_runner_waits_for_inflight_outbound_and_delivers_after() -> None:
    """出站进行中轮询等待锁：出站完成后轮询才切窗口，且新增消息不丢。"""
    window = FakeGuiWindow()
    peer_head = make_message(text="阿青旧消息", sender="阿青")
    peer_new = make_message(text="阿青新消息", sender="阿青")
    messages: list[Any] = [peer_head]
    client = FakeWechatGuiWindowClient(window, messages_by_chat={"阿青": messages})
    router = FakeWechatGuiRouter()
    gui_lock = asyncio.Lock()
    window_state = WechatGuiWindowState()
    reader = FakeWechatGuiWindowReader(client, messages_by_chat={"阿青": messages})
    runner = _runner(
        client=client, reader=reader, router=router, gui_lock=gui_lock, window_state=window_state
    )
    adapter = _adapter(
        client=client, reader=reader, gui_lock=gui_lock, window_state=window_state
    )
    # 先建阿青的水位基线（首帧零投递）。
    await runner._poll_target("阿青", WechatGuiContactModel(remark_name="阿青"))
    messages.append(peer_new)
    observed_open_calls: list[list[str]] = []
    spawned: list[asyncio.Task[None]] = []

    async def outbound_during_poll() -> None:
        spawned.append(
            asyncio.create_task(
                adapter.send_message(
                    OutboundMessageModel(chat_id="wechat_gui:小林", text="晚安")
                )
            )
        )
        await asyncio.sleep(0.02)
        observed_open_calls.append(list(client.open_calls))

    reader.arm_hook(outbound_during_poll)
    await runner._poll_target("阿青", WechatGuiContactModel(remark_name="阿青"))
    for task in spawned:
        await asyncio.wait_for(task, timeout=2.0)
    assert observed_open_calls == [["阿青"]]
    assert [message.text for message in router.inbound] == ["阿青新消息"]
    assert window.sent_in == ["小林"]
