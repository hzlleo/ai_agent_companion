# usage — token 用量域（含管理 API）

`backend/src/packages/harness/usage/` 承载 token 用量记账：LLM / 辅助端点每次调用一行记录（LLM 成功 / 失败均落账；语音与声纹仅**成功**调用落账，见「分面日预算」），管理面多维聚合（按条目 / 用途 / 时间桶）。表名 `ff_usage_records`（本工程 `ff_` 前缀约定）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（用途 / 状态 / 聚合维度 / 时间桶宽） | `UsagePurpose` / `UsageStatus` / `UsageGroupBy` / `UsageTimeBucket` |
| `orm.py` | 用量记录 ORM（`ff_usage_records` 表） | `UsageRecordRow` |
| `dtos.py` | 创建 / 记录 / 查询 / 聚合 DTO（Unit 文件） | `UsageCreateDTO` / `UsageRecordDTO` / `UsageQueryDTO` / `UsageAggregateQueryDTO` / `UsageAggregateDTO` |
| `service.py` | 创建（单条 / 批量）/ 游标分页查询 / 多维聚合 | `UsageService` |

依赖方向：usage → core（dtos 分页 / errors）+ database（session_scope / Base Mixins）；**不依赖 llm 域**（llm 网关经注入式记录器对接，接线点 `llm.gateway.wire_default_usage_recorder`）。

## 关键设计决策：persona_id 为普通可空索引列（非 PersonaMixin）

本表是管理面**跨人格聚合真源**：用量统计按条目 / 用途 / 时间聚合（全库口径），不按人格切分；PersonaMixin 的请求上下文自动过滤（无人格上下文即 fail loud）与跨人格聚合语义直接冲突。故 `persona_id` 落为普通可空索引列（系统调用——值班巡检 / 梦境整理 / 嵌入回填等无人格上下文），由查询方经 `UsageQueryDTO.persona_id` 显式过滤。

## 链路顺序（gateway → usage）

```
llm 网关调用（chat_by_role / chat_for_entry / stream_by_role）
    ↓ 成功 / 失败路径均构造 LlmUsageEvent（ts / tokens / latency / status / error_code）
    ↓ 注入式 UsageRecorder（默认接线 wire_default_usage_recorder——app 装配）
    ↓ UsageCreateDTO（purpose/status 枚举化 + total 归一）
    ↓ UsageService.create → UsageRecordRow → session_scope 提交 → ff_usage_records
    ↓ 管理面读取：UsageService.query（游标分页）/ UsageService.aggregate（多维聚合）
```

## 表结构（ff_usage_records）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | VARCHAR(64) | 可空；索引 | 人格 ID（系统调用为 NULL） |
| `ts_ms` | BIGINT | 非空；索引 | 调用发起时间（epoch 毫秒） |
| `model_entry` | VARCHAR(128) | 非空；索引 | 模型条目名（聚合维度） |
| `provider` | VARCHAR(64) | 非空 | provider 类型 |
| `purpose` | VARCHAR(16) | 非空；索引 | 调用用途（聚合维度） |
| `input_tokens` | INT | 非空 | 输入 token 数 |
| `output_tokens` | INT | 非空 | 输出 token 数 |
| `total_tokens` | INT | 非空 | 总 token 数（DTO 边界按 input+output 归一） |
| `latency_ms` | INT | 非空 | 端到端时延（毫秒，含失败耗时） |
| `status` | VARCHAR(16) | 非空 | success / error |
| `request_id` | VARCHAR(64) | 可空 | 请求追踪 ID |
| `error_code` | VARCHAR(64) | 可空 | 失败错误码（成功为 NULL） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

迁移：`backend/alembic/versions/000000000002_add_usage_records.py`（Revision 000000000002，Revises 000000000001）。

## 值域

