"""harness/config loader 测试——分层加载 / 运行时状态 / 热重载回调 / 异常路径。

全部经 ``tmp_path`` 配置目录注入（config_dir 参数），零真实配置依赖。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from packages.harness.config import loader
from packages.harness.config.loader import (
    load_all_config,
    load_config_yaml,
    load_extensions_config,
    register_reload_callback,
    reset_config_runtime_state,
    setup_logging_from_settings,
)
from packages.harness.config.model_resolver import get_model_resolver
from packages.harness.config.models import DatabaseConfig, LogConfig
from packages.harness.config.multimodal_fallback_loader import resolve_multimodal_chain
from packages.harness.config.settings import build_isolated_settings
from packages.harness.core.errors import BusinessException, ErrorCode


def _write_config(
    config_dir: Path,
    *,
    yaml_payload: dict[str, object] | None = None,
    yaml_text: str | None = None,
    extensions_payload: dict[str, object] | None = None,
    include_yaml: bool = True,
    include_extensions: bool = True,
) -> None:
    """写入测试配置文件（yaml_payload / yaml_text 二选一；控制文件存在性）。"""
    if include_yaml:
        text = yaml_text if yaml_text is not None else yaml.dump(yaml_payload, allow_unicode=True)
        (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    if include_extensions:
        import json

        payload = (
            extensions_payload
            if extensions_payload is not None
            else {
                "tools": [
                    {
                        "name": "t1",
                        "description": "测试工具",
                        "parameters": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "required": ["a"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        )
        (config_dir / "extensions_config.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _sample_llm_payload() -> dict[str, object]:
    """标准样例 llm 段（1 条目 + 角色绑定 + 回退链）。"""
    return {
        "llm": {
            "models": [
                {
                    "name": "main",
                    "base_url": "http://h/v1",
                    "api_key_env": "MAIN_KEY",
                    "model": "m1",
                    "roles": ["chat"],
                },
                {
                    "name": "vision",
                    "base_url": "http://v/v1",
                    "model": "mv",
                    "roles": ["look_at"],
                    "multimodal": True,
                },
            ],
            "role_bindings": {"duty": "main"},
            "multimodal_fallback": {
                "local": {"name": "lv", "base_url": "http://lv/v1", "model": "lmv"},
                "remote": {"name": "rv", "base_url": "http://rv/v1", "model": "rmv"},
            },
        }
    }


async def test_load_config_yaml_populates_runtime_state(tmp_path: Path) -> None:
    """加载成功：runtime_llm_config / 解析器索引 / 回退链记录三面同步。"""
    _write_config(tmp_path, yaml_payload=_sample_llm_payload())
    config = load_config_yaml(config_dir=tmp_path)

    assert config is loader.runtime_llm_config
    assert len(config.models) == 2
    assert config.role_bindings == {"duty": "main"}
    assert config.multimodal_fallback.local is not None
    assert config.multimodal_fallback.local.name == "lv"

    resolver = get_model_resolver()
    assert resolver.resolve("chat").name == "main"
    assert resolver.resolve("duty").name == "main"  # 显式绑定
    assert resolver.resolve("look_at").name == "vision"

    # 非多模态 chat 条目 → 回退链可用（loader → fallback loader 联动）
    chain = resolve_multimodal_chain(resolver.resolve("chat"))
    assert [entry.name for entry in chain] == ["lv", "rv"]


async def test_load_config_yaml_missing_file_keeps_defaults(tmp_path: Path) -> None:
    """文件缺失为合法状态：返回当前运行时值（初始空注册表）。"""
    reset_config_runtime_state()
    config = load_config_yaml(config_dir=tmp_path)
    assert config.models == []
    assert config.role_bindings == {}


async def test_load_config_yaml_malformed_yaml_raises(tmp_path: Path) -> None:
    """YAML 语法错误 → CONFIG_INVALID fail loud。"""
    _write_config(tmp_path, yaml_text="llm: [unclosed")
    with pytest.raises(BusinessException) as exc_info:
        load_config_yaml(config_dir=tmp_path)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_load_config_yaml_non_mapping_top_level_raises(tmp_path: Path) -> None:
    """顶层非 mapping → CONFIG_INVALID。"""
    _write_config(tmp_path, yaml_text="- just\n- a\n- list\n")
    with pytest.raises(BusinessException):
        load_config_yaml(config_dir=tmp_path)


async def test_load_config_yaml_llm_section_not_mapping_raises(tmp_path: Path) -> None:
    """llm 段非 mapping → CONFIG_INVALID。"""
    _write_config(tmp_path, yaml_text="llm: [1, 2]\n")
    with pytest.raises(BusinessException):
        load_config_yaml(config_dir=tmp_path)


async def test_load_config_yaml_models_not_list_raises(tmp_path: Path) -> None:
    """llm.models 非 list → CONFIG_INVALID。"""
    _write_config(tmp_path, yaml_payload={"llm": {"models": {"name": "x"}}})
    with pytest.raises(BusinessException) as exc_info:
        load_config_yaml(config_dir=tmp_path)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_load_config_yaml_invalid_entry_raises(tmp_path: Path) -> None:
    """模型条目非法（空 name）→ CONFIG_INVALID（pydantic 校验包装）。"""
    _write_config(
        tmp_path,
        yaml_payload={"llm": {"models": [{"name": "", "base_url": "http://h/v1", "model": "m"}]}},
    )
    with pytest.raises(BusinessException) as exc_info:
        load_config_yaml(config_dir=tmp_path)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_load_config_yaml_invalid_role_binding_raises(tmp_path: Path) -> None:
    """role_bindings 含未知角色 → ValidationError 包装为 CONFIG_INVALID。"""
    _write_config(
        tmp_path,
        yaml_payload={"llm": {"models": [], "role_bindings": {"wizard": "x"}}},
    )
    with pytest.raises(BusinessException) as exc_info:
        load_config_yaml(config_dir=tmp_path)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_load_config_yaml_unknown_top_keys_warned_not_fatal(tmp_path: Path) -> None:
    """未识别顶层键：告警但不 fatal（前向兼容）。"""
    from loguru import logger

    captured: list[str] = []
    handler_id = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    try:
        _write_config(
            tmp_path,
            yaml_payload={"future_section": {"x": 1}, **_sample_llm_payload()},
        )
        config = load_config_yaml(config_dir=tmp_path)
    finally:
        logger.remove(handler_id)
    assert len(config.models) == 2
    assert any("future_section" in message for message in captured)


async def test_load_config_yaml_empty_file_treated_as_empty(tmp_path: Path) -> None:
    """空文件（None 顶层）→ 空配置（合法）。"""
    _write_config(tmp_path, yaml_text="")
    config = load_config_yaml(config_dir=tmp_path)
    assert config.models == []


async def test_load_extensions_config_parses_tools(tmp_path: Path) -> None:
    """extensions 加载：tools 结构化（JsonSchema 字段 + alias 兼容）。"""
    _write_config(tmp_path)
    config = load_extensions_config(config_dir=tmp_path)
    assert len(config.tools) == 1
    tool = config.tools[0]
    assert tool.name == "t1"
    assert tool.parameters.type == "object"
    assert tool.parameters.required == ["a"]
    assert tool.parameters.additional_properties is False
    assert config is loader.extensions_config


async def test_load_extensions_config_missing_file_keeps_defaults(tmp_path: Path) -> None:
    """文件缺失：返回当前运行时值。"""
    reset_config_runtime_state()
    config = load_extensions_config(config_dir=tmp_path)
    assert config.tools == []


async def test_load_extensions_config_malformed_json_raises(tmp_path: Path) -> None:
    """JSON 语法错误 → CONFIG_INVALID。"""
    (tmp_path / "extensions_config.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(BusinessException) as exc_info:
        load_extensions_config(config_dir=tmp_path)
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_load_extensions_config_non_mapping_raises(tmp_path: Path) -> None:
    """顶层非 mapping → CONFIG_INVALID。"""
    (tmp_path / "extensions_config.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(BusinessException):
        load_extensions_config(config_dir=tmp_path)


async def test_load_all_config_notifies_reload_callbacks(tmp_path: Path) -> None:
    """load_all_config 完成后通知全部重载回调（热重载地基）。"""
    calls: list[str] = []
    register_reload_callback(lambda: calls.append("cb1"))

    def _boom() -> None:
        raise RuntimeError("cb2 failed")

    register_reload_callback(_boom)
    register_reload_callback(lambda: calls.append("cb3"))

    _write_config(tmp_path, yaml_payload=_sample_llm_payload())
    llm, extensions = load_all_config(config_dir=tmp_path)

    assert len(llm.models) == 2
    assert len(extensions.tools) == 1
    # 单回调失败不阻断后续回调（best-effort）
    assert calls == ["cb1", "cb3"]


async def test_load_all_config_default_paths_missing_files_ok() -> None:
    """默认路径（CWD scratch）无文件 → 空配置合法返回。"""
    reset_config_runtime_state()
    llm, extensions = load_all_config()
    assert llm.models == []
    assert extensions.tools == []


async def test_reset_config_runtime_state_full_reset() -> None:
    """复位：llm 段 / 工具清单 / 解析器 / 回退链 / 回调全清。"""
    _write_register_full_state()
    reset_config_runtime_state()
    assert loader.runtime_llm_config.models == []
    assert loader.extensions_config.tools == []
    assert get_model_resolver().bound_roles == []


def _write_register_full_state() -> None:
    """向运行时注入非空状态（复位测试的起点）。"""
    from packages.harness.config.model_entry import parse_model_entry
    from packages.harness.config.model_resolver import rebuild_model_resolver

    entry = parse_model_entry(
        {"name": "x", "base_url": "http://x/v1", "model": "mx", "roles": ["chat"]}
    )
    rebuild_model_resolver([entry])


async def test_setup_logging_from_settings_wires_core(tmp_path: Path) -> None:
    """Settings.log → core setup_logging 接线（字段映射 + 日志文件落盘）。"""
    from loguru import logger

    log = LogConfig(
        level="INFO",
        file=str(tmp_path / "logs" / "wired.log"),
        json_file=str(tmp_path / "logs" / "wired.json.log"),
        colorize_console=False,
    )
    settings = build_isolated_settings(
        database=DatabaseConfig(type="sqlite", name=":memory:"), log=log
    )
    setup_logging_from_settings(settings)
    logger.info("接线验证日志行")
    assert (tmp_path / "logs" / "wired.log").exists()
    assert (tmp_path / "logs" / "wired.json.log").exists()
