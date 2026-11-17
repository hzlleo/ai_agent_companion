"""sidecar 侧工具运行时——注册 / 隔离执行 / 计时（镜像后端 ToolRegistry 与
tool-sandbox SandboxToolRuntime 语义）。

职责链（单一执行入口 :meth:`BrowserSidecarRuntime.invoke`）::

    InvokeRequestModel(tool, arguments)
        ↓ ① 寻址（未知工具 → ok=False + error，不上抛）
        ↓ ② 参数校验（args_model 逐工具 Model Class 校验 + 默认值物化；
        ↓     失败 → ok=False + error）
        ↓ ③ 执行器调用（异步；:class:`BrowserSidecarError` 受控失败 → ok=False；
        ↓     其余异常捕获隔离 → ok=False——**invoke 恒不抛异常**）
        ↓ ④ 计时（epoch 毫秒差——成功 / 失败路径均计时 → latency_ms）

与后端 ToolRegistry / tool-sandbox 运行时的语义镜像关系（文档化决策）：

    - 「未知工具 / 参数校验失败 / 执行器异常」三类失败归一 ``ok=False``
      终态、恒不抛——与两侧 registry 逐类对齐；
    - 受控业务失败位由 :class:`BrowserSidecarError` 承担（工具预期失败面
      ——如导航策略拒绝 / 超时 / 标签页越界；错误消息为受控中文字符串）；
    - 时钟注入（epoch 毫秒）——测试确定性驱动（latency 断言面）。

执行器契约：``Callable[[dict], Awaitable[dict]]``——与后端
:data:`ToolExecutor` 同形态（浏览器工具按此接入，零 registry 改动）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from browser_sidecar.wire_models import InvokeRequestModel, InvokeResponseModel

__all__ = [
    "BrowserSidecarError",
    "BrowserSidecarExecutor",
    "BrowserSidecarToolSpec",
    "BrowserSidecarRuntime",
    "default_clock_ms",
]

#: 模块级 logger（stdlib logging——本树最小依赖约定，loguru 属后端栈）。
logger = logging.getLogger(__name__)


def default_clock_ms() -> int:
    """当前 epoch 毫秒（sidecar 树本地实现——零后端依赖）。

    Returns:
        当前 epoch 毫秒（如 ``1721012345678``）。
    """
    return int(time.time() * 1000)


class BrowserSidecarError(Exception):
    """工具受控失败（typed 预期失败面——运行时归一 ok=False 终态）。

    工具执行器以本异常声明「预期内的失败」（如导航策略拒绝 / 导航
    超时 / 标签页索引越界 / 截图体积超限）；消息为受控中文字符串
    （不携带敏感内容——token / 内部路径不入错误面）。

    Attributes:
        message: 人读失败原因（受控文案）。
    """

    def __init__(self, message: str) -> None:
        """初始化受控失败。

        Args:
            message: 人读失败原因（受控文案）。
        """
        super().__init__(message)
        self.message = message


#: sidecar 工具执行器契约——参数 dict → 结果 dict（异步；异常由运行时隔离）。
BrowserSidecarExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class BrowserSidecarToolSpec(BaseModel):
    """sidecar 工具规格（注册面——name / description / 参数校验模型）。

    与后端 :class:`ToolSpecModel` 的差异：本树规格不含 LLM 呈现面
    （JSON Schema 由后端侧持有并进入 LLM 请求 ``tools`` 面——sidecar
    只承接执行与校验）。

    Attributes:
        name: 工具名（运行时内唯一——重复注册后写覆盖前写）。
        description: 工具描述（文档面——不进 LLM）。
        args_model: 参数校验 Model Class（None = 不校验，参数原样透传；
            经 ``model_validate`` 归一参数并物化默认值；校验失败 →
            ok=False 结果，不上抛）。
    """

    name: str = Field(min_length=1, max_length=64, description="工具名（唯一标识）")
    description: str = Field(default="", description="工具描述（文档面）")
    args_model: type[BaseModel] | None = Field(
        default=None, description="参数校验 Model Class（None = 不校验）"
    )


class _ToolEntry:
    """注册表条目（规格 + 执行器——内部运行态，非传输契约）。

    Attributes:
        spec: 工具规格。
        executor: 执行器（异步——参数 dict → 结果 dict）。
    """

    __slots__ = ("spec", "executor")

    def __init__(self, spec: BrowserSidecarToolSpec, executor: BrowserSidecarExecutor) -> None:
        """初始化条目。

        Args:
            spec: 工具规格。
            executor: 执行器。
        """
        self.spec = spec
        self.executor = executor


class BrowserSidecarRuntime:
    """sidecar 工具运行时（注册 / 列举 / 隔离执行；invoke 恒不抛）。

    无全局状态：条目经 :meth:`register` 逐个注入（七浏览器工具经
    :func:`browser_sidecar.browser.register_browser_tools` 装配）；同名重复
    注册后写覆盖前写（与后端 registry 同语义——热装配覆盖面）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        """初始化运行时（空表 + 可注入时钟）。

        Args:
            clock: 时钟注入（epoch 毫秒；None = :func:`default_clock_ms`
                ——测试可控的 latency 断言面）。
        """
        self._entries: dict[str, _ToolEntry] = {}
        self._clock = clock if clock is not None else default_clock_ms

    # ------------------------------------------------------------------
    # 注册 / 列举
    # ------------------------------------------------------------------

    def register(self, spec: BrowserSidecarToolSpec, executor: BrowserSidecarExecutor) -> None:
        """注册工具（同名后写覆盖前写——装配面语义）。

        Args:
            spec: 工具规格（name 唯一键）。
            executor: 执行器（异步——参数 dict → 结果 dict）。
        """
        replaced = spec.name in self._entries
        self._entries[spec.name] = _ToolEntry(spec, executor)
        if replaced:
            logger.info("sidecar 工具已覆盖注册: %s", spec.name)
        else:
            logger.info("sidecar 工具已注册: %s", spec.name)

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名列表（注册序——观测面）。"""
        return list(self._entries)

    # ------------------------------------------------------------------
    # 执行（恒不抛异常）
    # ------------------------------------------------------------------

    async def invoke(self, request: InvokeRequestModel) -> InvokeResponseModel:
        """执行单次工具调用（寻址 → 校验 → 执行 → 计时；**恒不抛异常**）。

        Args:
            request: 调用请求（工具名 + 参数 dict）。

        Returns:
            调用结果（ok=False + error 携带人读原因——未知工具 / 参数
            校验失败 / 受控失败 / 执行器异常四类失败均归一，不上抛；
            sidecar 端点恒以 200 携带本形态）。
        """
        started_ms = self._clock()
        entry = self._entries.get(request.tool)
        if entry is None:
            return self._failure(
                started_ms,
                f"未知工具: {request.tool}（可用: {self.tool_names}）",
            )
        arguments = self._validate_arguments(entry, request.arguments)
        if arguments is None:
            return self._failure(started_ms, f"工具 {request.tool} 参数校验失败")
        try:
            result = await entry.executor(arguments)
        except BrowserSidecarError as exc:
            return self._failure(started_ms, exc.message)
        except Exception as exc:  # 执行器异常隔离——归一 ok=False，不上抛
            logger.warning(
                "sidecar 工具执行器异常（已隔离）: tool=%s, type=%s",
                request.tool,
                type(exc).__name__,
            )
            return self._failure(started_ms, f"工具执行器异常: {type(exc).__name__}")
        return InvokeResponseModel(
            ok=True,
            result=result,
            latency_ms=self._clock() - started_ms,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _validate_arguments(
        self, entry: _ToolEntry, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """参数校验（args_model 存在时经 Model Class 归一 + 默认值物化）。

        Args:
            entry: 注册表条目。
            arguments: 原始参数 dict。

        Returns:
            校验通过并物化默认值后的参数 dict；校验失败返回 None
            （错误细节已落日志——结果面收敛为统一失败文案）。
        """
        args_model = entry.spec.args_model
        if args_model is None:
            return arguments
        try:
            validated: BaseModel = args_model.model_validate(arguments)
        except ValidationError as exc:
            logger.warning(
                "sidecar 工具参数校验失败: tool=%s, errors=%s",
                entry.spec.name,
                str(exc)[:300],
            )
            return None
        return validated.model_dump()

    def _failure(self, started_ms: int, error: str) -> InvokeResponseModel:
        """失败结果归一（时延含失败路径耗时）。

        Args:
            started_ms: 执行起始 epoch 毫秒。
            error: 人读失败原因。

        Returns:
            ok=False 的调用结果。
        """
        return InvokeResponseModel(
            ok=False,
            error=error,
            latency_ms=self._clock() - started_ms,
        )
