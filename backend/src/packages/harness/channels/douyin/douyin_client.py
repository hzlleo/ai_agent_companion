"""douyin 通道浏览器客户端——经浏览器 sidecar ``evaluate`` 驱动抖音 web 会话。

依赖方向（同底座约束）：douyin → channels（模型 / 路由）+ tools（``evaluate``
工具名与 ``ToolResultModel``）+ core（错误码 / 时间）。**不新建浏览器连接**：
传输面是注入的 :class:`DouyinBrowserInvoker`——生产形态是 app 装配的
``BrowserSidecarClient``（共享 httpx 池 + Bearer token + 持久化 profile 的已
登录抖音页面），测试形态是记录式 fake。因此本模块零真实网络、零 playwright
依赖。

调用链::

    通道 runner
        ↓ open_chat（可选——配置了 DOUYIN_CHAT_URL 时经 navigate 工具打开）
        ↓ evaluate（douyin_scripts 的采集 / 发送表达式）
    BrowserSidecarClient.invoke（共享池 → sidecar → 已登录页面）
        ↓ 采集结果 → DouyinChatSnapshotModel（wire 校验）
        ↓ 发送结果 → 成功返回 / 失败抛 CHANNEL_API_ERROR

失败语义：所有浏览器面失败（sidecar 不可达 / token 缺失 / 工具失败 / wire
结构非法）统一归一 ``CHANNEL_API_ERROR``（可重试面——runner 退避）；
chat_id grammar 非法归一 ``VALIDATION_ERROR``（调用方 bug 面）。
"""

from __future__ import annotations

from typing import Any, Protocol

from loguru import logger

from packages.harness.channels.douyin.douyin_models import DouyinChatSnapshotModel
from packages.harness.channels.douyin.douyin_scripts import (
    build_douyin_collect_script,
    build_douyin_send_script,
)
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.tools.browser_models import EVALUATE_TOOL_NAME, NAVIGATE_TOOL_NAME
from packages.harness.tools.tool_models import ToolResultModel

__all__ = [
    "DOUYIN_EVALUATE_TIMEOUT_MS",
    "DOUYIN_MAX_CHARS",
    "DouyinBrowserInvoker",
    "DouyinWebClient",
]

#: 单次页面求值的字符上限（采集快照规模指引——超限受控失败而非截断）。
DOUYIN_MAX_CHARS: int = 262144

#: 单次页面求值的超时毫秒（DOM 采集 / 发送均为轻操作——秒级足够）。
DOUYIN_EVALUATE_TIMEOUT_MS: int = 15000


class DouyinBrowserInvoker(Protocol):
    """浏览器工具调用面协议（结构类型——app 注入真实客户端 / 测试注入 fake）。

    生产实现为 ``packages.harness.tools.browser.BrowserSidecarClient``（同
    签名 ``invoke``），因此本通道不持有任何 HTTP 连接资源。
    """

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """执行一次浏览器工具调用。

        Args:
            tool: 工具名（``navigate`` / ``evaluate``）。
            arguments: 调用参数（JSON 对象）。

        Returns:
            工具结果（失败时 ``ok=False`` + ``error``；恒不抛）。
        """
        ...


class DouyinWebClient:
    """抖音 web 会话客户端（采集 / 发送——经浏览器底座执行）。

    页面生命周期归 sidecar（持久化 context 承载登录态）：本类只在配置了
    ``chat_url`` 时调用一次 ``navigate`` 打开会话页，之后每次采集 / 发送都在
    **当前活动页**执行。运维侧也可以不配置 URL，由人工把页面停在会话页。
    """

    def __init__(
        self,
        *,
        invoker: DouyinBrowserInvoker,
        chat_url: str = "",
        evaluate_timeout_ms: int = DOUYIN_EVALUATE_TIMEOUT_MS,
    ) -> None:
        """初始化客户端。

        Args:
            invoker: 浏览器工具调用面（生产 = sidecar 客户端；测试 = fake）。
            chat_url: 会话页 URL（空串 = 不导航，沿用当前页面）。
            evaluate_timeout_ms: 单次求值超时毫秒（透传给 ``evaluate`` 工具）。
        """
        self._invoker = invoker
        self._chat_url = chat_url.strip()
        self._evaluate_timeout_ms = evaluate_timeout_ms

    @property
    def chat_url(self) -> str:
        """会话页 URL（空串 = 不导航——装配观测面）。"""
        return self._chat_url

    async def open_chat(self) -> None:
        """打开会话页（配置了 URL 时——导航失败的策略链原因原样上抛）。

        Raises:
            BusinessException: 导航失败（``CHANNEL_API_ERROR``——允许清单
                拒绝 / 私网拒绝 / 超时 / sidecar 不可达）。
        """
        if not self._chat_url:
            return
        outcome = await self._invoker.invoke(
            NAVIGATE_TOOL_NAME, {"url": self._chat_url}
        )
        if not outcome.ok:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"抖音会话页打开失败: {outcome.error or '未知原因'}",
            )
        logger.info("抖音 web 会话页已打开: url={}", self._chat_url)

    async def fetch_snapshot(
        self, *, conversation_id: str, limit: int
    ) -> DouyinChatSnapshotModel:
        """采集会话快照（消息列表——时间序）。

        Args:
            conversation_id: 期望的会话标识（空串 = 由页面 URL 推断）。
            limit: 单次采集消息上限（脚本取时间序末尾）。

        Returns:
            解析后的会话快照（``conversation_id`` 可能为空——调用方按配置
            兜底）。

        Raises:
            BusinessException: 浏览器面失败 / 返回结构非法
                （``CHANNEL_API_ERROR``——可重试面）。
        """
        script = build_douyin_collect_script(conversation_id=conversation_id, limit=limit)
        payload = await self._evaluate(script, action="会话采集")
        try:
            return DouyinChatSnapshotModel.model_validate(payload)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"抖音会话快照结构非法: {exc}",
            ) from exc

    async def send_text(self, *, text: str) -> None:
        """向当前会话发送文本（写入输入框并触发发送）。

        Args:
            text: 待发送文本。

        Raises:
            BusinessException: 浏览器面失败 / 脚本报告发送失败
                （``CHANNEL_API_ERROR``——路由器兜底隔离）。
        """
        script = build_douyin_send_script(text=text)
        payload = await self._evaluate(script, action="文本发送")
        if payload.get("ok") is not True:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"抖音文本发送失败: {payload.get('error') or '页面脚本报告失败'}",
            )

    async def _evaluate(self, script: str, *, action: str) -> dict[str, Any]:
        """执行页面脚本并取出结果对象（工具失败 / 结构非法归一可重试错误）。

        Args:
            script: 自执行 JS 表达式。
            action: 动作名（日志与错误文案面）。

        Returns:
            脚本返回的 JSON 对象（``result.result``）。

        Raises:
            BusinessException: 工具失败（``CHANNEL_API_ERROR``）；返回值不是
                JSON 对象（``CHANNEL_API_ERROR``）。
        """
        outcome = await self._invoker.invoke(
            EVALUATE_TOOL_NAME,
            {
                "script": script,
                "max_chars": DOUYIN_MAX_CHARS,
                "timeout_ms": self._evaluate_timeout_ms,
            },
        )
        if not outcome.ok:
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"抖音{action}失败: {outcome.error or '未知原因'}",
            )
        payload = outcome.result.get("result")
        if not isinstance(payload, dict):
            raise BusinessException(
                code=ErrorCode.CHANNEL_API_ERROR,
                message=f"抖音{action}返回结构非法（期望 JSON 对象）: {type(payload).__name__}",
            )
        return payload
