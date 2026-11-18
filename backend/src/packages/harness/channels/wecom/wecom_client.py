"""wecom 通道长连接客户端——企业微信「智能机器人」WebSocket 会话生命周期与命令面。

协议依据：企业微信开发者中心《智能机器人长连接》
（https://developer.work.weixin.qq.com/document/path/101463）。连接、订阅、
心跳、回复、主动推送与临时素材上传全部在该长连接上完成——不经
``/cgi-bin/gettoken`` 的 HTTP 面，凭据只有 **BotID + Secret**（官方文档
「获取凭证」；Secret 为长连接专用密钥，与回调 Token / EncodingAESKey 不同）。

生命周期（状态机——``WeComConnectionState``）::

    连接 → 订阅（aibot_subscribe：BotID + Secret 校验）
        ├─ 订阅被拒（errcode != 0）→ CHANNEL_TOKEN_MISSING（fail fast：
        │   凭据 / 机器人身份错误，重试无意义——run() 以异常终止）
        └─ 订阅通过 → READY（心跳任务启动 + 回调帧接管）
              ├─ 收到 disconnected_event（新连接踢掉本连接）→ KICKED
              │   （停止重连：继续重连会与新连接互相踢——官方建议主备切换）
              ├─ 心跳失败 / 收帧异常 → 断开 → 指数退避 + 抖动后重连
              └─ request_stop() → 关闭连接 → STOPPED（run() 正常返回）

单连接不重复（官方文档：每个机器人同一时间只能保持一个有效长连接，新连接
完成订阅会踢掉旧连接）：``run()`` 以在途任务句柄保证幂等——重复调用不产生
第二条连接（记 warning 后直接返回），连接与心跳任务的生命周期均归本对象。

请求 / 应答关联：每条出站命令携带自生成 ``req_id``，应答按 ``req_id`` 归位
（``_pending`` 表）；超时（``request_timeout_seconds``）与连接中断都会以
``CHANNEL_API_ERROR`` 拒绝在途请求，不静默悬挂。

心跳（官方文档：建议每 30 秒 ping）：独立任务按
``heartbeat_interval_seconds`` 发送应用层 ``ping``，单次应答超时
``heartbeat_timeout_seconds`` 即判定连接失效并主动关闭（由 supervisor 走
退避重连）；心跳失败不重试同一连接（避免半死连接持续吞消息）。

连接传输面经 ``connector`` 注入（生产实现 = ``websockets`` 客户端；测试注入
进程内 fake 连接，零真实网络）。``websockets`` 由声明的 ``uvicorn[standard]``
运行依赖提供（其 standard extra 含 websockets）——本切片不新增第三方依赖。

素材上传（官方文档「上传临时素材」）：初始化 → 分片（单片 ≤512KB 明文、
≤100 片）→ 完成，返回 ``media_id``；客户端侧按素材类型拒绝超限（图片
10MB / 语音 2MB / 视频 10MB / 普通文件 20MB），上传会话 30 分钟有效由平台
侧保证，重连后未过期的会话仍可续传（本实现每次上传独立会话，不跨连接续传）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from enum import StrEnum
from typing import Any, Final, Protocol

from loguru import logger
from pydantic import ValidationError
from websockets.asyncio.client import connect

from packages.harness.channels.wecom.wecom_models import (
    WECOM_CALLBACK_COMMANDS,
    WECOM_DEFAULT_WS_ENDPOINT,
    WECOM_EVENT_DISCONNECTED,
    WECOM_UPLOAD_CHUNK_SIZE_BYTES,
    WECOM_UPLOAD_MAX_CHUNKS,
    WECOM_UPLOAD_MIN_SIZE_BYTES,
    WeComAckModel,
    WeComCallbackFrameModel,
    WeComChatTargetModel,
    WeComCommandModel,
    WeComMediaKind,
    build_markdown_push_command,
    build_markdown_reply_command,
    build_media_push_command,
    build_ping_command,
    build_subscribe_command,
    build_upload_chunk_command,
    build_upload_finish_command,
    build_upload_init_command,
    wecom_upload_size_limit,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WeComConnectionProtocol",
    "WeComConnectionState",
    "WeComConnector",
    "WeComLongConnectionClient",
    "DEFAULT_WECOM_SECRET_ENV",
    "open_websocket_connection",
]

#: 长连接专用密钥的缺省环境变量名（``secret_env`` 未覆写时使用）。
DEFAULT_WECOM_SECRET_ENV: str = "WECOM_SECRET"

#: 单次应答的 req_id 生成前缀（观测面可读——与平台回调 req_id 区分）。
_REQ_ID_PREFIX: Final[str] = "req-"


class WeComConnectionState(StrEnum):
    """长连接状态值域（观测面 / 装配校验消费）。

    Attributes:
        IDLE: 未建立过连接。
        CONNECTING: WebSocket 握手 / 订阅鉴权进行中。
        READY: 订阅通过，回调接管中。
        BACKOFF: 断开后退避等待重连。
        KICKED: 被新连接踢下线（停止重连）。
        STOPPED: 已优雅停机。
    """

    IDLE = "idle"
    CONNECTING = "connecting"
    READY = "ready"
    BACKOFF = "backoff"
    KICKED = "kicked"
    STOPPED = "stopped"


class WeComConnectionProtocol(Protocol):
    """长连接传输面契约（生产实现 = websockets 客户端；测试 = 进程内 fake）。

    结构化类型：实现以下三方法即满足（无需继承）。
    """

    async def send(self, payload: str) -> None:
        """发送一帧文本载荷（JSON 字符串）。

        Args:
            payload: 待发送的 JSON 文本。
        """
        ...

    async def recv(self) -> str | bytes:
        """接收一帧载荷（文本 / 二进制）。

        Returns:
            收到的原始帧（文本或二进制）。

        Raises:
            Exception: 连接断开（任何收帧失败都视为连接失效——调用方断开重连）。
        """
        ...

    async def close(self) -> None:
        """关闭连接（幂等——重复关闭不抛）。"""
        ...


#: 连接工厂契约（端点 + 握手超时 → 已建立的连接）。
WeComConnector = Callable[[str, float], Awaitable[WeComConnectionProtocol]]

#: 可注入休眠契约（测试注入 fake 记录退避节奏 / 心跳间隔——不真实等待）。
#: 返回协程而非任意 awaitable：退避等待需要作为任务与停机事件竞速。
SleepFn = Callable[[float], Coroutine[Any, Any, None]]


async def open_websocket_connection(
    endpoint: str, open_timeout_seconds: float
) -> WeComConnectionProtocol:
    """生产连接工厂（websockets 客户端——握手超时 + 帧上限）。

    关闭 websockets 自带的保活 ping（``ping_interval=None``）：本协议以应用层
    ``{"cmd": "ping"}`` 命令保活，传输层 ping 会与平台协议语义混淆。

    Args:
        endpoint: WebSocket 端点（wss / ws）。
        open_timeout_seconds: 握手超时秒数。

    Returns:
        已建立的连接（满足 :class:`WeComConnectionProtocol`）。

    Raises:
        OSError: 握手失败（DNS / TCP / TLS / HTTP 升级被拒——由 supervisor 退避重连）。
    """
    return await connect(
        endpoint,
        open_timeout=open_timeout_seconds,
        ping_interval=None,
        max_size=8 * 1024 * 1024,
    )


class WeComLongConnectionClient:
    """企微智能机器人长连接客户端（消息面 + 素材上传 + 连接状态机）。

    生命周期归 :meth:`run` / :meth:`request_stop`；命令面方法（
    :meth:`send_markdown_reply` / :meth:`send_markdown_push` /
    :meth:`send_media_push` / :meth:`upload_media`）在 READY 态可用，未就绪或
    应答失败一律 ``CHANNEL_API_ERROR`` 上抛（调用方按通道错误语义映射）。
    """

    def __init__(
        self,
        *,
        bot_id: str,
        secret_env: str = DEFAULT_WECOM_SECRET_ENV,
        endpoint: str = WECOM_DEFAULT_WS_ENDPOINT,
        request_timeout_seconds: float = 15.0,
        heartbeat_interval_seconds: float = 30.0,
        heartbeat_timeout_seconds: float = 10.0,
        open_timeout_seconds: float = 10.0,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        backoff_jitter_ratio: float = 0.3,
        connector: WeComConnector | None = None,
        sleep: SleepFn = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """初始化客户端（密钥在构造期从环境变量解析——缺失即 fail loud）。

        Args:
            bot_id: 智能机器人 BotID。
            secret_env: 长连接专用密钥的环境变量名（密钥永不落配置树——
                与 telegram ``bot_token_env`` / QQ ``access_token_env`` 同模式）。
            endpoint: WebSocket 端点。
            request_timeout_seconds: 单条命令的应答超时秒数。
            heartbeat_interval_seconds: 应用层心跳间隔秒数（官方建议 30）。
            heartbeat_timeout_seconds: 心跳应答超时秒数（超时即判定连接失效）。
            open_timeout_seconds: 握手超时秒数。
            backoff_base_seconds: 重连退避基数秒（指数起点）。
            backoff_max_seconds: 重连退避封顶秒数。
            backoff_jitter_ratio: 退避抖动幅度比例（0 = 无抖动；取值 0-1，
                实际延时在 ``base ± base × ratio`` 区间内随机）。
            connector: 连接工厂（None = websockets 生产实现）。
            sleep: 休眠函数（退避与心跳等待共用；测试注入 fake 断言节奏）。
            rng: 随机源（抖动采样；None = 新建 ``random.Random``）。

        Raises:
            BusinessException: BotID 为空（``VALIDATION_ERROR``）或密钥环境变量
                缺失 / 空白（``CHANNEL_TOKEN_MISSING``——与 telegram token
                缺失同语义：启用即必须可鉴权）。
        """
        if not bot_id.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="企微长连接启用但 WECOM_BOT_ID 为空（智能机器人 BotID 必填）",
            )
        secret = os.environ.get(secret_env, "").strip()
        if not secret:
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message=(
                    "企微长连接专用密钥缺失：环境变量 "
                    f"{secret_env} 为空（智能机器人配置页「获取凭证」的 Secret）"
                ),
                details={"secret_env": secret_env},
            )
        self._bot_id = bot_id.strip()
        self._secret = secret
        self._secret_env = secret_env
        self._endpoint = endpoint
        self._request_timeout_seconds = request_timeout_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._open_timeout_seconds = open_timeout_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._backoff_jitter_ratio = min(max(backoff_jitter_ratio, 0.0), 1.0)
        self._connector: WeComConnector = (
            connector if connector is not None else open_websocket_connection
        )
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._stop_event = asyncio.Event()
        self._state = WeComConnectionState.IDLE
        self._connection: WeComConnectionProtocol | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[WeComAckModel]] = {}
        self._callback_handler: Callable[[WeComCallbackFrameModel], Awaitable[None]] | None = None
        self._reconnect_attempt = 0

    # ------------------------------------------------------------------
    # 装配与观测面
    # ------------------------------------------------------------------

    def set_callback_handler(
        self, handler: Callable[[WeComCallbackFrameModel], Awaitable[None]]
    ) -> None:
        """注册回调帧处理器（入站消息 / 事件——须立即返回，不得阻塞收帧循环）。

        Args:
            handler: 回调帧异步处理器（runner 侧入队后立即返回）。
        """
        self._callback_handler = handler

    @property
    def state(self) -> WeComConnectionState:
        """当前连接状态（观测面）。"""
        return self._state

    @property
    def secret_env(self) -> str:
        """密钥环境变量名（装配观测面——不含密钥本值）。"""
        return self._secret_env

    @property
    def bot_id(self) -> str:
        """智能机器人 BotID（装配观测面）。"""
        return self._bot_id

    @property
    def endpoint(self) -> str:
        """WebSocket 端点（装配观测面）。"""
        return self._endpoint

    @property
    def reconnect_attempt(self) -> int:
        """当前连续重连次数（成功订阅即复位；观测面）。"""
        return self._reconnect_attempt

    def request_stop(self) -> None:
        """请求优雅停机（置于停机事件；在途收帧被中断、连接随后关闭）。"""
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        """是否已请求停机。"""
        return self._stop_event.is_set()

    # ------------------------------------------------------------------
    # supervisor（连接 / 订阅 / 心跳 / 退避重连）
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """长连接 supervisor（由 app lifespan 作后台任务驱动，单实例单连接）。

        Raises:
            BusinessException: 订阅被拒（``CHANNEL_TOKEN_MISSING``——凭据 /
                机器人身份错误，fail fast 终止任务）；``asyncio.CancelledError``
                原样上抛（停机 cancel 路径）。其余失败一律退避重连，不让任务死亡。
        """
        if self._run_task is not None and not self._run_task.done():
            logger.warning(
                "企微长连接已有一个 supervisor 在运行（重复 run 调用被忽略——单连接不变式）"
            )
            return
        self._run_task = asyncio.current_task()
        logger.info(
            "企微长连接 supervisor 启动: endpoint={}, bot_id={}",
            self._endpoint,
            self._bot_id,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_and_serve()
                except asyncio.CancelledError:
                    raise
                except BusinessException:
                    # 订阅被拒属配置面确定性失败——重试无意义，任务带清晰错误终止。
                    raise
                except Exception as exc:  # 断开 / 传输失败——退避重连
                    if self._stop_event.is_set():
                        break
                    self._reconnect_attempt += 1
                    delay = self._backoff_delay(self._reconnect_attempt)
                    self._state = WeComConnectionState.BACKOFF
                    logger.warning(
                        "企微长连接断开（连续 {} 次，退避 {:.1f}s 后重连）: {}",
                        self._reconnect_attempt,
                        delay,
                        exc,
                    )
                    await self._sleep_or_stop(delay)
        finally:
            if (
                self._stop_event.is_set()
                and self._state is not WeComConnectionState.KICKED
            ):
                self._state = WeComConnectionState.STOPPED
            self._run_task = None
            logger.info(
                "企微长连接 supervisor 已停止: state={}, attempts={}",
                self._state.value,
                self._reconnect_attempt,
            )

    async def _connect_and_serve(self) -> None:
        """建立一次连接并服务到断开（订阅 → 心跳 → 收帧）。

        Raises:
            BusinessException: 订阅被拒（``CHANNEL_TOKEN_MISSING``）。
            Exception: 握手 / 收帧失败（supervisor 退避重连面）。
        """
        self._state = WeComConnectionState.CONNECTING
        logger.info(
            "企微长连接正在建立: endpoint={}, attempt={}",
            self._endpoint,
            self._reconnect_attempt,
        )
        connection = await self._connector(self._endpoint, self._open_timeout_seconds)
        self._connection = connection
        heartbeat_task: asyncio.Task[None] | None = None
        # 收帧循环先于订阅启动：订阅应答（以及任何早到帧）都经同一收帧路径归位，
        # 否则订阅会等待一个尚未开始消费的应答（自锁到超时）。
        receive_task = asyncio.create_task(
            self._receive_loop(connection), name="wecom-receive"
        )
        try:
            await self._subscribe(connection)
            self._state = WeComConnectionState.READY
            self._reconnect_attempt = 0
            logger.info(
                "企微长连接已就绪（订阅通过，回调接管）: bot_id={}", self._bot_id
            )
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="wecom-heartbeat"
            )
            await receive_task
        finally:
            for task in (heartbeat_task, receive_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (heartbeat_task, receive_task):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            self._connection = None
            self._fail_pending("企微长连接已关闭")
            try:
                await connection.close()
            except Exception:  # 关闭失败不掩盖原始断连原因
                logger.opt(exception=True).debug("企微长连接关闭时出现异常（已忽略）")

    async def _subscribe(self, connection: WeComConnectionProtocol) -> None:
        """发送订阅鉴权命令（BotID + Secret；被拒即 fail fast）。

        Args:
            connection: 已建立的连接（订阅请求经该连接发送）。

        Raises:
            BusinessException: 订阅被拒（``CHANNEL_TOKEN_MISSING``）。
        """
        command = build_subscribe_command(
            req_id=self._next_req_id(), bot_id=self._bot_id, secret=self._secret
        )
        try:
            await self._execute(
                command,
                connection=connection,
                error_code=ErrorCode.CHANNEL_TOKEN_MISSING,
            )
        except BusinessException as exc:
            logger.error("企微长连接订阅被拒（凭据 / 机器人身份无效）: {}", exc.message)
            raise
        logger.info("企微长连接订阅成功: bot_id={}", self._bot_id)

    async def _heartbeat_loop(self) -> None:
        """应用层心跳（间隔 ping；应答超时即关闭连接交由 supervisor 重连）。"""
        while not self._stop_event.is_set():
            await self._sleep_or_stop(self._heartbeat_interval_seconds)
            if self._stop_event.is_set():
                return
            try:
                await self._execute(
                    build_ping_command(req_id=self._next_req_id()),
                    timeout=self._heartbeat_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except BusinessException as exc:
                logger.warning("企微长连接心跳失败（判定连接失效）: {}", exc.message)
                connection = self._connection
                if connection is not None:
                    await self._close_connection(connection)
                return

    async def _receive_loop(self, connection: WeComConnectionProtocol) -> None:
        """收帧循环（回调帧派发 / 应答归位；停机或断连即返回）。

        Args:
            connection: 正在服务的连接。

        Raises:
            Exception: 收帧失败（断连——由 supervisor 退避重连）。
        """
        while not self._stop_event.is_set():
            raw = await self._recv_frame_or_stop(connection)
            if raw is None:
                return
            frame = self._parse_frame(raw)
            if frame is None:
                continue
            if isinstance(frame, WeComCallbackFrameModel):
                await self._dispatch_callback(frame)
            else:
                self._resolve_ack(frame)

    async def _recv_frame_or_stop(self, connection: WeComConnectionProtocol) -> str | bytes | None:
        """收一帧或在停机请求到达时立即返回（不悬挂在 recv 上）。

        Args:
            connection: 正在服务的连接。

        Returns:
            收到的原始帧；停机请求先到时为 None。

        Raises:
            Exception: 收帧失败（断连）。
        """
        recv_task = asyncio.create_task(connection.recv())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {recv_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (recv_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(recv_task, stop_task, return_exceptions=True)
        if recv_task in done:
            return recv_task.result()
        return None

    async def _dispatch_callback(self, frame: WeComCallbackFrameModel) -> None:
        """派发回调帧（事件分流 / 处理器异常隔离——不击穿收帧循环）。

        Args:
            frame: 回调帧。
        """
        if self._is_disconnect_event(frame):
            logger.warning(
                "企微长连接被新连接踢下线（disconnected_event——停止重连）: bot_id={}",
                self._bot_id,
            )
            self._state = WeComConnectionState.KICKED
            self._stop_event.set()
            return
        handler = self._callback_handler
        if handler is None:
            logger.debug("企微回调无处理器（丢弃）: cmd={}", frame.cmd)
            return
        try:
            await handler(frame)
        except Exception:  # 单帧处理失败不击穿长连接
            logger.opt(exception=True).error("企微回调处理失败（丢弃该帧）: cmd={}", frame.cmd)

    @staticmethod
    def _is_disconnect_event(frame: WeComCallbackFrameModel) -> bool:
        """判定是否为连接断开事件。

        Args:
            frame: 回调帧。

        Returns:
            True = 断开事件（新连接踢掉本连接）。
        """
        event = frame.body.event
        return event is not None and event.eventtype == WECOM_EVENT_DISCONNECTED

    @staticmethod
    def _parse_frame(raw: str | bytes) -> WeComCallbackFrameModel | WeComAckModel | None:
        """解析入站帧（回调命令 → 回调帧；其余 → 应答包裹）。

        Args:
            raw: 原始帧（文本 / 二进制）。

        Returns:
            解析后的模型；非法 JSON / 非对象载荷返回 None（调用方记日志跳过）。

        Raises:
            pydantic.ValidationError: 字段类型不符（由调用方按非法帧处理）。
        """
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("企微长连接收到非法 JSON 帧（跳过）: {!r}", text[:200])
            return None
        if not isinstance(data, dict):
            logger.warning("企微长连接收到非对象帧（跳过）: {!r}", text[:200])
            return None
        try:
            cmd = data.get("cmd")
            if isinstance(cmd, str) and cmd in WECOM_CALLBACK_COMMANDS:
                return WeComCallbackFrameModel.model_validate(data)
            return WeComAckModel.model_validate(data)
        except ValidationError as exc:
            logger.warning("企微长连接帧字段非法（跳过）: {}", str(exc)[:200])
            return None

    def _resolve_ack(self, ack: WeComAckModel) -> None:
        """按 req_id 归位应答（未知 req_id 仅记日志——迟到 / 多余的应答无害）。

        Args:
            ack: 应答包裹。
        """
        req_id = ack.headers.req_id
        future = self._pending.pop(req_id, None)
        if future is None:
            logger.debug("企微长连接收到未知 req_id 的应答（忽略）: req_id={}", req_id)
            return
        if not future.done():
            future.set_result(ack)

    def _fail_pending(self, reason: str) -> None:
        """拒绝全部在途请求（连接中断——不静默悬挂）。

        Args:
            reason: 失败原因（错误信息承载）。
        """
        if not self._pending:
            return
        for req_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(
                    BusinessException(
                        code=ErrorCode.CHANNEL_API_ERROR,
                        message=f"企微长连接请求失败: {reason}",
                        details={"req_id": req_id, "reason": reason},
                    )
                )
            self._pending.pop(req_id, None)

    async def _sleep_or_stop(self, delay: float) -> None:
        """睡满延时期限或停机请求先到即返回（退避 / 心跳等待可被优雅停机打断）。

        Args:
            delay: 延时时长秒数。
        """
        sleep_task: asyncio.Task[None] = asyncio.create_task(self._sleep(delay))
        stop_task: asyncio.Task[bool] = asyncio.create_task(self._stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (sleep_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep_task, stop_task, return_exceptions=True)
        if sleep_task in done and not sleep_task.cancelled():
            sleep_task.result()

    async def _close_connection(self, connection: WeComConnectionProtocol) -> None:
        """关闭连接（幂等；异常仅记日志）。

        Args:
            connection: 待关闭的连接。
        """
        try:
            await connection.close()
        except Exception:
            logger.opt(exception=True).debug("企微长连接关闭异常（已忽略）")

    # ------------------------------------------------------------------
    # 命令面（文本 / 附件 / 素材上传）
    # ------------------------------------------------------------------

    async def send_markdown_reply(self, *, callback_req_id: str, content: str) -> WeComAckModel:
        """回复消息回调（``aibot_respond_msg`` + markdown；透传回调 req_id）。

        Args:
            callback_req_id: 触发回复的消息回调 req_id。
            content: markdown 文本（调用方保证 ≤20480 字节——分段由 runner 承担）。

        Returns:
            平台应答（成功语义已校验）。

        Raises:
            BusinessException: 连接未就绪 / 应答超时 / 平台拒绝（``CHANNEL_API_ERROR``）。
        """
        command = build_markdown_reply_command(
            req_id=self._next_req_id(), callback_req_id=callback_req_id, content=content
        )
        return await self._execute(command)

    async def send_markdown_push(self, *, target: WeComChatTargetModel, content: str) -> WeComAckModel:
        """主动推送 markdown 消息（``aibot_send_msg``；chatid + chat_type 寻址）。

        Args:
            target: 解析后的会话目标。
            content: markdown 文本（≤20480 字节）。

        Returns:
            平台应答（成功语义已校验）。

        Raises:
            BusinessException: 连接未就绪 / 应答超时 / 平台拒绝（``CHANNEL_API_ERROR``）。
        """
        command = build_markdown_push_command(
            req_id=self._next_req_id(), target=target, content=content
        )
        return await self._execute(command)

    async def send_media_push(
        self,
        *,
        target: WeComChatTargetModel,
        media_kind: WeComMediaKind,
        media_id: str,
    ) -> WeComAckModel:
        """主动推送附件消息（image / voice / file；素材须先经 :meth:`upload_media`）。

        Args:
            target: 解析后的会话目标。
            media_kind: 上传素材类型。
            media_id: 临时素材 ID。

        Returns:
            平台应答（成功语义已校验）。

        Raises:
            BusinessException: 连接未就绪 / 应答超时 / 平台拒绝（``CHANNEL_API_ERROR``）；
                素材类型无对应消息体（``VALIDATION_ERROR``）。
        """
        command = build_media_push_command(
            req_id=self._next_req_id(),
            target=target,
            media_kind=media_kind,
            media_id=media_id,
        )
        return await self._execute(command)

    async def upload_media(
        self, content: bytes, *, media_kind: WeComMediaKind, filename: str
    ) -> str:
        """分片上传临时素材并返回 media_id（初始化 → 逐片 → 完成）。

        Args:
            content: 明文文件字节（图片 / 语音 / 文件 / 视频）。
            media_kind: 素材类型（决定大小上限与完成应答的媒体消息类型）。
            filename: 文件名（≤256 字节——平台侧呈现名）。

        Returns:
            临时素材 ID（3 天有效）。

        Raises:
            BusinessException: 空内容 / 超出类型上限 / 分片数超限
                （``VALIDATION_ERROR``）；连接未就绪 / 应答缺字段 / 平台拒绝 /
                应答超时（``CHANNEL_API_ERROR``）。
        """
        limit = wecom_upload_size_limit(media_kind)
        if len(content) < WECOM_UPLOAD_MIN_SIZE_BYTES:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    f"企微附件过小（小于平台下限 {WECOM_UPLOAD_MIN_SIZE_BYTES} 字节）: "
                    f"{len(content)} 字节（{media_kind.value}）"
                ),
                details={"filename": filename},
            )
        if len(content) > limit:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    f"企微附件超出 {media_kind.value} 类型上限: "
                    f"{len(content)} 字节 > {limit} 字节"
                ),
                details={"filename": filename, "media_kind": media_kind.value},
            )
        total_chunks = max(1, -(-len(content) // WECOM_UPLOAD_CHUNK_SIZE_BYTES))
        if total_chunks > WECOM_UPLOAD_MAX_CHUNKS:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=(
                    f"企微附件分片数超限: {total_chunks} > {WECOM_UPLOAD_MAX_CHUNKS}"
                    f"（单片 ≤{WECOM_UPLOAD_CHUNK_SIZE_BYTES} 字节）"
                ),
                details={"filename": filename, "total_chunks": total_chunks},
            )
        init_ack = await self._execute(
            build_upload_init_command(
                req_id=self._next_req_id(),
                media_kind=media_kind,
                filename=filename,
                total_size=len(content),
                total_chunks=total_chunks,
                md5=hashlib.md5(content).hexdigest(),
            )
        )
        upload_id = init_ack.body.upload_id if init_ack.body is not None else None
        if not upload_id:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="企微素材上传初始化应答缺少 upload_id",
                details={"filename": filename},
            )
        for index in range(total_chunks):
            start = index * WECOM_UPLOAD_CHUNK_SIZE_BYTES
            chunk = content[start : start + WECOM_UPLOAD_CHUNK_SIZE_BYTES]
            await self._execute(
                build_upload_chunk_command(
                    req_id=self._next_req_id(),
                    upload_id=upload_id,
                    chunk_index=index,
                    base64_data=base64.b64encode(chunk).decode("ascii"),
                )
            )
        finish_ack = await self._execute(
            build_upload_finish_command(req_id=self._next_req_id(), upload_id=upload_id)
        )
        media_id = finish_ack.body.media_id if finish_ack.body is not None else None
        if not media_id:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="企微素材上传完成应答缺少 media_id",
                details={"filename": filename},
            )
        logger.debug(
            "企微素材上传完成: filename={}, media_kind={}, bytes={}, chunks={}",
            filename,
            media_kind.value,
            len(content),
            total_chunks,
        )
        return media_id

    # ------------------------------------------------------------------
    # 命令执行与退避
    # ------------------------------------------------------------------

    async def _execute(
        self,
        command: WeComCommandModel,
        *,
        connection: WeComConnectionProtocol | None = None,
        timeout: float | None = None,
        error_code: ErrorCode = ErrorCode.CHANNEL_API_ERROR,
    ) -> WeComAckModel:
        """发送命令并等待平台应答（req_id 关联 + 超时 + 错误码映射）。

        Args:
            command: 待发送命令（req_id 已生成）。
            connection: 指定连接（None = 当前活跃连接）。
            timeout: 应答超时秒数（None = ``request_timeout_seconds``）。
            error_code: 平台应答失败的映射错误码（订阅面用 ``CHANNEL_TOKEN_MISSING``）。

        Returns:
            成功应答（``errcode == 0``）。

        Raises:
            BusinessException: 连接未就绪 / 应答超时 / 传输失败 / 平台拒绝
                （``error_code``——缺省 ``CHANNEL_API_ERROR``）。
        """
        target_connection = connection if connection is not None else self._connection
        if target_connection is None:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="企微长连接未就绪（命令未送达）",
                details={"cmd": command.cmd},
            )
        req_id = command.headers.req_id
        future: asyncio.Future[WeComAckModel] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        wait_seconds = self._request_timeout_seconds if timeout is None else timeout
        try:
            payload = command.model_dump_json(by_alias=True, exclude_none=True)
            await target_connection.send(payload)
            ack = await asyncio.wait_for(future, timeout=wait_seconds)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"企微长连接命令应答超时（{wait_seconds:.1f}s）: {command.cmd}",
                details={"cmd": command.cmd, "req_id": req_id},
            ) from exc
        except BusinessException:
            self._pending.pop(req_id, None)
            raise
        except Exception as exc:  # 发送失败（连接已断）——在途 future 同步拒绝
            self._pending.pop(req_id, None)
            if not future.done():
                future.cancel()
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"企微长连接命令发送失败: {command.cmd}",
                details={"cmd": command.cmd, "req_id": req_id, "error": str(exc)},
            ) from exc
        if not ack.is_success:
            raise BusinessException(
                code=error_code,
                message=(
                    f"企微长连接命令被拒（{command.cmd}）: "
                    f"errcode={ack.errcode}, errmsg={ack.errmsg or '(空)'}"
                ),
                details={
                    "cmd": command.cmd,
                    "req_id": req_id,
                    "errcode": ack.errcode,
                    "errmsg": ack.errmsg,
                },
            )
        return ack

    def _backoff_delay(self, attempt: int) -> float:
        """重连退避延时（指数增长 + 抖动 + 封顶）。

        Args:
            attempt: 连续失败次数（≥ 1）。

        Returns:
            本轮退避秒数（``min(base × 2^(n-1), max)`` 上叠加 ±ratio 抖动，
            下限 0）。
        """
        exponent = max(0, attempt - 1)
        base_delay = min(self._backoff_base_seconds * (2**exponent), self._backoff_max_seconds)
        if self._backoff_jitter_ratio <= 0:
            return float(base_delay)
        jitter = base_delay * self._backoff_jitter_ratio
        return float(max(0.0, base_delay + self._rng.uniform(-jitter, jitter)))

    @staticmethod
    def _next_req_id() -> str:
        """生成请求唯一标识（每条命令独立——应答按此归位）。

        Returns:
            req_id 字符串。
        """
        return f"{_REQ_ID_PREFIX}{uuid.uuid4().hex}"
