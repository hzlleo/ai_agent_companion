"""工具沙盒代理 —— ASGI 入口薄层（app 工厂 + uvicorn main）。

职责边界（本文件只做「入口」——工具语义全部在 ``tool_sandbox`` 包内）：

    - :func:`create_app`  —— FastAPI 工厂（token 鉴权依赖 + ``/health`` /
      ``/invoke`` 两端点 + 默认运行时装配 http_fetch + B 站只读工具组）；
    - :func:`main`        —— uvicorn 启动（host/port 经 env 覆写）。

鉴权（安全红线）：

    - ``POST /invoke`` 须携带 ``Authorization: Bearer <token>``；token 从
      env ``SANDBOX_TOKEN`` 读取，**恒时比较**（:func:`constant_time_token_matches`
      —— ``secrets.compare_digest``，防时序侧信道）；
    - 全部鉴权失败统一 401 + 统一 detail（「鉴权失败」——不区分头缺失 /
      形态错误 / token 不匹配，杜绝错误面 oracle）；
    - ``SANDBOX_TOKEN`` 缺失 / 为空 → app 工厂 fail fast（安全边界无
      token 拒绝启动—— RuntimeError，中文指引）；
    - token 永不入日志 / 错误响应。

端点面：

    - ``GET /health``  —— 无鉴权存活面（容器 HEALTHCHECK 消费；恒
      ``{"status": "ok"}``）；
    - ``POST /invoke`` —— 鉴权面；请求 :class:`InvokeRequestModel`，响应
      恒 200 携带 :class:`InvokeResponseModel`（运行时恒不抛——工具失败
      在载荷 ``ok=False`` 终态内表达；请求形态非法由 FastAPI 归 422）。
"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

from tool_sandbox.bili_tools import register_bili_tools
from tool_sandbox.http_fetch import build_http_fetch_tool
from tool_sandbox.runtime import SandboxToolRuntime
from tool_sandbox.wire_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
)

__all__ = [
    "constant_time_token_matches",
    "create_app",
    "main",
    "resolve_bind_address",
]

#: 鉴权 token 环境变量（容器 / compose 注入；缺失 = 工厂 fail fast）。
_TOKEN_ENV: str = "SANDBOX_TOKEN"

#: 监听地址环境变量（容器形态默认 0.0.0.0——端口映射由编排层收敛到回环；
#: 裸机运行建议显式 127.0.0.1，见本目录 README.md「运行」一节）。
_HOST_ENV: str = "SANDBOX_HOST"

#: 监听端口环境变量（与 compose 端口映射契约一致：8765）。
_PORT_ENV: str = "SANDBOX_PORT"

#: 缺省监听地址 / 端口（容器形态）。
_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_PORT: int = 8765


def constant_time_token_matches(provided: str, expected: str) -> bool:
    """恒时 token 比较（``secrets.compare_digest``——防时序侧信道）。

    Args:
        provided: 调用方提供的 token（Bearer 载荷）。
        expected: 期望 token（env 解析产物）。

    Returns:
        是否一致（byte 级比较——比较耗时与内容无关）。
    """
    return secrets.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


def resolve_bind_address() -> tuple[str, int]:
    """解析监听地址（env 覆写；缺省容器形态 0.0.0.0:8765）。

    Returns:
        ``(host, port)`` 二元组（``SANDBOX_HOST`` / ``SANDBOX_PORT``）。
    """
    host = os.environ.get(_HOST_ENV, _DEFAULT_HOST)
    try:
        port = int(os.environ.get(_PORT_ENV, str(_DEFAULT_PORT)))
    except ValueError as exc:
        raise RuntimeError(f"{_PORT_ENV} 必须是整数端口号") from exc
    return host, port


def create_app(*, runtime: SandboxToolRuntime | None = None) -> FastAPI:
    """构造 FastAPI 应用（token 鉴权 + 两端点 + 默认工具装配）。

    Args:
        runtime: 沙盒工具运行时（None = 新建并注册 http_fetch + B 站只读
            工具组——生产形态；测试注入 stub 运行时隔离工具语义）。

    Returns:
        已完成装配的 :class:`FastAPI` 实例。

    Raises:
        RuntimeError: ``SANDBOX_TOKEN`` 缺失 / 为空（启动期 fail fast
            ——安全边界无 token 拒绝启动，中文指引）。
    """
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"沙盒 token 未配置：请设置环境变量 {_TOKEN_ENV}（启动期 fail fast）")
    if runtime is None:
        runtime = SandboxToolRuntime()
        spec, executor = build_http_fetch_tool()
        runtime.register(spec, executor)
        register_bili_tools(runtime)
    app = FastAPI(
        title="tool-sandbox-agent",
        version="0.1.0",
        description="工具沙盒代理——执行器无关工具的隔离运行时",
    )

    async def _verify_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """Bearer token 校验（恒时比较；失败统一 401——无错误面 oracle）。

        Args:
            authorization: ``Authorization`` 请求头（None = 缺失）。

        Raises:
            HTTPException: 头缺失 / 非 Bearer 形态 / token 不匹配
                （统一 401 + ``鉴权失败``）。
        """
        if authorization is None:
            raise HTTPException(status_code=401, detail="鉴权失败")
        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="鉴权失败")
        if not constant_time_token_matches(parts[1].strip(), token):
            raise HTTPException(status_code=401, detail="鉴权失败")

    @app.get("/health", response_model=HealthResponseModel)
    async def health() -> HealthResponseModel:
        """存活探测（无鉴权——容器 HEALTHCHECK / 后端启动探测面）。"""
        return HealthResponseModel()

    @app.post(
        "/invoke",
        response_model=InvokeResponseModel,
        dependencies=[Depends(_verify_token)],
    )
    async def invoke(request: InvokeRequestModel) -> InvokeResponseModel:
        """执行沙盒工具（鉴权面；运行时恒不抛——恒 200 携带成败终态）。"""
        return await runtime.invoke(request)

    return app


def main() -> None:
    """uvicorn 启动入口（host/port 经 env 覆写——见 :func:`resolve_bind_address`）。"""
    host, port = resolve_bind_address()
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
