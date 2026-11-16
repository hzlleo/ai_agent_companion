"""app 沙盒工具装配测试——lifespan 接线矩阵（enabled / healthy / token）。

覆盖矩阵（warn-and-continue 语义——沙盒是可选运行时）：

    - disabled（缺省）：零沙盒装配（build_sandbox_client 零调用）；
    - enabled + 探测成功 + token 就绪：http_fetch 注册进 duty 注册表
      （内置工具面之后追加）+ 代理执行器回路（fake 客户端）；
    - enabled + 探测失败：跳过注册（duty 工具面仅内置工具）；
    - enabled + token 未配置：跳过注册（同上）；
    - duty 关闭 + sandbox 开启：零注册表（沙盒消费面是 duty 工具循环）；
    - build_sandbox_client 接线：共享池 http client + settings.tools_sandbox
      配置面透传。

零真实网络：FakeScheduler / FakeSandboxClient 注入（真实装配对象零
调用副作用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.config.models import (
    DatabaseConfig,
    DutySettings,
    Settings,
    ToolsSandboxSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.tools.sandbox_models import HTTP_FETCH_TOOL_NAME
from packages.harness.tools.tool_models import ToolResultModel

#: 内置工具面（装配矩阵断言基准——duty 启用 + 检索服务注入形态）。
_BUILTIN_TOOLS = ["get_current_time", "search_memory"]


def _settings(
    tmp_path: Path,
    *,
    duty_enabled: bool = True,
    tools_sandbox: ToolsSandboxSettings | None = None,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + duty / 沙盒分节）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-sandbox.db")),
        duty=DutySettings(enabled=duty_enabled, daily_at="23:59"),
        tools_sandbox=tools_sandbox if tools_sandbox is not None else ToolsSandboxSettings(),
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

    def register_interval(self, name: str, factory: Any, *, every_seconds: float) -> None:
        """记录间隔注册。"""
        self.registered[name] = {"kind": "interval", "every_seconds": every_seconds}

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


class FakeSandboxClient:
    """fake 沙盒客户端（SandboxClientFace 满足——探测 / token / invoke 可配置）。"""

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
        """fake 执行（记录调用；返回成功终态）。"""
        self.invocations.append((tool, dict(arguments)))
        return ToolResultModel(
            tool=tool,
            ok=True,
            result={"status_code": 200, "body": "ok"},
        )


@pytest.fixture(autouse=True)
def _reset_fake_scheduler_instances() -> None:
    """每测试清理 fake scheduler 类级实例登记。"""
    FakeScheduler.instances = []


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeSandboxClient,
) -> dict[str, Any]:
    """把 app_main.build_sandbox_client 替换为返回 fake 的工厂（捕获接线参数）。"""
    captured: dict[str, Any] = {}

    def _fake_factory(http_client: Any, settings_face: Any) -> FakeSandboxClient:
        """捕获接线参数并返回 fake 客户端。"""
        captured["http_client"] = http_client
        captured["settings_face"] = settings_face
        return fake

    monkeypatch.setattr(app_main, "build_sandbox_client", _fake_factory)
    return captured


class TestWiringMatrix:
    async def test_disabled_skips_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """disabled：零沙盒装配（工厂零调用；工具面仅内置工具）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        factory_called: list[bool] = []

        def _unexpected_factory(http_client: Any, settings_face: Any) -> None:
            """禁用路径不应触达的工厂（断言面）。"""
            factory_called.append(True)

        monkeypatch.setattr(app_main, "build_sandbox_client", _unexpected_factory)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_sandbox=ToolsSandboxSettings(enabled=False),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS
        finally:
            await shutdown_app_state(app)
        assert factory_called == []

    async def test_enabled_and_healthy_registers_http_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + 探测成功 + token 就绪：http_fetch 注册 + 工厂接线参数。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeSandboxClient(has_token=True, healthy=True)
        captured = _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_sandbox=ToolsSandboxSettings(
                enabled=True,
                base_url="http://127.0.0.1:8765",
                token_env="SANDBOX_TEST_TOKEN",
                request_timeout_seconds=6.0,
            ),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS + [HTTP_FETCH_TOOL_NAME]
            # 工厂接线：共享池 http client + settings.tools_sandbox 配置面
            assert captured["settings_face"] is settings.tools_sandbox
            assert captured["http_client"] is not None
            assert fake.probe_calls == 1
            # 代理执行器回路：registry.execute → fake invoke → ok=True
            result = await registry.execute(
                HTTP_FETCH_TOOL_NAME, {"url": "https://example.com/x"}
            )
            assert result.ok is True
            assert result.result == {"status_code": 200, "body": "ok"}
            # 本地默认值物化经 wire：timeout 默认 10
            assert fake.invocations == [
                (
                    HTTP_FETCH_TOOL_NAME,
                    {"url": "https://example.com/x", "timeout_seconds": 10},
                )
            ]
        finally:
            await shutdown_app_state(app)

    async def test_enabled_but_unhealthy_skips_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + 探测失败：跳过注册（duty 工具面仅内置工具——warn-and-continue）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeSandboxClient(healthy=False)
        _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_sandbox=ToolsSandboxSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS
            assert fake.probe_calls == 1
        finally:
            await shutdown_app_state(app)

    async def test_enabled_but_token_missing_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enabled + token 未配置：跳过注册（探测零调用——本地检查先行）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        fake = FakeSandboxClient(has_token=False, healthy=True)
        _install_fake_client(monkeypatch, fake)
        app = create_app()
        settings = _settings(
            tmp_path,
            tools_sandbox=ToolsSandboxSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            registry = app.state.duty_registry
            assert registry is not None
            assert registry.tool_names == _BUILTIN_TOOLS
            assert fake.probe_calls == 0
        finally:
            await shutdown_app_state(app)

    async def test_sandbox_enabled_with_duty_disabled_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """duty 关闭 + sandbox 开启：零注册表（沙盒消费面是 duty 工具循环）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        factory_called: list[bool] = []

        def _unexpected_factory(http_client: Any, settings_face: Any) -> None:
            """无注册表路径不应触达的工厂（断言面）。"""
            factory_called.append(True)

        monkeypatch.setattr(app_main, "build_sandbox_client", _unexpected_factory)
        app = create_app()
        settings = _settings(
            tmp_path,
            duty_enabled=False,
            tools_sandbox=ToolsSandboxSettings(enabled=True),
        )
        await startup_app_state(app, settings, config_dir=tmp_path)
        try:
            assert app.state.duty_registry is None
        finally:
            await shutdown_app_state(app)
        assert factory_called == []
