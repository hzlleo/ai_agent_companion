# llm — 模型网关域（P0-4 交付）

`backend/src/packages/harness/llm/` 承载模型无关的 LLM 网关 TRANSPORT 层：OpenAI 兼容 chat/completions 客户端（手写协议——锁定决策：**不引入 openai SDK**）、角色路由门面、池门控与用量埋点。本 chunk 不含 prompt 组装（chat/ 域后续 chunk 持有 prompt Unit 文件）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `llm_models.py` | 消息 / 参数 / 响应 / wire DTO / 用量事件 Model Classes | `ChatMessageModel` / `ChatRequestParams` / `ChatResponseModel` / `ChatStreamChunkModel` / `LlmUsageEvent` |
| `client.py` | OpenAI 兼容客户端（非流式 + SSE 流式 + tenacity 重试 + 错误映射） | `LlmHttpClient` / `LlmRetryableError` / `parse_retry_after_seconds` |
| `gateway.py` | 角色路由门面（wire 构建 → entry_gate → client → 用量发射） | `LlmGateway` / `UsageRecorder` / `wire_default_usage_recorder` |

依赖方向：llm → config（model_resolver / llm_wire / Settings）+ pools（entry_gate / 共享 HTTP 池）+ core（errors / context / time_utils）；**不硬依赖 usage 域**（经注入式记录器 + 惰性接线，见下文）。

## 调用链（单一真相）

```
chat_by_role(role, messages, params)
    ↓ ① config.model_resolver.resolve(role)           角色解析（未绑定 → MODEL_NOT_FOUND）
    ↓ ② config.llm_wire.build_llm_wire_config(entry)  wire 构建（密钥解析收口：api_key_env → LLM_API_KEY 兜底 → 空串）
    ↓ ③ pools.entry_gate(entry)                        熔断预检（open → LLM_REQUEST_FAILED）+ per-entry 信号量
    ↓ ④ llm.client.chat / stream                      传输（tenacity 幂等安全集重试；超时 = POOLS_LLM_REQUEST_TIMEOUT_SECONDS）
    ↓ ⑤ 用量发射（成功 / 失败路径均落账）                LlmUsageEvent → 注入式 UsageRecorder（默认接线 → usage 域）

chat_for_entry(entry, ...)        条目直连（跳过 ①；look_at 回退链 / 显式条目调用面）
stream_by_role / _stream_for_entry  流式同构（异步生成器；角色解析在首个迭代步发生）
```

熔断结果上报（pools llm_pool 文档化契约）：④ 成功 → `breaker.record_success()`；失败 → `breaker.record_failure()`。流式消费方提前终止（GeneratorExit）不落用量（无终态结果——文档化决策）。

## LlmGateway 构造注入

```python
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder

gateway = LlmGateway(
    resolver=None,            # None = config 全局解析器（loader 热重建后自动生效）
    pool_manager=None,        # None = pools 全局单例（调用时读取；未装配 fail loud INTERNAL_ERROR）
    settings=None,            # None = config 全局单例（llm 兜底密钥 + llm_request_timeout_seconds）
    usage_recorder=wire_default_usage_recorder(),  # None = 未接线（零记录）
    retry_attempts=3,         # 传输重试总次数上限（含首次）
    retry_wait_seconds=0.5,   # 指数退避基数秒（Retry-After 缺失时）
    retry_after_max_seconds=30.0,  # Retry-After 等待封顶
)
```

`wire_default_usage_recorder()` 是 llm → usage 的唯一接线点（函数体内惰性 import——llm 模块导入期零耦合；P0-6 app 装配调用）。

## 传输客户端（client.py）

- **共享连接**：`httpx.AsyncClient` 一律注入（pools.PoolManager 下发 / 测试 MockTransport）——无私建连接（硬规则）；per-request 超时覆盖共享 client 默认；
- **非流式**：`POST {base_url}/chat/completions` → `ChatResponseModel`（content / finish_reason / model / usage / latency_ms）；
- **流式**：请求体固定声明 `stream_options.include_usage`；SSE 行解析（`data:` 载荷、`[DONE]` 终止、注释 / event 行忽略）→ `ChatStreamChunkModel` 增量迭代（末块携带 usage 时归一为 usage-only 块）；
- **重试（tenacity）**：幂等安全集 = 连接错误（ConnectError / ConnectTimeout）/ HTTP 5xx / 408 / 429；429 尊重 `Retry-After` 头（秒数或 HTTP-date；封顶 `retry_after_max_seconds`），缺失回落指数退避；**流式仅「发起 → 2xx 确认」阶段可重试**（首块产出后重放非幂等）；
- **超时**：`settings.pools.llm_request_timeout_seconds`（env `POOLS_LLM_REQUEST_TIMEOUT_SECONDS`，默认 120）。

