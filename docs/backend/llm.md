# llm — 模型网关域（含 embeddings 传输面 + 注册表管理 API）

`backend/src/packages/harness/llm/` 承载模型无关的 LLM 网关 TRANSPORT 层：OpenAI 兼容 chat/completions 客户端（手写协议——锁定决策：**不引入 openai SDK**）、角色路由门面、池门控与用量埋点；记忆层扩展了 embeddings 传输面（`POST /embeddings`——记忆检索的向量化前置）。本域不含 prompt 组装（chat/ 域持有 prompt Unit 文件）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `llm_models.py` | 消息 / 参数 / 响应 / embeddings / wire DTO / 用量事件 Model Classes（含原生工具调用与多模态内容分块契约） | `ChatMessageModel` / `ChatRequestParams` / `ChatResponseModel` / `ChatStreamChunkModel` / `EmbeddingsResponseModel` / `LlmUsageEvent` / `ToolDefinitionModel` / `ToolCallModel` / `TextContentPart` / `ImageContentPart` / `ContentPartModel` / `build_chat_request_body` / `messages_have_content_parts` |
| `client.py` | OpenAI 兼容客户端（非流式 + SSE 流式 + embeddings + tenacity 重试 + 错误映射） | `LlmHttpClient` / `LlmRetryableError` / `parse_retry_after_seconds` |
| `gateway.py` | 角色路由门面（wire 构建 → entry_gate → client → 用量发射；chat 与 embed 双面；条件路由 + 内置亲密策略装配） | `LlmGateway` / `UsageRecorder` / `wire_default_usage_recorder` |
| `route_policy_models.py` | 条件路由 Model Classes：策略 / 条件 / 时段 / 裁决契约（Unit 文件） | `RoutePolicyModel` / `RouteConditionModel` / `TimeWindowModel` / `RouteDecisionModel` / `RouteDecisionReason` |
| `route_policy.py` | 条件路由纯函数：优先级排序 / 条件矩阵 / 时段换算 / 目标绑定检查（Unit 文件） | `evaluate_route` / `time_window_matches` |
| `intimate_models.py` | 内置亲密策略配置面：`LLM_INTIMATE_*` 环境契约（Unit 文件） | `IntimateChannelConfig` / `INTIMATE_CHANNEL_ENTRY_NAME` |
| `intimate_router.py` | 内置亲密策略装配面：条目构建 / 半配置告警（Unit 文件） | `build_intimate_entry` / `log_incomplete_intimate_config` |

依赖方向：llm → config（model_resolver / llm_wire / Settings）+ pools（entry_gate / 共享 HTTP 池）+ core（errors / context / time_utils）；**不硬依赖 usage 域**（经注入式记录器 + 惰性接线，见下文）。

## 调用链（单一真相）

```
chat_by_role(role, messages, params)
    ↓ ◇ 条件路由裁决（route_policies + 内置策略 → effective_role；见「条件路由策略」节）
    ↓ ① config.model_resolver.resolve(effective_role)  角色解析（未绑定 → MODEL_NOT_FOUND）
    ↓ ⓪ LLM_DAILY_BUDGET_* 日预算闸                 超限 → RATE_LIMITED（不落用量）
    ↓ ② config.llm_wire.build_llm_wire_config(entry)  wire 构建（密钥解析收口：api_key_env → LLM_API_KEY 兜底 → 空串）
    ↓ ③ pools.entry_gate(entry)                        熔断预检（open → LLM_REQUEST_FAILED）+ per-entry 信号量
    ↓ ④ llm.client.chat / stream                      传输（tenacity 幂等安全集重试；超时 = POOLS_LLM_REQUEST_TIMEOUT_SECONDS）
    ↓ ⑤ 用量发射（成功 / 失败路径均落账）                LlmUsageEvent → 注入式 UsageRecorder（默认接线 → usage 域）

chat_for_entry(entry, ...)        条目直连（跳过 ①；look_at 回退链 / 显式条目调用面）
stream_by_role / _stream_for_entry  流式同构（异步生成器；角色解析在首个迭代步发生）
embed_by_role(texts, ...)         向量化（⓪预算闸在 ①角色解析之前 → ①角色解析恒 embed → ③门控 → POST /embeddings → ⑤purpose="embed" 用量发射）
chat_by_role(..., intimacy_level=N)  条件路由 / 内置亲密策略（见「条件路由策略」与「内置亲密策略」节；未命中走上面的角色链）
```

预算闸位置（与实码一致）：chat / stream 在角色解析之后、条目门控 / 传输之前；
embed 在角色解析之前。

