"""app 搜索装配测试——lifespan 搜索能力启停（enabled / 校验 / 工具面接线）。

覆盖矩阵：

    - 全 disabled（缺省）：零搜索对象构造（service / 工具注册表 / 规格
      槽位全 None）；
    - enabled + 无 provider：启动校验 fail fast（``CONFIG_INVALID``
      清晰中文错误）；
    - enabled + provider + 工具面关：service 装配 + 工具槽位 None；
    - enabled + provider + 工具面开：chat 工具注册表 / 规格列表装配 +
      通道 ChatService 工厂绑定工具面（Recording ChatService 断言）+
      duty 注册表在 look_at 后追加 web_search（链路顺序）；
    - 通道工厂零工具形态：搜索工具面关闭时工厂产出零工具 ChatService；
    - 停机：搜索槽位复位 None（幂等）。

零真实网络：FakeScheduler / FakeRunner 注入（与 test_app_duty_lifespan
同法）；搜索服务零端点调用（装配面只构造）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.chat.service import ChatService
from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    DutySettings,
    Settings,
    TelegramSettings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.search.tool import WEB_SEARCH_TOOL_NAME


class FakeScheduler:
    """fake 中央调度器——记录注册 / 停机请求 / run 生命周期。"""

    def __init__(self, *, tick_seconds: float = 1.0, **kwargs: Any) -> None:
        """初始化（记录构造参数）。"""
        self.tick_seconds = tick_seconds
        self.registered: dict[str, dict[str, Any]] = {}
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False

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


class FakeRunner:
    """fake TelegramChannelRunner（通道装配——真实类零网络构造）。"""

    def __init__(self, **kwargs: Any) -> None:
        """初始化。"""
        self.kwargs = kwargs
        self.stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """fake 停机请求。"""
        self.stop_event.set()

    async def run(self) -> None:
        """fake 长轮询循环。"""
        await self.stop_event.wait()


def _settings(
    tmp_path: Path,
    *,
    duty_enabled: bool = False,
    telegram_enabled: bool = False,
) -> Settings:
    """构造隔离 Settings（tmp SQLite + 可选 duty / telegram 分节）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-search.db")),
        duty=DutySettings(enabled=duty_enabled, daily_at="23:59"),
        channels=ChannelsSettings(
            telegram=TelegramSettings(
                enabled=telegram_enabled,
                bot_token_env="TELEGRAM_TEST_BOT_TOKEN",
                state_dir=str(tmp_path / "channels-state"),
            )
        ),
    )


def _write_config(
    tmp_path: Path,
    *,
    enabled: bool,
    tool_enabled: bool = False,
    with_providers: bool = True,
) -> Path:
    """写入 tmp config.yaml（search 段——启动装配的输入面）。"""
    providers = (
        [
            {
                "name": "bocha-main",
                "kind": "bocha",
                "base_url": "http://bocha.test",
                "api_key_env": "SEARCH_BOCHA_API_KEY",
            },
            {
                "name": "zhipu-fallback",
                "kind": "zhipu",
                "base_url": "http://zhipu.test",
                "api_key_env": "SEARCH_ZHIPU_API_KEY",
            },
        ]
        if with_providers
        else []
    )
    content = {
        "search": {
            "providers": providers,
            "web_search": {
                "enabled": enabled,
                "tool_enabled": tool_enabled,
            },
        }
    }
    config_dir = tmp_path / "search-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(content, allow_unicode=True), encoding="utf-8"
    )
    return config_dir


class TestDisabledByDefault:
    async def test_all_disabled_is_noop(self, tmp_path: Path) -> None:
        """全 disabled：零搜索对象构造 + 停机幂等（槽位恒 None）。"""
        app = create_app()
        settings = _settings(tmp_path)
        config_dir = _write_config(tmp_path, enabled=False)
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            assert app.state.search_service is None
            assert app.state.search_tool_registry is None
            assert app.state.search_tool_specs is None
        finally:
            await shutdown_app_state(app)
        assert app.state.search_service is None

    async def test_missing_search_section_is_noop(self, tmp_path: Path) -> None:
        """config.yaml 无 search 段：合法零配置形态（槽位 None）。"""
        app = create_app()
        settings = _settings(tmp_path)
        config_dir = tmp_path / "empty-config"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("llm:\n  models: []\n", encoding="utf-8")
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            assert app.state.search_service is None
        finally:
            await shutdown_app_state(app)


