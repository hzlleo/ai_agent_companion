"""qq 通道客户端——NapCat OneBot v11 HTTP API 手写异步传输层（共享 httpx client 注入）。

锁定设计决策：**不引入任何 OneBot SDK**——按 OneBot v11 HTTP API 协议
手写；HTTP 出站一律经注入的共享 ``httpx.AsyncClient``（pools.PoolManager
下发——硬规则：无私建连接，与 llm / telegram 域客户端同约定）。

调用形态（文档化决策——单一动作对象包裹）：``POST {http_api_base_url}``
请求体 ``{"action": "<动作名>", "params": {...}}``（NapCat HTTP API 兼容
的动作名进 URL 路径形态不使用——单一形态锁定，防双形态漂移）。

方法面（OneBot v11 动作子集）：

    - ``get_login_info()``  —— 启动校验（登录账号探测——app lifespan
                                QQ 装配期消费）；
    - ``send_private_msg()`` —— 私聊发送（``private:<user_id>`` 目标面）；
    - ``send_group_msg()``   —— 群聊发送（``group:<group_id>`` 目标面）。

出站消息体：``params.message`` 直接传**纯文本字符串**（OneBot v11 的
message 字段合法形态之一；通道中立出站文本无需段数组组装——引用回复
不透传，见 ``qq_adapter`` 模块 docstring）。**不做超长分段**：QQ 客户端
对长文本自行折行渲染，平台未文档化单消息硬上限（与 Telegram Bot API
4096 上限的差异点——tg 分段规则不适用）。

access token 间接（与 llm ``api_key_env`` / telegram ``bot_token_env``
同模式——密钥永不落配置树）：构造期从 ``access_token_env`` 指定的环境
变量解析；**缺失 / 为空不 fail fast**——NapCat 侧未开启鉴权是合法部署
形态（``has_token=False``，请求不携带 Authorization 头）；NapCat 侧已
开启而本侧未配置时，调用将得 401/403 → ``CHANNEL_TOKEN_MISSING``（逐
消息隔离面——不阻断启动）。

错误码映射表（channels 域段——复用既有码表，无新增）：

    ============================  ========================  ==========
    失败类别                       错误码                    可重试
    ============================  ========================  ==========
    其余 HTTP 非 200               CHANNEL_API_ERROR          由调用方隔离
    HTTP 401 / 403（token 无效）   CHANNEL_TOKEN_MISSING      否
    retcode != 0 / status=failed  CHANNEL_API_ERROR          由调用方隔离
    httpx 传输异常（连接 / 超时）  CHANNEL_API_ERROR          由调用方隔离
    响应 JSON 非法                 CHANNEL_API_ERROR          否
    空文本（发送面）               VALIDATION_ERROR           否（调用方 bug）
    ============================  ========================  ==========
"""

from __future__ import annotations

import os
from typing import Any, Final

import httpx
from loguru import logger
from pydantic import ValidationError