熔断结果上报（pools llm_pool 文档化契约）：④ 成功 → `breaker.record_success()`；失败 → `breaker.record_failure()`。流式消费方提前终止（GeneratorExit）不落用量（无终态结果——文档化决策）。角色未绑定（含 embed 角色未绑定）在传输前失败——`MODEL_NOT_FOUND`，无条目可归属不落用量行（与 chat_by_role 同语义）。

## LlmGateway 构造注入

```python
from packages.harness.llm.gateway import LlmGateway, wire_default_usage_recorder

gateway = LlmGateway(
    resolver=None,            # None = config 全局解析器（loader 热重建后自动生效；条目与别名，策略集除外）
    pool_manager=None,        # None = pools 全局单例（调用时读取；未装配 fail loud INTERNAL_ERROR）
    settings=None,            # None = config 全局单例（llm 兜底密钥 + llm_request_timeout_seconds）
    usage_recorder=wire_default_usage_recorder(),  # None = 未接线（零记录）
    usage_counter=wire_default_usage_counter(),    # None = 零预算门禁（0 = 不限）
    clock=now_ms,             # 预算日桶 / 时延统计时钟（测试可注入）
    retry_attempts=3,         # 传输重试总次数上限（含首次）
    retry_wait_seconds=0.5,   # 指数退避基数秒（Retry-After 缺失时）
    retry_after_max_seconds=30.0,  # Retry-After 等待封顶
    top_logprobs=20,          # 每次生成位置回传前 N 候选（None = 省略；LLM_TOP_LOGPROBS）
    prompt_logprobs=20,       # 提示词 logprobs 数（None = 省略；LLM_PROMPT_LOGPROBS；流式恒省）
    intimate_config=None,     # None = 按 LLM_INTIMATE_* env 构造（默认关闭形态）
    route_policies=None,      # None/空 = 无用户策略；app 装配读 config.yaml llm.route_policies
)
```

app 装配从 `Settings.llm.top_logprobs` / `Settings.llm.prompt_logprobs`
读取后传入网关（env `LLM_TOP_LOGPROBS` / `LLM_PROMPT_LOGPROBS`）。

`wire_default_usage_recorder()` / `wire_default_usage_counter()` 是 llm → usage 的仅有两个接线点（函数体内惰性 import——llm 模块导入期零耦合；app 装配调用）。

## 条件路由策略

`config.yaml` 的 `llm.route_policies` 声明条件路由：**原角色在条件命中时
改由目标角色条目承接**。策略求值是纯函数
（`packages.harness.llm.route_policy.evaluate_route`），网关在
`chat_by_role` / `stream_by_role` 入口统一调用；命中后 `effective_role`
决定解析的条目，`resolved_purpose` 仍按原角色（预算 / 用量归属不变）。

```yaml
llm:
  models:
    - name: "main-chat"
      base_url: "https://api.example.com/v1"
      api_key_env: "MAIN_CHAT_API_KEY"
      model: "glm-5.3"
      roles: ["chat"]
    - name: "chat-night"
      base_url: "https://api.example.com/v1"
      api_key_env: "CHAT_NIGHT_API_KEY"
      model: "glm-5.3-night"
      roles: []                      # 目标角色经 role_bindings 绑定
  role_bindings:
    chat_night: "chat-night"         # 策略目标角色 → 条目名
  route_policies:
    - name: "night-chat"
      source_role: "chat"
      target_role: "chat_night"
      priority: 100
      condition:
        time_window:
          start_hour: 22
          end_hour: 6
          timezone: "Asia/Shanghai"
      fallback_to_source: true
```

### 配置字段表

`RoutePolicyModel`：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | `str (min_length=1)` | — | 策略名（日志与错误 details 引用；非空） |
| `source_role` | `str (min_length=1)` | — | 原角色名（调用方声明的角色） |
| `target_role` | `str (min_length=1)` | — | 目标角色名（不得等于 `source_role`） |
| `priority` | `int` | `0` | 优先级（降序求值；同优先级按声明序） |
| `condition` | `RouteConditionModel` | — | 命中条件（各维度 AND） |
| `fallback_to_source` | `bool` | `true` | 目标角色未绑定条目时回落原角色；`false` 抛 `MODEL_NOT_FOUND` |

