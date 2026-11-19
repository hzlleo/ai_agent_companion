"""wecom 长连接客户端测试——fake 连接 + 记录式伪时钟（零真实网络 / 零真实等待）。

覆盖矩阵：

    - 构造期校验（BotID 必填 / 密钥环境变量缺失 fail loud / 密钥本值不落配置树）；
    - 订阅链路（wire 形状 / 订阅被拒 fail fast / 单连接不变式——重复 run 被忽略）；
    - 心跳（应用层 ping 间隔与应答超时判定——超时即关闭连接交 supervisor 重连）；
    - 退避重连（指数增长 + 抖动区间 + 成功订阅复位）；
    - 被踢下线（disconnected_event → KICKED 且停止重连）；
    - 命令面（应答超时 / 平台拒绝 / 未就绪 / 断连拒绝在途请求）；
    - 素材上传（单分片 / 多分片 wire 形状 / 空内容与超限拒绝 / 应答缺字段）；
    - 帧解析容错（非法 JSON / 非对象 / 未知 req_id 不击穿收帧循环）；
    - 回调派发（处理器异常隔离）与优雅停机（关闭连接 + STOPPED 状态）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from packages.harness.channels.wecom.wecom_client import (
    WeComConnectionState,
    WeComLongConnectionClient,
)
from packages.harness.channels.wecom.wecom_models import (
    WECOM_CMD_PING,
    WECOM_CMD_RESPOND_MSG,
    WECOM_CMD_SEND_MSG,
    WECOM_CMD_SUBSCRIBE,
    WECOM_CMD_UPLOAD_MEDIA_CHUNK,
    WECOM_CMD_UPLOAD_MEDIA_FINISH,
    WECOM_CMD_UPLOAD_MEDIA_INIT,
    WECOM_UPLOAD_CHUNK_SIZE_BYTES,
    WeComChatTargetModel,
    WeComMediaKind,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from tests.wecom_fakes import (
    FakeWeComConnection,
    FakeWeComConnector,
    RecordingSleep,
    make_ack,
    make_callback_frame,
    make_event_frame,
)

#: 测试用密钥环境变量名（隔离值——不触达真实机器人）。
_SECRET_ENV = "WECOM_TEST_SECRET"
#: 测试用 BotID。
_BOT_ID = "test-bot-id"


def _client(
    *,
    connector: FakeWeComConnector | None = None,
    sleep: RecordingSleep | None = None,
    heartbeat_interval_seconds: float = 30.0,
    heartbeat_timeout_seconds: float = 0.05,
    request_timeout_seconds: float = 0.05,
    backoff_jitter_ratio: float = 0.0,
) -> WeComLongConnectionClient:
    """构造测试客户端（fake 连接 / 记录式休眠——零真实网络）。

    Args:
        connector: 连接工厂替身（None = 新建）。
        sleep: 休眠替身（None = 新建）。
        heartbeat_interval_seconds: 心跳间隔。
        heartbeat_timeout_seconds: 心跳应答超时。
        request_timeout_seconds: 命令应答超时。
        backoff_jitter_ratio: 退避抖动比例（缺省 0 = 确定性延时）。

    Returns:
        配置好的客户端。
    """
    return WeComLongConnectionClient(
        bot_id=_BOT_ID,
        secret_env=_SECRET_ENV,
        endpoint="wss://fake.example.com",
        connector=connector if connector is not None else FakeWeComConnector(),
        sleep=sleep if sleep is not None else RecordingSleep(),
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        backoff_base_seconds=1.0,
        backoff_max_seconds=60.0,
        backoff_jitter_ratio=backoff_jitter_ratio,
    )


async def _wait_until(predicate: Any, *, timeout_seconds: float = 2.0) -> None:
    """让出控制点直到条件成立（有界等待——超时即失败）。

    等待面覆盖两类信号：fake 驱动的即时状态迁移（几次让出即成立）与真实
    超时驱动的迁移（心跳应超时——等待上限远小于平台间隔，不构成轮询式等待）。

    Args:
        predicate: 零参判定函数。
        timeout_seconds: 等待上限秒数（超出即断言失败）。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("条件在等待上限内未成立")


