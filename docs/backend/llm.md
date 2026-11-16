# llm — 模型网关域（P0-4 交付；P1-1 扩展 embeddings；P2-1 增注册表管理 API）

`backend/src/packages/harness/llm/` 承载模型无关的 LLM 网关 TRANSPORT 层：OpenAI 兼容 chat/completions 客户端（手写协议——锁定决策：**不引入 openai SDK**）、角色路由门面、池门控与用量埋点；P1 记忆层 chunk 1 扩展了 embeddings 传输面（`POST /embeddings`——记忆检索的向量化前置）。本 chunk 不含 prompt 组装（chat/ 域持有 prompt Unit 文件）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `llm_models.py` | 消息 / 参数 / 响应 / embeddings / wire DTO / 用量事件 Model Classes | `ChatMessageModel` / `ChatRequestParams` / `ChatResponseModel` / `ChatStreamChunkModel` / `EmbeddingsResponseModel` / `LlmUsageEvent` |
| `client.py` | OpenAI 兼容客户端（非流式 + SSE 流式 + embeddings + tenacity 重试 + 错误映射） | `LlmHttpClient` / `LlmRetryableError` / `parse_retry_after_seconds` |
| `gateway.py` | 角色路由门面（wire 构建 → entry_gate → client → 用量发射；chat 与 embed 双面） | `LlmGateway` / `UsageRecorder` / `wire_default_usage_recorder` |

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
embed_by_role(texts, ...)         向量化（①角色解析恒 embed → ③门控 → POST /embeddings → ⑤purpose="embed" 用量发射）
```

熔断结果上报（pools llm_pool 文档化契约）：④ 成功 → `breaker.record_success()`；失败 → `breaker.record_failure()`。流式消费方提前终止（GeneratorExit）不落用量（无终态结果——文档化决策）。角色未绑定（含 embed 角色未绑定）在传输前失败——`MODEL_NOT_FOUND`，无条目可归属不落用量行（与 chat_by_role 同语义）。

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

## embeddings 传输面（P1-1——记忆检索的向量化前置）

`gateway.embed_by_role(texts, persona_id=None, request_id=None) -> EmbeddingsResponseModel`：解析 **embed 角色**条目（本地向量化端点为生产形态；测试 MockTransport）→ wire 构建 → `entry_gate`（熔断 + 信号量）→ `client.embeddings(texts)`（`POST {base_url}/embeddings`）→ 用量发射（**purpose 恒 `embed`**——usage 域枚举既有成员；token 计数取响应 `usage.prompt_tokens / total_tokens`，未回传按零用量落账）。

请求体（`build_embeddings_request_body`）：

```json
{"model": "bge-m3", "input": ["今天上班好累呀", "想吃小鱼干"]}
```

响应（wire，`extra="ignore"` 容忍端点附加字段）与归一：

```json
{
  "object": "list",
  "model": "bge-m3",
  "data": [
    {"index": 0, "object": "embedding", "embedding": [0.011, -0.023, 0.057]},
    {"index": 1, "object": "embedding", "embedding": [0.044, 0.019, -0.008]}
  ],
  "usage": {"prompt_tokens": 14, "completion_tokens": 0, "total_tokens": 14}
}
```

- 归一规则：`data` 按 `index` 归位（与输入同序同长）；数量不符 / 序号越界 → `LLM_CALL_FAILED`；
- 空输入列表 → `VALIDATION_ERROR`（调用方编程错误——不发请求）；
- 重试 / 超时 / 错误码映射与 chat 传输面完全一致（同一 `_post_with_retry` 通道，幂等安全集重试）；
- `EmbeddingsResponseModel`：`model` / `vectors: list[list[float]]` / `usage: TokenUsageModel | None` / `latency_ms`；
- chat 路径零变更（`embeddings` 为 client 新增方法；`_post_with_retry` 增加 `url` 参数默认回落 chat 端点）。

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

## 注册表管理 API（P2 chunk 1——`app/api/models.py` + `harness/config/registry_admin.py`）

config.yaml 是 LLM 注册表**唯一真源**：`/api/v1/models` 管理面对注册表的全部突变经 harness.config.registry_admin 完成「校验 → 原子持久化 → 热重建 → 失败回滚」链；`.env` 与 `extensions_config.json` 不在该路由触达范围。全局管理面（无人格上下文——注册表是进程级配置真源，非 PersonaMixin 数据）。

### 端点一览

| 端点 | 语义 | 错误面 |
|---|---|---|
| `GET /api/v1/models/entries` | 注册表状态（条目脱敏 + 解析状态 + 显式绑定） | — |
| `GET /api/v1/models/roles` | 五角色绑定面（chat/duty/dream/look_at/embed，bound/unbound） | — |
| `POST /api/v1/models/entries` | 创建条目 | 409 唯一名冲突 / 400 请求校验 / 422·500 写路径故障 |
| `PUT /api/v1/models/entries/{name}` | 部分更新（name 不可变；仅出现字段生效） | 404 / 400 / 422·500 |
| `DELETE /api/v1/models/entries/{name}` | 删除（角色绑定守卫） | 404 / 409 仍绑定角色 / 422·500 |

每次突变返回写后注册表状态（脱敏）。创建 / 更新请求体（Model Classes，`extra="forbid"`——携带 `api_key` 密钥字段即 400）：

```json
{
  "name": "vision-helper",
  "provider": "openai_compatible",
  "base_url": "http://127.0.0.1:9000/v1",
  "api_key_env": "VISION_HELPER_API_KEY",
  "model": "qwen2.5-vl-7b",
  "roles": ["look_at"],
  "concurrency": 2,
  "multimodal": true
}
```

字段规则：`name` 非空且全局唯一（strip 后校验）；`base_url` 须 http(s) URL；`model` 非空；`api_key_env` 只接受**环境变量名**（密钥值永不进请求 / 响应 / yaml——密钥唯一入口是 .env / 进程环境变量）；`roles` 值域同 `ModelRole`；`concurrency` ≥ 1（本工程注册表无 per-entry 超时字段——请求超时统一走全局 `POOLS_LLM_REQUEST_TIMEOUT_SECONDS`）。更新体无 `name`（不可变）；`concurrency` **缺席 = 不变，显式 null = 重置全局默认**（经 `model_fields_set` 区分）；`roles: []` = 清空声明。

读取面（脱敏 + 解析状态）：

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": {
    "entries": [
      {
        "name": "main-chat",
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key_env": "MAIN_CHAT_API_KEY",
        "model": "glm-5.3",
        "roles": ["chat", "duty"],
        "resolved_roles": ["chat", "dream", "duty"],
        "concurrency": 4,
        "multimodal": false
      }
    ],
    "role_bindings": {"dream": "main-chat"}
  }
}
```

