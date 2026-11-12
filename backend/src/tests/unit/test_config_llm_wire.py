"""harness/config llm_wire 测试——wire 构建 / 密钥解析收口。"""

from __future__ import annotations

import pytest

from packages.harness.config.llm_wire import LlmClientConfigModel, build_llm_wire_config
from packages.harness.config.model_entry import LlmModelEntry, parse_model_entry


def _entry(**overrides: object) -> LlmModelEntry:
    """构造测试条目（默认字段可覆盖）。

    Returns:
        归一化后的 :class:`LlmModelEntry`。
    """
    raw: dict[str, object] = {
        "name": "e",
        "base_url": "http://h/v1",
        "model": "m",
    }
    raw.update(overrides)
    return parse_model_entry(raw)


async def test_build_wire_full_field_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """全字段映射：条目 → wire（provider/base_url/model/concurrency/multimodal）。"""
    monkeypatch.setenv("WIRE_TEST_KEY", "sk-wire-123")
    entry = _entry(
        api_key_env="WIRE_TEST_KEY",
        concurrency=3,
        multimodal=True,
        provider="openai_compatible",
    )
    wire = build_llm_wire_config(entry)
    assert isinstance(wire, LlmClientConfigModel)
    assert wire.provider == "openai_compatible"
    assert wire.base_url == "http://h/v1"
    assert wire.model == "m"
    assert wire.api_key == "sk-wire-123"
    assert wire.concurrency == 3
    assert wire.multimodal is True


async def test_build_wire_resolves_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥解析收口：wire 构建即从环境变量解析明文。"""
    monkeypatch.setenv("WIRE_TEST_KEY2", "sk-xyz")
    wire = build_llm_wire_config(_entry(api_key_env="WIRE_TEST_KEY2"))
    assert wire.api_key == "sk-xyz"


async def test_build_wire_missing_env_uses_fallback() -> None:
    """条目声明了密钥变量但变量未设置 → 显式兜底明文生效。"""
    wire = build_llm_wire_config(_entry(api_key_env="WIRE_NEVER_SET_KEY"), fallback_api_key="sk-fb")
    assert wire.api_key == "sk-fb"


async def test_build_wire_keyless_entry_ignores_fallback() -> None:
    """免鉴权条目（api_key_env 空）不注入兜底密钥（显式免鉴权语义）。"""
    wire = build_llm_wire_config(_entry(api_key_env=""), fallback_api_key="sk-fb")
    assert wire.api_key == ""


async def test_build_wire_keyless_entry_with_unset_env_no_fallback() -> None:
    """声明密钥变量但未设置且无兜底 → 空密钥（网关侧按需判定缺密钥）。"""
    wire = build_llm_wire_config(_entry(api_key_env="WIRE_NEVER_SET_KEY2"))
    assert wire.api_key == ""


async def test_build_wire_defaults() -> None:
    """最小条目 wire：concurrency None / multimodal False。"""
    wire = build_llm_wire_config(_entry())
    assert wire.concurrency is None
    assert wire.multimodal is False


async def test_wire_api_key_repr_hidden() -> None:
    """wire 的 api_key 不出现在 repr（密钥不落日志）。"""
    wire = build_llm_wire_config(_entry(api_key_env=""), fallback_api_key=None)
    wire_with_key = wire.model_copy(update={"api_key": "sk-super-secret-value"})
    assert "sk-super-secret-value" not in repr(wire_with_key)
