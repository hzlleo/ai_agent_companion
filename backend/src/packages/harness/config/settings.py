"""Settings 访问面——全局单例访问 + 测试隔离构造助手。

物理定义位置说明：``Settings`` 聚合器与全部子配置 Model Class 定义在
:mod:`packages.harness.config.models`（models 与 logic 分离约定）；本
模块承载：

    - 全局单例 ``settings`` 的 re-export（调用方 ``from ... import settings``
      的稳定导入面）；
    - :func:`build_isolated_settings`——测试 / 工具场景的隔离构造入口
      （显式值构造，零 .env 文件依赖）。

依赖方向：config 域内部（settings → models），合法。
"""

from __future__ import annotations

from packages.harness.config.models import (
    ChannelsSettings,
    DatabaseConfig,
    DreamScheduleSettings,
    DutySettings,
    LlmEndpointConfig,
    LogConfig,
    PoolsConfig,
    SchedulerSettings,
    SeekDBSettings,
    ServerConfig,
    Settings,
    ToolsSandboxSettings,
    VoiceSettings,
    settings,
)

__all__ = [
    "ServerConfig",
    "DatabaseConfig",
    "LogConfig",
    "PoolsConfig",
    "LlmEndpointConfig",
    "SeekDBSettings",
    "ChannelsSettings",
    "DutySettings",
    "DreamScheduleSettings",
    "SchedulerSettings",
    "ToolsSandboxSettings",
    "VoiceSettings",
    "Settings",
    "settings",
    "build_isolated_settings",
]


def build_isolated_settings(
    *,
    database: DatabaseConfig | None = None,
    log: LogConfig | None = None,
    pools: PoolsConfig | None = None,
    server: ServerConfig | None = None,
    llm: LlmEndpointConfig | None = None,
    seekdb: SeekDBSettings | None = None,
    channels: ChannelsSettings | None = None,
    duty: DutySettings | None = None,
    dream_schedule: DreamScheduleSettings | None = None,
    scheduler: SchedulerSettings | None = None,
    tools_sandbox: ToolsSandboxSettings | None = None,
    voice: VoiceSettings | None = None,
) -> Settings:
    """构造隔离 Settings 实例（测试 / 工具专用）。

    显式传入的子配置原样嵌入（已是模型实例时 pydantic 直接采用，不重读
    env——确定性构造）；未传入的分节使用代码默认值。

    Args:
        database: 数据库配置（None = 代码默认）。
        log: 日志配置（None = 代码默认）。
        pools: 池配置（None = 代码默认）。
        server: 服务配置（None = 代码默认）。
        llm: LLM 默认端点配置（None = 代码默认）。
        seekdb: SeekDB 检索存储配置（None = 代码默认——enabled=False
            的 SQLite 回退形态）。
        channels: 通道域配置（None = 代码默认——telegram 关闭形态；
            通道 lifespan 测试注入 enabled=True 形态）。
        duty: 值班轮配置（None = 代码默认——关闭形态；duty lifespan
            测试注入 enabled=True 形态）。
        dream_schedule: 梦境调度配置（None = 代码默认——关闭形态）。
        scheduler: 中央调度器配置（None = 代码默认——tick 1.0s）。
        tools_sandbox: 工具沙盒配置（None = 代码默认——关闭形态；
            沙盒 lifespan 测试注入 enabled=True 形态）。
        voice: 语音域配置（None = 代码默认——TTS / ASR 关闭形态；
            voice 测试注入 enabled=True 形态）。

    Returns:
        隔离 :class:`Settings` 实例（与全局单例无关联）。
    """
    return Settings(
        server=server if server is not None else ServerConfig(),
        database=database if database is not None else DatabaseConfig(),
        log=log if log is not None else LogConfig(),
        pools=pools if pools is not None else PoolsConfig(),
        llm=llm if llm is not None else LlmEndpointConfig(),
        seekdb=seekdb if seekdb is not None else SeekDBSettings(),
        channels=channels if channels is not None else ChannelsSettings(),
        duty=duty if duty is not None else DutySettings(),
        dream_schedule=dream_schedule if dream_schedule is not None else DreamScheduleSettings(),
        scheduler=scheduler if scheduler is not None else SchedulerSettings(),
        tools_sandbox=tools_sandbox if tools_sandbox is not None else ToolsSandboxSettings(),
        voice=voice if voice is not None else VoiceSettings(),
    )
