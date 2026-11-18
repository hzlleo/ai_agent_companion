# config — 配置域

`backend/src/packages/harness/config/` 承载全工程配置：env 面 Settings（server / database / log / pools / llm / rhythm / flow / group_persona 等分节）、config.yaml 结构化面（模型注册表 / 角色绑定 / 多模态回退链 / 搜索 provider 注册表）、extensions_config.json 工具清单、运行时加载器（含热重载地基）、配置节管理基座（原子持久化 / 热重建 / 回滚共享机械）、模型解析器、wire 构建器与交互式配置向导。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 配置树 Model Classes（env 面五分节 + Settings 聚合器 + LlmConfig / MultimodalFallbackConfig / SearchConfig / ExtensionsConfig） | `Settings` / `settings` / `LlmConfig` / `SearchConfig` |
| `settings.py` | 全局单例访问面 + 隔离构造助手 | `settings` / `build_isolated_settings` |
| `loader.py` | config.yaml + extensions_config.json 运行时加载（分层加载 + 重载回调） | `load_all_config` / `register_reload_callback` |
| `section_admin.py` | 配置节管理基座（写锁串行化 / 原子持久化 + 备份轮换 / 热重建 + 失败回滚——节无关共享机械） | `ConfigSectionAdmin` / `DEFAULT_BACKUP_KEEP` |
| `registry_admin.py` | LLM 注册表管理服务（基座的 llm 段子类：条目 CRUD + 角色绑定守卫 + 脱敏读取） | `RegistryAdminService` / `render_config_yaml_text` |
| `registry_admin_dtos.py` | 注册表管理传输契约（创建 / 更新 / 脱敏 / 状态 / 角色绑定行 DTO） | `ModelEntryCreateDTO` / `RegistryStateDTO` |
| `search_registry.py` | 搜索 provider 注册表管理服务（基座的 search 段子类：provider CRUD + 唯一 provider 守卫 + 脱敏 + 运行时真源三件套） | `SearchRegistryAdminService` / `runtime_search_config` / `load_search_config` |
| `model_entry.py` | 模型注册表条目 + 角色枚举 + 密钥解析 | `LlmModelEntry` / `ModelRole` / `parse_model_entry` / `resolve_api_key` |
| `model_resolver.py` | 角色 → 条目解析器（热重建安全） | `ModelResolver` / `get_model_resolver` |
| `llm_wire.py` | 条目 → 网关 wire 配置（密钥解析收口） | `build_llm_wire_config` |
| `multimodal_fallback_loader.py` | look_at 非多模态时的回退链裁决 | `resolve_multimodal_chain` |
| `wizard.py`（facade）+ `wizard_ui/_fields/_parsers/_generators/_dtos` | 交互式配置向导六件套 | `run_wizard` / `main` |

依赖方向：config → core（合法）；core 不 import config（依赖边界，`LogConfig.to_core_config()` 完成映射接线）。

## 分层加载链（顺序）

```
代码默认值（Field default）
    ↓ 覆盖
.env 文件（pydantic-settings：各分节 env_prefix 读取）
    ↓ 覆盖
进程环境变量（pydantic-settings 语义：env 优先于 .env 文件）
    ↓ 互补（不重叠）
config.yaml / extensions_config.json（结构化面：llm 模型注册表 /
角色绑定 / 多模态回退链 / 工具清单——经 loader 运行时加载）
```

env 面与 yaml 面职责分离：基础设施参数（连接 / 端点 / 池）走 env；多端点结构化配置（模型注册表）走 config.yaml。文件缺失为合法状态（首次部署 / 测试 scratch CWD——保持默认值）；文件存在但解析失败为 `CONFIG_INVALID` fail loud。

## Settings env 面（.env.example 由 `pixi run gen-examples` 自动生成）