from packages.harness.channels.qq.qq_models import (
    Ob11ApiRequestModel,
    Ob11ApiResponseModel,
    Ob11LoginInfoModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["NapCatOneBotClient"]

#: 错误 details 携带的响应体片段截断长度。
_BODY_SNIPPET_LIMIT: Final[int] = 300


class NapCatOneBotClient:
    """NapCat OneBot v11 HTTP API 客户端（共享 ``httpx.AsyncClient`` 注入）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源。构造期从 ``access_token_env`` 指定的环境变量解析
    access token（缺失 / 为空不 fail fast——NapCat 未开启鉴权的合法
    形态，见模块 docstring「access token 间接」一节）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str = "http://127.0.0.1:3000",
        access_token_env: str = "QQ_ACCESS_TOKEN",
        request_timeout_seconds: float = 15.0,
    ) -> None:
        """初始化客户端（access token 环境变量间接解析——缺失仅告警）。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: NapCat HTTP API 基础 URL（尾斜杠容错；部署机上的
                NapCat 服务地址）。
            access_token_env: access token 环境变量名（实际 token 从该
                变量读取；空 = NapCat 侧未开启鉴权形态）。
            request_timeout_seconds: 单次动作请求超时秒数。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_seconds)
        token = os.environ.get(access_token_env, "").strip()
        self._token: str | None = token if token else None
        if self._token is None:
            logger.info(
                "QQ access token 未配置（环境变量 {} 为空）——按 NapCat 未开启鉴权形态调用",
                access_token_env,
            )

    @property
    def has_token(self) -> bool:
        """是否持有 access token（False = 请求不携带 Authorization 头）。"""
        return self._token is not None

    # ------------------------------------------------------------------
    # 方法面
    # ------------------------------------------------------------------

    async def get_login_info(self) -> Ob11LoginInfoModel:
        """get_login_info——启动校验（登录账号探测；零副作用）。

        Returns:
            登录账号信息（user_id / nickname）。

        Raises:
            BusinessException: token 无效（``CHANNEL_TOKEN_MISSING``）；
                传输 / 状态 / 解析失败（``CHANNEL_API_ERROR``——映射表
                见模块 docstring）。
        """
        response = await self._call_action("get_login_info")
        data = response.data
        if data is None:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="NapCat OneBot API 调用失败（get_login_info）: 动作成功但响应缺少 data 载荷",
                details={
                    "reason": "NapCat OneBot HTTP API 调用失败",
                    "action": "get_login_info",
                },
            )
        try:
            return Ob11LoginInfoModel.model_validate(data)
        except ValidationError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="NapCat OneBot API 调用失败（get_login_info）: data 载荷不是合法的登录信息对象",
                details={
                    "reason": "NapCat OneBot HTTP API 调用失败",
                    "action": "get_login_info",
                    "parse_error": str(exc)[:200],
                },
            ) from exc

    async def send_private_msg(self, user_id: int, text: str) -> Ob11ApiResponseModel:
        """send_private_msg——私聊发送（``private:<user_id>`` 目标面）。

        Args:
            user_id: 目标用户 QQ 号（正整数）。
            text: 消息文本（非空——纯文本字符串形态，见模块 docstring）。

        Returns:
            动作响应包裹（投递映射经 ``to_delivery_result`` 承担）。

        Raises:
            BusinessException: 空文本（``VALIDATION_ERROR``——调用方 bug
                面）；token 无效（``CHANNEL_TOKEN_MISSING``）；传输 /
                状态 / 解析 / retcode 失败（``CHANNEL_API_ERROR``）。
        """
        self._require_text(user_id=user_id, text=text)
        return await self._call_action(
            "send_private_msg", params={"user_id": user_id, "message": text}
        )

    async def send_group_msg(self, group_id: int, text: str) -> Ob11ApiResponseModel:
        """send_group_msg——群聊发送（``group:<group_id>`` 目标面）。

        Args:
            group_id: 目标群号（正整数）。
            text: 消息文本（非空——纯文本字符串形态，见模块 docstring）。

        Returns:
            动作响应包裹（投递映射经 ``to_delivery_result`` 承担）。

        Raises:
            BusinessException: 空文本（``VALIDATION_ERROR``——调用方 bug
                面）；token 无效（``CHANNEL_TOKEN_MISSING``）；传输 /
                状态 / 解析 / retcode 失败（``CHANNEL_API_ERROR``）。
        """
        self._require_text(user_id=group_id, text=text)
        return await self._call_action(
            "send_group_msg", params={"group_id": group_id, "message": text}
        )

    # ------------------------------------------------------------------
    # 内部——请求与错误映射
    # ------------------------------------------------------------------

    @staticmethod
    def _require_text(*, user_id: int, text: str) -> None:
        """发送面文本校验（空 / 纯空白拒绝——调用方 bug 面）。

        Args:
            user_id: 目标 ID（错误 details 携带——观测面）。
            text: 消息文本。

        Raises:
            BusinessException: 空文本（``VALIDATION_ERROR``）。
        """
        if not text.strip():
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="QQ 消息文本不能为空",
                details={"target_id": user_id},
            )

    async def _call_action(
        self, action: str, *, params: dict[str, Any] | None = None
    ) -> Ob11ApiResponseModel:
        """发起动作请求（单一对象包裹 POST → 三级校验 → 响应包裹）。

        Args:
            action: OneBot v11 动作名。
            params: 动作参数（None = 无参数动作，如 get_login_info）。

        Returns:
            校验通过的动作响应包裹（``is_success`` 恒 True——失败形态
                已映射为业务异常抛出）。

        Raises:
            BusinessException: 传输异常 / 非 200 / JSON 非法 / retcode
                失败（``CHANNEL_API_ERROR``）；401/403（``CHANNEL_TOKEN_MISSING``）。
        """
        envelope = Ob11ApiRequestModel(action=action, params=params or {}).model_dump()
        headers: dict[str, str] = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(
                self._base_url, json=envelope, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                details={
                    "reason": "NapCat OneBot HTTP API 传输失败",
                    "action": action,
                    "error": str(exc),
                },
            ) from exc
        self._raise_for_status(response, action)
        try:
            wire = Ob11ApiResponseModel.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise self._api_error(
                action,
                response,
                reason="响应不是合法的 OneBot v11 JSON",
                parse_error=str(exc)[:200],
            ) from exc
        if not wire.is_success:
            raise self._api_error(
                action,
                response,
                reason=f"动作执行失败（status={wire.status or '(缺失)'}，retcode={wire.retcode}）",
                retcode=wire.retcode,
                status=wire.status,
            )
        return wire

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        """HTTP 状态检查（401/403 → token 无效；其余非 200 → API 错误）。

        Args:
            response: HTTP 响应。
            action: 动作名（错误 details 携带）。

        Raises:
            BusinessException: 见本方法 docstring 主体。
        """
        if response.status_code in (401, 403):
            raise BusinessException(
                code=ErrorCode.CHANNEL_TOKEN_MISSING,
                message="QQ access token 无效或与 NapCat 配置不一致",
                details=self._error_details(action, response),
            )
        if response.status_code != 200:
            raise self._api_error(action, response, reason=f"返回非成功状态 {response.status_code}")

    def _api_error(
        self,
        action: str,
        response: httpx.Response,
        *,
        reason: str,
        retcode: int | None = None,
        status: str | None = None,
        parse_error: str | None = None,
    ) -> BusinessException:
        """构造 ``CHANNEL_API_ERROR`` 业务异常（details 统一拼装）。

        Args:
            action: 动作名。
            response: HTTP 响应（片段截断携带）。
            reason: 失败类别描述。
            retcode: OneBot 返回码（retcode 失败形态携带）。
            status: OneBot 执行状态（retcode 失败形态携带）。
            parse_error: JSON 解析错误（解析失败形态携带）。

        Returns:
            待抛出的业务异常（调用方 ``raise ... from exc`` 保因果链）。
        """
        details = self._error_details(action, response)
        if retcode is not None:
            details["retcode"] = retcode
        if status is not None:
            details["ob11_status"] = status
        if parse_error is not None:
            details["parse_error"] = parse_error
        return BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            details=details,
            message=f"NapCat OneBot API 调用失败（{action}）: {reason}",
        )

    @staticmethod
    def _error_details(action: str, response: httpx.Response) -> dict[str, Any]:
        """拼装错误 details（动作 / 状态 / 响应体片段）。

        Args:
            action: 动作名。
            response: HTTP 响应。

        Returns:
            details 字典。
        """
        return {
            "reason": "NapCat OneBot HTTP API 调用失败",
            "action": action,
            "status_code": response.status_code,
            "body_snippet": response.text[:_BODY_SNIPPET_LIMIT],
        }