- `UsagePurpose`：`chat` / `duty` / `dream` / `look_at` / `embed`（与 config.ModelRole 一一对应）+ `tts` / `asr` / `voiceprint`（语音与声纹面——tts / asr 经 voice 域 `wire_default_voice_usage_recorder` 接线，voiceprint 经同一接线记录声纹向量提取）+ `tool`（工具循环调用面的 purpose 值域成员，duty 工具循环当前按 `duty` 用途归账）+ `search`（联网搜索面——Bocha / Zhipu provider 链路按**调用行计数**预算，token 恒 0/0/0，成本经 `model_entry=provider 条目名` + `provider=kind` 维度观测；单次调用成本估算以 provider 计价页为准——Bocha web-search 与 Zhipu search_std 均为按次计费、无 token 计量，代码不硬编码价格）；
- `UsageStatus`：`success` / `error`；
- `UsageGroupBy`：`model_entry` / `purpose` / `time_bucket`；
- `UsageTimeBucket`：`minute`（60_000ms）/ `hour`（3_600_000ms）/ `day`（86_400_000ms）——桶起点 = `floor(ts_ms / 桶宽) * 桶宽`（跨方言经 `floor` 归一整数除法语义）；**时间桶起点按 UTC 对齐**（epoch 毫秒整除口径，`day` 桶边界为 UTC 零点——与本地日界无关；如需按本地日界切桶再议）。

## 分面日预算（ASR / TTS / voiceprint）

**配置键**（三组独立分面，0 = 不限；有效上限 10,000——计数页帽 50×200，
配置层 `le=10_000` 拒绝更大值）：

| 分面 | 全局档 env | 人格档 env | 配置类 |
|---|---|---|---|
| TTS 语音合成 | `TTS_DAILY_BUDGET_GLOBAL` | `TTS_DAILY_BUDGET_PERSONA` | `TtsSettings`（`VOICE_TTS_` 分节） |
| ASR 语音识别 | `ASR_DAILY_BUDGET_GLOBAL` | `ASR_DAILY_BUDGET_PERSONA` | `AsrSettings`（`VOICE_ASR_` 分节） |
| 声纹向量提取 | `VOICEPRINT_DAILY_BUDGET_GLOBAL` | `VOICEPRINT_DAILY_BUDGET_PERSONA` | `VoiceprintSettings`（`VOICEPRINT_` 分节） |

**计量单位与口径**：每次真实出站调用按 **1 次**计量——TTS 每句 / 每次合成
请求 1 行、ASR 每次转写（含实时语音的切段 / 中途转写）1 行、声纹每个样本
向量提取 / 每次校验 1 行；persona 上下文存在时写 `persona_id`，系统调用为
NULL。**仅成功后写 usage 行**：出站失败不记、被预算拒绝的调用不记（不消耗
预算、不自我强化耗尽）；token 三列恒 0（语音与向量提取无 token 语义），
`model_entry` / `provider` / `latency_ms` 保留观测价值。既有 LLM 记账语义
不变（成功 / 失败均落账）。

**闸门位置与错误**：各出站客户端调用之前判定（persona 档先行、global 档
其后；实时语音的逐段 ASR / 逐句 TTS 与声纹注册 / 校验逐次调用同样受控）。
超限抛既有 `RATE_LIMITED`（429），`details` 形如：

```json
{
  "scope": "persona",
  "used": 20,
  "limit": 20,
  "purpose": "asr",
  "persona_id": "persona-feiyu"
}
```

被拒调用零出站、零 usage 行；`details` 不含音频 / 文本原文。两档均 0 时
**不发起计数查询**。`persona_id` 缺失（系统调用）只受 global 档约束。

**计数查询与软竞态**：计数经 usage 表查询（UTC 日桶、按 purpose + `status=success` 强制过滤
——LLM / search / 其它分面行与历史失败行不计入；`UsageQueryDTO.purpose` 为分面过滤轴，
`purposes` 集合轴供 LLM 总量闸收窄用途）。跨 worker 各自查询同一张表，结果
**近似**全局一致：并发请求可能同时读到同一剩余额度并同时放行（最多超出
并发数条）；逐条强一致需要 Redis 式原子计数，本工程 NO Redis 决策下不引入。
计数查询异常按普通业务异常上抛（fail loud，不静默放行）。

**实时语音会话中途超限**：该句 / 该段受控失败并下行可恢复 `error`
（`code=RATE_LIMITED`），会话状态回到聆听继续服务——不关闭连接、不中断
后续语段（见 [realtime-voice.md](realtime-voice.md)）。**声纹批量注册中途
超限**：整体回滚本批已写样本文件与样本行（沿用校验失败回滚语义），已成功
完成的出站调用各留一行用量（真实成本已发生）。

**示例（运维核对）**：`TTS_DAILY_BUDGET_GLOBAL=500` 表示当日全部人格合计
最多 500 次成功 TTS 合成；`TTS_DAILY_BUDGET_PERSONA=100` 表示单人格最多
100 次。`ASR_*` / `VOICEPRINT_*` 同构、互不占用。管理面可按
`purpose=tts|asr|voiceprint` 过滤 records / aggregates 核对当日消耗。

