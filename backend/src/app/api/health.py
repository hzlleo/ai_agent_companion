"""健康检查路由——进程存活面（liveness，无任何重依赖）。

设计约束（本工程约定）：

    - **零依赖存活面**：不触 DB / 不触 LLM / 不触池——任何依赖宕机时
      本端点仍须 200（存活与就绪分离：liveness 在此，就绪度由各依赖
      健康面后续按需扩展）；
    - 响应经 ``ResultDTO`` 封装（与全部 app 端点契约一致）；
    - 时间戳 epoch 毫秒（``core.time_utils.now_ms``）。

版本真源：:data:`APP_VERSION` 经发行元数据解析（editable install 名
``ai-agent-companion-backend``）；未安装形态（pythonpath 直跑）回落
``0.1.0``（与 pyproject ``[project].version`` 一致）。
"""

from __future__ import annotations

from importlib import metadata

from fastapi import APIRouter
from pydantic import BaseModel, Field

from packages.harness.core.dtos import ResultDTO
from packages.harness.core.time_utils import now_ms

__all__ = ["APP_VERSION", "HealthStatusDTO", "router"]


def _resolve_app_version() -> str:
    """解析应用版本（发行元数据优先；未安装形态回落固定值）。

    Returns:
        版本字符串（形如 ``0.1.0``）。
    """
    try:
        return metadata.version("ai-agent-companion-backend")
    except metadata.PackageNotFoundError:
        return "0.1.0"


#: 应用版本（健康面 / FastAPI 元数据共用的单一真源）。
APP_VERSION: str = _resolve_app_version()


class HealthStatusDTO(BaseModel):
    """健康状态载荷（liveness）。

    Attributes:
        status: 存活状态（恒 ``ok``——端点能应答即存活）。
        version: 应用版本（发行元数据真源）。
        ts_ms: 应答时间（epoch 毫秒）。
    """

    status: str = Field(default="ok", description="存活状态（恒 ok）")
    version: str = Field(description="应用版本")
    ts_ms: int = Field(description="应答时间（epoch 毫秒）")


router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=ResultDTO[HealthStatusDTO])
async def health() -> ResultDTO[HealthStatusDTO]:
    """进程存活检查（GET /api/v1/health——零依赖，DB 宕机时仍 200）。

    Returns:
        ``ResultDTO`` 包裹的 :class:`HealthStatusDTO`。
    """
    return ResultDTO.ok(data=HealthStatusDTO(status="ok", version=APP_VERSION, ts_ms=now_ms()))
