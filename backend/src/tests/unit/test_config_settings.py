"""harness/config Settings 测试——env 面 / 聚合 / 隔离构造 / 日志映射 / 连接串。

零真实配置：全局单例读取根 conftest 硬覆盖的 env 值；隔离实例经
``build_isolated_settings`` 显式值构造。
"""

from __future__ import annotations

import os

from packages.harness.config.models import (
    DatabaseConfig,
    LlmEndpointConfig,
    LogConfig,
    PoolsConfig,
    ServerConfig,
    Settings,
    settings,
)
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.logging_models import LoggingConfig


async def test_settings_singleton_reads_conftest_env() -> None:
    """全局单例捕获根 conftest 硬覆盖的 env 值（防 shell 泄漏语义）。"""
    assert settings.database.type == "sqlite"
    assert settings.database.host == "localhost"
    assert settings.database.port == 3306
    assert settings.database.name == "test_db"
    assert settings.database.user == "test_user"
    assert settings.database.password.get_secret_value() == "test_pass"
    assert settings.env == "test"


async def test_settings_log_section_maps_env() -> None:
    """log 分节字段与 LOG_* env 面对齐（字段名 → env 名映射契约）。"""
    assert settings.log.level == "INFO"
    assert settings.log.file == "logs/test-app.log"
    assert settings.log.json_file == "logs/test-app.json.log"
    assert settings.log.rotation == "1 day"
    assert settings.log.retention == "30 days"
    assert settings.log.module_levels == ""
    assert settings.log.colorize_console is False


async def test_settings_pools_section_maps_env() -> None:
    """pools 分节字段与 POOLS_* env 面对齐。"""
    assert settings.pools.thread_pool_size == 10
    assert settings.pools.http_max_connections == 100
    assert settings.pools.http_max_keepalive_connections == 20
    assert settings.pools.http_connect_timeout_seconds == 5.0
    assert settings.pools.http_timeout_seconds == 30.0
    assert settings.pools.llm_semaphore_limit == 5
    assert settings.pools.cb_fail_max == 5
    assert settings.pools.cb_reset_timeout_seconds == 60


async def test_settings_llm_env_section_maps_env() -> None:
    """llm 默认端点分节与 LLM_* env 面对齐（测试值不可触达真实端点）。"""
    assert settings.llm.base_url == "http://127.0.0.1:9/v1"
    assert settings.llm.api_key.get_secret_value() == "test-only-llm-api-key"
    assert settings.llm.model == "test-model"


async def test_log_to_core_config_field_mapping() -> None:
    """LogConfig → core LoggingConfig 字段一一映射（接线契约锁定）。"""
    log = LogConfig(
        level="debug",
        file="logs/x.log",
        json_file="logs/x.json.log",
        rotation="2 days",
        retention="7 days",
        module_levels="a.b=DEBUG",
        colorize_console=False,
    )
    core: LoggingConfig = log.to_core_config()
    assert core.level == "DEBUG"  # LoggingConfig 校验统一大写
    assert core.log_file == "logs/x.log"
    assert core.log_json_file == "logs/x.json.log"
    assert core.rotation == "2 days"
    assert core.retention == "7 days"
    assert core.module_levels == "a.b=DEBUG"
    assert core.colorize_console is False


async def test_database_url_mysql_with_special_chars() -> None:
    """MySQL 连接串：特殊字符 URL 编码 + charset 参数。"""
    config = DatabaseConfig(
        type="mysql",
        host="db.example.com",
        port=3307,
        name="companion",
        user="user@x",
        password="p@ss:word/1",
    )
    url = config.async_sqlalchemy_url
    assert url.startswith("mysql+aiomysql://user%40x:p%40ss%3Aword%2F1@db.example.com:3307/")
    assert url.endswith("/companion?charset=utf8mb4")


async def test_database_url_sqlite_memory_and_file() -> None:
    """SQLite 连接串：内存库与文件库两形态。"""
    assert DatabaseConfig(type="sqlite", name=":memory:").async_sqlalchemy_url == (
        "sqlite+aiosqlite:///:memory:"
    )
    assert DatabaseConfig(type="sqlite", name="/tmp/x.db").async_sqlalchemy_url == (
        "sqlite+aiosqlite:////tmp/x.db"
    )


async def test_database_invalid_type_rejected() -> None:
    """非法数据库类型被 Literal 校验拒绝。"""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DatabaseConfig(type="postgres")


async def test_build_isolated_settings_explicit_passthrough() -> None:
    """隔离构造：显式子配置实例原样嵌入，其余分节回退代码默认值。"""
    db = DatabaseConfig(type="sqlite", name=":memory:")
    pools = PoolsConfig(thread_pool_size=3)
    iso = build_isolated_settings(database=db, pools=pools)
    assert iso.database is db
    assert iso.pools is pools
    # 未传入分节回退默认（默认 type=mysql——隔离构造不受 conftest env 影响
    # 的断言点：显式传入才是确定性通道）
    assert iso.server.host == ServerConfig().host
    assert iso.log.level == LogConfig().level


async def test_build_isolated_settings_defaults() -> None:
    """隔离构造：全 None 时全部回退代码默认值。"""
    iso = build_isolated_settings()
    assert isinstance(iso, Settings)
    assert iso.database.name == DatabaseConfig().name
    assert iso.llm.model == LlmEndpointConfig().model


async def test_settings_env_var_priority_over_dotenv_semantics(monkeypatch) -> None:
    """进程环境变量优先于 .env 文件（pydantic-settings 语义实证）。"""
    monkeypatch.setenv("DATABASE_PORT", "3310")
    config = DatabaseConfig(env_file=None)
    assert config.port == 3310


async def test_settings_unknown_env_keys_tolerated() -> None:
    """未知环境变量不崩溃（extra=ignore 容错语义）。"""
    os.environ["AI_COMPANION_UNKNOWN_TEST_VAR"] = "junk"
    try:
        config = ServerConfig(env_file=None)
        assert config.port == 8000
    finally:
        del os.environ["AI_COMPANION_UNKNOWN_TEST_VAR"]
