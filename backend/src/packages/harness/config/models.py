"""配置树 Model Classes —— 全工程配置的结构化真源。

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
      子模型 env_prefix=``TELEGRAM_``；qq 子模型 env_prefix=``QQ_``；
      wecom 子模型 env_prefix=``WECOM_``——企业微信智能机器人长连接）
    - ``DutySettings``       —— 值班轮分节（``Settings.duty``；env_prefix=
      ``DUTY_``——duty 域）
    - ``DreamScheduleSettings`` —— 梦境调度分节（``Settings.dream_schedule``；
      env_prefix=``DREAM_SCHEDULE_``——dream 域）
    - ``SchedulerSettings``  —— 中央调度器分节（``Settings.scheduler``；
      env_prefix=``SCHEDULER_``）
    - ``ToolsSandboxSettings`` —— 工具沙盒分节（``Settings.tools_sandbox``；
      env_prefix=``TOOLS_SANDBOX_``——tools 域）
    - ``ToolsBrowserSidecarSettings`` —— 浏览器 sidecar 分节
      （``Settings.tools_browser_sidecar``；env_prefix=
      ``TOOLS_BROWSER_SIDECAR_``——tools 域）
    - ``TtsSettings`` / ``AsrSettings`` / ``VoiceSettings`` —— 语音域分节
      （``Settings.voice``；子模型 env_prefix=``VOICE_TTS_`` /
      ``VOICE_ASR_``——voice 域）
    - ``WebSearchSettings`` —— 联网搜索运营分节（``Settings.search``；
      env_prefix=``SEARCH_``——search 域开关 / 预算 / 冷却）
    - ``RhythmSettings``    —— 拟人节律分节（``Settings.rhythm``；
      env_prefix=``RHYTHM_``——rhythm 域；物理定义在
      harness/rhythm/rhythm_models（config → rhythm 单向聚合）
    - ``IntentSettings``    —— 意图快径分节（``Settings.intent``；
      env_prefix=``INTENT_``——intent 域；物理定义在
      harness/intent/intent_models（config → intent 单向聚合）
    - ``Settings``           —— 聚合器（全局单例 ``settings``）
    - ``LlmConfig``          —— config.yaml 的 ``llm`` 段（模型注册表 / 角色绑定 /
      多模态回退链）
    - ``SearchProviderEntry`` / ``SearchConfig`` —— config.yaml 的
      ``search`` 段（搜索 provider 注册表 + web_search 子块）
    - ``MultimodalFallbackConfig`` —— look_at 角色非多模态时的回退链配置
    - ``JsonSchema`` / ``ExtensionTool`` / ``ExtensionsConfig`` —— extensions_config.json
      的运行时表示

依赖方向：config → core（合法；core 不反向 import config）。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.harness.chat.flow_models import FlowGateSettings
from packages.harness.chat.persona_rotation_models import GroupPersonaSettings
from packages.harness.config.model_entry import LlmModelEntry, ModelRole
from packages.harness.core.logging_models import LoggingConfig
from packages.harness.intent.intent_models import IntentSettings
from packages.harness.multimodal.sticker_models import StickerSettings
from packages.harness.rhythm.rhythm_models import RhythmSettings
from packages.harness.voice.realtime_models import RealtimeVoiceSettings
from packages.harness.voice.voiceprint_models import VoiceprintSettings

__all__ = [
    "RuntimeEnv",
    "ServerConfig",
    "DatabaseConfig",
    "LogConfig",
    "PoolsConfig",
    "LlmEndpointConfig",
    "SeekDBSettings",
    "TelegramSettings",
    "QqChannelSettings",
    "ChannelsSettings",
    "DutySettings",
    "DreamScheduleSettings",
    "SchedulerSettings",
    "ToolsSandboxSettings",
    "ToolsBrowserSidecarSettings",
    "TtsSettings",
    "AsrSettings",
    "VoiceSettings",
    "RealtimeVoiceSettings",
    "VoiceprintSettings",
    "StickerSettings",
    "WebSearchSettings",
    "Settings",
    "settings",
    "LlmConfig",
    "SearchProviderEntry",
    "SearchConfig",
    "SEARCH_PROVIDER_KINDS",
    "default_search_model_for_kind",
    "MultimodalFallbackConfig",
    "JsonSchema",
    "ExtensionTool",
    "ExtensionsConfig",
]

#: 每日时刻 "HH:MM" 合法形态（24 小时制——duty / dream_schedule 共用）。
_DAILY_AT_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class RuntimeEnv(StrEnum):
    """运行环境标识值域（``ENV``——生产策略判定面）。

    ``ENV=prod`` 表示生产语义：管理面鉴权与 QQ 入站 webhook 鉴权必须
    显式配置（启动期 fail-closed，见
    :func:`packages.harness.config.auth_policy.ensure_auth_policy`）。
    ``local`` / ``dev`` / ``test`` 保持「空 token = 鉴权关闭」的开发形态。
    """

    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


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

    管理面 owner-token 鉴权（审计放行条件——新端点不得扩张无鉴权
    面）：``admin_token_env`` 经环境变量间接（与 telegram ``bot_token_env``
    / 沙盒 ``token_env`` 同模式——**token 本值永不落 yaml / 配置树**），
    本配置只持有变量名；实际 token 由 ``app/middleware/admin_auth``
    鉴权依赖按请求从该变量读取。变量非空 = 管理路由要求
    ``Authorization: Bearer <token>``；为空 = 开发 / 测试形态鉴权关闭。
    **``ENV=prod`` 时启动期 fail-closed**（空 token 拒绝启动，见
    :func:`packages.harness.config.auth_policy.ensure_auth_policy` 与
    docs/backend/README.md 认证一节）。

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
        admin_token_env: 管理面 owner-token 鉴权的环境变量名（实际 token
            从该变量读取；变量为空 = 鉴权关闭——见类 docstring 取舍）。
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
    admin_token_env: str = Field(
        default="ADMIN_AUTH_TOKEN",
        min_length=1,
        description="管理面 owner-token 鉴权环境变量名（空 = 开发/测试鉴权关闭；生产 ENV=prod 强制非空）",
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
        text_format: 自定义文本 sink 格式模板（loguru format 语法；
            空串 = core 默认统一模板）。
        colorize_console: console sink 是否彩色输出。
        diagnose_exceptions: console / 文本 sink 的异常 ``diagnose`` 开关
            （默认 False——traceback 不展示帧局部变量，防异常帧内的
            Bearer 令牌 / API Key 明文入日志；backtrace 不受影响）。
    """

    level: str = Field(default="INFO", description="全局日志级别")
    file: str = Field(default="logs/app.log", description="文本日志文件路径")
    json_file: str = Field(default="logs/app.json.log", description="JSON 日志文件路径")
    rotation: str = Field(default="1 day", description="轮转策略（每日轮转）")
    retention: str = Field(default="30 days", description="保留时长（30 天）")
    module_levels: str = Field(default="", description="按模块覆盖级别规格串")
    text_format: str = Field(
        default="", description="自定义文本格式模板（空串=core 默认统一模板）"
    )
    colorize_console: bool = Field(default=True, description="console sink 彩色输出")
    diagnose_exceptions: bool = Field(
        default=False,
        description="异常 diagnose（traceback 帧变量展示；默认关闭防密钥泄漏）",
    )

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
            text_format=self.text_format or None,
            colorize_console=self.colorize_console,
            diagnose_exceptions=self.diagnose_exceptions,
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
        llm_request_timeout_seconds: LLM 单请求超时秒数（LLM 网关消费）。
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
        top_logprobs: 每次生成位置回传的前 N 候选数（≥1；None = 省略
            该字段——旧端点兼容）。
        prompt_logprobs: 提示词 token logprobs 数（≥1；None = 省略该
            字段；流式请求恒省略——见 llm 域传输客户端约束）。
    """

    base_url: str = Field(default="", description="默认端点 API 基础 URL")
    api_key: SecretStr = Field(default=SecretStr(""), description="默认端点 API Key")
    model: str = Field(default="", description="默认端点模型名")
    top_logprobs: int | None = Field(
        default=20,
        ge=1,
        description="每次生成位置回传的前 N 候选数（≥1；空值 = 省略该字段）",
    )
    prompt_logprobs: int | None = Field(
        default=20,
        ge=1,
        description="提示词 token logprobs 数（≥1；空值 = 省略该字段；流式请求恒省略）",
    )

    @field_validator("top_logprobs", "prompt_logprobs", mode="before")
    @classmethod
    def _blank_logprobs_to_none(cls, value: Any) -> Any:
        """空字符串 env 值归一为 None（``.env`` 中留空即省略该字段）。

        Args:
            value: 原始输入（环境变量为字符串）。

        Returns:
            None（空串 / ``none`` / ``null``）或原值（交由 pydantic 校验）。
        """
        if isinstance(value, str):
            text = value.strip()
            if not text or text.lower() in {"none", "null"}:
                return None
        return value

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class SeekDBSettings(BaseSettings):
    """SeekDB 检索存储配置（env_prefix=``SEEKDB_``——记忆检索层消费）。

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
    """值班轮配置（env_prefix=``DUTY_``——duty 域）。

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
    """梦境调度配置（env_prefix=``DREAM_SCHEDULE_``）。

    锁定决策：``enabled=False``（默认）——梦境经 admin API 手工触发；
    ``enabled=True`` 时 app lifespan 在中央调度器
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
    """中央调度器配置（env_prefix=``SCHEDULER_``）。

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
    """工具沙盒配置（env_prefix=``TOOLS_SANDBOX_``——tools 域沙盒运行时）。

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