`resolved_roles` = 解析器当前实际解析到该条目的角色集（显式绑定 + 声明先注册者胜出的合并裁决）；不含端点健康状态（管理面不探测端点）。

### 写路径链（原子持久化 + 热重建 + 回滚——顺序单一真相）

```
校验（DTO：http(s) / 非空 / extra=forbid；服务：唯一 409 / 存在 404 / 删除守卫 409）
    ↓ 构造新 LlmConfig（不可变重建——绝不原地修改运行时对象；asyncio.Lock 串行化突变窗口）
    ↓ 原子持久化：写前整文件备份 config.yaml.bak-<epoch-ms>（shutil.copy2）
    │             → 临时文件 config.yaml.tmp-<epoch-ms> 写入 + flush + fsync
    │             → os.replace 原子换名（崩溃安全：config.yaml 恒为完整旧版或完整新版）
    │             → 备份轮换（保留最新 10 份 REGISTRY_BACKUP_KEEP，更旧清理）
    ↓ 热重建：load_config_yaml(path) 重读磁盘——证明落盘内容可加载，
    │         并整体换血运行时（runtime_llm_config + 解析器索引 + 回退链记录）
    ↓ 重建失败 → apply_llm_config_to_runtime(写前快照) 回滚内存注册表
                + BusinessException(CONFIG_INVALID 422)
```

