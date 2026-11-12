"""通用 Schema（Pydantic v2）。

提供全工程统一的响应封装 ``ResultDTO`` 和分页模型。
所有 API 响应必须使用 ``ResultDTO`` 封装，保持前后端契约一致。

``PageResponse`` 提供 ``next_cursor`` / ``has_more`` 字段，
list 端点优先使用 cursor-based 分页（基于 id+created_at），同时保留
``page`` / ``page_size`` 向后兼容（offset 模式）。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, Field

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 游标工具函数
# ---------------------------------------------------------------------------


def encode_cursor(payload: dict[str, Any]) -> str:
    """将游标 payload（如 ``{"id": "...", "ts": 1690000000000}``）编码为 base64 字符串。

    游标对外是不透明字符串，调用方仅需原样回传即可。
    base64+JSON 编码便于跨语言/HTTP query string 传递，无加密需求。

    Args:
        payload: 游标载荷字典。

    Returns:
        base64 编码的字符串；空 dict 返回空字符串。
    """
    if not payload:
        return ""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """解码游标字符串为 payload 字典。

    Args:
        cursor: base64 编码的游标字符串；``None`` / 空字符串 → 返回 ``None``。

    Returns:
        游标载荷字典；解码失败时返回 ``None``（不抛异常，调用方按"无游标"处理）。
    """
    if not cursor:
        return None
    try:
        # 补齐 base64 padding
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return cast(dict[str, Any], payload)
    except Exception:  # best-effort: malformed cursor treated as no cursor
        return None


class ResultDTO(BaseModel, Generic[T]):
    """统一响应封装。

    所有 API 端点返回此结构，对应 Java 生态的 ResultDTO。
    前端通过 ``success`` 判断成败，通过 ``code`` 做错误分支，通过 ``data`` 取业务数据。

    Attributes:
        success: 是否成功（True=业务成功，False=业务失败）。
        code: 状态码字符串（成功恒为 "200"；失败见 ErrorCode 常量）。
        message: 面向用户的提示信息（成功="成功"；失败=错误描述）。
        data: 业务数据载荷，类型由泛型参数 T 决定。
    """

    success: bool = True
    code: str = "200"
    message: str = "成功"
    data: T | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def ok(cls, data: T | None = None, message: str = "成功") -> ResultDTO[T]:
        """构造成功响应。

        Args:
            data: 业务数据载荷。
            message: 提示信息（默认"成功"）。

        Returns:
            填充好的 ResultDTO 实例。
        """
        return cls(success=True, code="200", message=message, data=data)

    @classmethod
    def error(cls, message: str = "系统异常", code: str = "INTERNAL_ERROR") -> ResultDTO[T]:
        """构造失败响应。

        Args:
            message: 错误描述（面向用户）。
            code: 错误码（见 ErrorCode 常量）。

        Returns:
            填充好的 ResultDTO 实例，data=None。
        """
        return cls(success=False, code=code, message=message, data=None)


class PageResponse(BaseModel, Generic[T]):
    """分页响应封装。

    列表 API 返回此结构，包裹在 ResultDTO.data 中使用::

        ResultDTO[PageResponse[PersonaDTO]]

    Attributes:
        items: 当前页数据列表。
        total: 符合条件的总记录数（offset 模式返回完整计数；
            cursor 模式下若无法精确计算则返回 -1）。
        page: 当前页码（offset 模式有效；cursor 模式恒为 1）。
        page_size: 每页条数。
        next_cursor: 下一页游标（cursor 模式）；offset 模式为 None。
        has_more: 是否还有下一页（cursor 模式有效）。
    """

    items: list[T] = Field(default_factory=list, description="当前页数据列表")
    total: int = Field(default=0, ge=-1, description="总记录数；cursor 模式无精确值时为 -1")
    page: int = Field(default=1, ge=1, description="当前页码（cursor 模式恒为 1）")
    page_size: int = Field(default=20, ge=1, description="每页条数")
    next_cursor: str | None = Field(default=None, description="下一页游标（仅 cursor 模式返回）")
    has_more: bool = Field(default=False, description="是否还有下一页")

    model_config = {"from_attributes": True}

    @classmethod
    def of(
        cls,
        items: list[T],
        total: int,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[T]:
        """便捷构造分页响应（offset 模式，无游标）。

        Args:
            items: 当前页数据列表。
            total: 总记录数。
            page: 当前页码。
            page_size: 每页条数。

        Returns:
            填充好的 PageResponse 实例。
        """
        return cls(items=items, total=total, page=page, page_size=page_size)

    @classmethod
    def of_cursor(
        cls,
        items: list[T],
        page_size: int,
        next_cursor: str | None,
        total: int = -1,
    ) -> PageResponse[T]:
        """便捷构造游标分页响应（cursor 模式）。

        ``total`` 默认 -1 表示不计算总数；调用方可传入真实 COUNT(*) 结果。
        ``has_more`` 由 ``next_cursor`` 是否非空推导。

        Args:
            items: 当前页数据列表。
            page_size: 每页条数。
            next_cursor: 下一页游标；为 None 表示已到末尾。
            total: 总记录数；-1 表示未计算，调用方可传入真实值。

        Returns:
            填充好的 PageResponse 实例（cursor 模式字段已填充）。
        """
        return cls(
            items=items,
            total=total,
            page=1,
            page_size=page_size,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )


__all__ = ["ResultDTO", "PageResponse", "encode_cursor", "decode_cursor"]
