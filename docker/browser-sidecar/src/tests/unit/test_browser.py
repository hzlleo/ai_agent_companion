"""浏览器运行时测试——face 协议驱动（hermetic：零真实浏览器 / 零网络）。

覆盖矩阵：playwright 适配层（Page / Context 适配 + 路由拦截判定 + 弹出页
上限——经手写 fake playwright 对象驱动）；DNS 解析辅助；BrowserSession
（活动页 / 标签上限 / 列举 / 关闭 / 空闲收割）；BrowserManager（会话
get-or-create + 收割任务生命周期）；register_browser_tools 七工具执行器
（navigate 策略链全矩阵 + read_text 截断 + 截图上限 + click/fill +
list_tabs/close_tab + 参数校验经 wire）；env 读取面；默认构建装配。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from browser_sidecar import browser as browser_module
from browser_sidecar.browser import (
    BrowserManager,
    BrowserSession,
    PlaywrightContext,
    PlaywrightPage,
    build_default_browser_sidecar,
    launch_profile_context,
    register_browser_tools,
)
from browser_sidecar.runtime import BrowserSidecarError, BrowserSidecarRuntime
from browser_sidecar.wire_models import InvokeRequestModel

# ---------------------------------------------------------------------------
# face fakes（ContextFace / PageFace——session 与执行器逻辑的驱动面）
# ---------------------------------------------------------------------------


class FakePage:
    """PageFace fake（内存态页面——goto / 读取 / 截图动作记录）。"""

    def __init__(
        self,
        *,
        url: str = "about:blank",
        title: str = "页面标题",
        text: str = "页面正文",
        content_size: tuple[int, int] = (1280, 720),
        viewport_size: tuple[int, int] = (1280, 720),
        image: bytes = b"fake-png-bytes",
    ) -> None:
        """初始化 fake 页面。

        Args:
            url: 当前 URL。
            title: 页面标题。
            text: 正文文本（inner_text 返回值）。
            content_size: 滚动尺寸（整页截图面）。
            viewport_size: 视口尺寸（视口截图面）。
            image: 截图返回字节。
        """
        self.url = url
        self.title_text = title
        self.text = text
        self._content_size = content_size
        self._viewport_size = viewport_size
        self._image = image
        self.closed = False
        self.goto_calls: list[tuple[str, int]] = []
        self.click_calls: list[str] = []
        self.fill_calls: list[tuple[str, str]] = []
        self.screenshot_clips: list[tuple[int, int]] = []
        self.goto_error: Exception | None = None
        self.click_error: Exception | None = None

    async def goto(self, url: str, *, timeout_ms: int) -> None:
        """记录导航（可选注入失败）。"""
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_calls.append((url, timeout_ms))
        self.url = url

    async def title(self) -> str:
        """返回标题。"""
        return self.title_text

    async def inner_text(self) -> str:
        """返回正文（原始文本——零包装断言面）。"""
        return self.text

    async def click(self, selector: str) -> None:
        """记录点击（可选注入失败）。"""
        if self.click_error is not None:
            raise self.click_error
        self.click_calls.append(selector)

    async def fill(self, selector: str, value: str) -> None:
        """记录填充。"""
        self.fill_calls.append((selector, value))

    async def content_size(self) -> tuple[int, int]:
        """返回滚动尺寸。"""
        return self._content_size

    async def viewport_size(self) -> tuple[int, int]:
        """返回视口尺寸。"""
        return self._viewport_size

    async def screenshot(self, *, clip_width: int, clip_height: int) -> bytes:
        """记录裁剪并返回图片字节。"""
        self.screenshot_clips.append((clip_width, clip_height))
        return self._image

    async def close(self) -> None:
        """关闭（存活标记翻转）。"""
        self.closed = True


class FakeContext:
    """ContextFace fake（内存态页面列表 + 开页 / 关闭记录）。"""

    def __init__(self, pages: list[FakePage] | None = None) -> None:
        """初始化 fake context。

        Args:
            pages: 初始页面列表（默认空）。
        """
        self._pages: list[FakePage] = list(pages or [])
        self.closed = False
        self.profiles_requested: list[str] = []

    def pages(self) -> list[FakePage]:
        """存活页面列表（closed 过滤）。"""
        return [page for page in self._pages if not page.closed]

    async def new_page(self) -> FakePage:
        """新建页面（追加存活列表）。"""
        page = FakePage()
        self._pages.append(page)
        return page

    async def close(self) -> None:
        """关闭 context。"""
        self.closed = True


# ---------------------------------------------------------------------------
# playwright 原生形态 fakes（适配层驱动面——与 playwright.async_api 形状对齐）
# ---------------------------------------------------------------------------


class FakePlaywrightRoute:
    """playwright Route fake（放行 / 中止记录）。"""

    def __init__(self) -> None:
        """初始化（未触达态）。"""
        self.continued = False
        self.aborted = False

    async def continue_(self) -> None:
        """放行记录。"""
        self.continued = True

    async def abort(self) -> None:
        """中止记录。"""
        self.aborted = True


class FakePlaywrightRequest:
    """playwright Request fake（URL 承载）。"""

    def __init__(self, url: str) -> None:
        """初始化。

        Args:
            url: 请求 URL（宿主判定面）。
        """
        self.url = url


class FakePlaywrightPage:
    """playwright Page fake（原生形态——适配层 PlaywrightPage 的驱动面）。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com/page",
        viewport: dict[str, int] | None = None,
        evaluate_result: Any = None,
    ) -> None:
        """初始化。

        Args:
            url: 当前 URL。
            viewport: 视口属性（None 形态覆盖缺省兜底分支）。
            evaluate_result: evaluate 返回值（尺寸断言面）。
        """
        self.url = url
        self.viewport_size = viewport
        self._evaluate_result = evaluate_result
        self.closed = False
        self.goto_calls: list[dict[str, Any]] = []
        self.inner_text_selectors: list[str] = []
        self.click_calls: list[dict[str, Any]] = []
        self.fill_calls: list[dict[str, Any]] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    async def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        """记录导航参数。"""
        self.goto_calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})
        self.url = url

    async def title(self) -> str:
        """返回标题。"""
        return "适配层标题"

    async def inner_text(self, selector: str) -> str:
        """记录选择器并返回文本。"""
        self.inner_text_selectors.append(selector)
        return "适配层正文"

    async def click(self, selector: str, *, timeout: int) -> None:
        """记录点击参数。"""
        self.click_calls.append({"selector": selector, "timeout": timeout})

    async def fill(self, selector: str, value: str, *, timeout: int) -> None:
        """记录填充参数。"""
        self.fill_calls.append({"selector": selector, "value": value, "timeout": timeout})

    async def evaluate(self, script: str) -> Any:
        """返回预设 evaluate 结果。"""
        return self._evaluate_result

    async def screenshot(self, *, full_page: bool, clip: dict[str, int]) -> bytes:
        """记录截图参数并返回字节。"""
        self.screenshot_calls.append({"full_page": full_page, "clip": dict(clip)})
        return b"adapter-png"

    async def close(self) -> None:
        """关闭。"""
        self.closed = True


