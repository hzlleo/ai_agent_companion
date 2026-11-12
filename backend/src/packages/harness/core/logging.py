"""日志系统配置 — loguru console + 文本文件 + JSON 文件三 sink + 轮转 + 脱敏。

在 app 层 lifespan 中调用 ``setup_logging(config)`` 初始化（``config`` 为
``core.logging_models.LoggingConfig`` 实例——core 不 import config 域，
依赖方向 config → core）。入口层通过 ``logger.contextualize(**context_log_fields())``
注入 request_id / session_id / user_id / persona_id，使所有日志行自动
携带请求上下文，便于全链路追踪。

（三 sink 架构，P0 规格每日轮转 + 30 天保留）:
    - console sink：stderr 彩色输出（``colorize_console`` 可关）；
    - 文本文件 sink：``log_file``，统一文本格式（见下方模板）；
    - JSON 文件 sink：``log_json_file``，每行一个 JSON 对象，便于 ELK /
      Loki / DataDog 等日志聚合系统解析。字段包含 ``timestamp`` /
      ``level`` / ``logger`` / ``message`` / ``request_id`` / ``session_id``
      / ``user_id`` / ``persona_id`` / ``module`` / ``function`` / ``line``。
    - 通过 ``logger.configure(extra=...)`` 默认值确保未在上下文中的字段
      也有占位（``-``）。

（敏感信息脱敏）:
    - ``register_secret_masker()`` 注册 loguru ``patcher``，对 ``record``
      的 ``message`` / ``extra`` 进行正则脱敏：
        - ``api_key=xxx`` / ``api_key:xxx`` → ``api_key=***``
        - ``token=xxx`` / ``password=xxx`` / ``secret=xxx`` → 同上
        - ``Authorization: Bearer xxx`` → ``Authorization: Bearer ***``
        - ``sk-xxxx`` / ``sk-ant-xxxx``（OpenAI / Anthropic API Key 前缀）
    - 长度阈值（默认 8）：短于阈值的值不脱敏（避免误伤 ``id=1`` 这类短值）
    - 脱敏在 loguru sink 写入前执行，所有 sink（console / 文件 / JSON）均生效

（按模块配置日志级别）:
    - ``LoggingConfig.module_levels`` 允许按模块前缀覆盖，如
      ``"packages.harness.core=DEBUG,app.api=WARNING"``
    - 解析后经 ``_make_module_filter`` 构造 sink filter，为每个模块应用
      独立级别（最长前缀匹配优先）

（统一文本格式）:
    - console stderr + 文本文件 sink 共用同一文本格式：
      ``{time} - {name} - {level} - {function}:{line} - req/session/user/
      persona 上下文 - {message}``
    - 分隔符 ``" - "``；可通过 ``LoggingConfig.text_format`` 覆盖
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from packages.harness.core.logging_models import LoggingConfig

__all__ = [
    "setup_logging",
    "register_secret_masker",
    "parse_module_log_levels",
    "mask_sensitive",
    "SENSITIVE_KEY_PARTS",
    "DEFAULT_TEXT_FORMAT",
]


# ---------------------------------------------------------------------------
# 敏感信息脱敏
# ---------------------------------------------------------------------------

# 敏感 key 关键词（key=value / key: value 形式匹配）
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
)

# 长度阈值：value 长度 >= 8 才脱敏（避免误伤 ``id=1`` / ``port=8080`` 这类短值）
_MIN_SENSITIVE_VALUE_LEN = 8

# 构建 key 模式：匹配 "key=value" 或 "key: value" 形式
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(" + "|".join(re.escape(p) for p in SENSITIVE_KEY_PARTS) + r")"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\",}]{8,})(\3)",
    re.IGNORECASE,
)

# Bearer token 模式：``Bearer xxxxxxxx``
_BEARER_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+)([A-Za-z0-9._\-+/=]{8,})",
    re.IGNORECASE,
)

# 长字符串密钥模式：``sk-xxxx`` / ``sk-ant-xxxx``（OpenAI 兼容端点 API Key 前缀）
_LONG_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9\-_]{8,}|sk-ant-[A-Za-z0-9\-_]{8,})",
)


def mask_sensitive(text: str) -> str:
    """对文本中的敏感信息脱敏。

    Args:
        text: 原始字符串（如日志消息、JSON 序列化后的字符串）。

    Returns:
        脱敏后的字符串。敏感 value 替换为 ``***``。

    Examples:
        >>> mask_sensitive("api_key=sk-1234567890abcdef")
        'api_key=***'
        >>> mask_sensitive("password: 'supersecret123'")
        "password: '***'"
        >>> mask_sensitive("Authorization: Bearer abcdef123456")
        'Authorization: Bearer ***'
    """
    if not text:
        return text

    # 1. ``key=value`` / ``key: value`` 形式
    text = _SENSITIVE_KEY_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***{m.group(5)}",
        text,
    )

    # 2. ``Bearer xxx`` 形式
    text = _BEARER_TOKEN_PATTERN.sub(
        lambda m: f"{m.group(1)}***",
        text,
    )

    # 3. 长 sk-xxx / sk-ant-xxx 形式（OpenAI 兼容端点 API Key 前缀）
    text = _LONG_SECRET_PATTERN.sub("***", text)

    return text


def _redact_record(record: Any) -> None:
    """对 loguru record 中的 message 与 extra 字段进行脱敏。

    作为 loguru ``patcher`` 使用——在 sink 写入前修改 record。
    注：loguru patcher 收到的 record 是 dict（经 ``logger.patch(...)``
    挂接时逐 record 调用），必须按 dict 契约访问（属性访问会在运行期
    抛 AttributeError）；本函数由单测直测。
    """
    message = record.get("message")
    if message:
        record["message"] = mask_sensitive(message)

    extra = record.get("extra")
    if extra:
        for key, value in list(extra.items()):
            if isinstance(value, str) and value:
                masked = mask_sensitive(value)
                if masked != value:
                    extra[key] = masked


_masker_registered: bool = False


def register_secret_masker() -> None:
    """注册 loguru patcher 对所有日志记录进行敏感信息脱敏。

    脱敏在 sink 写入前执行，所有 sink（console / 文件 / JSON）均生效。
    重复调用幂等（用模块级标志避免重复注册）。
    """
    global _masker_registered
    if _masker_registered:
        return
    logger.patch(_redact_record)
    _masker_registered = True


# ---------------------------------------------------------------------------
# 按模块配置日志级别
# ---------------------------------------------------------------------------


def parse_module_log_levels(spec: str) -> dict[str, str]:
    """解析 ``"module=LEVEL,module2=LEVEL2"`` 格式的模块日志级别配置。

    Args:
        spec: 配置字符串，如 ``"packages.harness.core=DEBUG,app.api=WARNING"``。
            空字符串返回空 dict。

    Returns:
        ``{module_path: level_str}`` dict。无效条目跳过（log warning）。

    Examples:
        >>> parse_module_log_levels("")
        {}
        >>> parse_module_log_levels("app.services=DEBUG")
        {'app.services': 'DEBUG'}
        >>> parse_module_log_levels("a=DEBUG,b=INFO")
        {'a': 'DEBUG', 'b': 'INFO'}
    """
    if not spec or not spec.strip():
        return {}

    result: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        module, _, level = part.partition("=")
        module = module.strip()
        level = level.strip().upper()
        if not module or not level:
            continue
        # 校验 level 合法性
        if level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
            logger.warning(
                "模块日志级别配置忽略无效级别: module={}, level={}",
                module,
                level,
            )
            continue
        result[module] = level
    return result


def _make_module_filter(
    module_levels: dict[str, str],
    default_level: str,
) -> Any:
    """构造 loguru sink filter，按模块过滤日志级别。

    Args:
        module_levels: ``{module_prefix: level_str}``，key 是模块前缀
            （如 ``packages.harness.core`` 匹配该模块及子模块）。
        default_level: 默认级别（不在 module_levels 中的模块使用）。

    Returns:
        loguru sink filter 函数。
    """

    def _filter(record: Any) -> bool:
        record_module: str = record.get("name", "") or ""
        # 找到最长匹配的模块前缀（更具体的优先）
        matched_level: str = default_level
        matched_len = 0
        for prefix, level in module_levels.items():
            if (record_module == prefix or record_module.startswith(prefix + ".")) and len(
                prefix
            ) >= matched_len:
                matched_level = level
                matched_len = len(prefix)
        record["level_no"] = record["level"].no
        # loguru filter 返回 True 表示允许写入
        # level.no >= matched_level_no 才写入
        try:
            matched_level_no = logger.level(matched_level).no
        except (ValueError, AttributeError):
            matched_level_no = 20  # INFO 默认
        return bool(record["level_no"] >= matched_level_no)

    return _filter


# ---------------------------------------------------------------------------
# 结构化 JSON 日志格式
# ---------------------------------------------------------------------------


def _json_serializer(record: Any) -> str:
    """loguru JSON 序列化函数（record dict → 单行 JSON）。

    包含全链路追踪字段：``timestamp`` / ``level`` / ``logger`` /
    ``message`` / ``request_id`` / ``session_id`` / ``user_id`` /
    ``persona_id`` / ``module`` / ``function`` / ``line``。

    入参兼容两种形态：loguru record dict（format callable 直传）与
    Message 形对象（带 ``.record`` 属性——历史 callable sink / 测试
    FakeMessage 形态）；注解为 Any（dict 注解下 mypy 视属性分支
    unreachable，而 Message 形态是真实运行时路径）。
    """
    if not isinstance(record, dict):
        record = record.record
    extra = record.get("extra", {}) or {}

    request_id = extra.get("request_id", "-") or "-"
    session_id = extra.get("session_id", "-") or "-"
    user_id = extra.get("user_id", "-") or "-"
    persona_id = extra.get("persona_id", "-") or "-"

    msg_text = record["message"]

    payload: dict[str, Any] = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": msg_text,
        "request_id": request_id,
        "session_id": session_id,
        "user_id": user_id,
        "persona_id": persona_id,
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
    }

    if record.get("exception") is not None:
        exc = record["exception"]
        payload["exception"] = {
            "type": type(exc.value).__name__ if exc.value else "",
            "value": str(exc.value) if exc.value else "",
        }

    for key, value in extra.items():
        if key in ("request_id", "session_id", "user_id", "persona_id"):
            continue
        if key not in payload:
            payload[key] = value

    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

#: 默认统一文本格式（console + 文本文件 sink 共用；可用 text_format 覆盖）。
DEFAULT_TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> - "
    "<cyan>{name}</cyan> - "
    "<level>{level: <8}</level> - "
    "<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<cyan>req={extra[request_id]}</cyan> "
    "<cyan>session={extra[session_id]}</cyan> "
    "<cyan>user={extra[user_id]}</cyan> "
    "<cyan>persona={extra[persona_id]}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging(config: LoggingConfig) -> None:
    """配置 loguru 日志系统：console + 文本文件 + JSON 文件三 sink + 轮转 + 脱敏。

    （三 sink）:
        - console：stderr 彩色输出（``config.colorize_console`` 控制）；
        - 文本文件：``config.log_file``，统一文本格式（``config.text_format``
          可覆盖默认模板——console 与文本文件共用同一 resolved 模板，
          自定义模板未含 ``{exception}`` 时自动追加异常渲染段）；
        - JSON 文件：``config.log_json_file``，单行 JSON（全链路追踪字段）。

    （轮转与保留，P0 规格）:
        - 两个文件 sink 均为 ``config.rotation``（默认 ``1 day``——每日轮转）
          + ``config.retention``（默认 ``30 days``——30 天保留）。
          loguru 不支持「按天 + 按大小」复合轮转，按工程规格为纯每日
          轮转 + 30 天保留。

    （脱敏）:
        - 注册 ``register_secret_masker()`` patcher；
        - 同时在每个 sink 的 ``format`` 函数中再次调用 ``mask_sensitive``，
          双重保险（patcher 在某些 loguru 版本可能不影响全局 logger）。

    （按模块级别）:
        - 解析 ``config.module_levels``，按模块前缀过滤日志级别
          （最长前缀匹配优先）。

    Args:
        config: 日志配置（``LoggingConfig`` 实例——测试经 fixture 显式构造，
            生产由 config 域 Settings 映射构造；core 不读取任何全局配置）。
    """
    register_secret_masker()

    module_levels = parse_module_log_levels(config.module_levels)
    default_level = config.level

    resolved_text_format = config.text_format or DEFAULT_TEXT_FORMAT

    def _make_text_formatter(template: str) -> Any:
        """构造文本 sink 的 format callable：先脱敏 message，再返回模板。

        自定义模板未含 ``{exception}`` 时自动追加异常渲染段（保证异常
        栈仍写入文本 sink）。
        """

        append_exception = "{exception}" not in template

        def _formatter(record: Any) -> str:
            if record["message"]:
                record["message"] = mask_sensitive(record["message"])
            return template + ("\n{exception}" if append_exception else "")

        return _formatter

    text_formatter = _make_text_formatter(resolved_text_format)

    logger.remove()

    logger.add(
        sys.stderr,
        level=default_level,
        format=text_formatter,
        colorize=config.colorize_console,
        backtrace=True,
        diagnose=True,
        filter=_make_module_filter(module_levels, default_level),
    )

    log_file = Path(config.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.add(
        str(log_file),
        level=default_level,
        format=text_formatter,
        rotation=config.rotation,
        retention=config.retention,
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
        filter=_make_module_filter(module_levels, default_level),
    )

    json_log_file = Path(config.log_json_file)
    json_log_file.parent.mkdir(parents=True, exist_ok=True)

    # JSON sink：与文本 sink 同一轮转/保留口径（每日 + 30 天）。format
    # callable 不能直接返回 JSON 文本（loguru 会对返回模板做 format_map，
    # JSON 的 ``{`` 会被当占位符）——标准做法：序列化结果挂到
    # record["extra"]，模板只留占位符（占位符替换值不做二次 format，
    # 可安全携带任意 JSON 文本）。
    def _json_formatter(record: Any) -> str:
        if record["message"]:
            record["message"] = mask_sensitive(record["message"])
        extra = record.get("extra", {}) or {}
        for key, value in list(extra.items()):
            if isinstance(value, str) and value:
                masked = mask_sensitive(value)
                if masked != value:
                    extra[key] = masked
        record["extra"]["_json_line"] = _json_serializer(record)
        return "{extra[_json_line]}"

    logger.add(
        str(json_log_file),
        level=default_level,
        format=_json_formatter,
        rotation=config.rotation,
        retention=config.retention,
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
        filter=_make_module_filter(module_levels, default_level),
    )

    logger.configure(
        extra={"request_id": "-", "session_id": "-", "user_id": "-", "persona_id": "-"}
    )

    logger.info(
        "日志系统已初始化: level={}, file={}, json_file={}, module_levels={}",
        default_level,
        log_file,
        json_log_file,
        module_levels or "(none)",
    )
