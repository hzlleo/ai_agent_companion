"""LLM 网关传输层 Model Classes —— 消息 / 参数 / 响应与 OpenAI 兼容 wire DTO。

本模块是 llm 域的 Unit 文件（Model Classes 优先于裸 dict）：

    - 域内契约（模型无关）：:class:`ChatMessageModel` / :class:`ChatRequestParams`
      / :class:`ChatResponseModel` / :class:`ChatStreamChunkModel` /
      :class:`TokenUsageModel`——网关门面（gateway）与业务调用方消费；
    - 原生工具调用契约（P3-2）：:class:`ToolParametersSchema` /
      :class:`ToolDefinitionModel`（请求 tools 面）+ :class:`ToolCallModel`
      （响应 tool_calls 面 + assistant / tool 消息形态扩展）；
    - wire 契约（OpenAI 兼容端点）：``Wire*`` 系列——client 传输层的
      请求 / 响应序列化形态（仅 client 消费，不外泄）；
    - :class:`LlmUsageEvent`——网关 → usage 域的注入式用量事件契约
      （llm 域不硬依赖 usage 域；见 gateway.wire_default_usage_recorder）。

约定：全部时间字段 epoch 毫秒；token 计数非负；wire DTO ``extra="ignore"``
容忍端点附加字段（前向兼容）。

流式边界（文档化决策）：**流式传输面恒为 content-only**——原生工具调用
仅支持非流式 ``chat``；``build_chat_request_body`` 对 ``stream=True`` 且
携带 ``tools`` 的组合直接拒绝（``VALIDATION_ERROR``——调用方编程错误面）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.harness.core.errors import BusinessException, ErrorCode

__all__ = [
    "ChatMessageRole",
    "ChatMessageModel",
    "ChatRequestParams",
    "ChatResponseModel",
    "ChatStreamChunkModel",
    "TokenUsageModel",
    "EmbeddingsResponseModel",
    "LlmUsageEvent",
    "ToolParametersSchema",
    "ToolDefinitionModel",
    "ToolCallModel",
    "TOOL_CHOICE_AUTO",
    "WireChatMessage",
    "WireToolCallFunction",
    "WireToolCall",
    "WireTokenUsage",
    "WireChatChoice",
    "WireChatCompletionResponse",
    "WireStreamDelta",
    "WireStreamChoice",
    "WireStreamChunk",
    "WireEmbeddingItem",
    "WireEmbeddingsResponse",
    "build_chat_request_body",
    "build_embeddings_request_body",
]

#: 工具选择策略缺省值（OpenAI 兼容协议语义——模型自行决定是否调用工具）。
TOOL_CHOICE_AUTO: str = "auto"


class ChatMessageRole(StrEnum):
    """聊天消息角色值域（OpenAI 兼容协议角色 + 原生工具调用扩展）。

    - ``system``：系统 / 人格设定指令；
    - ``user``：用户输入；
    - ``assistant``：模型回复（多轮上下文回放；原生工具调用轮次携带
      ``tool_calls`` 形态）；
    - ``tool``：工具结果回放（原生工具调用循环——``tool_call_id`` 关联
      assistant 轮次的调用声明）。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolParametersSchema(BaseModel):
    """工具参数 JSON Schema（OpenAI 兼容 ``function.parameters`` 形态）。

    JSON Schema 字段高度动态，``extra="allow"`` 容纳全部合法键
    （``additionalProperties`` 等——构造输入与序列化输出均原样保留）。

    Attributes:
        type: schema 类型（通常 ``object``）。
        properties: 属性定义（属性名 → 子 schema）。
        required: 必填属性名列表。
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = Field(default=None, description="schema 类型（object）")
    properties: dict[str, Any] = Field(default_factory=dict, description="属性定义")
    required: list[str] = Field(default_factory=list, description="必填属性名")


class ToolDefinitionModel(BaseModel):
    """原生工具调用定义（请求 ``tools`` 面的模型无关形态）。

    wire 序列化时包裹为 OpenAI 兼容形态
    ``{"type": "function", "function": {...}}``（见 build_chat_request_body）。

    Attributes:
        name: 工具名（唯一标识——模型据此发起调用）。
        description: 工具描述（告知模型何时调用该工具）。
        parameters: 参数 JSON Schema（``type="object"`` 形态）。
    """

    name: str = Field(min_length=1, description="工具名（唯一标识）")
    description: str = Field(default="", description="工具描述（模型调用时机说明）")
    parameters: ToolParametersSchema = Field(
        default_factory=ToolParametersSchema, description="参数 JSON Schema"
    )


class ToolCallModel(BaseModel):
    """单条工具调用声明（响应 ``tool_calls`` 元素的模型无关形态）。

    wire 形态的 ``function.arguments`` 为 JSON 字符串；client 解析面
    robust 归一为本类的 ``arguments`` 已解析 dict（非法 JSON →
    ``LLM_CALL_FAILED``，见 client 映射表）。

    Attributes:
        id: 调用 ID（端点生成；tool 结果消息经 ``tool_call_id`` 回关联）。
        type: 调用类型（OpenAI 兼容协议当前仅 ``function``）。
        name: 工具名。
        arguments: 已解析的调用参数（JSON 对象——空参为空 dict）。
    """

    id: str = Field(default="", description="调用 ID（端点生成）")
    type: str = Field(default="function", description="调用类型（function）")
    name: str = Field(min_length=1, description="工具名")
    arguments: dict[str, Any] = Field(default_factory=dict, description="已解析调用参数")


class ChatMessageModel(BaseModel):
    """单条聊天消息（模型无关传输契约——含原生工具调用形态）。

    形态矩阵（按 role）：

        - ``system`` / ``user``：纯文本（``content``）；
        - ``assistant`` + ``tool_calls``：模型工具调用声明轮（``content``
          可为空串——wire 序列化为 ``null``）；多轮上下文回放用；
        - ``tool``：工具结果回放（``tool_call_id`` 关联声明轮 + ``content``
          承载反馈文本）。

    Attributes:
        role: 消息角色。
        content: 消息文本内容（多模态内容分块契约由后续多模态 chunk 扩展，
            本 chunk 为纯文本传输面；assistant 工具调用轮可为空串）。
        tool_calls: 模型工具调用声明（assistant 轮次携带；其余角色为 None）。
        tool_call_id: 关联的调用 ID（tool 角色消息必填语义；其余角色 None）。
    """

    role: ChatMessageRole = Field(description="消息角色")
    content: str = Field(description="消息文本内容")
    tool_calls: list[ToolCallModel] | None = Field(
        default=None, description="模型工具调用声明（assistant 轮）"
    )
    tool_call_id: str | None = Field(default=None, description="关联调用 ID（tool 角色消息）")


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
    """非流式聊天响应（模型无关归一形态——content 与 tool_calls 可并存）。

    Attributes:
        content: 回复文本（首个 choice；模型发起工具调用时可为空串）。
        tool_calls: 模型工具调用声明列表（原生工具调用响应携带；空列表 =
            纯文本回复）。content 与 tool_calls 可同时非空（模型边说明
            边调用的形态——调用方按 tool_calls 非空优先走工具循环）。
        finish_reason: 结束原因（stop / length / tool_calls /
            content_filter 等；端点未提供时为 None）。
        model: 实际服务的模型名（端点回显；缺省回落请求模型名）。
        usage: token 用量（端点未回传时为 None——调用方按零用量记账）。
        latency_ms: 端到端时延（epoch 毫秒差；client 侧测量）。
    """

    content: str = Field(description="回复文本")
    tool_calls: list[ToolCallModel] = Field(
        default_factory=list, description="模型工具调用声明（空 = 纯文本回复）"
    )
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


class EmbeddingsResponseModel(BaseModel):
    """向量化响应（模型无关归一形态——embed 角色调用面）。

    Attributes:
        model: 实际服务的模型名（端点回显；缺省回落请求模型名）。
        vectors: 向量列表（与输入文本同序同长——按 index 归一）。
        usage: token 用量（端点未回传时为 None——调用方按零用量记账）。
        latency_ms: 端到端时延（epoch 毫秒差；client 侧测量）。
    """

    model: str = Field(default="", description="实际服务的模型名")
    vectors: list[list[float]] = Field(default_factory=list, description="向量列表（与输入同序）")
    usage: TokenUsageModel | None = Field(default=None, description="token 用量")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")


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


class WireToolCallFunction(BaseModel):
    """wire 工具调用函数载荷（``tool_calls[].function`` 形态）。

    Attributes:
        name: 工具名。
        arguments: 调用参数（JSON 字符串——端点协议原样；client 解析面
            robust 归一为 dict）。
    """

    name: str = Field(default="", description="工具名")
    arguments: str = Field(default="", description="调用参数（JSON 字符串）")


class WireToolCall(BaseModel):
    """wire 工具调用声明（响应 ``choices[].message.tool_calls`` 元素形态）。

    Attributes:
        id: 调用 ID（端点生成——tool 结果消息经 ``tool_call_id`` 回关联）。
        type: 调用类型（OpenAI 兼容协议当前仅 ``function``）。
        function: 函数载荷（工具名 + JSON 字符串参数）。
    """

    id: str = Field(default="", description="调用 ID（端点生成）")
    type: str = Field(default="function", description="调用类型（function）")
    function: WireToolCallFunction = Field(
        default_factory=WireToolCallFunction, description="函数载荷"
    )


class WireChatMessage(BaseModel):
    """wire 消息（chat/completions 请求 / 响应 messages 元素形态）。

    原生工具调用扩展：assistant 轮可携带 ``tool_calls``（此时 ``content``
    为 None——端点协议语义）；tool 轮携带 ``tool_call_id``。

    Attributes:
        role: 角色（system / user / assistant / tool）。
        content: 文本内容（assistant 工具调用轮可为 None）。
        tool_calls: 模型工具调用声明（响应 assistant 轮）。
        tool_call_id: 关联调用 ID（tool 轮）。
    """

    role: str = Field(description="消息角色")
    content: str | None = Field(default=None, description="消息内容")
    tool_calls: list[WireToolCall] | None = Field(default=None, description="模型工具调用声明")
    tool_call_id: str | None = Field(default=None, description="关联调用 ID")


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


class WireEmbeddingItem(BaseModel):
    """wire 向量化单项（embeddings 响应 data 元素形态）。

    Attributes:
        index: 输入序号（与请求 input 列表位置对应）。
        embedding: 向量分量列表。
        object: 载荷类型标记（端点回显，恒 ``embedding``）。
    """

    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    index: int = Field(default=0, description="输入序号")
    embedding: list[float] = Field(default_factory=list, description="向量分量列表")
    object: str = Field(default="embedding", description="载荷类型标记")


class WireEmbeddingsResponse(BaseModel):
    """wire 向量化响应（embeddings 端点响应体形态）。

    Attributes:
        model: 实际服务的模型名（端点回显）。
        data: 向量化结果列表（与请求 input 同长；client 校验）。
        usage: token 用量（embeddings 端点通常回传 prompt_tokens）。
    """

    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    model: str = Field(default="", description="实际服务的模型名")
    data: list[WireEmbeddingItem] = Field(default_factory=list, description="向量化结果列表")
    usage: WireTokenUsage | None = Field(default=None, description="token 用量")


def _serialize_message(message: ChatMessageModel) -> dict[str, Any]:
    """单条消息 → wire dict 形态（按 role 分派工具调用形态）。

    Args:
        message: 域内消息（含 assistant 工具调用轮 / tool 结果轮形态）。

    Returns:
        OpenAI 兼容 messages 元素 dict：
            - assistant 轮携带 ``tool_calls`` 时 content 为空串则序列化
              ``null``（协议语义），``arguments`` 序列化为紧凑 JSON 字符串；
            - tool 轮输出 ``tool_call_id`` + content（缺失关联 ID 时为
              空串——调用方组装契约责任）；
            - 其余角色纯 role / content 二键。
    """
    if message.role == ChatMessageRole.ASSISTANT and message.tool_calls is not None:
        return {
            "role": message.role.value,
            "content": message.content if message.content else None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == ChatMessageRole.TOOL:
        return {
            "role": message.role.value,
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    return {"role": message.role.value, "content": message.content}


def build_chat_request_body(
    model: str,
    messages: list[ChatMessageModel] | tuple[ChatMessageModel, ...],
    params: ChatRequestParams | None,
    *,
    stream: bool,
    tools: Sequence[ToolDefinitionModel] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """构建 chat/completions 请求体（wire 序列化前的 dict 形态）。

    规则：未设置的采样参数不进入请求体（端点侧默认值生效）；
    ``stream=True`` 时固定声明 ``stream_options.include_usage``（用量
    末块回传）；``params.extra`` 原样并入顶层（provider 特有参数透传）；
    ``tools`` 非空时按 OpenAI 兼容形态包裹
    （``{"type": "function", "function": {...}}``）并携带 ``tool_choice``
    （缺省 ``auto``——模型自行决定）。

    Args:
        model: 模型名（provider model 参数）。
        messages: 聊天消息序列（含 assistant 工具调用轮 / tool 结果轮）。
        params: 采样参数（None = 全默认）。
        stream: 是否流式请求（**流式恒 content-only**——与 ``tools``
            组合直接拒绝）。
        tools: 原生工具定义列表（None / 空 = 不携带工具面）。
        tool_choice: 工具选择策略（None = ``auto``；仅 ``tools`` 非空时
            进入请求体）。

    Returns:
        可直接作为 HTTP JSON body 的 dict。

    Raises:
        BusinessException: ``stream=True`` 且 ``tools`` 非空
            （``VALIDATION_ERROR``——流式传输面不支持原生工具调用，
            调用方编程错误）。
    """
    if stream and tools:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="流式请求不支持原生工具调用（流式传输面恒 content-only）",
            details={"tools": len(tools)},
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": [_serialize_message(message) for message in messages],
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters.model_dump(exclude_none=True),
                },
            }
            for tool in tools
        ]
        body["tool_choice"] = tool_choice if tool_choice is not None else TOOL_CHOICE_AUTO
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


def build_embeddings_request_body(model: str, texts: list[str]) -> dict[str, Any]:
    """构建 embeddings 请求体（wire 序列化前的 dict 形态）。

    Args:
        model: 模型名（provider model 参数）。
        texts: 待向量化文本列表（非空——空列表由调用方先行拒绝）。

    Returns:
        可直接作为 HTTP JSON body 的 dict（``{"model": ..., "input": [...]}``）。
    """
    return {"model": model, "input": list(texts)}
