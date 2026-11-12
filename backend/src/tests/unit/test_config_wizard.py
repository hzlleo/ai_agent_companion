"""harness/config wizard 六件套测试——解析 / 生成 / 注入式应答主流程 / CLI。

测试驱动方式：注入式应答（``answers`` 参数 + ``input_fn`` / ``secret_fn``
注入点 / ``--non-interactive``），永不真实交互。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.harness.config import wizard as wizard_module
from packages.harness.config.wizard import (
    CONFIG_EXAMPLE,
    ENV_EXAMPLE,
    EXTENSIONS_EXAMPLE,
    _prompt_field,
    main,
    run_wizard,
)
from packages.harness.config.wizard_fields import (
    EnvField,
    _is_bool_value,
    _is_numeric_value,
    _is_sensitive_key,
    stringify_env_value,
)
from packages.harness.config.wizard_generators import (
    build_setup_schema,
    generate_env_content,
    mask_sensitive,
    write_config_files,
)
from packages.harness.config.wizard_parsers import (
    parse_env_template,
    parse_json_template,
    parse_yaml_template,
)
from packages.harness.config.wizard_ui import (
    _FIELD_LABELS,
    _Color,
    _print_error,
    _print_header,
    _print_info,
    _print_step,
    _print_success,
)

# ---------------------------------------------------------------------------
# 模板文件前置断言（gen-examples 产物必须存在——wizard 依赖三件套）
# ---------------------------------------------------------------------------


async def test_example_templates_exist() -> None:
    """三件套 .example 模板存在（gen-examples 产物契约）。"""
    assert ENV_EXAMPLE.is_file()
    assert CONFIG_EXAMPLE.is_file()
    assert EXTENSIONS_EXAMPLE.is_file()


# ---------------------------------------------------------------------------
# wizard_fields 纯函数
# ---------------------------------------------------------------------------


async def test_is_sensitive_key_matches() -> None:
    """敏感关键词（password/secret/token/api_key）命中返回 True。"""
    assert _is_sensitive_key("DATABASE_PASSWORD") is True
    assert _is_sensitive_key("LLM_API_KEY") is True
    assert _is_sensitive_key("AUTH_SECRET") is True
    assert _is_sensitive_key("ACCESS_TOKEN") is True
    assert _is_sensitive_key("SERVER_HOST") is False
    assert _is_sensitive_key("ENV") is False


async def test_is_bool_value_and_numeric() -> None:
    """布尔字面量 / 纯数字判定（大小写不敏感、含负数）。"""
    assert _is_bool_value("true") is True
    assert _is_bool_value("FALSE") is True
    assert _is_bool_value("1") is False
    assert _is_numeric_value("8000") is True
    assert _is_numeric_value("-1") is True
    assert _is_numeric_value("5.0") is False
    assert _is_numeric_value("") is False
    assert _is_numeric_value("abc") is False


async def test_stringify_env_value() -> None:
    """值字符串化：bool 小写、其他 str()。"""
    assert stringify_env_value(True) == "true"
    assert stringify_env_value(False) == "false"
    assert stringify_env_value(8000) == "8000"
    assert stringify_env_value("x") == "x"


async def test_env_field_type_inference() -> None:
    """EnvField 字段类型推断：password / boolean / number / text。"""
    assert EnvField(key="DATABASE_PASSWORD", default="x").field_type == "password"
    assert EnvField(key="DATABASE_POOL_PRE_PING", default="true").field_type == "boolean"
    assert EnvField(key="SERVER_PORT", default="8000").field_type == "number"
    assert EnvField(key="SERVER_HOST", default="127.0.0.1").field_type == "text"
    assert EnvField(key="DATABASE_PASSWORD", default="x").is_sensitive is True
    assert EnvField(key="SERVER_PORT", default="8000").is_sensitive is False


async def test_env_field_label_fallback_to_key() -> None:
    """标签缺失时回退 key 本身。"""
    field = EnvField(key="NEVER_REGISTERED_KEY", default="")
    assert field.label == "NEVER_REGISTERED_KEY"


# ---------------------------------------------------------------------------
# wizard_parsers 模板解析
# ---------------------------------------------------------------------------


async def test_parse_env_template_extracts_fields_in_order(tmp_path: Path) -> None:
    """解析 .env 模板：KEY=value 行 + 注释跟踪 + 空行/分隔线重置。"""
    template = tmp_path / "tpl.env"
    template.write_text(
        "# 头部注释\n"
        "# ============\n"
        "SERVER_HOST=127.0.0.1\n"
        "\n"
        "# 端口说明\n"
        "SERVER_PORT=8000\n"
        "# DATABASE_PASSWORD=\n",
        encoding="utf-8",
    )
    fields = parse_env_template(template)
    assert [f.key for f in fields] == ["SERVER_HOST", "SERVER_PORT"]
    assert fields[0].default == "127.0.0.1"
    # 分隔线重置注释 → SERVER_HOST 无注释；端口说明紧邻 key → 命中
    assert fields[0].comment == ""
    assert fields[1].comment == "端口说明"


async def test_parse_env_template_missing_file_raises(tmp_path: Path) -> None:
    """模板不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        parse_env_template(tmp_path / "nope.env")