## UsageService API

```python
service = UsageService()                                   # 无状态；Session 经 session_scope 每调用获取
record = await service.create(dto)                         # 单条（网关默认记录器消费面）
records = await service.create_batch(dtos)                 # 批量（单事务批量 INSERT；空序列 → []）
page = await service.query(UsageQueryDTO(...))             # 游标分页（id 降序——新记录在前）
rows = await service.aggregate(UsageAggregateQueryDTO(...))  # 多维聚合（总量降序）
```

## DTO JSON 示例与字段表

`UsageCreateDTO`（写入面）：

```json
{
  "ts_ms": 1762934400000,
  "persona_id": "persona-feiyu",
  "model_entry": "main-chat",
  "provider": "openai_compatible",
  "purpose": "chat",
  "input_tokens": 52,
  "output_tokens": 18,
  "total_tokens": 70,
  "latency_ms": 843,
  "status": "success",
  "request_id": "req-7f3a",
  "error_code": null
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts_ms` | int | 调用发起时间（epoch 毫秒） |
| `persona_id` | str \| None | 人格 ID（None = 系统调用） |
| `model_entry` | str | 条目名（非空） |
| `provider` | str | 默认 `openai_compatible` |
| `purpose` | UsagePurpose | 用途枚举 |
| `input_tokens` / `output_tokens` | int ≥ 0 | token 分项 |
| `total_tokens` | int ≥ 0 | 未提供（0）时按 input+output 归一 |
| `latency_ms` | int ≥ 0 | 时延毫秒 |
| `status` | UsageStatus | success / error |
| `request_id` / `error_code` | str \| None | 追踪 ID / 失败错误码 |

`UsageQueryDTO`（记录查询——过滤面 + 游标分页）：`start_ms`（含）/ `end_ms`（不含，**左闭右开**）/ `persona_id` / `model_entry` / `purpose` / `status` 过滤 + `cursor`（不透明游标，None = 首页）/ `limit`（1-200，默认 20）。返回 `PageResponse[UsageRecordDTO]`（`next_cursor` 非空表示还有更早记录；total 恒 -1——计数不在分页路径上执行）。

`UsageAggregateQueryDTO`（聚合查询）：过滤面同上 + `group_by`（model_entry / purpose / time_bucket）+ `time_bucket`（time_bucket 维度必填，缺失 `VALIDATION_ERROR` fail loud）。

`UsageAggregateDTO`（聚合结果行）：

```json
{
  "group": "main-chat",
  "bucket_ms": null,
  "input_tokens": 1100,
  "output_tokens": 540,
  "total_tokens": 1640,
  "call_count": 23,
  "avg_latency_ms": 812,
  "error_count": 2
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `group` | str | 分组键（条目名 / 用途值 / 桶起点毫秒字符串） |
| `bucket_ms` | int \| None | 时间桶起点（仅 time_bucket 维度非 None；与 group 一致性校验） |
| `input_tokens` / `output_tokens` / `total_tokens` | int | 求和 |
| `call_count` | int | 调用次数 |
| `avg_latency_ms` | int | 平均时延（四舍五入取整） |
| `error_count` | int | 失败次数（status=error） |

排序：总 token 降序为主序（成本视角），分组键升序为次序（确定性输出）。聚合形态已按管理 API 预塑形（admin 端点直接包装 `aggregate` 结果）。

## 管理 API（`app/api/usage.py`）

跨人格观测语义（文档化决策）：用量是管理面 observability 数据——三个端点**均无人格上下文要求**（不解析目标人格、不绑定 persona contextvar、无 PersonaMixin 自动过滤）；人格过滤经 `persona_id` 查询参数显式表达（缺省 = 全人格）。全部响应经 `ResultDTO` 封装；`UsageService` 自持 `session_scope`（会话边界在服务层），路由无 DB 依赖注入。全部时间参数为 epoch 毫秒，范围**左闭右开**（`start_ms <= ts_ms < end_ms`）。

### GET /api/v1/usage/records — 记录游标分页

查询参数（全可选）：`persona_id` / `model_entry` / `purpose`（chat/duty/dream/look_at/embed/tts/asr/tool）/ `status`（success/error）/ `start_ms`（含）/ `end_ms`（不含）/ `cursor`（不透明游标，首页缺省）/ `limit`（1-200，默认 20）。

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": {
    "items": [
      {
        "id": 42,
        "persona_id": "persona-feiyu",
        "ts_ms": 1762934400000,
        "model_entry": "main-chat",
        "provider": "openai_compatible",
        "purpose": "chat",
        "input_tokens": 52,
        "output_tokens": 18,
        "total_tokens": 70,
        "latency_ms": 843,
        "status": "success",
        "request_id": "req-7f3a",
        "error_code": null,
        "created_at": 1762934401000
      }
    ],
    "total": -1,
    "page": 1,
    "page_size": 20,
    "next_cursor": "eyJpZCI6NDF9",
    "has_more": true
  }
}
```

