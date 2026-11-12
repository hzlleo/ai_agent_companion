"""错误消息映射表 —— 与 ``ErrorCode`` 枚举 1:1 对齐。

结构约定：

- key 为 ``ErrorCode`` 成员值（SCREAMING_SNAKE_CASE），新增 ``ErrorCode``
  成员必须同步在此注册消息条目（1:1 对齐由 ``test_core_i18n.py`` 的
  回归测试锁定）；
- 每个条目是 ``{locale: 文本}`` 映射，至少覆盖 ``zh-CN``（默认回退）
  与 ``en``；
- 占位符用 ``{name}`` 形式，由 ``core.i18n.get_message`` 经
  ``str.format`` 插值（``details`` 字段透传）。

通知文案模板（非 ErrorCode 体系，``notification.<场景>`` 小写点分风格）
在对应域 chunk 落地时再入表——P0-2 无通知域。
"""

from __future__ import annotations

__all__ = ["MESSAGES"]

MESSAGES: dict[str, dict[str, str]] = {
    # --- 通用 ---
    "INTERNAL_ERROR": {
        "zh-CN": "系统内部错误，请稍后重试",
        "en": "Internal server error, please retry later",
    },
    "BAD_REQUEST": {
        "zh-CN": "请求参数错误",
        "en": "Bad request",
    },
    "VALIDATION_ERROR": {
        "zh-CN": "请求校验失败",
        "en": "Validation failed",
    },
    "NOT_FOUND": {
        "zh-CN": "资源不存在",
        "en": "Resource not found",
    },
    "METHOD_NOT_ALLOWED": {
        "zh-CN": "HTTP 方法不允许",
        "en": "HTTP method not allowed",
    },
    "CONFLICT": {
        "zh-CN": "状态冲突",
        "en": "Conflict",
    },
    "UNSUPPORTED_MEDIA_TYPE": {
        "zh-CN": "不支持的媒体类型",
        "en": "Unsupported media type",
    },
    "RATE_LIMITED": {
        "zh-CN": "请求过于频繁，请稍后重试",
        "en": "Rate limited, please retry later",
    },
    # --- 鉴权 ---
    "UNAUTHORIZED": {
        "zh-CN": "未认证或凭证已失效",
        "en": "Unauthorized",
    },
    "FORBIDDEN": {
        "zh-CN": "无权限执行该操作",
        "en": "Forbidden",
    },
    "TOKEN_EXPIRED": {
        "zh-CN": "凭证已过期，请重新登录",
        "en": "Token expired, please sign in again",
    },
    "TOKEN_INVALID": {
        "zh-CN": "凭证无效",
        "en": "Invalid token",
    },
    # --- 配置 ---
    "CONFIG_INVALID": {
        "zh-CN": "配置不合法（详见 details）",
        "en": "Invalid configuration (see details)",
    },
    # --- 人格上下文 ---
    "PERSONA_CONTEXT_REQUIRED": {
        "zh-CN": "缺少人格上下文（persona_id 未设置），无法执行人格作用域操作",
        "en": "Persona context is required (persona_id is not set) for persona-scoped operations",
    },
    # --- LLM 调用 ---
    "LLM_CALL_FAILED": {
        "zh-CN": "LLM 调用失败，请稍后重试",
        "en": "LLM call failed, please retry later",
    },
    "LLM_PROVIDER_NOT_SUPPORTED": {
        "zh-CN": "不支持的 LLM 供应方",
        "en": "Unsupported LLM provider",
    },
    "LLM_API_KEY_MISSING": {
        "zh-CN": "LLM API Key 未配置",
        "en": "LLM API key is not configured",
    },
    "LLM_REQUEST_FAILED": {
        "zh-CN": "LLM 请求失败（远端 OpenAI 兼容端点不可用）",
        "en": "LLM request failed (remote OpenAI-compatible endpoint unavailable)",
    },
    "MODEL_NOT_FOUND": {
        "zh-CN": "模型不存在",
        "en": "Model not found",
    },
}
