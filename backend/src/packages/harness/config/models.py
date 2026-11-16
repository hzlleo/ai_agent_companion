"""配置树 Model Classes —— 全工程配置的结构化真源（P0-3 交付面）。

分层加载链（优先级从低到高，文档化于 docs/backend/config.md）::

    代码默认值（本模块 Field default）
        ↓
    .env 文件（pydantic-settings 各分节 env_prefix 读取）
        ↓
    进程环境变量（pydantic-settings 语义：env 优先于 .env 文件）
        ↓
    config.yaml / extensions_config.json（结构化配置：llm 模型注册表 /
    角色绑定 / 多模态回退链 / 工具清单——与 env 面不重叠，经 loader 运行时加载）

模块内容（按配置域拆分）：

    - ``ServerConfig``       —— Web 服务（env_prefix=``SERVER_``）
    - ``DatabaseConfig``     —— 数据库连接（env_prefix=``DATABASE_``；MySQL 8
      权威存储 aiomysql，测试 SQLite aiosqlite）
    - ``LogConfig``          —— 日志（env_prefix=``LOG_``；映射 core.LoggingConfig）
    - ``PoolsConfig``        —— 共享池（env_prefix=``POOLS_``；线程池 / HTTP 池 /
      LLM 信号量 / 熔断阈值）
    - ``LlmEndpointConfig``  —— LLM 默认端点 env 面（env_prefix=``LLM_``）
    - ``ChannelsSettings``   —— 通道域分节（``Settings.channels``；telegram
      子模型 env_prefix=``TELEGRAM_``——P3 chunk 1）
    - ``DutySettings``       —— 值班轮分节（``Settings.duty``；env_prefix=
      ``DUTY_``——P3 chunk 2）
    - ``DreamScheduleSettings`` —— 梦境调度分节（``Settings.dream_schedule``；
      env_prefix=``DREAM_SCHEDULE_``——P3 chunk 2）
    - ``SchedulerSettings``  —— 中央调度器分节（``Settings.scheduler``；
      env_prefix=``SCHEDULER_``——P3 chunk 2）
    - ``ToolsSandboxSettings`` —— 工具沙盒分节（``Settings.tools_sandbox``；
      env_prefix=``TOOLS_SANDBOX_``——P3 chunk 3）
    - ``TtsSettings`` / ``AsrSettings`` / ``VoiceSettings`` —— 语音域分节
      （``Settings.voice``；子模型 env_prefix=``VOICE_TTS_`` /
      ``VOICE_ASR_``——P4 chunk 1）
    - ``Settings``           —— 聚合器（全局单例 ``settings``）
    - ``LlmConfig``          —— config.yaml 的 ``llm`` 段（模型注册表 / 角色绑定 /
      多模态回退链）
    - ``MultimodalFallbackConfig`` —— look_at 角色非多模态时的回退链配置
    - ``JsonSchema`` / ``ExtensionTool`` / ``ExtensionsConfig`` —— extensions_config.json
      的运行时表示

依赖方向：config → core（合法）；core 不 import config（chunk 边界）。
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.core.logging_models import LoggingConfig

__all__ = [
    "ServerConfig",
    "DatabaseConfig",
    "LogConfig",
    "PoolsConfig",
    "LlmEndpointConfig",
    "SeekDBSettings",
    "TelegramSettings",
    "ChannelsSettings",
    "DutySettings",
    "DreamScheduleSettings",
    "SchedulerSettings",
    "ToolsSandboxSettings",
    "TtsSettings",
    "AsrSettings",
    "VoiceSettings",
    "Settings",
    "settings",
    "LlmConfig",
    "MultimodalFallbackConfig",
    "JsonSchema",
    "ExtensionTool",
    "ExtensionsConfig",
]

#: 每日时刻 "HH:MM" 合法形态（24 小时制——duty / dream_schedule 共用）。
_DAILY_AT_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _validate_daily_at_shape(value: str) -> str:
    """校验每日时刻形态（``HH:MM`` 24 小时制——strip 后 fullmatch）。

    Args:
        value: 每日时刻字符串。

    Returns:
        校验通过的 strip 后原值。

    Raises:
        ValueError: 形态非法（装配错误面）。
    """
    stripped = value.strip()
    if not _DAILY_AT_RE.fullmatch(stripped):
        raise ValueError(f"daily_at 须为 HH:MM 形态（24 小时制）: {value!r}")
    return stripped


class ServerConfig(BaseSettings):
    """Web 服务配置（env_prefix=``SERVER_``）。

    Attributes:
        host: 服务监听地址（默认仅绑定回环——本机开发安全默认；容器部署
            由编排层显式设置 ``SERVER_HOST=0.0.0.0`` 对外暴露）。
        port: 服务监听端口。
        workers: uvicorn worker 进程数（生产模式）。
        cors_origins: CORS 允许来源列表（admin 前端跨域面）。默认含 Vite
            dev server 两形态（localhost / 127.0.0.1，端口 5173）；空列表
            = CORS 关闭（不挂 CORSMiddleware——同源部署形态）。env 覆写
            形态为 JSON 数组字符串（pydantic-settings 复杂类型 JSON 解析）：
            ``SERVER_CORS_ORIGINS='["http://localhost:5173"]'``。
    """

    host: str = Field(default="127.0.0.1", description="服务监听地址")
    port: int = Field(default=8000, ge=1, le=65535, description="服务监听端口")
    workers: int = Field(default=1, ge=1, description="uvicorn worker 进程数（生产模式）")
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        description="CORS 允许来源（默认 Vite dev server；空列表 = 关闭 CORS）",
    )

    model_config = SettingsConfigDict(
        env_prefix="SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DatabaseConfig(BaseSettings):
    """数据库连接配置（env_prefix=``DATABASE_``）。

    MySQL 8 为权威存储（aiomysql 驱动）；SQLite（aiosqlite）用于测试与
    本地开发。``type`` 切换方言，``name`` 在 sqlite 方言下为库文件路径
    （``:memory:`` 表示内存库）。

    Attributes:
        type: 数据库类型（``mysql`` / ``sqlite``）。
        host: MySQL 主机地址（sqlite 方言忽略）。
        port: MySQL 端口（sqlite 方言忽略）。
        name: MySQL 数据库名；sqlite 方言下为库文件路径（``:memory:`` 内存库）。
        user: MySQL 用户名。
        password: MySQL 密码（SecretStr 存储，repr 不泄露）。
        pool_size: 连接池常驻连接数（MySQL 方言）。
        max_overflow: 连接池溢出上限（MySQL 方言）。
        pool_recycle: 连接回收周期秒数（防 MySQL wait_timeout 断连）。
        pool_pre_ping: 取出连接前是否做一次健康检查。
    """

    type: Literal["mysql", "sqlite"] = Field(
        default="mysql", description="数据库类型：mysql（生产权威）/ sqlite（测试）"
    )
    host: str = Field(default="127.0.0.1", description="MySQL 主机地址")
    port: int = Field(default=3306, ge=1, le=65535, description="MySQL 端口")
    name: str = Field(
        default="ai_agent_companion",
        description="MySQL 数据库名；sqlite 方言下为库文件路径（:memory: 内存库）",
    )
    user: str = Field(default="root", description="MySQL 用户名")
    password: SecretStr = Field(default=SecretStr(""), description="MySQL 密码")
    pool_size: int = Field(default=10, ge=1, description="连接池常驻连接数")
    max_overflow: int = Field(default=20, ge=0, description="连接池溢出上限")
    pool_recycle: int = Field(default=3600, ge=0, description="连接回收周期（秒）")
    pool_pre_ping: bool = Field(default=True, description="取出连接前健康检查")

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def async_sqlalchemy_url(self) -> str:
        """构造异步 SQLAlchemy 连接串（按 ``type`` 分派方言）。

        mysql → ``mysql+aiomysql://user:pwd@host:port/db?charset=utf8mb4``
        （用户名/密码 URL 编码，防特殊字符破坏连接串）；sqlite →
        ``sqlite+aiosqlite:///{name}``（``:memory:`` 内存库）。

        Returns:
            异步 SQLAlchemy 连接字符串。
        """
        if self.type == "sqlite":
            return f"sqlite+aiosqlite:///{self.name}"
        pwd = quote_plus(self.password.get_secret_value())
        return (
            f"mysql+aiomysql://{quote_plus(self.user)}:{pwd}"
            f"@{self.host}:{self.port}/{self.name}?charset=utf8mb4"
        )


class LogConfig(BaseSettings):
    """日志配置（env_prefix=``LOG_``）——core ``setup_logging`` 的 env 面。

    字段与 core 层 ``LoggingConfig`` 契约一一映射（``to_core_config``）；
    core 不 import config（依赖方向 config → core），app 层 lifespan 经
    ``setup_logging_from_settings`` 完成接线。

    Attributes:
        level: 全局日志级别（loguru 值域，校验统一大写）。
        file: 文本日志文件路径（相对路径按进程 CWD 解析）。
        json_file: JSON 日志文件路径（结构化单行 JSON）。
        rotation: 轮转策略（默认每日轮转）。
        retention: 保留时长（默认 30 天）。
        module_levels: 按模块覆盖级别的规格串（空串 = 无覆盖）。
        colorize_console: console sink 是否彩色输出。
    """

    level: str = Field(default="INFO", description="全局日志级别")
    file: str = Field(default="logs/app.log", description="文本日志文件路径")
    json_file: str = Field(default="logs/app.json.log", description="JSON 日志文件路径")
    rotation: str = Field(default="1 day", description="轮转策略（每日轮转）")
    retention: str = Field(default="30 days", description="保留时长（30 天）")
    module_levels: str = Field(default="", description="按模块覆盖级别规格串")
    colorize_console: bool = Field(default=True, description="console sink 彩色输出")

    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def to_core_config(self) -> LoggingConfig:
        """映射为 core 层 ``LoggingConfig``（setup_logging 入参契约）。

        Returns:
            与本配置字段一一映射的 :class:`LoggingConfig` 实例。
        """
        return LoggingConfig(
            level=self.level,
            log_file=self.file,
            log_json_file=self.json_file,
            rotation=self.rotation,
            retention=self.retention,
            module_levels=self.module_levels,
            colorize_console=self.colorize_console,
        )


class PoolsConfig(BaseSettings):
    """共享池配置（env_prefix=``POOLS_``）——pools 域的消费面。

    Attributes:
        thread_pool_size: core 共享线程池大小（同步/CPU 密集操作下沉用）。
        http_max_connections: 共享 HTTP 池最大连接数。
        http_max_keepalive_connections: 共享 HTTP 池 keepalive 连接数。
        http_connect_timeout_seconds: HTTP 连接建立超时（秒）。
        http_timeout_seconds: HTTP 单请求总超时（秒）。
        llm_semaphore_limit: LLM 全局并发信号量上限（条目未配置并发时的回落值）。
        llm_request_timeout_seconds: LLM 单请求超时秒数（P0-4 网关消费）。
        cb_fail_max: 熔断失败阈值——连续失败达此次数后熔断。
        cb_reset_timeout_seconds: 熔断冷却秒数——冷却期满进入半开探测。
    """

    thread_pool_size: int = Field(default=8, ge=1, description="共享线程池大小")
    http_max_connections: int = Field(default=100, ge=1, description="HTTP 池最大连接数")
    http_max_keepalive_connections: int = Field(
        default=20, ge=1, description="HTTP 池 keepalive 连接数"
    )
    http_connect_timeout_seconds: float = Field(
        default=5.0, gt=0, description="HTTP 连接建立超时（秒）"
    )
    http_timeout_seconds: float = Field(default=30.0, gt=0, description="HTTP 单请求总超时（秒）")
    llm_semaphore_limit: int = Field(
        default=5, ge=1, description="LLM 全局并发信号量上限（条目级回落值）"
    )
    llm_request_timeout_seconds: int = Field(
        default=120, ge=1, description="LLM 单请求超时（秒；网关消费）"
    )
    cb_fail_max: int = Field(default=5, ge=1, description="熔断失败阈值（连续失败次数）")
    cb_reset_timeout_seconds: int = Field(default=60, ge=1, description="熔断冷却秒数")

    model_config = SettingsConfigDict(
        env_prefix="POOLS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class LlmEndpointConfig(BaseSettings):
    """LLM 默认端点 env 面（env_prefix=``LLM_``）。

    陪伴智能体的主对话端点为远程 OpenAI 兼容端点；此 env 面承载
    「未配置 config.yaml 模型注册表时的兜底端点」——base_url / api_key /
    model 三键。模型注册表（多端点 + 角色绑定）经 config.yaml ``llm`` 段
    配置（见 :class:`LlmConfig`），与本 env 面互补。

    Attributes:
        base_url: 默认端点 API 基础 URL（如 ``https://api.example.com/v1``）。
        api_key: 默认端点 API Key（SecretStr 存储，repr 不泄露）。
        model: 默认端点模型名（传给 provider 的 model 参数）。
    """

    base_url: str = Field(default="", description="默认端点 API 基础 URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="默认端点 API Key")
    model: str = Field(default="", description="默认端点模型名")

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class SeekDBSettings(BaseSettings):
    """SeekDB 检索存储配置（env_prefix=``SEEKDB_``——P1 chunk 2 检索层）。

    SeekDB（v1.4.0，MySQL 线协议 server 模式 2881 端口）承载记忆检索
    索引表 ``ff_memory_index``（向量 + 全文 + 标量混合检索，见
    docs/backend/memory.md 检索层一节）。

    锁定决策：``enabled=False``（默认）——全工程走 SQLite 回退存储
    （keyword 模式），零配置开发可用、测试 hermetic；``enabled=True``
    时 PoolManager 启动期创建共享 aiomysql 池（SeekDB 不可达 fail
    loud——部署顺序契约：SeekDB 先于 app 就绪）。

    密码经环境变量间接（与 llm 条目 ``api_key_env`` 同模式——密码
    永不落 yaml / 配置树）：本配置只持有变量名 ``password_env``，
    实际密码由存储 / 池装配层从该变量读取。

    Attributes:
        enabled: 是否启用 SeekDB 检索存储（False = SQLite 回退）。
        host: SeekDB 服务地址（server 模式）。
        port: SeekDB MySQL 线协议端口（server / docker 模式 2881）。
        user: 连接用户名。
        password_env: 密码环境变量名（实际密码从该变量读取）。
        database: 连接库名。
        vector_dim: 向量维度（插值进索引表 DDL 的 VECTOR(dim)；须与
            embed 角色模型输出一致——默认 1024 = Qwen3-Embedding-0.6B）。
        pool_min: 连接池常驻连接数（启动期即建立）。
        pool_max: 连接池连接上限（不得小于 pool_min）。
    """

    enabled: bool = Field(
        default=False, description="是否启用 SeekDB 检索存储（False = SQLite 回退）"
    )
    host: str = Field(default="127.0.0.1", description="SeekDB 服务地址")
    port: int = Field(default=2881, ge=1, le=65535, description="SeekDB MySQL 线协议端口")
    user: str = Field(default="root", description="SeekDB 连接用户名")
    password_env: str = Field(
        default="SEEKDB_PASSWORD", min_length=1, description="SeekDB 密码环境变量名"
    )
    database: str = Field(default="test", description="SeekDB 连接库名")
    vector_dim: int = Field(
        default=1024, ge=1, le=65535, description="向量维度（须与 embed 模型输出一致）"
    )
    pool_min: int = Field(default=1, ge=1, description="连接池常驻连接数")
    pool_max: int = Field(default=4, ge=1, description="连接池连接上限")

    model_config = SettingsConfigDict(
        env_prefix="SEEKDB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _verify_pool_bounds(self) -> SeekDBSettings:
        """校验连接池边界（pool_max 不得小于 pool_min）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: pool_max < pool_min（装配错误面）。
        """
        if self.pool_max < self.pool_min:
            raise ValueError(f"pool_max({self.pool_max}) 不得小于 pool_min({self.pool_min})")
        return self


class DutySettings(BaseSettings):
    """值班轮配置（env_prefix=``DUTY_``——P3 chunk 2 duty 域）。

    锁定决策：``enabled=False``（默认）——零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 app lifespan 装配中央调度器并注册 duty 每日作业
    （见 docs/backend/duty.md）。

    Attributes:
        enabled: 是否启用值班轮（False = 不注册 duty 调度作业）。
        daily_at: 每日值班时刻（"HH:MM" 24 小时制——naive 本地时间；
            scheduler 无 cron 语法 / 时区数据库）。
        max_tool_steps: 单轮工具调用步数上限（超出 → 强制最终报告指令；
            1-64）。
        report_to_owner: 值班报告是否投递属主（False = 仅落记忆）。
        charter_text: 值班任务书覆写（空 / None = duty_prompts 正典模板；
            非空整体替换——env 面空字符串归一 None）。
    """

    enabled: bool = Field(default=False, description="是否启用值班轮")
    daily_at: str = Field(default="22:00", description="每日值班时刻（HH:MM）")
    max_tool_steps: int = Field(default=8, ge=1, le=64, description="单轮工具调用步数上限")
    report_to_owner: bool = Field(default=True, description="值班报告是否投递属主")
    charter_text: str | None = Field(default=None, description="值班任务书覆写（None = 正典模板）")

    model_config = SettingsConfigDict(
        env_prefix="DUTY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("charter_text", mode="before")
    @classmethod
    def _empty_charter_text_to_none(cls, value: object) -> object:
        """env 面空字符串 / 纯空白归一为 None（未覆写语义）。

        Args:
            value: 原始值（env 注入时空字符串 = 未设置）。

        Returns:
            空 / 纯空白字符串归一为 None；其余原样返回。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("daily_at")
    @classmethod
    def _validate_daily_at(cls, value: str) -> str:
        """校验每日时刻形态（共享规则见 :func:`_validate_daily_at_shape`）。

        Args:
            value: 每日时刻字符串。

        Returns:
            校验通过的原值。

        Raises:
            ValueError: 形态非法（装配错误面）。
        """
        return _validate_daily_at_shape(value)


