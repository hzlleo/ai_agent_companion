"""harness/config model_resolver 测试——角色解析序 / 冲突规则 / 热重建 / 异常。"""

from __future__ import annotations

import pytest

from packages.harness.config.model_entry import LlmModelEntry, parse_model_entry
from packages.harness.config.model_resolver import (
    ModelResolver,
    get_model_resolver,
    rebuild_model_resolver,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _entry(name: str, roles: list[str], **kwargs: object) -> LlmModelEntry:
    """构造测试条目（name/roles 必给，其余透传）。"""
    raw: dict[str, object] = {"name": name, "base_url": "http://h/v1", "model": f"m-{name}"}
    raw.update(kwargs)
    raw["roles"] = roles
    return parse_model_entry(raw)


async def test_resolve_by_entry_roles_declaration() -> None:
    """解析序②：条目自身 roles 声明命中。"""
    resolver = ModelResolver([_entry("chat-model", ["chat"]), _entry("embed-model", ["embed"])])
    assert resolver.resolve("chat").name == "chat-model"
    assert resolver.resolve("embed").name == "embed-model"


async def test_resolve_explicit_binding_overrides_declaration() -> None:
    """解析序①：显式 role_bindings 优先于条目 roles 声明。"""
    resolver = ModelResolver(
        [_entry("a", ["chat"]), _entry("b", ["duty"])],
        {"chat": "b"},
    )
    assert resolver.resolve("chat").name == "b"


async def test_resolve_missing_role_raises_model_not_found() -> None:
    """未绑定角色 → MODEL_NOT_FOUND（details 含角色名与已绑定清单）。"""
    resolver = ModelResolver([_entry("a", ["chat"])])
    with pytest.raises(BusinessException) as exc_info:
        resolver.resolve("dream")
    assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
    assert exc_info.value.details is not None
    assert exc_info.value.details["role"] == "dream"
    assert exc_info.value.details["bound_roles"] == ["chat"]


async def test_resolve_optional_returns_none() -> None:
    """resolve_optional：未绑定返回 None（不抛异常）。"""
    resolver = ModelResolver([])
    assert resolver.resolve_optional("chat") is None


async def test_resolve_accepts_enum_member() -> None:
    """角色可传 ModelRole 枚举成员（.value 归一化）。"""
    from packages.harness.config.model_entry import ModelRole

    resolver = ModelResolver([_entry("a", ["chat"])])
    assert resolver.resolve(ModelRole.CHAT).name == "a"
    assert resolver.resolve_optional(ModelRole.DREAM) is None


async def test_duplicate_entry_name_last_wins() -> None:
    """重名条目：后者覆盖前者（配置错误尽早暴露语义）。"""
    from packages.harness.config.model_entry import ModelRole

    resolver = ModelResolver([_entry("dup", ["chat"]), _entry("dup", ["duty"])])
    overridden = resolver.get("dup")
    assert overridden is not None
    assert overridden.roles == [ModelRole.DUTY]


async def test_duplicate_role_claim_first_registered_wins() -> None:
    """同角色多条目声明：先注册者胜出（跨调用稳定）。"""
    resolver = ModelResolver([_entry("first", ["chat"]), _entry("second", ["chat"])])
    assert resolver.resolve("chat").name == "first"


async def test_binding_to_unknown_entry_ignored() -> None:
    """显式绑定指向不存在的条目名：按未绑定处理，回退条目声明。"""
    resolver = ModelResolver([_entry("a", ["chat"])], {"chat": "ghost"})
    assert resolver.resolve("chat").name == "a"


async def test_binding_unknown_role_ignored() -> None:
    """显式绑定含未知角色名：忽略该绑定。"""
    resolver = ModelResolver([_entry("a", ["chat"])], {"wizard": "a"})
    assert resolver.resolve_optional("wizard") is None


async def test_rebuild_swaps_index_atomically() -> None:
    """热重建：索引整体替换；在途已解析引用不受影响。"""
    resolver = ModelResolver([_entry("old", ["chat"])])
    in_flight = resolver.resolve("chat")
    resolver.rebuild([_entry("new", ["chat"])])
    assert resolver.resolve("chat").name == "new"
    assert in_flight.name == "old"  # 旧引用仍完整可用（不可变模型）


async def test_rebuild_clears_stale_roles() -> None:
    """热重建后旧角色绑定清除（无陈旧索引残留）。"""
    resolver = ModelResolver([_entry("a", ["chat", "duty"])])
    assert resolver.bound_roles == ["chat", "duty"]
    resolver.rebuild([_entry("b", ["chat"])])
    assert resolver.bound_roles == ["chat"]


async def test_get_by_name_and_models_snapshot() -> None:
    """按名查表 + 注册表快照（声明顺序保持）。"""
    entries = [_entry("a", ["chat"]), _entry("b", ["duty"])]
    resolver = ModelResolver(entries)
    assert resolver.get("a").name == "a"
    assert resolver.get("ghost") is None
    assert [entry.name for entry in resolver.models] == ["a", "b"]


async def test_global_resolver_singleton_and_rebuild() -> None:
    """全局单例：get 恒定对象身份；rebuild 原地重建索引。"""
    first = get_model_resolver()
    rebuild_model_resolver([_entry("global-a", ["chat"])])
    second = get_model_resolver()
    assert first is second
    assert second.resolve("chat").name == "global-a"
