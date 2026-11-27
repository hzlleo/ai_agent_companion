"""GUI 微信通道 runner 测试——轮询 / 变化检测 / P4 校验 / 会话水位 / 退避。

零真实网络 / 零真实等待：沙盒客户端与视觉解析器全部注入 fake；休眠经
记录式闸门在有限轮次内触发停机。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from packages.harness.channels.channel_models import ChannelType
from packages.harness.channels.wechat_gui.wechat_gui_models import (
    TitleMismatchAction,
    WechatGuiContactModel,
    WechatGuiWindowState,
)
from packages.harness.channels.wechat_gui.wechat_gui_runner import WechatGuiChannelRunner
from packages.harness.core.errors import BusinessException, ErrorCode
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
    window_state: WechatGuiWindowState | None = None,
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
        window_state=window_state,
    )


async def test_run_routes_messages_and_dedups_across_polls() -> None:
    """轮询：首帧建水位基线；下一帧只投递新增消息。"""
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
    assert [message.text for message in router.inbound] == ["第二条"]
    assert all(message.channel is ChannelType.WECHAT_GUI for message in router.inbound)
    assert client.open_calls == ["小林"]
    assert runner.watermark_targets == ("小林",)
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
    assert router.inbound == []


async def test_vlm_transient_failure_reparses_same_screenshot() -> None:
    """视觉解析瞬时失败不登记截图：同一画面下一轮重新解析（不增跳过计数）。"""
    client = _client(hashes=["sha-same", "sha-same"])
    reader = FakeWechatGuiReader(
        [make_read_result(messages=[make_message(text="你好")])],
        errors=[
            BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="视觉瞬时失败"),
            None,
        ],
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert len(reader.calls) == 2
    assert reader.calls[0] == reader.calls[1]
    assert runner.vlm_skipped_count == 0
    assert runner.consecutive_failures == 0


async def test_vlm_transient_failure_delivers_next_new_frame() -> None:
    """瞬时失败后恢复：同画面重建基线，随后新增消息正常投递。"""
    client = _client(hashes=["sha-1", "sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(messages=[make_message(text="你好")]),
            make_read_result(
                messages=[make_message(text="你好"), make_message(text="第二条")]
            ),
        ],
        errors=[
            BusinessException(code=ErrorCode.LLM_CALL_FAILED, message="视觉瞬时失败"),
            None,
            None,
        ],
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=3)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [message.text for message in router.inbound] == ["第二条"]
    assert runner.vlm_skipped_count == 0
    assert runner.poll_count == 3


async def test_title_mismatch_default_skip_counts_and_reopens() -> None:
    """P4 校验不一致：默认 skip 计数并复位目标——下一轮重新 open_chat 并解析。"""
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
    assert client.open_calls == ["小林", "小林"]
    assert len(reader.calls) == 2
    assert router.inbound == []


async def test_title_mismatch_fail_triggers_backoff_and_reopens() -> None:
    """P4 校验处置为 fail：退避 + 目标复位，下一轮重新 open_chat 并解析。"""
    client = _client(hashes=["sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(title="别人", messages=[make_message(text="你好")]),
            make_read_result(title="小林", messages=[make_message(text="你好")]),
        ]
    )
    router = FakeWechatGuiRouter()
    gate = SleepGate(stop_after=2)
    runner = _runner(
        client=client,
        reader=reader,
        router=router,
        sleep=gate,
        title_mismatch_action=TitleMismatchAction.FAIL,
    )
    gate.runner = runner
    await runner.run()
    assert gate.delays == [2.0, 0.5]
    assert runner.title_mismatch_count == 1
    assert client.open_calls == ["小林", "小林"]
    assert len(reader.calls) == 2
    assert router.inbound == []


async def test_shared_window_state_from_outbound_forces_reopen() -> None:
    """出站已切换过窗口：runner 不得按旧记忆跳过 open_chat。"""
    client = _client(hashes=["sha-1", "sha-2"])
    reader = FakeWechatGuiReader(
        [make_read_result(messages=[make_message()]) for _ in range(2)]
    )
    router = FakeWechatGuiRouter()
    window_state = WechatGuiWindowState()
    window_state.mark_open("阿青")
    gate = SleepGate(stop_after=1)
    runner = _runner(
        client=client,
        reader=reader,
        router=router,
        sleep=gate,
        window_state=window_state,
    )
    gate.runner = runner
    await runner.run()
    assert client.open_calls == ["小林"]


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


async def test_router_failure_is_isolated_without_replay() -> None:
    """单条路由失败不击穿轮询；水位照常前进（失败消息不重投）。"""
    baseline = make_message(text="基线")
    failing = make_message(text="会失败")
    healthy = make_message(text="正常")
    router = FakeWechatGuiRouter()
    router.failing_texts = {"会失败"}
    client = _client(hashes=["sha-1", "sha-2", "sha-3"])
    reader = FakeWechatGuiReader(
        [
            make_read_result(messages=[baseline]),
            make_read_result(messages=[baseline, failing, healthy]),
            make_read_result(messages=[baseline, failing, healthy]),
        ]
    )
    gate = SleepGate(stop_after=3)
    runner = _runner(client=client, reader=reader, router=router, sleep=gate)
    gate.runner = runner
    await runner.run()
    assert [item.text for item in router.inbound] == ["正常"]
    assert router.attempts == ["会失败", "正常"]
    assert runner.delivery_failure_count == 1
    assert runner.watermark_targets == ("小林",)


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
