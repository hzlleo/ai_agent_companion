# channels — 平台通道层（P3 chunk 1 交付：Telegram 首个适配器）

`backend/src/packages/harness/channels/` 承载平台通道抽象：**通道中立路由器**（入站消息 → 人格会话循环 → 出站投递）+ **平台适配器**（Telegram 首个——手写 Bot API 客户端 + 长轮询 runner）。P3 锁定决策：Telegram first；通道抽象保持类型可扩展（`ChannelType` StrEnum——P4 B站 / QQ 在此追加成员与同级子模型）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `channel_models.py` | 通道中立 Model Classes（Unit 文件）：通道类型值域 / 入站 / 出站 / 上下文 / 投递结果 | `ChannelType` / `InboundMessageModel` / `OutboundMessageModel` / `ChannelContextModel` / `ChannelDeliveryResultModel` |
| `channel_service.py` | 通道路由器（每聊天会话 + 人格绑定 + 单消息错误隔离）+ 适配器发送面协议 | `ChannelRouter` / `ChannelAdapterProtocol` / `CHANNEL_FALLBACK_REPLY_TEXT` |
| `telegram/tg_models.py` | Bot API wire 模型子集（Unit 文件）：update / message / 响应包裹（泛型） | `TgUpdateModel` / `TgMessageModel` / `TgResponseModel[T]` / `TgSendMessageResult` / `TgBotInfoModel` |
| `telegram/tg_client.py` | 手写 Bot API 客户端（共享 httpx client 注入；4096 分段） | `TelegramClient` / `split_tg_message_text` |
| `telegram/tg_runner.py` | 长轮询 runner（offset 持久化 + 去重 + 退避 + 优雅停机 + 发送面适配） | `TelegramChannelRunner` |

依赖方向：channels → chat（ChatService）+ persona（bind_persona_context）+ config（Settings.channels）+ core（errors / thread_pool / time_utils）；HTTP 出站一律经 pools 共享 `httpx.AsyncClient`（硬规则：无私建连接）。锁定设计决策：**不引入 aiogram / python-telegram-bot**——按 Bot API 协议手写（与 llm 域「不引入 openai SDK」同约定）。

## 架构：适配器 / 路由器 / runner 链路顺序

```
Telegram 云端
    ↓ getUpdates 长轮询（offset 游标）
TelegramChannelRunner（telegram/tg_runner.py）
    ↓ TgUpdateModel → InboundMessageModel（ID 字符串化 / epoch 秒 ×1000 归一）
ChannelRouter.handle_inbound（channel_service.py）
    ① 适配器寻址（未注册 → CHANNEL_DISABLED，仅记录不抛）
    ② 每聊天会话懒建（身份键 channel:chat_id——见「会话语义」）
    ③ bind_persona_context(state.persona_id)
    ④ ChatService.reply(state, text)          组装 → LlmGateway（role=chat）
                                              → 用量记账自动发生（零通道侧
                                              额外用量行——通道层不重复记账）
    ⑤ 适配器发送 OutboundMessageModel
TelegramChannelRunner.send_message（发送面适配）
    ↓ 4096 分段（见「分段规则」）
TelegramClient（telegram/tg_client.py）
    ↓ 共享 httpx.AsyncClient → Bot API
Telegram 云端
```

装配（`app/main.py` lifespan，`_startup_channels` / `_shutdown_channels`）：

```
TELEGRAM_ENABLED=true 时：
    TelegramClient（token 解析——缺失即 CHANNEL_TOKEN_MISSING fail fast）
    → ChannelRouter（ChatService 工厂闭包 app.state.gateway +
       ChatSessionService；session_factory=session_scope 持久化装配）
    → TelegramChannelRunner → router.register_adapter(TELEGRAM, runner)
    → asyncio.create_task(runner.run()) + background_task_manager.register
    （app.state.channels_router / channels_runner / channels_task 三槽位）
TELEGRAM_ENABLED=false（缺省）：no-op——零通道对象构造
```

停机 LIFO（`shutdown_app_state`）：`_shutdown_channels`（先于公共池——runner 消费共享 HTTP client）→ `pool_manager.stop()` → `close_database()` → `shutdown_shared_thread_pool()`。通道停机链：`request_stop()`（优雅——在途长轮询最多再等一个窗口）→ bounded 等待（5s）→ 超时 `cancel` 兜底（offset 已逐批持久化，重启安全）。

## 人格语义（P3 锁定决策）

**单一激活人格应答全部通道消息**：会话懒建经 `ChatService.create_session(db_session=...)` 内部解析当前激活人格（persona 域注册表的事务性单激活不变式消费面）；每聊天按会话身份绑定人格随 P4 多通道扩展交付。`reply` 全程经 `bind_persona_context(state.persona_id)` 绑定人格上下文（PersonaMixin 表作用域——与 chat 域会话持久化同语义）。

## 会话语义（每聊天懒建 + 持久化）