class FakePlaywrightContext:
    """playwright BrowserContext fake（路由 / 事件注册 + 页面列表）。"""

    def __init__(self, pages: list[FakePlaywrightPage] | None = None) -> None:
        """初始化（空注册态）。"""
        self._pages = list(pages or [])
        self.route_handlers: list[Any] = []
        self.page_handlers: list[Any] = []
        self.closed = False

    @property
    def pages(self) -> list[FakePlaywrightPage]:
        """页面列表（closed 过滤——弹出页上限判定面）。"""
        return [page for page in self._pages if not page.closed]

    async def route(self, pattern: str, handler: Any) -> None:
        """记录路由注册（playwright async API 的 route 为协程——形态对齐）。"""
        self.route_handlers.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        """记录事件注册。"""
        self.page_handlers.append((event, handler))

    async def new_page(self) -> FakePlaywrightPage:
        """新建页面。"""
        page = FakePlaywrightPage()
        self._pages.append(page)
        return page

    async def close(self) -> None:
        """关闭。"""
        self.closed = True


class FakeChromium:
    """playwright BrowserType fake（launch_persistent_context 参数记录）。"""

    def __init__(self, context: FakePlaywrightContext) -> None:
        """初始化。

        Args:
            context: 返回的 fake context。
        """
        self._context = context
        self.launch_calls: list[dict[str, Any]] = []

    async def launch_persistent_context(self, **kwargs: Any) -> FakePlaywrightContext:
        """记录启动参数并返回 fake context。"""
        self.launch_calls.append(dict(kwargs))
        return self._context


# ---------------------------------------------------------------------------
# 装配辅助（runtime + fake manager——执行器测试的公共面）
# ---------------------------------------------------------------------------


def _clock(start: float = 1000.0) -> list[float]:
    """构造可推进的 fake 时钟槽（epoch 秒）。"""
    return [start]


