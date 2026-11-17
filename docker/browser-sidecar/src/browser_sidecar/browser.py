"""浏览器 sidecar playwright 运行时——七工具执行面（本树的核心模块）。

职责链（navigate 工具的完整策略链——顺序锁定，纯判定步在 policy.py）::

    navigate(url)
        ↓ ① 协议面（policy.url_scheme_is_allowed——http/https 之外一律拒绝）
        ↓ ② 主机提取（policy.extract_url_host——小写 + 去尾点）
        ↓ ③ 私网拒绝（IP 字面量：policy.ip_literal_host_is_private——无论
        ↓     允许清单；域名：先 DNS 解析（注入 resolver），任一解析 IP
        ↓     私网即拒绝——DNS rebinding 对策：解析先行、按主机名导航）
        ↓ ④ 允许清单（policy.require_host_allowed——空清单 = 拒绝一切；
        ↓     点边界后缀匹配）
        ↓ ⑤ 导航 + 路由拦截（context 创建期 page.route("**/*")——子资源与
              重定向逐跳按 request_host_allowed 判定，被拒即静默 abort）

不可信内容分工（锁定决策——两侧 docstring 同源镜像）：本模块
``read_text`` 返回**原始页面文本**（零提示词包装——sidecar 不做任何
prompt 组装）；「平台内容不可信」的标注与包装由**后端消费层**承担
（backend harness/tools/browser.py 按记忆域 provenance=untrusted 约定
包裹文本后才进入 LLM / 记忆链路）。

浏览器上下文池（约束 4.3 的本树形态——文档化决策）：本树无 httpx 池；
「池」即 :class:`BrowserManager` 的 context-per-profile 注册表——
profile 名 → 持久化 BrowserContext（``profiles/<name>`` 卷挂载目录，
cookies / localStorage 跨重启留存），进程内单例复用，生命周期归
server lifespan（start 空闲收割任务 / stop 逆序关闭）。

资源护栏（逐条锁定）：

    - 导航超时：默认 20s（env ``BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS``
      覆写，1-120s）；
    - 截图尺寸上限：宽 1920 / 高 4096（clip 裁剪）+ 体积上限 6MiB
      （超限受控失败——wire 载荷面 bounded）；
    - 标签页上限 4（工具开页拒绝 + 弹出页超出即关——路由拦截外的
      第二道闸）;
    - 标签页空闲关闭 10 分钟（后台收割任务——时钟 / sleep 可注入）；
    - chromium 启动参数 ``--no-sandbox``（容器内标准形态——文档化
      权衡：sidecar 以非 root + compose 资源限额 + 网络策略收敛该面）
      + ``--disable-dev-shm-usage``（/dev/shm 128MB 默认不足——配合
      compose shm_size）。

测试纪律：本模块全部逻辑经注入面驱动（context 工厂 / resolver /
时钟 / sleep / playwright 适配层经手写 fake playwright 对象）——
**真实浏览器行为在部署机验证**（见本目录 README.md「部署验证」），
单测零浏览器 / 零网络。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

from playwright.async_api import BrowserContext as PlaywrightBrowserContext
from playwright.async_api import (
    BrowserType,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from pydantic import BaseModel, Field, field_validator

from browser_sidecar import policy
from browser_sidecar.runtime import (
    BrowserSidecarError,
    BrowserSidecarRuntime,
    BrowserSidecarToolSpec,
)

__all__ = [
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
    "TabInfoModel",
    "PageFace",
    "ContextFace",
    "PlaywrightPage",
    "PlaywrightContext",
    "BrowserSession",
    "BrowserManager",
    "Resolver",
    "default_resolve_host",
    "launch_profile_context",
    "register_browser_tools",
    "build_default_browser_sidecar",
]

# ---------------------------------------------------------------------------
# 工具名常量（与后端侧 harness/tools/browser_models.py 同名约定——跨树标识
# 一致；漂移门禁比对行）
# ---------------------------------------------------------------------------

#: navigate 工具名（后端侧 LLM 工具面同名注册——跨树约定标识）。
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

#: 全部浏览器工具名（注册序——观测面）。
BROWSER_TOOL_NAMES: tuple[str, ...] = (
    NAVIGATE_TOOL_NAME,
    READ_TEXT_TOOL_NAME,
    SCREENSHOT_TOOL_NAME,
    CLICK_TOOL_NAME,
    FILL_TOOL_NAME,
    LIST_TABS_TOOL_NAME,
    CLOSE_TAB_TOOL_NAME,
)

# ---------------------------------------------------------------------------
# 常量面（env 键 + 资源护栏）
# ---------------------------------------------------------------------------

#: 域名允许清单环境变量（容器内注入；空 = navigate 拒绝一切——默认拒绝）。
_ALLOWLIST_ENV: str = "BROWSER_SIDECAR_ALLOWLIST"

#: 导航超时秒数环境变量（1-120；缺省 20）。
_NAV_TIMEOUT_ENV: str = "BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS"

#: 生效 profile 名环境变量（context-per-profile 注册表的键——缺省 default）。
_PROFILE_ENV: str = "BROWSER_SIDECAR_PROFILE"

#: profiles 根目录环境变量（容器形态挂载点 /app/profiles——缺省相对
#: 路径 profiles，裸跑落在 CWD 下）。
_PROFILES_DIR_ENV: str = "BROWSER_SIDECAR_PROFILES_DIR"

#: 缺省导航超时（秒）。
_DEFAULT_NAV_TIMEOUT_SECONDS: float = 20.0

#: 导航超时合法范围（秒）。
_NAV_TIMEOUT_MIN_SECONDS: float = 1.0
_NAV_TIMEOUT_MAX_SECONDS: float = 120.0

#: 允许的 URL 协议面（参数模型校验——与 policy._ALLOWED_SCHEMES 同值；
#: 独立持有以保持参数模型块的双树 byte 镜像自包含）。
_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")

#: 标签页上限（工具开页 + 弹出页共同受限）。
_MAX_TABS: int = 4

#: 标签页空闲关闭秒数（后台收割任务——10 分钟）。
_IDLE_CLOSE_SECONDS: float = 600.0

#: 空闲收割巡检间隔秒数。
_SWEEP_INTERVAL_SECONDS: float = 60.0

#: 截图宽度上限（像素——clip 裁剪）。
_MAX_SCREENSHOT_WIDTH: int = 1920

#: 截图高度上限（像素——clip 裁剪；整页截图的滚动高度封顶）。
_MAX_SCREENSHOT_HEIGHT: int = 4096

#: 截图体积上限（字节——超限受控失败，wire 载荷面 bounded）。
_MAX_SCREENSHOT_BYTES: int = 6 * 1024 * 1024

#: 点击 / 填充动作超时（毫秒——选择器等待上限）。
_ACTION_TIMEOUT_MS: int = 10_000

#: 缺省视口（context 创建期固定——截图视口形态的尺寸面）。
_DEFAULT_VIEWPORT: tuple[int, int] = (1280, 720)

#: chromium 启动参数（容器内标准形态——见模块 docstring 资源护栏一节）。
_CHROMIUM_LAUNCH_ARGS: tuple[str, ...] = ("--no-sandbox", "--disable-dev-shm-usage")

#: DNS 解析器契约（主机名 → 解析 IP 文本列表；测试注入面同签名）。
Resolver = Callable[[str], Awaitable[list[str]]]

#: context 工厂契约（profile 名 → 持久化 context——测试注入面同签名）。
ContextFactory = Callable[[str], Awaitable["ContextFace"]]

#: playwright 操作包装的返回类型变量。
_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# 参数面 Model Classes（与后端 harness/tools/browser_models.py 人为镜像——
# 类定义块 byte 级一致；后端侧用于 registry 本地校验 + 默认值物化）；
# 紧随其后为结果面 Model Classes 与 face 协议。块边界约定（漂移门禁块
# 提取口径）：参数模型类**连续排布**，其前可以有注释 / 常量；
# CloseTabArgsModel（末位模型）到下一顶层定义行（TabInfoModel）之间
# **只隔空行**——保证按「类定义行 → 下一顶层定义行」提取的块在两树
# byte 级一致。
# ---------------------------------------------------------------------------


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


class TabInfoModel(BaseModel):
    """标签页信息（list_tabs 结果面——白名单字段）。

    Attributes:
        index: 标签页索引（close_tab 消费的 index 面）。
        url: 当前 URL。
        title: 页面标题。
    """

    index: int = Field(description="标签页索引")
    url: str = Field(description="当前 URL")
    title: str = Field(description="页面标题")


# ---------------------------------------------------------------------------
# 注入面协议（face——执行器逻辑的测试驱动面；真实实现为 playwright 适配层）
# ---------------------------------------------------------------------------


class PageFace(Protocol):
    """页面操作面（执行器消费的最小页面协议——playwright 适配 / 测试 fake）。"""

    @property
    def url(self) -> str:
        """当前 URL（导航终态 / 点击跳转后）。"""
        ...

    async def goto(self, url: str, *, timeout_ms: int) -> None:
        """导航（domcontentloaded 等待 + 毫秒超时）。

        Args:
            url: 目标 URL。
            timeout_ms: 导航超时（毫秒）。
        """
        ...

    async def title(self) -> str:
        """页面标题。"""
        ...

    async def inner_text(self) -> str:
        """页面正文文本（body 内文本——原始形态，零包装）。"""
        ...

    async def click(self, selector: str) -> None:
        """点击元素（选择器等待上限内失败抛异常）。

        Args:
            selector: CSS 选择器。
        """
        ...

    async def fill(self, selector: str, value: str) -> None:
        """填充表单字段。

        Args:
            selector: CSS 选择器。
            value: 填充值。
        """
        ...

    async def content_size(self) -> tuple[int, int]:
        """滚动内容尺寸（宽, 高——整页截图的尺寸面）。"""
        ...

    async def viewport_size(self) -> tuple[int, int]:
        """视口尺寸（宽, 高——视口截图的尺寸面）。"""
        ...

    async def screenshot(self, *, clip_width: int, clip_height: int) -> bytes:
        """截图（左上角起 clip 裁剪——PNG 字节）。

        Args:
            clip_width: 裁剪宽度。
            clip_height: 裁剪高度。
        """
        ...

    async def close(self) -> None:
        """关闭页面。"""
        ...


class ContextFace(Protocol):
    """浏览器上下文操作面（session 消费的最小 context 协议）。"""

    def pages(self) -> list[PageFace]:
        """当前存活页面列表（注册序）。"""
        ...

    async def new_page(self) -> PageFace:
        """新建页面（标签页上限内的开页面）。"""
        ...

    async def close(self) -> None:
        """关闭上下文（profile 会话终止面）。"""
        ...


# ---------------------------------------------------------------------------
# playwright 适配层（真实运行时——部署形态；单测经 fake playwright 对象驱动）
# ---------------------------------------------------------------------------


class PlaywrightPage:
    """playwright Page 适配器（PageFace 的真实实现）。

    Attributes:
        _page: playwright 异步 Page 实例。
    """

    def __init__(self, page: Page) -> None:
        """初始化适配器。

        Args:
            page: playwright 异步 Page 实例。
        """
        self._page = page

    @property
    def url(self) -> str:
        """当前 URL（playwright 同步属性面）。"""
        return self._page.url

    async def goto(self, url: str, *, timeout_ms: int) -> None:
        """导航（domcontentloaded 等待——重定向逐跳由路由拦截强制）。"""
        await self._page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

    async def title(self) -> str:
        """页面标题。"""
        return await self._page.title()

    async def inner_text(self) -> str:
        """页面正文文本（body 元素 inner text——原始形态）。"""
        return await self._page.inner_text("body")

    async def click(self, selector: str) -> None:
        """点击元素（选择器等待上限 10s）。"""
        await self._page.click(selector, timeout=_ACTION_TIMEOUT_MS)

    async def fill(self, selector: str, value: str) -> None:
        """填充表单字段（选择器等待上限 10s）。"""
        await self._page.fill(selector, value, timeout=_ACTION_TIMEOUT_MS)

    async def content_size(self) -> tuple[int, int]:
        """滚动内容尺寸（documentElement scrollWidth / scrollHeight）。"""
        result = await self._page.evaluate(
            "() => [document.documentElement.scrollWidth,"
            " document.documentElement.scrollHeight]"
        )
        return (int(result[0]), int(result[1]))

    async def viewport_size(self) -> tuple[int, int]:
        """视口尺寸（context 创建期固定 1280x720——None 兜底缺省值）。"""
        size = self._page.viewport_size
        if size is None:
            return _DEFAULT_VIEWPORT
        return (int(size["width"]), int(size["height"]))

    async def screenshot(self, *, clip_width: int, clip_height: int) -> bytes:
        """截图（clip 裁剪——左上角起；PNG 字节）。"""
        return await self._page.screenshot(
            full_page=False,
            clip={"x": 0, "y": 0, "width": clip_width, "height": clip_height},
        )

    async def close(self) -> None:
        """关闭页面。"""
        await self._page.close()


class PlaywrightContext:
    """playwright BrowserContext 适配器（ContextFace 的真实实现）。

    创建期装配两道 context 级强制（不依赖工具执行路径——popup / 页面内
    JS 跳转同样受限）：

        - 路由拦截 ``route("**/*")``：子资源与重定向逐跳按
          :func:`browser_sidecar.policy.request_host_allowed` 判定，被拒即
          静默 abort（页面仍渲染允许清单内来源的内容）；
        - 弹出页上限 ``on("page")``：超出标签页上限的弹出页立即关闭
          （异步 close 经当前事件循环调度）。

    Attributes:
        _context: playwright 异步 BrowserContext 实例。
        _allowlist_provider: 允许清单读取注入（逐请求现读）。
    """

    def __init__(
        self,
        context: PlaywrightBrowserContext,
        *,
        allowlist_provider: Callable[[], list[str]],
    ) -> None:
        """初始化适配器（装配弹出页上限；路由拦截经 :meth:`install_route_guard`）。

        Args:
            context: playwright 持久化 context（launch_persistent_context 产物）。
            allowlist_provider: 允许清单读取注入（env 读取——逐请求现读）。
        """
        self._context = context
        self._allowlist_provider = allowlist_provider
        context.on("page", self._on_popup)

    async def install_route_guard(self) -> None:
        """装配路由拦截（playwright async API 的 route 注册为协程——工厂消费）。
        """
        await self._context.route("**/*", self._route_guard)

    def pages(self) -> list[PageFace]:
        """当前存活页面列表（playwright 同步属性面）。"""
        return [PlaywrightPage(page) for page in self._context.pages]

    async def new_page(self) -> PageFace:
        """新建页面。"""
        return PlaywrightPage(await self._context.new_page())

    async def close(self) -> None:
        """关闭上下文。"""
        await self._context.close()

    async def _route_guard(self, route: Route, request: Request) -> None:
        """路由拦截判定（策略纯函数 + 放行 / 静默 abort）。

        Args:
            route: playwright Route（放行 / 中止面）。
            request: 请求（宿主判定面——子资源与重定向逐跳）。
        """
        if policy.request_host_allowed(request.url, self._allowlist_provider()):
            await route.continue_()
        else:
            await route.abort()

    def _on_popup(self, page: Page) -> None:
        """弹出页上限（超出即关——异步 close 经当前事件循环调度）。

        Args:
            page: 新出现的页面（window.open 等弹出形态）。
        """
        if len(self._context.pages) > _MAX_TABS:
            asyncio.get_running_loop().create_task(page.close())


#: 进程级 playwright 句柄（deploy 形态单例——真实浏览器启动面，单测不触达）。
_PLAYWRIGHT_HANDLE: Playwright | None = None


async def _default_chromium() -> BrowserType:
    """惰性启动 playwright 取 chromium 引擎（进程级单例——deploy 形态）。

    Returns:
        chromium BrowserType（launch_persistent_context 工厂面）。
    """
    global _PLAYWRIGHT_HANDLE
    if _PLAYWRIGHT_HANDLE is None:
        _PLAYWRIGHT_HANDLE = await async_playwright().start()
    return _PLAYWRIGHT_HANDLE.chromium


async def launch_profile_context(
    profile: str,
    profiles_dir: str,
    *,
    allowlist_provider: Callable[[], list[str]],
    chromium: BrowserType | None = None,
) -> ContextFace:
    """启动 profile 持久化 context（真实 playwright 面——context-per-profile）。

    Args:
        profile: profile 名（``profiles_dir/<profile>`` 为 user_data_dir
            ——cookies / localStorage 持久化挂载面）。
        profiles_dir: profiles 根目录（容器形态 /app/profiles 卷挂载点）。
        allowlist_provider: 允许清单读取注入（路由拦截消费）。
        chromium: chromium 引擎注入（None = 进程级单例启动——测试注入
            fake chromium 实现单测）。

    Returns:
        装配了路由拦截与弹出页上限的 context 适配器。
    """
    engine = chromium if chromium is not None else await _default_chromium()
    context = await engine.launch_persistent_context(
        user_data_dir=str(Path(profiles_dir) / profile),
        args=list(_CHROMIUM_LAUNCH_ARGS),
        viewport={"width": _DEFAULT_VIEWPORT[0], "height": _DEFAULT_VIEWPORT[1]},
    )
    adapter = PlaywrightContext(context, allowlist_provider=allowlist_provider)
    await adapter.install_route_guard()
    return adapter


# ---------------------------------------------------------------------------
# DNS 解析默认实现（SSRF 链第 ③ 步的 I/O 面——测试注入 resolver 替代）
# ---------------------------------------------------------------------------


async def _addrinfo_ips(host: str, loop: asyncio.AbstractEventLoop) -> list[str]:
    """经事件循环 getaddrinfo 解析主机名（去重保持序）。

    Args:
        host: 主机名（非 IP 字面量——调用方已排除字面量形态）。
        loop: 事件循环（线程池下沉解析——测试注入 fake loop 面）。

    Returns:
        解析 IP 文本列表（空列表 = 解析无记录）。
    """
    infos = await loop.getaddrinfo(host, None)
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in ips:
            ips.append(ip)
    return ips


async def default_resolve_host(host: str) -> list[str]:
    """DNS 解析默认实现（当前事件循环——注入面契约一致）。

    Args:
        host: 主机名。

    Returns:
        解析 IP 文本列表。
    """
    return await _addrinfo_ips(host, asyncio.get_running_loop())


# ---------------------------------------------------------------------------
# 会话（context 内标签簿记——活动页 / 空闲收割 / 上限强制）
# ---------------------------------------------------------------------------


class BrowserSession:
    """profile 会话（context + 标签簿记——工具执行的活动面）。

    簿记语义：

        - **活动页** = 存活页列表的末位（最新开 / 弹出的页）；全部关闭后
          首次使用时自动重开（工具面无「无页面」失败态）；
        - **空闲收割**：每页记录最后使用时刻（epoch 秒，时钟注入）；
          从未被工具触碰的页面（popup）视同空闲——超出空闲阈值的页在
          收割时关闭（全部页被收割合法——active_page 按需重开）；
        - **标签页上限**：工具开页在上限处受控拒绝（弹出页上限由
          PlaywrightContext 的 on("page") 承担）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        context: ContextFace,
        *,
        profile: str,
        clock: Callable[[], float],
        max_tabs: int = _MAX_TABS,
    ) -> None:
        """初始化会话。

        Args:
            context: context 操作面（playwright 适配 / 测试 fake）。
            profile: profile 名（观测面——日志 / 结果携带）。
            clock: 时钟注入（epoch 秒——空闲判定面）。
            max_tabs: 标签页上限（默认 4）。
        """
        self._context = context
        self._profile = profile
        self._clock = clock
        self._max_tabs = max_tabs
        self._last_used: dict[int, float] = {}

    @property
    def profile(self) -> str:
        """profile 名（观测面）。"""
        return self._profile

    def _live_pages(self) -> list[PageFace]:
        """存活页面列表（context 现读）。"""
        return self._context.pages()

    def _touch(self, page: PageFace) -> None:
        """记录页面最后使用时刻（空闲收割判定面）。"""
        self._last_used[id(page)] = self._clock()

    async def active_page(self) -> PageFace:
        """取活动页（存活末位；无存活页时按需重开）。

        Returns:
            活动页面（已 touch）。

        Raises:
            BrowserSidecarError: 重开路径命中标签页上限（理论不可达——
                全部关闭后重开，仅并发弹出竞态下可触达）。
        """
        pages = self._live_pages()
        page = pages[-1] if pages else await self.open_page()
        self._touch(page)
        return page

    async def open_page(self) -> PageFace:
        """新建页面（标签页上限内）。

        Returns:
            新页面（已 touch）。

        Raises:
            BrowserSidecarError: 已达标签页上限（先 close_tab 释放）。
        """
        if len(self._live_pages()) >= self._max_tabs:
            raise BrowserSidecarError(f"标签页已达上限（{self._max_tabs}）——请先 close_tab")
        page = await self._context.new_page()
        self._touch(page)
        return page

    async def list_tabs(self) -> list[dict[str, Any]]:
        """列举标签页（index / url / title——close_tab 消费的 index 面）。

        Returns:
            标签页信息列表（白名单字段——:class:`TabInfoModel` dump）。
        """
        tabs: list[dict[str, Any]] = []
        for index, page in enumerate(self._live_pages()):
            self._touch(page)
            tab = TabInfoModel(
                index=index,
                url=page.url,
                title=await _run_playwright("读取标题", page.title()),
            )
            tabs.append(tab.model_dump())
        return tabs

    async def close_tab(self, index: int) -> None:
        """按索引关闭标签页。

        Args:
            index: 标签页索引（list_tabs 报告的 index 面）。

        Raises:
            BrowserSidecarError: 索引越界（现存活标签页范围外）。
        """
        pages = self._live_pages()
        if index < 0 or index >= len(pages):
            raise BrowserSidecarError(f"标签页索引越界: {index}（现有 {len(pages)} 个）")
        page = pages[index]
        await _run_playwright("关闭标签页", page.close())
        self._last_used.pop(id(page), None)

    async def sweep_idle(self, idle_seconds: float) -> int:
        """收割空闲页面（超出阈值的页关闭——含从未触碰的弹出页）。

        Args:
            idle_seconds: 空闲阈值秒数（默认 600——10 分钟）。

        Returns:
            本次关闭的页面数（观测面）。
        """
        now = self._clock()
        closed = 0
        for page in list(self._live_pages()):
            last_used = self._last_used.get(id(page))
            if last_used is not None and (now - last_used) <= idle_seconds:
                continue
            await _run_playwright("收割空闲标签页", page.close())
            self._last_used.pop(id(page), None)
            closed += 1
        return closed

    async def close(self) -> None:
        """关闭会话（context 终止——manager stop 消费）。"""
        await self._context.close()


# ---------------------------------------------------------------------------
# 管理器（context-per-profile 池——本树的「共享池」+ 空闲收割后台任务）
# ---------------------------------------------------------------------------


class BrowserManager:
    """浏览器上下文管理器（context-per-profile 注册表 + 空闲收割任务）。

    生命周期归 server lifespan（deploy 形态）：``start`` 起后台收割任务，
    ``stop`` 逆序收尾（任务取消 → 会话关闭）。全部外部面注入（context
    工厂 / 时钟 / sleep——测试 hermetic 面）。

    Args（构造期属性文档见 ``__init__`` docstring）:
        无（见 ``__init__``）。
    """

    def __init__(
        self,
        *,
        context_factory: ContextFactory,
        profile: str,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] | None = None,
        idle_close_seconds: float = _IDLE_CLOSE_SECONDS,
        sweep_interval_seconds: float = _SWEEP_INTERVAL_SECONDS,
    ) -> None:
        """初始化管理器。

        Args:
            context_factory: context 工厂注入（profile 名 → 持久化 context；
                None 缺省由调用方注入真实 playwright 工厂或测试 fake）。
            profile: 生效 profile 名（context 注册表键——env 解析产物）。
            clock: 时钟注入（epoch 秒——空闲判定面）。
            sleep: 异步等待注入（收割巡检间隔——None = asyncio.sleep；
                测试注入 fake sleep 实现确定性驱动）。
            idle_close_seconds: 标签页空闲关闭阈值秒数（默认 600）。
            sweep_interval_seconds: 收割巡检间隔秒数（默认 60）。
        """
        self._context_factory = context_factory
        self._profile = profile
        self._clock = clock
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._idle_close_seconds = idle_close_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sessions: dict[str, BrowserSession] = {}
        self._sweeper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动后台收割任务（幂等——已在运行时 no-op）。"""
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name="browser-sidecar-tab-reaper"
            )

    async def stop(self) -> None:
        """停机（收割任务取消 → 会话逆序关闭；幂等）。"""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()

    async def session(self) -> BrowserSession:
        """取生效 profile 的会话（get-or-create——context-per-profile 池）。

        Returns:
            profile 会话（已存在则复用——cookies / localStorage 持久留存）。
        """
        existing = self._sessions.get(self._profile)
        if existing is None:
            context = await self._context_factory(self._profile)
            existing = BrowserSession(
                context, profile=self._profile, clock=self._clock
            )
            self._sessions[self._profile] = existing
        return existing

    async def sweep_idle_once(self) -> int:
        """单次收割巡检（全部会话——测试直调面）。

        Returns:
            关闭的页面总数。
        """
        closed = 0
        for session in self._sessions.values():
            closed += await session.sweep_idle(self._idle_close_seconds)
        return closed

    async def _sweep_loop(self) -> None:
        """收割巡检循环（间隔等待 → 全会话收割；stop 取消退出）。"""
        while True:
            await self._sleep(self._sweep_interval_seconds)
            await self.sweep_idle_once()


# ---------------------------------------------------------------------------
# 工具执行器辅助（playwright 异常归一——执行器对运行时恒不抛未受控异常）
# ---------------------------------------------------------------------------


async def _run_playwright(action: str, operation: Awaitable[_T]) -> _T:
    """playwright 操作包装（异常归一受控失败）。

    Args:
        action: 动作名（错误文案面——如 ``导航`` / ``读取标题``）。
        operation: 待执行的 playwright 异步操作。

    Returns:
        操作结果。

    Raises:
        BrowserSidecarError: playwright 异常（超时 / 选择器缺失 / 网络层
            拒绝等——类型名 + 摘要入受控文案）。
    """
    try:
        return await operation
    except BrowserSidecarError:
        raise
    except Exception as exc:
        raise BrowserSidecarError(
            f"{action}失败: {type(exc).__name__}: {str(exc)[:200]}"
        ) from exc


# ---------------------------------------------------------------------------
# 注册面（七工具——依赖构造注入；执行器恒不抛未受控异常）
# ---------------------------------------------------------------------------


def register_browser_tools(
    runtime: BrowserSidecarRuntime,
    *,
    manager: BrowserManager,
    allowlist_provider: Callable[[], list[str]] | None = None,
    resolver: Resolver | None = None,
    nav_timeout_ms: int | None = None,
) -> None:
    """注册七浏览器工具（依赖构造注入——navigate 策略链在执行器内组装）。

    Args:
        runtime: sidecar 工具运行时（七工具注册目标——同名后写覆盖前写）。
        manager: 浏览器上下文管理器（context-per-profile 池）。
        allowlist_provider: 允许清单读取注入（None = 环境变量
            ``BROWSER_SIDECAR_ALLOWLIST``——空 = 默认拒绝）。
        resolver: DNS 解析注入（None = :func:`default_resolve_host`——
            测试注入 fake resolver 实现 hermetic）。
        nav_timeout_ms: 导航超时毫秒（None = 环境变量
            ``BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS``，缺省 20s）。
    """
    provider = allowlist_provider if allowlist_provider is not None else _allowlist_from_env
    resolve = resolver if resolver is not None else default_resolve_host
    timeout_ms = nav_timeout_ms if nav_timeout_ms is not None else _nav_timeout_ms_from_env()

    async def _execute_navigate(args: dict[str, Any]) -> dict[str, Any]:
        """执行导航（策略链：协议 → 私网拒绝 → DNS 解析 → 允许清单 → 导航）。"""
        url = str(args["url"])
        try:
            if not policy.url_scheme_is_allowed(url):
                raise policy.PolicyViolationError(
                    "URL 协议必须是 http/https（about: / javascript: 等一律拒绝）"
                )
            host = policy.extract_url_host(url)
        except policy.PolicyViolationError as exc:
            raise BrowserSidecarError(exc.message) from None
        if policy.ip_literal_host_is_private(host):
            raise BrowserSidecarError(f"目标主机是私网 IP 字面量（SSRF 红线）: {host}")
        if policy.parse_ip_literal(host) is None:
            ips = await resolve(host)
            if not ips:
                raise BrowserSidecarError(f"域名解析失败: {host}")
            if any(policy.ip_text_is_private(ip) for ip in ips):
                raise BrowserSidecarError(f"域名解析到私网地址（SSRF 红线）: {host}")
        try:
            policy.require_host_allowed(host, provider())
        except policy.PolicyViolationError as exc:
            raise BrowserSidecarError(exc.message) from None
        session = await manager.session()
        page = await session.active_page()
        await _run_playwright("导航", page.goto(url, timeout_ms=timeout_ms))
        return {"url": page.url, "title": await _run_playwright("读取标题", page.title())}

    async def _execute_read_text(args: dict[str, Any]) -> dict[str, Any]:
        """执行正文读取（原始文本零包装——不可信标注由后端消费层承担）。"""
        max_chars = int(args["max_chars"])
        session = await manager.session()
        page = await session.active_page()
        text = await _run_playwright("读取页面文本", page.inner_text())
        truncated = len(text) > max_chars
        return {
            "url": page.url,
            "text": text[:max_chars],
            "truncated": truncated,
            "char_count": min(len(text), max_chars),
        }

    async def _execute_screenshot(args: dict[str, Any]) -> dict[str, Any]:
        """执行截图（视口 / 整页——尺寸上限 clip 裁剪 + 体积上限受控失败）。"""
        full_page = bool(args["full_page"])
        session = await manager.session()
        page = await session.active_page()
        if full_page:
            width, height = await _run_playwright("读取页面尺寸", page.content_size())
        else:
            width, height = await _run_playwright("读取视口尺寸", page.viewport_size())
        clip_width = min(width, _MAX_SCREENSHOT_WIDTH)
        clip_height = min(height, _MAX_SCREENSHOT_HEIGHT)
        image = await _run_playwright(
            "截图", page.screenshot(clip_width=clip_width, clip_height=clip_height)
        )
        if len(image) > _MAX_SCREENSHOT_BYTES:
            raise BrowserSidecarError(
                f"截图体积超上限（{len(image)} > {_MAX_SCREENSHOT_BYTES} 字节）——"
                "请缩小页面尺寸或改用视口截图"
            )
        return {
            "image_base64": base64.b64encode(image).decode("ascii"),
            "width": clip_width,
            "height": clip_height,
            "full_page": full_page,
        }

    async def _execute_click(args: dict[str, Any]) -> dict[str, Any]:
        """执行点击（选择器等待上限内失败转受控失败）。"""
        session = await manager.session()
        page = await session.active_page()
        await _run_playwright("点击", page.click(str(args["selector"])))
        return {"url": page.url, "title": await _run_playwright("读取标题", page.title())}

    async def _execute_fill(args: dict[str, Any]) -> dict[str, Any]:
        """执行填充（空值语义 = 清空字段）。"""
        session = await manager.session()
        page = await session.active_page()
        await _run_playwright("填充", page.fill(str(args["selector"]), str(args["value"])))
        return {"filled": True, "selector": str(args["selector"])}

    async def _execute_list_tabs(args: dict[str, Any]) -> dict[str, Any]:
        """执行标签页列举（index / url / title）。"""
        session = await manager.session()
        tabs = await session.list_tabs()
        return {"count": len(tabs), "tabs": tabs}

    async def _execute_close_tab(args: dict[str, Any]) -> dict[str, Any]:
        """执行标签页关闭（索引越界转受控失败）。"""
        tab_index = int(args["tab_index"])
        session = await manager.session()
        await session.close_tab(tab_index)
        return {"closed_tab_index": tab_index}

    runtime.register(
        BrowserSidecarToolSpec(
            name=NAVIGATE_TOOL_NAME,
            description="导航到目标 URL（策略链：协议→私网拒绝→DNS→允许清单→路由拦截；默认 20s 超时）。",
            args_model=NavigateArgsModel,
        ),
        _execute_navigate,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=READ_TEXT_TOOL_NAME,
            description="读取当前页面正文文本（原始文本零包装；按 max_chars 截断——不可信标注由后端承担）。",
            args_model=ReadTextArgsModel,
        ),
        _execute_read_text,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=SCREENSHOT_TOOL_NAME,
            description="截取当前页面截图（视口或整页；尺寸上限裁剪——返回 base64 PNG + 宽高）。",
            args_model=ScreenshotArgsModel,
        ),
        _execute_screenshot,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=CLICK_TOOL_NAME,
            description="点击页面元素（CSS 选择器）。",
            args_model=ClickArgsModel,
        ),
        _execute_click,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=FILL_TOOL_NAME,
            description="填充表单字段（CSS 选择器 + 值；空值 = 清空）。",
            args_model=FillArgsModel,
        ),
        _execute_fill,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=LIST_TABS_TOOL_NAME,
            description="列举当前标签页（索引 / URL / 标题）。",
            args_model=ListTabsArgsModel,
        ),
        _execute_list_tabs,
    )
    runtime.register(
        BrowserSidecarToolSpec(
            name=CLOSE_TAB_TOOL_NAME,
            description="按索引关闭标签页（标签页上限 4）。",
            args_model=CloseTabArgsModel,
        ),
        _execute_close_tab,
    )


# ---------------------------------------------------------------------------
# 环境变量读取（部署语义——每次调用现读，等价构造期快照）
# ---------------------------------------------------------------------------


def _allowlist_from_env() -> list[str]:
    """从环境变量读取允许清单。

    Returns:
        归一化允许清单（变量缺失 / 空 = 空列表——navigate 拒绝一切）。
    """
    return policy.parse_allowlist(os.environ.get(_ALLOWLIST_ENV, ""))


def _nav_timeout_ms_from_env() -> int:
    """从环境变量读取导航超时（毫秒）。

    Returns:
        导航超时毫秒（缺省 20000）。

    Raises:
        RuntimeError: 值非法（非数值 / 超出 1-120 秒范围——启动期 fail
            fast 面，中文指引）。
    """
    raw = os.environ.get(_NAV_TIMEOUT_ENV, str(_DEFAULT_NAV_TIMEOUT_SECONDS))
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_NAV_TIMEOUT_ENV} 必须是秒数（1-120）") from exc
    if not _NAV_TIMEOUT_MIN_SECONDS <= seconds <= _NAV_TIMEOUT_MAX_SECONDS:
        raise RuntimeError(f"{_NAV_TIMEOUT_ENV} 超出范围（1-120 秒）")
    return int(seconds * 1000)


def _profile_from_env() -> str:
    """从环境变量读取生效 profile 名。

    Returns:
        profile 名（缺省 / 空白 = ``default``——context 注册表键）。
    """
    return os.environ.get(_PROFILE_ENV, "default").strip() or "default"


def _profiles_dir_from_env() -> str:
    """从环境变量读取 profiles 根目录。

    Returns:
        目录路径（缺省 / 空 = ``profiles``——容器形态挂载点 /app/profiles
        由 compose 卷注入）。
    """
    return os.environ.get(_PROFILES_DIR_ENV, "profiles").strip() or "profiles"


def build_default_browser_sidecar() -> tuple[BrowserSidecarRuntime, BrowserManager]:
    """构建生产形态运行时（七工具 + env 驱动管理器——server 工厂消费）。

    Returns:
        ``(runtime, manager)`` 二元组（manager 生命周期归 server lifespan：
        start 起收割任务 / stop 逆序收尾；真实浏览器惰性启动——首个工具
        调用才触达 playwright）。
    """

    async def _factory(profile: str) -> ContextFace:
        """真实 context 工厂（playwright 持久化 context + 路由拦截）。"""
        return await launch_profile_context(
            profile,
            _profiles_dir_from_env(),
            allowlist_provider=_allowlist_from_env,
        )

    manager = BrowserManager(
        context_factory=_factory,
        profile=_profile_from_env(),
        clock=time.time,
    )
    runtime = BrowserSidecarRuntime()
    register_browser_tools(runtime, manager=manager)
    return runtime, manager