async def test_parse_yaml_template_returns_dict() -> None:
    """解析 YAML 模板为顶层 dict。"""
    data = parse_yaml_template(CONFIG_EXAMPLE)
    assert isinstance(data, dict)
    assert "llm" in data


async def test_parse_yaml_template_invalid_structure_raises(tmp_path: Path) -> None:
    """YAML 顶层非 dict → ValueError。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected dict"):
        parse_yaml_template(bad)


async def test_parse_json_template_returns_dict() -> None:
    """解析 JSON 模板为顶层 dict。"""
    data = parse_json_template(EXTENSIONS_EXAMPLE)
    assert isinstance(data, dict)
    assert "tools" in data


async def test_parse_json_template_invalid_structure_raises(tmp_path: Path) -> None:
    """JSON 顶层非 dict → ValueError。"""
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected dict"):
        parse_json_template(bad)


# ---------------------------------------------------------------------------
# wizard_generators 生成与写入
# ---------------------------------------------------------------------------


async def test_generate_env_content_replaces_values_only() -> None:
    """env 内容生成：仅替换命中 key，模板结构（注释/空行）保留。"""
    template = ENV_EXAMPLE
    values = {"SERVER_PORT": "9000", "DATABASE_TYPE": "sqlite"}
    content = generate_env_content(template, values)
    lines = content.splitlines()
    joined = "\n".join(lines)
    assert "SERVER_PORT=9000" in joined
    assert "DATABASE_TYPE=sqlite" in joined
    assert "SERVER_PORT=8000" not in joined
    # 未命中的 key 保留模板默认
    assert "SERVER_WORKERS=1" in joined
    # 注释保留
    assert "# 服务监听端口" in joined


async def test_generate_env_content_trailing_newline(tmp_path: Path) -> None:
    """生成内容恒以换行结尾（无尾随换行的模板补齐）。"""
    template = tmp_path / "t.env"
    template.write_text("A=1", encoding="utf-8")
    assert generate_env_content(template, {"A": "2"}).endswith("\n")


async def test_mask_sensitive_masks_only_sensitive() -> None:
    """敏感值打码（摘要展示用）；空值敏感字段不打码。"""
    masked = mask_sensitive(
        {
            "DATABASE_PASSWORD": "secret-value",
            "LLM_API_KEY": "",
            "SERVER_PORT": "8000",
        }
    )
    assert masked == {"DATABASE_PASSWORD": "***", "LLM_API_KEY": "", "SERVER_PORT": "8000"}


async def test_build_setup_schema_groups_steps() -> None:
    """schema 构建：按步骤分组 + config_files 三件套映射。"""
    schema = build_setup_schema()
    step_names = [step.name for step in schema.steps]
    assert "服务配置" in step_names
    assert "数据库配置" in step_names
    assert "LLM 端点配置" in step_names

    db_step = next(step for step in schema.steps if step.name == "数据库配置")
    keys = {field.key for field in db_step.fields}
    assert "DATABASE_TYPE" in keys
    assert "DATABASE_HOST" in keys

    assert schema.config_files["env"].template == ".env.example"
    assert schema.config_files["config"].output == "config.yaml"
    assert schema.config_files["extensions"].output == "extensions_config.json"


async def test_write_config_files_dry_run_no_write(tmp_path: Path) -> None:
    """dry_run：返回内容预览但不落盘。"""
    out_env = tmp_path / "out.env"
    result = write_config_files(
        {"SERVER_PORT": "9000"},
        dry_run=True,
        env_template=ENV_EXAMPLE,
        config_template=CONFIG_EXAMPLE,
        extensions_template=EXTENSIONS_EXAMPLE,
        env_output=out_env,
        config_output=tmp_path / "out.yaml",
        extensions_output=tmp_path / "out.json",
    )
    assert set(result) == {".env", "config.yaml", "extensions_config.json"}
    assert "SERVER_PORT=9000" in result[".env"]
    assert not out_env.exists()


async def test_write_config_files_writes_three_files(tmp_path: Path) -> None:
    """写入三件套：.env 值替换 + yaml/json 模板复制。"""
    out_env = tmp_path / "out.env"
    out_yaml = tmp_path / "out.yaml"
    out_json = tmp_path / "out.json"
    write_config_files(
        {"DATABASE_TYPE": "sqlite"},
        env_template=ENV_EXAMPLE,
        config_template=CONFIG_EXAMPLE,
        extensions_template=EXTENSIONS_EXAMPLE,
        env_output=out_env,
        config_output=out_yaml,
        extensions_output=out_json,
    )
    assert "DATABASE_TYPE=sqlite" in out_env.read_text(encoding="utf-8")
    assert out_yaml.read_text(encoding="utf-8") == CONFIG_EXAMPLE.read_text(encoding="utf-8")
    assert out_json.read_text(encoding="utf-8") == EXTENSIONS_EXAMPLE.read_text(encoding="utf-8")


async def test_write_config_files_env_key_not_in_template_ignored(tmp_path: Path) -> None:
    """模板中不存在的 key 不注入（白名单语义）。"""
    out_env = tmp_path / "out.env"
    write_config_files(
        {"HACKED_KEY": "x"},
        env_template=ENV_EXAMPLE,
        config_template=CONFIG_EXAMPLE,
        extensions_template=EXTENSIONS_EXAMPLE,
        env_output=out_env,
        config_output=tmp_path / "y.yaml",
        extensions_output=tmp_path / "z.json",
    )
    assert "HACKED_KEY" not in out_env.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _prompt_field 注入式交互
# ---------------------------------------------------------------------------


async def test_prompt_field_non_interactive_returns_current() -> None:
    """非交互模式：直接返回当前值。"""
    field = EnvField(key="SERVER_PORT", default="8000")
    assert _prompt_field(field, "7000", True) == "7000"


async def test_prompt_field_text_input_and_empty_keeps_current() -> None:
    """文本字段：输入新值生效；空输入保留当前值。"""
    field = EnvField(key="SERVER_HOST", default="127.0.0.1")
    assert _prompt_field(field, "old", False, input_fn=lambda _: "new-host") == "new-host"
    assert _prompt_field(field, "old", False, input_fn=lambda _: "") == "old"


async def test_prompt_field_boolean_y_n_semantics() -> None:
    """布尔字段：y/yes → true；其他 → false；空输入保留当前值。"""
    field = EnvField(key="DATABASE_POOL_PRE_PING", default="true")
    assert _prompt_field(field, "true", False, input_fn=lambda _: "y") == "true"
    assert _prompt_field(field, "true", False, input_fn=lambda _: "yes") == "true"
    assert _prompt_field(field, "true", False, input_fn=lambda _: "n") == "false"
    assert _prompt_field(field, "true", False, input_fn=lambda _: "") == "true"
    assert _prompt_field(field, "false", False, input_fn=lambda _: "") == "false"


async def test_prompt_field_number_rejects_non_numeric() -> None:
    """数字字段：非数字输入被忽略，保留默认值。"""
    field = EnvField(key="SERVER_PORT", default="8000")
    assert _prompt_field(field, "8000", False, input_fn=lambda _: "not-a-num") == "8000"
    assert _prompt_field(field, "8000", False, input_fn=lambda _: "9000") == "9000"


async def test_prompt_field_sensitive_uses_secret_fn_and_masks_display() -> None:
    """敏感字段：secret_fn 注入 + 当前值显示打码。"""
    field = EnvField(key="DATABASE_PASSWORD", default="real-secret")
    prompts: list[str] = []

    def _secret(prompt: str) -> str:
        prompts.append(prompt)
        return "new-secret"

    assert _prompt_field(field, "real-secret", False, secret_fn=_secret) == "new-secret"
    assert prompts, "secret_fn must be invoked"
    assert "real-secret" not in prompts[0]
    assert "***" in prompts[0]


async def test_prompt_field_sensitive_empty_current_no_mask_marker() -> None:
    """敏感字段当前值为空：提示不含 [当前: ***] 段。"""
    field = EnvField(key="DATABASE_PASSWORD", default="")
    prompts: list[str] = []

    def _secret(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    assert _prompt_field(field, "", False, secret_fn=_secret) == ""
    assert "[当前:" not in prompts[0]


# ---------------------------------------------------------------------------
# run_wizard 主流程（注入式应答 / dry-run / 取消）
# ---------------------------------------------------------------------------


async def test_run_wizard_dry_run_previews_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """dry-run：打印预览，不写任何文件。"""
    out_env = tmp_path / "w.env"
    monkeypatch.setattr(wizard_module, "ENV_FILE", out_env)
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", tmp_path / "w.yaml")
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", tmp_path / "w.json")

    run_wizard(dry_run=True, non_interactive=True)
    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert not out_env.exists()


async def test_run_wizard_non_interactive_writes_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非交互：按模板默认值写三件套。"""
    out_env = tmp_path / "w.env"
    out_yaml = tmp_path / "w.yaml"
    out_json = tmp_path / "w.json"
    monkeypatch.setattr(wizard_module, "ENV_FILE", out_env)
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", out_yaml)
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", out_json)

    run_wizard(non_interactive=True)
    env_text = out_env.read_text(encoding="utf-8")
    assert "SERVER_PORT=8000" in env_text
    assert out_yaml.exists()
    assert out_json.exists()


