"""GUI 微信通道 wire 契约 Model Classes（本树自持——与沙盒树
``docker/wechat-gui/src/wechat_gui_poc/wire_models.py`` 人为镜像，零代码共享）。

锁定决策：wire 契约在两树各自持有一份文本镜像（本工程约定的唯一跨树耦合
点）——不抽公共包、不做安装期共享；契约漂移由仓库根漂移门禁脚本对两份
Model Class 定义做文本比对看护。两侧类定义块须保持 byte 级一致——修改任一
侧必须同步另一侧（沙盒树只依赖标准库，镜像因此采用 dataclass 形态，不以
pydantic 为基类）。

契约面（HTTP 端点 ↔ 载荷）：

    ====================  ================================  ==========
    端点                   载荷                              鉴权
    ====================  ================================  ==========
    GET /health           :class:`HealthResponseModel`       无（存活面）
    POST /invoke          :class:`InvokeRequestModel` →      Bearer token
                           :class:`InvokeResponseModel`
    ====================  ================================  ==========

``POST /invoke`` 的 HTTP 状态语义：agent 端点恒 200 携带
:class:`InvokeResponseModel`（工具失败在载荷 ``ok=False`` 终态内表达）；
非 200（401 鉴权失败 / 422 请求形态非法）由调用方按传输层失败处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HealthResponseModel",
    "InvokeRequestModel",
    "InvokeResponseModel",
]


@dataclass(frozen=True)
class HealthResponseModel:
    """GUI 微信 agent 健康探测响应（GET /health 载荷——无鉴权存活面）。

    Attributes:
        status: 存活状态（恒 ``"ok"``——后端启动期探测的判定面）。
    """

    status: str = "ok"

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            健康探测响应对象。
        """
        return {"status": self.status}


@dataclass(frozen=True)
class InvokeRequestModel:
    """GUI 微信 agent 工具调用请求（backend → 沙盒 wire 契约——POST /invoke 载荷）。

    Attributes:
        tool: 工具名（agent 运行时内唯一标识——五工具面见 agent.py）。
        arguments: 调用参数（JSON 对象——agent 侧校验后进入执行器）。
    """

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            工具调用请求对象。
        """
        return {"tool": self.tool, "arguments": self.arguments}


@dataclass(frozen=True)
class InvokeResponseModel:
    """GUI 微信 agent 工具调用响应（沙盒 → backend wire 契约——恒携带成败终态）。

    Attributes:
        ok: 是否成功（未知工具 / 参数校验失败 / 执行失败 / 执行异常均归一
            为本形态，agent 端点恒 200 携带）。
        result: 结果 JSON（成功时为工具执行器返回 dict；失败恒空 dict）。
        error: 失败原因（成功为 None；人读中文字符串）。
        latency_ms: agent 侧单次执行时延（epoch 毫秒差；含失败路径耗时）。
    """

    ok: bool = True
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            协议响应对象（``ok`` / ``result`` / ``latency_ms`` / 可选 ``error``）。
        """
        payload: dict[str, object] = {
            "ok": self.ok,
            "result": self.result,
            "latency_ms": self.latency_ms,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload
