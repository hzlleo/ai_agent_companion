"""telegram 通道 runner 测试——fake client / fake router / 注入休眠（零真实网络）。

覆盖矩阵：

    - 启动校验（getMe token 无效 fail fast / 暂态失败进入退避循环）；
    - offset 游标推进 + 批间传递 + JSON 状态文件原子持久化往返；
    - update 去重（同 update_id 重投只路由一次）+ 非文本更新跳过仍推进；
    - 失败退避（指数 + 封顶 + 成功复位）；
    - 优雅停机（request_stop → 循环退出）与发送面适配（Outbound 投递）；
    - 状态文件损坏 / 写失败降级（runner 不死）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from packages.harness.channels.channel_models import (
    ChannelDeliveryResultModel,
    ChannelType,
    InboundMessageModel,
    OutboundMessageModel,
)
from packages.harness.channels.telegram import tg_runner as tg_runner_module
from packages.harness.channels.telegram.tg_models import (
    TgBotInfoModel,
    TgChatModel,
    TgMessageModel,
    TgSendMessageResult,
    TgUpdateModel,
    TgUserModel,
)
from packages.harness.channels.telegram.tg_runner import (
    TG_OFFSET_STATE_FILENAME,
    TelegramChannelRunner,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _update(
    update_id: int,
    *,
    text: str | None = "你好",
    chat_id: int = 10086,
) -> TgUpdateModel:
    """构造 message update（测试直供形态）。

    Args:
        update_id: 更新 ID。
        text: 消息文本（None = 非文本消息）。
        chat_id: 会话 ID。

    Returns:
        :class:`TgUpdateModel` 实例。
    """
    return TgUpdateModel(
        update_id=update_id,
        message=TgMessageModel(
            message_id=update_id * 10,
            chat=TgChatModel(id=chat_id, type="private"),
            from_user=TgUserModel(id=7),
            text=text,
            date=1731000000,
        ),
    )


class FakeTgClient:
    """fake TelegramClient——脚本化批次 / 可编程失败 / 记录调用形状。"""

    def __init__(
        self,
        *,
        batches: list[list[TgUpdateModel]] | None = None,
        fail_first_polls: int = 0,
        get_me_error: Exception | None = None,
    ) -> None:
        self.polls: list[int | None] = []
        self._batches = list(batches or [])
        self._fail_first = fail_first_polls
        self.get_me_error = get_me_error
        self.sent: list[dict[str, Any]] = []

    async def get_me(self) -> TgBotInfoModel:
        """fake getMe（可编程抛错）。"""
        if self.get_me_error is not None:
            raise self.get_me_error
        return TgBotInfoModel(id=42, username="companion_bot", first_name="C")

    async def get_updates(self, *, offset: int | None = None) -> list[TgUpdateModel]:
        """fake getUpdates（脚本批次弹尽后恒空 + 可编程连续失败）。"""
        await asyncio.sleep(0)
        if self._fail_first > 0:
            self._fail_first -= 1
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="轮询失败（测试注入）"
            )
        self.polls.append(offset)
        if self._batches:
            return self._batches.pop(0)
        return []

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | str | None = None,
    ) -> list[TgSendMessageResult]:
        """fake send_message（记录出站形状）。"""
        self.sent.append(
            {"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id}
        )
        return [TgSendMessageResult(message_id=900, chat=TgChatModel(id=1, type="private"))]


class FakeRouter:
    """fake ChannelRouter——记录入站消息。"""

    def __init__(self) -> None:
        self.handled: list[InboundMessageModel] = []

    async def handle_inbound(self, message: InboundMessageModel) -> ChannelDeliveryResultModel:
        """fake 路由（记录 + 恒成功投递形态）。"""
        await asyncio.sleep(0)
        self.handled.append(message)
        return ChannelDeliveryResultModel(
            channel=message.channel, chat_id=message.chat_id, message_ids=["900"]
        )


class RecordingSleep:
    """注入休眠——记录退避节奏（不真实等待；可编程在累计 N 次后请求停机）。"""

    def __init__(self, stop_after_delays: int | None = None) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after_delays
        self._stop_target: list[TelegramChannelRunner] = []

    def bind_runner(self, runner: TelegramChannelRunner) -> None:
        """绑定停机目标（runner 构造后回填——闭包持有引用）。"""
        self._stop_target.append(runner)

    async def __call__(self, delay: float) -> None:
        """记录延时并让出控制点（测试驱动节奏断言）。"""
        self.delays.append(delay)
        if self._stop_after is not None and len(self.delays) >= self._stop_after:
            for runner in self._stop_target:
                runner.request_stop()
        await asyncio.sleep(0)


def _runner(
    client: FakeTgClient,
    router: FakeRouter,
    *,
    state_dir: Path,
    sleep: RecordingSleep | None = None,
    backoff_base_seconds: float = 1.0,
    backoff_max_seconds: float = 60.0,
) -> TelegramChannelRunner:
    """构造被测 runner（fake client / fake router / 注入休眠）。"""
    return TelegramChannelRunner(
        client=client,
        router=router,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
        state_dir=str(state_dir),
        sleep=sleep if sleep is not None else RecordingSleep(),
    )


async def _run_until(
    task: asyncio.Task[None], predicate: Any, *, timeout: float = 5.0
) -> None:
    """等待谓词成立（真实时钟有界轮询——线程池往返需真实毫秒）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if task.done():
            return
        assert loop.time() < deadline, "等待 runner 推进超时（测试驱动死锁）"
        await asyncio.sleep(0.01)