def _build_runtime(
    context: FakeContext,
    *,
    allowlist: tuple[str, ...] = ("example.com",),
    resolved: tuple[str, ...] = ("93.184.216.34",),
    resolver_error: Exception | None = None,
    nav_timeout_ms: int = 5000,
) -> BrowserSidecarRuntime:
    """构造装配好的运行时（七工具 + 全注入面——hermetic）。

    Args:
        context: fake context（会话页面承载）。
        allowlist: 允许清单注入（空 = navigate 拒绝一切）。
        resolved: DNS 解析注入返回值。
        resolver_error: 解析器注入异常（传输失败面）。
        nav_timeout_ms: 导航超时注入。

    Returns:
        已注册七工具的运行时（经 runtime.invoke 驱动）。
    """

    async def _factory(profile: str) -> FakeContext:
        """fake context 工厂（记录 profile）。"""
        context.profiles_requested.append(profile)
        return context

    async def _resolve(host: str) -> list[str]:
        """fake 解析器（固定返回 / 注入异常）。"""
        if resolver_error is not None:
            raise resolver_error
        return list(resolved)

    manager = BrowserManager(
        context_factory=_factory,
        profile="default",
        clock=lambda: 1000.0,
    )
    runtime = BrowserSidecarRuntime()
    register_browser_tools(
        runtime,
        manager=manager,
        allowlist_provider=lambda: list(allowlist),
        resolver=_resolve,
        nav_timeout_ms=nav_timeout_ms,
    )
    return runtime


async def _invoke(runtime: BrowserSidecarRuntime, tool: str, arguments: dict[str, Any]):
    """runtime.invoke 薄封装（InvokeRequestModel 组装）。"""
    return await runtime.invoke(InvokeRequestModel(tool=tool, arguments=arguments))


# ---------------------------------------------------------------------------
# playwright 适配层
# ---------------------------------------------------------------------------


class TestPlaywrightPageAdapter:
    async def test_goto_maps_timeout_and_wait(self) -> None:
        """goto：毫秒超时 + domcontentloaded 等待形态透传。"""
        raw = FakePlaywrightPage()
        page = PlaywrightPage(raw)
        await page.goto("https://example.com/x", timeout_ms=5000)
        assert raw.goto_calls == [
            {"url": "https://example.com/x", "timeout": 5000, "wait_until": "domcontentloaded"}
        ]

    async def test_text_and_title(self) -> None:
        """title / inner_text：body 选择器 + 原文返回。"""
        raw = FakePlaywrightPage()
        page = PlaywrightPage(raw)
        assert await page.title() == "适配层标题"
        assert await page.inner_text() == "适配层正文"
        assert raw.inner_text_selectors == ["body"]

    async def test_click_fill_map_action_timeout(self) -> None:
        """click / fill：动作超时 10s 透传。"""
        raw = FakePlaywrightPage()
        page = PlaywrightPage(raw)
        await page.click("#submit")
        await page.fill("#input", "值")
        assert raw.click_calls == [{"selector": "#submit", "timeout": 10000}]
        assert raw.fill_calls == [{"selector": "#input", "value": "值", "timeout": 10000}]

    async def test_sizes(self) -> None:
        """content_size（evaluate 解析）/ viewport_size（属性 + None 兜底）。"""
        raw = FakePlaywrightPage(evaluate_result=[100, 200])
        page = PlaywrightPage(raw)
        assert await page.content_size() == (100, 200)
        raw_with_viewport = FakePlaywrightPage(viewport={"width": 800, "height": 600})
        assert await PlaywrightPage(raw_with_viewport).viewport_size() == (800, 600)
        raw_without_viewport = FakePlaywrightPage(viewport=None)
        assert await PlaywrightPage(raw_without_viewport).viewport_size() == (1280, 720)

    async def test_screenshot_clip_and_close(self) -> None:
        """screenshot：clip 字典形态 + full_page=False；close 透传。"""
        raw = FakePlaywrightPage()
        page = PlaywrightPage(raw)
        image = await page.screenshot(clip_width=640, clip_height=480)
        assert image == b"adapter-png"
        assert raw.screenshot_calls == [
            {"full_page": False, "clip": {"x": 0, "y": 0, "width": 640, "height": 480}}
        ]
        assert page.url == raw.url
        await page.close()
        assert raw.closed is True


