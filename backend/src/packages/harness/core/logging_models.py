"""日志配置 Model Class —— core.logging 的配置契约。

core 层不 import config/（chunk 边界：config 是 P0-3 交付面，依赖方向
必须 config → core），因此 core.logging 以本 Model Class 为配置入参契约：

- app 层 lifespan 调用 ``setup_logging(config)`` 时，由 config 域的
  Settings 构造本模型实例传入（字段一一映射）；
- 测试用 fixture 显式构造本模型（tmp_path 落盘路径），零真实配置依赖。

字段默认值即工程规格：3 sink（console + 文本文件 + JSON 文件）、
每日轮转、30 天保留。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

__all__ = ["LoggingConfig"]

#: loguru 合法日志级别（大小写不敏感，校验前统一大写）。
_VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
)


class LoggingConfig(BaseModel):
    """日志系统配置（setup_logging 入参契约）。

    Attributes:
        level: 全局日志级别（默认 ``INFO``；大小写不敏感，校验时统一大写）。
        log_file: 文本日志文件路径（相对路径按进程 CWD 解析；父目录自动创建）。
        log_json_file: JSON 日志文件路径（结构化单行 JSON，供聚合系统解析）。
        rotation: 轮转策略（默认 ``1 day``——每日轮转；loguru 支持的时间/
            大小规格字符串）。
        retention: 保留时长（默认 ``30 days``——30 天保留；到期自动清理）。
        module_levels: 按模块覆盖级别的规格串（如
            ``"packages.harness.core=DEBUG,app.api=WARNING"``；空串 = 无覆盖）。
        text_format: 自定义文本 sink 格式模板（loguru format 语法；
            ``None`` = 使用默认统一模板）。
        colorize_console: console sink 是否彩色输出（默认 True）。
    """

    level: str = Field(default="INFO", description="全局日志级别")
    log_file: str = Field(default="logs/app.log", description="文本日志文件路径")
    log_json_file: str = Field(default="logs/app.json.log", description="JSON 日志文件路径")
    rotation: str = Field(default="1 day", description="轮转策略（每日轮转）")
    retention: str = Field(default="30 days", description="保留时长（30 天）")
    module_levels: str = Field(default="", description="按模块覆盖级别规格串")
    text_format: str | None = Field(
        default=None, description="自定义文本格式模板（None=默认统一模板）"
    )
    colorize_console: bool = Field(default=True, description="console sink 彩色输出")

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        """校验日志级别合法（loguru 值域），统一大写。

        Args:
            value: 原始级别字符串。

        Returns:
            大写规范化后的级别字符串。

        Raises:
            ValueError: 级别不在 loguru 合法值域内。
        """
        normalized = value.strip().upper()
        if normalized not in _VALID_LOG_LEVELS:
            valid = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"非法日志级别: {value!r}（合法值: {valid}）")
        return normalized

    @field_validator("log_file", "log_json_file")
    @classmethod
    def _validate_non_empty_path(cls, value: str) -> str:
        """校验日志文件路径非空（空路径会导致 sink 创建失败）。

        Args:
            value: 原始路径字符串。

        Returns:
            原路径字符串。

        Raises:
            ValueError: 路径为空 / 纯空白。
        """
        if not value.strip():
            raise ValueError("日志文件路径不能为空")
        return value