`RouteConditionModel`：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_intimacy_level` | `int \| None (0-3)` | `None` | 最低亲密度（None = 不约束下沿） |
| `max_intimacy_level` | `int \| None (0-3)` | `None` | 最高亲密度（须不小于下沿；None = 不约束上沿） |
| `time_window` | `TimeWindowModel \| None` | `None` | 时段窗口（None = 不约束） |
| `requires_summary` | `bool \| None` | `None` | True = 仅摘要可用；False = 仅摘要不可用；None = 不约束 |
| `purposes` | `tuple[str, ...]` | `()` | 对话类型白名单（本轮 `resolved_purpose`：`chat` / `duty` / `dream` 等既调用途名；空 = 不约束） |

`TimeWindowModel`：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `start_hour` | `int (0-23)` | — | 窗口起始小时（含） |
| `end_hour` | `int (0-23)` | — | 窗口结束小时（不含）；`<= start_hour` 视为跨午夜（如 22→6） |
| `timezone` | `str` | `"UTC"` | IANA 时区名；装配期经 `zoneinfo.ZoneInfo` 校验，非法名拒绝加载 |

### 求值顺序

```
evaluate_route(role, policies, intimacy_level, now_ms, has_summary, purpose, resolver)
    ↓ ① 仅保留 source_role == 原角色 的策略
    ↓ ② priority 降序（同优先级按声明序）
    ↓ ③ 逐条检查条件（亲密度区间 / 摘要可用性 / 时段 / 用途——AND）
    ↓ 第一个全命中策略 → resolver.resolve_optional(target_role)
        ├─ 已绑定 → routed（生效角色 = target_role）
        ├─ 未绑定 + fallback_to_source=true → fallback_unbound（回落原角色）
        └─ 未绑定 + fallback_to_source=false → MODEL_NOT_FOUND（details 含策略名）
    ↓ 无策略命中 → direct（生效角色 = 原角色）
