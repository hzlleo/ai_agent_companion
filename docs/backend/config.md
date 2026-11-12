# config — 配置域（P0-3 交付）

`backend/src/packages/harness/config/` 承载全工程配置：env 面 Settings（server / database / log / pools / llm）、config.yaml 结构化面（模型注册表 / 角色绑定 / 多模态回退链）、extensions_config.json 工具清单、运行时加载器（含热重载地基）、模型解析器、wire 构建器与交互式配置向导。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 配置树 Model Classes（env 面五分节 + Settings 聚合器 + LlmConfig / MultimodalFallbackConfig / ExtensionsConfig） | `Settings` / `settings` / `LlmConfig` |
| `settings.py` | 全局单例访问面 + 隔离构造助手 | `settings` / `build_isolated_settings` |
| `loader.py` | config.yaml + extensions_config.json 运行时加载（分层加载 + 重载回调） | `load_all_config` / `register_reload_callback` |
| `model_entry.py` | 模型注册表条目 + 角色枚举 + 密钥解析 | `LlmModelEntry` / `ModelRole` / `parse_model_entry` |
| `model_resolver.py` | 角色 → 条目解析器（热重建安全） | `ModelResolver` / `get_model_resolver` |
| `llm_wire.py` | 条目 → 网关 wire 配置（密钥解析收口） | `build_llm_wire_config` |
| `multimodal_fallback_loader.py` | look_at 非多模态时的回退链裁决 | `resolve_multimodal_chain` |
| `wizard.py`（facade）+ `wizard_ui/_fields/_parsers/_generators/_dtos` | 交互式配置向导六件套 | `run_wizard` / `main` |

依赖方向：config → core（合法）；core 不 import config（chunk 边界，`LogConfig.to_core_config()` 完成映射接线）。

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
| LogConfig | `LOG_` | `LEVEL`(INFO) · `FILE`(logs/app.log) · `JSON_FILE`(logs/app.json.log) · `ROTATION`(1 day) · `RETENTION`(30 days) · `MODULE_LEVELS`("") · `COLORIZE_CONSOLE`(true) |
| PoolsConfig | `POOLS_` | `THREAD_POOL_SIZE`(8) · `HTTP_MAX_CONNECTIONS`(100) · `HTTP_MAX_KEEPALIVE_CONNECTIONS`(20) · `HTTP_CONNECT_TIMEOUT_SECONDS`(5.0) · `HTTP_TIMEOUT_SECONDS`(30.0) · `LLM_SEMAPHORE_LIMIT`(5) · `LLM_REQUEST_TIMEOUT_SECONDS`(120) · `CB_FAIL_MAX`(5) · `CB_RESET_TIMEOUT_SECONDS`(60) |
| LlmEndpointConfig | `LLM_` | `BASE_URL`("") · `API_KEY`("") · `MODEL`("") |
| Settings | （无前缀） | `ENV`(local) |

- `DatabaseConfig.async_sqlalchemy_url`：`mysql` → `mysql+aiomysql://user:pwd@host:port/db?charset=utf8mb4`（用户名/密码 URL 编码）；`sqlite` → `sqlite+aiosqlite:///{name}`（`name=":memory:"` 内存库）。
- `LogConfig.to_core_config()`：字段一一映射为 `core.LoggingConfig`（`file→log_file` / `json_file→log_json_file`）；app lifespan 经 `setup_logging_from_settings(settings)` 接线 `core.setup_logging`。
- 嵌套覆盖：`Settings` 开启 `env_nested_delimiter="__"`（如 `DATABASE__POOL_SIZE`），与各分节自身 `env_prefix` 并行生效。
- 测试隔离：`build_isolated_settings(database=..., pools=...)` 显式值构造（子配置实例 pass-through，不重读 env）。

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

## llm_wire — 网关下发契约（P0-4 消费）

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

- 测试位置：`src/tests/unit/test_config_*.py`（settings / loader / model_entry / model_resolver / llm_wire / multimodal_fallback / wizard / examples_generation，8 份）；
- 全部 `async def test_`；零真实配置（loader 测试经 `tmp_path` config_dir 注入；Settings 经 `build_isolated_settings` 显式值构造；向导经注入式应答）；
- 覆盖率（`pixi run tests-unit` 实测）：harness/config 组 **98.1%**；
- 运行：`cd backend && pixi run tests-unit` / `pixi run gen-examples` / `pixi run setup --help`。