- **持久化目标** = 进程实际加载的 config.yaml（`loader.get_loaded_config_yaml_path()`——启动期 `config_dir` 注入与写路径天然一致；首次部署无文件时在该路径创建，无写前备份）；
- **llm 段之外的顶层键原样保留**（向前兼容后续配置段）；既有文件不可解析 / 顶层非 mapping → 422 拒绝覆写（fail loud，不破坏不可审计内容）；
- **回滚语义**：仅回滚**内存**注册表（写已成功、失败在重建）；磁盘保留新内容，`.bak-<epoch-ms>` 保留写前内容供人工恢复，下次成功写自愈覆盖；
- **原子写失败**（备份 / 写入 / 换名异常）：临时文件即时清理（不留 `.tmp-*` 残留），原文件不变，`INTERNAL_ERROR` 500；
- **阻塞 I/O 下沉**：文件读写 / 备份 / 换名经 `run_blocking_ops`（core 共享线程池）。

### 删除守卫（两步操作约定）

仍承接任一角色的条目禁止删除（409 `CONFLICT`，`details.bound_roles` 为显式绑定 + 条目声明合并的绑定集）。解除路径（两步）：

1. 条目**声明**的角色：`PUT /api/v1/models/entries/{name}` 置 `roles: []`；
2. **显式 `role_bindings`** 指向的条目：先在 config.yaml 手工移除该绑定（本管理面不提供显式绑定编辑端点——显式绑定编辑属后续 chunk 评审面）。

再执行 `DELETE`。

### 测试与门禁（注册表管理面）

- 测试位置：`src/tests/unit/test_models_api.py`（ASGI + 处理器直调契约面）：脱敏 / 解析状态 / 创建→落盘→热重建 round-trip / 部分更新（含 concurrency 显式 null 重置）/ 删除守卫与两步解绑流 / 唯一名 409 / 非法 base_url·未知字段·未知角色 400 / 原子写失败不留临时文件 / 重建失败内存回滚（注入仅首次失败的解析器）/ 备份生成与轮换（11+ 次持久化后保留 10 份）/ 不可解析既有文件拒绝覆写 / 写路径追踪访问器；
- 密钥防泄漏断言：测试注入真实密钥环境变量值，断言该值永不出现于任何响应面；
- 覆盖率（`pixi run coverage-gates` 实测）：harness/config 组 97.3%（含 registry_admin.py / registry_admin_dtos.py 新文件）；app/api 组 96.5%。

## 测试与门禁

- 测试位置：`src/tests/unit/test_llm_models.py` / `test_llm_client.py` / `test_llm_gateway.py` / `test_llm_embeddings.py`（4 份）；
- 全部 `async def test_`；HTTP 一律 `httpx.MockTransport`（零真实出站）；网关经注入解析器 + 隔离 Settings 的真实 PoolManager（共享 client 替换为 MockTransport）；重试等待零 sleep（Retry-After 0 / 等待策略直测）；熔断 open 经预开熔断器确定性触发；embeddings 面覆盖请求形态 / index 归位 / 数量与序号契约 / purpose=embed 用量发射 / persona 解析序 / 角色未绑定；
- 默认接线全链测试：gateway → `wire_default_usage_recorder` → usage 服务 → 共享 SQLite 引擎（`test_llm_gateway.py::test_default_usage_recorder_full_chain`）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/llm 组 **95%**（client 95% / gateway 100% / llm_models 100%）；
- 运行：`cd backend && pixi run tests-unit`。