```

- **时段**：以调用方注入的 `now_ms`（epoch 毫秒）在策略声明时区换算小时；
  网关时钟可注入（测试零真实等待）。跨午夜窗口按 `[start, 24) ∪ [0, end)`
  处理，`end == start` 覆盖全天。
- **用途维度**：条件比较的是 `resolved_purpose`（显式 `purpose` 覆盖，缺省
  为原角色名）——即「对话类型」的配置面；命中改道不改变用途归属。
- **与 `role_bindings` 的关系**：目标角色不是注册表五角色时必须经
  `role_bindings`（条目声明 `roles` 仅限五角色）绑定到具体条目；loader
  在重建解析器后把这些绑定登记为路由别名。未绑定条目按策略回落或抛错，
  不会静默直连。
- **条目默认参数**：目标条目可声明 `default_params`（如通道默认温度），
  网关只补齐调用方未设置的采样字段；调用方显式值优先。
- **日志**：命中 / 回落记录一条 `logger.info`（策略名、原角色、生效角色、
  reason；不打印消息内容）；回落路径附带求值轨迹摘要
  （`policy→[dimension:matched,...]`），未命中的 `direct` 在有源角色匹配
  策略时记一条 `logger.debug` 轨迹。

### 热重载边界

`route_policies` 与内置亲密策略在网关构造期快照；运行中重载 config.yaml
只刷新条目与 `intimate` 别名绑定，策略增删改须重启进程生效。

## 内置亲密策略（roles:intimate）

长期陪伴对话允许把一个**独立的 OpenAI 兼容端点**接成亲密回复通道：运营可
为该端点单独调模型 / 采样温度 / 输出策略，而主对话端点（`roles:chat`）
保持原样。该通道**默认关闭**，未开启时全部请求走既有 chat 角色通道，请求
字节零变化。

**实现形态**：通道不再由网关特判，而是在装配期转译为一条内置条件路由
策略（`name="__intimate_builtin"`、源角色 `chat`、目标角色 `intimate`、
优先级最低 `-999`、`requires_summary=true`、`fallback_to_source=true`），
并把通道条目登记为 `intimate` 路由别名。用户 `route_policies` 的优先级
高于内置策略，因此可覆盖。`LLM_INTIMATE_*` env 面、配置类与语义保持不变
（与「条件路由策略」节共用同一求值路径）。

通道标识 `roles:intimate` 只存在于本 env 面 / 路由别名，**不进入**
config.yaml 的模型注册表（`ModelRole` 维持 chat / duty / dream / look_at /
embed 五角色，注册表管理 API 面不变）。

### 配置键表（`.env.example` / Setup Wizard「亲密回复通道配置」步骤同源）

| 环境变量 | 配置字段 | 默认 | 说明 |
|---|---|---|---|
| `LLM_INTIMATE_ENABLED` | `enabled` | `false` | 全局开关；与配置齐全性同时为真才可能被路由选中 |
| `LLM_INTIMATE_BASE_URL` | `base_url` | 空 | 亲密端点根路径（含 `/v1`；空 = 未配置） |
| `LLM_INTIMATE_API_KEY` | `api_key` | 空 | 亲密端点凭据（`SecretStr`；装载期去除首尾空白，仅空白 = 未配置；**不回落**全局 `LLM_API_KEY`） |
| `LLM_INTIMATE_MODEL` | `model` | 空 | 亲密端点模型名（空 = 未配置） |
| `LLM_INTIMATE_TEMPERATURE` | `temperature` | 空 | 通道缺省采样温度（0-2；空 = 不注入；调用方显式 temperature 优先） |
| `LLM_INTIMATE_MIN_INTIMACY_LEVEL` | `min_intimacy_level` | `2` | 触发通道的最低亲密等级（0-3，与关系摘要 `intimacy_level` 同轴） |

配置类：`packages.harness.llm.intimate_models.IntimateChannelConfig`
（`env_prefix="LLM_INTIMATE_"`，不经 config 域 `Settings` 聚合——与
`COMPLIANCE_` / `INJECTION_` 同形态）。

### 启用条件与半配置策略

- 通道可用 = `LLM_INTIMATE_ENABLED=true` **且** `base_url` / `api_key` /
  `model` 三项全部非空；
- **半配置（锁定）**：开关为真但必填项缺任一项时，整通道视为未启用，
  网关在装配期记**一条** warning（列出缺失的 `LLM_INTIMATE_*` 变量名），
  不因半配置抛错阻断回复；
- 未配置任何 `LLM_INTIMATE_*`（默认部署形态）等同于关闭——不产生 warning。

### 路由矩阵

调用方（chat 域）只传本轮关系摘要的 `intimacy_level`（0-3；摘要缺失 /
读取降级传 `None` / 0）。内置策略的命中条件 = 关系摘要可用（
`requires_summary=true`）且等级 ≥ `min_intimacy_level`；开关关闭 / 半配置
时不生成内置策略，等价于无策略直连。判定顺序固定，先命中者即结论：

| 开关 | 配置齐全 | 本轮等级 | 等价裁决原因 | 实际通道 |
|---|---|---|---|---|
| 关 | 任意 | 任意 | `direct`（无内置策略） | 默认（chat 角色条目） |
| 开 | 否 | 任意 | `direct`（无内置策略，并记一条 warning） | 默认 |
| 开 | 是 | `None`（摘要不可用） | `direct`（条件不命中） | 默认 |
| 开 | 是 | 低于 `min_intimacy_level` | `direct`（条件不命中） | 默认 |
| 开 | 是 | ≥ `min_intimacy_level` | `routed`（`__intimate_builtin`） | 亲密通道 |

关系摘要读取失败时 chat 域降级为空摘要（stranger / 0），因此该回合不满足
`min_intimacy_level` 并回退默认通道——亲密通道不自行读库、不猜关系状态。
`reply` 与 swipes 变体重生成同源：两者都经事实读取面派生关系摘要并把
`intimacy_level` 传给网关；swipe 的关系读取面未接线时输入为 `None`，读取失败
按空摘要（0）降级——均回退默认通道。

### 命中时链路

```
chat_by_role(role, messages, params, intimacy_level=N)
    ↓ ① evaluate_route(..., policies=[用户策略, __intimate_builtin])  条件路由求值
    ↓ ② resolver.resolve_optional("intimate")                内置条目别名（装配期登记）
    ↓ ③ build_intimate_entry(config)                         条目键 = llm-intimate-channel；通道默认温度挂 default_params
    ↓ ④ pools.entry_gate(条目)                               公共池：专属信号量 + 熔断器
    ↓ ⑤ llm.client.chat / stream                             共享 HTTP 池；wire 凭据 = LLM_INTIMATE_API_KEY
    ↓ ⑥ 用量发射（purpose 保持原角色用途，model_entry=llm-intimate-channel）
