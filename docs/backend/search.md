# search — 联网搜索域（含管理 API）

owner 拍板为两 provider 的 failover 形态：web search 落地为一等能力——
Bocha 主 / Zhipu `search_std` 兜底双 provider（决策记录见 [LEDGER](../LEDGER.md)），经 config.yaml `search` 段注册表管理
（ConfigSectionAdmin 基座的第二个消费者），`web_search` 工具同时供 duty 工具循环与
chat 工具循环消费，per-day / per-persona 预算门禁经 usage 域 `purpose=search` 行计数。

## 模块一览

| 模块 | 职责 |
|---|---|
| `packages/harness/config/models.py`（SearchConfig 部分） | `SearchProviderEntry` / `WebSearchSettings` / `SearchConfig`——config.yaml `search` 段模型 + `SEARCH_*` env 面 |
| `packages/harness/config/search_registry.py` | `SearchRegistryAdminService`（ConfigSectionAdmin 搜索子类：provider CRUD / 脱敏 / 唯一 provider 守卫）+ 运行时真源 `runtime_search_config`（加载 / 应用 / 复位三件套） |
| `packages/harness/search/search_models.py` | Unit 文件：`SearchQueryModel` / `SearchResultModel` / `SearchResponseModel` / 用量事件与消费面协议 |
| `packages/harness/search/bocha_client.py` | Bocha Web Search 客户端（共享 httpx 池 + Bearer + 用量发射钩子） |
| `packages/harness/search/zhipu_client.py` | Zhipu Web Search 客户端（同上；引擎码 = 条目 model 字段） |
| `packages/harness/search/search_service.py` | `SearchService`——provider failover 链 + 降级冷却 + 预算门禁 + 用量落账 |
| `packages/harness/search/tool.py` | `web_search` LLM 工具（duty / chat 两循环共用注册入口） |
| `app/api/search.py` | 管理 API：provider CRUD + usage-summary |

## 注册表配置（config.yaml `search` 段）

| 字段 | 位置 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `providers` | `search`（config.yaml） | `list[SearchProviderEntry]` | `[]` | provider 注册表；**列表序 = failover 尝试序** |
| `providers[].name` | 条目 | `str`（1-64） | 必填 | 唯一标识（usage 行 `model_entry` 列） |
| `providers[].kind` | 条目 | `bocha` \| `zhipu` | 必填 | provider 类型 |
| `providers[].base_url` | 条目 | `str` | 按 kind | API 基础 URL（bocha `https://api.bochaai.com` / zhipu `https://open.bigmodel.cn`） |
| `providers[].api_key_env` | 条目 | `str` | 按 kind | API Key **环境变量名**（默认 `SEARCH_BOCHA_API_KEY` / `SEARCH_ZHIPU_API_KEY`——密钥本值永不落 yaml） |
| `providers[].model` | 条目 | `str` | 按 kind | provider 专属标识（bocha 观测标识 `web-search`——无引擎选择参数；zhipu 引擎码 `search_std`，可配 `search_pro` 等） |
| `providers[].max_results` | 条目 | `int`（1-20） | `8` | 单次返回条数上限 |
| `providers[].timeout_seconds` | 条目 | `float`（>0） | `15.0` | 单次请求超时 |
| `web_search.enabled` | `search.web_search` 子块 | `bool` | `False` | 能力总闸（duty / chat 工具面 + 服务面） |
| `web_search.tool_enabled` | 同上 | `bool` | `False` | 工具面曝光闸（**工具仅在两级开关同真时注册**） |
| `web_search.daily_budget_per_persona` | 同上 | `int`（≥1） | `50` | 单人格每日搜索调用数预算 |
| `web_search.daily_budget_global` | 同上 | `int`（≥1） | `200` | 全局每日搜索调用数预算 |
| `web_search.cooldown_seconds` | 同上 | `int`（≥1） | `60` | provider 失败降级冷却秒数 |

`web_search` 子块类型 `WebSearchSettings` 同时是 **SEARCH_* env 面**
（`SEARCH_ENABLED` / `SEARCH_TOOL_ENABLED` / `SEARCH_DAILY_BUDGET_PER_PERSONA` /
`SEARCH_DAILY_BUDGET_GLOBAL` / `SEARCH_COOLDOWN_SECONDS`，`Settings.search` 聚合嵌入 +
`build_isolated_settings(search=...)` 注入）。分层合并遵循全工程优先级链
（代码默认 < .env < env < config.yaml）：**yaml 子块键覆盖 env，未提供的键回落
env / 代码默认**。provider CRUD 持久化 `search` 段时会把当前生效 `web_search` 值
（env 已合并）整体落盘——yaml 是最高层，写入即固化（.bak 备份可回溯）。

