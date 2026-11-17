"""telegram 通道客户端——Bot API 手写异步传输层（共享 httpx client 注入）。

锁定设计决策：**不引入 aiogram / python-telegram-bot**——按 Bot API 协议
手写；HTTP 出站一律经注入的共享 ``httpx.AsyncClient``（pools.PoolManager
下发——硬规则：无私建连接，与 llm 域客户端同约定）。

方法面（Bot API 子集）：

    - ``get_me()``        —— 启动校验（token 有效性探测）；
    - ``get_updates()``   —— 长轮询拉取（``timeout`` 参数 + offset 游标；
                             请求超时 = 轮询超时 + 传输裕量）；
    - ``send_message()``  —— 发送消息（4096 上限分段：换行边界优先，
                             无换行硬切；引用回复只挂在首段）。

4096 分段规则（Bot API 单消息文本上限 4096 字符）：

    1. 文本 ≤ 4096 → 单条发送；
    2. 超限时在每段「前 4096 字符窗口」内找**最后一个换行符**切分
       （段落不跨语义行；切点换行符被消费——下段不以空行开头）；
    3. 窗口内无换行（或仅在首位）→ 硬切 4096；
    4. ``reply_to_message_id`` 只挂首段（后续段是续文——逐段引用会
       在客户端渲染成多条引用噪声）。

错误码映射表（channels 域段，见 core errors 注册表）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    token 环境变量缺失 / 为空      CHANNEL_TOKEN_MISSING      否（构造期）
    HTTP 401 / 403（token 无效）   CHANNEL_TOKEN_MISSING      否
    其余 HTTP 非 200               CHANNEL_API_ERROR          由 runner 退避
    ok=False 响应（error_code     CHANNEL_API_ERROR          由 runner 退避
    401/403 → TOKEN_MISSING）
    httpx 传输异常（连接 / 超时）  CHANNEL_API_ERROR          由 runner 退避
    响应 JSON 非法 / result 缺失   CHANNEL_API_ERROR          否
    空文本 / 非数字引用 ID         VALIDATION_ERROR           否（调用方 bug）
    ============================  ========================  ==========
"""

from __future__ import annotations

import os
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

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


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
    ) -> None:
        """初始化客户端（token 解析 fail fast）。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            bot_token_env: bot token 环境变量名（实际 token 从该变量读取）。
            api_base: Bot API 基础 URL（尾斜杠容错）。
            poll_timeout_seconds: getUpdates 长轮询超时秒数（请求超时 =
                该值 + 传输裕量）。

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
    ) -> list[TgSendMessageResult]:
        """sendMessage——发送消息（4096 上限分段；返回全部段的送达结果）。

        Args:
            chat_id: 目标会话 ID（数值或字符串——Bot API 双形态；通道层
                字符串 ID 直接透传）。
            text: 消息文本（非空；超 4096 字符自动分段——规则见模块
                docstring）。
            reply_to_message_id: 引用回复的目标消息 ID（None = 不引用；
                仅挂首段）。

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
            logger.info(
                "Telegram 长消息分段发送: chat_id={}, parts={}", chat_id, len(parts)
            )
        results: list[TgSendMessageResult] = []
        for index, part in enumerate(parts):
            body: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if index == 0 and reply_to is not None:
                body["reply_to_message_id"] = reply_to
            results.append(await self._send_single(body))
        return results

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
            return await self._http.get(
                self._method_url(method), params=params, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                details={
                    "reason": "Telegram Bot API 传输失败",
                    "method": method,
                    "error": str(exc),
                },
            ) from exc

    async def _send_single(self, body: dict[str, Any]) -> TgSendMessageResult:
        """发送单段消息（POST sendMessage → 校验 → 归一结果）。

        Args:
            body: 请求体（chat_id / text / 可选 reply_to_message_id）。

        Returns:
            该段送达结果。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``）。
        """
        try:
            response = await self._http.post(self._method_url("sendMessage"), json=body)
        except httpx.HTTPError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                details={
                    "reason": "Telegram Bot API 传输失败",
                    "method": "sendMessage",
                    "error": str(exc),
                },
            ) from exc
        wrapper = self._validate_wrapper(
            TgResponseModel[TgSendMessageResult], response, "sendMessage"
        )
        result = wrapper.result
        if result is None:
            raise self._api_error("sendMessage", response, reason="ok=True 但 result 缺失")
        return result

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