class TestStartupValidation:
    async def test_enabled_without_providers_fails_fast(self, tmp_path: Path) -> None:
        """启动校验：enabled + 零 provider → CONFIG_INVALID 清晰中文错误。"""
        app = create_app()
        settings = _settings(tmp_path)
        config_dir = _write_config(tmp_path, enabled=True, with_providers=False)
        with pytest.raises(BusinessException) as exc_info:
            await startup_app_state(app, settings, config_dir=config_dir)
        assert exc_info.value.code == ErrorCode.CONFIG_INVALID
        assert "provider" in exc_info.value.message
        await shutdown_app_state(app)


class TestEnabledWiring:
    async def test_enabled_service_without_tool_surface(self, tmp_path: Path) -> None:
        """enabled + 工具面关：service 装配 + 工具槽位 None。"""
        app = create_app()
        settings = _settings(tmp_path)
        config_dir = _write_config(tmp_path, enabled=True, tool_enabled=False)
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            assert app.state.search_service is not None
            assert app.state.search_tool_registry is None
            assert app.state.search_tool_specs is None
        finally:
            await shutdown_app_state(app)

    async def test_tool_surface_wires_chat_factory_and_duty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """工具面开：chat 注册表 / 规格装配 + 通道工厂绑定 + duty 注册表
        在 look_at 后追加 web_search（链路顺序锁定）。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        created: list[dict[str, Any]] = []
        real_chat_service = ChatService

        class RecordingChatService(real_chat_service):
            """Recording ChatService——捕获工厂构造参数。"""

            def __init__(self, **kwargs: Any) -> None:
                """记录构造参数后委托真实构造。"""
                created.append(kwargs)
                super().__init__(**kwargs)

        monkeypatch.setattr(app_main, "ChatService", RecordingChatService)
        app = create_app()
        settings = _settings(tmp_path, duty_enabled=True, telegram_enabled=True)
        config_dir = _write_config(tmp_path, enabled=True, tool_enabled=True)
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            assert app.state.search_service is not None
            chat_registry = app.state.search_tool_registry
            assert chat_registry is not None
            assert chat_registry.tool_names == [WEB_SEARCH_TOOL_NAME]
            assert app.state.search_tool_specs is not None
            assert [spec.name for spec in app.state.search_tool_specs] == [
                WEB_SEARCH_TOOL_NAME
            ]
            # duty 注册表：内置 + look_at + web_search（web_search 最后）
            duty_registry = app.state.duty_registry
            assert duty_registry is not None
            assert duty_registry.tool_names[-1] == WEB_SEARCH_TOOL_NAME
            assert "look_at" in duty_registry.tool_names
            # 通道工厂：绑定工具面（tools + tool_executor 成对注入）
            router = app.state.channels_router
            assert router is not None
            service = router._chat_service_factory()
            assert isinstance(service, real_chat_service)
            assert created, "工厂未产出 ChatService"
            factory_kwargs = created[-1]
            assert factory_kwargs.get("tools") is not None
            assert factory_kwargs.get("tool_executor") is chat_registry
        finally:
            await shutdown_app_state(app)
        assert app.state.search_service is None
        assert app.state.search_tool_registry is None
        assert app.state.search_tool_specs is None

    async def test_chat_factory_zero_tool_when_search_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """通道工厂零工具形态：搜索关闭时工厂产出零工具 ChatService。"""
        monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
        monkeypatch.setattr(app_main, "TelegramChannelRunner", FakeRunner)
        created: list[dict[str, Any]] = []
        real_chat_service = ChatService

        class RecordingChatService(real_chat_service):
            """Recording ChatService——捕获工厂构造参数。"""

            def __init__(self, **kwargs: Any) -> None:
                """记录构造参数后委托真实构造。"""
                created.append(kwargs)
                super().__init__(**kwargs)

        monkeypatch.setattr(app_main, "ChatService", RecordingChatService)
        app = create_app()
        settings = _settings(tmp_path, telegram_enabled=True)
        config_dir = _write_config(tmp_path, enabled=False)
        await startup_app_state(app, settings, config_dir=config_dir)
        try:
            router = app.state.channels_router
            assert router is not None
            router._chat_service_factory()
            assert created
            assert "tools" not in created[-1]
            assert "tool_executor" not in created[-1]
        finally:
            await shutdown_app_state(app)