id 降序（新记录在前）；`next_cursor` 非空表示还有更早记录（原样回传接续）；`total` 恒 -1（计数不在分页路径上执行）。

### GET /api/v1/usage/aggregates — 多维聚合

查询参数：`group_by`（**必填**：`model_entry` / `purpose` / `time_bucket`——暴露服务层既有维度，不新增）+ `time_bucket`（`group_by=time_bucket` 时**必填**：minute / hour / day；缺失 → 422 `VALIDATION_ERROR`）+ 与 records 相同的过滤面（不含分页参数）。

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": [
    {
      "group": "main-chat",
      "bucket_ms": null,
      "input_tokens": 1100,
      "output_tokens": 540,
      "total_tokens": 1640,
      "call_count": 23,
      "avg_latency_ms": 812,
      "error_count": 2
    },
    {
      "group": "960000",
      "bucket_ms": 960000,
      "input_tokens": 30,
      "output_tokens": 20,
      "total_tokens": 50,
      "call_count": 2,
      "avg_latency_ms": 150,
      "error_count": 0
    }
  ]
}
```

时间维度行的 `group` 为桶起点 epoch 毫秒字符串、`bucket_ms` 为其数值面；排序：总 token 降序 + 分组键升序。

### GET /api/v1/usage/summary — 总览（单 payload）

查询参数：与 records 相同的过滤面（不含分页参数）。经 `group_by=purpose` 聚合行派生：计数 / token 求和 / 失败数精确；整体平均时延按 per-purpose 均值（已取整）的调用次数加权（与逐记录加权均值的偏差 < 1ms）。

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": {
    "call_count": 4,
    "input_tokens": 16,
    "output_tokens": 27,
    "total_tokens": 43,
    "avg_latency_ms": 165,
    "error_count": 1,
    "purposes": [
      {
        "group": "chat",
        "bucket_ms": null,
        "input_tokens": 15,
        "output_tokens": 25,
        "total_tokens": 40,
        "call_count": 2,
        "avg_latency_ms": 150,
        "error_count": 0
      },
      {
        "group": "dream",
        "bucket_ms": null,
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
        "call_count": 1,
        "avg_latency_ms": 300,
        "error_count": 1
      }
    ]
  }
}
```

空过滤结果返回全零 + 空 `purposes`（除零守卫）。请求校验失败（未知枚举 / limit 越界 / 负时间戳）→ 400 `VALIDATION_ERROR`；`time_bucket` 维度缺桶宽 → 422 `VALIDATION_ERROR`（服务层业务校验）。

## 测试与门禁

- 测试位置：`src/tests/unit/test_usage_service.py`（服务面）+ `src/tests/unit/test_usage_api.py`（管理 API：ASGI + 处理器直调契约面——分页接续 / 全过滤面 / 三维聚合正确性 / 总览派生 / 错误映射）；
- 全部 `async def test_`；引擎经共享会话级 SQLite fixture（StaticPool 单连接 + 每测试自动清表）；服务经 engine holder 槽位**外部安装**（`owner_token=None`——生命周期归共享 fixture，见 database.engine 治理约定）；
- 聚合正确性锁定：分组求和 / 计数 / 均时延 / 失败计数 / 总量降序 / minute-hour-day 三档桶宽边界对齐（含跨记录归桶与 25 小时独立桶）；
- 覆盖率（`pixi run coverage-gates` 实测）：harness/usage 组 **99.5%**（service 100% / orm 100% / models 100% / dtos 98%）；app/api 组 96.5%；
- 运行：`cd backend && pixi run tests-unit`。