密钥间接键（`SEARCH_BOCHA_API_KEY` / `SEARCH_ZHIPU_API_KEY`）按敏感键守卫在
`.env.example` 留空；条目声明的变量为空时调用链 fail loud
（`LLM_API_KEY_MISSING`——配置错误不降级不计数）。

config.yaml 示例：

```yaml
search:
  providers:
    - name: "bocha-main"
      kind: "bocha"
      api_key_env: "SEARCH_BOCHA_API_KEY"   # .env 侧设置本值
      max_results: 8
      timeout_seconds: 15
    - name: "zhipu-fallback"
      kind: "zhipu"
      model: "search_std"                    # 兜底引擎档
      api_key_env: "SEARCH_ZHIPU_API_KEY"
      max_results: 5
  web_search:
    enabled: true
    tool_enabled: true
    daily_budget_per_persona: 50
    daily_budget_global: 200
    cooldown_seconds: 60
```

> loader 只解析 `llm` 段——`search` 段由 `search_registry.load_search_config`
> 自行解析（app lifespan 在 `load_all_config` 之后调用）；`search` 在 loader 的
> 已知顶层段白名单内，不产生「未知顶层键」告警（键值原样保留供本域消费）。

## provider wire 契约（实现口径）

| 项 | Bocha | Zhipu |
|---|---|---|
| 端点 | `POST {base_url}/v1/web-search` | `POST {base_url}/api/paas/v4/web_search` |
| 鉴权 | `Authorization: Bearer <api_key_env 解析值>` | 同左 |
| 请求体 | `{"query", "freshness": "noLimit", "summary": false, "count"}` | `{"search_query", "search_engine", "search_intent": false, "count", "request_id"}` |
| 响应 | `{code: 200, data: {webPages: {value: [{name, url, snippet, dateLastCrawled}]}}}` | `{search_result: [{title, link, content, media, icon, refer, publish_date}]}` |
| 归一映射 | `name→title` / `url→url` / `snippet→snippet`（缺失回退 `summary` 键；兼容未包裹响应形态） | `title→title` / `link→url` / `content→snippet` |
| 空结果 | `webPages.value: []` 合法 | `search_result` 键缺失 / 空列表合法 |
| 错误 | 业务码非 200 / HTTP 非 200 / 传输 / 非 JSON / 结构破坏 → `LLM_CALL_FAILED`（503） | 同左 |
| 长度限制 | query 1-256（本工程面） | `search_query` 官方 schema ≤70 字符——超长截断到 70（兜底降级语义） |

**来源依据与不确定性披露**：Zhipu 契约按智谱开放平台官方 OpenAPI 规格
（docs.bigmodel.cn `post /paas/v4/web_search` 组件 schema——服务器
`https://open.bigmodel.cn/api/`，必填 `search_query` / `search_engine` /
`search_intent`，响应 `search_result` 数组）实现；任务书建议的
`/api/paas/v4/tools` 为 agent 工具循环端点（响应嵌于 `choices[].message`），
与任务书描述的 `search_result` 响应形状分属两个端点——以「官方规格 + 响应形状」
双交叉证据取 `web_search` 专用端点。Bocha 契约按官方开放平台首页 curl 示例
（open.bochaai.com——字段名 `summary`（bool）而非 `summarize`）+ 任务书响应
形状交叉锁定；官方文档站为飞书 wiki（纯 fetch 不可渲染），`dateLastCrawled`
键按任务书形状实现但不消费，未包裹响应形态（首页示例的 `datePublished` /
`siteName` 形态）经 `data` 回退 + `snippet→summary` 回退兼容。**测试经
MockTransport 锁定上述契约为本工程真相**——部署实测如需修正属契约变更
（同步客户端 docstring + 测试 + 本表）。

## 链路顺序

### SearchService.search（单一入口）

```
search(query, persona_id=?, max_results=?)
    ↓ ① 入参校验（SearchQueryModel——query 1-256 / 条数 1-20）
    ↓ ② 运行时配置读取（runtime_search_config——CRUD 热重建后自动生效）
    ↓ ③ 预算门禁（先 persona 档后 global 档；超限 → SEARCH_BUDGET_EXCEEDED）
    ↓ ④ provider failover 链（列表序）：
    ↓     降级冷却中 → 跳过；密钥缺失 → LLM_API_KEY_MISSING（不降级）；
    ↓     客户端按 kind 瞬时构建（per-call——配置热重建天然生效）；
    ↓     成功 → 清除降级标记 + 用量行（success）+ 返回；
    ↓     失败 → 标记降级（now + cooldown_seconds）+ 用量行（error）+ 落下一级
    ↓ ⑤ 全部降级 / 失败 → LLM_REQUEST_FAILED（details: skipped / attempted / last_error）
```

