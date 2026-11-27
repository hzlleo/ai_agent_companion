"""GUI 微信通道 wire 契约 Model Classes（本树自持——与沙盒树
``docker/wechat-gui/src/wechat_gui_poc/wire_models.py`` 人为镜像，零代码共享）。

锁定决策：wire 契约在两树各自持有一份文本镜像（本工程约定的唯一跨树耦合
点）——不抽公共包、不做安装期共享；契约漂移由仓库根漂移门禁脚本对两份
Model Class 定义与部署契约常量行做文本比对看护。两侧类定义块与常量行须保持
byte 级一致——修改任一侧必须同步另一侧（沙盒树只依赖标准库，镜像因此采用
dataclass 形态，不以 pydantic 为基类）。

契约面（HTTP 端点 ↔ 载荷）：

    ====================  ================================  ==========
    端点                   载荷                              鉴权
    ====================  ================================  ==========
    GET /health           :class:`HealthResponseModel`       无（存活面）
    POST /invoke          :class:`InvokeRequestModel` →      Bearer token
                           :class:`InvokeResponseModel`
    ====================  ================================  ==========

五工具结果模型（``ScreenshotResultModel`` / ``ClickResultModel`` /
``TypeTextResultModel`` / ``PressKeyResultModel`` / ``OpenChatResultModel``）
同属 wire 契约：沙盒构造并序列化，后端按镜像模型解码结果字段。

``POST /invoke`` 的 HTTP 状态语义：agent 端点恒 200 携带
:class:`InvokeResponseModel`（工具失败在载荷 ``ok=False`` 终态内表达）；
非 200（401 鉴权失败 / 413 请求体超限 / 422 请求形态非法）由调用方按传输层
失败处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "HealthResponseModel",
    "InvokeRequestModel",
    "InvokeResponseModel",
    "ScreenshotResultModel",
    "ClickResultModel",
    "TypeTextResultModel",
    "PressKeyResultModel",
    "OpenChatResultModel",
    "WECHAT_GUI_HEALTH_PATH",
    "WECHAT_GUI_INVOKE_PATH",
    "WECHAT_GUI_TOOL_SCREENSHOT",
    "WECHAT_GUI_TOOL_CLICK",
    "WECHAT_GUI_TOOL_TYPE_TEXT",
    "WECHAT_GUI_TOOL_PRESS_KEY",
    "WECHAT_GUI_TOOL_OPEN_CHAT",
]

# ─────────────────────────────────────────────────────────────────────────────
# 部署契约常量（与沙盒树同名常量行逐字镜像）
# ─────────────────────────────────────────────────────────────────────────────

WECHAT_GUI_HEALTH_PATH: Final[str] = "/health"
WECHAT_GUI_INVOKE_PATH: Final[str] = "/invoke"
WECHAT_GUI_TOOL_SCREENSHOT: Final[str] = "screenshot"
WECHAT_GUI_TOOL_CLICK: Final[str] = "click"
WECHAT_GUI_TOOL_TYPE_TEXT: Final[str] = "type_text"
WECHAT_GUI_TOOL_PRESS_KEY: Final[str] = "press_key"
WECHAT_GUI_TOOL_OPEN_CHAT: Final[str] = "open_chat"

# ─────────────────────────────────────────────────────────────────────────────
# wire 模型（与沙盒树同名定义块逐字镜像）
# ─────────────────────────────────────────────────────────────────────────────

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

    @classmethod
    def from_wire(cls, payload: object) -> InvokeRequestModel:
        """从 wire 请求对象还原模型（严格字段校验）。

        Args:
            payload: 已解析的 JSON 载荷。

        Returns:
            工具调用请求模型。

        Raises:
            ValueError: 根节点非对象 / 携带未声明字段 / tool 非法 /
                arguments 非对象。
        """
        if not isinstance(payload, dict):
            raise ValueError("请求根节点必须是对象")
        extra = set(payload) - {"tool", "arguments"}
        if extra:
            raise ValueError(f"请求携带未声明的字段：{sorted(extra)}")
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("字段 tool 必须是非空字符串")
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("字段 arguments 必须是对象")
        return cls(tool=tool.strip(), arguments=dict(arguments))


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

    @classmethod
    def from_wire(cls, payload: object) -> InvokeResponseModel:
        """从 wire 响应对象还原模型（严格字段校验）。

        Args:
            payload: 已解析的 JSON 载荷。

        Returns:
            工具调用响应模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("响应根节点必须是对象")
        ok = payload.get("ok")
        result = payload.get("result", {})
        error = payload.get("error")
        latency_ms = payload.get("latency_ms", 0)
        if not isinstance(ok, bool):
            raise ValueError("字段 ok 必须是布尔值")
        if not isinstance(result, dict):
            raise ValueError("字段 result 必须是对象")
        if error is not None and not isinstance(error, str):
            raise ValueError("字段 error 必须是字符串或 null")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("字段 latency_ms 必须是非负整数")
        return cls(
            ok=ok,
            result=dict(result),
            error=error,
            latency_ms=latency_ms,
        )