class TestPlaywrightContextAdapter:
    async def test_route_and_page_handlers_registered(self) -> None:
        """创建期装配：route("**/*") + on("page") 双注册。"""
        raw = FakePlaywrightContext()
        context = PlaywrightContext(raw, allowlist_provider=lambda: ["example.com"])
        await context.install_route_guard()
        assert [pattern for pattern, _ in raw.route_handlers] == ["**/*"]
        assert [event for event, _ in raw.page_handlers] == ["page"]

    async def test_route_guard_allows_allowlisted(self) -> None:
        """路由拦截：清单内请求放行。"""
        raw = FakePlaywrightContext()
        context = PlaywrightContext(raw, allowlist_provider=lambda: ["example.com"])
        await context.install_route_guard()
        _, handler = raw.route_handlers[0]
        route = FakePlaywrightRoute()
        await handler(route, FakePlaywrightRequest("https://cdn.example.com/app.js"))
        assert route.continued is True
        assert route.aborted is False

    async def test_route_guard_blocks_unknown_host(self) -> None:
        """路由拦截：清单外宿主中止（跨域 CDN 默认阻断——文档化取舍）。"""
        raw = FakePlaywrightContext()
        context = PlaywrightContext(raw, allowlist_provider=lambda: ["example.com"])
        await context.install_route_guard()
        _, handler = raw.route_handlers[0]
        route = FakePlaywrightRoute()
        await handler(route, FakePlaywrightRequest("https://evil-cdn.net/app.js"))
        assert route.aborted is True
        assert route.continued is False

    async def test_route_guard_blocks_private_literal(self) -> None:
        """路由拦截：私网字面量中止——即使清单显式包含（SSRF 红线优先）。"""
        raw = FakePlaywrightContext()
        context = PlaywrightContext(raw, allowlist_provider=lambda: ["127.0.0.1"])
        await context.install_route_guard()
        _, handler = raw.route_handlers[0]
        route = FakePlaywrightRoute()
        await handler(route, FakePlaywrightRequest("http://127.0.0.1/admin"))
        assert route.aborted is True

    async def test_popup_cap_closes_excess(self) -> None:
        """弹出页上限：第 5 页出现即关（异步 close 调度）。"""
        pages = [FakePlaywrightPage() for _ in range(5)]
        raw = FakePlaywrightContext(pages=pages)
        PlaywrightContext(raw, allowlist_provider=lambda: [])
        _, handler = raw.page_handlers[0]
        handler(pages[4])
        await asyncio.sleep(0)
        assert pages[4].closed is True

    async def test_popup_within_cap_kept(self) -> None:
        """弹出页上限内：不关闭。"""
        pages = [FakePlaywrightPage() for _ in range(3)]
        raw = FakePlaywrightContext(pages=pages)
        PlaywrightContext(raw, allowlist_provider=lambda: [])
        _, handler = raw.page_handlers[0]
        handler(pages[2])
        await asyncio.sleep(0)
        assert pages[2].closed is False

    async def test_pages_wrap_and_close(self) -> None:
        """pages / new_page 包装为适配器；close 透传。"""
        raw = FakePlaywrightContext()
        context = PlaywrightContext(raw, allowlist_provider=lambda: [])
        wrapped = await context.new_page()
        assert isinstance(wrapped, PlaywrightPage)
        pages = context.pages()
        assert len(pages) == 1
        assert pages[0].url == wrapped.url
        await context.close()
        assert raw.closed is True


class TestLaunchProfileContext:
    async def test_launch_wires_user_data_dir_and_args(self) -> None:
        """持久化启动：user_data_dir / chromium 参数 / 视口 + 路由装配。"""
        raw = FakePlaywrightContext()
        chromium = FakeChromium(raw)
        context = await launch_profile_context(
            "work",
            "/tmp/profiles",
            allowlist_provider=lambda: ["example.com"],
            chromium=chromium,
        )
        assert chromium.launch_calls[0]["user_data_dir"] == "/tmp/profiles/work"
        assert chromium.launch_calls[0]["args"] == ["--no-sandbox", "--disable-dev-shm-usage"]
        assert chromium.launch_calls[0]["viewport"] == {"width": 1280, "height": 720}
        assert isinstance(context, PlaywrightContext)
        assert raw.route_handlers != []


class TestAddrinfoHelper:
    async def test_addrinfo_ips_dedup_keeps_order(self) -> None:
        """getaddrinfo 解析：去重保持序（fake loop 驱动）。"""

        class _FakeLoop:
            """fake 事件循环（getaddrinfo 记录 + 固定返回）。"""

            def __init__(self) -> None:
                """初始化（空记录态）。"""
                self.requested: list[tuple[str, Any]] = []

            async def getaddrinfo(self, host: str, port: Any) -> list[tuple[Any, ...]]:
                """返回两组同 IP 记录（去重断言面）。"""
                self.requested.append((host, port))
                return [
                    (2, 1, 6, "", ("93.184.216.34", 0)),
                    (2, 1, 6, "", ("93.184.216.34", 0)),
                    (2, 1, 6, "", ("198.51.100.7", 0)),
                ]

        loop = _FakeLoop()
        ips = await browser_module._addrinfo_ips("example.com", loop)
        assert ips == ["93.184.216.34", "198.51.100.7"]
        assert loop.requested == [("example.com", None)]


