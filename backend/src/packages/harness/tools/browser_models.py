"""harness/tools 域——浏览器 sidecar wire 契约 + 注入面协议（Model Class
Unit 文件）。

本模块是 tools 域的 Unit 文件（Model Classes 优先于裸 dict）：

    - :class:`InvokeRequestModel` / :class:`InvokeResponseModel` /
      :class:`HealthResponseModel` —— sidecar wire 契约（**DUPLICATED**：与
      docker/browser-sidecar 树的 ``browser_sidecar/wire_models.py`` 人为
      镜像，零代码共享——两侧类定义块 byte 级一致，契约漂移由仓库根
      漂移门禁脚本对两份定义做文本比对看护；修改任一侧必须同步另一侧）；
    - :class:`BrowserSidecarSettingsFace` —— sidecar 配置注入面协议
      （真实 ``ToolsBrowserSidecarSettings`` 天然满足——结构化类型，
      config 域不反向依赖 tools 域）；
    - :class:`BrowserSidecarClientFace` —— sidecar 客户端注入面协议
      （真实 :class:`~packages.harness.tools.browser.BrowserSidecarClient` /
      测试 fake 均满足）；
    - 七浏览器工具参数面 :class:`NavigateArgsModel` /
      :class:`ReadTextArgsModel` / :class:`ScreenshotArgsModel` /
      :class:`ClickArgsModel` / :class:`FillArgsModel` /
      :class:`ListTabsArgsModel` / :class:`CloseTabArgsModel`
      （**DUPLICATED**：与 sidecar 树 ``browser_sidecar/browser.py`` 的参数
      模型人为镜像——后端侧用于 registry 本地校验 + 默认值物化
      （fail-fast，免网络往返）；sidecar 侧为权威执行面，双侧校验为有意
      冗余）；
    - ``build_<tool>_spec`` 七工厂 —— 浏览器工具规格（后端侧 LLM 呈现面）。

wire 契约（HTTP 端点 ↔ 载荷；REST 同构镜像 tool-sandbox——非新协议）：

    ====================  ================================  ==========
    端点                   载荷                              鉴权
    ====================  ================================  ==========
    GET /health           :class:`HealthResponseModel`       无（存活面）
    POST /invoke          :class:`InvokeRequestModel` →      Bearer token
                           :class:`InvokeResponseModel`
    ====================  ================================  ==========

``POST /invoke`` 的 HTTP 状态语义：sidecar 端点恒 200 携带
:class:`InvokeResponseModel`（工具失败在载荷 ``ok=False`` 终态内表达）；
非 200（401 鉴权失败 / 422 请求形态非法）由客户端按传输层失败处理。
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.tools.tool_models import ToolResultModel, ToolSpecModel

__all__ = [
    "InvokeRequestModel",
    "InvokeResponseModel",
    "HealthResponseModel",
    "BrowserSidecarSettingsFace",
    "BrowserSidecarClientFace",
    "NAVIGATE_TOOL_NAME",
    "READ_TEXT_TOOL_NAME",
    "SCREENSHOT_TOOL_NAME",
    "CLICK_TOOL_NAME",
    "FILL_TOOL_NAME",
    "LIST_TABS_TOOL_NAME",
    "CLOSE_TAB_TOOL_NAME",
    "BROWSER_TOOL_NAMES",
    "NavigateArgsModel",
    "ReadTextArgsModel",
    "ScreenshotArgsModel",
    "ClickArgsModel",
    "FillArgsModel",
    "ListTabsArgsModel",
    "CloseTabArgsModel",
    "build_navigate_spec",
    "build_read_text_spec",
    "build_screenshot_spec",
    "build_click_spec",
    "build_fill_spec",
    "build_list_tabs_spec",
    "build_close_tab_spec",
]


class InvokeRequestModel(BaseModel):
    """浏览器 sidecar 工具调用请求（backend → sidecar wire 契约——POST /invoke 载荷）。

    Attributes:
        tool: 工具名（sidecar 运行时内唯一标识——七工具面见 browser.py）。
        arguments: 调用参数（JSON 对象——sidecar 侧经工具参数 Model Class
            校验 + 默认值物化后进入执行器）。
    """

    model_config = {"extra": "forbid"}

    tool: str = Field(min_length=1, max_length=64, description="工具名（sidecar 内唯一标识）")
    arguments: dict[str, Any] = Field(default_factory=dict, description="调用参数（JSON 对象）")


class InvokeResponseModel(BaseModel):
    """浏览器 sidecar 工具调用响应（sidecar → backend wire 契约——恒携带成败终态）。

    Attributes:
        ok: 是否成功（False = 未知工具 / 参数校验失败 / 执行器受控失败 /
            执行器异常——四类失败均归一为本形态，sidecar 端点恒 200 携带）。
        result: 结果 JSON（成功时为工具执行器返回 dict；失败恒空 dict）。
        error: 失败原因（成功为 None；人读中文字符串）。
        latency_ms: sidecar 侧单次执行时延（epoch 毫秒差；含失败路径耗时）。
    """

    ok: bool = Field(default=True, description="是否成功")
    result: dict[str, Any] = Field(default_factory=dict, description="结果 JSON")
    error: str | None = Field(default=None, description="失败原因（成功为 None）")
    latency_ms: int = Field(default=0, ge=0, description="sidecar 侧单次执行时延（毫秒）")


class HealthResponseModel(BaseModel):
    """浏览器 sidecar 健康探测响应（GET /health 载荷——无鉴权存活面）。

    Attributes:
        status: 存活状态（恒 ``"ok"``——backend 启动期探测的判定面）。
    """

    status: str = Field(default="ok", description="存活状态（恒 ok）")


class BrowserSidecarSettingsFace(Protocol):
    """浏览器 sidecar 配置注入面协议（真实 ``ToolsBrowserSidecarSettings``
    天然满足）。

    结构化类型：config 域的 ``Settings.tools_browser_sidecar`` 分节
    （BaseSettings）按属性结构满足本协议——tools 域消费配置而不 import
    config（域间经协议解耦——与 SandboxSettingsFace 同约定）。

    Attributes:
        enabled: 是否启用浏览器 sidecar。
        base_url: sidecar 服务基础 URL。
        token_env: 鉴权 token 环境变量名（实际 token 从该变量读取）。
        request_timeout_seconds: 单次请求超时秒数（invoke / 探测共用）。
    """

    enabled: bool
    base_url: str
    token_env: str
    request_timeout_seconds: float


class BrowserSidecarClientFace(Protocol):
    """浏览器 sidecar 客户端注入面协议（真实 BrowserSidecarClient / 测试
    fake 均满足）。

    Attributes:
        has_token: 是否持有 Bearer token（False = 装配层应跳过浏览器工具
            注册——warn-and-continue 语义）。
    """

    @property
    def has_token(self) -> bool:
        """是否持有 Bearer token（False = 装配层应跳过浏览器工具注册）。"""
        ...

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """执行浏览器工具（恒不抛——失败归一 ok=False 终态）。

        Args:
            tool: 工具名。
            arguments: 调用参数（registry 校验物化后的 dict）。

        Returns:
            执行结果（成功 result = sidecar 工具结果 dict；失败 ok=False
                + error 人读原因）。
        """
        ...

    async def probe_health(self) -> bool:
        """健康探测（GET /health——恒不抛，任何失败 False）。

        Returns:
            sidecar 是否存活（200 + status=ok）。
        """
        ...


# ---------------------------------------------------------------------------
# 浏览器七工具（参数面与 sidecar 树 browser_sidecar/browser.py 人为镜像）
# ---------------------------------------------------------------------------
# 块边界约定（镜像 HttpFetchArgsModel / B 站参数模型范式——漂移门禁块
# 提取口径）：七个参数模型类**连续排布**，其前可以有注释 / 常量；
# CloseTabArgsModel（末位模型）到下一顶层定义行（首个 build_*_spec 工厂）
# 之间**只隔空行**——保证按「类定义行 → 下一顶层定义行」提取的块在两树
# byte 级一致。

#: navigate 工具名（与 sidecar 树同名约定——跨树标识一致）。
NAVIGATE_TOOL_NAME: str = "navigate"

#: read_text 工具名（同上）。
READ_TEXT_TOOL_NAME: str = "read_text"

#: screenshot 工具名（同上）。
SCREENSHOT_TOOL_NAME: str = "screenshot"

#: click 工具名（同上）。
CLICK_TOOL_NAME: str = "click"

#: fill 工具名（同上）。
FILL_TOOL_NAME: str = "fill"

#: list_tabs 工具名（同上）。
LIST_TABS_TOOL_NAME: str = "list_tabs"

#: close_tab 工具名（同上）。
CLOSE_TAB_TOOL_NAME: str = "close_tab"

#: 全部浏览器工具名（装配 / 日志面消费——顺序即注册序）。
BROWSER_TOOL_NAMES: tuple[str, ...] = (
    NAVIGATE_TOOL_NAME,
    READ_TEXT_TOOL_NAME,
    SCREENSHOT_TOOL_NAME,
    CLICK_TOOL_NAME,
    FILL_TOOL_NAME,
    LIST_TABS_TOOL_NAME,
    CLOSE_TAB_TOOL_NAME,
)

#: 允许的 URL 协议面（参数模型校验——与 sidecar 树 browser.py 同值镜像）。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: navigate 参数 JSON Schema（与 NavigateArgsModel 字段对齐——LLM 呈现面）。
_NAVIGATE_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "目标 URL（http/https）",
                "minLength": 1,
                "maxLength": 2048,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
)

#: read_text 参数 JSON Schema（与 ReadTextArgsModel 字段对齐）。
_READ_TEXT_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "读取字符上限（1-100000，默认 8000）",
                "minimum": 1,
                "maximum": 100000,
            },
        },
        "additionalProperties": False,
    }
)

#: screenshot 参数 JSON Schema（与 ScreenshotArgsModel 字段对齐）。
_SCREENSHOT_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "是否整页截图（默认否）",
            },
        },
        "additionalProperties": False,
    }
)

#: click 参数 JSON Schema（与 ClickArgsModel 字段对齐）。
_CLICK_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS 选择器",
                "minLength": 1,
                "maxLength": 512,
            },
        },
        "required": ["selector"],
        "additionalProperties": False,
    }
)

#: fill 参数 JSON Schema（与 FillArgsModel 字段对齐）。
_FILL_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS 选择器",
                "minLength": 1,
                "maxLength": 512,
            },
            "value": {
                "type": "string",
                "description": "填充值（空串 = 清空字段）",
                "maxLength": 8192,
            },
        },
        "required": ["selector", "value"],
        "additionalProperties": False,
    }
)

#: list_tabs / close_tab 参数 JSON Schema（空 object / 索引面）。
_LIST_TABS_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
)

#: close_tab 参数 JSON Schema（与 CloseTabArgsModel 字段对齐）。
_CLOSE_TAB_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "tab_index": {
                "type": "integer",
                "description": "标签页索引（list_tabs 报告的 index）",
                "minimum": 0,
            },
        },
        "required": ["tab_index"],
        "additionalProperties": False,
    }
)


class NavigateArgsModel(BaseModel):
    """navigate 参数面（导航目标 URL——sidecar 侧校验锚点）。

    Attributes:
        url: 目标 URL（http/https 且含主机名——协议与主机在字段校验
            层拒绝；允许清单 / 私网拒绝 / DNS 解析在执行层校验）。
    """

    model_config = {"extra": "forbid"}

    url: str = Field(min_length=1, max_length=2048, description="目标 URL（http/https）")

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, value: str) -> str:
        """校验 URL 协议与主机名（http/https + 主机非空）。

        Args:
            value: 原始 URL 字符串。

        Returns:
            校验通过的原值。

        Raises:
            ValueError: 协议非法或主机名缺失（装配 / 调用参数面）。
        """
        parsed = urlparse(value)
        if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            raise ValueError("url 必须是 http/https 协议且包含主机名")
        return value


class ReadTextArgsModel(BaseModel):
    """read_text 参数面（当前页面正文读取——sidecar 侧校验锚点）。

    Attributes:
        max_chars: 读取字符上限（1-100000，默认 8000——超限截断 +
            ``truncated`` 标记；返回原始文本，无任何提示词包装）。
    """

    model_config = {"extra": "forbid"}

    max_chars: int = Field(
        default=8000, ge=1, le=100000, description="读取字符上限（1-100000，默认 8000）"
    )


class ScreenshotArgsModel(BaseModel):
    """screenshot 参数面（当前页面截图——sidecar 侧校验锚点）。

    Attributes:
        full_page: 是否整页截图（默认 False——仅视口；True 时按滚动
            尺寸截图；两种形态均受截图尺寸上限 clip 裁剪）。
    """

    model_config = {"extra": "forbid"}

    full_page: bool = Field(default=False, description="是否整页截图（默认否）")


class ClickArgsModel(BaseModel):
    """click 参数面（CSS 选择器点击——sidecar 侧校验锚点）。

    Attributes:
        selector: CSS 选择器（1-512 字符）。
    """

    model_config = {"extra": "forbid"}

    selector: str = Field(min_length=1, max_length=512, description="CSS 选择器")


class FillArgsModel(BaseModel):
    """fill 参数面（表单字段填充——sidecar 侧校验锚点）。

    Attributes:
        selector: CSS 选择器（1-512 字符）。
        value: 填充值（0-8192 字符——空串合法，语义为清空字段）。
    """

    model_config = {"extra": "forbid"}

    selector: str = Field(min_length=1, max_length=512, description="CSS 选择器")
    value: str = Field(max_length=8192, description="填充值（空串 = 清空字段）")


class ListTabsArgsModel(BaseModel):
    """list_tabs 参数面（无参数——空 schema 的校验锚点）。"""

    model_config = {"extra": "forbid"}


class CloseTabArgsModel(BaseModel):
    """close_tab 参数面（按索引关闭标签页——sidecar 侧校验锚点）。

    Attributes:
        tab_index: 标签页索引（>= 0——list_tabs 报告的 index 面）。
    """

    model_config = {"extra": "forbid"}

    tab_index: int = Field(ge=0, description="标签页索引（list_tabs 报告的 index）")


def build_navigate_spec() -> ToolSpecModel:
    """构建 navigate 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（经 ``register_browser_sidecar_tools`` 装配——执行器代理
        到 sidecar；参数校验经 :class:`NavigateArgsModel` 本地物化后走 wire）。
    """
    return ToolSpecModel(
        name=NAVIGATE_TOOL_NAME,
        description="浏览器导航到目标 URL（经隔离 sidecar 执行；策略链：协议→私网拒绝→DNS→允许清单→路由拦截；默认 20s 超时）。",
        args_schema=_NAVIGATE_SCHEMA,
        args_model=NavigateArgsModel,
    )


def build_read_text_spec() -> ToolSpecModel:
    """构建 read_text 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（结果文本经不可信平台内容标注包裹——见
        ``harness/tools/browser.py`` 的结果格式化）。
    """
    return ToolSpecModel(
        name=READ_TEXT_TOOL_NAME,
        description="读取浏览器当前页面正文（经隔离 sidecar 执行；返回文本按不可信平台内容标注；按 max_chars 截断）。",
        args_schema=_READ_TEXT_SCHEMA,
        args_model=ReadTextArgsModel,
    )


def build_screenshot_spec() -> ToolSpecModel:
    """构建 screenshot 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（结果携带 base64 PNG——视觉理解配合 look_at 工具消费）。
    """
    return ToolSpecModel(
        name=SCREENSHOT_TOOL_NAME,
        description="截取浏览器当前页面截图（经隔离 sidecar 执行；返回 base64 PNG + 宽高——视觉理解配合 look_at 工具消费）。",
        args_schema=_SCREENSHOT_SCHEMA,
        args_model=ScreenshotArgsModel,
    )


def build_click_spec() -> ToolSpecModel:
    """构建 click 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_navigate_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=CLICK_TOOL_NAME,
        description="点击浏览器页面元素（CSS 选择器——经隔离 sidecar 执行）。",
        args_schema=_CLICK_SCHEMA,
        args_model=ClickArgsModel,
    )


def build_fill_spec() -> ToolSpecModel:
    """构建 fill 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_navigate_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=FILL_TOOL_NAME,
        description="填充浏览器表单字段（CSS 选择器 + 值——经隔离 sidecar 执行；空值 = 清空）。",
        args_schema=_FILL_SCHEMA,
        args_model=FillArgsModel,
    )


def build_list_tabs_spec() -> ToolSpecModel:
    """构建 list_tabs 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_navigate_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=LIST_TABS_TOOL_NAME,
        description="列举浏览器标签页（索引 / URL / 标题——经隔离 sidecar 执行）。",
        args_schema=_LIST_TABS_SCHEMA,
        args_model=ListTabsArgsModel,
    )


def build_close_tab_spec() -> ToolSpecModel:
    """构建 close_tab 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_navigate_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=CLOSE_TAB_TOOL_NAME,
        description="按索引关闭浏览器标签页（经隔离 sidecar 执行；标签页上限 4）。",
        args_schema=_CLOSE_TAB_SCHEMA,
        args_model=CloseTabArgsModel,
    )