# ─────────────────────────────────────────────────────────────────────────────
# agent HTTP 契约：五工具结果模型（与后端 wechat_gui_wire_models.py 文本镜像）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScreenshotResultModel:
    """screenshot 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        image_base64: PNG base64 载荷。
        sha256: 截图内容 sha256。
        width: 截图宽度（像素）。
        height: 截图高度（像素）。
    """

    image_base64: str
    sha256: str
    width: int
    height: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            截图结果对象。
        """
        return {
            "image_base64": self.image_base64,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_wire(cls, payload: object) -> ScreenshotResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            截图结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        image_base64 = payload.get("image_base64")
        sha256 = payload.get("sha256")
        width = payload.get("width")
        height = payload.get("height")
        if not isinstance(image_base64, str) or not image_base64:
            raise ValueError("字段 image_base64 必须是非空字符串")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("字段 sha256 必须是非空字符串")
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise ValueError("字段 width 必须是正整数")
        if isinstance(height, bool) or not isinstance(height, int) or height < 1:
            raise ValueError("字段 height 必须是正整数")
        return cls(image_base64=image_base64, sha256=sha256, width=width, height=height)


@dataclass(frozen=True)
class ClickResultModel:
    """click 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        x: 点击 X 坐标（像素）。
        y: 点击 Y 坐标（像素）。
    """

    x: int
    y: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            点击结果对象。
        """
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_wire(cls, payload: object) -> ClickResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            点击结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        x = payload.get("x")
        y = payload.get("y")
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            raise ValueError("字段 x 必须是非负整数")
        if isinstance(y, bool) or not isinstance(y, int) or y < 0:
            raise ValueError("字段 y 必须是非负整数")
        return cls(x=x, y=y)


@dataclass(frozen=True)
class TypeTextResultModel:
    """type_text 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        chars: 实际粘贴文本的字符数。
    """

    chars: int

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            文本输入结果对象。
        """
        return {"chars": self.chars}

    @classmethod
    def from_wire(cls, payload: object) -> TypeTextResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            文本输入结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        chars = payload.get("chars")
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
            raise ValueError("字段 chars 必须是非负整数")
        return cls(chars=chars)


@dataclass(frozen=True)
class PressKeyResultModel:
    """press_key 工具结果（沙盒 → backend wire 契约）。

    Attributes:
        key: 已注入的按键表达式原文。
    """

    key: str

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            按键注入结果对象。
        """
        return {"key": self.key}

    @classmethod
    def from_wire(cls, payload: object) -> PressKeyResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            按键注入结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("字段 key 必须是非空字符串")
        return cls(key=key)


@dataclass(frozen=True)
class OpenChatResultModel:
    """open_chat 工具结果（沙盒 → backend wire 契约——标题与全屏截图）。

    Attributes:
        remark_name: 搜索并打开的目标备注名。
        title_image_base64: 标题区域 PNG base64。
        full_image_base64: 全屏 PNG base64。
        title_sha256: 标题区域截图 sha256。
        full_sha256: 全屏截图 sha256。
        title_changed: 标题区域是否在截止内变化（False = 超时未变化，
            后端仍以视觉标题校验为 fail-closed 落定前提）。
    """

    remark_name: str
    title_image_base64: str
    full_image_base64: str
    title_sha256: str
    full_sha256: str
    title_changed: bool

    def to_wire(self) -> dict[str, object]:
        """转换为单行 JSON 可序列化载荷。

        Returns:
            会话打开结果对象。
        """
        return {
            "remark_name": self.remark_name,
            "title_image_base64": self.title_image_base64,
            "full_image_base64": self.full_image_base64,
            "title_sha256": self.title_sha256,
            "full_sha256": self.full_sha256,
            "title_changed": self.title_changed,
        }

    @classmethod
    def from_wire(cls, payload: object) -> OpenChatResultModel:
        """从 wire 结果对象还原模型。

        Args:
            payload: 工具结果对象。

        Returns:
            会话打开结果模型。

        Raises:
            ValueError: 根节点非对象 / 字段缺失或类型不符。
        """
        if not isinstance(payload, dict):
            raise ValueError("结果根节点必须是对象")
        remark_name = payload.get("remark_name")
        title_image_base64 = payload.get("title_image_base64")
        full_image_base64 = payload.get("full_image_base64")
        title_sha256 = payload.get("title_sha256")
        full_sha256 = payload.get("full_sha256")
        title_changed = payload.get("title_changed")
        if not isinstance(remark_name, str) or not remark_name:
            raise ValueError("字段 remark_name 必须是非空字符串")
        if not isinstance(title_image_base64, str) or not title_image_base64:
            raise ValueError("字段 title_image_base64 必须是非空字符串")
        if not isinstance(full_image_base64, str) or not full_image_base64:
            raise ValueError("字段 full_image_base64 必须是非空字符串")
        if not isinstance(title_sha256, str) or not title_sha256:
            raise ValueError("字段 title_sha256 必须是非空字符串")
        if not isinstance(full_sha256, str) or not full_sha256:
            raise ValueError("字段 full_sha256 必须是非空字符串")
        if not isinstance(title_changed, bool):
            raise ValueError("字段 title_changed 必须是布尔值")
        return cls(
            remark_name=remark_name,
            title_image_base64=title_image_base64,
            full_image_base64=full_image_base64,
            title_sha256=title_sha256,
            full_sha256=full_sha256,
            title_changed=title_changed,
        )