### 预算语义（按调用行计数）

- **今日边界**：epoch 毫秒整除 `86_400_000` 对齐的 **UTC 日桶**（与 usage 域
  `time_bucket=day` 同口径）；
- **计数含失败行**（`status=error`）——provider 故障重试同样消耗预算，防故障
  风暴绕过；**预算拒绝本身不落行**（调用未发生，零 provider 成本）；
- **判定**：`used < limit` 放行——恰好第 `limit` 次调用通过，第 `limit+1` 次拒绝；
- **人格作用域**：`persona_id` 显式参数优先，缺省读 `persona_id_var` 上下文
  （duty 轮 / 通道路由已绑定则自动携带）；无人格上下文 = 系统调用——persona
  档跳过，global 档仍生效；
- **计数来源**：usage 域 `purpose=search` 行（`UsageService.query` 游标分页遍历 +
  提前收束——预算默认 ≤200 时单页即出）。

预算超限响应（`SEARCH_BUDGET_EXCEEDED`——字符串字面量错误码，
`get_http_status_for_code` 回落 400）：

```json
{
  "success": false,
  "code": "SEARCH_BUDGET_EXCEEDED",
  "message": "单人格每日搜索预算已用尽: used=50, limit=50, persona=7",
  "data": {"used": 50, "limit": 50, "scope": "persona", "persona_id": "7"}
}
```

global 档 `data` 恰为 `{"used": ..., "limit": ..., "scope": "global"}`。

### 用量行（purpose=search）

| 列 | 值 |
|---|---|
| `purpose` | `search`（枚举成员，搜索调用专用） |
| `model_entry` | provider 条目名（如 `bocha-main`） |
| `provider` | kind（`bocha` / `zhipu`） |
| `input/output/total_tokens` | 恒 0/0/0（按次计费——预算按计数，表结构不变） |
| `latency_ms` | 客户端侧真实时延（含失败耗时） |
| `status` / `error_code` | success / error（error 行携带 `LLM_CALL_FAILED`） |

每次 provider **尝试**恰一行（成功 / 失败均落——主失败 + 兜底成功 = 2 行）；
成本估算口径见 usage.md 值域一节（代码不硬编码价格）。

### 工具面 + chat 工具循环（装配链）

```
app lifespan startup_app_state
    ↓ load_all_config → _startup_search(app, settings, config_dir)
    ↓   load_search_config（search 段——先于通道：通道 ChatService 工厂消费产物）
    ↓   enabled=false → no-op；enabled + 零 provider → fail fast（CONFIG_INVALID 中文错误）
    ↓   SearchService（共享 HTTP 池 + UsageService）→ app.state.search_service
    ↓   tool_enabled=true → chat 工具注册表（仅 web_search）+ 规格列表
    ↓     → app.state.search_tool_registry / search_tool_specs
    ↓ _startup_channels：ChatService 工厂按槽位绑定工具面
    ↓   （tools=规格列表 + tool_executor=注册表——成对注入；缺省零工具形态）
    ↓ _startup_scheduler（duty enabled 时）：duty 注册表在内置 + 沙盒 +
    ↓   look_at 工具后追加 web_search（链路顺序：web_search 最后）
```

`web_search` 工具规格：参数 `{query: str 1-256}`；执行器委托 SearchService
（预算 / failover 在服务内闭环——工具面保持薄）；结果为紧凑中文文本块
（`【N】标题 / 链接：URL / 摘要：snippet`，空结果渲染「（无搜索结果）」）+
`count` / `provider` 机器可读字段；服务异常（预算 / 全降级）经 registry 隔离
归一 `ok=False`（恒不上抛）。

chat 工具循环（`ChatService` 构造注入 `tools` + `tool_executor` 后 `reply` 走
有界循环；机械镜像 duty 循环）：

```
reply(state, user_text)
    ↓ 追加用户回合 + 持久化（既有路径）
    ↓ compose_messages（组装零变更——人格声线不受工具面污染）
    ↓ 工具循环（≤ CHAT_TOOL_STEP_CAP = 5 步）：
    ↓   gateway.chat_by_role(chat, messages, tools=specs, persona_id, purpose="chat")
    ↓   tool_calls 非空 → assistant 轮回放 + 逐调用 tool_executor.execute_call
    ↓     （tool_call_id 配对）+ tool 轮反馈；tool_calls 空 → content 即最终回复
    ↓ 步数上限触发 → 收尾指令（system）+ 无工具面最终调用（final-report forcing）
    ↓ 空回复 → LLM_CALL_FAILED；否则追加 assistant 回合 + 持久化
```

