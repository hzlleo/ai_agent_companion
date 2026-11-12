"""请求级上下文变量（ContextVars）+ 上下文快照绑定。

用于在中间件层 / 入口层与 Service / 日志 / LLM 用量采集之间传递
请求作用域状态，避免显式参数透传。

ContextVars 天然支持 asyncio 任务隔离：每个请求/会话在独立 task 中执行，
``set()`` 的值不会泄漏到其他请求。

上下文轴（陪伴智能体的全链路追踪面）：

- ``request_id_var``：请求/调用链追踪 ID（即 trace_id，入口层生成 UUID4）；
- ``user_id_var``：当前用户（智能体属主）ID；
- ``session_id_var``：当前会话（chat session）ID；
- ``persona_id_var``：当前激活人格 ID（多人格切换面）；
- ``locale_var``：当前请求 locale（从 ``Accept-Language`` 解析）。

``RequestContext`` 是上述轴的不可变快照 Model Class；``bind_context``
提供合并语义的作用域绑定（嵌套作用域仅覆盖显式传入字段，退出恢复），
``context_log_fields`` 输出日志 contextualize 字段（与 core.logging 的
默认 extra 对齐）。
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any

__all__ = [
    "RequestContext",
    "request_id_var",
    "user_id_var",
    "session_id_var",
    "persona_id_var",
    "locale_var",
    "current_context",
    "bind_context",
    "context_log_fields",
]

# 当前请求的追踪 ID（UUID4 字符串）；None 表示未设置（如离线脚本 / 后台任务）
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# 当前用户（智能体属主）ID；None 表示未认证 / 系统级操作
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)

# 当前会话（chat session）ID；None 表示非会话上下文
session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)

# 当前激活人格 ID；None 表示未绑定人格（默认人格 / 系统态）
persona_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "persona_id", default=None
)

# 当前请求的 locale（由中间件从 Accept-Language 解析后设置）
locale_var: contextvars.ContextVar[str] = contextvars.ContextVar("locale", default="zh-CN")


@dataclass(frozen=True)
class RequestContext:
    """请求上下文不可变快照。

    ``bind_context`` 绑定本对象到各 ContextVar；嵌套作用域通过
    ``dataclasses.replace`` 合并（子作用域仅覆盖显式传入的字段，其余
    继承外层）。

    Attributes:
        request_id: 请求/调用链追踪 ID（None 表示未设置）。
        user_id: 当前用户（智能体属主）ID。
        session_id: 当前会话 ID。
        persona_id: 当前激活人格 ID。
        locale: 当前 locale（默认 ``zh-CN``）。
    """

    request_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    persona_id: str | None = None
    locale: str = "zh-CN"


def current_context() -> RequestContext:
    """返回当前上下文快照（各 ContextVar 当前值的组合）。

    Returns:
        ``RequestContext`` 不可变快照；未设置的轴为 None / 默认值。
    """
    return RequestContext(
        request_id=request_id_var.get(),
        user_id=user_id_var.get(),
        session_id=session_id_var.get(),
        persona_id=persona_id_var.get(),
        locale=locale_var.get(),
    )


def _apply_context(ctx: RequestContext) -> None:
    """把快照字段写入各 ContextVar（模块内部辅助）。

    Args:
        ctx: 待应用的上下文快照。
    """
    request_id_var.set(ctx.request_id)
    user_id_var.set(ctx.user_id)
    session_id_var.set(ctx.session_id)
    persona_id_var.set(ctx.persona_id)
    locale_var.set(ctx.locale)


@asynccontextmanager
async def bind_context(**fields: str | None) -> AsyncIterator[RequestContext]:
    """绑定一段代码作用域的请求上下文。

    合并语义：以当前 :class:`RequestContext` 为基座，仅覆盖显式传入的
    ``fields``（``None`` 值同样生效——用于显式清空继承字段），其余字段
    继承外层。作用域退出时恢复原上下文（逐 var reset），嵌套作用域与
    并发 asyncio 任务天然隔离（ContextVars 复制语义）。

    用法::

        async with bind_context(session_id=sid, persona_id=pid):
            await chat_service.reply(...)

    Args:
        **fields: 要覆盖的上下文字段（RequestContext 的字段名）。

    Yields:
        合并后绑定的 :class:`RequestContext` 实例。
    """
    base = current_context()
    merged: dict[str, Any] = {**asdict(base), **fields}
    ctx = replace(base, **merged)
    _apply_context(ctx)
    try:
        yield ctx
    finally:
        _apply_context(base)


def context_log_fields() -> dict[str, str]:
    """输出日志 contextualize 字段（与 core.logging 默认 extra 对齐）。

    中间件/入口层用法::

        with logger.contextualize(**context_log_fields()):
            ...

    未设置的轴输出 ``-`` 占位（与 loguru ``logger.configure(extra=...)``
    的默认值一致），保证日志字段恒定存在，便于聚合系统解析。

    Returns:
        ``{"request_id", "session_id", "user_id", "persona_id"}`` 四键 dict。
    """
    ctx = current_context()
    return {
        "request_id": ctx.request_id or "-",
        "session_id": ctx.session_id or "-",
        "user_id": ctx.user_id or "-",
        "persona_id": ctx.persona_id or "-",
    }