| 分节 | env 前缀 | 字段（默认值） |
|---|---|---|
| ServerConfig | `SERVER_` | `HOST`(127.0.0.1) · `PORT`(8000) · `WORKERS`(1) |
| DatabaseConfig | `DATABASE_` | `TYPE`(mysql) · `HOST`(127.0.0.1) · `PORT`(3306) · `NAME`(ai_agent_companion) · `USER`(root) · `PASSWORD`("") · `POOL_SIZE`(10) · `MAX_OVERFLOW`(20) · `POOL_RECYCLE`(3600) · `POOL_PRE_PING`(true) |
| LogConfig | `LOG_` | `LEVEL`(INFO) · `FILE`(logs/app.log) · `JSON_FILE`(logs/app.json.log) · `ROTATION`(1 day) · `RETENTION`(30 days) · `MODULE_LEVELS`("") · `COLORIZE_CONSOLE`(true) · `DIAGNOSE_EXCEPTIONS`(false) |
| PoolsConfig | `POOLS_` | `THREAD_POOL_SIZE`(8) · `HTTP_MAX_CONNECTIONS`(100) · `HTTP_MAX_KEEPALIVE_CONNECTIONS`(20) · `HTTP_CONNECT_TIMEOUT_SECONDS`(5.0) · `HTTP_TIMEOUT_SECONDS`(30.0) · `LLM_SEMAPHORE_LIMIT`(5) · `LLM_REQUEST_TIMEOUT_SECONDS`(120) · `CB_FAIL_MAX`(5) · `CB_RESET_TIMEOUT_SECONDS`(60) |
| LlmEndpointConfig | `LLM_` | `BASE_URL`("") · `API_KEY`("") · `MODEL`("") |
| SeekDBSettings | `SEEKDB_` | `ENABLED`(false) · `HOST`(127.0.0.1) · `PORT`(2881) · `USER`(root) · `PASSWORD_ENV`(SEEKDB_PASSWORD) · `DATABASE`(test) · `VECTOR_DIM`(1024) · `POOL_MIN`(1) · `POOL_MAX`(4) |
| TelegramSettings | `TELEGRAM_` | `ENABLED`(false) · `BOT_TOKEN_ENV`(TELEGRAM_BOT_TOKEN) · `API_BASE`(https://api.telegram.org) · `POLL_TIMEOUT_SECONDS`(25) · `OWNER_CHAT_ID`(null) · `BACKOFF_BASE_SECONDS`(1.0) · `BACKOFF_MAX_SECONDS`(60.0) · `STATE_DIR`(data/channels) |
| QqChannelSettings | `QQ_` | `ENABLED`(false) · `HTTP_API_BASE_URL`(http://127.0.0.1:3000) · `ACCESS_TOKEN_ENV`(QQ_ACCESS_TOKEN) · `WEBHOOK_TOKEN_ENV`(QQ_WEBHOOK_TOKEN) · `REQUEST_TIMEOUT_SECONDS`(15.0) |
| DutySettings | `DUTY_` | `ENABLED`(false) · `DAILY_AT`(22:00) · `MAX_TOOL_STEPS`(8) · `REPORT_TO_OWNER`(true) · `CHARTER_TEXT`(null) |
| DreamScheduleSettings | `DREAM_SCHEDULE_` | `ENABLED`(false) · `DAILY_AT`(03:00) |
| SchedulerSettings | `SCHEDULER_` | `TICK_SECONDS`(1.0) |
| ToolsSandboxSettings | `TOOLS_SANDBOX_` | `ENABLED`(false) · `BASE_URL`(http://127.0.0.1:8765) · `TOKEN_ENV`(SANDBOX_TOKEN) · `REQUEST_TIMEOUT_SECONDS`(15.0) |
| VoiceSettings.tts（TtsSettings） | `VOICE_TTS_` | `ENABLED`(false) · `BASE_URL`(http://127.0.0.1:9880) · `MODEL_ENTRY`(cosyvoice3) · `REQUEST_TIMEOUT_SECONDS`(30.0) · `DEFAULT_SPEAKER`(中文女) · `DEFAULT_SPEED`(1.0) |
| VoiceSettings.asr（AsrSettings） | `VOICE_ASR_` | `ENABLED`(false) · `BASE_URL`(http://127.0.0.1:8082) · `MODEL_ENTRY`(qwen3-asr-0.6b) · `REQUEST_TIMEOUT_SECONDS`(60.0) · `LANGUAGE`(null) · `MAX_UPLOAD_BYTES`(26214400) |
| WebSearchSettings（`Settings.search`） | `SEARCH_` | `ENABLED`(false) · `TOOL_ENABLED`(false) · `DAILY_BUDGET_PER_PERSONA`(50) · `DAILY_BUDGET_GLOBAL`(200) · `COOLDOWN_SECONDS`(60) |
| RhythmSettings（`Settings.rhythm`） | `RHYTHM_` | `ENABLED`(false) · `BEDTIME`(23:30) · `WAKE_TIME`(08:00) · `TIMEZONE_NOTE`(naive-local) · `REPLY_DELAY_MIN_MS`(1500) · `REPLY_DELAY_MAX_MS`(45000) · `TYPING_SPEED_MS_PER_CHAR`(45) · `TYPING_BASE_MS`(800) · `BURST_PROBABILITY`(0.12) |
| FlowGateSettings（`Settings.flow`） | `FLOW_` | `ENABLED`(false) · `BURST_WINDOW_MS`(4000) · `BURST_COUNT`(3) · `MERGE_QUIET_MS`(2500) · `MERGE_MAX_MESSAGES`(5) · `MIN_REPLY_GAP_MS`(1200) · `BACKLOG_DEFER_THRESHOLD`(6) · `BACKLOG_DEFER_STEP_MS`(8000) · `BACKLOG_DEFER_MAX_MS`(90000) · `PENDING_GRACE_MS`(60000) |
| GroupPersonaSettings（`Settings.group_persona`） | `GROUP_PERSONA_` | `ENABLED`(false) · `MAX_RESPONDERS`(1) · `COOLDOWN_MS`(60000) |
| Settings | （无前缀） | `ENV`(local) |

分节与 `.env.example` 逐分节对齐（生成器 `scripts/generate_examples_env.py` 的 `_ENV_SECTIONS` 登记表为序——键面以 `.env.example` 为真源，本表为语义说明；敏感字段在 example 中恒留空）。各分节语义详见所属域文档（channels / duty / tools-sandbox / voice）。

- `DatabaseConfig.async_sqlalchemy_url`：`mysql` → `mysql+aiomysql://user:pwd@host:port/db?charset=utf8mb4`（用户名/密码 URL 编码）；`sqlite` → `sqlite+aiosqlite:///{name}`（`name=":memory:"` 内存库）。
- `LogConfig.to_core_config()`：字段一一映射为 `core.LoggingConfig`（`file→log_file` / `json_file→log_json_file`）；app lifespan 经 `setup_logging_from_settings(settings)` 接线 `core.setup_logging`。
- 嵌套覆盖：`Settings` 开启 `env_nested_delimiter="__"`（如 `DATABASE__POOL_SIZE`），与各分节自身 `env_prefix` 并行生效。
- 测试隔离：`build_isolated_settings(database=..., pools=..., seekdb=...)` 显式值构造（子配置实例 pass-through，不重读 env）。

### SeekDBSettings（检索层 env 面）

SeekDB（v1.4.0，MySQL 线协议 2881 端口）承载记忆检索索引（见 memory.md「检索层」）。要点：

- `ENABLED=false`（默认）——全工程走 SQLite 回退存储（keyword 模式），零配置开发可用、测试 hermetic；
- `PASSWORD_ENV` 密码环境变量**间接**（与 llm 条目 `api_key_env` 同模式——密码永不落 yaml / 配置树；实际密码从该变量读取，未设置时空串 + 告警）；
- `VECTOR_DIM` 插值进索引表 DDL 的 `VECTOR(dim)`——须与 embed 角色模型输出一致（默认 1024 = Qwen3-Embedding-0.6B）；
- `POOL_MAX >= POOL_MIN` 模型校验强制。

`SEEKDB_*` 全部键位见 `.env.example` 的 SEEKDB 分节（env 模板由 `scripts/generate_examples_env.py` 自 Pydantic 模型自动生成——模板与模型不会漂移）。

## config.yaml — llm 段（模型注册表）

```yaml
llm:
  models:
    - name: "main-chat"                    # 条目唯一标识（角色绑定引用）
      provider: "openai_compatible"        # provider 类型
      base_url: "https://api.example.com/v1"
      api_key_env: "MAIN_CHAT_API_KEY"     # 密钥环境变量名（空 = 本地免鉴权）
      model: "glm-5.3"                     # 模型名（provider model 参数）
      roles: ["chat", "duty"]              # 条目承接的角色集
      concurrency: 4                       # per-entry 并发上限（空 = 全局默认）
      multimodal: false                    # 多模态输入支持标志
  role_bindings:                           # 显式角色绑定（优先于 roles 声明）
    chat: "main-chat"
  multimodal_fallback:                     # look_at 非多模态时的回退链
    local:                                 # 第一级：本地视觉端点（低延迟）
      name: "local-vision"
      base_url: "http://127.0.0.1:8001/v1"
      api_key_env: ""
      model: "qwen2.5-vl-7b"
      multimodal: true
    remote:                                # 第二级：远程多模态端点（兜底）
      name: "remote-vision"
      base_url: "https://api.example.com/v1"
      api_key_env: "REMOTE_VISION_API_KEY"
      model: "glm-4.6v"
      multimodal: true
```

### LlmModelEntry 字段表

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `name` | `str (min_length=1)` | — | 条目唯一标识；重名条目后者覆盖前者（告警） |
| `provider` | `str` | `openai_compatible` | provider 类型（多 provider OpenAI 兼容注册表） |
| `base_url` | `str (min_length=1)` | — | API 基础 URL（含 `/v1`） |
| `api_key_env` | `str` | `""` | 密钥环境变量名（空 = 本地免鉴权；密钥永不落 yaml） |
| `model` | `str (min_length=1)` | — | 模型名（provider model 参数） |
| `roles` | `list[ModelRole]` | `[]` | 承接角色集；值域 chat / duty / dream / look_at / embed |
| `concurrency` | `int \| None (ge=1)` | `None` | per-entry 并发上限（None = `POOLS_LLM_SEMAPHORE_LIMIT` 回落） |
| `multimodal` | `bool` | `false` | 多模态输入支持（look_at 回退判据） |

### 角色解析链（顺序）

```
resolve(role)
    ↓ ① 显式角色绑定（llm.role_bindings[role] → 按条目名查表；
    │     绑定指向不存在条目名 → 告警并视为未绑定）
    ↓ ② 条目自身 roles 声明（注册表顺序，首个声明该角色的条目胜出；
    │     同角色重复声明 → 告警并保持先注册者）
    ↓ 未命中 → BusinessException(MODEL_NOT_FOUND, details 含角色名
              与已绑定角色清单)
```

热重建语义：`ModelResolver.rebuild` 原子替换内部索引（条目表 + 角色→条目索引）；在途已解析条目引用不受影响（不可变 Pydantic 模型）。loader 每次加载 config.yaml 后经 `rebuild_model_resolver` 重建全局索引。

### 多模态回退链（顺序）

```
look_at 角色绑定的条目 multimodal=true？
    ├─ 是 → [该条目]（零回退直连）
    └─ 否 → 链 = [multimodal_fallback.local, multimodal_fallback.remote]
             中已配置的级（本地优先——低延迟 / 零外发成本；远程兜底）
             ├─ 链非空 → 按序返回（调用方逐级尝试）
             └─ 链为空 → BusinessException(CONFIG_INVALID，含修复指引：
                        配置 local/remote 级或将 look_at 绑定到
                        multimodal 条目)
```

## 配置节管理基座（section_admin.py — ConfigSectionAdmin）

config.yaml 结构化配置节**写路径的共享抽象基建**：从 LLM 注册表管理面抽取的节无关机械，全部配置节管理面（现有消费者：`llm` 段注册表（registry_admin）、`search` 段搜索 provider 注册表（search_registry））复用同一实现，不再各自复制。

### 基座提供的机械

| 机械 | 语义 |
|---|---|
| 写串行化 | `asyncio.Lock` 串行化全部突变——单事件循环内「校验→持久化→重建」为原子窗口，杜绝并发写相互覆盖丢失条目 |
| 原子持久化 | 写前整文件备份 `.bak-<epoch-ms>` → 临时文件写入 + flush + fsync → `os.replace` 原子换名 → 备份轮换（保留最新 `backup_keep` 份）；任一步失败清理临时文件，原文件不变（崩溃窗口内 config.yaml 恒为完整旧版或完整新版） |
| 重建 + 回滚编排 | 持久化成功后经重建钩子重读磁盘（证明落盘可加载并整体换血运行时）；重建失败 → 经设置钩子把写前快照恢复到运行时（仅回滚内存——磁盘保留新内容与 `.bak` 写前备份供人工恢复） |
| 阻塞 I/O 下沉 | 文件读写 / 备份 / 换名全部经 `run_blocking_ops`（core 共享线程池） |

### 缝契约（子类实现；`SectionConfigT` 绑定 BaseModel——零裸 dict 公共面）

| 缝 | 语义 | LLM 子类实现（RegistryAdminService） |
|---|---|---|
| `_section_key` | 持有的 config.yaml 顶层键 | `"llm"` |
| `_current_section` | 读内存运行时当前节（get——快照 / 读改写基底） | `loader.runtime_llm_config` |
| `_apply_section_to_runtime` | 节整体应用到运行时（set——回滚恢复路径） | `loader.apply_llm_config_to_runtime` |
| `_render_section` | 节 Model Class → yaml 段 dict（无损回读序列化） | `_llm_section`（registry_admin） |
| `_validate_section` | 节级校验钩子（持久化前最后闸门，零 I/O 副作用） | 空体（域决策：条目级校验在突变方法锁窗口内先行；重名 / 悬挂绑定在解析器层告警容错） |
| `_rebuild_from_disk` | 热重建钩子（重读磁盘整体换血运行时） | `loader.load_config_yaml(path=...)` |
| `_resolve_yaml_path` | 默认持久化目标解析 | `loader.get_loaded_config_yaml_path()` |
| `_backup_suffix`（可覆写默认） | 备份文件名后缀（默认 `.bak`；覆写须保持「字典序 == epoch-ms 时间序」轮换契约） | 默认 `.bak` |

### 接入形态（两种——按突变语义选择）

- **简单节（整体替换语义）**：直接调 `await admin.mutate_section(new_section, yaml_path=None)`——锁内「校验→快照→持久化→热重建」；调用方给出最终节内容。
- **读改写节（条目 CRUD）**：子类自持突变方法（参照 `RegistryAdminService.create_entry`）：`async with self._write_lock` 窗口内「读当前节 → 操作特定校验（唯一 409 / 存在 404 / 守卫 409）→ 构造新节」后调用继承的 `_persist_and_rebuild` 尾段——**构造发生在锁内**，杜绝并发读改写丢失条目。

### 链路顺序（写路径单一真相）

```
校验（节级钩子 _validate_section；条目级校验在子类突变方法锁窗口内先行）
    ↓ 快照当前节（model_copy(deep=True)——回滚源）
    ↓ 原子持久化（写前备份 .bak-<epoch-ms> → 临时文件 + fsync
    │             → os.replace 原子换名 → 备份轮换保留 backup_keep 份）
    ↓ 热重建（_rebuild_from_disk 重读磁盘——证明落盘内容可加载，
    │         并整体换血运行时）
    ↓ 重建失败 → _apply_section_to_runtime(写前快照) 回滚内存
                + BusinessException(CONFIG_INVALID 422)
                （磁盘保留新内容与 .bak 写前备份——下次成功写自愈覆盖）
```

### 未来配置节接入

新配置节管理面 = 定义节 Model Class（列表节 + 绑定式语义）→ 子类化 `ConfigSectionAdmin[节类型]` 实现七个缝 → 条目 CRUD 突变方法走读改写形态（锁内校验 + 构造）。机械（锁 / 备份 / 轮换 / 重建编排 / 回滚）零复制——与 LLM 注册表行为同源。

### 搜索 provider 注册表子类（search_registry.py — R2a-4 已接入）

基座的第二个消费者，缝实现：

| 缝 | 搜索子类实现（SearchRegistryAdminService） |
|---|---|
| `_section_key` | `"search"` |
| `_current_section` | 本模块 `runtime_search_config`（运行时真源——loader 语义镜像，**消费方经模块属性访问**，加载 / CRUD 换血后自动生效） |
| `_apply_section_to_runtime` | `apply_search_config_to_runtime`（回滚路径） |
| `_render_section` | `_search_section`（providers + web_search 全量——子块按当前生效值落盘） |
| `_validate_section` | 条目名唯一（节级兜底——跨操作不变式） |
| `_rebuild_from_disk` | `_rebuild_runtime_from_yaml`（重读磁盘 search 段整体换血） |
| `_resolve_yaml_path` | 搜索域加载路径（`load_search_config` 记录）回落 loader 路径——同一 config.yaml |

条目级守卫（锁窗口内先行）：唯一名 409 / 存在性 404 / **唯一 provider 守卫**
（`web_search.enabled=true` 时删除最后一个条目 → 409 + 两步指引——保住
「启用即至少一个条目」的启动校验不变式；能力关闭时允许清空）。脱敏读取：
条目只回 `api_key_env` 变量名 + failover 链位次（`chain_position`）。

### WebSearchSettings（搜索运营 env 面——单类双源）

`WebSearchSettings`（env_prefix=`SEARCH_`）承载联网搜索运营开关 / 预算 / 冷却，
**同时是 config.yaml `search.web_search` 子块的类型**（分层合并遵循本文件顶部
优先级链——yaml 子块键覆盖 env，未提供的键回落 env / 代码默认；provider CRUD
持久化时把当前生效值整体落盘——yaml 最高层，写入即固化）。要点：

- `ENABLED=false`（默认）——零配置开发可用、测试 hermetic；`ENABLED=true` 且
  `search` 段无 provider 条目时启动期 fail fast；
- `TOOL_ENABLED` 与 `ENABLED` 为**两级开关**——`web_search` 工具仅在两者同真
  时注册（能力总闸 + 工具面曝光闸）；
- 密钥间接键（`SEARCH_BOCHA_API_KEY` / `SEARCH_ZHIPU_API_KEY`——provider 条目
  `api_key_env` 默认指向的变量）不在 Settings env 面内（yaml 注册表间接引用），
  按敏感键守卫在 `.env.example` 留空。

全部字段语义 / 预算口径 / failover 链详见 [search.md](search.md)。

## config.yaml — search 段（搜索 provider 注册表）

```yaml
search:
  providers:            # 列表序 = failover 尝试序（前者为主、后者兜底）
    - name: "bocha-main"
      kind: "bocha"     # bocha | zhipu
      api_key_env: "SEARCH_BOCHA_API_KEY"   # 密钥本值永不落 yaml
      max_results: 8
      timeout_seconds: 15
    - name: "zhipu-fallback"
      kind: "zhipu"
      model: "search_std"                    # zhipu 引擎码（缺省 search_std）
      api_key_env: "SEARCH_ZHIPU_API_KEY"
  web_search:           # 运营子块（WebSearchSettings——yaml 键优先于 SEARCH_* env）
    enabled: true
    tool_enabled: true
    daily_budget_per_persona: 50
    daily_budget_global: 200
    cooldown_seconds: 60
```

`search` 段由 `search_registry.load_search_config` 自行解析（loader 仅解析
`llm` 段——其「未知顶层键」告警对本段无害，键值原样保留）；条目字段表 /
wire 契约 / 管理面详见 [search.md](search.md)。

## llm_wire — 网关下发契约（llm 网关消费）

`build_llm_wire_config(entry, fallback_api_key=None)` 构建 `LlmClientConfigModel`（provider / base_url / api_key / model / concurrency / multimodal）。密钥解析收口于此：条目 `api_key_env` → 环境变量明文；声明了变量但未设置且给兜底 → 兜底明文；免鉴权条目（`api_key_env=""`）恒空串（不注入兜底）。`api_key` 字段 repr 隐藏（不落日志）。

## extensions_config.json — 工具清单

```json
{
    "tools": [
        {
            "name": "look_at_image",
            "description": "查看用户发送的图像并描述内容……",
            "parameters": {
                "type": "object",
                "properties": {"image_path": {"type": "string"}},
                "required": ["image_path"],
                "additionalProperties": false
            }
        }
    ]
}
```

`ExtensionTool.parameters` 为 `JsonSchema` Model Class（`extra="allow"` 容纳全部 JSON Schema 键；`additionalProperties` 键经 alias 映射）。

## 加载器 API（loader.py）

| API | 语义 |
|---|---|
| `load_config_yaml(path=None, config_dir=None)` | 解析 llm 段 → 更新 `runtime_llm_config` + 重建解析器索引 + 记录回退链 |
| `load_extensions_config(path=None, config_dir=None)` | 解析 tools → 更新 `extensions_config` |
| `load_all_config(config_dir=None)` | 一次性加载两文件 + 通知全部重载回调（app lifespan 最先调用，先于 `pool_manager.start()`） |
| `register_reload_callback(cb)` | 注册重载后回调（轻量状态重置；单回调失败 best-effort 不阻断） |
| `reset_config_runtime_state()` | 复位全部运行时状态（测试隔离） |
| `setup_logging_from_settings(settings)` | Settings.log → `core.setup_logging` 接线 |

## 配置向导（wizard 六件套）

```
pixi run setup                       # 交互式配置（DB / 日志 / 池 / LLM 端点逐项询问）
pixi run setup -- --dry-run          # 预览不写入
pixi run setup -- --non-interactive  # 使用模板默认值（CI/自动化）
```

主流程：读取 `.env.example` 解析字段 → 已有 `.env` 现值作默认 → 按步骤收集输入（敏感字段 getpass 隐藏 / 布尔 Y·n / 数字校验）→ 摘要（敏感打码）→ 确认后写入 `.env`（值替换）+ `config.yaml` / `extensions_config.json`（模板复制）。字段中文标签从 Settings 各分节 `Field(description=...)` 内省（`wizard_ui._FIELD_LABELS`）——与配置模型零漂移。测试经 `answers` 注入式应答驱动（全量注入面，零真实交互）。

## 示例三件套生成

```
cd backend && pixi run gen-examples
```

从 Pydantic 配置类内省生成 `backend/.env.example` / `config.example.yaml` / `extensions_config.example.json`（幂等 byte-stable——由 `test_config_examples_generation.py` 逐字节校验锁定）。示例数据单一真源：`LlmConfig.default_models()` / `ExtensionsConfig.default_tools()`。修改配置类后重新生成再提交。

## 测试与门禁

- 测试位置：`src/tests/unit/test_config_*.py`（settings / loader / section_admin / model_entry / model_resolver / llm_wire / multimodal_fallback / wizard / examples_generation，9 份）；LLM 注册表管理面的端到端契约在 `src/tests/unit/test_models_api.py`（见 [llm.md](llm.md) 注册表管理一节）；
- `test_config_section_admin.py` 锁基座缝契约（玩具节替身）：持久化 round-trip（未知顶层键保留 + 备份字节一致）/ 校验钩子零 I/O 副作用 / 原子写失败不留临时文件 / 重建失败内存回滚 / 备份轮换上限 / 写锁读改写串行化 / 不可解析既有文件拒绝覆写；
- 全部 `async def test_`；零真实配置（loader 测试经 `tmp_path` config_dir 注入；Settings 经 `build_isolated_settings` 显式值构造；向导经注入式应答）；
- 覆盖率（`pixi run coverage-gates` 实测）：harness/config 组 **96.4%**（section_admin 基座 + registry_admin 子类计入本组）；
- 运行：`cd backend && pixi run tests-unit` / `pixi run gen-examples` / `pixi run setup --help`。