**持久化不变**：会话回合只落用户 + 最终 assistant 文本（工具步为循环内部
消息——不进 `state.turns` / 不落 `ff_chat_messages`）；流式 `stream_reply`
恒 content-only（网关面 tools+stream 已拒绝）。CLI 零变更（`tools=None` 缺省）。

## 管理 API（`app/api/search.py`）

受保护路由集（owner-token 鉴权——与 models 路由同挂载）。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/search/providers` | GET | 注册表状态（脱敏条目 + 链位次 + 运营子块） |
| `/api/v1/search/providers` | POST | 创建条目（原子持久化 + 热重建；唯一名 409） |
| `/api/v1/search/providers/{name}` | PUT | 部分更新（name 不可变——请求体携带 `name` 即 400） |
| `/api/v1/search/providers/{name}` | DELETE | 删除（唯一 provider 守卫：能力启用时 409 + 两步指引） |
| `/api/v1/search/usage-summary` | GET | 今日调用数 vs 预算（UTC 日桶口径） |

创建示例：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search/providers \
  -H "Authorization: Bearer $ADMIN_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "bocha-main", "kind": "bocha", "max_results": 8}'
```

响应（写后状态，脱敏——`api_key_env` 只回变量名，密钥值永不出现）：

```json
{
  "success": true,
  "code": "200",
  "message": "搜索 provider 已创建",
  "data": {
    "providers": [
      {"name": "bocha-main", "kind": "bocha", "base_url": "https://api.bochaai.com",
       "api_key_env": "SEARCH_BOCHA_API_KEY", "model": "web-search",
       "max_results": 8, "timeout_seconds": 15.0, "chain_position": 1}
    ],
    "web_search": {"enabled": true, "tool_enabled": true,
      "daily_budget_per_persona": 50, "daily_budget_global": 200, "cooldown_seconds": 60}
  }
}
```

usage-summary 示例：

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": {
    "day_start_ms": 1794899200000,
    "global_used": 3,
    "global_limit": 200,
    "persona_usage": [
      {"persona_id": "7", "used": 2, "limit": 50},
      {"persona_id": "9", "used": 1, "limit": 50}
    ]
  }
}
```

## 配置节管理（ConfigSectionAdmin 搜索子类）

`SearchRegistryAdminService` 消费配置节管理基座的「读改写节（条目 CRUD）」接入形态：
条目级校验（唯一名 409 / 存在性 404 / 唯一 provider 守卫 409）在各突变方法的
`asyncio.Lock` 锁窗口内先行完成，构造新 `SearchConfig` 后调用继承的
`_persist_and_rebuild` 尾段（原子持久化 → 热重建 → 失败回滚内存）。缝实现：
`_section_key="search"` / `_current_section=runtime_search_config`（本模块运行时
真源，模块属性访问——加载 / CRUD 换血后全消费方自动生效）/ `_validate_section`
= 条目名唯一（节级兜底）/ `_resolve_yaml_path` = 搜索域加载路径回落 loader 路径
（同一 config.yaml）。

**唯一 provider 守卫语义**：`web_search.enabled=true` 时删除最后一个条目会破坏
「启用即至少一个条目」的启动校验不变式——409 拒绝（details 携带两步指引：
先关能力或先补条目）；能力关闭时允许清空（零条目 + 关闭是合法零配置形态）。

## 测试与门禁

- 客户端：MockTransport（零真实网络）——请求形状（URL / Bearer / 锁定体 /
  超时）/ 响应映射（含双形态兼容）/ 错误路径 / 用量事件；
- 服务：fake usage face + MockTransport 双 provider 链——failover / 冷却窗口
  （时钟注入）/ 全降级 / 预算边界（恰好达限放行）/ 用量行形状；
- 注册表：CRUD round-trip（备份字节一致 + 节外保留）/ 守卫 / 故障注入
  （fsync / 重建失败回滚）/ 并发读改写；
- API：ASGITransport CRUD 矩阵 + usage-summary（真实 UsageService + 共享
  SQLite）；chat 工具循环：脚本化 fake 网关（步数上限 forcing / 持久化仅终态
  回合 / tools=None 零变更回归）；lifespan：FakeScheduler / FakeRunner 装配
  链路（启动校验 fail fast / 通道工厂绑定 / duty 注册顺序）。