class TestConstruction:
    async def test_secret_missing_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """密钥环境变量缺失 → CHANNEL_TOKEN_MISSING（与 telegram token 缺失同语义）。"""
        monkeypatch.delenv(_SECRET_ENV, raising=False)
        with pytest.raises(BusinessException) as exc_info:
            _client()
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING
        assert _SECRET_ENV in exc_info.value.message

    async def test_empty_bot_id_rejected(self) -> None:
        """BotID 为空 → VALIDATION_ERROR（无身份无法订阅）。"""
        with pytest.raises(BusinessException) as exc_info:
            WeComLongConnectionClient(bot_id="  ", secret_env=_SECRET_ENV)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_observability_surface_exposes_no_secret(self) -> None:
        """观测面只暴露 BotID / 端点 / 密钥变量名——不含密钥本值。"""
        client = _client()
        assert client.bot_id == _BOT_ID
        assert client.endpoint == "wss://fake.example.com"
        assert client.secret_env == _SECRET_ENV
        assert client.state is WeComConnectionState.IDLE


class TestSubscribeAndLifecycle:
    async def test_subscribe_sent_with_credentials(self) -> None:
        """订阅命令携带 BotID + 密钥，成功后进入 READY。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            subscribe = connection.commands_for(WECOM_CMD_SUBSCRIBE)
            assert len(subscribe) == 1
            assert subscribe[0]["body"] == {
                "bot_id": _BOT_ID,
                "secret": "test-only-wecom-secret",
            }
            assert client.reconnect_attempt == 0
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_duplicate_run_ignored(self) -> None:
        """重复 run 不产生第二条连接（单连接不变式——官方：新连接会踢旧连接）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            await client.run()  # 第二次调用应立即返回
            assert connector.attempts == 1
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_subscribe_rejection_raises(self) -> None:
        """订阅被拒的错误码映射为 CHANNEL_TOKEN_MISSING（run 以异常终止）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)

        original_call = connector.__call__

        async def _connector_with_rejection(
            endpoint: str, open_timeout_seconds: float
        ) -> FakeWeComConnection:
            connection = await original_call(endpoint, open_timeout_seconds)
            connection.subscribe_errcode = 40001
            return connection

        client = WeComLongConnectionClient(
            bot_id=_BOT_ID,
            secret_env=_SECRET_ENV,
            endpoint="wss://fake.example.com",
            connector=_connector_with_rejection,
            sleep=RecordingSleep(),
        )
        with pytest.raises(BusinessException) as exc_info:
            await asyncio.wait_for(client.run(), timeout=2.0)
        assert exc_info.value.code == ErrorCode.CHANNEL_TOKEN_MISSING

    async def test_graceful_stop_closes_connection(self) -> None:
        """停机请求关闭连接并落到 STOPPED（run 正常返回）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        await _wait_until(lambda: client.state is WeComConnectionState.READY)
        client.request_stop()
        await asyncio.wait_for(task, timeout=2.0)
        connection = connector.connections[0]
        assert connection.closed is True
        assert client.state is WeComConnectionState.STOPPED
        assert client.stop_requested is True

    async def test_kicked_event_stops_reconnect(self) -> None:
        """disconnected_event → KICKED 且不再重连（事件门控等待——无轮询 /
        无固定墙钟窗口：连接产出 / 订阅完成 / 连接关闭三处均以显式事件
        同步，最终 await run 任务收尾）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(connector.connected_event.wait(), timeout=5.0)
        connection = connector.connections[0]
        await asyncio.wait_for(connection.subscribed_event.wait(), timeout=5.0)
        connection.push(make_event_frame("disconnected_event"))
        await asyncio.wait_for(connection.closed_event.wait(), timeout=5.0)
        await task
        assert client.state is WeComConnectionState.KICKED
        assert connector.attempts == 1


class TestHeartbeat:
    async def test_heartbeat_ping_sent_after_interval(self) -> None:
        """心跳按配置间隔发送应用层 ping（记录熬夜节奏断言，不按墙钟）。"""
        connector = FakeWeComConnector()
        sleep = RecordingSleep(gated=True)
        client = _client(connector=connector, sleep=sleep, heartbeat_interval_seconds=30.0)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            await _wait_until(lambda: 30.0 in sleep.delays)
            sleep.release_once()
            await _wait_until(lambda: len(connector.connections[0].commands_for(WECOM_CMD_PING)) == 1)
            assert sleep.delays[0] == 30.0
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_heartbeat_timeout_closes_connection(self) -> None:
        """心跳应答超时判定连接失效并主动关闭（交 supervisor 重连）。"""
        connector = FakeWeComConnector()
        sleep = RecordingSleep(gated=True)
        client = _client(connector=connector, sleep=sleep, heartbeat_timeout_seconds=0.05)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            first_connection = connector.connections[0]
            first_connection.silent_commands = {WECOM_CMD_PING}
            sleep.release_once()
            await _wait_until(lambda: first_connection.closed)
            assert first_connection.commands_for(WECOM_CMD_PING)
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestReconnectBackoff:
    async def test_backoff_grows_exponentially(self) -> None:
        """连续握手失败按指数退避（1s → 2s），成功订阅后复位。"""
        connector = FakeWeComConnector(failures_before_success=2)
        sleep = RecordingSleep(gated=True)
        client = _client(connector=connector, sleep=sleep)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: len(sleep.delays) == 1)
            assert sleep.delays[0] == pytest.approx(1.0)
            assert client.state is WeComConnectionState.BACKOFF
            sleep.release_once()
            await _wait_until(lambda: len(sleep.delays) == 2)
            assert sleep.delays[1] == pytest.approx(2.0)
            sleep.release_once()
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            assert client.reconnect_attempt == 0
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_backoff_jitter_within_bounds(self) -> None:
        """抖动后的退避落在 base ± base×ratio 区间内。"""
        connector = FakeWeComConnector(failures_before_success=1)
        sleep = RecordingSleep(gated=True)
        client = _client(connector=connector, sleep=sleep, backoff_jitter_ratio=0.3)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: len(sleep.delays) == 1)
            assert 0.7 <= sleep.delays[0] <= 1.3
            sleep.release_once()
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestCommandFace:
    async def test_not_ready_rejected(self) -> None:
        """未连接时发送命令 → CHANNEL_API_ERROR（不悬挂）。"""
        client = _client()
        target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
        with pytest.raises(BusinessException) as exc_info:
            await client.send_markdown_push(target=target, content="hi")
        assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR

    async def test_reply_and_push_success(self) -> None:
        """回复透传 req_id；推送携带 chatid + chat_type（应答成功即返回）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            await client.send_markdown_reply(callback_req_id="cb-9", content="回复")
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            await client.send_markdown_push(target=target, content="推送")
            connection = connector.connections[0]
            reply = connection.commands_for(WECOM_CMD_RESPOND_MSG)
            push = connection.commands_for(WECOM_CMD_SEND_MSG)
            assert reply[0]["headers"]["req_id"] == "cb-9"
            assert reply[0]["body"]["markdown"]["content"] == "回复"
            assert push[0]["body"]["chatid"] == "u1"
            assert push[0]["body"]["chat_type"] == 1
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_duplicate_pending_req_id_rejected(self) -> None:
        """同键在途重复 → 受控拒绝且原在途 future 未被替换（应答仍归位原请求）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector, request_timeout_seconds=1.0)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            connection.silent_commands = {WECOM_CMD_RESPOND_MSG}
            first = asyncio.create_task(
                client.send_markdown_reply(callback_req_id="cb-dup", content="第一条")
            )
            await _wait_until(
                lambda: len(connection.commands_for(WECOM_CMD_RESPOND_MSG)) == 1
            )
            with pytest.raises(BusinessException) as exc_info:
                await client.send_markdown_reply(callback_req_id="cb-dup", content="第二条")
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
            assert exc_info.value.message == "请求号在途重复，拒绝覆盖在途应答"
            assert exc_info.value.details == {"req_id": "cb-dup"}
            assert len(connection.commands_for(WECOM_CMD_RESPOND_MSG)) == 1
            assert first.done() is False
            connection.push(make_ack("cb-dup"))
            ack = await asyncio.wait_for(first, timeout=2.0)
            assert ack.errcode == 0
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_ack_timeout_rejected(self) -> None:
        """平台不应答 → 应答超时映射 CHANNEL_API_ERROR（不悬挂）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector, request_timeout_seconds=0.02)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            connection.silent_commands = {WECOM_CMD_SEND_MSG}
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            with pytest.raises(BusinessException) as exc_info:
                await client.send_markdown_push(target=target, content="超时")
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
            assert "超时" in exc_info.value.message
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_media_push_video_rejected(self) -> None:
        """附件推送不接受视频素材类型（无对应消息体）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            with pytest.raises(BusinessException) as exc_info:
                await client.send_media_push(
                    target=target, media_kind=WeComMediaKind.VIDEO, media_id="mid"
                )
            assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_platform_rejection_mapped(self) -> None:
        """平台非零 errcode → CHANNEL_API_ERROR（details 携带 errcode / errmsg）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connector.connections[0].command_errcodes = {WECOM_CMD_SEND_MSG: 93000}
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            with pytest.raises(BusinessException) as exc_info:
                await client.send_markdown_push(target=target, content="被拒")
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
            assert exc_info.value.details["errcode"] == 93000
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_connection_loss_rejects_pending(self) -> None:
        """断连拒结在途请求（不静默悬挂）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector, request_timeout_seconds=5.0)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            connection.silent_commands = {WECOM_CMD_SEND_MSG}
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            send_task = asyncio.create_task(
                client.send_markdown_push(target=target, content="断连")
            )
            await asyncio.sleep(0)
            connection.push_error(ConnectionError("fake 断连"))
            with pytest.raises(BusinessException) as exc_info:
                await asyncio.wait_for(send_task, timeout=2.0)
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestUploadMedia:
    async def test_single_chunk_upload(self) -> None:
        """单片上传：初始化 → 分片（base64 原文）→ 完成（返回 media_id）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            media_id = await client.upload_media(
                b"hello-image", media_kind=WeComMediaKind.IMAGE, filename="a.png"
            )
            assert media_id == "fake-media-id"
            connection = connector.connections[0]
            init_command = connection.commands_for(WECOM_CMD_UPLOAD_MEDIA_INIT)[0]
            chunk_command = connection.commands_for(WECOM_CMD_UPLOAD_MEDIA_CHUNK)[0]
            finish_command = connection.commands_for(WECOM_CMD_UPLOAD_MEDIA_FINISH)[0]
            assert init_command["body"]["type"] == "image"
            assert init_command["body"]["total_size"] == len(b"hello-image")
            assert init_command["body"]["total_chunks"] == 1
            assert base64.b64decode(chunk_command["body"]["base64_data"]) == b"hello-image"
            assert finish_command["body"]["upload_id"] == "fake-upload-id"
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_multi_chunk_upload_splits_at_chunk_limit(self) -> None:
        """超单片上限的内容按 512KB 切片（片数 = ceil(size / 512KB)）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        content = b"x" * (WECOM_UPLOAD_CHUNK_SIZE_BYTES + 10)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            await client.upload_media(content, media_kind=WeComMediaKind.FILE, filename="b.bin")
            chunks = connector.connections[0].commands_for(WECOM_CMD_UPLOAD_MEDIA_CHUNK)
            assert [chunk["body"]["chunk_index"] for chunk in chunks] == [0, 1]
            rebuilt = b"".join(
                base64.b64decode(chunk["body"]["base64_data"]) for chunk in chunks
            )
            assert rebuilt == content
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_too_small_content_rejected(self) -> None:
        """小于平台下限（5 字节）→ VALIDATION_ERROR（不发起上传）。"""
        client = _client()
        with pytest.raises(BusinessException) as exc_info:
            await client.upload_media(b"abcd", media_kind=WeComMediaKind.IMAGE, filename="a.png")
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert "5" in exc_info.value.message

    async def test_size_limit_rejected(self) -> None:
        """超出语音 2MB 上限 → VALIDATION_ERROR（不发起上传）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        oversized = b"v" * (2 * 1024 * 1024 + 1)
        with pytest.raises(BusinessException) as exc_info:
            await client.upload_media(
                oversized, media_kind=WeComMediaKind.VOICE, filename="voice.amr"
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR

    async def test_missing_media_id_rejected(self) -> None:
        """完成应答缺 media_id → CHANNEL_API_ERROR。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connector.connections[0].upload_finish_without_body = True
            with pytest.raises(BusinessException) as exc_info:
                await client.upload_media(
                    b"data!", media_kind=WeComMediaKind.FILE, filename="c.txt"
                )
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_missing_upload_id_rejected(self) -> None:
        """初始化应答缺 upload_id → CHANNEL_API_ERROR。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connector.connections[0].upload_init_without_body = True
            with pytest.raises(BusinessException) as exc_info:
                await client.upload_media(
                    b"data!", media_kind=WeComMediaKind.FILE, filename="c.txt"
                )
            assert exc_info.value.code == ErrorCode.CHANNEL_API_ERROR
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)


