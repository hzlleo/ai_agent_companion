"""多模态回退链加载器——look_at 角色非多模态时的回退裁决。

场景：用户发送图像等多模态输入，而 look_at 角色绑定的模型条目不支持
多模态（``multimodal=False``）。回退链固定为「本地视觉端点 → 远程
多模态端点」两级（本地优先：低延迟 / 零外发成本；远程兜底：本地不可
用或能力不足时承接）。

解析序（:func:`resolve_multimodal_chain`）::

    role 条目本身支持多模态？
        ├─ 是 → [role 条目]（零回退，直连）
        └─ 否 → 回退链 = [local, remote] 中已配置且非 None 的级
                  ├─ 链非空 → 返回链（调用方按序尝试）
                  └─ 链为空 → BusinessException(CONFIG_INVALID)
                              （fail loud：半生效配置会让部分会话走回退、
                              部分会话失败，行为不可审计）

配置传输：回退链配置随 config.yaml ``llm.multimodal_fallback`` 段由
loader 加载（loader → 本模块单向依赖，经 :func:`record_multimodal_fallback`
注入；重载即重新注入，无缓存漂移）。
"""

from __future__ import annotations

from loguru import logger

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.config.models import MultimodalFallbackConfig
from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "record_multimodal_fallback",
    "resolve_multimodal_chain",
    "reset_multimodal_fallback_runtime_state",
]

# 进程内状态：loader 注入的回退链配置（None = 未配置——look_at 条目
# 自身多模态时无需回退，非多模态时 fail loud）。
_fallback_config: MultimodalFallbackConfig | None = None


def record_multimodal_fallback(config: MultimodalFallbackConfig | None) -> None:
    """记录 loader 加载的回退链配置（重载即重新注入）。

    Args:
        config: ``llm.multimodal_fallback`` 段配置；``None`` / 空配置表示
            该段未配置（清除记录）。
    """
    global _fallback_config
    if config is not None and (config.local is not None or config.remote is not None):
        _fallback_config = config
        logger.info(
            "多模态回退链配置已记录: local={}, remote={}",
            config.local.name if config.local else "(未配置)",
            config.remote.name if config.remote else "(未配置)",
        )
    else:
        _fallback_config = None
        logger.info("多模态回退链配置为空，已清除记录")


def reset_multimodal_fallback_runtime_state() -> None:
    """清空进程内回退链状态（测试隔离用）。"""
    global _fallback_config
    _fallback_config = None


def resolve_multimodal_chain(role_entry: LlmModelEntry) -> list[LlmModelEntry]:
    """解析 look_at 角色的多模态调用链（含回退裁决）。

    Args:
        role_entry: look_at 角色当前绑定的模型条目。

    Returns:
        按序尝试的条目链：role 条目自身多模态时为 ``[role_entry]``；
        否则为已配置的回退链（``[local, remote]`` 过滤 None 级）。

    Raises:
        BusinessException: role 条目非多模态且回退链未配置
            （``CONFIG_INVALID``，中文可操作指引：在 config.yaml 的
            ``llm.multimodal_fallback`` 段配置本地视觉端点或远程多模态端点，
            或将 look_at 角色绑定到 ``multimodal: true`` 的条目）。
    """
    if role_entry.multimodal:
        return [role_entry]

    chain: list[LlmModelEntry] = []
    if _fallback_config is not None:
        if _fallback_config.local is not None:
            chain.append(_fallback_config.local)
        if _fallback_config.remote is not None:
            chain.append(_fallback_config.remote)

    if not chain:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={
                "reason": "look_at 角色绑定的模型不支持多模态，且未配置回退链",
                "role_entry": role_entry.name,
                "fix": (
                    "在 config.yaml 的 llm.multimodal_fallback 段配置 local（本地"
                    "视觉端点）或 remote（远程多模态端点），或将 look_at 角色绑定"
                    "到 multimodal: true 的条目"
                ),
            },
        )
    return chain
