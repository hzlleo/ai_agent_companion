"""模型条目 → LLM 客户端 wire 配置的单一真源构建器。

wire 形态（:class:`LlmClientConfigModel`）是 config 域向 LLM 网关
（P0-4 交付面）下发的客户端配置契约：条目的 ``api_key_env`` 在此解析为
密钥明文（密钥只经环境变量进入进程，wire 构建即解析点），下游网关不再
触碰 ``os.environ``——密钥解析收口于 config 域单一位置。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from packages.harness.config.model_entry import LlmModelEntry, resolve_api_key

__all__ = ["LlmClientConfigModel", "build_llm_wire_config"]


class LlmClientConfigModel(BaseModel):
    """LLM 客户端 wire 配置（config → llm 网关的下发契约）。

    Attributes:
        provider: provider 类型（透传条目声明）。
        base_url: API 基础 URL。
        api_key: 已解析的 API Key 明文（本地免鉴权端点为空串；repr 隐藏）。
        model: 模型名（provider model 参数）。
        concurrency: 条目并发上限（None = 网关侧用全局默认）。
        multimodal: 是否支持多模态输入（网关 look_at 链路判据）。
    """

    model_config = ConfigDict(protected_namespaces=())

    provider: str = Field(default="openai_compatible", description="provider 类型")
    base_url: str = Field(default="", description="API 基础 URL")
    api_key: str = Field(default="", repr=False, description="已解析的 API Key 明文")
    model: str = Field(default="", description="模型名")
    concurrency: int | None = Field(default=None, description="条目并发上限")
    multimodal: bool = Field(default=False, description="是否支持多模态输入")


def build_llm_wire_config(
    entry: LlmModelEntry,
    *,
    fallback_api_key: str | None = None,
) -> LlmClientConfigModel:
    """构建模型条目的 wire 配置（密钥解析收口点）。

    密钥解析序：条目 ``api_key_env`` 指向的环境变量 → ``fallback_api_key``
    显式兜底（调用方从 Settings 的 SecretStr 取出传入；仅当条目声明了
    ``api_key_env`` 但变量未设置时启用）→ 空串。条目显式声明免鉴权
    （``api_key_env`` 为空）时不读兜底——免鉴权端点不应被意外注入全局密钥。

    Args:
        entry: 模型条目（注册表条目或回退链条目）。
        fallback_api_key: 条目声明了密钥变量但变量未设置时的兜底明文。

    Returns:
        :class:`LlmClientConfigModel` wire 配置实例。
    """
    api_key = ""
    if entry.api_key_env:
        api_key = resolve_api_key(entry.api_key_env)
        if not api_key and fallback_api_key:
            api_key = fallback_api_key
    return LlmClientConfigModel(
        provider=entry.provider,
        base_url=entry.base_url,
        api_key=api_key,
        model=entry.model,
        concurrency=entry.concurrency,
        multimodal=entry.multimodal,
    )
