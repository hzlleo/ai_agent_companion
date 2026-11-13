# chat — 会话循环 / 世界书 / 控制台 CLI（P0-6 交付；P1-1 会话持久化）

`backend/src/packages/harness/chat/` 承载人格感知的会话循环：chara card 正典 + 世界书 → LLM 请求消息组装（prompt Unit 文件唯一真源），经 llm 网关（role=chat）非流式 / 流式调用；P1 记忆层 chunk 1 落地**可选会话持久化**（`ff_chat_sessions` / `ff_chat_messages`——回合落库 + 会话恢复，缺省关闭保持 P0 行为）。控制台 REPL（`pixi run chat`）为用户可见交付面；app 层已完成最终装配（`app/main.py` create_app + lifespan + `/api/v1/health` 存活面 + personas / memories 路由）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `prompts.py` | **全部 LLM 提示词文本的唯一存放地**（Unit 文件）：模板 + 逐模板版本常量 + 纯渲染函数 | `CHARTER_TEMPLATE` / `PERSONA_*_SECTION_TEMPLATE` / `WORLD_BOOK_ENTRY_TEMPLATE` / `POST_HISTORY_TEMPLATE` / `FIRST_GREETING_FALLBACK_TEMPLATE` / `render_*` |
| `chat_models.py` | 会话回合 / 会话状态 / 组装结果 Model Classes（Unit 文件） | `ChatTurnModel` / `ChatSessionStateModel` / `MessageCompositionResult` |
| `service.py` | 消息组装（世界书筛选 / token 近似估算）+ `ChatService` 会话循环 | `compose_messages` / `estimate_text_tokens` / `ChatService` |
| `cli.py` | 控制台 REPL（启动 / 停机序列 + 命令面 + 流式直写） | `run_chat_cli` / `main` |

依赖方向：chat → persona（chara card / PersonaRecordDTO / PersonaService）+ llm（LlmGateway / 消息模型）+ config / database / pools（CLI 启动栈）+ core（errors / time_utils）。prompt 文本与逻辑分离（模板全在 prompts.py；service / cli 禁止内联 prompt 字符串）。

## 提示词组装链（单一真相）

`compose_messages(state)` 纯函数（无 I/O——测试直调面）：

```
[system 首块]
    ① render_charter()                                会话章程（任务简报帧）
    ② 世界书 before_char 条目块                         检索增强（背景 / 世界观）
    ③ render_persona_system_prompt(card)               人格正典：
        ├─ card.system_prompt 非空 → 整体覆写（正典自定义提示）
        └─ 否则 name 自称头 + 非空叙事段（description / personality / scenario，
           空字段段跳过）
    ④ 世界书 after_char 条目块                          检索增强（行为补充）
[历史回合]
    ⑤ 全部 turns → ChatMessageModel（时间序；含刚追加的用户回合）
[system 末块]
    ⑥ render_post_history_message(card)                历史后再注入（deep
                                                       anti-drift nudge——
                                                       卡片声明 post_history_instructions 时）
```

各块以 `\n\n` 连接；⑥ 位于**最后一条历史回合之后**（紧邻生成位——防长对话人格漂移）。

### 世界书筛选语义

| 规则 | 行为 |
|---|---|
| `constant=True` | 恒注入（世界观保底——**不占 token 预算**） |
| 关键词触发 | `keys` 任一命中扫描文本；`selective=True` 再要求 `secondary_keys` 任一命中 |
| 扫描窗口 | 最近 `scan_depth` 条回合内容拼接（`None` = 全部历史） |
| 大小写 | 按条目 `case_sensitive`（默认不区分） |
| token 预算 | 关键词条目按 `(position, insertion_order)` 升序累计 `estimate_text_tokens`；首条超 `token_budget` 即截断（其后关键词条目不再注入） |
| 注入次序 | `before_char` 段在人格正典前 / `after_char` 段在后；同段内 `insertion_order` 升序 |
| `recursive_scanning` | 标志读取但 P0 未启用（P1 记忆层扩展点） |

token 近似估算（P0 无 tokenizer 的确定性约定）：CJK 区段字符 1 token/字；其余字符 4 字符 ≈ 1 token。会话结构性上限见下（防 in-memory 历史无限膨胀）。

## 会话流（ChatService）

