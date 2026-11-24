"""生产环境鉴权启动策略——fail-closed 判定（纯函数面）。

管理面 owner-token 与 QQ / SmsForwarder / WhatsApp 入站 webhook 的鉴权在
开发 / 测试环境保持「空凭据 = 校验关闭」形态；``ENV=prod`` 时管理面 token
必须非空、启用的入站通道凭据必须齐全，否则 app 启动期拒绝启动（安全边界
不得以无鉴权形态上线）。

判定面与文档边界：保护路由清单与豁免契约归 ``app/main.py`` 的
``_include_routers`` 唯一权威；本模块只回答「生产环境是否允许以当前
凭据配置启动」。
"""

from __future__ import annotations

import os

from packages.harness.config.models import RuntimeEnv, Settings
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "ensure_auth_policy",
    "resolve_admin_token",
    "resolve_qq_webhook_token",
    "resolve_sms_forwarder_token",
]


def resolve_admin_token(settings: Settings) -> str:
    """解析管理面 owner token（``admin_token_env`` 间接读取）。

    Args:
        settings: 生效 Settings（``settings.server.admin_token_env`` 持有
            环境变量名）。

    Returns:
        token 本值（strip 后）；变量未设置 / 为空返回空串（= 开发形态下
        鉴权关闭）。
    """
    return os.environ.get(settings.server.admin_token_env, "").strip()


def resolve_qq_webhook_token(settings: Settings) -> str:
    """解析 QQ 入站 webhook 校验 token（``webhook_token_env`` 间接读取）。

    Args:
        settings: 生效 Settings（``settings.channels.qq.webhook_token_env``
            持有环境变量名）。

    Returns:
        token 本值（strip 后）；变量未设置 / 为空返回空串。
    """
    return os.environ.get(settings.channels.qq.webhook_token_env, "").strip()


def resolve_sms_forwarder_token(settings: Settings) -> str:
    """解析 SmsForwarder 入站校验 token（配置字段直接持有本值）。

    Args:
        settings: 生效 Settings（``settings.channels.sms_forwarder.token``
            持有 SecretStr 本值）。

    Returns:
        token 本值（strip 后）；配置为空返回空串（= 开发形态下鉴权关闭）。
    """
    return settings.channels.sms_forwarder.token.get_secret_value().strip()


def ensure_auth_policy(settings: Settings) -> None:
    """启动期鉴权策略校验（生产环境 fail-closed）。

    规则（锁定）：``settings.env`` 为 ``prod`` 时，管理面 owner token
    必须非空；QQ / SmsForwarder 通道启用时其入站 token 同样必须非空；
    WhatsApp 通道启用时 hub 校验 token / 应用密钥 / 访问令牌 / 企业号码 ID
    四项必须全部非空。开发 / 测试环境保持既有「空 = 鉴权关闭」行为
    （零配置开发可用）。

    Args:
        settings: 生效 Settings（运行环境标识 / 管理面 token 变量名 /
            QQ / SmsForwarder / WhatsApp 通道开关与其凭据配置）。

    Raises:
        BusinessException: 生产环境 ``ADMIN_AUTH_TOKEN`` 解析为空
            （``CONFIG_INVALID``）；生产环境 QQ 通道启用但
            ``QQ_WEBHOOK_TOKEN`` 解析为空（同码）；生产环境 SmsForwarder
            通道启用但 ``SMS_FORWARDER_TOKEN`` 为空（同码）；生产环境
            WhatsApp 通道启用但四项凭据任一为空（同码）。
    """
    if settings.env is not RuntimeEnv.PROD:
        return
    if not resolve_admin_token(settings):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=(
                "生产环境必须设置 ADMIN_AUTH_TOKEN："
                f"环境变量 {settings.server.admin_token_env} 为空"
            ),
            details={"admin_token_env": settings.server.admin_token_env, "env": settings.env.value},
        )
    if settings.channels.qq.enabled and not resolve_qq_webhook_token(settings):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message=(
                "生产环境启用 QQ 通道时必须设置 QQ_WEBHOOK_TOKEN："
                f"环境变量 {settings.channels.qq.webhook_token_env} 为空"
            ),
            details={
                "qq_webhook_token_env": settings.channels.qq.webhook_token_env,
                "env": settings.env.value,
            },
        )
    if settings.channels.sms_forwarder.enabled and not resolve_sms_forwarder_token(settings):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            message="生产环境启用 SmsForwarder 通道时必须设置 SMS_FORWARDER_TOKEN",
            details={"env": settings.env.value},
        )
    if settings.channels.whatsapp.enabled:
        whatsapp = settings.channels.whatsapp
        required_credentials = {
            "WHATSAPP_VERIFY_TOKEN": whatsapp.verify_token.get_secret_value().strip(),
            "WHATSAPP_APP_SECRET": whatsapp.app_secret.get_secret_value().strip(),
            "WHATSAPP_ACCESS_TOKEN": whatsapp.access_token.get_secret_value().strip(),
            "WHATSAPP_PHONE_NUMBER_ID": whatsapp.phone_number_id.strip(),
        }
        missing = [name for name, value in required_credentials.items() if not value]
        if missing:
            raise BusinessException(
                code=ErrorCode.CONFIG_INVALID,
                message=(
                    "生产环境启用 WhatsApp 通道时必须设置 WHATSAPP_VERIFY_TOKEN / "
                    "WHATSAPP_APP_SECRET / WHATSAPP_ACCESS_TOKEN / "
                    "WHATSAPP_PHONE_NUMBER_ID（缺失："
                    f"{', '.join(missing)}）"
                ),
                details={"missing": ", ".join(missing), "env": settings.env.value},
            )