# ---------------------------------------------------------------------------
# BrowserSession
# ---------------------------------------------------------------------------


class TestBrowserSession:
    async def test_active_page_picks_latest(self) -> None:
        """活动页 = 存活末位（最新页）。"""
        first = FakePage(url="https://a.example/1")
        second = FakePage(url="https://b.example/2")
        context = FakeContext(pages=[first, second])
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        assert await session.active_page() is second

    async def test_active_page_reopens_when_empty(self) -> None:
        """无存活页：按需重开（工具面无「无页面」失败态）。"""
        context = FakeContext()
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        page = await session.active_page()
        assert page in context.pages()

    async def test_open_page_cap_enforced(self) -> None:
        """开页上限：第 5 页受控拒绝。"""
        context = FakeContext(pages=[FakePage() for _ in range(4)])
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        with pytest.raises(BrowserSidecarError, match="上限"):
            await session.open_page()

    async def test_list_tabs_shape(self) -> None:
        """列举：index / url / title（close_tab 消费的 index 面）。"""
        context = FakeContext(
            pages=[FakePage(url="https://a.example/1", title="甲"), FakePage(url="https://b.example/2")]
        )
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        tabs = await session.list_tabs()
        assert tabs == [
            {"index": 0, "url": "https://a.example/1", "title": "甲"},
            {"index": 1, "url": "https://b.example/2", "title": "页面标题"},
        ]

    async def test_close_tab_valid_and_invalid(self) -> None:
        """关闭：合法索引关闭；越界受控拒绝。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        await session.close_tab(0)
        assert page.closed is True
        with pytest.raises(BrowserSidecarError, match="越界"):
            await session.close_tab(3)

    async def test_sweep_idle_closes_stale_and_untouched(self) -> None:
        """空闲收割：超阈值页 + 从未触碰页关闭；新鲜页保留。"""
        stale = FakePage()
        fresh = FakePage()
        untouched = FakePage()
        context = FakeContext(pages=[stale, fresh, untouched])
        now = [1000.0]
        session = BrowserSession(context, profile="p", clock=lambda: now[0])
        session._touch(stale)
        session._touch(untouched)
        now[0] = 1600.0  # fresh 在此时刻被触碰（100s 前——阈值内）
        session._touch(fresh)
        now[0] = 1700.0  # stale / untouched 已空闲 700s（超 600s 阈值）
        closed = await session.sweep_idle(600.0)
        assert closed == 2
        assert stale.closed is True
        assert untouched.closed is True
        assert fresh.closed is False

    async def test_sweep_idle_keeps_active_within_threshold(self) -> None:
        """空闲阈值内：活动页保留。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        now = [1000.0]
        session = BrowserSession(context, profile="p", clock=lambda: now[0])
        await session.active_page()
        now[0] = 1300.0  # 300s < 600s
        closed = await session.sweep_idle(600.0)
        assert closed == 0
        assert page.closed is False

    async def test_session_close_closes_context(self) -> None:
        """会话关闭：context 终止透传。"""
        context = FakeContext()
        session = BrowserSession(context, profile="p", clock=lambda: 0.0)
        await session.close()
        assert context.closed is True


# ---------------------------------------------------------------------------
# BrowserManager
# ---------------------------------------------------------------------------