- **身份键**：`channel:chat_id`（如 `channel:telegram:10086`）——路由器内存字典维度；持久化会话标题同值（`ff_chat_sessions.title`，便于管理台溯源）；
- **懒建**：每聊天首条消息建会话（`create_session(db_session=...)` → 落库携 `session_id`）；其后消息复用同一 `ChatSessionStateModel`（内存回合累积 + `ChatService.reply` 逐回合持久化——`ff_chat_messages`，经 ChatService 自持 `session_scope`，通道层零额外持久化路径）；
- **重启行为（文档化边界）**：进程重启后路由器内存字典为空——同聊天新消息起新会话（新 `session_id`；历史行保留在库）。按身份键恢复既有会话属 P4 多通道扩展面（需会话身份列 / 查询 API，当前 memory 域未提供）；
- **纯内存降级形态**（`session_factory=None`——测试 / 诊断用）：会话创建不落库；真实 `ChatService` 将抛 `INTERNAL_ERROR` → 走兜底回发。

## 错误隔离（铁律）

**单条消息的任何失败不得击穿 runner**——`ChannelRouter.handle_inbound` 恒不抛异常：

| 失败类别 | 用户面行为 | 错误细节 |
|---|---|---|
| LLM / 网关 / 持久化 / 未知异常 | 回发短中文兜底：`我这边出了点问题，稍后再试`（不引用原消息） | 只进日志（`logger.opt(exception=True)`） |
| 无激活人格（`NOT_FOUND`——已知确定态） | 回发业务异常的用户面消息（含创建 / 激活指引） | 只进日志 |
| 兜底回发自身失败 | 放弃该消息（空投递结果返回）——不级联 | 只进日志 |
| 未注册适配器（`CHANNEL_DISABLED`） | 静默空结果（无适配器可回发） | 只进日志 |

runner 循环层另有兜底：getUpdates 失败按指数退避重试（见下）——两层隔离保证长轮询任务不死亡（`asyncio.CancelledError` 原样上抛——停机 cancel 路径不被吞）。

## offset 持久化（重启不重消费）

- **游标语义**：批末推进 `offset = max(update_id) + 1`；下一轮 `getUpdates(offset=...)` 携带（服务端据此丢弃更早更新——确认语义）；非消息更新与无文本消息**跳过路由但 offset 照常推进**（否则服务端无限重投）；
- **状态文件**：`state_dir/telegram_offset.json`（`{"offset": N}` 单键）；
- **原子写**：临时文件（`.tmp-<epoch-ms>`）+ flush + fsync + `os.replace` 原子换名（config 域 registry_admin 同模式）；**不做 .bak 备份轮换**——offset 是单键可重建状态（丢失 = 重消费一遍待处理更新，幂等无害），不值得备份成本（文档化决策，与 registry_admin 的差异点）；
- **降级**：文件损坏 → 告警 + 视为首次拉取；写失败 → 告警 + 内存态继续——两种降级均不让 runner 死；
- **阻塞 I/O 下沉**：读写均经 `run_blocking_ops`（core 共享线程池 / 事件循环默认执行器——不占事件循环）。

## 去重与退避

- **去重**：在途 `update_id` 已见集合（FIFO 窗口 1024 条）兜底「确认前重投」（服务端 offset 确认是主去重面——集合只覆盖确认间隙的双投）；
- **退避**：连续失败按 `backoff_base_seconds × 2^(n-1)` 指数增长、`backoff_max_seconds` 封顶；成功即复位。仅覆盖 getUpdates 链路（发送失败在路由器层兜底隔离，不进退避）。

## 启动校验与 token 间接

- **token 永不落配置树**：`TelegramSettings.bot_token_env` 只持有变量名（默认 `TELEGRAM_BOT_TOKEN`）——与 llm `api_key_env` / SeekDB `password_env` 同间接模式；实际 token 由 `TelegramClient` 构造期从环境变量读取；
- **缺失 fail fast**：`TELEGRAM_ENABLED=true` 且变量缺失 / 纯空白 → lifespan 装配期抛 `CHANNEL_TOKEN_MISSING`（中文错误信息含变量名）——app 拒绝启动；
- **无效 fail fast**：runner `run()` 首步 getMe 校验——401/403（token 无效 / 撤销）抛 `CHANNEL_TOKEN_MISSING` 终止任务（清晰错误日志 + lifespan done-callback 落 ERROR）；暂态失败（网络 / 5xx）仅告警并进入退避循环（不因启动时网络抖动杀通道）。

## sendMessage 4096 分段规则

| 规则 | 行为 |
|---|---|
| 文本 ≤ 4096 字符 | 单条发送 |
| 超限 | 每段「前 4096 字符窗口」内找**最后一个换行符**切分（段落不跨语义行；切点换行符被消费——下段不以空行开头） |
| 窗口内无换行（或仅在首位） | 硬切 4096 |
| `reply_to_message_id` | 只挂首段（续文逐段引用会渲染成多条引用噪声） |
| 空文本 / 非数字引用 ID | `VALIDATION_ERROR`（调用方 bug 面——不进重试） |

