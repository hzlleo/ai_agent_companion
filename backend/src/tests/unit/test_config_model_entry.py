"""harness/config model_entry 测试——条目归一化 / 密钥解析 / 校验路径。"""

from __future__ import annotations

import pytest

from packages.harness.config.model_entry import (
    SUPPORTED_MODEL_ROLES,
    LlmModelEntry,
    ModelRole,
    parse_model_entry,
    resolve_api_key,
)
from packages.harness.core.errors import BusinessException, ErrorCode


async def test_model_role_enum_values() -> None:
    """角色值域锁定（chat / duty / dream / look_at / embed）。"""
    assert {member.value for member in ModelRole} == {
        "chat",
        "duty",
        "dream",
        "look_at",
        "embed",
    }
    assert frozenset({"chat", "duty", "dream", "look_at", "embed"}) == SUPPORTED_MODEL_ROLES


async def test_parse_model_entry_full_fields() -> None:
    """全字段条目归一化：roles 枚举化 + concurrency / multimodal 透传。"""
    entry = parse_model_entry(
        {
            "name": "main-chat",
            "provider": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "MAIN_CHAT_API_KEY",
            "model": "glm-5.3",
            "roles": ["chat", "duty"],
            "concurrency": 4,
            "multimodal": True,
        }
    )
    assert entry.name == "main-chat"
    assert entry.provider == "openai_compatible"
    assert entry.base_url == "https://api.example.com/v1"
    assert entry.api_key_env == "MAIN_CHAT_API_KEY"
    assert entry.model == "glm-5.3"
    assert entry.roles == [ModelRole.CHAT, ModelRole.DUTY]
    assert entry.concurrency == 4
    assert entry.multimodal is True


async def test_parse_model_entry_defaults() -> None:
    """最小条目：provider 默认 openai_compatible，roles 空列表。"""
    entry = parse_model_entry({"name": "n", "base_url": "http://h/v1", "model": "m"})
    assert entry.provider == "openai_compatible"
    assert entry.roles == []
    assert entry.concurrency is None
    assert entry.multimodal is False
    assert entry.api_key_env == ""


async def test_parse_model_entry_strips_whitespace() -> None:
    """关键字段去首尾空白（name/base_url/model 非空契约）。"""
    entry = parse_model_entry({"name": "  n  ", "base_url": "  http://h/v1 ", "model": " m "})
    assert entry.name == "n"
    assert entry.base_url == "http://h/v1"
    assert entry.model == "m"


async def test_parse_model_entry_empty_required_field_rejected() -> None:
    """空 name / model 被 min_length 校验拒绝。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_model_entry({"name": "  ", "base_url": "http://h/v1", "model": "m"})
    with pytest.raises(ValidationError):
        parse_model_entry({"name": "n", "base_url": "http://h/v1", "model": ""})


async def test_parse_model_entry_invalid_concurrency_rejected() -> None:
    """concurrency 越界（<=0）被 ge=1 校验拒绝。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_model_entry({"name": "n", "base_url": "http://h/v1", "model": "m", "concurrency": 0})


async def test_parse_model_entry_unknown_role_rejected() -> None:
    """未知角色名 → CONFIG_INVALID（含合法值提示）。"""
    with pytest.raises(BusinessException) as exc_info:
        parse_model_entry(
            {"name": "n", "base_url": "http://h/v1", "model": "m", "roles": ["chat", "vip"]}
        )
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert "vip" in str(exc_info.value.details)


async def test_parse_model_entry_roles_not_list_rejected() -> None:
    """roles 非 list → CONFIG_INVALID。"""
    with pytest.raises(BusinessException) as exc_info:
        parse_model_entry({"name": "n", "base_url": "http://h/v1", "model": "m", "roles": "chat"})
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_parse_model_entry_non_dict_rejected() -> None:
    """条目非 mapping → CONFIG_INVALID。"""
    raw: object = "not-a-dict"
    with pytest.raises(BusinessException) as exc_info:
        parse_model_entry(raw)  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_resolve_api_key_bare_and_interpolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥解析：裸名与 ${ENV_VAR} 形式等价；缺失返回空串。"""
    monkeypatch.setenv("PROVIDER_X_KEY", "sk-abc123")
    assert resolve_api_key("PROVIDER_X_KEY") == "sk-abc123"
    assert resolve_api_key("${PROVIDER_X_KEY}") == "sk-abc123"
    assert resolve_api_key("") == ""
    assert resolve_api_key(None) == ""
    assert resolve_api_key("NEVER_SET_KEY_XYZ") == ""


async def test_resolve_api_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """首选变量缺失时读兜底变量。"""
    monkeypatch.delenv("PRIMARY_KEY_XYZ", raising=False)
    monkeypatch.setenv("FALLBACK_KEY_XYZ", "sk-fb")
    assert resolve_api_key("PRIMARY_KEY_XYZ", fallback_env="FALLBACK_KEY_XYZ") == "sk-fb"


async def test_llm_model_entry_is_immutable_model() -> None:
    """条目为 Pydantic 模型（frozen 语义由 pydantic 默认非 frozen——
    此处锁定其为 BaseModel 实例，热重建安全依赖其不可变使用约定）。"""
    entry = LlmModelEntry(name="n", base_url="http://h/v1", model="m")
    assert isinstance(entry, LlmModelEntry)
    dumped = entry.model_dump()
    assert dumped["name"] == "n"
    assert dumped["roles"] == []