```

消息序列、原生工具面、输出协议与合规重试语义完全复用——只有端点 / 模型 /
凭据 / 缺省温度被替换。流式路径同一判据；`stream_reply` 的增量块形态不变。

请求体示例（命中形态；messages / tools 与默认通道一致，仅 model 变化）：

```json
{
  "model": "intimate-glm",
  "messages": [
    {"role": "system", "content": "你是大肥鱼……"},
    {"role": "user", "content": "今天想抱抱"}
  ],
  "temperature": 1.1,
  "max_tokens": 512,
  "logprobs": true,
  "top_logprobs": 20,
  "prompt_logprobs": 20
}
```

### 失败语义（不做静默换道）

- 通道未配置 / 未开启 / 等级不足 / 摘要缺失 → 默认通道（不是失败）；
- **已配置且被选中的通道调用失败**（连接、5xx、超时、响应解析）按既有
  `LLM_*` 错误映射原样上抛，不自动重试默认通道——调用方看到的是同一
  回合的失败，而不是被悄悄换成一个语气不同的端点；
- 条目熔断 / 半开语义与注册表条目一致（独立条目键
  `llm-intimate-channel`，不污染 chat 条目健康信号）；
- 本地校验错误（流式携带多模态分块等）与默认通道同语义，在门控前拒绝。

### 默认关闭说明

- 代码默认 `enabled=False` 且三项必填为空；`config.example.yaml` 与
  `.env.example` 均以关闭形态生成；
- 未注入 `intimate_config` 的网关按 env 现场构造；测试 / 装配可显式注入
  `LlmGateway(intimate_config=...)` 覆盖（隔离配置，不依赖真实 `.env`）；
- 关闭形态的请求体与「从未提供该功能」逐字节一致（回归测试覆盖）。

## 传输客户端（client.py）

- **共享连接**：`httpx.AsyncClient` 一律注入（pools.PoolManager 下发 / 测试 MockTransport）——无私建连接（硬规则）；per-request 超时覆盖共享 client 默认；
- **非流式**：`POST {base_url}/chat/completions` → `ChatResponseModel`（content / finish_reason / model / usage / latency_ms）；
- **流式**：请求体固定声明 `stream_options.include_usage`；SSE 行解析（`data:` 载荷、`[DONE]` 终止、注释 / event 行忽略）→ `ChatStreamChunkModel` 增量迭代（末块携带 usage 时归一为 usage-only 块）；流式请求**恒省略 `prompt_logprobs`**（vLLM 对 `stream=true` 且 `prompt_logprobs>0` 返回 400，且流式响应不回传该字段——见「logprobs 与置信度」节）；
- **重试（tenacity）**：幂等安全集 = 连接错误（ConnectError / ConnectTimeout）/ HTTP 5xx / 408 / 429；429 尊重 `Retry-After` 头（秒数或 HTTP-date；封顶 `retry_after_max_seconds`），缺失回落指数退避；**流式仅「发起 → 2xx 确认」阶段可重试**（首块产出后重放非幂等）；
- **超时**：`settings.pools.llm_request_timeout_seconds`（env `POOLS_LLM_REQUEST_TIMEOUT_SECONDS`，默认 120）。

### 错误码映射表（复用既有 LLM_*——本域零新增）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| 连接错误（ConnectError / ConnectTimeout） | `LLM_CALL_FAILED` | 503 | 是 |
| HTTP 5xx / 408 / 429 | `LLM_CALL_FAILED` | 503 | 是（429 尊重 Retry-After） |
| HTTP 401 / 403（鉴权失败） | `LLM_API_KEY_MISSING` | 500 | 否 |
| 其余 HTTP 4xx | `LLM_CALL_FAILED` | 503 | 否 |
| 读 / 写 / 池超时（含流中段 ReadTimeout） | `LLM_REQUEST_FAILED` | 503 | 否 |
| 流中段传输中断（ReadError / RemoteProtocolError——非超时） | `LLM_CALL_FAILED` | 503 | 否 |
| 响应 JSON 非法 / choices 空 / SSE 块非法 | `LLM_CALL_FAILED` | 503 | 否 |
| tool_calls arguments 非法 JSON | `LLM_CALL_FAILED` | 503 | 否 |
| tool_calls arguments 非 JSON 对象 | `LLM_CALL_FAILED` | 503 | 否 |
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
  "presence_penalty": 0.1,
  "logprobs": true,
  "top_logprobs": 20,
  "prompt_logprobs": 20
}
```

上例为非流式形态；流式请求的 `prompt_logprobs` 恒被省略（端点上界约束见「logprobs 与置信度」节）。

携带原生工具面时（`tools` 非空进入请求体，`tool_choice` 缺省 `auto`）：

