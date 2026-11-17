"""harness/tools 域——工具沙盒 wire 契约 + 注入面协议（Model Class Unit 文件）。

本模块是 tools 域的 Unit 文件（Model Classes 优先于裸 dict）：

    - :class:`InvokeRequestModel` / :class:`InvokeResponseModel` /
      :class:`HealthResponseModel` —— 沙盒 wire 契约（**DUPLICATED**：与
      docker/tool-sandbox-agent 树的 ``tool_sandbox/wire_models.py`` 人为
      镜像，零代码共享——两侧类定义块 byte 级一致，契约漂移由仓库根
      漂移门禁脚本对两份定义做文本比对看护；修改任一侧必须同步另一侧）；
    - :class:`SandboxSettingsFace` —— 沙盒配置注入面协议（真实
      ``ToolsSandboxSettings`` 天然满足——结构化类型，config 域不反向
      依赖 tools 域）；
    - :class:`SandboxClientFace` —— 沙盒客户端注入面协议（真实
      :class:`~packages.harness.tools.sandbox.ToolSandboxClient` / 测试
      fake 均满足）；
    - :class:`HttpFetchArgsModel` —— http_fetch 参数面（**DUPLICATED**：
      与沙盒树 ``tool_sandbox/http_fetch.py`` 的参数模型人为镜像——后端
      侧用于 registry 本地校验 + 默认值物化（fail-fast，免网络往返）；
      沙盒侧为权威执行面，双侧校验为有意冗余）；
    - :func:`build_http_fetch_spec` —— http_fetch 工具规格（后端侧 LLM
      呈现面：名称 / 描述 / 参数 JSON Schema——duty 轮工具清单消费）；
    - B 站只读工具组参数面 :class:`BiliSearchVideosArgsModel` /
      :class:`BiliVideoInfoArgsModel` / :class:`BiliVideoCommentsArgsModel` /
      :class:`BiliSubtitleArgsModel`（**DUPLICATED**：与沙盒树
      ``tool_sandbox/bili_tools.py`` 的参数模型人为镜像——同 http_fetch
      范式；后端侧本地校验 + 默认值物化，沙盒侧为权威执行面）；
    - ``build_bili_*_spec`` 四工厂 —— B 站工具规格（后端侧 LLM 呈现面）。

wire 契约（HTTP 端点 ↔ 载荷）：

    ====================  ================================  ==========
    端点                   载荷                              鉴权
    ====================  ================================  ==========
    GET /health           :class:`HealthResponseModel`       无（存活面）
    POST /invoke          :class:`InvokeRequestModel` →      Bearer token
                          :class:`InvokeResponseModel`
    ====================  ================================  ==========

``POST /invoke`` 的 HTTP 状态语义：沙盒端点恒 200 携带
:class:`InvokeResponseModel`（工具失败在载荷 ``ok=False`` 终态内表达）；
非 200（401 鉴权失败 / 422 请求形态非法）由客户端按传输层失败处理。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from packages.harness.llm.llm_models import ToolParametersSchema
from packages.harness.tools.tool_models import ToolResultModel, ToolSpecModel

__all__ = [
    "InvokeRequestModel",
    "InvokeResponseModel",
    "HealthResponseModel",
    "SandboxSettingsFace",
    "SandboxClientFace",
    "HTTP_FETCH_TOOL_NAME",
    "HttpFetchArgsModel",
    "build_http_fetch_spec",
    "BILI_SEARCH_VIDEOS_TOOL_NAME",
    "BILI_VIDEO_INFO_TOOL_NAME",
    "BILI_VIDEO_COMMENTS_TOOL_NAME",
    "BILI_VIDEO_SUBTITLE_TOOL_NAME",
    "BILI_TOOL_NAMES",
    "BiliSearchVideosArgsModel",
    "BiliVideoInfoArgsModel",
    "BiliVideoCommentsArgsModel",
    "BiliSubtitleArgsModel",
    "build_bili_search_videos_spec",
    "build_bili_video_info_spec",
    "build_bili_video_comments_spec",
    "build_bili_video_subtitle_spec",
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


class SandboxSettingsFace(Protocol):
    """工具沙盒配置注入面协议（真实 ``ToolsSandboxSettings`` 天然满足）。

    结构化类型：config 域的 ``Settings.tools_sandbox`` 分节（BaseSettings）
    按属性结构满足本协议——tools 域消费配置而不 import config（依赖
    方向 config → core 之外，域间经协议解耦——与 MemorySearchFace 同
    约定）。

    Attributes:
        enabled: 是否启用工具沙盒。
        base_url: 沙盒服务基础 URL。
        token_env: 鉴权 token 环境变量名（实际 token 从该变量读取）。
        request_timeout_seconds: 单次请求超时秒数（invoke / 探测共用）。
    """

    enabled: bool
    base_url: str
    token_env: str
    request_timeout_seconds: float


class SandboxClientFace(Protocol):
    """沙盒客户端注入面协议（真实 ToolSandboxClient / 测试 fake 均满足）。

    Attributes:
        has_token: 是否持有 Bearer token（False = 装配层应跳过沙盒工具
            注册——warn-and-continue 语义）。
    """

    @property
    def has_token(self) -> bool:
        """是否持有 Bearer token（False = 装配层应跳过沙盒工具注册）。"""
        ...

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """执行沙盒工具（恒不抛——失败归一 ok=False 终态）。

        Args:
            tool: 工具名。
            arguments: 调用参数（registry 校验物化后的 dict）。

        Returns:
            执行结果（成功 result = 沙盒工具结果 dict；失败 ok=False
                + error 人读原因）。
        """
        ...

    async def probe_health(self) -> bool:
        """健康探测（GET /health——恒不抛，任何失败 False）。

        Returns:
            沙盒是否存活（200 + status=ok）。
        """
        ...


#: http_fetch 工具名（与沙盒树同名约定——跨树标识一致）。
HTTP_FETCH_TOOL_NAME: str = "http_fetch"

#: 允许的 URL 协议面（与沙盒树 http_fetch 模块同约定——参数模型镜像面）。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: http_fetch 参数 JSON Schema（与 HttpFetchArgsModel 字段对齐——LLM 呈现面）。
_HTTP_FETCH_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "目标 URL（http/https）",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "请求超时秒数（1-30，默认 10）",
                "minimum": 1,
                "maximum": 30,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
)


class HttpFetchArgsModel(BaseModel):
    """http_fetch 参数面（url + 超时秒——沙盒侧校验锚点）。

    Attributes:
        url: 目标 URL（http/https 且含主机名——协议与主机在字段校验
            层拒绝；域名允许清单在执行层校验）。
        timeout_seconds: 请求超时秒数（1-30，默认 10）。
    """

    model_config = {"extra": "forbid"}

    url: str = Field(min_length=1, max_length=2048, description="目标 URL（http/https）")
    timeout_seconds: int = Field(
        default=10, ge=1, le=30, description="请求超时秒数（1-30，默认 10）"
    )

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


def build_http_fetch_spec() -> ToolSpecModel:
    """构建 http_fetch 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（经 ``register_sandbox_tool`` 装配——执行器代理到沙盒；
        参数校验经 :class:`HttpFetchArgsModel` 本地物化默认值后走 wire）。
    """
    return ToolSpecModel(
        name=HTTP_FETCH_TOOL_NAME,
        description="抓取 HTTP/HTTPS 页面文本（经隔离沙盒执行；域名允许清单管制；正文按字节截断 8KB）。",
        args_schema=_HTTP_FETCH_SCHEMA,
        args_model=HttpFetchArgsModel,
    )


# ---------------------------------------------------------------------------
# B 站只读工具组（平台工具——参数面与沙盒树 bili_tools.py 人为镜像）
# ---------------------------------------------------------------------------
# 块边界约定（镜像 HttpFetchArgsModel 范式——漂移门禁块提取口径）：四个
# 参数模型类**连续排布**，其前可以有注释 / 常量；BiliSubtitleArgsModel
# （末位模型）到下一顶层定义行（首个 build_*_spec 工厂）之间**只隔空行**
# ——保证按「类定义行 → 下一顶层定义行」提取的块在两树 byte 级一致。

#: bili_search_videos 工具名（与沙盒树同名约定——跨树标识一致）。
BILI_SEARCH_VIDEOS_TOOL_NAME: str = "bili_search_videos"

#: bili_video_info 工具名（同上）。
BILI_VIDEO_INFO_TOOL_NAME: str = "bili_video_info"

#: bili_video_comments 工具名（同上）。
BILI_VIDEO_COMMENTS_TOOL_NAME: str = "bili_video_comments"

#: bili_video_subtitle 工具名（同上）。
BILI_VIDEO_SUBTITLE_TOOL_NAME: str = "bili_video_subtitle"

#: 全部 B 站只读工具名（装配 / 日志面消费——顺序即注册序）。
BILI_TOOL_NAMES: tuple[str, ...] = (
    BILI_SEARCH_VIDEOS_TOOL_NAME,
    BILI_VIDEO_INFO_TOOL_NAME,
    BILI_VIDEO_COMMENTS_TOOL_NAME,
    BILI_VIDEO_SUBTITLE_TOOL_NAME,
)


#: bili_search_videos 参数 JSON Schema（与 BiliSearchVideosArgsModel 字段对齐）。
_BILI_SEARCH_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键词（1-64 字符）",
                "minLength": 1,
                "maxLength": 64,
            },
            "page": {
                "type": "integer",
                "description": "页码（1-5，默认 1）",
                "minimum": 1,
                "maximum": 5,
            },
            "page_size": {
                "type": "integer",
                "description": "每页条数（10/20/30，默认 20）",
                "enum": [10, 20, 30],
            },
        },
        "required": ["keyword"],
        "additionalProperties": False,
    }
)

#: bili_video_info 参数 JSON Schema（与 BiliVideoInfoArgsModel 字段对齐）。
_BILI_VIDEO_INFO_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "bvid": {
                "type": "string",
                "description": "Bilibili 视频 BV 号（BV + 10 位字母数字）",
                "minLength": 12,
                "maxLength": 12,
                "pattern": r"^BV[0-9A-Za-z]{10}$",
            },
        },
        "required": ["bvid"],
        "additionalProperties": False,
    }
)

#: bili_video_comments 参数 JSON Schema（与 BiliVideoCommentsArgsModel 字段对齐）。
_BILI_VIDEO_COMMENTS_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "bvid": {
                "type": "string",
                "description": "Bilibili 视频 BV 号（BV + 10 位字母数字）",
                "minLength": 12,
                "maxLength": 12,
                "pattern": r"^BV[0-9A-Za-z]{10}$",
            },
            "page": {
                "type": "integer",
                "description": "页码（1-50，默认 1）",
                "minimum": 1,
                "maximum": 50,
            },
            "page_size": {
                "type": "integer",
                "description": "每页条数（10-20，默认 20）",
                "minimum": 10,
                "maximum": 20,
            },
        },
        "required": ["bvid"],
        "additionalProperties": False,
    }
)

#: bili_video_subtitle 参数 JSON Schema（与 BiliSubtitleArgsModel 字段对齐）。
_BILI_VIDEO_SUBTITLE_SCHEMA: ToolParametersSchema = ToolParametersSchema.model_validate(
    {
        "type": "object",
        "properties": {
            "bvid": {
                "type": "string",
                "description": "Bilibili 视频 BV 号（BV + 10 位字母数字）",
                "minLength": 12,
                "maxLength": 12,
                "pattern": r"^BV[0-9A-Za-z]{10}$",
            },
            "fetch_subtitle": {
                "type": "boolean",
                "description": "是否抓取首个字幕文件正文（默认否）",
            },
        },
        "required": ["bvid"],
        "additionalProperties": False,
    }
)


class BiliSearchVideosArgsModel(BaseModel):
    """bili_search_videos 参数面（关键词视频搜索——沙盒侧校验锚点）。

    Attributes:
        keyword: 搜索关键词（1-64 字符）。
        page: 页码（1-5，默认 1）。
        page_size: 每页条数（10/20/30，默认 20）。
    """

    model_config = {"extra": "forbid"}

    keyword: str = Field(min_length=1, max_length=64, description="搜索关键词（1-64 字符）")
    page: int = Field(default=1, ge=1, le=5, description="页码（1-5，默认 1）")
    page_size: Literal[10, 20, 30] = Field(default=20, description="每页条数（10/20/30，默认 20）")


class BiliVideoInfoArgsModel(BaseModel):
    """bili_video_info 参数面（BV 号单视频详情——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )


