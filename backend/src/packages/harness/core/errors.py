"""业务异常与错误码 — harness core 层物理定义位置。

本模块是 ``BusinessException`` / ``ErrorCode`` / ``get_http_status_for_code``
的物理定义位置，全工程统一的业务异常契约：

- 后续 app 层（FastAPI 全局异常处理器，后续 chunk 落地）捕获
  ``BusinessException`` 后转为 ``ResultDTO.error()`` 响应；
- ``packages.harness.*`` 各域包与 llm/ 网关均从此模块导入，
  依赖方向 ``packages.harness.* → packages.harness.core.errors``（同层，合法）。

``ErrorCode`` 是 ``StrEnum`` ——继承 ``str`` 保证与既有字符串比较
兼容（``ErrorCode.X == "X"`` 为 ``True``），同时每个成员携带 ``http_status``
属性，供异常处理器将业务错误码映射到语义化的 HTTP 状态码。

``register_message_resolver`` 回调注册模式实现依赖反转：core 层不能
反向 import app 层 i18n，app 层在加载时注册 ``get_message`` 回调。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

# 消息解析器回调——由 app 层（i18n 装配点）在模块加载时注册。
# 使用回调注册而非直接 import，避免 harness core → app 反向依赖。
# 注册前为 None，此时 message 回退到 code 字符串本身。
_message_resolver: Callable[[str, dict[str, Any] | None], str] | None = None


def register_message_resolver(
    resolver: Callable[[str, dict[str, Any] | None], str],
) -> None:
    """注册消息解析器（由 app 层在模块加载时调用）。

    harness core 层不能反向 import app 层 i18n 模块（依赖方向约束），
    因此通过回调注册模式实现依赖反转：app 层在加载时将 ``get_message``
    注册为回调，harness core 层通过回调间接调用。

    Args:
        resolver: 接收 ``(code, details)`` 返回本地化消息字符串的回调。
    """
    global _message_resolver
    _message_resolver = resolver


class BusinessException(Exception):
    """业务异常基类。

    对应 Java 生态的 BusinessException，保留命名以保持跨语言一致性。
    由全局异常处理器捕获后转为统一响应体::

        {
            "success": false,
            "code": "PERSONA_NOT_FOUND",
            "message": "人格不存在",
            "details": {"persona_id": "abc"},
            "data": null
        }

    Attributes:
        code: 错误码（见下方 ErrorCode 枚举），用于前端逻辑判断。
            既可传入 ``ErrorCode`` 枚举成员，也可传字符串字面量
            （如 ``"INTERNAL_ERROR"``）以保持向后兼容。
        details: 附加详情（如 ``{"persona_id": "abc"}``），可选。
            用于传递结构化上下文，前端可据此展示附加信息。
        message: 面向用户的错误描述。**优先使用 code + i18n 自动派生**，
            仅在需要覆盖 i18n 默认消息时显式传入。若为 ``None``，则通过
            ``_message_resolver(code, details)`` 派生（app 层注册的 i18n 回调）；
            回调未注册时回退到 ``str(code)``。
        headers: 随响应透传的额外 HTTP 响应头（可选，默认 ``None`` 不携带）。
            由全局异常处理器原样并入 JSONResponse——供可重试类错误码
            声明 ``Retry-After`` 等节奏提示头。
    """

    def __init__(
        self,
        code: str = "INTERNAL_ERROR",
        details: dict[str, Any] | None = None,
        message: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.details = details
        self.headers = headers
        if message is not None:
            self.message = message
        elif _message_resolver is not None:
            self.message = _message_resolver(code, details)
        else:
            # 解析器未注册（非请求上下文 / 测试环境 / harness 独立运行）→ 回退到 code 字符串
            self.message = str(code)
        super().__init__(self.message)

    def __repr__(self) -> str:
        """调试友好表示（code + message）。"""
        return f"BusinessException(code={self.code!r}, message={self.message!r})"

    @property
    def http_status(self) -> int:
        """根据 code 返回对应的 HTTP 状态码。

        若 ``code`` 是 ``ErrorCode`` 成员，直接取其 ``http_status`` 属性；
        若是字符串字面量，则按值查找对应的 ``ErrorCode`` 成员；
        若均不匹配，默认返回 400（客户端错误）。
        """
        return get_http_status_for_code(self.code)


# =========================================================================== []
# 错误码枚举
# 命名规范：<资源>_<错误类型>，全大写下划线分隔
#
# 每个枚举成员以 ``(code, http_status)`` 二元组定义：
#   - code: 错误码字符串（同时也是 str 值，保证向后兼容字符串比较）
#   - http_status: 对应的 HTTP 状态码（4xx 客户端错误 / 5xx 服务端错误）
#
# 后续域 chunk（persona / chat / usage / memory …）按各自域段在此追加成员，
# 并同步 i18n_messages.py 消息表（1:1 对齐由测试锁定）。
# =========================================================================== []


class ErrorCode(StrEnum):
    """错误码枚举。

    继承 ``StrEnum`` 保证与既有字符串比较兼容——``ErrorCode.X == "X"`` 为
    ``True``。每个枚举成员携带 ``http_status`` 属性，供异常处理器映射
    HTTP 状态码。

    HTTP 状态码分配原则：
        - 400 Bad Request：通用客户端错误（请求格式/语义不明确）
        - 401 Unauthorized：未认证 / Token 失效
        - 403 Forbidden：已认证但无权限
        - 404 Not Found：资源不存在
        - 405 Method Not Allowed：HTTP 方法不允许
        - 409 Conflict：状态冲突（资源已存在 / 状态机非法转移）
        - 422 Unprocessable Entity：请求语义可解析但字段校验失败
        - 429 Too Many Requests：限流
        - 500 Internal Server Error：服务端内部错误（DB / 第三方调用失败等）
        - 503 Service Unavailable：依赖服务不可用（LLM 端点 / 检索服务）
    """

    # 类级声明——告知静态类型检查器每个枚举实例携带 http_status 属性。
    # 实际赋值在 __new__ 中按成员完成（Enum 不支持 __init__ 赋值实例属性）。
    http_status: int

    # --- 通用 ---
    INTERNAL_ERROR = ("INTERNAL_ERROR", 500)
    BAD_REQUEST = ("BAD_REQUEST", 400)
    VALIDATION_ERROR = ("VALIDATION_ERROR", 422)
    NOT_FOUND = ("NOT_FOUND", 404)
    METHOD_NOT_ALLOWED = ("METHOD_NOT_ALLOWED", 405)
    CONFLICT = ("CONFLICT", 409)
    UNSUPPORTED_MEDIA_TYPE = ("UNSUPPORTED_MEDIA_TYPE", 415)
    RATE_LIMITED = ("RATE_LIMITED", 429)

    # --- 鉴权（P1+ 管理后台 / 平台通道落地后扩展） ---
    UNAUTHORIZED = ("UNAUTHORIZED", 401)
    FORBIDDEN = ("FORBIDDEN", 403)
    TOKEN_EXPIRED = ("TOKEN_EXPIRED", 401)
    TOKEN_INVALID = ("TOKEN_INVALID", 401)

    # --- 配置 ---
    CONFIG_INVALID = ("CONFIG_INVALID", 422)

    # --- 人格上下文（database PersonaMixin 自动隔离的守卫面） ---
    PERSONA_CONTEXT_REQUIRED = ("PERSONA_CONTEXT_REQUIRED", 500)

    # --- LLM 调用（远程 OpenAI 兼容端点；llm/ 网关 chunk 消费） ---
    LLM_CALL_FAILED = ("LLM_CALL_FAILED", 503)
    LLM_PROVIDER_NOT_SUPPORTED = ("LLM_PROVIDER_NOT_SUPPORTED", 422)
    LLM_API_KEY_MISSING = ("LLM_API_KEY_MISSING", 500)
    LLM_REQUEST_FAILED = ("LLM_REQUEST_FAILED", 503)
    MODEL_NOT_FOUND = ("MODEL_NOT_FOUND", 404)

    def __new__(cls, code: str, http_status: int) -> ErrorCode:
        """构造枚举成员。

        Args:
            code: 错误码字符串，同时作为 ``str`` 父类的值（保证 ``ErrorCode.X == "X"``）。
            http_status: 该错误码对应的 HTTP 状态码。

        Returns:
            枚举成员实例，``str.__new__`` 创建保证 ``isinstance(member, str)`` 为 ``True``。
        """
        obj = str.__new__(cls, code)
        obj._value_ = code
        obj.http_status = http_status
        return obj

    def __str__(self) -> str:
        """显式定义 ``__str__`` 避免 Enum 默认的 ``EnumClass.MEMBER`` 输出。"""
        return str.__str__(self)


# =========================================================================== []
# 错误码 → HTTP 状态码 查询
# =========================================================================== []


# 预构建 ``code string → http_status`` 索引，避免每次异常处理都遍历枚举。
_CODE_TO_HTTP_STATUS: dict[str, int] = {member.value: member.http_status for member in ErrorCode}


def get_http_status_for_code(code: str) -> int:
    """根据错误码字符串查询对应的 HTTP 状态码。

    Args:
        code: 错误码——既可传 ``ErrorCode`` 成员（``isinstance(code, ErrorCode)``），
            也可传字符串字面量（如 ``"UNAUTHORIZED"``）。

    Returns:
        对应的 HTTP 状态码；若 ``code`` 不是已知的 ``ErrorCode``，默认返回 400。
    """
    if isinstance(code, ErrorCode):
        return code.http_status
    return _CODE_TO_HTTP_STATUS.get(code, 400)


__all__ = [
    "BusinessException",
    "ErrorCode",
    "get_http_status_for_code",
    "register_message_resolver",
]
