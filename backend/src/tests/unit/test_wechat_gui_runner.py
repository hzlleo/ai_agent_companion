"""GUI 微信通道 runner 测试——轮询 / 变化检测 / P4 校验 / 去重 / 退避。

零真实网络 / 零真实等待：沙盒客户端与视觉解析器全部注入 fake；休眠经
记录式闸门在有限轮次内触发停机。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WECHAT_GUI_DEDUP_WINDOW_MS,
    TitleMismatchAction,
    WechatGuiContactModel,
    WechatGuiSnapshotModel,
    wechat_gui_message_dedup_key,
)
from packages.harness.channels.wechat_gui.wechat_gui_runner import (
    WechatGuiChannelRunner,
    WechatGuiMessageDedupWindow,
)
from tests.wechat_gui_fakes import (
    FakeWechatGuiClient,
    FakeWechatGuiReader,
    FakeWechatGuiRouter,
    SleepGate,
    make_message,
    make_read_result,
    make_screenshot,
)

_NOW_MS = 1_731_000_000_000


def _client(*, hashes: list[str]) -> FakeWechatGuiClient:
    """构造按截图 sha 序列返回的 fake 客户端。"""
    return FakeWechatGuiClient(screenshots=[make_screenshot(sha=sha) for sha in hashes])


def _runner(
    *,
    client: FakeWechatGuiClient,
    reader: FakeWechatGuiReader,
    router: Any,
    sleep: Any = None,
    contacts: dict[str, WechatGuiContactModel] | None = None,
    title_mismatch_action: TitleMismatchAction = TitleMismatchAction.SKIP,
    backoff_max_seconds: float = 60.0,
) -> WechatGuiChannelRunner:
    """构造测试 runner（显式参数 + fake 路由器——不读全局 Settings）。"""
    return WechatGuiChannelRunner(
        client=client,
        reader=reader,
        router=router,
        contacts=contacts or {"小林": WechatGuiContactModel(remark_name="小林")},
        poll_interval_seconds=0.5,
        backoff_base_seconds=2.0,
        backoff_max_seconds=backoff_max_seconds,
        max_messages_per_poll=20,
        title_mismatch_action=title_mismatch_action,
        clock=lambda: _NOW_MS,
        sleep=sleep,
    )


async def test_run_routes_messages_and_dedups_across_polls() -> None:
    """轮询：第一轮投新消息；第二轮同一消息被去重，只投新增。"""
    client = _client(hashes=["sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(messages=[make_message(text="你好")]),
            make_read_result(
                messages=[make_message(text="你好"), make_message(text="第二条")]
            ),
        ]
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [message.text for message in router.inbound] == ["你好", "第二条"]
    assert all(message.channel is ChannelType.WECHAT_GUI for message in router.inbound)
    assert client.open_calls == ["小林"]
    assert runner.dedup_size == 2
    assert runner.poll_count == 2


async def test_unchanged_screenshot_skips_vision_model() -> None:
    """截图未变化：不调用视觉模型（省调用 + 防重复解析）。"""
    client = _client(hashes=["sha-same", "sha-same"])
    reader = FakeWechatGuiReader([make_read_result(messages=[make_message(text="你好")])])
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert len(reader.calls) == 1
    assert runner.vlm_skipped_count == 1
    assert runner.poll_count == 1
    assert len(router.inbound) == 1


async def test_title_mismatch_default_skip_counts() -> None:
    """P4 校验不一致：默认 skip——不路由并累计计数。"""
    client = _client(hashes=["sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(title="别人", messages=[make_message(text="你好")]),
            make_read_result(title="小林", messages=[make_message(text="你好")]),
        ]
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert runner.title_mismatch_count == 1
    assert [message.text for message in router.inbound] == ["你好"]


async def test_title_mismatch_fail_triggers_backoff() -> None:
    """P4 校验处置为 fail：本轮失败进入退避（不路由）。"""
    client = _client(hashes=["sha-1", "sha-2", "sha-3"])
    reader = FakeWechatGuiReader(
        [make_read_result(title="别人", messages=[make_message()]) for _ in range(3)]
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=1)
    runner = _runner(
        client=client,
        reader=reader,
        router=router,
        sleep=gate,
        title_mismatch_action=TitleMismatchAction.FAIL,
    )
    gate.runner = runner
    await runner.run()
    assert gate.delays == [2.0]
    assert runner.consecutive_failures == 1
    assert runner.title_mismatch_count == 1
    assert router.inbound == []


async def test_per_target_failure_is_isolated_within_round() -> None:
    """逐联系人隔离：一个目标失败不阻断同轮其余目标；整轮失败触发退避。"""
    client = _client(hashes=["sha-a", "sha-b"])
    client.fail_screenshot = RuntimeError("沙盒不可达")
    reader = FakeWechatGuiReader([])
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=1)
    contacts = {
        "小林": WechatGuiContactModel(remark_name="小林"),
        "阿青": WechatGuiContactModel(remark_name="阿青"),
    }
    runner = _runner(
        client=client, reader=reader, router=router, sleep=gate, contacts=contacts
    )
    gate.runner = runner
    await runner.run()
    assert client.open_calls == ["小林", "阿青"]
    assert runner.consecutive_failures == 1


async def test_backoff_grows_and_caps_on_repeated_failures() -> None:
    """退避：连续失败指数增长且按封顶收敛。"""
    client = _client(hashes=["sha-1"])
    client.fail_screenshot = RuntimeError("沙盒不可达")
    gate = SleepGate(stop_after=3)
    runner = _runner(
        client=client,
        reader=FakeWechatGuiReader([]),
        router=FakeWechatGuiRouter(),
        sleep=gate,
        backoff_max_seconds=5.0,
    )
    gate.runner = runner
    await runner.run()
    assert gate.delays == [2.0, 4.0, 5.0]
    assert runner.consecutive_failures == 3


async def test_router_failure_is_isolated_and_message_retried() -> None:
    """单条路由异常不击穿轮询；失败消息不登记去重（下轮重试）。"""
    failing_text = "会失败"
    message = make_message(text=failing_text)
    expected_id = wechat_gui_message_dedup_key("小林", message, captured_at_ms=_NOW_MS)
    router = FakeWechatGuiRouter()
    router.failing_message_ids = {expected_id}
    client = _client(hashes=["sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(messages=[message, make_message(text="正常")]),
            make_read_result(messages=[message]),
        ]
    )
    gate = SleepGate(stop_after=2)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [item.text for item in router.inbound] == ["正常"]
    assert runner.dedup_size == 1


async def test_no_contacts_round_is_noop() -> None:
    """无启用联系人：本轮不采集（只按轮询节奏休眠）。"""
    client = _client(hashes=["sha-1"])
    gate = SleepGate(stop_after=1)
    runner = _runner(
        client=client,
        reader=FakeWechatGuiReader([]),
        router=FakeWechatGuiRouter(),
        sleep=gate,
        contacts={"小林": WechatGuiContactModel(remark_name="小林", enabled=False)},
    )
    gate.runner = runner
    await runner.run()
    assert runner.targets == ()
    assert client.screenshot_calls == 0
    assert gate.delays == [0.5]


async def test_stop_request_interrupts_polling() -> None:
    """停机：请求后 run 立即收口。"""
    runner = _runner(
        client=_client(hashes=["sha-1"]),
        reader=FakeWechatGuiReader([]),
        router=FakeWechatGuiRouter(),
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    runner.request_stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_run_is_single_instance() -> None:
    """单实例不变式：并发第二次 run 幂等返回（不发起额外采集）。"""
    client = _client(hashes=["sha-1", "sha-2"])
    runner = _runner(
        client=client,
        reader=FakeWechatGuiReader([make_read_result(), make_read_result()]),
        router=FakeWechatGuiRouter(),
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    await runner.run()
    runner.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert client.screenshot_calls == 1


async def test_runner_rejects_invalid_bounds() -> None:
    """构造不变式：轮询间隔 / 退避封顶 / 消息上限。"""
    client = _client(hashes=["sha-1"])
    reader = FakeWechatGuiReader([])
    router: Any = FakeWechatGuiRouter()
    contacts = {"小林": WechatGuiContactModel(remark_name="小林")}
    with pytest.raises(ValueError):
        WechatGuiChannelRunner(
            client=client,
            reader=reader,
            router=router,
            contacts=contacts,
            poll_interval_seconds=0.0,
        )
    with pytest.raises(ValueError):
        WechatGuiChannelRunner(
            client=client,
            reader=reader,
            router=router,
            contacts=contacts,
            backoff_base_seconds=10.0,
            backoff_max_seconds=5.0,
        )
    with pytest.raises(ValueError):
        WechatGuiChannelRunner(
            client=client,
            reader=reader,
            router=router,
            contacts=contacts,
            max_messages_per_poll=0,
        )


async def test_dedup_window_evicts_oldest() -> None:
    """去重窗口：超容按 FIFO 淘汰最旧键。"""
    window = WechatGuiMessageDedupWindow(capacity=2)
    window.remember("k-1")
    window.remember("k-2")
    window.remember("k-3")
    assert window.size == 2
    assert window.seen("k-1") is False
    assert window.seen("k-2") is True
    assert window.ids == frozenset({"k-2", "k-3"})


async def test_snapshot_model_used_for_dedup_window_semantics() -> None:
    """时间窗常量与快照字段协同（同文本跨窗产生不同去重键）。"""
    message = make_message(text="你好", ts_ms=_NOW_MS)
    snapshot = WechatGuiSnapshotModel(
        title="小林",
        messages=[message],
        screenshot_sha256="sha",
        captured_at_ms=_NOW_MS,
    )
    later = message.model_copy(update={"ts_ms": _NOW_MS + WECHAT_GUI_DEDUP_WINDOW_MS})
    assert wechat_gui_message_dedup_key(
        snapshot.title, message, captured_at_ms=snapshot.captured_at_ms
    ) != wechat_gui_message_dedup_key(
        snapshot.title, later, captured_at_ms=snapshot.captured_at_ms
    )
