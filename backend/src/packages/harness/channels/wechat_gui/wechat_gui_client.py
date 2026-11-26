"""GUI 微信沙盒客户端——经仅回环 agent 的 ``/invoke`` 执行五工具调用。

依赖方向：wechat_gui → channels（模型 / 会话键）+ core（错误码）。传输面是
注入的共享 ``httpx.AsyncClient``（pools.PoolManager 下发——与 llm / channels
/ 沙盒客户端同硬规则，本类零连接资源）。

调用链::

    通道 runner / adapter
        ↓ screenshot / click / type_text / press_key / open_chat
    WechatGuiSandboxClient.invoke（POST /invoke——Bearer 鉴权）
        ↓ agent（docker/wechat-gui 树：stdlib HTTP + uinputd 控制面）
        ↓ 恒 200 携带 InvokeResponseModel（工具失败在载荷 ok=False 内表达）

失败语义（锁定）：所有传输面失败（token 缺失 / agent 不可达 / 非 200 /
wire 结构非法 / 工具 ok=False）统一归一 ``CHANNEL_API_ERROR``——runner 按
退避重试、适配器经路由器兜底隔离；chat_id grammar 非法归一
``VALIDATION_ERROR``（调用方 bug 面，见模型层）。
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from packages.harness.channels.wechat_gui.wechat_gui_models import (
    WechatGuiOpenChatResultModel,
    WechatGuiScreenshotModel,
)
from packages.harness.channels.wechat_gui.wechat_gui_wire_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
)
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "WECHAT_GUI_HEALTH_PATH",
    "WECHAT_GUI_INVOKE_PATH",
    "WECHAT_GUI_TOOL_CLICK",
    "WECHAT_GUI_TOOL_OPEN_CHAT",
    "WECHAT_GUI_TOOL_PRESS_KEY",
    "WECHAT_GUI_TOOL_SCREENSHOT",
    "WECHAT_GUI_TOOL_TYPE_TEXT",
    "WechatGuiSandboxClient",
]

#: agent invoke 端点路径（沙盒树同名常量镜像——部署契约面）。
WECHAT_GUI_INVOKE_PATH: str = "/invoke"

#: agent health 端点路径（同上）。
WECHAT_GUI_HEALTH_PATH: str = "/health"

#: screenshot 工具名。
WECHAT_GUI_TOOL_SCREENSHOT: str = "screenshot"
#: click 工具名。
WECHAT_GUI_TOOL_CLICK: str = "click"
#: type_text 工具名。
WECHAT_GUI_TOOL_TYPE_TEXT: str = "type_text"
#: press_key 工具名。
WECHAT_GUI_TOOL_PRESS_KEY: str = "press_key"
#: open_chat 工具名。
WECHAT_GUI_TOOL_OPEN_CHAT: str = "open_chat"


def _decode_invoke_response(payload: object) -> InvokeResponseModel:
    """校验并解码 agent invoke 响应载荷。

    Args:
        payload: 已解析的 JSON 载荷。

    Returns:
        响应模型。

    Raises:
        ValueError: 根节点非对象 / 字段缺失或类型不符。
    """
    if not isinstance(payload, dict):
        raise ValueError("响应根节点必须是对象")
    ok = payload.get("ok")
    result = payload.get("result", {})
    error = payload.get("error")
    latency_ms = payload.get("latency_ms", 0)
    if not isinstance(ok, bool):
        raise ValueError("字段 ok 必须是布尔值")
    if not isinstance(result, dict):
        raise ValueError("字段 result 必须是对象")
    if error is not None and not isinstance(error, str):
        raise ValueError("字段 error 必须是字符串或 null")
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
        raise ValueError("字段 latency_ms 必须是非负整数")
    return InvokeResponseModel(
        ok=ok,
        result=dict(result),
        error=error,
        latency_ms=latency_ms,
    )


def _require_str(result: dict[str, Any], key: str, tool: str) -> str:
    """读取工具结果中的必填字符串字段。

    Args:
        result: 工具结果对象。
        key: 字段名。
        tool: 工具名（错误文案面）。

    Returns:
        字段值。

    Raises:
        BusinessException: 字段缺失或类型不符（``CHANNEL_API_ERROR``）。
    """
    value = result.get(key)
    if not isinstance(value, str) or not value:
        raise BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            message=f"GUI 微信工具 {tool} 结果缺少合法字段 {key}",
        )
    return value


def _require_int(result: dict[str, Any], key: str, tool: str) -> int:
    """读取工具结果中的必填正整数字段。

    Args:
        result: 工具结果对象。
        key: 字段名。
        tool: 工具名（错误文案面）。

    Returns:
        字段值。

    Raises:
        BusinessException: 字段缺失或类型不符（``CHANNEL_API_ERROR``）。
    """
    value = result.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BusinessException(
            code=ErrorCode.CHANNEL_API_ERROR,
            message=f"GUI 微信工具 {tool} 结果缺少合法字段 {key}",
        )
    return value


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
            outcome = _decode_invoke_response(payload)
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
        return WechatGuiScreenshotModel(
            image_base64=_require_str(
                outcome.result, "image_base64", WECHAT_GUI_TOOL_SCREENSHOT
            ),
            sha256=_require_str(outcome.result, "sha256", WECHAT_GUI_TOOL_SCREENSHOT),
            width=_require_int(outcome.result, "width", WECHAT_GUI_TOOL_SCREENSHOT),
            height=_require_int(outcome.result, "height", WECHAT_GUI_TOOL_SCREENSHOT),
        )

    async def click(self, x: int, y: int) -> None:
        """在绝对坐标点击（左键）。

        Args:
            x: X 坐标（像素）。
            y: Y 坐标（像素）。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）。
        """
        await self.invoke(WECHAT_GUI_TOOL_CLICK, {"x": x, "y": y})

    async def type_text(self, text: str) -> None:
        """向当前焦点输入框输入文本（剪贴板粘贴）。

        Args:
            text: 待输入文本。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）。
        """
        await self.invoke(WECHAT_GUI_TOOL_TYPE_TEXT, {"text": text})

    async def press_key(self, key: str) -> None:
        """注入按键 / 组合键。

        Args:
            key: 按键表达式（如 ``enter`` / ``ctrl+f``）。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）。
        """
        await self.invoke(WECHAT_GUI_TOOL_PRESS_KEY, {"key": key})

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
        return WechatGuiOpenChatResultModel(
            remark_name=_require_str(
                outcome.result, "remark_name", WECHAT_GUI_TOOL_OPEN_CHAT
            ),
            title_image_base64=_require_str(
                outcome.result, "title_image_base64", WECHAT_GUI_TOOL_OPEN_CHAT
            ),
            title_sha256=_require_str(
                outcome.result, "title_sha256", WECHAT_GUI_TOOL_OPEN_CHAT
            ),
            full_image_base64=_require_str(
                outcome.result, "full_image_base64", WECHAT_GUI_TOOL_OPEN_CHAT
            ),
            full_sha256=_require_str(
                outcome.result, "full_sha256", WECHAT_GUI_TOOL_OPEN_CHAT
            ),
        )