### 错误码映射表（复用既有 LLM_*——本 chunk 零新增）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| 连接错误（ConnectError / ConnectTimeout） | `LLM_CALL_FAILED` | 503 | 是 |
| HTTP 5xx / 408 / 429 | `LLM_CALL_FAILED` | 503 | 是（429 尊重 Retry-After） |
| HTTP 401 / 403（鉴权失败） | `LLM_API_KEY_MISSING` | 500 | 否 |
| 其余 HTTP 4xx | `LLM_CALL_FAILED` | 503 | 否 |
| 读 / 写 / 池超时 | `LLM_REQUEST_FAILED` | 503 | 否 |
| 响应 JSON 非法 / choices 空 / SSE 块非法 | `LLM_CALL_FAILED` | 503 | 否 |
| 条目熔断中（entry_gate 预检） | `LLM_REQUEST_FAILED` | 503 | —（冷却剩余毫秒在 details） |
| 角色未绑定条目 | `MODEL_NOT_FOUND` | 404 | — |

429 的 `Retry-After` 解析值同时进入 `details.retry_after_seconds` 与异常 `headers["Retry-After"]`（app 层可透传）。

## wire 请求 / 响应 JSON 形态

非流式请求体（`build_chat_request_body`）：

```json
{
  "model": "glm-5.3",
  "messages": [
    {"role": "system", "content": "你是陪伴智能体"},
    {"role": "user", "content": "今天有点累"}
  ],
  "temperature": 0.7,
  "max_tokens": 1024,
  "top_p": 0.9,
  "presence_penalty": 0.1
}
```

流式请求体额外携带 `"stream": true` + `"stream_options": {"include_usage": true}`；未设置的采样参数不进入请求体；`params.extra` 原样并入顶层（provider 特有参数透传）。

非流式响应（wire，`extra="ignore"` 容忍端点附加字段）：

```json
{
  "id": "resp-1",
  "model": "glm-5.3",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "抱抱你，今天发生什么了？"},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 52, "completion_tokens": 18, "total_tokens": 70}
}
```

流式 SSE 形态（`data:` 行；末两块为 usage-only 块 + `[DONE]`）：

```
data: {"id":"resp-1","model":"glm-5.3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"resp-1","model":"glm-5.3","choices":[{"index":0,"delta":{"content":"抱抱"},"finish_reason":null}]}

data: {"id":"resp-1","model":"glm-5.3","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":"stop"}]}

data: {"id":"resp-1","model":"glm-5.3","choices":[],"usage":{"prompt_tokens":52,"completion_tokens":18,"total_tokens":70}}

data: [DONE]
```

## Model Classes 字段表

`ChatMessageModel`：`role`（system/user/assistant）/ `content`（文本；多模态内容分块由后续多模态 chunk 扩展）。

`ChatRequestParams`：`temperature`（0.0-2.0）/ `max_tokens`（≥1）/ `top_p`（0-1.0）/ `extra`（provider 透传 dict）——未设置字段不进 wire 请求体。

`ChatResponseModel`：`content` / `finish_reason` / `model`（端点回显，缺省回落请求名）/ `usage: TokenUsageModel | None` / `latency_ms`（epoch 毫秒差）。

`TokenUsageModel`：`input_tokens` / `output_tokens` / `total_tokens`（未提供时按 input+output 归一）。

`ChatStreamChunkModel`：`content`（本块增量，可空串）/ `finish_reason` / `usage`（仅末块）。

`LlmUsageEvent`（网关 → usage 注入契约）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts_ms` | int | 调用发起时间（epoch 毫秒） |
| `persona_id` | str \| None | 人格 ID（None = 系统调用；显式参数 → 上下文变量 → None） |
| `purpose` | str | chat/duty/dream/look_at/embed/tts/asr/tool |
| `model_entry` | str | 模型条目名 |
| `provider` | str | provider 类型（默认 openai_compatible） |
| `input_tokens` / `output_tokens` / `total_tokens` | int | token 数（错误路径 / 端点未回传为 0） |
| `latency_ms` | int | 端到端时延（含失败耗时） |
| `status` | str | success / error |
| `request_id` | str \| None | 请求追踪 ID（显式参数 → 上下文变量 → None） |
| `error_code` | str \| None | 失败错误码（成功为 None） |

## 用量发射语义

- 成功 / 失败路径**均落账**（失败 tokens 为 0，调用计数 + 时延仍有观测价值）；流式以末块 usage 为准（无则零用量）；
- persona_id / request_id 解析序：显式参数 → `core.context` 上下文变量（请求中间件已绑定则自动携带）→ None；
- 记录器异常**仅告警不阻断主调用**（用量是观测面不是业务面）；未接线（recorder=None）零记录。

## 测试与门禁

- 测试位置：`src/tests/unit/test_llm_models.py` / `test_llm_client.py` / `test_llm_gateway.py`（3 份）；
- 全部 `async def test_`；HTTP 一律 `httpx.MockTransport`（零真实出站）；网关经注入解析器 + 隔离 Settings 的真实 PoolManager（共享 client 替换为 MockTransport）；重试等待零 sleep（Retry-After 0 / 等待策略直测）；熔断 open 经预开熔断器确定性触发；
- 默认接线全链测试：gateway → `wire_default_usage_recorder` → usage 服务 → 共享 SQLite 引擎（`test_llm_gateway.py::test_default_usage_recorder_full_chain`）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/llm 组 **95%**（client 95% / gateway 100% / llm_models 100%）；
- 运行：`cd backend && pixi run tests-unit`。
