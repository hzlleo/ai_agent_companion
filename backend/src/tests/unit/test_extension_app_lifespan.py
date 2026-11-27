"""app 外部工具扩展装配测试——lifespan 消费 extensions_config.json 的接线矩阵。

覆盖矩阵（零真实网络——装配期不发起请求）：

    - 无配置文件：扩展工具零注册（duty 工具面 = 内置 + look_at）；
    - enabled=true + 合法工具：扩展工具进入 duty 注册表；
    - enabled=false：扩展工具零注册；
    - tools=[]：扩展工具零注册；
    - {env:} 缺失：该工具被跳过（warn-and-continue），内置工具面不受影响。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import app.main as app_main
from app.main import create_app, shutdown_app_state, startup_app_state
from packages.harness.config.models import DatabaseConfig, DutySettings, Settings
from packages.harness.config.settings import build_isolated_settings
from packages.harness.multimodal.look_at import LOOK_AT_TOOL_NAME

_BUILTIN_TOOLS = ["get_current_time", "search_memory"]
_LOOK_AT_TOOLS = [LOOK_AT_TOOL_NAME]
_EXTENSION_TOOL = "calendar_query"


class FakeScheduler:
    """fake 中央调度器——记录注册 / 停机请求 / run 生命周期。"""

    def __init__(self, *, tick_seconds: float = 1.0, **kwargs: Any) -> None:
        """初始化（记录构造参数）。"""
        self.tick_seconds = tick_seconds
        self.kwargs = kwargs
        self.registered: dict[str, dict[str, Any]] = {}
        self.stop_event = asyncio.Event()
        self.stop_requested = False
        self.ran = False

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


def _settings(tmp_path: Path) -> Settings:
    """构造隔离 Settings（tmp SQLite + duty 启用）。"""
    return build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=str(tmp_path / "app-extension.db")),
        duty=DutySettings(enabled=True, daily_at="23:59"),
    )


def _write_extensions(
    tmp_path: Path,
    *,
    enabled: bool,
    tools: list[dict[str, Any]] | None = None,
) -> None:
    """写入 extensions_config.json。"""
    payload = {"enabled": enabled, "allow_override_builtin": False, "tools": tools or []}
    (tmp_path / "extensions_config.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _tool(*, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """构造合法扩展工具配置 dict。"""
    return {
        "name": _EXTENSION_TOOL,
        "description": "查询本地日历",
        "parameters": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
            "additionalProperties": False,
        },
        "kind": "http",
        "http": {
            "url": "http://127.0.0.1:8091/calendar/query",
            "method": "POST",
            "headers": headers or {},
            "result_path": "events",
        },
    }


async def test_missing_file_registers_no_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无配置文件：扩展工具零注册。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    app = create_app()
    await startup_app_state(app, _settings(tmp_path), config_dir=tmp_path)
    try:
        registry = app.state.duty_registry
        assert registry is not None
        assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
    finally:
        await shutdown_app_state(app)


async def test_enabled_file_registers_extension_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=true：扩展工具进入 duty 注册表（内置之后）。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    _write_extensions(tmp_path, enabled=True, tools=[_tool()])
    app = create_app()
    await startup_app_state(app, _settings(tmp_path), config_dir=tmp_path)
    try:
        registry = app.state.duty_registry
        assert registry is not None
        assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS + [_EXTENSION_TOOL]
    finally:
        await shutdown_app_state(app)


async def test_disabled_file_registers_no_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=false：扩展工具零注册（零日志噪声的默认态）。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    _write_extensions(tmp_path, enabled=False, tools=[_tool()])
    app = create_app()
    await startup_app_state(app, _settings(tmp_path), config_dir=tmp_path)
    try:
        registry = app.state.duty_registry
        assert registry is not None
        assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
    finally:
        await shutdown_app_state(app)


async def test_empty_tools_registers_no_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tools=[]：扩展工具零注册。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    _write_extensions(tmp_path, enabled=True, tools=[])
    app = create_app()
    await startup_app_state(app, _settings(tmp_path), config_dir=tmp_path)
    try:
        registry = app.state.duty_registry
        assert registry is not None
        assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
    finally:
        await shutdown_app_state(app)


async def test_missing_env_skips_extension_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """{env:} 缺失：该扩展工具被跳过，内置工具面不受影响。"""
    monkeypatch.setattr(app_main, "Scheduler", FakeScheduler)
    monkeypatch.delenv("EXT_LIFESPAN_TOKEN", raising=False)
    _write_extensions(
        tmp_path,
        enabled=True,
        tools=[_tool(headers={"Authorization": "{env:EXT_LIFESPAN_TOKEN}"})],
    )
    app = create_app()
    await startup_app_state(app, _settings(tmp_path), config_dir=tmp_path)
    try:
        registry = app.state.duty_registry
        assert registry is not None
        assert registry.tool_names == _BUILTIN_TOOLS + _LOOK_AT_TOOLS
    finally:
        await shutdown_app_state(app)
