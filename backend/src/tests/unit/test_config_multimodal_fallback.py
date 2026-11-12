"""harness/config multimodal_fallback_loader 测试——回退链裁决 / fail loud。"""

from __future__ import annotations

import pytest

from packages.harness.config.model_entry import LlmModelEntry, parse_model_entry
from packages.harness.config.models import MultimodalFallbackConfig
from packages.harness.config.multimodal_fallback_loader import (
    record_multimodal_fallback,
    reset_multimodal_fallback_runtime_state,
    resolve_multimodal_chain,
)
from packages.harness.core.errors import BusinessException, ErrorCode


def _entry(name: str, *, multimodal: bool = False) -> LlmModelEntry:
    """构造测试条目。

    Returns:
        归一化后的 :class:`LlmModelEntry`。
    """
    return parse_model_entry(
        {
            "name": name,
            "base_url": f"http://{name}/v1",
            "model": f"m-{name}",
            "multimodal": multimodal,
        }
    )


def _fallback(
    *, local: str | None = "local-v", remote: str | None = "remote-v"
) -> MultimodalFallbackConfig:
    """构造回退链配置（级名 None 表示该级未配置）。"""
    return MultimodalFallbackConfig(
        local=_entry(local, multimodal=True) if local else None,
        remote=_entry(remote, multimodal=True) if remote else None,
    )


async def test_multimodal_role_entry_short_circuits() -> None:
    """role 条目自身多模态 → 零回退直连（[entry]）。"""
    record_multimodal_fallback(_fallback())
    chain = resolve_multimodal_chain(_entry("mm", multimodal=True))
    assert [entry.name for entry in chain] == ["mm"]


async def test_non_multimodal_entry_full_chain_order() -> None:
    """非多模态条目 → 回退链按「本地 → 远程」顺序返回。"""
    record_multimodal_fallback(_fallback())
    chain = resolve_multimodal_chain(_entry("text-only"))
    assert [entry.name for entry in chain] == ["local-v", "remote-v"]


async def test_chain_local_only() -> None:
    """仅配置本地级 → 单级链。"""
    record_multimodal_fallback(_fallback(remote=None))
    chain = resolve_multimodal_chain(_entry("text-only"))
    assert [entry.name for entry in chain] == ["local-v"]


async def test_chain_remote_only() -> None:
    """仅配置远程级 → 单级链。"""
    record_multimodal_fallback(_fallback(local=None))
    chain = resolve_multimodal_chain(_entry("text-only"))
    assert [entry.name for entry in chain] == ["remote-v"]


async def test_missing_chain_fails_loud() -> None:
    """非多模态 + 空回退链 → CONFIG_INVALID（fail loud，含修复指引）。"""
    record_multimodal_fallback(_fallback(local=None, remote=None))
    with pytest.raises(BusinessException) as exc_info:
        resolve_multimodal_chain(_entry("text-only"))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert exc_info.value.details is not None
    assert "multimodal_fallback" in str(exc_info.value.details.get("fix", ""))


async def test_no_recorded_config_fails_loud_for_non_multimodal() -> None:
    """未记录任何回退配置 + 非多模态条目 → 同样 fail loud。"""
    reset_multimodal_fallback_runtime_state()
    with pytest.raises(BusinessException) as exc_info:
        resolve_multimodal_chain(_entry("text-only"))
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


async def test_no_recorded_config_multimodal_entry_ok() -> None:
    """未记录回退配置 + 多模态条目 → 直连不受影响。"""
    reset_multimodal_fallback_runtime_state()
    chain = resolve_multimodal_chain(_entry("mm", multimodal=True))
    assert [entry.name for entry in chain] == ["mm"]


async def test_record_none_clears_state() -> None:
    """record(None) 清除记录（重载语义：段缺席即清空）。"""
    record_multimodal_fallback(_fallback())
    record_multimodal_fallback(None)
    with pytest.raises(BusinessException):
        resolve_multimodal_chain(_entry("text-only"))


async def test_record_empty_config_clears_state() -> None:
    """空配置（两级均 None）记录等同清除。"""
    record_multimodal_fallback(_fallback())
    record_multimodal_fallback(MultimodalFallbackConfig())
    with pytest.raises(BusinessException):
        resolve_multimodal_chain(_entry("text-only"))


async def test_re_record_replaces_chain() -> None:
    """再次记录即覆盖（重载链路：新配置立即可见）。"""
    record_multimodal_fallback(_fallback())
    record_multimodal_fallback(_fallback(local="local-new", remote=None))
    chain = resolve_multimodal_chain(_entry("text-only"))
    assert [entry.name for entry in chain] == ["local-new"]
