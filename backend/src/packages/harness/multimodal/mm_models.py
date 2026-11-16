"""harness/multimodal 域 Model Classes——look_at 请求 / 结果 / 工具参数与
DataUrl 助手（Unit 文件）。

契约分层：

    - :class:`LookAtRequestModel` —— look_at 服务请求（图像字节 + MIME
      允许清单校验 + 聚焦问题可选上限；服务调用面的单一校验真源）；
    - :class:`LookAtResultModel`  —— look_at 服务结果（结构化中文描述 +
      实际承接的模型条目名 + 端到端时延）；
    - :class:`LookAtToolArgsModel` —— look_at 工具参数面（LLM 工具循环
      消费：图像经 base64 字符串携带——registry args 校验锚点）；
    - :func:`build_data_url` / :func:`parse_data_url` —— data URL
      构建 / 解析助手（RFC 2397 ``data:<mime>;base64,<payload>`` 形态；
      llm 域 ``ImageContentPart`` 的 url 载荷由本助手产出）。

大输入边界（文档化决策）：图像为**内存字节**，单次上限 10MB
（:data:`LOOK_AT_IMAGE_MAX_BYTES`——超过即 ``VALIDATION_ERROR``，
调用方先行压缩 / 裁剪）；MIME 允许清单 png / jpeg / webp / gif
（:data:`LOOK_AT_IMAGE_MIME_ALLOWLIST`——OpenAI 兼容视觉端点的通用
支持面）。图像字节永不落日志（observability 面仅记录尺寸 / MIME /
条目名等元数据）。
"""

from __future__ import annotations

import base64
from binascii import Error as Base64Error
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from packages.harness.config.model_entry import LlmModelEntry
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatRequestParams,
    ChatResponseModel,
    ToolDefinitionModel,
)

__all__ = [
    "LOOK_AT_IMAGE_MIME_ALLOWLIST",
    "LOOK_AT_IMAGE_MAX_BYTES",
    "LOOK_AT_QUESTION_MAX_CHARS",
    "LookAtRequestModel",
    "LookAtResultModel",
    "LookAtToolArgsModel",
    "LookAtGatewayFace",
    "LookAtFace",
    "build_data_url",
    "parse_data_url",
]

#: 图像 MIME 允许清单（png / jpeg / webp / gif——视觉端点通用支持面）。
LOOK_AT_IMAGE_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: 单次 look_at 图像上限（字节；10MB——超过由调用方先行压缩 / 裁剪）。
LOOK_AT_IMAGE_MAX_BYTES: int = 10 * 1024 * 1024

#: 聚焦问题上限（字符；200——问题是大方向提示，不是长指令）。
LOOK_AT_QUESTION_MAX_CHARS: int = 200

#: data URL 前缀模板（RFC 2397；base64 扩展形态）。
_DATA_URL_TEMPLATE: str = "data:{mime_type};base64,{payload}"


class LookAtRequestModel(BaseModel):
    """look_at 服务请求（图像理解调用面的单一校验真源）。

    Attributes:
        image: 图像字节（非空；上限 :data:`LOOK_AT_IMAGE_MAX_BYTES`）。
        mime_type: 图像 MIME 类型（png / jpeg / webp / gif——大小写与
            首尾空白归一后按允许清单校验）。
        question: 聚焦问题（None = 整体结构化描述；≤200 字符）。
    """

    image: bytes = Field(description="图像字节（内存形态）")
    mime_type: str = Field(description="图像 MIME 类型（允许清单内）")
    question: str | None = Field(
        default=None, max_length=LOOK_AT_QUESTION_MAX_CHARS, description="聚焦问题（可选）"
    )

    @field_validator("image")
    @classmethod
    def _check_image(cls, value: bytes) -> bytes:
        """图像字节守卫（非空 + 上限）。

        Args:
            value: 原始图像字节。

        Returns:
            原样字节。

        Raises:
            ValueError: 空载荷或超过 10MB 上限。
        """
        if not value:
            raise ValueError("图像字节不能为空")
        if len(value) > LOOK_AT_IMAGE_MAX_BYTES:
            raise ValueError(f"图像超过单次上限 {LOOK_AT_IMAGE_MAX_BYTES} 字节（10MB）")
        return value

    @field_validator("mime_type")
    @classmethod
    def _check_mime(cls, value: str) -> str:
        """MIME 归一 + 允许清单校验（小写 / 去空白后判定）。

        Args:
            value: 原始 MIME 字符串。

        Returns:
            归一化后的 MIME（小写）。

        Raises:
            ValueError: 不在允许清单内（消息携带合法值清单）。
        """
        normalized = value.strip().lower()
        if normalized not in LOOK_AT_IMAGE_MIME_ALLOWLIST:
            allowed = ", ".join(sorted(LOOK_AT_IMAGE_MIME_ALLOWLIST))
            raise ValueError(f"不支持的图像类型 {value!r}（允许: {allowed}）")
        return normalized


class LookAtResultModel(BaseModel):
    """look_at 服务结果（视觉理解归一形态）。

    Attributes:
        description: 结构化中文描述文本（视觉模型按任务书产出——
            主体 / 场景 / 文字内容 / 图表数据 / 细节 + 可选聚焦回答）。
        model_entry: 实际承接调用的模型条目名（回退链中命中的条目——
            观测 / 审计面）。
        latency_ms: 端到端时延（epoch 毫秒差；含回退链重试耗时）。
    """

    description: str = Field(description="结构化中文描述文本")
    model_entry: str = Field(min_length=1, description="实际承接的模型条目名")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")


