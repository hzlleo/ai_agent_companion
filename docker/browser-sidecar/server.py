"""浏览器自动化 sidecar —— ASGI 入口薄层（app 工厂 + uvicorn main）。

职责边界（本文件只做「入口」——工具语义全部在 ``browser_sidecar`` 包内）：

    - :func:`create_app`  —— FastAPI 工厂（token 鉴权依赖 + ``/health`` /
      ``/invoke`` 两端点 + 默认运行时装配七浏览器工具 + lifespan 承接
      管理器生命周期）；
    - :func:`main`        —— uvicorn 启动（host/port 经 env 覆写）。

鉴权（安全红线——与 tool-sandbox 树同约定）：

    - ``POST /invoke`` 须携带 ``Authorization: Bearer <token>``；token 从
      env ``BROWSER_SIDECAR_TOKEN`` 读取，**恒时比较**（:func:`constant_time_token_matches`
      —— ``secrets.compare_digest``，防时序侧信道）；
    - 全部鉴权失败统一 401 + 统一 detail（「鉴权失败」——不区分头缺失 /
      形态错误 / token 不匹配，杜绝错误面 oracle）；
    - ``BROWSER_SIDECAR_TOKEN`` 缺失 / 为空 → app 工厂 fail fast（安全边界
      无 token 拒绝启动—— RuntimeError，中文指引）；
    - token 永不入日志 / 错误响应。

端点面（REST 同构镜像 tool-sandbox 树——非新协议）：

    - ``GET /health``  —— 无鉴权存活面（容器 HEALTHCHECK 消费；恒
      ``{"status": "ok"}``）；
    - ``POST /invoke`` —— 鉴权面；请求 :class:`InvokeRequestModel`，响应
      恒 200 携带 :class:`InvokeResponseModel`（运行时恒不抛——工具失败
      在载荷 ``ok=False`` 终态内表达；请求形态非法由 FastAPI 归 422）。

生命周期：默认运行时的 :class:`~browser_sidecar.browser.BrowserManager`
经 app lifespan 起停（收割任务 + 会话收尾）；注入运行时（测试）不建
管理器——lifespan no-op。
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

from browser_sidecar.browser import BrowserManager, build_default_browser_sidecar
from browser_sidecar.runtime import BrowserSidecarRuntime
from browser_sidecar.wire_models import (
    HealthResponseModel,
    InvokeRequestModel,
    InvokeResponseModel,
)

__all__ = [
    "BROWSER_SIDECAR_HEALTH_PATH",
    "BROWSER_SIDECAR_INVOKE_PATH",
    "BROWSER_SIDECAR_TOKEN_ENV_DEFAULT",
    "constant_time_token_matches",
    "create_app",
    "main",
    "resolve_bind_address",
]


# ---------------------------------------------------------------------------
# 部署契约常量（与后端 harness/tools/browser.py 同名常量镜像——漂移门禁
# 比对项）
# ---------------------------------------------------------------------------

#: sidecar invoke 端点路径（后端客户端 URL 拼接的镜像——改任一侧须同步
#: 另一侧，漂移门禁看护）。
BROWSER_SIDECAR_INVOKE_PATH: str = "/invoke"

#: sidecar health 端点路径（同上——后端启动探测 / 容器 HEALTHCHECK 消费面）。
BROWSER_SIDECAR_HEALTH_PATH: str = "/health"

#: 鉴权 token 环境变量缺省名（后端客户端缺省 token_env 同名镜像——
#: compose 注入契约）。
BROWSER_SIDECAR_TOKEN_ENV_DEFAULT: str = "BROWSER_SIDECAR_TOKEN"

#: 鉴权 token 环境变量（容器 / compose 注入；缺失 = 工厂 fail fast）。
_TOKEN_ENV: str = BROWSER_SIDECAR_TOKEN_ENV_DEFAULT

#: 监听地址环境变量（容器形态默认 0.0.0.0——端口映射由编排层收敛到回环；
#: 裸机运行建议显式 127.0.0.1，见本目录 README.md「运行」一节）。
_HOST_ENV: str = "BROWSER_SIDECAR_HOST"

#: 监听端口环境变量（与 compose 端口映射契约一致：8766）。
_PORT_ENV: str = "BROWSER_SIDECAR_PORT"

#: 缺省监听地址 / 端口（容器形态）。
_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_PORT: int = 8766


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
    """解析监听地址（env 覆写；缺省容器形态 0.0.0.0:8766）。

    Returns:
        ``(host, port)`` 二元组（``BROWSER_SIDECAR_HOST`` / ``BROWSER_SIDECAR_PORT``）。
    """
    host = os.environ.get(_HOST_ENV, _DEFAULT_HOST)
    try:
        port = int(os.environ.get(_PORT_ENV, str(_DEFAULT_PORT)))
    except ValueError as exc:
        raise RuntimeError(f"{_PORT_ENV} 必须是整数端口号") from exc
    return host, port


def create_app(*, runtime: BrowserSidecarRuntime | None = None) -> FastAPI:
    """构造 FastAPI 应用（token 鉴权 + 两端点 + 默认工具装配 + lifespan）。

    Args:
        runtime: sidecar 工具运行时（None = 生产形态——七浏览器工具 +
            env 驱动管理器；测试注入 stub 运行时隔离工具语义且不建
            管理器——lifespan no-op）。

    Returns:
        已完成装配的 :class:`FastAPI` 实例。

    Raises:
        RuntimeError: ``BROWSER_SIDECAR_TOKEN`` 缺失 / 为空（启动期 fail
            fast——安全边界无 token 拒绝启动，中文指引）。
    """
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(
            f"浏览器 sidecar token 未配置：请设置环境变量 {_TOKEN_ENV}（启动期 fail fast）"
        )
    manager: BrowserManager | None = None
    if runtime is None:
        runtime, manager = build_default_browser_sidecar()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        """应用生命周期（默认运行时：管理器收割任务起 / 停；注入形态 no-op）。"""
        if manager is not None:
            await manager.start()
        try:
            yield
        finally:
            if manager is not None:
                await manager.stop()

    app = FastAPI(
        title="browser-sidecar",
        version="0.1.0",
        description="浏览器自动化 sidecar——JS 渲染回退执行面",
        lifespan=_lifespan,
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

    @app.get(BROWSER_SIDECAR_HEALTH_PATH, response_model=HealthResponseModel)
    async def health() -> HealthResponseModel:
        """存活探测（无鉴权——容器 HEALTHCHECK / 后端启动探测面）。"""
        return HealthResponseModel()

    @app.post(
        BROWSER_SIDECAR_INVOKE_PATH,
        response_model=InvokeResponseModel,
        dependencies=[Depends(_verify_token)],
    )
    async def invoke(request: InvokeRequestModel) -> InvokeResponseModel:
        """执行浏览器工具（鉴权面；运行时恒不抛——恒 200 携带成败终态）。"""
        return await runtime.invoke(request)

    return app


def main() -> None:
    """uvicorn 启动入口（host/port 经 env 覆写——见 :func:`resolve_bind_address`）。"""
    host, port = resolve_bind_address()
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