```
create_session(persona=记录 | db_session=查激活人格)
    → ChatSessionStateModel（卡片快照 + 空回合列表 + 结构性上限）
opening(state)                       首条人格消息（first_mes 优先，空则兜底模板）
    → 追加为首个 assistant 回合（幂等：已有 assistant 回合则返回其内容）
reply(state, user_text)              非流式：追加用户回合 → compose → chat_by_role
stream_reply(state, user_text)       流式：追加用户回合 → compose → stream_by_role
    → 逐块 yield 增量；流自然结束后聚合全文追加 assistant 回合
clear_history(state) / history(state)   /clear 与 /history 命令面
```

网关调用参数恒定：`role=chat`、`purpose="chat"`、`persona_id=str(persona_id)` 显式直传（与 `bind_persona_context` 上下文语义等价；选直传为可测性）。

失败 / 边界语义：

- 空白用户输入 → `VALIDATION_ERROR`；模型空回复 / 空流 → `LLM_CALL_FAILED`；
- 网关异常透传（用户回合保留在历史——重试语义由调用方决定）；
- 流式被消费方提前终止（`GeneratorExit`）→ **不追加助手回合**（半截回复不入历史）；
- 结构性上限：`max_history_turns`（默认 64，滑动窗口丢最老）+ `max_turn_chars`（默认 2000，单回合截断保护）。

**会话持久化（P1 记忆层——可选绑定，缺省关闭）**：`ChatService` 构造注入 `session_service`（memory 域 `ChatSessionService`）后：

- `create_session(db_session=..., title=...)` 同步落 `ff_chat_sessions` 行（经 `bind_persona_context` 绑定人格上下文——PersonaMixin 自动注入），状态携带 `session_id`；未给 `db_session` 时纯内存态；
- `reply / stream_reply` 的用户与助手回合经 `session_scope` 短事务逐条落 `ff_chat_messages`（seq 递增；**开场白不落库**——正典确定性回放，resume 后幂等重现）；网关失败时用户回合保留在库（重试即重新生成）；流式提前终止不落半截助手回合；持久化写入失败原样上抛（append-only 日志不静默丢行）；
- `resume_session(session_id, db_session=...)` 把持久化会话回放进内存态（全量消息重放进 `turns`；卡片按会话归属人格重取**当前**正典）；调用方须先绑定目标人格上下文（他人格会话 NOT_FOUND）；未绑定持久化服务的 resume → `INTERNAL_ERROR`；
- 持久化消息不受 `max_history_turns` 滑动窗口约束（全量记录——内存窗口只是上下文组装面）；
- 未绑定持久化服务（P0 形态）时行为与既有完全一致——`/clear` 仅清内存历史（持久化消息保留）。

**P0 锁定决策（保持）**：会话创建时快照卡片（运行期管理面编辑不热更到在途会话；resume 按当前正典重取）。

## prompt 版本与 byte-lock

prompts.py 模块级版本常量：`PROMPTS_VERSION`（套件总版本）+ 逐模板 `*_TEMPLATE_VERSION`。模板文本变更 = 行为变更，须同步：① 上调版本常量；② 更新 `tests/unit/test_chat_prompts.py` 的 sha256 摘要锁（`_BYTE_LOCKS`）——测试红即「知情确认」关卡。

## 控制台 CLI（pixi run chat）

启动序列（isolated-safe——全组件可注入）：

```
settings → setup_logging_from_settings → load_all_config
    → PoolManager start → init_database
    → LlmGateway(usage_recorder=wire_default_usage_recorder())
    → PersonaService.get_active_persona（session_scope）
    → ChatService(session_service=ChatSessionService())   恒绑定持久化
    → create_session(persona, db_session)                 会话行落库
```

- **无激活人格**：打印中文三步指引（POST 创建 → PUT 启用 → POST activate 激活），LIFO 收尾后退出码 1；
- **有激活人格**：横幅 + 开场白 → REPL；**每次启动即开新持久化会话**（空会话直接退场无害，管理面可删除——文档化决策）。

REPL 命令面：`/quit`（退出码 0；EOF 等效）/ `/clear`（清空内存历史，人格绑定与持久化消息保留）/ `/history`（回合列表，含时间戳）/ `/new`（新建持久化会话，重新开场）/ `/sessions`（最近 10 个持久化会话——id / 消息数 / 标题 / 最后活跃）/ `/resume <id>`（恢复历史会话续聊——回放回合进上下文，开场白幂等重现：有 assistant 回合返回末条，否则正典开场）/ `/help`；空行跳过；网关与持久化 `BusinessException` 上屏不崩循环。