```json
{
  "model": "glm-5.3",
  "messages": [
    {"role": "system", "content": "你是值班助手"},
    {"role": "user", "content": "现在几点了"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_current_time",
        "description": "获取当前时间",
        "parameters": {
          "type": "object",
          "properties": {"tz": {"type": "string", "description": "时区名"}},
          "required": []
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

流式请求体额外携带 `"stream": true` + `"stream_options": {"include_usage": true}`（**流式恒 content-only 纯文本**——与 `tools` / 多模态内容分块组合在 `build_chat_request_body` 直接拒绝 `VALIDATION_ERROR`，网关流式面在 entry_gate 之前同语义预检）；未设置的采样参数不进入请求体；`params.extra` 原样并入顶层（provider 特有参数透传）。

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

## logprobs 与置信度（强制字段）

新版 vLLM 仅在请求**显式声明** logprob 字段时才回传对应数据。非流式
chat/completions 请求（含工具请求）按配置携带以下三字段；**流式请求
恒省略 `prompt_logprobs`**：

| 请求字段 | 非流式缺省 | 流式 | 语义 |
|---|---|---|---|
| ``logprobs`` | ``true`` | 按 `top_logprobs` 旋钮声明 | 要求回传生成 token 的对数概率数据 |
| ``top_logprobs`` | ``20`` | ``20``（可配） | 每个生成位置回传前 N 候选分布 |
| ``prompt_logprobs`` | ``20`` | **不发送** | 提示词每个位置回传对数概率（可识别提示词各 token 的置信度） |

流式约束（端点事实）：vLLM 协议校验器对 ``stream=true`` 且
``prompt_logprobs > 0`` 直接返回 400；流式响应本身也不回传 prompt
logprobs（该字段只出现在非流式响应顶层）。因此流式路径只声明
``logprobs`` + ``top_logprobs``，``prompt_logprobs`` 由请求体构建层
强制省略——即使调用方显式传入也丢弃；``logprobs`` 是否声明只取决于
``top_logprobs``。

配置面（旧端点逃生门）：网关构造参数 ``top_logprobs`` /
``prompt_logprobs``（缺省 ``DEFAULT_TOP_LOGPROBS=20`` /
``DEFAULT_PROMPT_LOGPROBS=20``；置 ``None`` 省略对应字段），app 装配
从 ``Settings.llm`` 读取；env 面为 ``LLM_TOP_LOGPROBS`` /
``LLM_PROMPT_LOGPROBS``（int ≥1；留空 / ``none`` / ``null`` = 省略）。
两项全关时 ``logprobs`` 亦不发送。

响应侧归一（``ChatResponseModel``）：

- ``content_logprobs``：生成 token 的 token/logprob/候选分布列表；
  条目的 ``logprob`` 为 ``null`` 表示端点真缺失，映射层跳过并记一条
  debug 计数日志（不打印原文）；空 token 与 ``0.0`` 是合法回传值，
  原样保留；
- ``prompt_logprobs``：提示词条目列表；端点以 ``null`` 占位、以及空
  dict / 无整数 token_id 的元素均保留为 ``None`` 占位（位次不左移）；
  回传长度与提示词长度不一致时按实际位次归一，不抛异常；
- ``mean_token_logprob``：生成 token 平均对数概率；派生输入过滤
  NaN / ``+inf``（丢弃），保留 ``-inf``（``exp`` 下溢为 0.0，语义
  正确）；全组无可用值时该字段与 ``confidence`` 均为 ``None``；
- ``confidence``：``exp(平均对数概率)``（几何平均 token 概率的观测
  信号，不参与生成逻辑）；平均对数概率大于 0 时直接取 1.0，不调用
  ``math.exp``（防上溢）。

流式块（``ChatStreamChunkModel``）尽力捕获增量 logprobs 与首块
prompt_logprobs；端点未回传 / 旧端点缺失时对应字段为 ``None``。

请求 / 响应示例（裁剪自真机形态；``prompt_logprobs`` 首元素为 null 表示
该位次不计分——无权值 token 位）：

```json
// 非流式请求（网关自动附加——调用方无需手写）
{"model": "...", "messages": [...], "logprobs": true,
 "top_logprobs": 20, "prompt_logprobs": 20}

// 流式请求（prompt_logprobs 强制省略）
{"model": "...", "messages": [...], "stream": true,
 "stream_options": {"include_usage": true}, "logprobs": true,
 "top_logprobs": 20}

// 响应片段
{"choices": [{"index": 0, "message": {"role": "assistant", "content": "量子计算"},
  "logprobs": {"content": [{"token": "量子", "logprob": -0.000017,
    "bytes": [233, 135, 143, 229, 173, 144],
    "top_logprobs": [{"token": "量子", "logprob": -0.000017}]}]},
  "finish_reason": "stop"}],
 "prompt_logprobs": [null, {"128799": {"logprob": -20.89, "rank": 127879,
   "decoded_token": "的"}}]}
