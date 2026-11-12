"""LLM 网关传输层 Model Classes —— 消息 / 参数 / 响应与 OpenAI 兼容 wire DTO。

本模块是 llm 域的 Unit 文件（Model Classes 优先于裸 dict）：

    - 域内契约（模型无关）：:class:`ChatMessageModel` / :class:`ChatRequestParams`
      / :class:`ChatResponseModel` / :class:`ChatStreamChunkModel` /
      :class:`TokenUsageModel`——网关门面（gateway）与业务调用方消费；
    - wire 契约（OpenAI 兼容端点）：``Wire*`` 系列——client 传输层的
      请求 / 响应序列化形态（仅 client 消费，不外泄）；
    - :class:`LlmUsageEvent`——网关 → usage 域的注入式用量事件契约
      （llm 域不硬依赖 usage 域；见 gateway.wire_default_usage_recorder）。

约定：全部时间字段 epoch 毫秒；token 计数非负；wire DTO ``extra="ignore"``
容忍端点附加字段（前向兼容）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ChatMessageRole",
    "ChatMessageModel",
    "ChatRequestParams",
    "ChatResponseModel",
    "ChatStreamChunkModel",
    "TokenUsageModel",
    "LlmUsageEvent",
    "WireChatMessage",
    "WireTokenUsage",
    "WireChatChoice",
    "WireChatCompletionResponse",
    "WireStreamDelta",
    "WireStreamChoice",
    "WireStreamChunk",
    "build_chat_request_body",
]


class ChatMessageRole(StrEnum):
    """聊天消息角色值域（OpenAI 兼容协议的标准三角色）。

    - ``system``：系统 / 人格设定指令；
    - ``user``：用户输入；
    - ``assistant``：模型回复（多轮上下文回放）。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessageModel(BaseModel):
    """单条聊天消息（模型无关传输契约）。

    Attributes:
        role: 消息角色。
        content: 消息文本内容（多模态内容分块契约由后续多模态 chunk 扩展，
            本 chunk 为纯文本传输面）。
    """

    role: ChatMessageRole = Field(description="消息角色")
    content: str = Field(description="消息文本内容")


class TokenUsageModel(BaseModel):
    """token 用量（模型无关归一形态）。

    Attributes:
        input_tokens: 输入（prompt）token 数。
        output_tokens: 输出（completion）token 数。
        total_tokens: 总 token 数（未显式提供时按 input + output 归一）。
    """

    input_tokens: int = Field(default=0, ge=0, description="输入 token 数")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 数")
    total_tokens: int = Field(default=0, ge=0, description="总 token 数")

    @model_validator(mode="after")
    def _fill_total(self) -> TokenUsageModel:
        """total_tokens 归一：未提供（0）且分项非零时按 input + output 补齐。

        Returns:
            归一后的实例。
        """
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class ChatRequestParams(BaseModel):
    """聊天请求采样参数（模型无关；未设置的字段不进入 wire 请求体）。

    Attributes:
        temperature: 采样温度（0.0 - 2.0）。
        max_tokens: 单次回复最大生成 token 数。
        top_p: 核采样概率阈值（0 - 1.0）。
        extra: provider 特有参数透传（原样并入 wire 请求体顶层；键冲突时
            extra 覆盖标准字段——调用方自负其责）。
    """

    model_config = ConfigDict(protected_namespaces=())

    temperature: float | None = Field(default=None, ge=0.0, le=2.0, description="采样温度")
    max_tokens: int | None = Field(default=None, ge=1, description="最大生成 token 数")
    top_p: float | None = Field(default=None, gt=0.0, le=1.0, description="核采样阈值")
    extra: dict[str, Any] = Field(default_factory=dict, description="provider 特有参数透传")


class ChatResponseModel(BaseModel):
    """非流式聊天响应（模型无关归一形态）。

    Attributes:
        content: 回复文本（首个 choice）。
        finish_reason: 结束原因（stop / length / content_filter 等；端点
            未提供时为 None）。
        model: 实际服务的模型名（端点回显；缺省回落请求模型名）。
        usage: token 用量（端点未回传时为 None——调用方按零用量记账）。
        latency_ms: 端到端时延（epoch 毫秒差；client 侧测量）。
    """

    content: str = Field(description="回复文本")
    finish_reason: str | None = Field(default=None, description="结束原因")
    model: str = Field(default="", description="实际服务的模型名")
    usage: TokenUsageModel | None = Field(default=None, description="token 用量")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")


class ChatStreamChunkModel(BaseModel):
    """流式增量块（SSE data 块归一形态）。

    Attributes:
        content: 本块增量文本（可为空串——首块 role-only / 末块 usage-only）。
        finish_reason: 结束原因（携带于最后一个内容块；此前为 None）。
        usage: token 用量（仅当端点在流末块附带——client 请求体固定声明
            ``stream_options.include_usage``）。
    """

    content: str = Field(default="", description="本块增量文本")
    finish_reason: str | None = Field(default=None, description="结束原因")
    usage: TokenUsageModel | None = Field(default=None, description="token 用量（末块）")