停机序列（LIFO——启动逆序）：`close_database` → `pool_manager.stop`（含 core 共享线程池）。

## app 装配（app/main.py + app/api/health.py）

```
create_app() → FastAPI(title/version/lifespan)
    ├─ register_exception_handlers(app)          共享异常处理器
    ├─ include_router(personas_router)           /api/v1/personas*
    ├─ include_router(memories_router)           /api/v1/memories*（P1-1 记忆管理面）
    └─ include_router(health_router)             /api/v1/health

startup_app_state(app, settings[, config_dir])   可独立驱动的纯异步函数对
    setup_logging_from_settings → load_all_config
    → init_database → pool_manager.start → app.state.gateway = LlmGateway(默认用量接线)
shutdown_app_state(app)                          LIFO：pool stop → close_database → 线程池幂等兜底
lifespan                                          上述函数对的 asynccontextmanager 薄封装
```

函数对不藏在 lifespan 闭包内——测试无需 ASGI lifespan runner 直调（本工程不引入 asgi-lifespan 依赖）。`app.state.gateway` 为后续 API 消费方预装配；CLI 自建等价栈。persona / usage ORM 在 main.py 导入期注册（fresh SQLite 首启 create_all 即全表；生产 schema 以 Alembic 为准）。

### GET /api/v1/health（liveness）

零依赖存活面（不触 DB / LLM / 池——依赖宕机时仍 200）：

```json
{
  "success": true,
  "code": "200",
  "message": "成功",
  "data": {
    "status": "ok",
    "version": "0.1.0",
    "ts_ms": 1762939200000
  }
}
```

版本真源 `APP_VERSION`（health.py）：发行元数据（editable install 名 `ai-agent-companion-backend`）优先；未安装形态（pythonpath 直跑）回落 `0.1.0`。

## 消息序列 JSON 示例

`compose_messages` 产出的请求消息序列（两轮对话后）：

```json
{
  "model": "glm-5.3",
  "messages": [
    {
      "role": "system",
      "content": "【会话章程】\n你正在与用户进行一对一的陪伴对话…\n\n【世界书 · 补充设定】\n世界观保底设定\n\n你现在扮演「大肥鱼」——一名真实可信的 AI 陪伴者。\n…\n\n【性格特点】\n元气、贪吃、爱撒娇…"
    },
    {"role": "assistant", "content": "你回来啦！今天有没有想我？"},
    {"role": "user", "content": "今天上班好累呀"},
    {"role": "assistant", "content": "辛苦啦，快坐下来歇歇。"},
    {"role": "user", "content": "想吃小鱼干"},
    {
      "role": "system",
      "content": "【重要提醒】\n以上是「大肥鱼」与用户的历史对话。…请始终以 大肥鱼 的身份…"
    }
  ]
}
```

`MessageCompositionResult` 观测元数据：

```json
{
  "system_prompt": "（同上 system 首块全文）",
  "post_history_message": "（同上 system 末块全文；未声明为 null）",
  "included_book_entries": 2,
  "estimated_system_tokens": 186
}
```

## 测试与门禁

- `tests/unit/test_chat_prompts.py`——byte-lock（sha256 摘要锁）+ 渲染契约（覆写 / 空段跳过 / 兜底）；
- `tests/unit/test_chat_service.py`——假网关记录组装消息：正典字段 / 世界书全语义（constant / scan_depth / selective / 大小写 / 预算截断 / 位置次序）/ PHI 末位 / 参数直通 / 流式聚合与提前终止 / 会话上限 / 查库路径（共享 SQLite）；
- `tests/unit/test_chat_persistence.py`——持久化集成（P1-1）：落库 / 网关失败保留用户回合 / resume 全链路 / 持久化关闭兼容 P0 / CLI `/new` `/sessions` `/resume` 命令（真实启动栈 + tmp SQLite + 假网关）；
- `tests/unit/test_chat_cli.py`——注入式 I/O 驱动全流程（真实启动栈 + tmp SQLite + 假网关，never real stdin）：命令面 / 无激活人格指引（退出码 1）/ 业务异常不崩 REPL / main 入口；
- `tests/unit/test_app_main.py`——create_app 路由注册 / startup-shutdown 函数对（真实组件 + LIFO 追踪）/ health 与 personas 经 ASGITransport 走真实 app / lifespan 全周期 / 外部引擎治理；
- 门禁：`harness/chat` ≥80%（实测 99.4%）、`app/main.py` ≥80%（实测 100%）、`app/api` ≥80%（实测 100%）。