class TestFrameHandling:
    async def test_invalid_frames_ignored(self) -> None:
        """非法 JSON / 非对象 / 未知 req_id 应答均跳过且不击穿收帧循环。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            connection.push("not-json")
            connection.push("[1, 2, 3]")
            connection.push(json.dumps({"headers": {"req_id": "ghost"}, "errcode": 0}))
            connection.push(json.dumps({"headers": {"req_id": "x"}, "errcode": "bad"}))
            await asyncio.sleep(0)
            assert client.state is WeComConnectionState.READY
            target = WeComChatTargetModel(chat_type=1, target_id="u1", is_group=False)
            await client.send_markdown_push(target=target, content="仍然可用")
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_callback_dispatched_and_handler_failure_isolated(self) -> None:
        """回调帧派发给处理器；处理器异常不击穿收帧循环。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        frames: list[str] = []

        async def _handler(frame: Any) -> None:
            frames.append(frame.cmd)
            if len(frames) == 1:
                raise RuntimeError("fake 处理器失败")

        client.set_callback_handler(_handler)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connection = connector.connections[0]
            connection.push(
                make_callback_frame(
                    {
                        "msgid": "m1",
                        "chattype": "single",
                        "from": {"userid": "u1"},
                        "msgtype": "text",
                        "text": {"content": "hi"},
                    }
                )
            )
            connection.push(
                make_callback_frame(
                    {
                        "msgid": "m2",
                        "chattype": "single",
                        "from": {"userid": "u1"},
                        "msgtype": "text",
                        "text": {"content": "again"},
                    },
                    req_id="cb-req-2",
                )
            )
            await _wait_until(lambda: len(frames) == 2)
            assert frames == ["aibot_msg_callback", "aibot_msg_callback"]
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_callback_without_handler_ignored(self) -> None:
        """未登记处理器时回调帧仅丢弃（不抛）。"""
        connector = FakeWeComConnector()
        client = _client(connector=connector)
        task = asyncio.create_task(client.run())
        try:
            await _wait_until(lambda: client.state is WeComConnectionState.READY)
            connector.connections[0].push(
                make_callback_frame(
                    {
                        "msgid": "m9",
                        "chattype": "single",
                        "from": {"userid": "u1"},
                        "msgtype": "text",
                        "text": {"content": "hi"},
                    }
                )
            )
            await asyncio.sleep(0)
            assert client.state is WeComConnectionState.READY
        finally:
            client.request_stop()
            await asyncio.wait_for(task, timeout=2.0)
