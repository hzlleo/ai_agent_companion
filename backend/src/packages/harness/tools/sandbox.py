"""harness/tools 域——工具沙盒客户端 + registry 代理注册（共享池出站）。

职责链（duty 工具循环 → 沙盒工具的完整路径）::

    duty 轮 tool_calls
        ↓ registry.execute(name, arguments)（tools/registry——隔离 + 计时）
        ↓ 代理执行器（本模块 register_sandbox_tool 注册的闭包）
        ↓ ToolSandboxClient.invoke（POST /invoke——Bearer 鉴权；**恒不抛**）
        ↓ 沙盒 ASGI 端点（docker/tool-sandbox-agent 树——token 恒时比较）
        ↓ 沙盒侧运行时（寻址 → 参数校验 → 执行器 → 计时；恒不抛）
        ↓ 工具执行器（如 http_fetch——域名允许清单 + 8KB 截断）

设计决策：

    - **共享 httpx 池**：客户端不私建连接——出站一律经注入的共享
      ``httpx.AsyncClient``（pools.PoolManager 下发——与 llm / channels
      域同硬规则）；
    - **token 环境变量间接**（``token_env``）：密钥永不落配置树（与
      llm ``api_key_env`` / telegram ``bot_token_env`` 同模式）；缺失 /
      为空**不 fail fast**——沙盒是可选组件（has_token=False 由装配层
      跳过注册，warn-and-continue；invoke 短路失败归一 ok=False）；
    - **恒不抛**：token 缺失 / 传输异常 / 非成功状态 / wire 破坏 /
      沙盒工具失败五类失败均归一 :class:`ToolResultModel` ok=False 终态
      （duty 工具循环恒不见异常——与 registry.execute 同契约）；
    - **registry executor 无关**：:func:`register_sandbox_tool` 以闭包
      代理接入（registry 零改动——代理失败转 ``INTERNAL_ERROR`` 业务
      异常，经 registry 归一 ok=False）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError

from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.time_utils import now_ms
from packages.harness.tools.registry import ToolRegistry
from packages.harness.tools.sandbox_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
    SandboxClientFace,
    SandboxSettingsFace,
)
from packages.harness.tools.tool_models import ToolResultModel, ToolSpecModel

__all__ = [
    "ToolSandboxClient",
    "build_sandbox_client",
    "register_sandbox_tool",
]


class ToolSandboxClient:
    """工具沙盒客户端（共享 ``httpx.AsyncClient`` 注入；恒不抛）。

    生命周期归注入方（pools.PoolManager / 测试 MockTransport client）；
    本类零连接资源。构造期从 ``token_env`` 指定的环境变量解析 Bearer
    token；缺失 / 为空时不 fail fast（可选组件语义——
    :attr:`has_token` 为 False，装配层据此跳过沙盒工具注册）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str = "http://127.0.0.1:8765",
        token_env: str = "SANDBOX_TOKEN",
        request_timeout_seconds: float = 15.0,
    ) -> None:
        """初始化客户端（token 环境变量间接解析——缺失仅告警不 fail fast）。

        Args:
            http_client: 共享 HTTP 客户端（硬规则：不得私建连接）。
            base_url: 沙盒服务基础 URL（尾斜杠容错）。
            token_env: 鉴权 token 环境变量名（实际 token 从该变量读取）。
            request_timeout_seconds: 单次请求超时秒数（invoke / 健康探测
                共用——启动期探测的 bounded 上限）。
        """
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(request_timeout_seconds)
        token = os.environ.get(token_env, "").strip()
        self._token: str | None = token if token else None
        if self._token is None:
            logger.warning(
                "工具沙盒 token 未配置（环境变量 {} 为空）——invoke 将短路失败",
                token_env,
            )

    @property
    def has_token(self) -> bool:
        """是否持有 Bearer token（False = 装配层应跳过沙盒工具注册）。"""
        return self._token is not None

    @property
    def base_url(self) -> str:
        """沙盒服务基础 URL（观测面——探测失败告警携带）。"""
        return self._base_url

    # ------------------------------------------------------------------
    # 方法面
    # ------------------------------------------------------------------

    async def probe_health(self) -> bool:
        """健康探测（GET /health——200 + status=ok 判定；恒不抛）。

        Returns:
            沙盒是否存活（传输异常 / 非 200 / 载荷非法 / status 非 ok
            均为 False——装配层 warn-and-continue 判定面）。
        """
        try:
            response = await self._http.get(f"{self._base_url}/health", timeout=self._timeout)
        except httpx.HTTPError:
            logger.warning("工具沙盒健康探测失败（传输异常）: base_url={}", self._base_url)
            return False
        if response.status_code != 200:
            logger.warning(
                "工具沙盒健康探测非成功状态: base_url={}, status={}",
                self._base_url,
                response.status_code,
            )
            return False
        try:
            health = HealthResponseModel.model_validate_json(response.content)
        except ValidationError:
            logger.warning("工具沙盒健康探测响应非法: base_url={}", self._base_url)
            return False
        return health.status == "ok"

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """执行沙盒工具（POST /invoke——Bearer 鉴权；**恒不抛**）。

        Args:
            tool: 工具名（沙盒内唯一标识）。
            arguments: 调用参数（registry 校验物化后的 dict）。

        Returns:
            执行结果（ok=False + error 携带失败原因——token 未配置 /
            沙盒不可达 / 非成功状态 / wire 契约破坏 / 沙盒工具失败；
            成功时 result = 沙盒工具结果 dict、latency_ms = 沙盒侧
            执行时延；失败时 latency_ms = 客户端侧往返耗时）。
        """
        started_ms = now_ms()
        if self._token is None:
            return self._failure(tool, started_ms, "沙盒 token 未配置（invoke 短路失败）")
        request = InvokeRequestModel(tool=tool, arguments=arguments)
        try:
            response = await self._http.post(
                f"{self._base_url}/invoke",
                json=request.model_dump(),
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "工具沙盒不可达: tool={}, error={}", tool, type(exc).__name__
            )
            return self._failure(tool, started_ms, f"沙盒不可达: {type(exc).__name__}")
        if response.status_code != 200:
            logger.warning(
                "工具沙盒 invoke 非成功状态: tool={}, status={}",
                tool,
                response.status_code,
            )
            return self._failure(tool, started_ms, f"沙盒返回非成功状态 {response.status_code}")
        try:
            wire = InvokeResponseModel.model_validate_json(response.content)
        except ValidationError as exc:
            logger.warning(
                "工具沙盒响应非法（wire 契约破坏）: tool={}, errors={}",
                tool,
                str(exc)[:200],
            )
            return self._failure(tool, started_ms, "沙盒响应非法（wire 契约破坏）")
        if not wire.ok:
            return ToolResultModel(
                tool=tool,
                ok=False,
                error=wire.error or "沙盒工具执行失败",
                latency_ms=wire.latency_ms,
            )
        return ToolResultModel(
            tool=tool,
            ok=True,
            result=wire.result,
            latency_ms=wire.latency_ms,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _failure(tool: str, started_ms: int, error: str) -> ToolResultModel:
        """失败终态归一（时延 = 客户端侧往返耗时）。

        Args:
            tool: 工具名（echo 回调用面）。
            started_ms: 请求起始 epoch 毫秒。
            error: 人读失败原因。

        Returns:
            ok=False 的执行结果。
        """
        return ToolResultModel(
            tool=tool,
            ok=False,
            error=error,
            latency_ms=now_ms() - started_ms,
        )


def build_sandbox_client(
    http_client: httpx.AsyncClient, settings: SandboxSettingsFace
) -> ToolSandboxClient:
    """从配置面构造沙盒客户端（app 装配消费——共享池 client 注入）。

    Args:
        http_client: 共享 HTTP 客户端（pools.PoolManager 下发）。
        settings: 沙盒配置面（真实 ``Settings.tools_sandbox`` 分节 /
            测试注入均可——结构化协议）。

    Returns:
        客户端实例（token 经 ``settings.token_env`` 环境变量间接解析）。
    """
    return ToolSandboxClient(
        http_client,
        base_url=settings.base_url,
        token_env=settings.token_env,
        request_timeout_seconds=settings.request_timeout_seconds,
    )


def register_sandbox_tool(
    registry: ToolRegistry,
    sandbox_client: SandboxClientFace,
    spec: ToolSpecModel,
) -> None:
    """注册沙盒代理工具（executor 闭包代理 /invoke——registry 保持 executor 无关）。

    失败语义：客户端恒不抛（ok=False 终态）；代理执行器把 ok=False 转
    ``INTERNAL_ERROR`` 业务异常（沙盒侧基础设施失败面——无新增错误码，
    复用既有码表），经 registry 归一 ok=False——duty 工具循环恒不见异常。

    Args:
        registry: 工具注册表（装配目标——同名后写覆盖前写）。
        sandbox_client: 沙盒客户端（真实 :class:`ToolSandboxClient` /
            测试 fake 均可——结构化协议）。
        spec: 工具规格（后端侧 LLM 呈现面——如
            :func:`~packages.harness.tools.sandbox_models.build_http_fetch_spec`）。
    """

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        """代理执行（沙盒失败 → BusinessException → registry 归一 ok=False）。"""
        outcome = await sandbox_client.invoke(spec.name, args)
        if not outcome.ok:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message=f"沙盒工具 {spec.name} 执行失败: {outcome.error or '未知原因'}",
            )
        return outcome.result

    registry.register(spec, _execute)