class LlmUsageEvent(BaseModel):
    """网关用量事件（llm → usage 域的注入式记录契约）。

    llm 域不硬依赖 usage 域：网关经注入的异步记录器发射本事件，由
    ``gateway.wire_default_usage_recorder()``（P0-6 app 装配调用）映射为
    usage 域的 ``UsageCreateDTO`` 落库。

    Attributes:
        ts_ms: 调用发起时间（epoch 毫秒）。
        persona_id: 人格 ID（None = 系统调用无人格上下文）。
        purpose: 调用用途（chat / duty / dream / look_at / embed / tts /
            asr / tool；usage 域枚举校验）。
        model_entry: 模型条目名（注册表条目标识）。
        provider: provider 类型。
        input_tokens: 输入 token 数（错误路径 / 端点未回传时为 0）。
        output_tokens: 输出 token 数。
        total_tokens: 总 token 数。
        latency_ms: 端到端时延毫秒（含失败耗时）。
        status: 调用结果（success / error）。
        request_id: 请求追踪 ID（None = 无请求上下文）。
        error_code: 失败错误码（成功为 None）。
    """

    ts_ms: int = Field(description="调用发起时间（epoch 毫秒）")
    persona_id: str | None = Field(default=None, description="人格 ID（系统调用为 None）")
    purpose: str = Field(description="调用用途")
    model_entry: str = Field(min_length=1, description="模型条目名")
    provider: str = Field(default="openai_compatible", description="provider 类型")
    input_tokens: int = Field(default=0, ge=0, description="输入 token 数")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 数")
    total_tokens: int = Field(default=0, ge=0, description="总 token 数")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")
    status: str = Field(description="调用结果（success/error）")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
    error_code: str | None = Field(default=None, description="失败错误码")


# =========================================================================== []
# wire DTO（OpenAI 兼容端点请求 / 响应形态——仅 client 传输层消费）
# =========================================================================== []


class WireChatMessage(BaseModel):
    """wire 消息（chat/completions 请求体 messages 元素形态）。

    Attributes:
        role: 角色（system / user / assistant）。
        content: 文本内容。
    """

    role: str = Field(description="消息角色")
    content: str = Field(description="消息内容")


class WireTokenUsage(BaseModel):
    """wire 用量（OpenAI 兼容 usage 形态）。

    Attributes:
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        total_tokens: 总 token 数。
    """

    prompt_tokens: int = Field(default=0, ge=0, description="输入 token 数")
    completion_tokens: int = Field(default=0, ge=0, description="输出 token 数")
    total_tokens: int = Field(default=0, ge=0, description="总 token 数")


class WireChatChoice(BaseModel):
    """wire 非流式 choice（响应 choices 元素形态）。

    Attributes:
        index: choice 序号。
        message: 回复消息。
        finish_reason: 结束原因。
    """

    index: int = Field(default=0, description="choice 序号")
    message: WireChatMessage = Field(description="回复消息")
    finish_reason: str | None = Field(default=None, description="结束原因")


class WireChatCompletionResponse(BaseModel):
    """wire 非流式响应（chat/completions 响应体形态）。

    Attributes:
        id: 端点侧响应 ID。
        model: 实际服务的模型名（端点回显）。
        choices: 回复 choice 列表（空 = 非法响应，client 拒绝）。
        usage: token 用量（部分端点流式关闭时不回传）。
    """

    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    id: str = Field(default="", description="端点侧响应 ID")
    model: str = Field(default="", description="实际服务的模型名")
    choices: list[WireChatChoice] = Field(default_factory=list, description="回复列表")
    usage: WireTokenUsage | None = Field(default=None, description="token 用量")


class WireStreamDelta(BaseModel):
    """wire 流式增量（chunk.choices[].delta 形态）。

    Attributes:
        role: 角色回显（首块携带）。
        content: 增量文本（usage-only 末块为 None）。
    """

    role: str | None = Field(default=None, description="角色回显（首块）")
    content: str | None = Field(default=None, description="增量文本")


class WireStreamChoice(BaseModel):
    """wire 流式 choice（chunk.choices 元素形态）。

    Attributes:
        index: choice 序号。
        delta: 增量载荷。
        finish_reason: 结束原因（末个内容块携带）。
    """

    index: int = Field(default=0, description="choice 序号")
    delta: WireStreamDelta = Field(default_factory=WireStreamDelta, description="增量载荷")
    finish_reason: str | None = Field(default=None, description="结束原因")


class WireStreamChunk(BaseModel):
    """wire 流式块（chat/completions stream=true 的 SSE data 载荷形态）。

    Attributes:
        id: 端点侧响应 ID。
        model: 实际服务的模型名。
        choices: 增量 choice 列表（usage-only 末块为空）。
        usage: token 用量（端点在流末块附带；include_usage 声明时存在）。
    """

    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    id: str = Field(default="", description="端点侧响应 ID")
    model: str = Field(default="", description="实际服务的模型名")
    choices: list[WireStreamChoice] = Field(default_factory=list, description="增量列表")
    usage: WireTokenUsage | None = Field(default=None, description="token 用量（末块）")


def build_chat_request_body(
    model: str,
    messages: list[ChatMessageModel] | tuple[ChatMessageModel, ...],
    params: ChatRequestParams | None,
    *,
    stream: bool,
) -> dict[str, Any]:
    """构建 chat/completions 请求体（wire 序列化前的 dict 形态）。

    规则：未设置的采样参数不进入请求体（端点侧默认值生效）；
    ``stream=True`` 时固定声明 ``stream_options.include_usage``（用量
    末块回传）；``params.extra`` 原样并入顶层（provider 特有参数透传）。

    Args:
        model: 模型名（provider model 参数）。
        messages: 聊天消息序列。
        params: 采样参数（None = 全默认）。
        stream: 是否流式请求。

    Returns:
        可直接作为 HTTP JSON body 的 dict。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": message.role.value, "content": message.content} for message in messages
        ],
    }
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    if params is not None:
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.max_tokens is not None:
            body["max_tokens"] = params.max_tokens
        if params.top_p is not None:
            body["top_p"] = params.top_p
        body.update(params.extra)
    return body