class DreamScheduleSettings(BaseSettings):
    """梦境调度配置（env_prefix=``DREAM_SCHEDULE_``——P3 chunk 2）。

    锁定决策：``enabled=False``（默认）——梦境经 admin API 手工触发
    （P1 chunk 3 交付面）；``enabled=True`` 时 app lifespan 在中央调度器
    注册 dream 每日作业（DreamService.run_dream 复用——as-is）。

    Attributes:
        enabled: 是否启用梦境定时调度（False = 仅手工触发面）。
        daily_at: 每日梦境时刻（"HH:MM" 24 小时制——naive 本地时间）。
    """

    enabled: bool = Field(default=False, description="是否启用梦境定时调度")
    daily_at: str = Field(default="03:00", description="每日梦境时刻（HH:MM）")

    model_config = SettingsConfigDict(
        env_prefix="DREAM_SCHEDULE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("daily_at")
    @classmethod
    def _validate_daily_at(cls, value: str) -> str:
        """校验每日时刻形态（共享规则见 :func:`_validate_daily_at_shape`）。

        Args:
            value: 每日时刻字符串。

        Returns:
            校验通过的原值。

        Raises:
            ValueError: 形态非法（装配错误面）。
        """
        return _validate_daily_at_shape(value)


class SchedulerSettings(BaseSettings):
    """中央调度器配置（env_prefix=``SCHEDULER_``——P3 chunk 2）。

    Attributes:
        tick_seconds: 调度器 tick 循环间隔秒（到期判定分辨率下限；
            > 0）。
    """

    tick_seconds: float = Field(default=1.0, gt=0, description="调度器 tick 循环间隔秒")

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ToolsSandboxSettings(BaseSettings):
    """工具沙盒配置（env_prefix=``TOOLS_SANDBOX_``——P3 chunk 3 工具沙盒）。

    锁定决策：``enabled=False``（默认）——沙盒是**可选运行时**：全工程
    无沙盒照常运行（duty 工具面回落到内置工具）；``enabled=True`` 时
    app lifespan 在 duty 装配后经 ``/health`` 探测（bounded 超时）——
    探测失败 / token 未配置 → 跳过沙盒工具注册并告警
    （warn-and-continue，见 docs/backend/tools-sandbox.md）。

    鉴权 token 经环境变量间接（与 llm 条目 ``api_key_env`` / telegram
    ``bot_token_env`` 同模式——**密钥永不落 yaml / 配置树**）：本配置只
    持有变量名 ``token_env``，实际 token 由 tools 域沙盒客户端在构造期
    从该变量读取。

    Attributes:
        enabled: 是否启用工具沙盒（False = 不探测 / 不注册沙盒工具）。
        base_url: 沙盒服务基础 URL（compose 形态 127.0.0.1:8765）。
        token_env: 鉴权 token 环境变量名（实际 token 从该变量读取）。
        request_timeout_seconds: 单次请求超时秒数（invoke / 健康探测
            共用——启动期探测的 bounded 上限）。
    """

    enabled: bool = Field(default=False, description="是否启用工具沙盒")
    base_url: str = Field(
        default="http://127.0.0.1:8765", min_length=1, description="沙盒服务基础 URL"
    )
    token_env: str = Field(
        default="SANDBOX_TOKEN", min_length=1, description="鉴权 token 环境变量名"
    )
    request_timeout_seconds: float = Field(
        default=15.0, gt=0, description="单次请求超时（invoke / 健康探测共用）"
    )

    model_config = SettingsConfigDict(
        env_prefix="TOOLS_SANDBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class TelegramSettings(BaseSettings):
    """Telegram 通道配置（env_prefix=``TELEGRAM_``——P3 chunk 1 通道层）。

    锁定决策：``enabled=False``（默认）——零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 app lifespan 装配 Telegram 通道 runner（长轮询
    后台任务，见 docs/backend/channels.md）。

    bot token 经环境变量间接（与 llm 条目 ``api_key_env`` / SeekDB
    ``password_env`` 同模式——**密钥永不落 yaml / 配置树**）：本配置只
    持有变量名 ``bot_token_env``，实际 token 由 channels 域客户端在
    构造期从该变量读取；``enabled=True`` 且变量缺失 / 为空时启动期
    fail fast（``CHANNEL_TOKEN_MISSING``，中文错误信息）。

    Attributes:
        enabled: 是否启用 Telegram 通道（False = 不装配 runner）。
        bot_token_env: bot token 环境变量名（实际 token 从该变量读取）。
        api_base: Bot API 基础 URL（默认官方端点；可指向自建代理）。
        poll_timeout_seconds: getUpdates 长轮询超时秒数（Bot API 长轮询
            上限 50 秒——请求超时为该值 + 传输裕量）。
        owner_chat_id: 属主会话 ID（Telegram chat id 字符串形态；None =
            未配置。P3 duty 轮值班通知的投递目标，通道层本 chunk 不消费）。
        backoff_base_seconds: 轮询失败退避基数秒（指数退避起点）。
        backoff_max_seconds: 退避封顶秒数（不得小于基数）。
        state_dir: 通道状态目录（offset 持久化 JSON 文件落此；相对路径
            按进程 CWD 解析）。
    """

    enabled: bool = Field(default=False, description="是否启用 Telegram 通道")
    bot_token_env: str = Field(
        default="TELEGRAM_BOT_TOKEN", min_length=1, description="bot token 环境变量名"
    )
    api_base: str = Field(
        default="https://api.telegram.org", min_length=1, description="Bot API 基础 URL"
    )
    poll_timeout_seconds: int = Field(
        default=25, ge=1, le=50, description="getUpdates 长轮询超时秒数（Bot API 上限 50）"
    )
    owner_chat_id: str | None = Field(
        default=None, description="属主会话 ID（duty 通知目标；None = 未配置）"
    )
    backoff_base_seconds: float = Field(default=1.0, gt=0, description="退避基数秒")
    backoff_max_seconds: float = Field(default=60.0, gt=0, description="退避封顶秒数")
    state_dir: str = Field(
        default="data/channels", min_length=1, description="通道状态目录（offset 持久化）"
    )

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("owner_chat_id", mode="before")
    @classmethod
    def _empty_owner_chat_id_to_none(cls, value: object) -> object:
        """env 面空字符串归一为 None（未配置语义）。

        Args:
            value: 原始值（env 注入时空字符串 = 未设置）。

        Returns:
            空字符串归一为 None；其余原样返回。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _verify_backoff_bounds(self) -> TelegramSettings:
        """校验退避边界（backoff_max 不得小于 backoff_base）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: backoff_max_seconds < backoff_base_seconds（装配
                错误面——封顶必须不小于基数）。
        """
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                f"backoff_max_seconds({self.backoff_max_seconds}) 不得小于 "
                f"backoff_base_seconds({self.backoff_base_seconds})"
            )
        return self


class ChannelsSettings(BaseModel):
    """通道域配置分节（``Settings.channels``——多平台通道聚合容器）。

    P3 chunk 1 仅含 telegram 子模型；P4 多通道扩展（B站 / QQ 等）在此
    追加同级子模型（各自独立 env_prefix 的 ``BaseSettings``——本工程
    约定：env 面子配置为 BaseSettings，聚合容器为普通 BaseModel）。

    Attributes:
        telegram: Telegram 通道配置（默认关闭）。
    """

    telegram: TelegramSettings = Field(
        default_factory=TelegramSettings, description="Telegram 通道配置"
    )


class TtsSettings(BaseSettings):
    """TTS 语音合成配置（env_prefix=``VOICE_TTS_``——P4 chunk 1 voice 域）。

    锁定决策：``enabled=False``（默认）——语音是可选能力面：CosyVoice3
    包装服务是部署机上的外部本地进程（权重经 ``models/`` 子模块承载，
    compose 接线属后续部署 chunk），缺省零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 voice 域 API / CLI ``--voice`` 方可消费。

    Attributes:
        enabled: 是否启用 TTS 合成（False = voice API / CLI 拒绝）。
        base_url: CosyVoice 包装服务基础 URL（部署契约见 harness/voice/
            tts_client 模块 docstring——POST /tts → audio/wav）。
        model_entry: 用量记账的模型条目名（usage 行 model_entry 列）。
        request_timeout_seconds: 单次合成请求超时秒数（音频合成耗时
            显著长于普通 API）。
        default_speaker: 缺省发音人（无人格 voice_profile 绑定时的回落
            值——CosyVoice 预置 speaker 名）。
        default_speed: 缺省语速（0.5-2.0；1.0 为自然语速）。
    """

    enabled: bool = Field(default=False, description="是否启用 TTS 语音合成")
    base_url: str = Field(
        default="http://127.0.0.1:9880", min_length=1, description="CosyVoice 包装服务基础 URL"
    )
    model_entry: str = Field(
        default="cosyvoice3", min_length=1, description="用量记账模型条目名"
    )
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, description="单次合成请求超时（秒）"
    )
    default_speaker: str = Field(
        default="中文女", min_length=1, description="缺省发音人（CosyVoice 预置 speaker）"
    )
    default_speed: float = Field(default=1.0, ge=0.5, le=2.0, description="缺省语速（0.5-2.0）")

    model_config = SettingsConfigDict(
        env_prefix="VOICE_TTS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class AsrSettings(BaseSettings):
    """ASR 语音识别配置（env_prefix=``VOICE_ASR_``——P4 chunk 1 voice 域）。

    锁定决策：``enabled=False``（默认）——与 TtsSettings 同可选能力面
    语义（Qwen3-ASR 经 vLLM 工具链以 OpenAI 兼容 audio 端点部署——
    部署契约见 harness/voice/asr_client 模块 docstring）。

    Attributes:
        enabled: 是否启用 ASR 转写（False = voice API 拒绝）。
        base_url: Qwen3-ASR 服务基础 URL（OpenAI 兼容路径由客户端拼接）。
        model_entry: 用量记账的模型条目名（兼作 multipart ``model`` 字段
            值——与部署侧模型 id 同名约定）。
        request_timeout_seconds: 单次转写请求超时秒数（音频上传 + 转写）。
        language: 缺省语言提示（None = 端点自动检测；请求级参数覆盖）。
    """

    enabled: bool = Field(default=False, description="是否启用 ASR 语音识别")
    base_url: str = Field(
        default="http://127.0.0.1:8082", min_length=1, description="Qwen3-ASR 服务基础 URL"
    )
    model_entry: str = Field(
        default="qwen3-asr-0.6b", min_length=1, description="用量记账模型条目名（兼模型 id）"
    )
    request_timeout_seconds: float = Field(
        default=60.0, gt=0, description="单次转写请求超时（秒）"
    )
    language: str | None = Field(
        default=None, description="缺省语言提示（None = 端点自动检测）"
    )

    model_config = SettingsConfigDict(
        env_prefix="VOICE_ASR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("language", mode="before")
    @classmethod
    def _empty_language_to_none(cls, value: object) -> object:
        """env 面空字符串 / 纯空白归一为 None（自动检测语义）。

        Args:
            value: 原始值（env 注入时空字符串 = 未设置）。

        Returns:
            空 / 纯空白字符串归一为 None；其余原样返回。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class VoiceSettings(BaseModel):
    """语音域配置分节（``Settings.voice``——TTS / ASR 聚合容器）。

    与 ``ChannelsSettings`` 同约定：env 面子配置为独立 env_prefix 的
    ``BaseSettings``，聚合容器为普通 BaseModel。

    Attributes:
        tts: TTS 合成配置（默认关闭——CosyVoice3 包装服务）。
        asr: ASR 转写配置（默认关闭——Qwen3-ASR OpenAI 兼容端点）。
    """

    tts: TtsSettings = Field(default_factory=TtsSettings, description="TTS 合成配置")
    asr: AsrSettings = Field(default_factory=AsrSettings, description="ASR 转写配置")


class Settings(BaseSettings):
    """全局配置聚合器——所有 env 面子配置的聚合入口。

    用法::

        from packages.harness.config.models import settings
        settings.server.host
        settings.database.type

    测试经显式值构造隔离实例（``Settings(database=DatabaseConfig(type="sqlite"))``），
    零真实配置依赖；生产使用模块级单例 ``settings``（.env + 进程环境变量）。
    ``env_nested_delimiter="__"`` 允许 ``DATABASE__POOL_SIZE`` 形式的嵌套
    覆盖，同时各子配置自身的 env_prefix 独立生效（如 ``DATABASE_POOL_SIZE``）。

    Attributes:
        server: Web 服务配置。
        database: 数据库连接配置。
        log: 日志配置。
        pools: 共享池配置。
        llm: LLM 默认端点 env 面。
        seekdb: SeekDB 检索存储配置（enabled=False 默认——SQLite 回退）。
        channels: 通道域配置分节（telegram 默认关闭——P3 chunk 1）。
        duty: 值班轮配置（默认关闭——P3 chunk 2）。
        dream_schedule: 梦境定时调度配置（默认关闭——P3 chunk 2）。
        scheduler: 中央调度器配置（tick 间隔——P3 chunk 2）。
        tools_sandbox: 工具沙盒配置（默认关闭——P3 chunk 3；沙盒为可选
            运行时，duty 工具面在沙盒不可达时回落到内置工具）。
        voice: 语音域配置（TTS / ASR 默认关闭——P4 chunk 1；外部本地
            模型服务经 HTTP 契约消费，部署接线属后续 chunk）。
        env: 运行环境标识（local / dev / prod）。
    """

    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    log: LogConfig = LogConfig()
    pools: PoolsConfig = PoolsConfig()
    llm: LlmEndpointConfig = LlmEndpointConfig()
    seekdb: SeekDBSettings = SeekDBSettings()
    channels: ChannelsSettings = ChannelsSettings()
    duty: DutySettings = DutySettings()
    dream_schedule: DreamScheduleSettings = DreamScheduleSettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    tools_sandbox: ToolsSandboxSettings = ToolsSandboxSettings()
    voice: VoiceSettings = VoiceSettings()
    env: str = Field(default="local", description="运行环境标识（local / dev / prod）")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


# 全局单例——app 层 lifespan 与各业务模块共享。
# 测试不消费本单例（fixture 显式构造隔离 Settings，见 tests/conftest.py）。
settings = Settings()


class MultimodalFallbackConfig(BaseModel):
    """多模态回退链配置（config.yaml ``llm.multimodal_fallback`` 段）。

    当 look_at 角色绑定的模型条目不支持多模态输入时，按
    「本地视觉端点 → 远程多模态端点」顺序回退（本地优先——低延迟 /
    零成本；远程兜底——本地不可用或能力不足）。两级均为可选配置，
    至少配置一级才构成有效回退链。

    Attributes:
        local: 本地视觉端点（低延迟辅助模型，如本地部署的 vision 模型）。
        remote: 远程多模态端点（公网 OpenAI 兼容多模态模型，兜底）。
    """

    local: LlmModelEntry | None = Field(
        default=None, description="本地视觉端点（回退链第一级，低延迟）"
    )
    remote: LlmModelEntry | None = Field(
        default=None, description="远程多模态端点（回退链第二级，兜底）"
    )


class LlmConfig(BaseModel):
    """config.yaml ``llm`` 段——模型注册表 + 角色绑定 + 多模态回退链。

    config.yaml 示例::

        llm:
          models:
            - name: "main-chat"
              provider: "openai_compatible"
              base_url: "https://api.example.com/v1"
              api_key_env: "MAIN_CHAT_API_KEY"
              model: "glm-5.3"
              roles: ["chat", "duty"]
              concurrency: 4
          role_bindings:
            chat: "main-chat"
          multimodal_fallback:
            local:
              name: "local-vision"
              base_url: "http://127.0.0.1:8001/v1"
              model: "qwen2.5-vl-7b"
              multimodal: true
            remote:
              name: "remote-vision"
              base_url: "https://api.example.com/v1"
              api_key_env: "REMOTE_VISION_API_KEY"
              model: "glm-4.6v"
              multimodal: true

    Attributes:
        models: LLM 模型条目列表（多 provider OpenAI 兼容注册表）。
        role_bindings: 显式角色绑定（角色 → 条目名；优先于条目自身 roles 声明）。
        multimodal_fallback: look_at 角色非多模态时的回退链配置。
    """

    models: list[LlmModelEntry] = Field(
        default_factory=list, description="LLM 模型条目列表（多端点注册表）"
    )
    role_bindings: dict[str, str] = Field(
        default_factory=dict,
        description="显式角色绑定（角色 → 条目名；优先于条目 roles 声明）",
    )
    multimodal_fallback: MultimodalFallbackConfig = Field(
        default_factory=MultimodalFallbackConfig,
        description="多模态回退链配置（本地视觉 → 远程多模态）",
    )

    @field_validator("role_bindings")
    @classmethod
    def _validate_role_bindings(cls, value: dict[str, str]) -> dict[str, str]:
        """校验角色绑定的角色名合法（chat/duty/dream/look_at/embed）。

        Args:
            value: 原始角色绑定映射。

        Returns:
            通过校验的映射。

        Raises:
            ValueError: 含未知角色名。
        """
        from packages.harness.config.model_entry import ModelRole

        valid_roles = {member.value for member in ModelRole}
        invalid = [role for role in value if role not in valid_roles]
        if invalid:
            raise ValueError(f"未知角色名: {invalid}（合法值: {sorted(valid_roles)}）")
        return value

    @classmethod
    def default_models(cls) -> list[dict[str, Any]]:
        """返回 config.example.yaml 中 ``llm.models`` 的示例条目列表。

        示例数据的单一真相源——根 scripts/generate_examples_yaml.py 调用
        此方法生成 ``config.example.yaml`` 的 ``llm.models`` 段，保证示例
        永不与模型契约漂移。条目为 config.yaml 输入格式的 dict（键名与
        :func:`parse_model_entry` 输入键一致）。

        Returns:
            示例模型条目 dict 列表（主对话 / 梦境 / 本地视觉 / 本地嵌入）。
        """
        return [
            {
                "name": "main-chat",
                "provider": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "MAIN_CHAT_API_KEY",
                "model": "glm-5.3",
                "roles": ["chat", "duty"],
                "concurrency": 4,
            },
            {
                "name": "dream-writer",
                "provider": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "DREAM_WRITER_API_KEY",
                "model": "glm-5.3-mini",
                "roles": ["dream"],
                "concurrency": 2,
            },
            {
                "name": "local-vision",
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:8001/v1",
                "api_key_env": "",
                "model": "qwen2.5-vl-7b",
                "roles": ["look_at"],
                "concurrency": 2,
                "multimodal": True,
            },
            {
                "name": "local-embed",
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:8002/v1",
                "api_key_env": "",
                "model": "bge-m3",
                "roles": ["embed"],
                "concurrency": 4,
            },
        ]

    @classmethod
    def default_role_bindings(cls) -> dict[str, str]:
        """返回示例角色绑定（空映射——条目自身 roles 声明已覆盖）。

        Returns:
            空角色绑定映射（文档性展示用）。
        """
        return {}


class JsonSchema(BaseModel):
    """JSON Schema Model Class——``ExtensionTool.parameters`` 字段类型。

    JSON Schema 字段高度动态（``type`` / ``properties`` / ``required`` /
    ``additionalProperties`` 等），使用 ``extra="allow"`` 容纳全部合法键。

    Attributes:
        type: schema 类型（``"object"`` / ``"string"`` / ``"array"`` 等）。
        properties: 属性定义（``type="object"`` 时使用）。
        required: 必填属性名列表。
        additional_properties: 是否允许额外属性（YAML/JSON 键名
            ``additionalProperties``）。
    """

    model_config = {"extra": "allow", "populate_by_name": True}

    type: str | None = Field(default=None, description="schema 类型")
    properties: dict[str, Any] = Field(default_factory=dict, description="属性定义")
    required: list[str] = Field(default_factory=list, description="必填属性名")
    additional_properties: bool | None = Field(
        default=None, alias="additionalProperties", description="是否允许额外属性"
    )


class ExtensionTool(BaseModel):
    """单个 LLM 工具配置（function calling schema）。

    Attributes:
        name: 工具名称（唯一标识）。
        description: 工具描述（告诉 LLM 何时调用此工具）。
        parameters: 工具参数 schema（JSON Schema 格式）。
    """

    name: str = Field(default="", description="工具名称（唯一标识）")
    description: str = Field(default="", description="工具描述（LLM 调用时机说明）")
    parameters: JsonSchema = Field(
        default_factory=JsonSchema, description="工具参数 schema（JSON Schema）"
    )


class ExtensionsConfig(BaseModel):
    """extensions_config.json 的运行时表示。

    Attributes:
        tools: LLM 工具列表（每个工具定义一个 function calling schema）。
    """

    tools: list[ExtensionTool] = Field(
        default_factory=list, description="LLM 工具列表（function calling schema）"
    )

    @classmethod
    def default_tools(cls) -> list[dict[str, Any]]:
        """返回 extensions_config.example.json 中的示例工具定义列表。

        示例数据的单一真相源——根 scripts/generate_examples_json.py 调用
        此方法生成示例 JSON，保证示例永不与模型契约漂移。

        Returns:
            示例工具定义 dict 列表。
        """
        return [
            {
                "name": "look_at_image",
                "description": (
                    "查看用户发送的图像并描述内容（多模态 look_at 链路的"
                    "工具面示例；实际视觉调用由 look_at 角色模型承担）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "图像文件路径（会话媒体目录内相对路径）",
                        }
                    },
                    "required": ["image_path"],
                    "additionalProperties": False,
                },
            },
        ]