async def test_run_wizard_injected_answers_flow_to_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入式应答：answers 中的值流入 .env（测试驱动主路径）。"""
    out_env = tmp_path / "w.env"
    monkeypatch.setattr(wizard_module, "ENV_FILE", out_env)
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", tmp_path / "w.yaml")
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", tmp_path / "w.json")

    run_wizard(
        non_interactive=False,
        answers={"DATABASE_TYPE": "mysql", "SERVER_PORT": "9100"},
        confirm="y",
    )
    env_text = out_env.read_text(encoding="utf-8")
    assert "DATABASE_TYPE=mysql" in env_text
    assert "SERVER_PORT=9100" in env_text


async def test_run_wizard_existing_env_used_as_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有 .env：现有值作为默认值（空应答回退现有值而非模板值）。"""
    existing = tmp_path / "w.env"
    existing.write_text("SERVER_PORT=7777\n", encoding="utf-8")
    monkeypatch.setattr(wizard_module, "ENV_FILE", existing)
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", tmp_path / "w.yaml")
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", tmp_path / "w.json")

    run_wizard(non_interactive=True)
    env_text = existing.read_text(encoding="utf-8")
    # 已有值保留（非交互模式全部保留当前值）
    assert "SERVER_PORT=7777" in env_text


async def test_run_wizard_confirm_declined_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """确认拒绝：不写入任何文件。"""
    out_env = tmp_path / "w.env"
    monkeypatch.setattr(wizard_module, "ENV_FILE", out_env)
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", tmp_path / "w.yaml")
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", tmp_path / "w.json")

    run_wizard(non_interactive=False, answers={}, confirm="n")
    captured = capsys.readouterr()
    assert "已取消" in captured.out
    assert not out_env.exists()