class TestBrowserManager:
    async def test_session_get_or_create_singletons(self) -> None:
        """会话 get-or-create：同 profile 复用同一会话（context-per-profile 池）。"""
        context = FakeContext()

        async def _factory(profile: str) -> FakeContext:
            """fake 工厂（记录请求）。"""
            context.profiles_requested.append(profile)
            return context

        manager = BrowserManager(context_factory=_factory, profile="default", clock=lambda: 0.0)
        first = await manager.session()
        second = await manager.session()
        assert first is second
        assert context.profiles_requested == ["default"]

    async def test_sweep_loop_runs_then_stops(self) -> None:
        """收割任务：间隔等待 → 全会话收割 → 取消收尾（fake sleep 确定性驱动）。"""
        context = FakeContext(pages=[FakePage()])

        async def _factory(profile: str) -> FakeContext:
            """fake 工厂。"""
            return context

        sleeps: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            """记录等待；第二次抛取消终止循环。"""
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError()

        manager = BrowserManager(
            context_factory=_factory,
            profile="default",
            clock=lambda: 100000.0,  # 远离 touch 时刻——收割必中
            sleep=_fake_sleep,
            sweep_interval_seconds=60.0,
        )
        await manager.session()  # 建会话（stop 关闭面 + 收割对象）
        await manager.start()
        await asyncio.sleep(0)  # 调度让步（任务跑到取消终止）
        await manager.stop()
        assert sleeps == [60.0, 60.0]
        assert manager._sweeper is None
        assert context.closed is True

    async def test_start_idempotent(self) -> None:
        """start 幂等：重复启动不换任务。"""
        context = FakeContext()

        async def _factory(profile: str) -> FakeContext:
            """fake 工厂。"""
            return context

        manager = BrowserManager(
            context_factory=_factory,
            profile="default",
            clock=lambda: 0.0,
            sleep=_never_sleep,
        )
        await manager.start()
        first = manager._sweeper
        await manager.start()
        assert manager._sweeper is first
        await manager.stop()

    async def test_sweep_idle_once_closes_idle_pages(self) -> None:
        """单次收割巡检：空闲页关闭（测试直调面）。"""
        page = FakePage()
        context = FakeContext(pages=[page])

        async def _factory(profile: str) -> FakeContext:
            """fake 工厂。"""
            return context

        manager = BrowserManager(context_factory=_factory, profile="default", clock=lambda: 1e9)
        await manager.session()
        closed = await manager.sweep_idle_once()
        assert closed == 1
        assert page.closed is True


async def _never_sleep(seconds: float) -> None:
    """永不返回的 fake sleep（幂等测试用——stop 取消收尾）。"""
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# 七工具执行器（经 runtime.invoke——wire 面全链）
# ---------------------------------------------------------------------------


