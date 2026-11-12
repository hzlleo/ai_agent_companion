"""app/middleware/error_handler 测试——BusinessException / 请求校验错误映射。

本地装配最小 FastAPI app（探针路由 + register_exception_handlers）；
ASGITransport 零真实网络。锁定：HTTP 状态映射（ErrorCode → 状态码）、
ResultDTO error 形态、details 透传、headers 透传、400 校验形态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Query
from httpx import ASGITransport, AsyncClient

from app.middleware.error_handler import register_exception_handlers
from packages.harness.core.dtos import ResultDTO
from packages.harness.core.errors import BusinessException


@pytest.fixture
async def error_app() -> AsyncIterator[FastAPI]:
    """装配探针 app（仅异常处理器 + 各类触发路由——无 DB / 无业务依赖）。

    Yields:
        已注册共享异常处理器的 :class:`FastAPI` 实例。
    """
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/probe/business")
    async def probe_business(
        code: str = Query("NOT_FOUND"),
        message: str = Query("人格不存在"),
        with_headers: bool = Query(False),
    ) -> ResultDTO[None]:
        """业务异常探针（code / message / headers 可参数化触发）。"""
        headers = {"Retry-After": "30"} if with_headers else None
        raise BusinessException(code=code, message=message, details={"k": "v"}, headers=headers)

    @app.get("/probe/unknown-code")
    async def probe_unknown_code() -> ResultDTO[None]:
        """未知字符串错误码探针（默认映射 400）。"""
        raise BusinessException(code="SOME_UNKNOWN_CODE", message="未知错误码")

    @app.get("/probe/validation")
    async def probe_validation(q: int = Query(...)) -> ResultDTO[int]:
        """请求校验探针（缺 q / 非整型触发 RequestValidationError）。"""
        return ResultDTO.ok(data=q)

    register_exception_handlers(app)
    yield app


@pytest.fixture
async def client(error_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGITransport 客户端（零真实网络 / 零真实服务器）。

    Args:
        error_app: 探针 app。

    Yields:
        :class:`httpx.AsyncClient`（base_url=http://test）。
    """
    transport = ASGITransport(app=error_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# BusinessException → ErrorCode 映射 + ResultDTO 形态
# ---------------------------------------------------------------------------


async def test_business_exception_not_found_404(client: AsyncClient) -> None:
    """NOT_FOUND → 404 + ResultDTO error 形态 + details 入 data。"""
    response = await client.get(
        "/probe/business", params={"code": "NOT_FOUND", "message": "人格不存在"}
    )
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "人格不存在"
    assert body["data"] == {"k": "v"}


async def test_business_exception_conflict_409(client: AsyncClient) -> None:
    """CONFLICT → 409。"""
    response = await client.get("/probe/business", params={"code": "CONFLICT"})
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


async def test_business_exception_internal_500(client: AsyncClient) -> None:
    """INTERNAL_ERROR → 500。"""
    response = await client.get("/probe/business", params={"code": "INTERNAL_ERROR"})
    assert response.status_code == 500
    assert response.json()["success"] is False


async def test_business_exception_unknown_code_defaults_400(client: AsyncClient) -> None:
    """未知字符串错误码 → 默认 400（get_http_status_for_code 契约）。"""
    response = await client.get("/probe/unknown-code")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "SOME_UNKNOWN_CODE"
    assert body["message"] == "未知错误码"


async def test_business_exception_headers_passthrough(client: AsyncClient) -> None:
    """异常携带 headers → 响应头原样透传（Retry-After）。"""
    response = await client.get("/probe/business", params={"with_headers": "true"})
    assert response.status_code == 404
    assert response.headers.get("Retry-After") == "30"


# ---------------------------------------------------------------------------
# RequestValidationError → 400 + ResultDTO 形态
# ---------------------------------------------------------------------------


async def test_validation_error_missing_query_400(client: AsyncClient) -> None:
    """缺必填查询参数 → 400 + VALIDATION_ERROR + data.errors 列表。"""
    response = await client.get("/probe/validation")
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    errors = body["data"]["errors"]
    assert isinstance(errors, list)
    assert errors[0]["loc"] == ["query", "q"]
    assert errors[0]["type"] == "missing"


async def test_validation_error_bad_type_400(client: AsyncClient) -> None:
    """查询参数类型错误 → 400（同形态）。"""
    response = await client.get("/probe/validation", params={"q": "not-int"})
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_registration_idempotent(error_app: FastAPI, client: AsyncClient) -> None:
    """重复注册安全：add_exception_handler 按类型覆写（幂等）。"""
    register_exception_handlers(error_app)
    response = await client.get("/probe/business", params={"code": "NOT_FOUND"})
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
