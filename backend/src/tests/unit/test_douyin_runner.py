"""douyin 通道 runner 测试——轮询路由 / 去重 / 退避 / 出站语义 / 隔离。

覆盖矩阵：

    - 轮询：新消息逐条路由 + 去重登记 + 第二轮只投新增；自身回声 / 系统消息
      语义；会话标识兜底（页面与配置皆空 → default）；
    - 隔离：单条路由异常不击穿轮询（其余消息照常 + 失败消息下轮重试）；
    - 退避：连续失败按指数增长且封顶；停机请求即时收口；
    - 出站：文本经页面发送 + 附件逐条标记不支持 + message_ids 空；grammar
      非法 / 文本失败上抛（路由器兜底面）；
    - 构造不变式：轮询间隔 > 0、退避封顶 ≥ 基数；去重窗口 FIFO 淘汰。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelAttachmentKind,
    ChannelMessageAttachment,
    ChannelType,
    OutboundMessageModel,
)
from packages.harness.channels.douyin.douyin_client import DouyinWebClient
from packages.harness.channels.douyin.douyin_runner import (
    DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR,
    DouyinChannelRunner,
    DouyinMessageDedupWindow,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.douyin_fakes import (
    FakeDouyinBrowserInvoker,
    FakeDouyinRouter,
    SleepGate,
    make_message_payload,
    make_snapshot_payload,
)


def _runner(
    *,
    invoker: FakeDouyinBrowserInvoker,
    router: Any,
    sleep: Any = None,
    conversation_id: str = "",
    backoff_max_seconds: float = 60.0,
) -> DouyinChannelRunner:
    """构造测试 runner（显式参数 + fake 路由器——不读全局 Settings）。"""
    return DouyinChannelRunner(
        client=DouyinWebClient(invoker=invoker),
        router=router,
        conversation_id=conversation_id,
        poll_interval_seconds=0.5,
        backoff_base_seconds=2.0,
        backoff_max_seconds=backoff_max_seconds,
        max_messages_per_poll=20,
        sleep=sleep,
    )


async def test_run_routes_new_messages_and_dedups_across_polls() -> None:
    """轮询：第一轮投 m-1；第二轮只有 m-2（m-1 已在去重窗口）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.extend(
        [
            make_snapshot_payload(messages=[make_message_payload(message_id="m-1")]),
            make_snapshot_payload(
                messages=[
                    make_message_payload(message_id="m-1"),
                    make_message_payload(message_id="m-2", text="第二条"),
                ]
            ),
        ]
    )
    router = FakeDouyinRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(invoker=invoker, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [message.message_id for message in router.inbound] == ["m-1", "m-2"]
    assert runner.dedup_size == 2
    assert all(message.channel is ChannelType.DOUYIN for message in router.inbound)


async def test_run_marks_system_messages_synthetic_and_skips_self() -> None:
    """轮询：自身回声不进路由；系统消息按 synthetic 上浮。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append(
        make_snapshot_payload(
            messages=[
                make_message_payload(message_id="m-self", is_self=True),
                make_message_payload(message_id="m-sys", text="对方已开启新会话", is_system=True),
            ]
        )
    )
    router = FakeDouyinRouter()
    gate = SleepGate(stop_after=1)
    runner = _runner(invoker=invoker, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [message.message_id for message in router.inbound] == ["m-sys"]
    assert router.inbound[0].synthetic is True


async def test_run_uses_default_conversation_when_unspecified() -> None:
    """轮询：页面与配置皆未报告会话 → 会话键按 default 兜底。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append(
        make_snapshot_payload(conversation_id="", messages=[make_message_payload(message_id="m-1")])
    )
    router = FakeDouyinRouter()
    gate = SleepGate(stop_after=1)
    runner = _runner(invoker=invoker, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert router.inbound[0].chat_id == "im:default"


async def test_router_failure_is_isolated_and_message_retried_next_poll() -> None:
    """隔离：单条路由异常不击穿轮询；失败消息不登记去重（下轮重试）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.extend(
        [
            make_snapshot_payload(
                messages=[
                    make_message_payload(message_id="m-fail"),
                    make_message_payload(message_id="m-ok"),
                ]
            ),
            make_snapshot_payload(messages=[make_message_payload(message_id="m-fail")]),
        ]
    )
    router = FakeDouyinRouter()
    router.failing_message_ids = {"m-fail"}
    gate = SleepGate(stop_after=2)
    runner = _runner(invoker=invoker, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [message.message_id for message in router.inbound] == ["m-ok"]
    assert runner.dedup_size == 1


async def test_backoff_grows_and_caps_on_repeated_failures() -> None:
    """退避：连续失败指数增长且按封顶收敛；成功即复位。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_ok = False
    router = FakeDouyinRouter()
    gate = SleepGate(stop_after=3)
    runner = _runner(invoker=invoker, router=router, sleep=gate, backoff_max_seconds=5.0)
    gate.runner = runner
    await runner.run()
    assert gate.delays == [2.0, 4.0, 5.0]
    assert runner.consecutive_failures == 3


async def test_stop_request_interrupts_polling() -> None:
    """停机：请求后 run 立即收口（不再发起采集）。"""
    invoker = FakeDouyinBrowserInvoker()
    router = FakeDouyinRouter()
    runner = _runner(invoker=invoker, router=router)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    runner.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    evaluate_calls = len(invoker.evaluate_calls)
    assert evaluate_calls <= 1


async def test_run_is_single_instance() -> None:
    """单实例不变式：并发第二次 run 幂等返回（不发起额外采集）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.extend([make_snapshot_payload(), make_snapshot_payload()])
    router = FakeDouyinRouter()
    runner = _runner(invoker=invoker, router=router)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    await runner.run()
    runner.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(invoker.evaluate_calls) == 1


async def test_send_message_text_and_unsupported_attachments() -> None:
    """出站：文本经页面发送；附件逐条标记不支持；message_ids 恒空。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append({"ok": True, "text": "晚安"})
    router = FakeDouyinRouter()
    runner = _runner(invoker=invoker, router=router)
    message = OutboundMessageModel(
        chat_id="im:c-1",
        text="晚安",
        attachments=[
            ChannelMessageAttachment(kind=ChannelAttachmentKind.IMAGE, url="https://p3.example/a.jpg"),
            ChannelMessageAttachment(kind=ChannelAttachmentKind.FILE, url="https://p3.example/b.pdf"),
        ],
        silent=True,
    )
    result = await runner.send_message(message)
    assert result.channel is ChannelType.DOUYIN
    assert result.chat_id == "im:c-1"
    assert result.message_ids == []
    assert [(item.index, item.ok, item.error) for item in result.attachment_results] == [
        (0, False, DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR),
        (1, False, DOUYIN_ATTACHMENT_UNSUPPORTED_ERROR),
    ]
    assert '"晚安"' not in invoker.evaluate_calls[0]["script"]
    assert "\\u665a\\u5b89" in invoker.evaluate_calls[0]["script"]


async def test_send_message_rejects_bad_chat_id() -> None:
    """出站 grammar：前缀缺失 → VALIDATION_ERROR（调用方 bug 面）。"""
    runner = _runner(invoker=FakeDouyinBrowserInvoker(), router=FakeDouyinRouter())
    with pytest.raises(BusinessException) as error:
        await runner.send_message(OutboundMessageModel(chat_id="c-1", text="你好"))
    assert error.value.code is ErrorCode.VALIDATION_ERROR


async def test_send_message_text_failure_raises() -> None:
    """出站文本失败：上抛 CHANNEL_API_ERROR（路由器兜底隔离）。"""
    invoker = FakeDouyinBrowserInvoker()
    invoker.evaluate_queue.append({"ok": False, "error": "未找到输入框"})
    runner = _runner(invoker=invoker, router=FakeDouyinRouter())
    with pytest.raises(BusinessException) as error:
        await runner.send_message(OutboundMessageModel(chat_id="im:c-1", text="你好"))
    assert error.value.code is ErrorCode.CHANNEL_API_ERROR


async def test_runner_rejects_invalid_bounds() -> None:
    """构造不变式：轮询间隔必须 > 0；退避封顶不得小于基数。"""
    client = DouyinWebClient(invoker=FakeDouyinBrowserInvoker())
    fake_router: Any = FakeDouyinRouter()
    with pytest.raises(ValueError):
        DouyinChannelRunner(
            client=client,
            router=fake_router,
            poll_interval_seconds=0.0,
        )
    with pytest.raises(ValueError):
        DouyinChannelRunner(
            client=client,
            router=fake_router,
            backoff_base_seconds=10.0,
            backoff_max_seconds=5.0,
        )


async def test_dedup_window_evicts_oldest() -> None:
    """去重窗口：超容按 FIFO 淘汰最旧标识。"""
    window = DouyinMessageDedupWindow(capacity=2)
    window.remember("m-1")
    window.remember("m-2")
    window.remember("m-3")
    assert window.size == 2
    assert window.seen("m-1") is False
    assert window.seen("m-2") is True
    assert window.seen("m-3") is True
    window.remember("m-3")
    assert window.size == 2
