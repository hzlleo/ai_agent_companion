"""app 浏览器 sidecar 工具装配测试——lifespan 接线矩阵。

覆盖矩阵（warn-and-continue 语义——sidecar 是可选运行时）：

    - disabled（缺省）：零 sidecar 装配（build_browser_sidecar_client 零调用）；
    - enabled + 探测成功 + token 就绪（搜索关闭）：七工具注册进 duty
      注册表（内置 + 沙盒之后）+ 独立 chat 槽位（browser_tool_registry /
      browser_tool_specs）+ 代理执行器回路（fake 客户端——read_text 不可信
      包裹锚点）；
    - enabled + 探测失败：跳过注册（工具面回落——槽位 None）；
    - enabled + token 未配置：跳过注册（探测零调用——本地检查先行）；
    - duty 关闭 + sidecar 开启：duty 零注册表，但 chat 槽位仍装配
      （浏览器工具的 chat 消费面不依赖 duty——与沙盒的差异点）；
    - 搜索 chat 注册表已建（tool_enabled）：七工具并入搜索注册表
      （search_tool_specs 追加——单一 chat 注册表面），browser 槽位保持
      None。

零真实网络：FakeScheduler / FakeBrowserSidecarClient 注入（真实装配对象
零调用副作用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.config.models import (
    DatabaseConfig,
    DutySettings,
    Settings,
    ToolsBrowserSidecarSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.multimodal.look_at import LOOK_AT_TOOL_NAME
from packages.harness.tools.browser_models import (
    BROWSER_LLM_TOOL_NAMES,
    EVALUATE_TOOL_NAME,
)
from packages.harness.tools.tool_models import ToolResultModel

#: 内置工具面（装配矩阵断言基准——duty 启用 + 检索服务注入形态）。
_BUILTIN_TOOLS = ["get_current_time", "search_memory"]
#: look_at 工具（duty 分支恒注册——不受 sidecar 开关影响）。
_LOOK_AT_TOOLS = [LOOK_AT_TOOL_NAME]


def _settings(
    tmp_path: Path,
    *,
    duty_enabled: bool = True,
    tools_browser_sidecar: ToolsBrowserSidecarSettings | None = None,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + duty / 浏览器 sidecar 分节）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-browser.db")),
        duty=DutySettings(enabled=duty_enabled, daily_at="23:59"),
        tools_browser_sidecar=(
            tools_browser_sidecar if tools_browser_sidecar is not None
            else ToolsBrowserSidecarSettings()
        ),
    )


class FakeScheduler:
    """fake 中央调度器——记录注册 / 停机请求 / run 生命周期。"""

    instances: list[FakeScheduler] = []

    def __init__(self, *, tick_seconds: float = 1.0, **kwargs: Any) -> None:
        """初始化（记录构造参数）。"""
        self.tick_seconds = tick_seconds
        self.kwargs = kwargs
        self.registered: dict[str, dict[str, Any]] = {}
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False
        FakeScheduler.instances.append(self)

    def register_daily(self, name: str, factory: Any, *, daily_at: str) -> None:
        """记录每日注册。"""
        self.registered[name] = {"kind": "daily", "daily_at": daily_at}

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_requested = True
        self.stop_event.set()

    async def run(self) -> None:
        """fake tick 循环（await 停机事件）。"""
        self.ran = True
        await self.stop_event.wait()

    @property
    def job_names(self) -> list[str]:
        """已注册作业名。"""
        return list(self.registered)


class FakeBrowserSidecarClient:
    """fake sidecar 客户端（BrowserSidecarClientFace 满足——探测 / token / invoke 可配置）。"""

    def __init__(self, *, has_token: bool = True, healthy: bool = True) -> None:
        """初始化（配置探测与 token 形态）。"""
        self._has_token = has_token
        self._healthy = healthy
        self.probe_calls = 0
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    @property
    def has_token(self) -> bool:
        """是否持有 token。"""
        return self._has_token

    async def probe_health(self) -> bool:
        """fake 探测（记录调用次数）。"""
        self.probe_calls += 1
        return self._healthy

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ToolResultModel:
        """fake 执行（记录调用；按工具返回结果——read_text 原始文本锚点）。"""
        self.invocations.append((tool, dict(arguments)))
        if tool == "read_text":
            return ToolResultModel(
                tool=tool,
                ok=True,
                result={
                    "url": "https://example.com/page",
                    "text": "原始页面正文",
                    "truncated": False,
                    "char_count": 6,
                },
            )
        return ToolResultModel(
            tool=tool,
            ok=True,
            result={"url": "https://example.com/page", "title": "Example"},
        )


@pytest.fixture(autouse=True)
def _reset_fake_scheduler_instances() -> None:
    """每测试清理 fake scheduler 类级实例登记。"""
    FakeScheduler.instances = []


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeBrowserSidecarClient,
) -> dict[str, Any]:
    """把 app_main.build_browser_sidecar_client 替换为返回 fake 的工厂（捕获接线参数）。"""
    captured: dict[str, Any] = {}

    def _fake_factory(http_client: Any, settings_face: Any) -> FakeBrowserSidecarClient:
        """捕获接线参数并返回 fake 客户端。"""
        captured["http_client"] = http_client
        captured["settings_face"] = settings_face
        return fake

    monkeypatch.setattr(app_main, "build_browser_sidecar_client", _fake_factory)
    return captured


def _write_search_config(tmp_path: Path, *, tool_enabled: bool) -> Path:
    """写入 tmp config.yaml（search 段——chat 注册表并入路径的输入面）。"""
    content = {
        "search": {
            "providers": [
                {
                    "name": "bocha-main",
                    "kind": "bocha",
                    "base_url": "http://bocha.test",
                    "api_key_env": "SEARCH_BOCHA_API_KEY",
                }
            ],
            "web_search": {"enabled": True, "tool_enabled": tool_enabled},
        }
    }
    config_dir = tmp_path / "browser-search-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(content, allow_unicode=True), encoding="utf-8"
    )
    return config_dir


class TestWiringMatrix:
    async def test_disabled_skips_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """disabled：零 sidecar 装配（工厂零调用；工具面 = 内置 + look_at）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        factory_called: list[bool] = []

        def _unexpected_factory(http_client: Any, settings_face: Any) -> None:
            """禁用路径不应触达的工厂（断言面）。"""
            factory_called.append(True)

        monkeypatch.setattr(app_main, "build_browser_sidecar_client", _unexpected_factory)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(enabled=False),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
            assert app.state.browser_tool_registry is None
            assert app.state.browser_tool_specs is None
        finally:
            await shutdown_app_state(app)
        assert factory_called == []

    async def test_enabled_and_healthy_registers_browser_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + 探测成功 + token 就绪：duty 注册（沙盒位后）+ 独立 chat
        槽位 + 工厂接线参数 + 代理回路（read_text 不可信包裹锚点）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeBrowserSidecarClient(has_token=True, healthy=True)
        captured = _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(
                enabled=True,
                base_url="http://127.0.0.1:8766",
                token_env="BROWSER_SIDECAR_TEST_TOKEN",
                request_timeout_seconds=6.0,
            ),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS + [
                *BROWSER_LLM_TOOL_NAMES,
                *_LOOK_AT_TOOLS,
            ]
            # chat 槽位（搜索关闭——独立 browser 槽位形态）
            assert app.state.browser_tool_registry is not None
            assert [spec.name for spec in app.state.browser_tool_specs] == list(
                BROWSER_LLM_TOOL_NAMES
            )
            # 工厂接线：共享池 http client + settings.tools_browser_sidecar 配置面
            assert captured["settings_face"] is settings.tools_browser_sidecar
            assert captured["http_client"] is not None
            assert fake.probe_calls == 1
            # 代理执行器回路：registry.execute → fake invoke → ok=True（navigate）
            result = await registry.execute(
                "navigate", {"url": "https://example.com/x"}
            )
            assert result.ok is True
            assert result.result == {"url": "https://example.com/page", "title": "Example"}
            # read_text 回路：不可信包裹（后端消费层职责锚点）
            result = await registry.execute("read_text", {})
            assert result.ok is True
            assert result.result["text"].startswith("【平台内容·不可信】")
            assert result.result["provenance"] == "untrusted"
            assert fake.invocations == [
                ("navigate", {"url": "https://example.com/x"}),
                ("read_text", {"max_chars": 8000}),
            ]
        finally:
            await shutdown_app_state(app)

    async def test_enabled_but_unhealthy_skips_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + 探测失败：跳过浏览器注册（工具面回落——槽位 None）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeBrowserSidecarClient(healthy=False)
        _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
            assert app.state.browser_sidecar_client is None
            assert app.state.browser_tool_registry is None
            assert fake.probe_calls == 1
        finally:
            await shutdown_app_state(app)

    async def test_enabled_but_token_missing_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + token 未配置：跳过浏览器注册（探测零调用——本地检查先行）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeBrowserSidecarClient(has_token=False, healthy=True)
        _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
            assert fake.probe_calls == 0
        finally:
            await shutdown_app_state(app)

    async def test_duty_disabled_still_builds_chat_slots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """duty 关闭 + sidecar 开启：duty 零注册表，chat 槽位仍装配
        （浏览器工具的 chat 消费面不依赖 duty——与沙盒的差异点）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeBrowserSidecarClient(healthy=True)
        _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            duty_enabled=False,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.duty_registry is None
            assert app.state.browser_tool_registry is not None
            assert [spec.name for spec in app.state.browser_tool_specs] == list(
                BROWSER_LLM_TOOL_NAMES
            )
        finally:
            await shutdown_app_state(app)

    async def test_browser_tools_join_search_chat_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """搜索 chat 注册表已建（tool_enabled）：七工具并入搜索注册表
        （search_tool_specs 追加——单一 chat 注册表面），browser 槽位 None。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeBrowserSidecarClient(healthy=True)
        _install_fake_client(monkeypatch, fake)
        config_dir = _write_search_config(tmp_path, tool_enabled=True)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_browser_sidecar=ToolsBrowserSidecarSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            # 并入形态：搜索槽位承载 web_search + 七浏览器工具
            assert app.state.browser_tool_registry is None
            assert app.state.browser_tool_specs is None
            chat_registry = app.state.search_tool_registry
            assert chat_registry is not None
            assert chat_registry.tool_names == ["web_search", *BROWSER_LLM_TOOL_NAMES]
            assert EVALUATE_TOOL_NAME not in chat_registry.tool_names
            assert [spec.name for spec in app.state.search_tool_specs] == [
                "web_search",
                *BROWSER_LLM_TOOL_NAMES,
            ]
            # duty 注册表（搜索关闭沙盒 + look_at + web_search 追加形态）
            duty_registry = app.state.duty_registry
            assert duty_registry is not None
            assert duty_registry.tool_names == _BUILTIN_TOOLS + [
                *BROWSER_LLM_TOOL_NAMES,
                *_LOOK_AT_TOOLS,
                "web_search",
            ]
            assert EVALUATE_TOOL_NAME not in duty_registry.tool_names
        finally:
            await shutdown_app_state(app)