class BiliVideoCommentsArgsModel(BaseModel):
    """bili_video_comments 参数面（视频评论读取——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
        page: 页码（1-50，默认 1——sort=2 时间序页序）。
        page_size: 每页条数（10-20，默认 20——接口 ps 上限 20）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )
    page: int = Field(default=1, ge=1, le=50, description="页码（1-50，默认 1）")
    page_size: int = Field(default=20, ge=10, le=20, description="每页条数（10-20，默认 20）")


class BiliSubtitleArgsModel(BaseModel):
    """bili_video_subtitle 参数面（视频字幕读取——沙盒侧校验锚点）。

    Attributes:
        bvid: Bilibili 视频 BV 号（``BV`` + 10 位字母数字，共 12 字符）。
        fetch_subtitle: 是否抓取首个字幕文件正文（默认 False——仅返回
            字幕清单；True 时经 CDN 后缀允许清单管制抓取）。
    """

    model_config = {"extra": "forbid"}

    bvid: str = Field(
        min_length=12,
        max_length=12,
        pattern=r"^BV[0-9A-Za-z]{10}$",
        description="Bilibili 视频 BV 号（BV + 10 位字母数字）",
    )
    fetch_subtitle: bool = Field(default=False, description="是否抓取首个字幕文件正文（默认否）")


def build_bili_search_videos_spec() -> ToolSpecModel:
    """构建 bili_search_videos 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（经 ``register_sandbox_tools_for_bili`` 装配——执行器
        代理到沙盒；参数校验经 :class:`BiliSearchVideosArgsModel` 本地
        物化默认值后走 wire）。
    """
    return ToolSpecModel(
        name=BILI_SEARCH_VIDEOS_TOOL_NAME,
        description="搜索 Bilibili 视频（经隔离沙盒执行；公开只读接口；关键词 + 分页；返回白名单裁剪字段）。",
        args_schema=_BILI_SEARCH_SCHEMA,
        args_model=BiliSearchVideosArgsModel,
    )


def build_bili_video_info_spec() -> ToolSpecModel:
    """构建 bili_video_info 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_bili_search_videos_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=BILI_VIDEO_INFO_TOOL_NAME,
        description="获取 Bilibili 视频详情（经隔离沙盒执行；公开只读接口；标题 / 简介 / UP 主 / 时长 / stat 子集 / 首 P cid）。",
        args_schema=_BILI_VIDEO_INFO_SCHEMA,
        args_model=BiliVideoInfoArgsModel,
    )


def build_bili_video_comments_spec() -> ToolSpecModel:
    """构建 bili_video_comments 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_bili_search_videos_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=BILI_VIDEO_COMMENTS_TOOL_NAME,
        description="获取 Bilibili 视频评论（经隔离沙盒执行；公开只读接口；时间序游标分页；返回白名单裁剪扁平列表）。",
        args_schema=_BILI_VIDEO_COMMENTS_SCHEMA,
        args_model=BiliVideoCommentsArgsModel,
    )


def build_bili_video_subtitle_spec() -> ToolSpecModel:
    """构建 bili_video_subtitle 工具规格（后端侧 LLM 呈现面）。

    Returns:
        工具规格（同 ``build_bili_search_videos_spec`` 装配范式）。
    """
    return ToolSpecModel(
        name=BILI_VIDEO_SUBTITLE_TOOL_NAME,
        description="获取 Bilibili 视频字幕清单（经隔离沙盒执行；公开只读接口；可选抓取首个字幕正文——CDN 后缀允许清单管制）。",
        args_schema=_BILI_VIDEO_SUBTITLE_SCHEMA,
        args_model=BiliSubtitleArgsModel,
    )