class LookAtToolArgsModel(BaseModel):
    """look_at 工具参数面（LLM 工具循环——图像经 base64 携带）。

    LLM 侧工具调用无法传二进制：图像以 base64 字符串进入参数面，
    执行器解码为字节后委托服务（解码失败 → ok=False 工具错误，恒不
    上抛）。MIME 由调用方显式声明（缺省 png——执行器不做字节嗅探）。

    Attributes:
        image_base64: 图像 base64 载荷（标准字母表；非空）。
        mime_type: 图像 MIME 类型（png / jpeg / webp / gif，缺省 png）。
        question: 聚焦问题（None = 整体结构化描述；≤200 字符）。
    """

    model_config = {"extra": "forbid"}

    image_base64: str = Field(min_length=1, description="图像 base64 载荷")
    mime_type: str = Field(default="image/png", description="图像 MIME 类型（缺省 png）")
    question: str | None = Field(
        default=None, max_length=LOOK_AT_QUESTION_MAX_CHARS, description="聚焦问题（可选）"
    )

    @field_validator("mime_type")
    @classmethod
    def _check_mime(cls, value: str) -> str:
        """MIME 归一 + 允许清单校验（与服务请求面同规则）。

        Args:
            value: 原始 MIME 字符串。

        Returns:
            归一化后的 MIME（小写）。

        Raises:
            ValueError: 不在允许清单内。
        """
        normalized = value.strip().lower()
        if normalized not in LOOK_AT_IMAGE_MIME_ALLOWLIST:
            allowed = ", ".join(sorted(LOOK_AT_IMAGE_MIME_ALLOWLIST))
            raise ValueError(f"不支持的图像类型 {value!r}（允许: {allowed}）")
        return normalized


def build_data_url(image: bytes, mime_type: str) -> str:
    """构建 data URL（RFC 2397 base64 形态——llm 图片分块 url 载荷）。

    Args:
        image: 图像字节（调用方保证非空——空载荷无视觉语义）。
        mime_type: 图像 MIME 类型（允许清单内；原样进入 URL，不做
            二次校验——校验归 :class:`LookAtRequestModel` 单一真源）。

    Returns:
        ``data:<mime_type>;base64,<payload>`` 字符串。
    """
    payload = base64.b64encode(image).decode("ascii")
    return _DATA_URL_TEMPLATE.format(mime_type=mime_type, payload=payload)


def parse_data_url(data_url: str) -> tuple[str, bytes]:
    """解析 data URL（构建助手的逆运算——测试 / 校验面消费）。

    Args:
        data_url: ``data:<mime_type>;base64,<payload>`` 字符串。

    Returns:
        ``(mime_type, image_bytes)`` 二元组。

    Raises:
        ValueError: 前缀 / 结构 / base64 载荷非法。
    """
    if not data_url.startswith("data:"):
        raise ValueError("data URL 须以 data: 前缀开头")
    header, separator, payload = data_url.partition(",")
    if not separator:
        raise ValueError("data URL 缺少逗号分隔的载荷段")
    media = header[len("data:") :]
    if not media.endswith(";base64"):
        raise ValueError("data URL 须为 base64 形态（data:<mime>;base64,<payload>）")
    mime_type = media[: -len(";base64")]
    if not mime_type:
        raise ValueError("data URL 缺少 MIME 类型")
    try:
        image = base64.b64decode(payload, validate=True)
    except (Base64Error, ValueError) as exc:
        raise ValueError(f"data URL base64 载荷非法: {exc}") from exc
    return mime_type, image


class LookAtGatewayFace(Protocol):
    """LLM 网关注入面协议（真实 :class:`~packages.harness.llm.gateway.LlmGateway`
    / 测试 fake 均满足——与 voice 域 TtsClientFace 同约定）。

    look_at 服务只消费条目直连面（回退链条目逐个尝试）；角色门面
    ``chat_by_role`` 不在注入面（角色解析由服务持有的 resolver 承担）。
    """

    async def chat_for_entry(
        self,
        entry: LlmModelEntry,
        messages: Sequence[ChatMessageModel],
        params: ChatRequestParams | None = None,
        *,
        persona_id: str | None = None,
        purpose: str = "chat",
        request_id: str | None = None,
        tools: Sequence[ToolDefinitionModel] | None = None,
        tool_choice: str | None = None,
    ) -> ChatResponseModel:
        """按显式条目非流式调用（wire 构建 → 门控 → 传输 → 用量发射）。

        Args:
            entry: 模型条目（回退链条目）。
            messages: 聊天消息序列（含多模态内容分块轮）。
            params: 采样参数（None = 端点默认）。
            persona_id: 人格 ID（None = 读请求上下文）。
            purpose: 用量用途（look_at 链路恒 ``look_at``）。
            request_id: 请求追踪 ID（None = 读请求上下文）。
            tools: 原生工具定义列表（None / 空 = 不携带）。
            tool_choice: 工具选择策略（None = ``auto``）。

        Returns:
            归一响应（content 承载视觉描述文本）。
        """
        ...


class LookAtFace(Protocol):
    """look_at 服务注入面协议（真实 :class:`LookAtService` / 测试 fake
    均满足——工具注册面消费）。
    """

    async def describe(
        self,
        image: bytes,
        mime_type: str,
        question: str | None = None,
        *,
        persona_id: str | None = None,
        request_id: str | None = None,
    ) -> LookAtResultModel:
        """理解图像（字节 + MIME + 可选聚焦问题 → 结构化中文描述）。

        Args:
            image: 图像字节（非空；上限 10MB）。
            mime_type: 图像 MIME 类型（png / jpeg / webp / gif）。
            question: 聚焦问题（None = 整体结构化描述）。
            persona_id: 人格 ID（None = 读请求上下文）。
            request_id: 请求追踪 ID（None = 读请求上下文）。

        Returns:
            look_at 结果（description / model_entry / latency_ms）。
        """
        ...