async def test_run_wizard_missing_template_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模板缺失：sys.exit(1) fail fast。"""
    monkeypatch.setattr(wizard_module, "ENV_EXAMPLE", tmp_path / "nope.env")
    with pytest.raises(SystemExit) as exc_info:
        run_wizard(non_interactive=True)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# main CLI 入口
# ---------------------------------------------------------------------------


async def test_main_help_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """CLI --help：正常打印用法并退出 0（非交互 smoke 契约）。"""
    monkeypatch.setattr("sys.argv", ["wizard.py", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "ai_agent_companion 配置向导" in captured.out


async def test_main_dry_run_flag_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """CLI --dry-run 分发到 run_wizard(dry_run=True)。"""
    monkeypatch.setattr("sys.argv", ["wizard.py", "--dry-run"])
    monkeypatch.setattr(wizard_module, "ENV_FILE", tmp_path / "w.env")
    monkeypatch.setattr(wizard_module, "CONFIG_FILE", tmp_path / "w.yaml")
    monkeypatch.setattr(wizard_module, "EXTENSIONS_FILE", tmp_path / "w.json")
    main()
    assert "dry-run" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# wizard_ui 输出辅助
# ---------------------------------------------------------------------------


async def test_print_helpers_output(capsys: pytest.CaptureFixture) -> None:
    """_print_* 输出含预期标记文本。"""
    _print_header()
    _print_step("步骤X")
    _print_info("信息Y")
    _print_success("成功Z")
    _print_error("错误W")
    out = capsys.readouterr().out
    assert "ai_agent_companion — 配置向导" in out
    assert "步骤X" in out
    assert "信息Y" in out
    assert "成功Z" in out
    assert "错误W" in out


async def test_color_constants_non_empty() -> None:
    """ANSI 颜色常量非空（终端输出契约）。"""
    assert _Color.GREEN
    assert _Color.RESET


async def test_field_labels_introspected_from_settings() -> None:
    """字段标签从 BaseSettings Field(description=...) 内省（env 名对齐）。"""
    assert _FIELD_LABELS["SERVER_HOST"] == "服务监听地址"
    assert _FIELD_LABELS["DATABASE_TYPE"] is not None
    assert _FIELD_LABELS["ENV"] is not None