## 错误码映射（channels 域段——本 chunk 新增于 core ErrorCode 注册表）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| token 环境变量缺失 / 为空（构造期） | `CHANNEL_TOKEN_MISSING` | 500 | 否（fail fast） |
| HTTP 401 / 403 或 ok=False + error_code 401/403（token 无效） | `CHANNEL_TOKEN_MISSING` | 500 | 否 |
| 其余 HTTP 非 200 / ok=False / 传输异常 / JSON 非法 | `CHANNEL_API_ERROR` | 503 | runner 退避 |
| 路由器收到未注册适配器通道的入站消息 | `CHANNEL_DISABLED` | 409 | 否（装配面） |

## Settings（`Settings.channels`）

env 面（`env_prefix=TELEGRAM_`；嵌套覆写 `CHANNELS__TELEGRAM__*` 亦可用）：

| 字段 | env 变量 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `TELEGRAM_ENABLED` | `false` | 是否装配通道 runner（false = 零通道对象构造） |
| `bot_token_env` | `TELEGRAM_BOT_TOKEN_ENV` | `TELEGRAM_BOT_TOKEN` | bot token 环境变量名（**间接**——token 本值永不落配置树） |
| `api_base` | `TELEGRAM_API_BASE` | `https://api.telegram.org` | Bot API 基础 URL（可指向自建代理） |
| `poll_timeout_seconds` | `TELEGRAM_POLL_TIMEOUT_SECONDS` | `25` | 长轮询超时秒数（1–50；请求超时 = 该值 + 5s 传输裕量） |
| `owner_chat_id` | `TELEGRAM_OWNER_CHAT_ID` | `null` | 属主会话 ID（空字符串归一 null；P3 duty 轮值班通知目标——本 chunk 不消费） |
| `backoff_base_seconds` | `TELEGRAM_BACKOFF_BASE_SECONDS` | `1.0` | 轮询失败退避基数秒 |
| `backoff_max_seconds` | `TELEGRAM_BACKOFF_MAX_SECONDS` | `60.0` | 退避封顶秒数（校验 ≥ 基数） |
| `state_dir` | `TELEGRAM_STATE_DIR` | `data/channels` | offset 状态目录（相对路径按进程 CWD 解析） |

JSON 示例（pydantic-settings 复杂类型 env 形态 + .env 形态）：

```bash
# .env 形态（分节 env 前缀）
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:AAF-your-real-bot-token   # bot_token_env 指向的本值（永不落 yaml）
TELEGRAM_POLL_TIMEOUT_SECONDS=25
TELEGRAM_BACKOFF_BASE_SECONDS=1.0
TELEGRAM_BACKOFF_MAX_SECONDS=60.0
TELEGRAM_STATE_DIR=data/channels
```

```yaml
# config.yaml 不承载通道配置（env 面专属——与 server/database 同约定）；
# 嵌套覆写形态（进程环境变量，优先级高于 .env）：
#   CHANNELS__TELEGRAM__ENABLED=true
#   CHANNELS__TELEGRAM__POLL_TIMEOUT_SECONDS=10
```

隔离构造（测试 / 工具）：

```python
from packages.harness.config.models import ChannelsSettings, TelegramSettings
from packages.harness.config.settings import build_isolated_settings

settings = build_isolated_settings(
    channels=ChannelsSettings(
        telegram=TelegramSettings(enabled=True, state_dir="/tmp/channels-state")
    )
)
```

> 注：`backend/.env.example` 由根 `scripts/generate_examples*.py` 从配置类内省生成；其分节登记表本 chunk 未变更（scripts/ 不在交付面），TELEGRAM_* 键面以本表 + `src/conftest.py` 硬覆盖清单为准。

## 测试面

| 测试文件 | 覆盖 |
|---|---|
| `tests/unit/test_channels_models.py` | 值域 / 校验 / 默认值 / 身份键 |
| `tests/unit/test_channels_settings.py` | env 面对齐 / 默认值 / 边界校验 / 隔离构造 / 嵌套覆写 |
| `tests/unit/test_tg_client.py` | MockTransport：请求形状（timeout / offset / 超时裕量）/ 4096 分段 / 错误映射表 / token fail fast |
| `tests/unit/test_tg_runner.py` | fake client + fake router + 注入休眠：offset 往返 / 去重 / 退避 / 优雅停机 / 启动校验 / 发送面 |
| `tests/unit/test_channel_router.py` | fake ChatService：会话复用 / 持久化调用形状 / 人格绑定 / 错误隔离矩阵 |
| `tests/unit/test_app_channels_lifespan.py` | enabled / disabled 装配 / 停机 LIFO 顺序 / token 缺失 fail fast / 任务终态收割 / cancel 兜底 |

零真实网络（MockTransport / fake）；零真实配置（隔离 Settings + conftest 硬覆盖；测试 token 为专用假值）；全部 `async def test_*`。
