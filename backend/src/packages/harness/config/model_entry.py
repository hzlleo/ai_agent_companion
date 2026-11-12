"""LLM 模型条目 Model Class——多 provider OpenAI 兼容模型注册表的单条目契约。

本工程的主 LLM 为远程 OpenAI 兼容端点（vLLM 0.32+ 等自持推理盒）；
本地辅助模型端点（vision / embedding / TTS / ASR）同样以条目形态进入
注册表——注册表是通用的「provider → base_url → model」三级寻址。

条目字段（本工程配置契约）：

    - ``name``         —— 条目唯一标识（角色绑定 / 日志追踪引用它）
    - ``provider``     —— provider 类型（本工程统一 OpenAI 兼容协议；
      保留字段以支持后续差异化协议）
    - ``base_url``     —— API 基础 URL（OpenAI 兼容端点根路径，含 ``/v1``）
    - ``api_key_env``  —— API Key 所在环境变量名（密钥永不落 yaml，统一走
      .env / 进程环境变量；本地端点可留空表示免鉴权）
    - ``model``        —— 模型名（传给 provider 的 model 参数）
    - ``roles``        —— 该条目承接的角色集（chat / duty / dream /
      look_at / embed；显式 role_bindings 优先于条目声明）
    - ``concurrency``  —— 该条目并发上限（per-entry 信号量限值；None = 用
      全局默认 ``POOLS_LLM_SEMAPHORE_LIMIT``）
    - ``multimodal``   —— 是否支持多模态输入（look_at 链路回退判据）

:func:`parse_model_entry` 是单条目归一化的唯一入口（config.yaml 加载
与后续配置推送通道共用），:func:`resolve_api_key` 从环境变量解析密钥。
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "ModelRole",
    "LlmModelEntry",
    "parse_model_entry",
    "resolve_api_key",
    "SUPPORTED_MODEL_ROLES",
]


class ModelRole(StrEnum):
    """模型角色值域——陪伴智能体的五类模型用途。

    - ``chat``：主对话（日常陪聊、人格化回复）；
    - ``duty``：值班任务（日程提醒、例行巡检等结构化任务）；
    - ``dream``：梦境生成（记忆整理 / 梦境叙事写作）；
    - ``look_at``：多模态查看（图像理解；非多模态条目触发回退链）；
    - ``embed``：向量化（记忆检索 / 语义嵌入）。
    """

    CHAT = "chat"
    DUTY = "duty"
    DREAM = "dream"
    LOOK_AT = "look_at"
    EMBED = "embed"


#: 支持的角色集合（角色名校验真源）。
SUPPORTED_MODEL_ROLES: frozenset[str] = frozenset({member.value for member in ModelRole})


class LlmModelEntry(BaseModel):
    """单个 LLM 模型条目（模型注册表的一行）。

    密钥安全：条目只携带 ``api_key_env``（环境变量名），不携带密钥明文；
    运行时经 :func:`resolve_api_key` 解析（见 llm_wire 构建链）。

    Attributes:
        name: 条目唯一标识（非空；角色绑定与日志引用）。
        provider: provider 类型（默认 ``openai_compatible``）。
        base_url: API 基础 URL（非空；OpenAI 兼容端点根路径含 ``/v1``）。
        api_key_env: API Key 环境变量名（空 = 本地免鉴权端点）。
        model: 模型名（非空；传给 provider 的 model 参数）。
        roles: 该条目承接的角色集（可为空——仅供显式角色绑定引用）。
        concurrency: 条目并发上限（None = 全局默认）。
        multimodal: 是否支持多模态输入。
    """

    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(min_length=1, description="条目唯一标识（角色绑定引用）")
    provider: str = Field(default="openai_compatible", description="provider 类型")
    base_url: str = Field(min_length=1, description="API 基础 URL（含 /v1）")
    api_key_env: str = Field(default="", description="API Key 环境变量名（空=免鉴权）")
    model: str = Field(min_length=1, description="模型名（provider model 参数）")
    roles: list[ModelRole] = Field(default_factory=list, description="条目承接的角色集")
    concurrency: int | None = Field(default=None, ge=1, description="条目并发上限（None=全局默认）")
    multimodal: bool = Field(default=False, description="是否支持多模态输入")

    @field_validator("name", "base_url", "model")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        """去除首尾空白并拒绝空值（条目关键字段非空契约）。

        Args:
            value: 原始字段值。

        Returns:
            去除空白后的值。

        Raises:
            ValueError: 值为空 / 纯空白。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("条目关键字段（name/base_url/model）不能为空")
        return stripped


