"""``core.logging`` + ``core.logging_models`` 单元测试。

覆盖矩阵：

    - ``mask_sensitive``：key=value / key: value（含引号）/ Bearer /
      sk- 前缀 / 大小写不敏感 / 短值豁免 / 多密钥同串 / 非敏感 key 不动；
    - ``register_secret_masker``：幂等注册 / patcher 通道脱敏；
    - ``parse_module_log_levels``：空串 / 单条 / 多条 / 大小写规范化 /
      空白剥离 / 无效条目跳过；
    - ``_make_module_filter``：默认级别 / 前缀覆盖 / 最长前缀优先；
    - ``_json_serializer``：全链路字段 / Message 形对象兼容 / exception 段；
    - ``setup_logging``：三 sink 落盘（文本 + JSON）/ 上下文字段注入 /
      JSON 行可解析且含脱敏 / 文件 sink 收到脱敏文本；
    - ``LoggingConfig``：级别校验 / 路径非空校验 / 默认值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from packages.harness.core.logging import (
    DEFAULT_TEXT_FORMAT,
    SENSITIVE_KEY_PARTS,
    _json_serializer,
    _make_module_filter,
    mask_sensitive,
    parse_module_log_levels,
    register_secret_masker,
    setup_logging,
)
from packages.harness.core.logging_models import LoggingConfig


class TestMaskSensitive:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("api_key=sk-1234567890abcdef", "api_key=***"),
            ("password: 'supersecret123'", "password: '***'"),
            ("token=abc1234567890xyz", "token=***"),
            ("Authorization: Bearer abcdef123456", "Authorization: Bearer ***"),
            ("secret=verysecretvalue999", "secret=***"),
            ("access_token=eyJhbGciOiJIUzI1longvalue", "access_token=***"),
            ("refresh_token=verylongtokenvalue12345", "refresh_token=***"),
            ("apikey=longapikeyvalue12345", "apikey=***"),
        ],
    )
    async def test_redacts_known_sensitive_keys(self, raw: str, expected: str) -> None:
        """已知敏感 key 形态逐例脱敏。"""
        assert mask_sensitive(raw) == expected

    async def test_redacts_sk_prefixed_keys(self) -> None:
        """sk- 前缀长密钥脱敏（OpenAI 兼容端点 API Key 前缀）。"""
        raw = "Found key: sk-proj-abc123def456ghi789"
        masked = mask_sensitive(raw)
        assert "sk-proj-abc123def456ghi789" not in masked
        assert "***" in masked

    async def test_redacts_sk_ant_prefixed_keys(self) -> None:
        """sk-ant- 前缀长密钥脱敏。"""
        raw = "key=sk-ant-api03-verylongkeyvalue123"
        masked = mask_sensitive(raw)
        assert "sk-ant-api03-verylongkeyvalue123" not in masked
        assert "***" in masked

    async def test_does_not_redact_short_values(self) -> None:
        """短于 8 字符的值不脱敏（避免误伤 id=1 / port=8080）。"""
        assert mask_sensitive("password=123") == "password=123"
        assert mask_sensitive("token=abc") == "token=abc"
        assert mask_sensitive("api_key=k1") == "api_key=k1"

    async def test_does_not_redact_non_sensitive_keys(self) -> None:
        """非敏感 key 不动。"""
        assert mask_sensitive("id=user-abc") == "id=user-abc"
        assert mask_sensitive("port=8080") == "port=8080"
        assert mask_sensitive("host=localhost") == "host=localhost"

    async def test_redacts_multiple_secrets_in_one_string(self) -> None:
        """同串多密钥全部脱敏。"""
        raw = "api_key=sk-aaaabbbbcccc, password=verylongpassword123"
        masked = mask_sensitive(raw)
        assert "sk-aaaabbbbcccc" not in masked
        assert "verylongpassword123" not in masked
        assert "api_key=***" in masked
        assert "password=***" in masked

    async def test_empty_string_unchanged(self) -> None:
        """空串原样返回。"""
        assert mask_sensitive("") == ""

    async def test_no_secrets_unchanged(self) -> None:
        """无密钥文本原样返回。"""
        raw = "Regular log message with no secrets"
        assert mask_sensitive(raw) == raw

    async def test_case_insensitive_key_match(self) -> None:
        """敏感 key 大小写不敏感匹配。"""
        raw = "API_KEY=longapikey12345678"
        masked = mask_sensitive(raw)
        assert "longapikey12345678" not in masked
        assert "***" in masked

    async def test_sensitive_key_parts_constant(self) -> None:
        """SENSITIVE_KEY_PARTS 常量内容锁定。"""
        expected = {
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "authorization",
            "access_token",
            "refresh_token",
        }
        assert set(SENSITIVE_KEY_PARTS) == expected


class TestRegisterSecretMasker:
    async def test_register_is_idempotent(self) -> None:
        """重复注册不崩溃（模块级标志幂等）。"""
        register_secret_masker()
        register_secret_masker()

    async def test_redact_record_masks_message_and_extra(self) -> None:
        """``_redact_record`` 直测：loguru patcher 契约（record dict 传入）。"""
        from packages.harness.core.logging import _redact_record

        record: dict[str, Any] = {
            "message": "api_key=sk-abcdefgh123456 调用失败",
            "extra": {
                "request_id": "req-1",
                "user_id": "sk-secretuserkey998877",
                "count": 3,
                "empty": "",
            },
        }
        _redact_record(record)
        assert record["message"] == "api_key=*** 调用失败"
        assert record["extra"]["request_id"] == "req-1"
        assert record["extra"]["user_id"] == "***"
        assert record["extra"]["count"] == 3
        assert record["extra"]["empty"] == ""

    async def test_redact_record_no_message_noop(self) -> None:
        """空 message / 无 extra 时 no-op（不抛异常）。"""
        from packages.harness.core.logging import _redact_record

        record: dict[str, Any] = {"message": "", "extra": {}}
        _redact_record(record)
        assert record["message"] == ""

    async def test_masker_redacts_via_sink_format(self) -> None:
        """经 sink format 函数脱敏（patcher 之外的双保险通道）。"""
        captured: list[str] = []

        def _sink(message: Any) -> None:
            captured.append(str(message))

        handler_id = logger.add(
            _sink,
            level="INFO",
            format=lambda record: mask_sensitive(record["message"]) + "\n",
        )
        try:
            logger.bind(request_id="-", session_id="-", user_id="-", persona_id="-").info(
                "api_key=sk-abcdefghij1234567890"
            )
        finally:
            logger.remove(handler_id)

        assert len(captured) == 1
        assert "sk-abcdefghij1234567890" not in captured[0]
        assert "***" in captured[0]


class TestParseModuleLogLevels:
    async def test_empty_string_returns_empty_dict(self) -> None:
        """空串 → 空 dict。"""
        assert parse_module_log_levels("") == {}

    async def test_single_module(self) -> None:
        """单条解析。"""
        assert parse_module_log_levels("packages.harness.core=DEBUG") == {
            "packages.harness.core": "DEBUG"
        }

    async def test_multiple_modules(self) -> None:
        """多条解析。"""
        result = parse_module_log_levels("a.module=DEBUG,b.module=WARNING")
        assert result == {"a.module": "DEBUG", "b.module": "WARNING"}

    async def test_level_normalized_to_uppercase(self) -> None:
        """级别小写输入规范化为大写。"""
        assert parse_module_log_levels("a.module=debug") == {"a.module": "DEBUG"}

    async def test_whitespace_stripped(self) -> None:
        """条目与键值的空白剥离。"""
        result = parse_module_log_levels("  a.module = DEBUG  ,  b.api = INFO  ")
        assert result == {"a.module": "DEBUG", "b.api": "INFO"}

    async def test_invalid_level_skipped(self) -> None:
        """非法级别条目跳过。"""
        assert parse_module_log_levels("a.module=INVALID_LEVEL") == {}

    async def test_mixed_valid_invalid_entries(self) -> None:
        """有效与无效混合——仅保留有效条目。"""
        result = parse_module_log_levels("a.valid=DEBUG,a.invalid=BAD,b.valid=ERROR")
        assert result == {"a.valid": "DEBUG", "b.valid": "ERROR"}

    async def test_no_equals_sign_skipped(self) -> None:
        """无 = 的条目跳过。"""
        assert parse_module_log_levels("a.moduleDEBUG") == {}

    async def test_empty_module_or_level_skipped(self) -> None:
        """空模块名 / 空级别的条目跳过（剥离空白后为空）。"""
        assert parse_module_log_levels("=DEBUG") == {}
        assert parse_module_log_levels("a.module=") == {}
        assert parse_module_log_levels(" = ") == {}

    async def test_all_loguru_levels_accepted(self) -> None:
        """loguru 全级别值域合法。"""
        spec = ",".join(f"m{lvl}={lvl}" for lvl in (
            "TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL",
        ))
        result = parse_module_log_levels(spec)
        assert len(result) == 7


class TestMakeModuleFilter:
    async def test_default_level_passes(self) -> None:
        """无覆盖时按默认级别判定（INFO 级 record 放行 INFO 默认）。"""
        module_filter = _make_module_filter({}, "INFO")

        class _Level:
            def __init__(self, no: int) -> None:
                self.no = no

        class _Record(dict[str, Any]):
            def __init__(self, level_no: int) -> None:
                super().__init__(name="some.module", level=_Level(level_no))

        assert module_filter(_Record(20)) is True
        assert module_filter(_Record(10)) is False

    async def test_module_prefix_override(self) -> None:
        """模块前缀覆盖：DEBUG 模块的 TRACE 记录被放行。"""
        module_filter = _make_module_filter({"packages.harness.core": "TRACE"}, "INFO")

        class _Level:
            def __init__(self, no: int) -> None:
                self.no = no

        class _Record(dict[str, Any]):
            def __init__(self, name: str, level_no: int) -> None:
                super().__init__(name=name, level=_Level(level_no))

        assert module_filter(_Record("packages.harness.core.cache", 5)) is True
        assert module_filter(_Record("app.other", 5)) is False

    async def test_longest_prefix_wins(self) -> None:
        """最长前缀优先（更具体的覆盖生效）。"""
        module_filter = _make_module_filter(
            {"a": "ERROR", "a.sub": "TRACE"}, "INFO"
        )

        class _Level:
            def __init__(self, no: int) -> None:
                self.no = no

        class _Record(dict[str, Any]):
            def __init__(self, name: str, level_no: int) -> None:
                super().__init__(name=name, level=_Level(level_no))

        # a.sub 前缀更具体 → TRACE（5）放行
        assert module_filter(_Record("a.sub.module", 5)) is True
        # a 前缀（无 a.sub 命中）→ ERROR（40）拦截 DEBUG（10）
        assert module_filter(_Record("a.plain", 10)) is False

    async def test_exact_module_match(self) -> None:
        """模块名精确匹配（app.api 命中放行；app.apiX 不命中走默认拦截）。"""
        module_filter = _make_module_filter({"app.api": "TRACE"}, "INFO")

        class _Level:
            def __init__(self, no: int) -> None:
                self.no = no

        class _Record(dict[str, Any]):
            def __init__(self, name: str, level_no: int) -> None:
                super().__init__(name=name, level=_Level(level_no))

        assert module_filter(_Record("app.api", 5)) is True
        assert module_filter(_Record("app.apiX", 5)) is False

    async def test_invalid_override_level_falls_back_info(self) -> None:
        """覆盖级别非法（未经 parse 校验直传）→ 按 INFO(20) 兜底判定。"""
        module_filter = _make_module_filter({"app.api": "NOT_A_LEVEL"}, "INFO")

        class _Level:
            def __init__(self, no: int) -> None:
                self.no = no

        class _Record(dict[str, Any]):
            def __init__(self, name: str, level_no: int) -> None:
                super().__init__(name=name, level=_Level(level_no))

        assert module_filter(_Record("app.api", 25)) is True
        assert module_filter(_Record("app.api", 10)) is False


class TestJsonSerializer:
    @staticmethod
    def _make_record(message: str = "hello") -> dict[str, Any]:
        """构造最小 loguru record dict（测试辅助）。"""
        from datetime import UTC, datetime

        class _Level:
            name = "INFO"
            no = 20

        record: dict[str, Any] = {
            "time": datetime(2026, 1, 1, tzinfo=UTC),
            "level": _Level(),
            "name": "test.module",
            "message": message,
            "module": "test.module",
            "function": "test_func",
            "line": 42,
            "exception": None,
            "extra": {
                "request_id": "-",
                "session_id": "-",
                "user_id": "-",
                "persona_id": "-",
            },
        }
        return record

    async def test_payload_fields(self) -> None:
        """JSON 载荷含全链路追踪字段。"""
        line = _json_serializer(self._make_record("hello"))
        payload = json.loads(line)
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.module"
        assert payload["module"] == "test.module"
        assert payload["function"] == "test_func"
        assert payload["line"] == 42
        assert payload["request_id"] == "-"
        assert payload["session_id"] == "-"
        assert payload["user_id"] == "-"
        assert payload["persona_id"] == "-"

    async def test_message_like_object_compat(self) -> None:
        """Message 形对象（带 .record 属性）兼容路径。"""

        class _FakeMessage:
            def __init__(self, record: dict[str, Any]) -> None:
                self.record = record

        line = _json_serializer(_FakeMessage(self._make_record("wrapped")))
        assert json.loads(line)["message"] == "wrapped"

    async def test_exception_section(self) -> None:
        """exception 段携带类型与值。"""

        class _Exc:
            value = ValueError("boom")

        record = self._make_record("failing")
        record["exception"] = _Exc()
        payload = json.loads(_json_serializer(record))
        assert payload["exception"] == {"type": "ValueError", "value": "boom"}

    async def test_extra_passthrough_keys(self) -> None:
        """extra 的非上下文键透传进 payload。"""
        record = self._make_record("extra")
        record["extra"] = {
            "request_id": "req-1",
            "session_id": "-",
            "user_id": "-",
            "persona_id": "-",
            "custom_field": "custom-value",
        }
        payload = json.loads(_json_serializer(record))
        assert payload["request_id"] == "req-1"
        assert payload["custom_field"] == "custom-value"


class TestSetupLogging:
    async def test_three_sinks_write_files(
        self, logging_config: LoggingConfig, tmp_path: Path
    ) -> None:
        """setup_logging 后：文本 + JSON 文件 sink 落盘可读。"""
        setup_logging(logging_config)
        logger.info("初始化验证消息")

        log_file = Path(logging_config.log_file)
        json_file = Path(logging_config.log_json_file)
        assert log_file.exists()
        assert json_file.exists()
        text_content = log_file.read_text(encoding="utf-8")
        assert "初始化验证消息" in text_content
        assert "req=-" in text_content  # 默认 extra 占位注入

    async def test_json_sink_lines_parseable(
        self, logging_config: LoggingConfig
    ) -> None:
        """JSON 文件每行可解析为 dict（聚合系统契约）。"""
        setup_logging(logging_config)
        logger.bind(request_id="req-42", persona_id="persona-7").info("JSON 验证消息")

        json_file = Path(logging_config.log_json_file)
        lines = [ln for ln in json_file.read_text(encoding="utf-8").splitlines() if ln]
        payloads = [json.loads(ln) for ln in lines]
        target = [p for p in payloads if p["message"] == "JSON 验证消息"]
        assert len(target) == 1
        assert target[0]["request_id"] == "req-42"
        assert target[0]["persona_id"] == "persona-7"

    async def test_json_sink_masks_secrets(self, logging_config: LoggingConfig) -> None:
        """JSON sink 消息脱敏（format callable 通道）。"""
        setup_logging(logging_config)
        logger.info("调用端点 api_key=sk-abcdefgh123456 结束")

        json_file = Path(logging_config.log_json_file)
        content = json_file.read_text(encoding="utf-8")
        assert "sk-abcdefgh123456" not in content
        assert "api_key=***" in content

    async def test_json_sink_masks_extra_values(self, logging_config: LoggingConfig) -> None:
        """JSON sink 的 extra 字符串值脱敏（上下文轴携带密钥形态时）。"""
        setup_logging(logging_config)
        logger.bind(request_id="sk-boundsecret123456").info("上下文轴脱敏验证")

        json_file = Path(logging_config.log_json_file)
        lines = [ln for ln in json_file.read_text(encoding="utf-8").splitlines() if ln]
        payload = json.loads(lines[-1])
        assert payload["request_id"] == "***"

    async def test_text_sink_masks_secrets(self, logging_config: LoggingConfig) -> None:
        """文本 sink 消息脱敏（text formatter 通道）。"""
        setup_logging(logging_config)
        logger.info("password=supersecret1234 泄漏测试")

        log_file = Path(logging_config.log_file)
        content = log_file.read_text(encoding="utf-8")
        assert "supersecret1234" not in content
        assert "password=***" in content

    async def test_custom_text_format_applied(self, logging_config: LoggingConfig) -> None:
        """自定义 text_format 生效（console 与文本文件共用 resolved 模板）。"""
        config = logging_config.model_copy(
            update={"text_format": "CUSTOM|{name}|{message}"}
        )
        setup_logging(config)
        logger.info("自定义模板消息")

        log_file = Path(config.log_file)
        content = log_file.read_text(encoding="utf-8")
        custom_lines = [ln for ln in content.splitlines() if ln.startswith("CUSTOM|")]
        assert custom_lines, "应存在 CUSTOM| 前缀的日志行"
        assert any("自定义模板消息" in ln for ln in custom_lines)
        # 自定义模板替换默认模板——上下文段（req=/session=）不再出现
        assert all("req=" not in ln for ln in custom_lines)

    async def test_module_levels_filter_applied(
        self, logging_config: LoggingConfig
    ) -> None:
        """module_levels 覆盖：被降级模块的 INFO 不落 JSON 文件。"""
        # loguru record 的 name = 调用方模块 __name__——动态取本测试模块名，
        # 避免依赖 pytest 导入模式的模块路径形态。
        this_module = __name__
        config = logging_config.model_copy(
            update={"module_levels": f"{this_module}=ERROR", "level": "DEBUG"}
        )
        setup_logging(config)
        logger.info("这条不应出现在 JSON 文件（本模块被降级到 ERROR）")
        logger.error("这条应出现在 JSON 文件")

        json_file = Path(config.log_json_file)
        content = json_file.read_text(encoding="utf-8")
        assert "这条不应出现在 JSON 文件" not in content
        assert "这条应出现在 JSON 文件" in content


class TestLoggingConfig:
    async def test_defaults(self) -> None:
        """默认值：INFO / 每日轮转 / 30 天保留。"""
        config = LoggingConfig()
        assert config.level == "INFO"
        assert config.rotation == "1 day"
        assert config.retention == "30 days"
        assert config.log_file == "logs/app.log"
        assert config.log_json_file == "logs/app.json.log"
        assert config.module_levels == ""
        assert config.text_format is None
        assert config.colorize_console is True

    async def test_level_normalized_and_validated(self) -> None:
        """级别规范化大写 + 非法值拒绝。"""
        assert LoggingConfig(level="debug").level == "DEBUG"
        with pytest.raises(ValueError, match="非法日志级别"):
            LoggingConfig(level="NOISY")

    async def test_empty_log_file_rejected(self) -> None:
        """空日志路径拒绝。"""
        with pytest.raises(ValueError, match="不能为空"):
            LoggingConfig(log_file="   ")

    async def test_default_text_format_constant(self) -> None:
        """默认文本模板含四上下文轴占位（与 configure extra 对齐）。"""
        assert "{extra[request_id]}" in DEFAULT_TEXT_FORMAT
        assert "{extra[session_id]}" in DEFAULT_TEXT_FORMAT
        assert "{extra[user_id]}" in DEFAULT_TEXT_FORMAT
        assert "{extra[persona_id]}" in DEFAULT_TEXT_FORMAT
