"""telegram 通道客户端——Bot API 手写异步传输层（共享 httpx client 注入）。

锁定设计决策：**不引入 aiogram / python-telegram-bot**——按 Bot API 协议
手写；HTTP 出站一律经注入的共享 ``httpx.AsyncClient``（pools.PoolManager
下发——硬规则：无私建连接，与 llm 域客户端同约定）。

方法面（Bot API 子集）：

    - ``get_me()``        —— 启动校验（token 有效性探测）；
    - ``get_updates()``   —— 长轮询拉取（``timeout`` 参数 + offset 游标；
                              请求超时 = 轮询超时 + 传输裕量）；
    - ``send_message()``  —— 发送消息（4096 上限分段：换行边界优先，
                              无换行硬切；引用回复只挂在首段；v2 增
                              ``disable_notification`` 免通知参数）；
    - ``send_media_ref()``    —— v2 媒体发送（JSON 形态：file_id /
                                 HTTP URL 直传平台——sendPhoto 等方法）；
    - ``send_media_upload()`` —— v2 媒体发送（multipart 上传形态：
                                 base64 解码字节经 httpx ``files=``
                                 上传为 InputFile）。

4096 分段规则（Bot API 单消息文本上限 4096 字符）：

    1. 文本 ≤ 4096 → 单条发送；
    2. 超限时在每段「前 4096 字符窗口」内找**最后一个换行符**切分
       （段落不跨语义行；切点换行符被消费——下段不以空行开头）；
    3. 窗口内无换行（或仅在首位）→ 硬切 4096；
    4. ``reply_to_message_id`` 只挂首段（后续段是续文——逐段引用会
       在客户端渲染成多条引用噪声）。

出站 429 限流规则（单次重试）：

    - 响应 HTTP 429 且 JSON 体含 ``parameters.retry_after``（秒）时，
      按该值延迟后**单次**重试同一请求；延迟夹取到 1..30 秒区间
      （平台建议值可能远大于本通道可接受等待，夹取防一条消息阻塞
      出站链路过久）；
    - 重试仍非 200（或重试响应仍为限流）→ 抛既有 ``CHANNEL_API_ERROR``
      （错误码映射表不新增成员）；
    - 429 但缺 ``parameters.retry_after``（非法 / 非数字）→ 不重试，
      直接按既有错误映射上抛；
    - 延迟经注入的 ``sleep``（生产为 ``asyncio.sleep``；测试注入记录式
      伪休眠断言延迟值与重试次数）。

v2 媒体发送约定（附件不流式——通道面契约，见 channel_models）：

    - 方法 / 参数名由调用方（runner 适配层）按附件 kind 解析（image →
      sendPhoto/photo、audio → sendVoice/voice 或 sendAudio/audio、
      file → sendDocument/document、sticker → sendSticker/sticker）；
    - ``send_media_ref``：媒体引用（file_id 或 http(s) URL）进 JSON
      请求体——Bot API 双形态原生支持；
    - ``send_media_upload``：原始字节经 multipart 表单上传（文件字段名
      = 平台参数名；``data`` 字段全字符串形态）；
    - ``disable_notification=True`` 时两种形态均携带免通知参数（False /
      None 省略——v1 wire 形态保持）；
    - 附件发送**不携带引用回复**（``reply_to_message_id`` 只属于文本
      消息——v2 契约，见 channel_models）。

错误码映射表（channels 域段，见 core errors 注册表）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    token 环境变量缺失 / 为空      CHANNEL_TOKEN_MISSING      否（构造期）
    HTTP 401 / 403（token 无效）   CHANNEL_TOKEN_MISSING      否
    其余 HTTP 非 200               CHANNEL_API_ERROR          由 runner 退避
    HTTP 429 + retry_after（出站） CHANNEL_API_ERROR          客户端单次重试
                                                            （夹取 1..30s）
    ok=False 响应（error_code     CHANNEL_API_ERROR          由 runner 退避
    401/403 → TOKEN_MISSING）
    httpx 传输异常（连接 / 超时）  CHANNEL_API_ERROR          由 runner 退避
    响应 JSON 非法 / result 缺失   CHANNEL_API_ERROR          否
    空文本 / 非数字引用 ID /
    空媒体引用 / 空上传内容        VALIDATION_ERROR           否（调用方 bug）
    ============================  ========================  ==========
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any, Final, TypeVar

import httpx
from loguru import logger
from pydantic import ValidationError

from packages.harness.channels.telegram.tg_models import (
    TgBotInfoModel,
    TgResponseModel,
    TgSendMessageResult,
    TgUpdateModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "TelegramClient",
    "split_tg_message_text",
    "TG_MESSAGE_MAX_CHARS",
]

#: 响应包裹参数化的载荷类型变量（_validate_wrapper 泛型契约）。
_T = TypeVar("_T")

#: Bot API 单消息文本上限（字符数）。
TG_MESSAGE_MAX_CHARS: Final[int] = 4096

#: 长轮询请求超时的传输裕量秒数（请求超时 = 轮询超时 + 本裕量——防服务
#: 端在轮询窗口末尾返回时被客户端误判超时）。
_POLL_TIMEOUT_MARGIN_SECONDS: Final[float] = 5.0

#: 出站 429 重试延迟下界秒数（平台建议值过小 / 为零时夹取到该值）。
_RETRY_AFTER_MIN_SECONDS: Final[float] = 1.0

#: 出站 429 重试延迟上界秒数（平台建议值过大时夹取——单条消息不长期阻塞）。
_RETRY_AFTER_MAX_SECONDS: Final[float] = 30.0

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300

#: 休眠函数签名（出站 429 重试延迟——生产 ``asyncio.sleep``，测试注入伪实现）。
SleepFn = Callable[[float], Awaitable[None]]


def split_tg_message_text(text: str, *, max_chars: int = TG_MESSAGE_MAX_CHARS) -> list[str]:
    """按字符上限切分长文本（换行边界优先，无换行硬切）。

    切分规则见模块 docstring「4096 分段规则」一节；纯函数（无 I/O）——
    测试直调面。每段非空（切点消费保证无空段；空文本入参返回 ``[""]``
    由调用方先行校验拒绝）。

    Args:
        text: 待切分文本。
        max_chars: 单段字符上限（默认 4096；须 ≥ 1）。

    Returns:
        切分后的段列表（原文本 ≤ 上限时为单元素列表）。

    Raises:
        ValueError: ``max_chars < 1``（公共纯函数参数面——零上限会令
            切分循环无法推进，入口拒绝而非死循环）。
    """
    if max_chars < 1:
        raise ValueError("max_chars 须 ≥ 1")
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        newline_index = window.rfind("\n")
        if newline_index >= 1:
            parts.append(remaining[:newline_index])
            remaining = remaining[newline_index + 1 :]
        else:
            parts.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
    if remaining:
        parts.append(remaining)
    return parts


class TelegramClient:
    """Telegram Bot API 客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源。构造期从 ``bot_token_env`` 指定的环境变量解析
    token（密钥永不落配置树——与 llm ``api_key_env`` 同间接模式），
    缺失 / 为空即 fail fast（``CHANNEL_TOKEN_MISSING``）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        bot_token_env: str = "TELEGRAM_BOT_TOKEN",
        api_base: str = "https://api.telegram.org",
        poll_timeout_seconds: int = 25,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        """初始化客户端（token 解析 fail fast）。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            bot_token_env: bot token 环境变量名（实际 token 从该变量读取）。
            api_base: Bot API 基础 URL（尾斜杠容错）。
            poll_timeout_seconds: getUpdates 长轮询超时秒数（请求超时 =
                该值 + 传输裕量）。
            sleep: 休眠函数（出站 429 单次重试延迟；缺省 ``asyncio.sleep``，
                测试注入记录式伪休眠断言延迟值与重试次数）。

        Raises:
            BusinessException: token 环境变量缺失 / 为空
                （``CHANNEL_TOKEN_MISSING``——启动期 fail fast）。
        """
        token = os.environ.get(bot_token_env, "").strip()
        if not token:
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message=(
                    f"Telegram bot token 未配置：请设置环境变量 {bot_token_env}"
                    "（TELEGRAM_ENABLED=true 时必填）"
                ),
                details={"bot_token_env": bot_token_env},
            )
        self._http = http_client
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._poll_timeout_seconds = poll_timeout_seconds
        self._sleep = sleep

    # ------------------------------------------------------------------
    # 方法面
    # ------------------------------------------------------------------

    async def get_me(self) -> TgBotInfoModel:
        """getMe——启动校验（token 有效性探测；不消费任何轮询状态）。

        Returns:
            bot 用户信息（username / id）。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``——映射表
                见模块 docstring）。
        """
        response = await self._request("getMe")
        wrapper = self._validate_wrapper(TgResponseModel[TgBotInfoModel], response, "getMe")
        result = wrapper.result
        if result is None:
            raise self._api_error("getMe", response, reason="ok=True 但 result 缺失")
        return result

    async def get_updates(self, *, offset: int | None = None) -> list[TgUpdateModel]:
        """getUpdates——长轮询拉取待处理更新。

        请求形态：GET + ``timeout=<poll_timeout_seconds>``（长轮询窗口）
        + ``offset``（已确认游标——服务端据此丢弃更早更新）；请求超时 =
        轮询超时 + 传输裕量（防窗口末尾返回被误判超时）。轮询窗口自然
        耗尽时服务端返回空列表（合法形态——非错误）。

        Args:
            offset: 已确认游标（None = 首次拉取——不携带 offset 参数）。

        Returns:
            更新列表（可能为空）。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        params: dict[str, str] = {"timeout": str(self._poll_timeout_seconds)}
        if offset is not None:
            params["offset"] = str(offset)
        response = await self._request(
            "getUpdates",
            params=params,
            timeout=httpx.Timeout(self._poll_timeout_seconds + _POLL_TIMEOUT_MARGIN_SECONDS),
        )
        wrapper = self._validate_wrapper(
            TgResponseModel[list[TgUpdateModel]], response, "getUpdates"
        )
        if wrapper.result is None:
            return []
        return list(wrapper.result)

    async def send_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | str | None = None,
        disable_notification: bool | None = None,
    ) -> list[TgSendMessageResult]:
        """sendMessage——发送消息（4096 上限分段；返回全部段的送达结果）。

        Args:
            chat_id: 目标会话 ID（数值或字符串——Bot API 双形态；通道层
                字符串 ID 直接透传）。
            text: 消息文本（非空；超 4096 字符自动分段——规则见模块
                docstring）。
            reply_to_message_id: 引用回复的目标消息 ID（None = 不引用；
                仅挂首段）。
            disable_notification: 免通知投递（v2——True 时各段请求体携带
                ``disable_notification: true``；None / False 省略该字段
                ——v1 wire 形态保持）。

        Returns:
            各段送达结果（单段发送时单元素列表）。

        Raises:
            BusinessException: 空文本 / 非数字引用 ID（``VALIDATION_ERROR``
                ——调用方 bug 面）；token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        if not text.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="Telegram 消息文本不能为空",
                details={"chat_id": str(chat_id)},
            )
        reply_to: int | None = None
        if reply_to_message_id is not None:
            try:
                reply_to = int(reply_to_message_id)
            except (TypeError, ValueError) as exc:
                raise BusinessException(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="reply_to_message_id 必须是数字字符串（Telegram 消息 ID 形态）",
                    details={"reply_to_message_id": str(reply_to_message_id)},
                ) from exc
        parts = split_tg_message_text(text)
        if len(parts) > 1:
            logger.info("Telegram 长消息分段发送: chat_id={}, parts={}", chat_id, len(parts))
        results: list[TgSendMessageResult] = []
        for index, part in enumerate(parts):
            body: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if index == 0 and reply_to is not None:
                body["reply_to_message_id"] = reply_to
            if disable_notification is True:
                body["disable_notification"] = True
            results.append(await self._send_single(body))
        return results

    async def send_media_ref(
        self,
        *,
        chat_id: int | str,
        api_method: str,
        media_field: str,
        media_ref: str,
        caption: str | None = None,
        disable_notification: bool | None = None,
    ) -> TgSendMessageResult:
        """v2 媒体发送——JSON 引用形态（file_id / HTTP URL 直传平台）。

        Args:
            chat_id: 目标会话 ID（数值或字符串——通道层字符串 ID 透传）。
            api_method: Bot API 方法名（``sendPhoto`` / ``sendVoice`` /
                ``sendAudio`` / ``sendDocument`` / ``sendSticker``——由
                runner 适配层按附件 kind 解析）。
            media_field: 媒体参数名（``photo`` / ``voice`` / ``audio`` /
                ``document`` / ``sticker``——与方法配套）。
            media_ref: 媒体引用（平台 file_id 或 http(s) URL——Bot API
                媒体参数双形态；``tg-file://`` 伪方案由调用方先行剥离）。
            caption: 附件说明（None = 无；``sendSticker`` 无 caption 参数
                ——调用方按平台差异先行丢弃，本方法不感知）。
            disable_notification: 免通知投递（True 时请求体携带
                ``disable_notification: true``；None / False 省略）。

        Returns:
            该媒体消息送达结果。

        Raises:
            BusinessException: 空媒体引用（``VALIDATION_ERROR``——调用方
                bug 面）；token 无效（``CHANNEL_TOKEN_MISSING``）；传输 /
                状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        if not media_ref.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Telegram 媒体引用不能为空（{media_field}）",
                details={"chat_id": str(chat_id), "api_method": api_method},
            )
        body: dict[str, Any] = {"chat_id": chat_id, media_field: media_ref}
        if caption:
            body["caption"] = caption
        if disable_notification is True:
            body["disable_notification"] = True
        return await self._post_send_method(api_method, json_body=body)

    async def send_media_upload(
        self,
        *,
        chat_id: int | str,
        api_method: str,
        media_field: str,
        content: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        caption: str | None = None,
        disable_notification: bool | None = None,
    ) -> TgSendMessageResult:
        """v2 媒体发送——multipart 上传形态（原始字节 → InputFile）。

        Args:
            chat_id: 目标会话 ID（数值或字符串——multipart 表单内字符串
                化承载）。
            api_method: Bot API 方法名（同 ``send_media_ref``）。
            media_field: 媒体参数名 / multipart 文件字段名（同
                ``send_media_ref``）。
            content: 媒体原始字节（调用方解码 base64 载荷所得）。
            filename: 上传文件名（调用方按 kind / media_type 合成）。
            content_type: MIME 类型（缺省 ``application/octet-stream``）。
            caption: 附件说明（None = 无；表单字段形态承载）。
            disable_notification: 免通知投递（True 时表单携带
                ``disable_notification=true`` 字符串形态；None / False
                省略）。

        Returns:
            该媒体消息送达结果。

        Raises:
            BusinessException: 空字节 / 空文件名（``VALIDATION_ERROR``
                ——调用方 bug 面）；token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        if not content:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Telegram 媒体上传内容不能为空（{media_field}）",
                details={"chat_id": str(chat_id), "api_method": api_method},
            )
        if not filename.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="Telegram 媒体上传文件名不能为空",
                details={"chat_id": str(chat_id), "api_method": api_method},
            )
        form_data: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            form_data["caption"] = caption
        if disable_notification is True:
            form_data["disable_notification"] = "true"
        files = {media_field: (filename, content, content_type)}
        return await self._post_send_method(api_method, form_data=form_data, form_files=files)

    # ------------------------------------------------------------------
    # 内部——请求与错误映射
    # ------------------------------------------------------------------

    def _method_url(self, method: str) -> str:
        """拼接 Bot API 方法 URL（``<api_base>/bot<token>/<method>``）。

        Args:
            method: Bot API 方法名。

        Returns:
            完整方法 URL。
        """
        return f"{self._api_base}/bot{self._token}/{method}"

    async def _request(
        self,
        method: str,
        *,
        params: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """发起 GET 请求（传输异常统一映射 ``CHANNEL_API_ERROR``）。

        Args:
            method: Bot API 方法名。
            params: 查询参数（None = 无）。
            timeout: 请求超时（None = 共享 client 默认超时）。

        Returns:
            HTTP 响应（状态检查由调用侧 ``_validate_wrapper`` 承担）。

        Raises:
            BusinessException: httpx 传输异常（连接 / 超时——
                ``CHANNEL_API_ERROR``）。
        """
        try:
            return await self._http.get(self._method_url(method), params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self._transport_error(method, exc) from exc

    async def _send_single(self, body: dict[str, Any]) -> TgSendMessageResult:
        """发送单段消息（POST sendMessage → 校验 → 归一结果）。

        Args:
            body: 请求体（chat_id / text / 可选 reply_to_message_id /
                可选 disable_notification）。

        Returns:
            该段送达结果。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        return await self._post_send_method("sendMessage", json_body=body)

    async def _post_send_method(
        self,
        api_method: str,
        *,
        json_body: dict[str, Any] | None = None,
        form_data: dict[str, str] | None = None,
        form_files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> TgSendMessageResult:
        """发送面统一 POST（JSON / multipart 双形态 → 校验 → 归一结果）。

        v2 收敛点：sendMessage 单段与 sendPhoto / sendVoice 等媒体方法
        共用（传输异常 / 状态检查 / 响应包裹校验 / result 提取同链路；
        仅请求体形态不同）。

        429 限流：响应含 ``parameters.retry_after`` 时按夹取后的延迟
        （1..30 秒）单次重试——重试仍失败按既有错误映射上抛；无该字段
        不重试。

        Args:
            api_method: Bot API 方法名。
            json_body: JSON 请求体（与 form_data / form_files 互斥——
                JSON 形态时携带）。
            form_data: multipart 表单字段（multipart 形态时携带）。
            form_files: multipart 文件字段（multipart 形态时携带）。

        Returns:
            该消息送达结果。

        Raises:
            BusinessException: 传输异常（``CHANNEL_API_ERROR``）；token
                无效（``CHANNEL_TOKEN_MISSING``）；状态 / 解析失败
                （``CHANNEL_API_ERROR``）；ok=True 但 result 缺失
                （``CHANNEL_API_ERROR``）。
        """
        response = await self._post_once(
            api_method,
            json_body=json_body,
            form_data=form_data,
            form_files=form_files,
        )
        retry_after = self._retry_after_seconds(response)
        if retry_after is not None:
            logger.warning(
                "Telegram 出站被限流（HTTP 429），{:.1f} 秒后单次重试: method={}",
                retry_after,
                api_method,
            )
            await self._sleep(retry_after)
            response = await self._post_once(
                api_method,
                json_body=json_body,
                form_data=form_data,
                form_files=form_files,
            )
        wrapper = self._validate_wrapper(TgResponseModel[TgSendMessageResult], response, api_method)
        result = wrapper.result
        if result is None:
            raise self._api_error(api_method, response, reason="ok=True 但 result 缺失")
        return result

    async def _post_once(
        self,
        api_method: str,
        *,
        json_body: dict[str, Any] | None,
        form_data: dict[str, str] | None,
        form_files: dict[str, tuple[str, bytes, str]] | None,
    ) -> httpx.Response:
        """执行一次出站 POST（传输异常统一映射 ``CHANNEL_API_ERROR``）。

        Args:
            api_method: Bot API 方法名。
            json_body: JSON 请求体（None = 非 JSON 形态）。
            form_data: multipart 表单字段（None = 非表单形态）。
            form_files: multipart 文件字段（None = 非表单形态）。

        Returns:
            HTTP 响应（状态检查由调用侧承担——重试判定需要原始状态码）。

        Raises:
            BusinessException: httpx 传输异常（``CHANNEL_API_ERROR``）。
        """
        try:
            return await self._http.post(
                self._method_url(api_method),
                json=json_body,
                data=form_data,
                files=form_files,
            )
        except httpx.HTTPError as exc:
            raise self._transport_error(api_method, exc) from exc

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """解析 429 响应的 ``parameters.retry_after`` 并夹取重试延迟。

        Args:
            response: 出站请求的 HTTP 响应。

        Returns:
            夹取到 1..30 秒的延迟秒数；非 429 / 体非 JSON / 无该参数 /
            参数非数字时返回 None（调用方不重试）。
        """
        if response.status_code != 429:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            return None
        value = parameters.get("retry_after")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return min(max(float(value), _RETRY_AFTER_MIN_SECONDS), _RETRY_AFTER_MAX_SECONDS)

    @staticmethod
    def _transport_error(method: str, exc: httpx.HTTPError) -> BusinessException:
        """构造传输异常业务异常（GET / POST 发送面共用）。

        Args:
            method: Bot API 方法名（错误 details 携带）。
            exc: httpx 传输异常（连接 / 超时）。

        Returns:
            待抛出的 ``CHANNEL_API_ERROR`` 业务异常（调用方
            ``raise ... from exc`` 保因果链）。
        """
        return BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            details={
                "reason": "Telegram Bot API 传输失败",
                "method": method,
                "error": str(exc),
            },
        )

    def _validate_wrapper(
        self,
        wrapper_cls: type[TgResponseModel[_T]],
        response: httpx.Response,
        method: str,
    ) -> TgResponseModel[_T]:
        """HTTP 状态 + ok 标志 + JSON 三级校验（统一错误映射入口）。

        Args:
            wrapper_cls: 参数化的响应包裹类（按端点给定——载荷类型经
                泛型传导给返回值）。
            response: HTTP 响应。
            method: Bot API 方法名（错误 details 携带）。

        Returns:
            校验通过的响应包裹实例（``result`` 类型与 ``wrapper_cls``
            参数化一致）。

        Raises:
            BusinessException: 401 / 403（token 无效——
                ``CHANNEL_TOKEN_MISSING``）；其余非 200 / ok=False /
                JSON 非法（``CHANNEL_API_ERROR``）。
        """
        self._raise_for_status(response, method)
        try:
            wrapper = wrapper_cls.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise self._api_error(
                method, response, reason="响应不是合法的 Bot API JSON", parse_error=str(exc)[:200]
            ) from exc
        if not wrapper.ok:
            if wrapper.error_code in (401, 403):
                raise BusinessException(
                    code=ErrorCode.CHANNEL_TOKEN_MISSING,
                    message="Telegram bot token 无效或已被撤销",
                    details=self._error_details(method, response, wrapper.description),
                )
            raise self._api_error(
                method,
                response,
                reason="Bot API 返回 ok=False",
                description=wrapper.description,
                error_code=wrapper.error_code,
            )
        return wrapper

    def _raise_for_status(self, response: httpx.Response, method: str) -> None:
        """HTTP 状态检查（401/403 → token 无效；其余非 200 → API 错误）。

        Args:
            response: HTTP 响应。
            method: Bot API 方法名（错误 details 携带）。

        Raises:
            BusinessException: 见本方法 docstring 主体。
        """
        if response.status_code in (401, 403):
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message="Telegram bot token 无效或已被撤销",
                details=self._error_details(method, response, None),
            )
        if response.status_code != 200:
            raise self._api_error(
                method, response, reason=f"Bot API 返回非成功状态 {response.status_code}"
            )

    def _api_error(
        self,
        method: str,
        response: httpx.Response,
        *,
        reason: str,
        description: str | None = None,
        error_code: int | None = None,
        parse_error: str | None = None,
    ) -> BusinessException:
        """构造 ``CHANNEL_API_ERROR`` 业务异常（details 统一拼装）。

        Args:
            method: Bot API 方法名。
            response: HTTP 响应（片段截断携带）。
            reason: 失败类别描述。
            description: 平台错误描述（ok=False 形态携带）。
            error_code: 平台错误码（ok=False 形态携带）。
            parse_error: JSON 解析错误（解析失败形态携带）。

        Returns:
            待抛出的业务异常（调用方 ``raise ... from exc`` 保因果链）。
        """
        details = self._error_details(method, response, description)
        if error_code is not None:
            details["error_code"] = error_code
        if parse_error is not None:
            details["parse_error"] = parse_error
        return BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            details=details,
            message=f"Telegram Bot API 调用失败（{method}）: {reason}",
        )

    @staticmethod
    def _error_details(
        method: str,
        response: httpx.Response,
        description: str | None,
    ) -> dict[str, Any]:
        """拼装错误 details（方法 / 状态 / 平台描述 / 响应体片段）。

        Args:
            method: Bot API 方法名。
            response: HTTP 响应。
            description: 平台错误描述（None = 无）。

        Returns:
            details 字典。
        """
        details: dict[str, Any] = {
            "reason": "Telegram Bot API 调用失败",
            "method": method,
            "status_code": response.status_code,
            "body_snippet": response.text[:_BODY_SNIPPET_LIMIT],
        }
        if description is not None:
            details["description"] = description
        return details