class ToolsBrowserSidecarSettings(BaseSettings):
    """浏览器自动化 sidecar 配置（env_prefix=``TOOLS_BROWSER_SIDECAR_``——
    tools 域浏览器运行时）。

    锁定决策：``enabled=False``（默认）——浏览器 sidecar 是**可选运行时**
    （JS 渲染回退面）：全工程无 sidecar 照常运行（duty / chat 工具面回落
    到既有工具集）；``enabled=True`` 时 app lifespan 在搜索装配后经
    ``/health`` 探测（bounded 超时）——探测失败 / token 未配置 → 跳过
    浏览器工具注册并告警（warn-and-continue，与工具沙盒同降级语义，
    见 docs/backend/tools-browser.md）。

    鉴权 token 经环境变量间接（与沙盒 ``SANDBOX_TOKEN`` / llm 条目
    ``api_key_env`` 同模式——**密钥永不落 yaml / 配置树**）：本配置只
    持有变量名 ``token_env``，实际 token 由 tools 域 sidecar 客户端在
    构造期从该变量读取。

    Attributes:
        enabled: 是否启用浏览器 sidecar（False = 不探测 / 不注册浏览器
            工具）。
        base_url: sidecar 服务基础 URL（compose 形态 127.0.0.1:8766）。
        token_env: 鉴权 token 环境变量名（实际 token 从该变量读取）。
        request_timeout_seconds: 单次请求超时秒数（invoke / 健康探测
            共用——浏览器操作长于纯 HTTP 工具，缺省 30s）。
    """

    enabled: bool = Field(default=False, description="是否启用浏览器 sidecar")
    base_url: str = Field(
        default="http://127.0.0.1:8766", min_length=1, description="sidecar 服务基础 URL"
    )
    token_env: str = Field(
        default="BROWSER_SIDECAR_TOKEN", min_length=1, description="鉴权 token 环境变量名"
    )
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, description="单次请求超时（invoke / 健康探测共用）"
    )

    model_config = SettingsConfigDict(
        env_prefix="TOOLS_BROWSER_SIDECAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class TelegramSettings(BaseSettings):
    """Telegram 通道配置（env_prefix=``TELEGRAM_``——channels 域）。

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
            未配置——duty 轮值班通知的投递目标，由 duty 域消费，
            通道层自身不读此值）。
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


class QqChannelSettings(BaseSettings):
    """QQ 通道配置（env_prefix=``QQ_``——channels 域）。

    锁定决策：``enabled=False``（默认）——零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 app lifespan 装配 QQ 通道适配器（NapCat OneBot
    v11 HTTP API 出站 + webhook 推送入站——**无长驻 runner**，与
    telegram 的差异见 docs/backend/channels.md QQ 一节）。

    双 token 环境变量间接（密钥永不落 yaml / 配置树——与 telegram
    ``bot_token_env`` 同模式；QQ 的差异是 token **可选**）：

        - ``access_token_env``——**出站**鉴权（NapCat HTTP API 侧的
          access token；配置时以 ``Authorization: Bearer`` 头携带）；
          NapCat 未开启鉴权是合法部署形态 → 变量为空时照常调用（无
          Authorization 头）；NapCat 开启而本侧未配 → 调用得 401/403 →
          ``CHANNEL_TOKEN_MISSING``（逐消息隔离，不阻断启动）；
        - ``webhook_token_env``——**入站**校验（NapCat 事件上报侧配置
          的 access token；webhook 端点按 ``Authorization: Bearer`` 恒时
          比较）。**空 = 鉴权关闭**（仅适合 NapCat 与后端同机 / 可信
          内网部署）；``ENV=prod`` 且本通道启用时启动期 fail-closed
          （空 token 拒绝启动，见
          :func:`packages.harness.config.auth_policy.ensure_auth_policy`）。

    Attributes:
        enabled: 是否启用 QQ 通道（False = 不装配适配器 / webhook 直传忽略）。
        http_api_base_url: NapCat HTTP API 基础 URL（单一动作对象包裹
            POST 的目标——见 harness/channels/qq/qq_client）。
        access_token_env: 出站 access token 环境变量名（实际 token 从该
            变量读取；空 = NapCat 未开启鉴权形态）。
        webhook_token_env: 入站 webhook 校验 token 环境变量名（实际
            token 从该变量读取；空 = 开发/测试入站鉴权关闭——生产
            ``ENV=prod`` 且通道启用时强制非空）。
        request_timeout_seconds: 单次动作请求超时秒数。
    """

    enabled: bool = Field(default=False, description="是否启用 QQ 通道")
    http_api_base_url: str = Field(
        default="http://127.0.0.1:3000", min_length=1, description="NapCat HTTP API 基础 URL"
    )
    access_token_env: str = Field(
        default="QQ_ACCESS_TOKEN", min_length=1, description="出站 access token 环境变量名"
    )
    webhook_token_env: str = Field(
        default="QQ_WEBHOOK_TOKEN",
        min_length=1,
        description="入站 webhook 校验 token 环境变量名（生产环境通道启用时强制非空）",
    )
    request_timeout_seconds: float = Field(default=15.0, gt=0, description="单次动作请求超时（秒）")

    model_config = SettingsConfigDict(
        env_prefix="QQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class WecomChannelSettings(BaseSettings):
    """企业微信「智能机器人」长连接通道配置（env_prefix=``WECOM_``——channels 域）。

    锁定决策：``enabled=False``（默认）——零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 app lifespan 装配长连接 runner（WebSocket 后台任务，
    见 docs/backend/channels.md「企业微信智能机器人长连接」）。

    凭据最小集与协议约束（官方文档《智能机器人长连接》）：长连接模式的订阅
    鉴权只需 **BotID + Secret**，素材上传与消息收发都在同一连接内完成——因此
    ``corp_id`` / ``agent_id`` / access_token 三件套**不引入**（经典自建应用的
    凭据面在长连接模式无消费点，配置树上出现即为死配置）。

    Secret 经环境变量间接（与 telegram ``bot_token_env`` / QQ
    ``access_token_env`` 同模式——**密钥永不落 yaml / 配置树**）：本配置只持有
    变量名 ``secret_env``，实际密钥由长连接客户端在构造期从该变量读取；
    ``enabled=True`` 且变量缺失 / 为空时装配期拒绝该通道（``CHANNEL_TOKEN_MISSING``）。

    Attributes:
        enabled: 是否启用企微长连接通道（False = 零连接 / 零后台任务）。
        bot_id: 智能机器人 BotID（管理后台创建机器人后「获取凭证」取得；
            enabled=True 时必填）。
        secret_env: 长连接专用密钥的环境变量名（实际密钥从该变量读取；与
            回调模式的 Token / EncodingAESKey 不是同一凭据）。
        ws_endpoint: 长连接 WebSocket 端点（官方为
            ``wss://openws.work.weixin.qq.com``——可指向代理）。
        request_timeout_seconds: 单条命令的应答超时秒数。
        heartbeat_interval_seconds: 应用层心跳间隔秒数（官方建议 30）。
        heartbeat_timeout_seconds: 心跳应答超时秒数（超时即判定连接失效并重连）。
        open_timeout_seconds: WebSocket 握手超时秒数。
        backoff_base_seconds: 重连退避基数秒（指数退避起点）。
        backoff_max_seconds: 重连退避封顶秒数（不得小于基数）。
        backoff_jitter_ratio: 退避抖动幅度比例（0-1；0 = 无抖动）。
        reply_window_seconds: 消息回调的回复有效期秒数（官方文档：24 小时内
            可回复该会话——超期出站降级为主动推送）。
    """

    enabled: bool = Field(default=False, description="是否启用企业微信智能机器人长连接通道")
    bot_id: str = Field(
        default="", description="智能机器人 BotID（enabled=true 时必填）"
    )
    secret_env: str = Field(
        default="WECOM_SECRET", min_length=1, description="长连接专用密钥的环境变量名"
    )
    ws_endpoint: str = Field(
        default="wss://openws.work.weixin.qq.com",
        min_length=1,
        description="长连接 WebSocket 端点",
    )
    request_timeout_seconds: float = Field(
        default=15.0, gt=0, description="单条命令应答超时（秒）"
    )
    heartbeat_interval_seconds: float = Field(
        default=30.0, gt=0, description="应用层心跳间隔（秒——官方建议 30）"
    )
    heartbeat_timeout_seconds: float = Field(
        default=10.0, gt=0, description="心跳应答超时（秒——超时判定连接失效）"
    )
    open_timeout_seconds: float = Field(default=10.0, gt=0, description="WebSocket 握手超时（秒）")
    backoff_base_seconds: float = Field(default=1.0, gt=0, description="重连退避基数秒")
    backoff_max_seconds: float = Field(default=60.0, gt=0, description="重连退避封顶秒数")
    backoff_jitter_ratio: float = Field(
        default=0.3, ge=0, le=1, description="重连退避抖动幅度比例（0-1）"
    )
    reply_window_seconds: float = Field(
        default=86400.0, gt=0, description="消息回调回复有效期（秒——官方 24 小时）"
    )

    model_config = SettingsConfigDict(
        env_prefix="WECOM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _verify_channel_bounds(self) -> WecomChannelSettings:
        """校验通道配置不变式（退避边界 / 启用时必填 BotID / 心跳预算）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ``backoff_max_seconds < backoff_base_seconds``（封顶必须
                不小于基数）；``enabled=True`` 且 ``bot_id`` 为空（无身份无法
                订阅）；``heartbeat_timeout_seconds >= heartbeat_interval_seconds``
                （应答超时覆盖整个间隔会让心跳永远判定失败）。
        """
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                f"backoff_max_seconds({self.backoff_max_seconds}) 不得小于 "
                f"backoff_base_seconds({self.backoff_base_seconds})"
            )
        if self.enabled and not self.bot_id.strip():
            raise ValueError("WECOM_ENABLED=true 时 WECOM_BOT_ID 必填（智能机器人 BotID）")
        if self.heartbeat_timeout_seconds >= self.heartbeat_interval_seconds:
            raise ValueError(
                f"heartbeat_timeout_seconds({self.heartbeat_timeout_seconds}) 必须小于 "
                f"heartbeat_interval_seconds({self.heartbeat_interval_seconds})"
            )
        return self


class DouyinChannelSettings(BaseSettings):
    """抖音 web 通道配置（env_prefix=``DOUYIN_``——channels 域）。

    锁定决策：``enabled=False``（默认）——零配置开发可用、测试 hermetic；
    ``enabled=True`` 时 app lifespan 装配轮询 runner（后台任务，见
    docs/backend/channels.md「抖音 web 通道」）。

    底座依赖（同底座约束）：抖音 web 通道**不自建浏览器**——它消费浏览器
    sidecar 的已登录页面（``TOOLS_BROWSER_SIDECAR_*`` 分节 + 持久化 profile
    里的抖音登录态），经 ``evaluate`` / ``navigate`` 工具读写会话。因此本分节
    不重复 sidecar 的地址与令牌配置；``DOUYIN_ENABLED=true`` 而 sidecar 未
    启用 / 未就绪时，装配期记 ERROR 并只跳过本通道（其余通道不受影响）。

    凭据面：登录态是浏览器 cookie（存在 sidecar 的 profiles 卷），**没有
    独立 token / secret env 键**——这是与 telegram / QQ / 企微的差别，也是
    「同底座」的直接结果。

    Attributes:
        enabled: 是否启用抖音 web 通道（False = 零轮询任务 / 零页面操作）。
        chat_url: 会话页 URL（空串 = 不导航，沿用 sidecar 当前页面——人工
            把页面停在抖音会话页的部署形态）。非空时经 sidecar ``navigate``
            打开一次，域名必须在 ``BROWSER_SIDECAR_ALLOWLIST`` 内。
        conversation_id: 目标会话标识（空串 = 由页面 URL 参数推断，仍为空时
            按 ``default`` 兜底——单会话部署形态）。
        poll_interval_seconds: 采集轮询间隔秒数（出站请求量的唯一闸）。
        max_messages_per_poll: 单次采集消息上限（1-100——脚本取时间序末尾）。
        backoff_base_seconds: 轮询失败退避基数秒。
        backoff_max_seconds: 退避封顶秒数（不得小于基数）。
    """

    enabled: bool = Field(default=False, description="是否启用抖音 web 通道")
    chat_url: str = Field(
        default="",
        description="会话页 URL（空 = 不导航，沿用 sidecar 当前页面）",
    )
    conversation_id: str = Field(
        default="",
        description="目标会话标识（空 = 由页面 URL 推断 / default 兜底）",
    )
    poll_interval_seconds: float = Field(
        default=5.0, gt=0, description="采集轮询间隔秒数"
    )
    max_messages_per_poll: int = Field(
        default=20, ge=1, le=100, description="单次采集消息上限（1-100）"
    )
    backoff_base_seconds: float = Field(default=2.0, gt=0, description="轮询失败退避基数秒")
    backoff_max_seconds: float = Field(default=60.0, gt=0, description="退避封顶秒数")

    model_config = SettingsConfigDict(
        env_prefix="DOUYIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("chat_url")
    @classmethod
    def _validate_chat_url(cls, value: str) -> str:
        """校验会话页 URL（空串合法 = 不导航；非空必须 http/https）。

        Args:
            value: 原始配置值。

        Returns:
            去除首尾空白后的值。

        Raises:
            ValueError: 非空且协议不是 http/https（navigate 工具会拒绝，
                此处提前在配置面 fail fast）。
        """
        cleaned = value.strip()
        if cleaned and not cleaned.startswith(("http://", "https://")):
            raise ValueError("DOUYIN_CHAT_URL 必须是 http/https URL（空串 = 不导航）")
        return cleaned

    @model_validator(mode="after")
    def _verify_backoff_bounds(self) -> DouyinChannelSettings:
        """校验退避边界（封顶不得小于基数）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: ``backoff_max_seconds < backoff_base_seconds``。
        """
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                f"backoff_max_seconds({self.backoff_max_seconds}) 不得小于 "
                f"backoff_base_seconds({self.backoff_base_seconds})"
            )
        return self


class ChannelsSettings(BaseModel):
    """通道域配置分节（``Settings.channels``——多平台通道聚合容器）。

    当前含 telegram、qq（NapCat / OneBot v11）、企业微信智能机器人长连接与
    抖音 web 四个子模型；多通道扩展（B站等）在此追加同级子模型（各自独立
    env_prefix 的 ``BaseSettings``——本工程约定：env 面子配置为
    BaseSettings，聚合容器为普通 BaseModel）。

    Attributes:
        telegram: Telegram 通道配置（默认关闭）。
        qq: QQ 通道配置（默认关闭——NapCat OneBot v11）。
        wecom: 企业微信智能机器人长连接配置（默认关闭）。
        douyin: 抖音 web 通道配置（默认关闭——浏览器 sidecar 底座）。
    """

    telegram: TelegramSettings = Field(
        default_factory=TelegramSettings, description="Telegram 通道配置"
    )
    qq: QqChannelSettings = Field(default_factory=QqChannelSettings, description="QQ 通道配置")
    wecom: WecomChannelSettings = Field(
        default_factory=WecomChannelSettings, description="企业微信智能机器人长连接配置"
    )
    douyin: DouyinChannelSettings = Field(
        default_factory=DouyinChannelSettings, description="抖音 web 通道配置"
    )


class TtsSettings(BaseSettings):
    """TTS 语音合成配置（env_prefix=``VOICE_TTS_``——voice 域）。

    锁定决策：``enabled=False``（默认）——语音是可选能力面：CosyVoice3
    包装服务是部署机上的外部本地进程（权重经 ``models/`` 子模块承载，
    compose 接线属部署侧事务），缺省零配置开发可用、测试 hermetic；
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
    """ASR 语音识别配置（env_prefix=``VOICE_ASR_``——voice 域）。

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
        max_upload_bytes: ASR 上传请求体大小上限（字节；25MB 量级——
            音频载荷体积天然大于截图，与 look_at 图像 10MB 上限同为
            资源面守卫但量级不同）。
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
    max_upload_bytes: int = Field(
        default=26214400,
        ge=1024,
        description="ASR 上传请求体上限（字节；25MB 量级——音频体积天然大于截图，"
        "与 look_at 图像 10MB 上限同为资源面守卫）",
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


class WebSearchSettings(BaseSettings):
    """联网搜索运营配置（env_prefix=``SEARCH_``——search 域）。

    锁定决策：``enabled=False``（默认）——联网搜索是
    **可选能力面**（外部搜索 provider 经 HTTP 契约消费），缺省零配置
    开发可用、测试 hermetic；``enabled=True`` 且 config.yaml ``search``
    段无 provider 条目时启动期 fail fast（中文错误）。

    本类同时是 config.yaml ``search.web_search`` 子块的类型（分层加载链
    单类双源——yaml 子块键优先于 SEARCH_* env，与「代码默认 < .env <
    env < config.yaml」的文档化优先级一致：yaml 提供的键覆盖 env，
    未提供的键回落 env / 代码默认）。provider CRUD 持久化 ``search``
    段时会把当前生效值（env 已合并）一并落盘——yaml 是最高层，写入
    即固化（运维在 env 调值后再做 CRUD 会把 env 值固化进 yaml，
    .bak 备份与审计面可回溯）。

    Attributes:
        enabled: 是否启用联网搜索能力（False = 工具面 / 服务面不装配）。
        tool_enabled: 是否注册 ``web_search`` LLM 工具（duty 工具循环 +
            chat 工具循环共用；**工具仅在 enabled 与本开关同时为真时
            注册**——两级开关：能力总闸 + 工具面曝光闸）。
        daily_budget_per_persona: 单人格每日搜索调用数预算（按 usage
            行计数——含失败尝试行，防 provider 故障重试风暴绕过预算）。
        daily_budget_global: 全局每日搜索调用数预算（全部人格合计）。
        cooldown_seconds: provider 失败降级冷却秒数（冷却窗口内跳过
            该 provider 直落下一级；全部降级 → 清晰业务异常）。
    """

    enabled: bool = Field(default=False, description="是否启用联网搜索能力")
    tool_enabled: bool = Field(
        default=False, description="是否注册 web_search 工具（需 enabled 同时为真）"
    )
    daily_budget_per_persona: int = Field(
        default=50, ge=1, description="单人格每日搜索调用数预算"
    )
    daily_budget_global: int = Field(default=200, ge=1, description="全局每日搜索调用数预算")
    cooldown_seconds: int = Field(
        default=60, ge=1, description="provider 失败降级冷却秒数"
    )

    model_config = SettingsConfigDict(
        env_prefix="SEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


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
        channels: 通道域配置分节（telegram / qq 默认关闭）。
        duty: 值班轮配置（默认关闭）。
        dream_schedule: 梦境定时调度配置（默认关闭）。
        scheduler: 中央调度器配置（tick 间隔）。
        tools_sandbox: 工具沙盒配置（默认关闭；沙盒为可选
            运行时，duty 工具面在沙盒不可达时回落到内置工具）。
        tools_browser_sidecar: 浏览器 sidecar 配置（默认关闭；sidecar
            为可选运行时——JS 渲染回退面，duty / chat 工具面在 sidecar
            不可达时回落到既有工具集）。
        voice: 语音域配置（TTS / ASR 默认关闭；外部本地
            模型服务经 HTTP 契约消费，部署接线属部署侧事务）。
        realtime_voice: 实时语音会话配置（默认关闭——REALTIME_VOICE_
            env 面；物理定义在
            :class:`packages.harness.voice.realtime_models.
            RealtimeVoiceSettings`，此处聚合引用）。
        voiceprint: 声纹配置（默认关闭——VOICEPRINT_ env 面；物理定义在
            :class:`packages.harness.voice.voiceprint_models.VoiceprintSettings`）。
        sticker: 表情包库配置（默认关闭——STICKER_ env 面；物理定义在
            :class:`packages.harness.multimodal.sticker_models.StickerSettings`）。
        search: 联网搜索运营配置（开关 / 工具面 / 预算 / 冷却——
            SEARCH_ env 面；provider 注册表经 config.yaml ``search``
            段承载，见 :class:`SearchConfig`）。
        rhythm: 拟人节律配置（默认关闭——RHYTHM_ env 面；物理定义在
            :class:`packages.harness.rhythm.rhythm_models.RhythmSettings`，
            此处聚合引用——models 与 logic 分离约定不变）。
        flow: 心流门控配置（默认关闭——FLOW_ env 面；物理定义在
            :class:`packages.harness.chat.flow_models.FlowGateSettings`，
            此处聚合引用）。
        group_persona: 群聊多人格应答配置（默认关闭——GROUP_PERSONA_
            env 面；物理定义在
            :class:`packages.harness.chat.persona_rotation_models.
            GroupPersonaSettings`，此处聚合引用）。
        intent: 意图快径配置（默认开启——INTENT_ env 面；物理定义在
            :class:`packages.harness.intent.intent_models.IntentSettings`，
            此处聚合引用）。
        env: 运行环境标识（local / dev / test / prod——prod 触发生产
            鉴权 fail-closed 策略）。
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
    tools_browser_sidecar: ToolsBrowserSidecarSettings = ToolsBrowserSidecarSettings()
    voice: VoiceSettings = VoiceSettings()
    realtime_voice: RealtimeVoiceSettings = RealtimeVoiceSettings()
    voiceprint: VoiceprintSettings = VoiceprintSettings()
    sticker: StickerSettings = StickerSettings()
    search: WebSearchSettings = WebSearchSettings()
    rhythm: RhythmSettings = RhythmSettings()
    flow: FlowGateSettings = FlowGateSettings()
    group_persona: GroupPersonaSettings = GroupPersonaSettings()
    intent: IntentSettings = IntentSettings()
    env: RuntimeEnv = Field(
        default=RuntimeEnv.LOCAL,
        description="运行环境标识（local / dev / test / prod）",
    )

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


# ---------------------------------------------------------------------------
# 联网搜索配置节（config.yaml ``search`` 段——provider 注册表 + web_search
# 子块；管理面见 harness/config/search_registry.py）
# ---------------------------------------------------------------------------

#: 搜索 provider kind 值域（bocha 主 / zhipu 兜底——两 provider 的 failover 次序）。
SEARCH_PROVIDER_KINDS: tuple[str, str] = ("bocha", "zhipu")

#: 各 kind 的缺省 API Key 环境变量名（api_key_env 间接——密钥本值永不落
#: yaml / 配置树；示例与测试面统一指向这两个变量）。
_DEFAULT_API_KEY_ENV_FOR_KIND: dict[str, str] = {
    "bocha": "SEARCH_BOCHA_API_KEY",
    "zhipu": "SEARCH_ZHIPU_API_KEY",
}

#: 各 kind 的缺省 base_url（官方端点——条目可覆写）。
_DEFAULT_BASE_URL_FOR_KIND: dict[str, str] = {
    "bocha": "https://api.bochaai.com",
    "zhipu": "https://open.bigmodel.cn",
}


def default_search_model_for_kind(kind: str) -> str:
    """按 kind 返回 provider 专属 model 字段缺省值。

    语义（provider 差异）：bocha 的 web-search 端点无模型选择参数——
    ``model`` 为观测标识（恒 ``web-search``）；zhipu 的
    ``/api/paas/v4/web_search`` 端点以 ``search_engine`` 字段选择引擎
    （``model`` 缺省 ``search_std`` 兜底档；可配 ``search_pro`` 等）。

    Args:
        kind: provider kind（bocha / zhipu）。

    Returns:
        该 kind 的 model 字段缺省值。

    Raises:
        ValueError: 未知 kind。
    """
    if kind == "bocha":
        return "web-search"
    if kind == "zhipu":
        return "search_std"
    raise ValueError(f"未知搜索 provider kind: {kind!r}（合法值: {list(SEARCH_PROVIDER_KINDS)}）")


class SearchProviderEntry(BaseModel):
    """搜索 provider 条目（config.yaml ``search.providers`` 列表项）。

    密钥不变式（与 llm 条目 ``api_key_env`` 同模式）：条目只携带
    环境变量名，密钥本值经 ``os.environ`` 在调用链解析点读取——
    **永不落 yaml / 配置树 / 响应面**。

    Attributes:
        name: 条目唯一标识（failover 链内引用；usage 行 model_entry 列）。
        kind: provider 类型（``bocha`` 博查 Web Search API / ``zhipu``
            智谱 Web Search API）。
        base_url: provider API 基础 URL（空 = 按 kind 缺省官方端点）。
        api_key_env: API Key 环境变量名（空 = 按 kind 缺省变量名；
            指向的变量为空时调用链 fail loud）。
        model: provider 专属模型 / 引擎标识（空 = 按 kind 缺省——bocha
            观测标识 ``web-search`` / zhipu 引擎码缺省 ``search_std``）。
        max_results: 单次搜索返回条数上限（1-20；请求级可覆盖）。
        timeout_seconds: 单次请求超时秒数（httpx.Timeout 全阶段）。
    """

    name: str = Field(min_length=1, max_length=64, description="条目唯一标识")
    kind: Literal["bocha", "zhipu"] = Field(description="provider 类型（bocha/zhipu）")
    base_url: str = Field(
        default="", min_length=0, max_length=512, description="API 基础 URL（空=按 kind 缺省）"
    )
    api_key_env: str = Field(
        default="",
        min_length=0,
        max_length=128,
        description="API Key 环境变量名（空=按 kind 缺省；密钥本值永不落 yaml）",
    )
    model: str = Field(
        default="", min_length=0, max_length=128, description="provider 专属模型/引擎标识（空=按 kind 缺省）"
    )
    max_results: int = Field(
        default=8, ge=1, le=20, description="单次搜索返回条数上限（1-20）"
    )
    timeout_seconds: float = Field(default=15.0, gt=0, le=120, description="单次请求超时（秒）")

    @model_validator(mode="after")
    def _fill_kind_defaults(self) -> SearchProviderEntry:
        """按 kind 物化缺省值（base_url / api_key_env / model 三个空值缝）。

        Returns:
            缺省物化后的实例（显式提供的值原样保留）。
        """
        if not self.base_url:
            self.base_url = _DEFAULT_BASE_URL_FOR_KIND[self.kind]
        if not self.api_key_env:
            self.api_key_env = _DEFAULT_API_KEY_ENV_FOR_KIND[self.kind]
        if not self.model:
            self.model = default_search_model_for_kind(self.kind)
        return self


class SearchConfig(BaseModel):
    """config.yaml ``search`` 段——搜索 provider 注册表 + web_search 子块。

    config.yaml 示例::

        search:
          providers:
            - name: "bocha-main"
              kind: "bocha"
              api_key_env: "SEARCH_BOCHA_API_KEY"
              max_results: 8
              timeout_seconds: 15
            - name: "zhipu-fallback"
              kind: "zhipu"
              model: "search_std"
              api_key_env: "SEARCH_ZHIPU_API_KEY"
          web_search:
            enabled: true
            tool_enabled: true
            daily_budget_per_persona: 50
            daily_budget_global: 200
            cooldown_seconds: 60

    failover 语义：``providers`` 列表序即尝试序（前者为主、后者兜底）；
    条目失败后进入降级冷却（``web_search.cooldown_seconds``），冷却窗口
    内跳过直落下一级；全部降级 → 清晰业务异常。

    Attributes:
        providers: provider 条目列表（列表序 = failover 尝试序；条目名
            节内唯一——管理面 CRUD 校验）。
        web_search: 运营子块（开关 / 工具面 / 预算 / 冷却——
            :class:`WebSearchSettings`；yaml 键优先于 SEARCH_* env）。
    """

    providers: list[SearchProviderEntry] = Field(
        default_factory=list, description="provider 条目列表（列表序=failover 尝试序）"
    )
    web_search: WebSearchSettings = Field(
        default_factory=WebSearchSettings, description="运营子块（开关/预算/冷却）"
    )


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