class TestNavigatePolicyChain:
    async def test_navigate_success_full_chain(self) -> None:
        """成功链：DNS 解析 → 允许清单 → goto（超时透传）→ url/title。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is True
        assert response.result == {"url": "https://example.com/x", "title": "页面标题"}
        assert page.goto_calls == [("https://example.com/x", 5000)]

    async def test_navigate_refuses_bad_scheme(self) -> None:
        """协议拒绝：javascript: 在参数校验层拒绝（fail-fast 免浏览器触达）。"""
        context = FakeContext()
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "navigate", {"url": "javascript:alert(1)"})
        assert response.ok is False
        assert "参数校验失败" in (response.error or "")

    async def test_navigate_refuses_private_ip_literal(self) -> None:
        """私网字面量拒绝：SSRF 红线（清单显式包含也不放行——resolver 零调用）。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context, allowlist=("127.0.0.1",))
        response = await _invoke(runtime, "navigate", {"url": "https://127.0.0.1/admin"})
        assert response.ok is False
        assert "私网 IP 字面量" in (response.error or "")
        assert page.goto_calls == []

    async def test_navigate_refuses_private_dns_resolution(self) -> None:
        """DNS 解析私网拒绝：解析到 10.x 即拒（DNS rebinding 对策）。"""
        context = FakeContext()
        runtime = _build_runtime(context, resolved=("10.0.0.5",))
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is False
        assert "私网地址" in (response.error or "")

    async def test_navigate_refuses_empty_allowlist(self) -> None:
        """空清单：navigate 拒绝一切（默认拒绝——零配置零出站）。"""
        context = FakeContext()
        runtime = _build_runtime(context, allowlist=())
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is False
        assert "默认拒绝" in (response.error or "")

    async def test_navigate_refuses_unlisted_host(self) -> None:
        """清单外主机：受控拒绝。"""
        context = FakeContext()
        runtime = _build_runtime(context, allowlist=("example.com",))
        response = await _invoke(runtime, "navigate", {"url": "https://evil.example.org/x"})
        assert response.ok is False
        assert "不在允许清单" in (response.error or "")

    async def test_navigate_refuses_empty_dns_answer(self) -> None:
        """解析无记录：受控拒绝（解析失败面）。"""
        context = FakeContext()
        runtime = _build_runtime(context, resolved=())
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is False
        assert "域名解析失败" in (response.error or "")

    async def test_navigate_public_ip_literal_skips_dns(self) -> None:
        """公网字面量：跳过 DNS（清单显式包含时按字面量精确命中放行）。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context, allowlist=("8.8.8.8",))
        response = await _invoke(runtime, "navigate", {"url": "https://8.8.8.8/x"})
        assert response.ok is True
        assert page.goto_calls == [("https://8.8.8.8/x", 5000)]

    async def test_navigate_playwright_error_wrapped(self) -> None:
        """导航异常归一：playwright 错误转受控失败（恒不抛未受控异常）。"""
        page = FakePage()
        page.goto_error = RuntimeError("net::ERR_FAILED at https://example.com/x")
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is False
        assert "导航失败" in (response.error or "")
        assert "RuntimeError" in (response.error or "")


class TestReadText:
    async def test_read_text_raw_passthrough_no_wrapping(self) -> None:
        """原始文本零包装（不可信标注由后端消费层承担——分侧锚点）。"""
        page = FakePage(text="a" * 100)
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "read_text", {"max_chars": 50})
        assert response.ok is True
        assert response.result["text"] == "a" * 50
        assert response.result["truncated"] is True
        assert response.result["char_count"] == 50
        assert "【" not in response.result["text"]

    async def test_read_text_default_max_chars_materialized(self) -> None:
        """缺省参数物化：max_chars 默认 8000 经 wire 进入执行器。"""
        page = FakePage(text="短文本")
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "read_text", {})
        assert response.ok is True
        assert response.result == {
            "url": "about:blank",
            "text": "短文本",
            "truncated": False,
            "char_count": 3,
        }

    async def test_read_text_bounds_rejected(self) -> None:
        """参数边界：max_chars 超上限拒绝。"""
        context = FakeContext()
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "read_text", {"max_chars": 100001})
        assert response.ok is False
        assert "参数校验失败" in (response.error or "")


class TestScreenshot:
    async def test_screenshot_viewport_form(self) -> None:
        """视口截图：clip = 视口尺寸 + base64 往返。"""
        page = FakePage(viewport_size=(1280, 720), image=b"\x89PNG-fake")
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "screenshot", {})
        assert response.ok is True
        assert base64.b64decode(response.result["image_base64"]) == b"\x89PNG-fake"
        assert response.result["width"] == 1280
        assert response.result["height"] == 720
        assert response.result["full_page"] is False
        assert page.screenshot_clips == [(1280, 720)]

    async def test_screenshot_full_page_capped(self) -> None:
        """整页截图：滚动尺寸 + 尺寸上限裁剪（1920x4096 封顶）。"""
        page = FakePage(content_size=(5000, 9000))
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "screenshot", {"full_page": True})
        assert response.ok is True
        assert response.result["width"] == 1920
        assert response.result["height"] == 4096
        assert response.result["full_page"] is True
        assert page.screenshot_clips == [(1920, 4096)]

    async def test_screenshot_oversize_image_rejected(self) -> None:
        """体积上限：超 6MiB 受控失败（wire 载荷面 bounded）。"""
        page = FakePage(image=b"x" * (6 * 1024 * 1024 + 1))
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "screenshot", {})
        assert response.ok is False
        assert "截图体积超上限" in (response.error or "")


class TestClickFillTabs:
    async def test_click_success(self) -> None:
        """点击成功：选择器透传 + url/title 结果。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "click", {"selector": "#submit"})
        assert response.ok is True
        assert response.result == {"url": "about:blank", "title": "页面标题"}
        assert page.click_calls == ["#submit"]

    async def test_click_error_wrapped(self) -> None:
        """点击异常归一：选择器超时转受控失败。"""
        page = FakePage()
        page.click_error = TimeoutError("waiting for selector")
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "click", {"selector": "#missing"})
        assert response.ok is False
        assert "点击失败" in (response.error or "")
        assert "TimeoutError" in (response.error or "")

    async def test_fill_success_with_empty_value(self) -> None:
        """填充：空值合法（清空字段语义）。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "fill", {"selector": "#input", "value": ""})
        assert response.ok is True
        assert response.result == {"filled": True, "selector": "#input"}
        assert page.fill_calls == [("#input", "")]

    async def test_list_tabs(self) -> None:
        """列举：count + tabs（index / url / title）。"""
        context = FakeContext(pages=[FakePage(url="https://a.example/1", title="甲")])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "list_tabs", {})
        assert response.ok is True
        assert response.result == {
            "count": 1,
            "tabs": [{"index": 0, "url": "https://a.example/1", "title": "甲"}],
        }

    async def test_close_tab_success_and_out_of_range(self) -> None:
        """关闭：合法索引成功；越界受控拒绝。"""
        page = FakePage()
        context = FakeContext(pages=[page])
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "close_tab", {"tab_index": 0})
        assert response.ok is True
        assert response.result == {"closed_tab_index": 0}
        assert page.closed is True
        out_of_range = await _invoke(runtime, "close_tab", {"tab_index": 9})
        assert out_of_range.ok is False
        assert "越界" in (out_of_range.error or "")

    async def test_close_tab_negative_rejected(self) -> None:
        """参数边界：负索引拒绝。"""
        context = FakeContext()
        runtime = _build_runtime(context)
        response = await _invoke(runtime, "close_tab", {"tab_index": -1})
        assert response.ok is False
        assert "参数校验失败" in (response.error or "")


# ---------------------------------------------------------------------------
# 环境变量读取面 + 默认构建
# ---------------------------------------------------------------------------


class TestEnvReaders:
    async def test_allowlist_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """允许清单 env 读取：conftest 空 → 空；覆写 → 归一清单。"""
        assert browser_module._allowlist_from_env() == []
        monkeypatch.setenv("BROWSER_SIDECAR_ALLOWLIST", "Example.COM, cdn.example.org")
        assert browser_module._allowlist_from_env() == ["example.com", "cdn.example.org"]

    async def test_nav_timeout_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """导航超时 env：缺省 20s → 20000ms；覆写 30.5s → 30500ms。"""
        monkeypatch.delenv("BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS", raising=False)
        assert browser_module._nav_timeout_ms_from_env() == 20000
        monkeypatch.setenv("BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS", "30.5")
        assert browser_module._nav_timeout_ms_from_env() == 30500

    async def test_nav_timeout_invalid_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """导航超时非法值：非数值 / 超范围 → RuntimeError（fail fast）。"""
        monkeypatch.setenv("BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS", "abc")
        with pytest.raises(RuntimeError, match="BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS"):
            browser_module._nav_timeout_ms_from_env()
        monkeypatch.setenv("BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS", "300")
        with pytest.raises(RuntimeError, match="范围"):
            browser_module._nav_timeout_ms_from_env()

    async def test_profile_and_dir_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """profile / profiles 目录 env：缺省 default / profiles；覆写生效。"""
        monkeypatch.delenv("BROWSER_SIDECAR_PROFILE", raising=False)
        monkeypatch.delenv("BROWSER_SIDECAR_PROFILES_DIR", raising=False)
        assert browser_module._profile_from_env() == "default"
        assert browser_module._profiles_dir_from_env() == "profiles"
        monkeypatch.setenv("BROWSER_SIDECAR_PROFILE", " work ")
        monkeypatch.setenv("BROWSER_SIDECAR_PROFILES_DIR", "/app/profiles")
        assert browser_module._profile_from_env() == "work"
        assert browser_module._profiles_dir_from_env() == "/app/profiles"
        monkeypatch.setenv("BROWSER_SIDECAR_PROFILE", "   ")
        assert browser_module._profile_from_env() == "default"


class TestBuildDefault:
    async def test_build_default_registers_seven_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认构建：七工具注册 + 管理器 env 驱动（fake context 工厂注入）。"""
        monkeypatch.setenv("BROWSER_SIDECAR_PROFILE", "work")

        async def _fake_launch(
            profile: str,
            profiles_dir: str,
            *,
            allowlist_provider: Any,
            chromium: Any = None,
        ) -> FakeContext:
            """fake 持久化启动（hermetic——零真实浏览器）。"""
            context = FakeContext()
            context.profiles_requested.append(f"{profiles_dir}/{profile}")
            return context

        monkeypatch.setattr(browser_module, "launch_profile_context", _fake_launch)
        runtime, manager = build_default_browser_sidecar()
        assert runtime.tool_names == list(browser_module.BROWSER_TOOL_NAMES)
        session = await manager.session()
        assert session.profile == "work"
        assert session._context.profiles_requested == ["profiles/work"]
        await manager.stop()

    async def test_build_default_env_timeout_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认构建：env 导航超时进入执行器（15s → 15000ms 透传 goto）。"""
        monkeypatch.setenv("BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS", "15")
        page = FakePage()
        context = FakeContext(pages=[page])

        async def _factory(profile: str) -> FakeContext:
            """fake 工厂。"""
            return context

        manager = BrowserManager(context_factory=_factory, profile="default", clock=lambda: 0.0)
        runtime = BrowserSidecarRuntime()
        register_browser_tools(
            runtime,
            manager=manager,
            allowlist_provider=lambda: ["example.com"],
            resolver=_resolve_public,
        )
        response = await _invoke(runtime, "navigate", {"url": "https://example.com/x"})
        assert response.ok is True
        assert page.goto_calls == [("https://example.com/x", 15000)]


async def _resolve_public(host: str) -> list[str]:
    """固定公网解析的 fake resolver（env 超时测试消费）。"""
    return ["93.184.216.34"]
