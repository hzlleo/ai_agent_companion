"""GUI 微信沙盒客户端——经仅回环 agent 的 ``/invoke`` 执行五工具调用。

依赖方向：wechat_gui → channels（模型 / 会话键）+ core（错误码）。传输面是
注入的共享 ``httpx.AsyncClient``（pools.PoolManager 下发——与 llm / channels
/ 沙盒客户端同硬规则，本类零连接资源）。

调用链::

    通道 runner / adapter
        ↓ screenshot / type_text / press_key / open_chat
    WechatGuiSandboxClient.invoke（POST /invoke——Bearer 鉴权）
        ↓ agent（docker/wechat-gui 树：stdlib HTTP + uinputd 控制面）
        ↓ 恒 200 携带 InvokeResponseModel（工具失败在载荷 ok=False 内表达）

失败语义（锁定）：所有传输面失败（token 缺失 / agent 不可达 / 非 200 /
wire 结构非法 / 工具 ok=False）统一归一 ``CHANNEL_API_ERROR``——runner 按
退避重试、适配器经路由器兜底隔离；chat_id grammar 非法归一
``VALIDATION_ERROR``（调用方 bug 面，见模型层）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from loguru import logger

from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WechatGuiOpenChatResultModel,
    WechatGuiScreenshotModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_wire_models import (
    WECHAT_GUI_HEALTH_PATH,
    WECHAT_GUI_INVOKE_PATH,
    WECHAT_GUI_TOOL_OPEN_CHAT,
    WECHAT_GUI_TOOL_PRESS_KEY,
    WECHAT_GUI_TOOL_SCREENSHOT,
    WECHAT_GUI_TOOL_TYPE_TEXT,
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
    OpenChatResultModel,
    PressKeyResultModel,
    ScreenshotResultModel,
    TypeTextResultModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = ["WechatGuiSandboxClient"]

_ResultT = TypeVar("_ResultT")


def _decode_result(
    tool: str, decoder: Callable[[object], _ResultT], payload: object
) -> _ResultT:
    """用镜像 Result Model 解码工具结果（字段缺失 / 类型错归一可重试错误）。

    Args:
        tool: 工具名（错误文案面）。
        decoder: 镜像 Result Model 的 ``from_wire``。
        payload: 工具结果对象。

    Returns:
        解码后的结果模型。

    Raises:
        BusinessException: 结果字段非法（``CHANNEL_API_ERROR``）。
    """
    try:
        return decoder(payload)
    except ValueError as exc:
        raise BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            message=f"GUI 微信工具 {tool} 结果字段非法: {exc}",
        ) from exc


class WechatGuiSandboxClient:
    """GUI 微信沙盒客户端（共享 httpx 池注入；恒不抛传输异常）。

    生命周期归注入方（共享池 / 测试 MockTransport client）；本类零连接资源。
    token 为空时不 fail fast（装配层据 :attr:`has_token` 跳过通道）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        """初始化客户端。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: agent 基础 URL（尾斜杠容错）。
            token: Bearer token（空串 = 未配置——invoke 短路失败）。
            request_timeout_seconds: 单次请求超时秒数。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_seconds)
        cleaned_token = token.strip()
        self._token: str | None = cleaned_token if cleaned_token else None
        if self._token is None:
            logger.warning("GUI 微信 agent token 未配置——invoke 将短路失败")

    @property
    def has_token(self) -> bool:
        """是否持有 Bearer token（False = 装配层应跳过该通道）。"""
        return self._token is not None

    @property
    def base_url(self) -> str:
        """agent 基础 URL（观测面）。"""
        return self._base_url

    async def probe_health(self) -> bool:
        """健康探测（GET /health——200 + status=ok；恒不抛）。

        Returns:
            agent 是否存活。
        """
        try:
            response = await self._http.get(
                f"{self._base_url}{WECHAT_GUI_HEALTH_PATH}", timeout=self._timeout
            )
        except httpx.HTTPError:
            logger.warning("GUI 微信 agent 健康探测失败（传输异常）: base_url={}", self._base_url)
            return False
        if response.status_code != 200:
            logger.warning(
                "GUI 微信 agent 健康探测非成功状态: base_url={}, status={}",
                self._base_url,
                response.status_code,
            )
            return False
        try:
            payload = response.json()
        except ValueError:
            logger.warning("GUI 微信 agent 健康探测响应非 JSON: base_url={}", self._base_url)
            return False
        if not isinstance(payload, dict):
            return False
        status = payload.get("status", HealthResponseModel().status)
        return isinstance(status, str) and status == "ok"

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> InvokeResponseModel:
        """执行一次 agent 工具调用（失败归一 ``CHANNEL_API_ERROR``）。

        Args:
            tool: 工具名。
            arguments: 调用参数。

        Returns:
            成功的工具响应（``result`` 已通过结构校验的原始对象）。

        Raises:
            BusinessException: token 缺失 / agent 不可达 / 非 200 / wire
                结构非法 / 工具 ``ok=False``（``CHANNEL_API_ERROR``）。
        """
        if self._token is None:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="GUI 微信 agent token 未配置（invoke 短路失败）",
            )
        request = InvokeRequestModel(tool=tool, arguments=arguments)
        try:
            response = await self._http.post(
                f"{self._base_url}{WECHAT_GUI_INVOKE_PATH}",
                json=request.to_wire(),
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "GUI 微信 agent 不可达: tool={}, error={}", tool, type(exc).__name__
            )
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"GUI 微信 agent 不可达: {type(exc).__name__}",
            ) from exc
        if response.status_code != 200:
            logger.warning(
                "GUI 微信 agent invoke 非成功状态: tool={}, status={}",
                tool,
                response.status_code,
            )
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"GUI 微信 agent 返回非成功状态 {response.status_code}",
            )
        try:
            payload = response.json()
            outcome = InvokeResponseModel.from_wire(payload)
        except ValueError as exc:
            logger.warning(
                "GUI 微信 agent 响应非法（wire 契约破坏）: tool={}, error={}",
                tool,
                str(exc)[:200],
            )
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message="GUI 微信 agent 响应非法（wire 契约破坏）",
            ) from exc
        if not outcome.ok:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"GUI 微信工具 {tool} 执行失败: {outcome.error or '未知原因'}",
            )
        return outcome

    # ------------------------------------------------------------------
    # 五工具
    # ------------------------------------------------------------------

    async def screenshot(self) -> WechatGuiScreenshotModel:
        """抓取沙盒当前屏幕。

        Returns:
            截图结果（base64 / sha256 / 尺寸）。

        Raises:
            BusinessException: 工具失败或结果字段非法（``CHANNEL_API_ERROR``）。
        """
        outcome = await self.invoke(WECHAT_GUI_TOOL_SCREENSHOT, {})
        result = _decode_result(
            WECHAT_GUI_TOOL_SCREENSHOT, ScreenshotResultModel.from_wire, outcome.result
        )
        return WechatGuiScreenshotModel(
            image_base64=result.image_base64,
            sha256=result.sha256,
            width=result.width,
            height=result.height,
        )

    async def type_text(self, text: str) -> None:
        """向当前焦点输入框输入文本（剪贴板粘贴）。

        Args:
            text: 待输入文本。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）。
        """
        outcome = await self.invoke(WECHAT_GUI_TOOL_TYPE_TEXT, {"text": text})
        _decode_result(
            WECHAT_GUI_TOOL_TYPE_TEXT, TypeTextResultModel.from_wire, outcome.result
        )

    async def press_key(self, key: str) -> None:
        """注入按键 / 组合键。

        Args:
            key: 按键表达式（如 ``enter`` / ``ctrl+f``）。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）。
        """
        outcome = await self.invoke(WECHAT_GUI_TOOL_PRESS_KEY, {"key": key})
        _decode_result(
            WECHAT_GUI_TOOL_PRESS_KEY, PressKeyResultModel.from_wire, outcome.result
        )

    async def open_chat(self, remark_name: str) -> WechatGuiOpenChatResultModel:
        """搜索并打开联系人会话。

        Args:
            remark_name: 联系人备注名。

        Returns:
            标题区域与全屏截图结果（标题须再经视觉校验）。

        Raises:
            BusinessException: 工具失败或结果字段非法（``CHANNEL_API_ERROR``）。
        """
        outcome = await self.invoke(
            WECHAT_GUI_TOOL_OPEN_CHAT, {"remark_name": remark_name}
        )
        result = _decode_result(
            WECHAT_GUI_TOOL_OPEN_CHAT, OpenChatResultModel.from_wire, outcome.result
        )
        if not result.title_changed:
            logger.warning(
                "GUI 微信 open_chat 标题区域在截止内未变化（仍以视觉标题校验落定）: "
                "remark_name={!r}",
                result.remark_name,
            )
        return WechatGuiOpenChatResultModel(
            remark_name=result.remark_name,
            title_image_base64=result.title_image_base64,
            title_sha256=result.title_sha256,
            full_image_base64=result.full_image_base64,
            full_sha256=result.full_sha256,
        )
