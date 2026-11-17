"""管理面 owner-token 鉴权依赖——admin API 路由的统一准入守卫。

R2 设计审计放行条件（新端点不得扩张无鉴权面）：personas / memories /
usage / voice / look_at / models 六个管理路由集共享本依赖；豁免面
（health / channels-qq webhook）与挂载契约见 ``app/main.py`` 装配注释
及 docs/backend/README.md 认证一节。

鉴权语义（与 ``app/api/channels_qq`` webhook 入站鉴权同模式）：

    - **env 间接**：token 本值永不落 yaml / 配置树——``ServerConfig.
      admin_token_env`` 只持有环境变量名（默认 ``ADMIN_AUTH_TOKEN``），
      本模块按请求从该变量读取实际 token（与 telegram ``bot_token_env``
      / 沙盒 ``token_env`` 同约定）；
    - **空 = 鉴权关闭**：变量未设置 / 为空时依赖直接放行（开发 / 内网
      形态）。关闭态只在**启动期**经 :func:`log_admin_auth_startup_notice`
      落一条 info 日志（不逐请求刷屏）；公网暴露必须配置 token，R3 将
      补齐强制启用（见 docs/backend/README.md 认证一节）；
    - **恒时比较**：token 配置时要求 ``Authorization: Bearer <token>``
      头（scheme 前缀解析 + ``secrets.compare_digest``——防时序侧信道）；
    - **失败 → 401**：``BusinessException(UNAUTHORIZED)`` 经共享异常
      处理器归一为 ``ResultDTO`` error 形态（``success=false / code=
      UNAUTHORIZED``）；``details`` 只携带变量名等非密钥上下文。

Settings 解析次序（与 channels_qq / main.lifespan 同回退次序）：
``request.app.state.settings``（``create_app`` 上挂；隔离测试注入面）
→ 全局单例 ``global_settings``。
"""

from __future__ import annotations

import os
import secrets
from typing import cast

from fastapi import Request
from loguru import logger

from packages.harness.config.models import Settings
from packages.harness.config.models import settings as global_settings
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "log_admin_auth_startup_notice",
    "require_admin_token",
    "resolve_admin_token",
]


def resolve_admin_token(settings: Settings) -> str:
    """解析管理面 owner token（``admin_token_env`` 间接读取）。

    Args:
        settings: 生效 Settings（``settings.server.admin_token_env`` 持有
            环境变量名）。

    Returns:
        token 本值（strip 后）；变量未设置 / 为空返回空串（= 鉴权关闭）。
    """
    return os.environ.get(settings.server.admin_token_env, "").strip()


def _bearer_token(authorization: str) -> str | None:
    """解析 Authorization 头的 Bearer token。

    与 ``app/api/channels_qq._bearer_token`` 同形态（两侧各自持有——
    middleware 不反向依赖 api 层模块）。

    Args:
        authorization: 原始 Authorization 头值（可能缺失——空串入参）。

    Returns:
        token 值（已 strip）；头形态非 ``Bearer <token>``（scheme 不符 /
        token 为空）返回 None。
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    stripped = token.strip()
    return stripped if stripped else None


async def require_admin_token(request: Request) -> None:
    """管理面鉴权依赖（token 配置时恒时比较 Bearer；不匹配 → 401）。

    挂载形态：``app/main.py`` 经 ``APIRouter(dependencies=[Depends(
    require_admin_token)])`` 对受保护路由集**单点注册**（本依赖不感知
    豁免面——豁免路由不挂本依赖，清单契约归 main 装配层）。

    Args:
        request: 原始 ASGI 请求（Authorization 头消费面；Settings 经
            ``request.app.state.settings`` 解析，未上挂时回落全局单例）。

    Raises:
        BusinessException: token 已配置但请求头缺失 / scheme 不符 /
            值不匹配（``UNAUTHORIZED`` → 401，ResultDTO error 形态经
            共享异常处理器）。
    """
    settings = cast("Settings | None", getattr(request.app.state, "settings", None)) or (
        global_settings
    )
    expected = resolve_admin_token(settings)
    if not expected:
        return
    presented = _bearer_token(request.headers.get("authorization", ""))
    if presented is None or not secrets.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise BusinessException(
            code=ErrorCode.UNAUTHORIZED,
            message="管理面鉴权失败：Authorization Bearer token 缺失或不匹配",
            details={"admin_token_env": settings.server.admin_token_env},
        )


def log_admin_auth_startup_notice(settings: Settings) -> None:
    """启动期鉴权模式通告（ONE info——不逐请求刷屏）。

    由 ``app.main.startup_app_state`` 在日志系统就绪后调用一次；日志
    只携带变量名与模式，**永不落 token 本值**。

    Args:
        settings: 生效 Settings（``settings.server.admin_token_env``
            驱动模式判定）。
    """
    if resolve_admin_token(settings):
        logger.info(
            "管理面 owner-token 鉴权已启用（Bearer 头校验）: admin_token_env={}",
            settings.server.admin_token_env,
        )
        return
    logger.info(
        "管理面 owner-token 鉴权未启用（环境变量 {} 为空——放行全部管理路由；"
        "公网暴露必须配置，R3 将补齐强制启用）",
        settings.server.admin_token_env,
    )