class TestStartupValidation:
    async def test_invalid_token_fails_fast(self, tmp_path: Path) -> None:
        """getMe token 无效：run() 即抛 CHANNEL_TOKEN_MISSING（fail fast）。"""
        client = FakeTgClient(
            get_me_error=BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING, message="Telegram bot token 无效"
            )
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        with pytest.raises(BusinessException) as exc_info:
            await runner.run()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
        assert client.polls == []
        assert router.handled == []

    async def test_transient_get_me_failure_continues(self, tmp_path: Path) -> None:
        """getMe 暂态失败（非 token 类）：告警后进入轮询循环（不终止）。"""
        client = FakeTgClient(
            get_me_error=BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR, message="暂不可达（测试注入）"
            ),
            batches=[[_update(1)]],
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert len(router.handled) == 1
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestOffsetHandling:
    async def test_offset_advances_and_persists(self, tmp_path: Path) -> None:
        """offset 推进：批内最大 update_id + 1，并原子落盘状态文件。"""
        client = FakeTgClient(batches=[[_update(5), _update(8)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert client.polls[0] is None
            assert all(offset == 9 for offset in client.polls[1:])
            assert runner.offset == 9
            state_file = tmp_path / TG_OFFSET_STATE_FILENAME
            assert state_file.is_file()
            assert json.loads(state_file.read_text(encoding="utf-8")) == {"offset": 9}
            assert list(tmp_path.glob(f"{TG_OFFSET_STATE_FILENAME}.tmp-*")) == []
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_persisted_offset_round_trip(self, tmp_path: Path) -> None:
        """持久化往返：重启后首个 getUpdates 携带已持久化 offset（不重消费）。"""
        first_client = FakeTgClient(batches=[[_update(5)]])
        first_runner = _runner(first_client, FakeRouter(), state_dir=tmp_path)
        first_task = asyncio.create_task(first_runner.run())
        await _run_until(first_task, lambda: len(first_client.polls) >= 1)
        first_runner.request_stop()
        await asyncio.wait_for(first_task, timeout=5.0)

        second_client = FakeTgClient()
        second_runner = _runner(second_client, FakeRouter(), state_dir=tmp_path)
        second_task = asyncio.create_task(second_runner.run())
        try:
            await _run_until(second_task, lambda: len(second_client.polls) >= 1)
            assert second_client.polls[0] == 6
            assert all(offset == 6 for offset in second_client.polls)
        finally:
            second_runner.request_stop()
            await asyncio.wait_for(second_task, timeout=5.0)

    async def test_corrupt_state_file_degrades_to_first_poll(
        self, tmp_path: Path
    ) -> None:
        """状态文件损坏：降级为首次拉取（offset=None——告警不死）。"""
        state_file = tmp_path / TG_OFFSET_STATE_FILENAME
        state_file.write_text("not-json{", encoding="utf-8")
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert client.polls[0] is None
            assert runner.offset is None
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestUpdateProcessing:
    async def test_duplicate_update_routed_once(self, tmp_path: Path) -> None:
        """去重：同 update_id 重投（服务端未确认形态）只路由一次。"""
        client = FakeTgClient(batches=[[_update(5)], [_update(5), _update(6)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 2)
            assert [message.text for message in router.handled] == ["你好", "你好"]
            assert runner.offset == 7
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_non_text_updates_skipped_but_offset_advances(
        self, tmp_path: Path
    ) -> None:
        """非文本更新（无 message / 无 text）：跳过路由但 offset 照常推进。"""
        client = FakeTgClient(
            batches=[
                [
                    TgUpdateModel(update_id=1),
                    _update(2, text=None),
                    _update(3),
                ]
            ]
        )
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert len(router.handled) == 1
            assert router.handled[0].message_id == "30"
            assert runner.offset == 4
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_inbound_mapping_normalizes_ids_and_ts(self, tmp_path: Path) -> None:
        """入站映射：ID 字符串化 + epoch 秒 → 毫秒归一。"""
        client = FakeTgClient(batches=[[_update(5, chat_id=20000)]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 1)
            message = router.handled[0]
            assert message.channel is ChannelType.TELEGRAM
            assert message.chat_id == "20000"
            assert message.sender_id == "7"
            assert message.ts_ms == 1731000000 * 1000
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_anonymous_sender_normalized_zero(self, tmp_path: Path) -> None:
        """匿名发送者（from 缺失——频道帖子形态）：归一 "0"。"""
        update = TgUpdateModel(
            update_id=1,
            message=TgMessageModel(
                message_id=10,
                chat=TgChatModel(id=100, type="channel"),
                from_user=None,
                text="频道帖",
                date=1731000000,
            ),
        )
        client = FakeTgClient(batches=[[update]])
        router = FakeRouter()
        runner = _runner(client, router, state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(router.handled) >= 1)
            assert router.handled[0].sender_id == "0"
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestBackoff:
    async def test_exponential_backoff_capped(self, tmp_path: Path) -> None:
        """退避节奏：基数指数 → 封顶（2, 4, 8, 8）；恢复成功后不再退避。"""
        client = FakeTgClient(batches=[[_update(1)]], fail_first_polls=4)
        sleep = RecordingSleep()
        runner = _runner(
            client,
            FakeRouter(),
            state_dir=tmp_path,
            sleep=sleep,
            backoff_base_seconds=2.0,
            backoff_max_seconds=8.0,
        )
        task = asyncio.create_task(runner.run())
        try:
            # 恢复成功的首个 poll 落地后，失败计数已冻结为 4（后续无退避）
            await _run_until(task, lambda: len(client.polls) >= 1)
            assert sleep.delays == [2.0, 4.0, 8.0, 8.0]
            assert client.polls[0] is None
            assert all(offset == 2 for offset in client.polls[1:])
            assert runner.offset == 2
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_repeated_failures_keep_polling(self, tmp_path: Path) -> None:
        """连续失败不终止循环（runner 不死——退避后继续轮询）。"""
        client = FakeTgClient(fail_first_polls=100)
        sleep = RecordingSleep(stop_after_delays=3)
        runner = _runner(client, FakeRouter(), state_dir=tmp_path, sleep=sleep)
        sleep.bind_runner(runner)
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(task, timeout=5.0)
        assert sleep.delays == [1.0, 2.0, 4.0]
        assert task.exception() is None
        assert client.polls == []


class TestGracefulStop:
    async def test_request_stop_exits_loop_cleanly(self, tmp_path: Path) -> None:
        """优雅停机：request_stop → 循环退出（任务正常完成无异常）。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        task = asyncio.create_task(runner.run())
        await _run_until(task, lambda: len(client.polls) >= 1)
        assert runner.stop_requested is False
        runner.request_stop()
        await asyncio.wait_for(task, timeout=5.0)
        assert task.exception() is None
        assert runner.stop_requested is True

    async def test_offset_persist_failure_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """offset 写失败降级：告警继续（runner 不死；内存 offset 已推进）。"""
        client = FakeTgClient(batches=[[_update(5)]])
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)

        def _explode(func: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(func, "__name__", "") == "_write_blocking":
                raise OSError("disk full (test injected)")
            return asyncio.get_running_loop().run_in_executor(
                None, lambda: func(*args, **kwargs)
            )

        monkeypatch.setattr(tg_runner_module, "run_blocking_ops", _explode)
        task = asyncio.create_task(runner.run())
        try:
            await _run_until(task, lambda: len(client.polls) >= 2)
            assert runner.offset == 6
            assert not (tmp_path / TG_OFFSET_STATE_FILENAME).is_file()
        finally:
            runner.request_stop()
            await asyncio.wait_for(task, timeout=5.0)


class TestSendAdapterFace:
    async def test_send_message_maps_delivery_result(self, tmp_path: Path) -> None:
        """发送面适配：Outbound → client 调用形状 → 投递结果归一。"""
        client = FakeTgClient()
        runner = _runner(client, FakeRouter(), state_dir=tmp_path)
        result = await runner.send_message(
            OutboundMessageModel(chat_id="10086", text="回复", reply_to_message_id="42")
        )
        assert client.sent == [
            {"chat_id": "10086", "text": "回复", "reply_to": "42"}
        ]
        assert result.channel is ChannelType.TELEGRAM
        assert result.chat_id == "10086"
        assert result.message_ids == ["900"]
