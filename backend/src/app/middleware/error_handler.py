"""全局异常处理器注册——BusinessException / 请求校验错误 → 统一 ResultDTO。

app 装配（app 组装点）经 :func:`register_exception_handlers` 一次性挂接
全部共享异常处理器；本模块**只注册不实现中间件**（FastAPI 异常处理器
即全局兜底层，无需 ASGI 中间件形态）。

映射契约：

    - ``BusinessException`` → HTTP 状态取 ``exc.http_status``
      （ErrorCode → HTTP 映射，见 harness.core.errors）；响应体为
      ``ResultDTO`` error 形态（``success=False / code / message``，
      ``data`` 携带 ``exc.details`` 结构化上下文）；``exc.headers``
      原样透传（可重试类错误码的 Retry-After 等节奏提示头）。
    - ``RequestValidationError``（FastAPI 请求参数 / 请求体校验失败）→
      **HTTP 400** + ``ResultDTO`` error 形态（``code=VALIDATION_ERROR``，
      ``data`` 携带逐字段错误列表 ``[{loc, msg, type}]``）。

      注：本工程 HTTP 面约定请求校验失败统一 400（客户端请求形态错
      误）；``ErrorCode.VALIDATION_ERROR`` 携带的 422 状态仅用于
      BusinessException 语义（服务层业务校验），两者按层分工不冲突。
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.types import ExceptionHandler

from packages.harness.core.dtos import ResultDTO
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.core.i18n import get_message

__all__ = [
    "business_exception_handler",
    "validation_exception_handler",
    "register_exception_handlers",
]


async def business_exception_handler(request: Request, exc: BusinessException) -> JSONResponse:
    """业务异常处理器——ErrorCode → HTTP 状态映射 + ResultDTO error 形态。

    Args:
        request: 触发异常的请求（日志定位用）。
        exc: 业务异常（code / message / details / headers 已解析）。

    Returns:
        ``JSONResponse``：HTTP 状态 = ``exc.http_status``；响应体 =
        ``ResultDTO`` error 形态（``data`` 携带 ``exc.details``）；
        ``exc.headers`` 原样透传。
    """
    logger.warning(
        "业务异常: path={}, code={}, http_status={}, message={}",
        request.url.path,
        exc.code,
        exc.http_status,
        exc.message,
    )
    payload = ResultDTO[dict[str, Any]](
        success=False,
        code=exc.code,
        message=exc.message,
        data=exc.details,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=payload.model_dump(),
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求校验失败处理器——HTTP 400 + ResultDTO error 形态。

    Args:
        request: 触发校验失败的请求。
        exc: FastAPI 请求校验异常（携带逐字段错误列表）。

    Returns:
        ``JSONResponse``：HTTP 400；响应体 = ``ResultDTO`` error 形态
        （``code=VALIDATION_ERROR``；``data.errors`` 为
        ``[{loc, msg, type}]`` 列表）。
    """
    errors = [
        {
            "loc": list(err.get("loc", [])),
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    logger.warning(
        "请求校验失败: path={}, errors={}",
        request.url.path,
        errors,
    )
    payload = ResultDTO[dict[str, list[dict[str, Any]]]](
        success=False,
        code=ErrorCode.VALIDATION_ERROR.value,
        message=get_message(ErrorCode.VALIDATION_ERROR.value),
        data={"errors": errors},
    )
    return JSONResponse(
        status_code=400,
        content=payload.model_dump(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """向 app 挂接全部共享异常处理器（app 装配点一次性调用）。

    重复调用安全：``add_exception_handler`` 按异常类型覆写注册（幂等）。

    注：Starlette 的 ``ExceptionHandler`` 类型面按基类 ``Exception`` 声明
    （``Callable[[Request, Exception], ...]``），而运行期派发按注册的
    具体异常类匹配——本模块的窄类型处理器（``BusinessException`` /
    ``RequestValidationError``）经 ``cast`` 适配注册面（类型断言而非
    抑制注释；窄化语义由 Starlette 派发契约保证）。

    Args:
        app: 待挂接的 FastAPI 应用实例。
    """
    app.add_exception_handler(BusinessException, cast(ExceptionHandler, business_exception_handler))
    app.add_exception_handler(
        RequestValidationError, cast(ExceptionHandler, validation_exception_handler)
    )
