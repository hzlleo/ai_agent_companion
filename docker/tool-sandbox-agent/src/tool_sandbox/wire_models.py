"""沙盒 wire 契约 Model Classes（本树自持——与后端 harness/tools 的
sandbox_models.py 人为镜像，零代码共享）。

锁定决策：wire 契约在两树**各自持有一份文本镜像**（本工程约定的唯一
跨树耦合点）——不抽公共包、不做安装期共享；契约漂移由仓库根漂移
门禁脚本（后端侧职责）对两份 Model Class 定义做文本比对看护。
两侧类定义块须保持 byte 级一致——修改任一侧必须同步另一侧。

契约面（HTTP 端点 ↔ 载荷）：

    ====================  ================================  ==========
    端点                   载荷                              鉴权
    ====================  ================================  ==========
    GET /health           :class:`HealthResponseModel`       无（存活面）
    POST /invoke          :class:`InvokeRequestModel` →      Bearer token
                          :class:`InvokeResponseModel`
    ====================  ================================  ==========

``POST /invoke`` 的 HTTP 状态语义：沙盒端点恒 200 携带
:class:`InvokeResponseModel`（工具失败在载荷 ``ok=False`` 终态内表达）；
非 200（401 鉴权失败 / 422 请求形态非法）由调用方按传输层失败处理。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "InvokeRequestModel",
    "InvokeResponseModel",
    "HealthResponseModel",
]


class InvokeRequestModel(BaseModel):
    """沙盒工具调用请求（backend → 沙盒 wire 契约——POST /invoke 载荷）。

    Attributes:
        tool: 工具名（沙盒运行时内唯一标识）。
        arguments: 调用参数（JSON 对象——沙盒侧经工具参数 Model Class
            校验 + 默认值物化后进入执行器）。
    """

    model_config = {"extra": "forbid"}

    tool: str = Field(min_length=1, max_length=64, description="工具名（沙盒内唯一标识）")
    arguments: dict[str, Any] = Field(default_factory=dict, description="调用参数（JSON 对象）")


class InvokeResponseModel(BaseModel):
    """沙盒工具调用响应（沙盒 → backend wire 契约——恒携带成败终态）。

    Attributes:
        ok: 是否成功（False = 未知工具 / 参数校验失败 / 执行器受控失败 /
            执行器异常——四类失败均归一为本形态，沙盒端点恒 200 携带）。
        result: 结果 JSON（成功时为工具执行器返回 dict；失败恒空 dict）。
        error: 失败原因（成功为 None；人读中文字符串）。
        latency_ms: 沙盒侧单次执行时延（epoch 毫秒差；含失败路径耗时）。
    """

    ok: bool = Field(default=True, description="是否成功")
    result: dict[str, Any] = Field(default_factory=dict, description="结果 JSON")
    error: str | None = Field(default=None, description="失败原因（成功为 None）")
    latency_ms: int = Field(default=0, ge=0, description="沙盒侧单次执行时延（毫秒）")


class HealthResponseModel(BaseModel):
    """沙盒健康探测响应（GET /health 载荷——无鉴权存活面）。

    Attributes:
        status: 存活状态（恒 ``"ok"``——backend 启动期探测的判定面）。
    """

    status: str = Field(default="ok", description="存活状态（恒 ok）")