def resolve_api_key(api_key_env: str | None, fallback_env: str | None = None) -> str:
    """从环境变量解析 API Key。

    支持裸变量名与 ``${ENV_VAR}`` 形式；首选变量缺失时尝试兜底变量。
    密钥永不落 yaml——本函数是配置链与密钥的唯一交汇点。

    Args:
        api_key_env: 环境变量名（裸名或 ``${ENV_VAR}`` 形式；空 = 免鉴权端点，
            返回空串）。
        fallback_env: 首选变量缺失时的兜底变量名。

    Returns:
        解析出的 API Key 明文；变量未设置时返回空串。
    """
    if not api_key_env:
        return ""
    name = api_key_env.strip()
    if name.startswith("${") and name.endswith("}"):
        name = name[2:-1].strip()
    value = os.environ.get(name, "")
    if not value and fallback_env:
        value = os.environ.get(fallback_env, "")
    return value


def parse_model_entry(
    raw: dict[str, Any],
) -> LlmModelEntry:
    """将单个模型条目原始 dict 归一化为 :class:`LlmModelEntry`。

    归一化规则：

        - 值统一字符串化后交由模型校验（min_length / ge 等声明约束生效）；
        - ``roles`` 接受 list[str]，逐项按 :class:`ModelRole` 校验（未知
          角色名 → ``CONFIG_INVALID``，含合法值提示）；
        - 键名固定为本工程契约（name / provider / base_url / api_key_env /
          model / roles / concurrency / multimodal），无历史别名。

    Args:
        raw: 模型条目 dict（来源：config.yaml 解析 / 配置推送通道）。

    Returns:
        归一化后的 :class:`LlmModelEntry`。

    Raises:
        BusinessException: 键类型非法（``CONFIG_INVALID``）。pydantic 字段
            校验失败（空值 / 越界）原样上抛 ``ValidationError``，由调用方
            （loader）统一包装为 ``CONFIG_INVALID``。
    """
    if not isinstance(raw, dict):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={"reason": "模型条目必须是 mapping", "got": type(raw).__name__},
        )

    roles_raw = raw.get("roles", [])
    if not isinstance(roles_raw, list):
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={"reason": "roles 必须是列表", "entry": str(raw.get("name", ""))},
        )
    roles: list[str] = [str(role) for role in roles_raw]
    invalid = [role for role in roles if role not in SUPPORTED_MODEL_ROLES]
    if invalid:
        raise BusinessException(
            code=ErrorCode.CONFIG_INVALID,
            details={
                "reason": "模型条目含未知角色",
                "entry": str(raw.get("name", "")),
                "invalid_roles": invalid,
                "valid_roles": sorted(SUPPORTED_MODEL_ROLES),
            },
        )

    concurrency_raw = raw.get("concurrency")
    concurrency = int(concurrency_raw) if concurrency_raw is not None else None

    return LlmModelEntry(
        name=str(raw.get("name", "")),
        provider=str(raw.get("provider", "openai_compatible")),
        base_url=str(raw.get("base_url", "")),
        api_key_env=str(raw.get("api_key_env", "")),
        model=str(raw.get("model", "")),
        roles=[ModelRole(role) for role in roles],
        concurrency=concurrency,
        multimodal=bool(raw.get("multimodal", False)),
    )