```

## embeddings 传输面（记忆检索的向量化前置）

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

## 工具调用与多模态内容分块（已落地形态）

**原生工具调用**（非流式 `chat` 面）：请求 `tools` 透传（`ToolDefinitionModel` 列表——wire 包裹为 `{"type": "function", "function": {...}}`）+ `tool_choice`（缺省 `auto`）；响应 `tool_calls` 经 client robust 解析归一为 `ToolCallModel` 列表（`arguments` JSON 字符串 → dict，非法 JSON / 非 JSON 对象 → `LLM_CALL_FAILED`）。工具循环链路顺序（单一真相）：调用方组装 `tools` → `chat_by_role` / `chat_for_entry`（非流式）→ 响应 `tool_calls` 非空 → 调用方执行工具 → 以 `assistant`（携带 `tool_calls` 声明轮）+ `tool`（`tool_call_id` 关联 + content 承载反馈）消息回放历史 → 下一轮 `chat`，直至 `tool_calls` 为空或步数上限。流式传输面恒 content-only——`stream=True` 与 `tools` 组合在请求体构建层拒绝（`VALIDATION_ERROR`）。

工具调用响应 wire 形态（`content` 与 `tool_calls` 可并存——模型边说明边调用）：

```json
{
  "id": "resp-2",
  "model": "glm-5.3",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call-1",
            "type": "function",
            "function": {"name": "get_current_time", "arguments": "{\"tz\":\"Asia/Shanghai\"}"}
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {"prompt_tokens": 61, "completion_tokens": 12, "total_tokens": 73}
}
```

**多模态内容分块**（非流式面）：`ChatMessageModel.content` 支持 `list[ContentPartModel]`（`TextContentPart` / `ImageContentPart` 判别联合——user 轮携带 `[image, text]` 视觉理解形态；图片经 data URL 内联，不落盘不外发）。纯字符串形态在全部既有调用面原样有效（wire 序列化保持二键 `{"role", "content"}`）；仅分块列表形态序列化为 parts 数组。流式传输面拒绝分块形态（同上 `VALIDATION_ERROR`——网关 `messages_have_content_parts` 预检 + 请求体构建层守卫双层一致）。

```json
{
  "model": "qwen2.5-vl-7b",
  "messages": [
    {"role": "system", "content": "你是图像理解助手"},
    {
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,<载荷>"}},
        {"type": "text", "text": "图里有什么"}
      ]
    }
  ]
}
```

## Model Classes 字段表

`ChatMessageModel`：`role`（system / user / assistant / **tool**——工具结果回放轮，`tool_call_id` 关联 assistant 声明轮）/ `content`（**纯文本或多模态内容分块列表**——`str | list[ContentPartModel]`，多模态分块已落地；assistant 工具调用轮可为空串）/ `tool_calls`（assistant 轮的工具调用声明）/ `tool_call_id`（tool 轮关联 ID）。

`ChatRequestParams`：`temperature`（0.0-2.0）/ `max_tokens`（≥1）/ `top_p`（0-1.0）/ `extra`（provider 透传 dict）——未设置字段不进 wire 请求体。

`ChatResponseModel`：`content` / **`tool_calls`**（`list[ToolCallModel]`——空列表 = 纯文本回复；content 与 tool_calls 可并存，调用方按 tool_calls 非空优先走工具循环）/ `finish_reason` / `model`（端点回显，缺省回落请求名）/ `usage: TokenUsageModel | None` / `latency_ms`（epoch 毫秒差）/ `content_logprobs: list[LogprobTokenModel] | None`（生成 token logprobs；真缺失条目已跳过）/ `prompt_logprobs: list[PromptLogprobEntryModel | None] | None`（提示词条目，None 占位保留位次）/ `mean_token_logprob: float | None` / `confidence: float | None`（`exp(平均对数概率)`，平均值为正时直接 1.0）。

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

## 日预算闸与慢调用观测

**日预算（`LLM_DAILY_BUDGET_GLOBAL` / `LLM_DAILY_BUDGET_PERSONA`，默认 0 = 不限）**：
调用前按 persona 档先行、global 档其后判定（位置：chat / stream 在角色解析之后、
条目门控 / 传输之前；embed 在角色解析之前）；
超限抛既有 `RATE_LIMITED`（429），`details` 含 `scope` / `used` / `limit`（persona
档另含 `persona_id`），且**不落用量行**（被预算拒绝的调用不计入预算，避免自我
强化耗尽）。计数口径为当日 UTC 日桶内的 **LLM 用途** usage 行（chat / duty /
dream / look_at / embed / tool；含失败行）——ASR / TTS / voiceprint / search 各自
独立预算，不占用本闸（见 [usage.md](usage.md#分面日预算asr--tts--voiceprint)）；
`persona_id` 缺失（系统调用）只受 global 档约束。

**有效上限 10,000**（两档同）：计数走 usage 游标分页，页帽 50 × 每页 200 = 10,000 行
封顶——配置层以 `le=10_000` 拒绝更大值，避免「预算永远不会触发」的静默失效。

**软竞态（锁定决策）**：计数经 usage 表查询（DB 口径），跨 worker 各自查询同一张
表，结果**近似**全局一致——并发请求可能同时读到同一剩余额度并同时放行（最多
超出并发数条）。预算不是计费级精确配额；逐条强一致需要 Redis 式原子计数，本工
程 NO Redis 决策下不引入。计数查询异常按普通业务异常上抛（fail loud，不静默放行）。

**分面边界（R4-2 起）**：LLM 计数经 `LLM_BUDGET_PURPOSES` 收窄到模型调用用途；
ASR / TTS / voiceprint 由 voice 域独立闸按 purpose 计数，二者双向隔离（LLM 行不计入
语音预算，语音行也不计入 LLM 预算）。计数分页页帽与 10,000 有效上限口径一致。

**慢调用观测**：成功调用时延 ≥ `SLOW_CALL_WARN_MS`（代码常量 10s）落一条 WARN，
字段为 `entry` / `model` / `purpose` / `latency_ms`（不打印消息内容）。仅观测，
不重试、不中断、不计失败。

## 用量发射语义

- 成功 / 失败路径**均落账**（失败 tokens 为 0，调用计数 + 时延仍有观测价值）；流式以末块 usage 为准（无则零用量）；
- persona_id / request_id 解析序：显式参数 → `core.context` 上下文变量（请求中间件已绑定则自动携带）→ None；
- 记录器异常**仅告警不阻断主调用**（用量是观测面不是业务面）；未接线（recorder=None）零记录。

## 注册表管理 API（`app/api/models.py` + `harness/config/registry_admin.py`）

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
2. **显式 `role_bindings`** 指向的条目：先在 config.yaml 手工移除该绑定（本管理面不提供显式绑定编辑端点——显式绑定编辑属后续评审面）。

再执行 `DELETE`。

### 测试与门禁（注册表管理面）

- 测试位置：`src/tests/unit/test_models_api.py`（ASGI + 处理器直调契约面）：脱敏 / 解析状态 / 创建→落盘→热重建 round-trip / 部分更新（含 concurrency 显式 null 重置）/ 删除守卫与两步解绑流 / 唯一名 409 / 非法 base_url·未知字段·未知角色 400 / 原子写失败不留临时文件 / 重建失败内存回滚（注入仅首次失败的解析器）/ 备份生成与轮换（11+ 次持久化后保留 10 份）/ 不可解析既有文件拒绝覆写 / 写路径追踪访问器；
- 密钥防泄漏断言：测试注入真实密钥环境变量值，断言该值永不出现于任何响应面；
- 覆盖率（`pixi run coverage-gates` 实测）：harness/config 组 97.3%（含 registry_admin.py / registry_admin_dtos.py 新文件）；app/api 组 96.5%。

## 测试与门禁

- 测试位置：`src/tests/unit/test_llm_models.py` / `test_llm_client.py` / `test_llm_gateway.py` / `test_llm_embeddings.py` / `test_llm_logprobs.py` / `test_llm_route_policy.py` / `test_llm_intimate_channel.py`（7 份；预算与慢调用用例在 gateway 文件内，条件路由与内置策略等价性在 route_policy / intimate_channel 文件内）；
- 全部 `async def test_`；HTTP 一律 `httpx.MockTransport`（零真实出站）；网关经注入解析器 + 隔离 Settings 的真实 PoolManager（共享 client 替换为 MockTransport）；重试等待零 sleep（Retry-After 0 / 等待策略直测）；熔断 open 经预开熔断器确定性触发；embeddings 面覆盖请求形态 / index 归位 / 数量与序号契约 / purpose=embed 用量发射 / persona 解析序 / 角色未绑定；
- 默认接线全链测试：gateway → `wire_default_usage_recorder` → usage 服务 → 共享 SQLite 引擎（`test_llm_gateway.py::test_default_usage_recorder_full_chain`）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/llm 组 **95%**（client 95% / gateway 100% / llm_models 100%）；
- 运行：`cd backend && pixi run tests-unit`。
