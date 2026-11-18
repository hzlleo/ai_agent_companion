# chat — 会话循环 / 世界书 / 控制台 CLI（含会话持久化）

`backend/src/packages/harness/chat/` 承载人格感知的会话循环：chara card 正典 + 世界书 → LLM 请求消息组装（prompt Unit 文件唯一真源），经 llm 网关（role=chat）非流式 / 流式调用；记忆层落地**可选会话持久化**（`ff_chat_sessions` / `ff_chat_messages`——回合落库 + 会话恢复，缺省关闭保持纯内存行为）。控制台 REPL（`pixi run chat`）为用户可见交付面；app 层已完成最终装配（`app/main.py` create_app + lifespan——personas / memories / usage / voice / look_at / models / channels-qq webhooks / health 共八路由）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `prompts.py` | **全部 LLM 提示词文本的唯一存放地**（Unit 文件）：模板 + 逐模板版本常量 + 纯渲染函数 | `CHARTER_TEMPLATE` / `PERSONA_*_SECTION_TEMPLATE` / `WORLD_BOOK_ENTRY_TEMPLATE` / `POST_HISTORY_TEMPLATE` / `FIRST_GREETING_FALLBACK_TEMPLATE` / `render_*` |
| `output_protocol.py` | 输出协议解析（R2b——纯函数 Unit）：前导 ``<心声>`` / ``<情绪>`` 标签 → 可见正文 / 心声 / 情绪增量；流式起始前缀 best-effort 剥离器 | `parse_assistant_output` / `ParsedAssistantOutputModel` / `StreamTagStripper` / `strip_leading_protocol_prefix` |
| `chat_models.py` | 会话回合 / 会话状态 / 组装结果 Model Classes（Unit 文件；含世界书回合状态——冷却追踪） | `ChatTurnModel` / `ChatSessionStateModel` / `MessageCompositionResult` |
| `world_book.py` | 世界书高级选择算法链（R2b——纯函数 Unit 文件；rng / 回合状态可注入）+ token 近似估算 | `select_world_book_entries` / `SelectedEntryModel` / `WorldBookTurnStateModel` / `WorldBookSelectionResult` / `RECURSION_HOP_CAP` / `estimate_text_tokens` |
| `service.py` | 消息组装（消费选择结果）+ `ChatService` 会话循环（回合状态回存） | `compose_messages` / `ChatService` |
| `cli.py` | 控制台 REPL（启动 / 停机序列 + 命令面 + 流式直写） | `run_chat_cli` / `main` |

依赖方向：chat → persona（chara card / PersonaRecordDTO / PersonaService）+ llm（LlmGateway / 消息模型）+ config / database / pools（CLI 启动栈）+ core（errors / time_utils）。prompt 文本与逻辑分离（模板全在 prompts.py；service / cli 禁止内联 prompt 字符串）。`world_book` 只依赖 persona 卡片模型（不导入 chat 其他 Unit——依赖无环；`service` 再导出 `estimate_text_tokens` 供既有测试面）。

## 提示词组装链（单一真相）

`compose_messages(state, rng=None)` 纯函数（无 I/O——测试直调面；世界书选择经 `world_book.select_world_book_entries`，rng 可注入——仅 `trigger_probability < 1.0` 卡片消耗随机数；推进后的回合状态随结果携带，由 `ChatService` 回存）：

```
[system 首块]
    ① render_charter()                                会话章程（任务简报帧）
    ② 世界书 before_char 条目块                         检索增强（背景 / 世界观）
    ③ render_persona_system_prompt(card)               人格正典：
        ├─ card.system_prompt 非空 → 整体覆写（正典自定义提示）
        └─ 否则 name 自称头 + 非空叙事段（description / personality / scenario，
           空字段段跳过）
    ④ 心情状态块（emotion_summary 非空时）              R2b 轻情绪——M 态
                                                       非中性摘要（见
                                                       [emotion.md](emotion.md)）
    ⑤ 长期指令块（directives 非空时）                   R2b D7-14 USER.md
                                                       式常备指令（见
                                                       [persona.md](persona.md)
                                                       长期指令一节）
    ⑥ render_output_protocol()                          R2b 输出格式协议
                                                       （心声 / 情绪标签指令）
    ⑦ 世界书 after_char 条目块                          检索增强（行为补充）
[历史回合]
    ⑧ 全部 turns → ChatMessageModel（时间序；含刚追加的用户回合）
[system 末块]
    ⑨ render_post_history_message(card)                历史后再注入（deep
                                                       anti-drift nudge——
                                                       卡片声明 post_history_instructions 时）
```

各块以 `\n\n` 连接；⑨ 位于**最后一条历史回合之后**（紧邻生成位——防长对话人格漂移）。

长期指令块（⑤）的渲染语义（`render_directives_block`）：每条指令一行
`- 正文`；最多 20 条且总字符 ≤ 2000（含列表符号）——超帽追加
「（其余长期指令已省略）」说明行；无有效指令时整块跳过。块文本模板
（`DIRECTIVES_BLOCK_TEMPLATE`）与截断说明行（`DIRECTIVES_TRUNCATION_NOTE`）
同属 prompt Unit 文件 byte-lock 面（版本常量 `DIRECTIVES_BLOCK_TEMPLATE_VERSION`）。

注：`compose_messages(state, rng=None, emotion_summary=None, directives=None)`
仍是纯函数——M 态读取 / 衰减与常备指令读取均在 `ChatService` 侧（仅持久化
会话已落库且对应服务已接线时激活），纯内存形态 / 无持久化测试面零附加 I/O。
长期指令读取失败不阻断回复（日志 + 空块——增强面语义）。

### 意图快径（含 DIRECTIVE 常备指令捕获——R2b D7-14）

通道路径的 `ChatService.reply` 前端可注入意图快径面（`INTENT_ENABLED` 默认开启）：
高频固定问法（时间 / 问候 / 身份 / 帮助）直接命中罐头回复并**完全跳过网关**。
R2b 追加的 `DIRECTIVE` 类型捕获「记住 / 记得 / 以后都 / 以后 / 别再 / 不要」开头
的消息：matcher 剥离前缀与句末标点得到正文，经装配注入的 `DirectiveSinkFace`
落库为长期指令（[persona.md](persona.md) 长期指令一节），**落库成功才**回复
「已记住：正文」；落库失败 / 未接线时退化为普通附和（不承诺记住）。副作用恒不
阻断回复链路（错误仅日志）；快径回合照常落用户 + 助手回合（记忆 / 梦境可见）。

### 世界书筛选语义（R2b 高级触发——`world_book.select_world_book_entries` 纯函数）

选择算法链（顺序锁定，逐候选按链序过门；字段规格见 persona.md 世界书高级触发语义一节）：

```
① constant 恒注入 → ② 关键词匹配（scan_depth 窗口） → ③ 递归扩展
（书级总开关 × 条目级收窄，2 跳上限 + visited 环安全） → ④ 互斥分组
剪枝（确定性优先级） → ⑤ 概率门（rng 可注入，消耗序 = 渲染序） →
⑥ 冷却门（回合状态——功能式传递） → ⑦ token 预算装箱（渲染序累计，
首条超预算即截止）
```

| 规则 | 行为 |
|---|---|
| `constant=True` | 恒注入（世界观保底——**不占 token 预算**、无视分组 / 概率 / 冷却门） |
| 关键词触发 | `keys` 任一命中扫描文本；`selective=True` 再要求 `secondary_keys` 任一命中 |
| 扫描窗口 | 最近 `scan_depth` 条回合内容拼接（`None` = 全部历史；**书级**——条目级扫描面只有递归正文） |
| 大小写 | 按条目 `case_sensitive`（默认不区分） |
| 递归扩展 | 书级 `recursive_scanning` 总开关 + 条目级 `recursive` 收窄；2 跳上限（跳 2 入选者正文不再入面）；visited 集合保证环不可能 |
| 互斥分组 | 同 `include_group` 非 constant 命中条目只注入 `(insertion_order, 条目索引)` 升序最高优先级者；constant 无视分组 |
| 概率门 | `trigger_probability < 1.0` 时消耗 `rng.random() < p`；=1.0 恒过门零消耗（既有卡片零随机性） |
| 冷却门 | 注入后 `cooldown_turns` 个组装回合内拒绝（第 N+1 回合解禁）；回合状态功能式传递（选择函数不变更入参，推进态经结果返回由 `ChatService` 回存） |
| token 预算 | 候选按渲染序累计 `estimate_text_tokens`；首条超 `token_budget` 即截断（其后非 constant 候选不再评估） |
| 注入次序（渲染序） | `before_char` 段在人格正典前 / `after_char` 段在后；**段内 `(depth 降序, insertion_order 升序, 索引升序)`**——depth 0 为段尾槽（最贴近对话末尾一侧）；缺省 depth=4 同槽退化为既有纯 insertion_order 序（兼容证明：既有测试零变更） |

冷却的会话作用域（诚实文档化）：回合计数与注入追踪存于 `ChatSessionStateModel.world_book_state`（组装调用计数 + `{条目索引: 注入回合号}`）——**纯内存态、无持久化**，进程重启即复位；`reply / stream_reply` 每次组装后回存推进态（快径回复不组装、不推进时钟）。

token 近似估算（无 tokenizer 的确定性约定）：CJK 区段字符 1 token/字；其余字符 4 字符 ≈ 1 token。会话结构性上限见下（防 in-memory 历史无限膨胀）。

## 会话流（ChatService）

```
create_session(persona=记录 | db_session=解析主人格)
    → ChatSessionStateModel（卡片快照 + 空回合列表 + 结构性上限）
opening(state)                       首条人格消息（first_mes 优先，空则兜底模板）
    → 追加为首个 assistant 回合（幂等：已有 assistant 回合则返回其内容）
reply(state, user_text)              非流式：追加用户回合 → compose → chat_by_role
stream_reply(state, user_text)       流式：追加用户回合 → compose → stream_by_role
    （compose 含世界书选择 + 回合状态回存——冷却时钟每次组装 +1）
    → 逐块 yield 增量；流自然结束后聚合全文追加 assistant 回合
clear_history(state) / history(state)   /clear 与 /history 命令面
```

网关调用参数恒定：`role=chat`、`purpose="chat"`、`persona_id=str(persona_id)` 显式直传（与 `bind_persona_context` 上下文语义等价；选直传为可测性）。

**输出协议（R2b——`reply` 面）**：模型原始输出先经
`output_protocol.parse_assistant_output` 解析——返回 / 落回合 / 进历史 /
发通道的**只有可见正文**；心声与情绪增量只进回合 `meta`（`ff_chat_messages.meta`，
9 号迁移）与会话详情 API。协议语法与解析规则见下节「输出协议」；流式边界
见同节「stream_reply 边界」。

失败 / 边界语义：

- 空白用户输入 → `VALIDATION_ERROR`；模型空回复 / 空流 → `LLM_CALL_FAILED`；
- 网关异常透传（用户回合保留在历史——重试语义由调用方决定）；
- 流式被消费方提前终止（`GeneratorExit`）→ **不追加助手回合**（半截回复不入历史）；
- 结构性上限：`max_history_turns`（默认 64，滑动窗口丢最老）+ `max_turn_chars`（默认 2000，单回合截断保护）。

**会话持久化（记忆层——可选绑定，缺省关闭）**：`ChatService` 构造注入 `session_service`（memory 域 `ChatSessionService`）后：

- `create_session(db_session=..., title=...)` 同步落 `ff_chat_sessions` 行（经 `bind_persona_context` 绑定人格上下文——PersonaMixin 自动注入），状态携带 `session_id`；未给 `db_session` 时纯内存态；
- `reply / stream_reply` 的用户与助手回合经 `session_scope` 短事务逐条落 `ff_chat_messages`（seq 递增；**开场白不落库**——正典确定性回放，resume 后幂等重现）；网关失败时用户回合保留在库——调用方重试是**完整重发**（用户消息再次进入历史与数据库，append-only；「重生成 / 复用上一条用户输入」的显式面见下节「消息变体（swipes）」）；流式提前终止不落半截助手回合；持久化写入失败原样上抛（append-only 日志不静默丢行）；
- `resume_session(session_id, db_session=...)` 全量读取持久化会话、经 `max_history_turns` 窗口截断后回放进内存态 `turns`（保留最新回合；卡片按会话归属人格重取**当前**正典）；调用方须先绑定目标人格上下文（他人格会话 NOT_FOUND）；未绑定持久化服务的 resume → `INTERNAL_ERROR`；
- 持久化消息不受 `max_history_turns` 滑动窗口约束（全量记录——内存窗口只是上下文组装面）；
- 未绑定持久化服务（纯内存形态）时行为与既有完全一致——`/clear` 仅清内存历史（持久化消息保留）。

**情景落库（对话回合 → 情景层管道——可选绑定，缺省关闭）**：`ChatService` 构造可注入
`episode_sink`（memory 域 `EpisodeSinkFace` 实现 `ChatTurnEpisodeSink`）。app 通道工厂
绑定 `app.state.episode_sink` 后，每个**已落库**回合随即写入 kind=chat 情景事件
（user 回合 provenance=owner / assistant 回合 provenance=agent；载荷含会话主键与对应侧
文本；不去重——重复回合是真实事件的重复）。时序锁定：消息行提交之后才写情景（消息是
事实源，情景是派生镜像）；未落库会话（纯内存态）零写入；sink 恒不抛（失败仅日志，
回复链路不受影响）；fire-and-forget（后台管理器绑定——停机 drain 收口）。
未注入 sink（CLI / 单元测试 / 未装配记忆服务的形态）行为与既有完全一致。

**锁定决策（保持）**：会话创建时快照卡片（运行期管理面编辑不热更到在途会话；resume 按当前正典重取）。

## 输出协议（`<心声>` / `<情绪>`——R2b）

`prompts.py` 的 `OUTPUT_PROTOCOL_TEMPLATE`（版本常量
`OUTPUT_PROTOCOL_TEMPLATE_VERSION`）在 compose 中作为格式指令块注入，要求
模型按下列文法输出：

```
[<心声>一两句不会直接说出口的内心活动</心声>]
[<情绪>维度:+增量,维度:增量,...</情绪>]
正文
```

- 两个标签均可选、次序容忍、各至多生效一次；`<心声>` 内容只属于题内面
  （persist 到 meta），**永不进入可见正文 / 通道 / TTS**；
- 情绪维度值域 = emotion 域 `EmotionDimension` 8 维（joy 愉悦 / affection
  亲昵 / shyness 羞怯 / excitement 兴奋 / calm 平静 / sadness 低落 /
  anxiety 不安 / annoyance 微恼）；增量取值 -1..1，解析器夹取越界值；
- 未知维度丢弃（debug 记录）、重复维度后者覆盖、同种标签重复时首个生效
  （重复标签连同内容消费丢弃）；
- **优雅降级**：无标签 / 标签未闭合 / 标签内容全非法 → 原文可见、
  `thought=None`、`emotion_deltas={}`——解析器从不抛异常（模型输出不可信）。

解析产物落库形态（`ff_chat_messages.meta`——JSON，可空）：

```json
{
  "thought": "他今天又迟到了，会不会有麻烦。",
  "emotion_deltas": {"joy": 0.4, "affection": 0.2},
  "emotion_snapshot": {"joy": 0.9, "affection": 0.7, "calm": 0.5}
}
```

`GET /api/v1/memories/sessions/{id}` 的 `messages[].meta` 即上述文档（未产生
元数据的回合为 `null`）；前端会话详情以灰色斜体「心声:」块与情绪小标签
渲染（对方不可见面）。情绪状态推进 / 衰减 / 摘要规则见
[emotion.md](emotion.md)。

### stream_reply 边界（锁定）

流式路径**不跑完整协议**：`stream_reply` 经 `StreamTagStripper` 仅对流的
**起始前缀**做 best-effort 标签剥离（完整出现的前导标签区扣留并丢弃；未知
标签原样放行；未闭合标签在流结束时剥离开标签标记、其后文本原样交还）；
**心声 / 情绪不落 meta、不推进 M 态**（流式通道是操作员可见面——诚实边界，
不引入逐块语义解析复杂度）。落库正文 = 已放行（客户端实际可见）文本。

## 消息变体（swipes——R2b D7-5）

单回合多候选重生成 + 选定提升（实现：`harness/chat/swipe.py` 的 `SwipeService`，
持久面见 [memory.md](memory.md) 的 `ff_chat_messages` 变体列）。设计边界（锁定）：

- **上下文脊柱**：重生成只回放父回合之前的**正典对话链**（`variant_of_id IS NULL`）——
  既有变体**不进入上下文**（每回合针对原始对话脊柱重掷）；已选中的变体已就地替换正典
  正文，因此自然进入后续回合上下文。这是「swipes 是逐回合重掷」语义的直接结果；
- **生成路径**：与 `reply` 同一 `compose_messages`（人格卡片 / 世界书 / 情绪摘要 /
  输出协议全量复用，**prompt 字节零变更**）+ `chat_by_role`（`role=chat`、
  `purpose="chat"`、`persona_id` 直传）；**不装载工具面**（单轮候选——工具循环属
  `reply` 的回合语义）；
- **协议面**：每个候选独立经 `parse_assistant_output` 解析——可见正文落变体 `content`，
  心声 / 情绪增量落 `meta`（附 `variant_index`）；变体情绪增量**不推进共享 M 态**
  （探索面：只有选定变体经 select API 时推进，见下）；
- **持久化**：候选全部生成成功后才逐条落库（网关中途失败 / 空正文 → 零变体行残留）；
  变体行 `seq = 父回合 seq`、`variant_of_id = 父回合主键`、`variant_index` 从父回合
  既有最大值**续编**（既有候选不清洗——历史全部保留）；变体不推进会话
  `message_count` / `last_active_at`（正典链计数只度量真实对话回合）；
- **选定提升（select）**：变体正文 / 协议元数据**就地替换**父回合行内内容（父行幸存、
  正典身份不变），并在此刻**唯一一次**把该变体情绪增量推进 M 态（推进后快照 +
  `selected_variant_id` 注记写回父行 `meta`）；变体行原样保留（历史可回溯）。

**镜像分叉（append-only 真相）**：会话 episode 镜像（kind=chat）与检索索引保存的是
回合生成时刻的原始回复；选定变体只就地替换正典消息内容（及协议元数据），不会重写
镜像与索引。镜像与索引是历史轨迹，正典会话表才是展示 / 编辑面——二者有意允许分叉，
检索命中展示的是当时生成的文本而非后来选中的变体。

### API（app/api/memories.py）

| 方法与路径 | 语义 | 请求体 |
|---|---|---|
| `POST /api/v1/memories/sessions/{id}/messages/{mid}/variants` | 重生成候选变体（返回变体列表） | `{"count": 4}`（1-8，默认 4） |
| `POST /api/v1/memories/sessions/{id}/messages/{mid}/select-variant` | 选定变体提升正典回合（返回更新后的正典消息） | `{"variant_id": 22}` |
| `GET /api/v1/memories/sessions/{id}?include_variants=true` | 详情嵌套变体（`messages[].variants`，按序号升序） | —（默认 `false` 不含变体） |

父回合语义：仅 **assistant 正典链行**可作父回合（用户回合 / 变体行 → `VALIDATION_ERROR`；
他会话消息 → `NOT_FOUND`）。选定要求变体属于同一父回合（否则 `NOT_FOUND`）。

变体行（`POST .../variants` 响应项）示例：

```json
{
  "id": 22,
  "session_id": 1,
  "seq": 2,
  "role": "assistant",
  "content": "辛苦了，先坐下歇一会儿吧。",
  "ts_ms": 1760000000600,
  "meta": {
    "thought": "他听起来真的很累，先别急着给建议。",
    "emotion_deltas": {"affection": 0.3},
    "emotion_snapshot": null,
    "variant_index": 1
  },
  "variants": []
}
```

选定后的正典回合（`POST .../select-variant` 响应项）：

```json
{
  "id": 2,
  "session_id": 1,
  "seq": 2,
  "role": "assistant",
  "content": "辛苦了，先坐下歇一会儿吧。",
  "ts_ms": 1760000000500,
  "meta": {
    "thought": "他听起来真的很累，先别急着给建议。",
    "emotion_deltas": {"affection": 0.3},
    "emotion_snapshot": {"joy": 0.8, "affection": 0.6, "calm": 0.5},
    "variant_index": 1,
    "selected_variant_id": 22
  },
  "variants": [{"id": 22, "content": "辛苦了，先坐下歇一会儿吧。", "ts_ms": 1760000000600, "meta": {"thought": "他听起来真的很累，先别急着给建议。", "emotion_deltas": {"affection": 0.3}, "emotion_snapshot": null, "variant_index": 1}}]
}
```

前端会话详情对话框：assistant 回合「重生成变体」动作 → 候选轮播（`← 1/N →`、候选正文 /
心声）→「选中此条」调用 select API 后失效详情缓存重拉。

## prompt 版本与 byte-lock

prompts.py 模块级版本常量：`PROMPTS_VERSION`（套件总版本——R2b 输出协议 /
心情块加入后为 1.1.0）+ 逐模板 `*_TEMPLATE_VERSION`（含新增
`OUTPUT_PROTOCOL_TEMPLATE_VERSION` / `EMOTION_STATE_TEMPLATE_VERSION`）。模板文本变更 = 行为变更，须同步：① 上调版本常量；② 更新 `tests/unit/test_chat_prompts.py` 的 sha256 摘要锁（`_BYTE_LOCKS`）——测试红即「知情确认」关卡。

## 控制台 CLI（pixi run chat）

启动序列（isolated-safe——全组件可注入）：

```
settings → setup_logging_from_settings → load_all_config
    → PoolManager start → init_database
    → LlmGateway(usage_recorder=wire_default_usage_recorder())
    → PersonaService.get_primary_persona（session_scope）
    → ChatService(session_service=ChatSessionService())   恒绑定持久化
    → create_session(persona, db_session)                 会话行落库
```

- **无主人格**：打印中文三步指引（POST 创建 → PUT 启用 → POST set-primary 设主），LIFO 收尾后退出码 1；
- **有主人格**：横幅 + 开场白 → REPL；**每次启动即开新持久化会话**（空会话直接退场无害，管理面可删除——文档化决策）。

REPL 命令面：`/quit`（退出码 0；EOF 等效）/ `/clear`（清空内存历史，人格绑定与持久化消息保留）/ `/history`（回合列表，含时间戳）/ `/new`（新建持久化会话，重新开场）/ `/sessions`（最近 10 个持久化会话——id / 消息数 / 标题 / 最后活跃）/ `/resume <id>`（恢复历史会话续聊——回放回合进上下文，开场白幂等重现：有 assistant 回合返回末条，否则正典开场）/ `/help`；空行跳过；网关与持久化 `BusinessException` 上屏不崩循环。

停机序列（LIFO——启动逆序）：`close_database` → `pool_manager.stop`（含 core 共享线程池）。

## app 装配（app/main.py + app/api/health.py）

```
create_app() → FastAPI(title/version/lifespan)
    ├─ register_exception_handlers(app)          共享异常处理器
    ├─ include_router(personas_router)           /api/v1/personas*（人格管理面）
    ├─ include_router(memories_router)           /api/v1/memories*（记忆管理面：episodes / curated /
    │                                             sessions / search / dream）
    ├─ include_router(usage_router)              /api/v1/usage*（跨人格用量观测面）
    ├─ include_router(voice_router)              /api/v1/voice*（语音合成 / 识别）
    ├─ include_router(look_at_router)            /api/v1/look_at（图像理解 multipart 上传）
    ├─ include_router(models_router)             /api/v1/models*（LLM 注册表管理面）
    ├─ include_router(qq_channels_router)        /api/v1/channels/qq*（QQ webhook 入站）
    └─ include_router(health_router)             /api/v1/health（零依赖存活面）

startup_app_state(app, settings[, config_dir])   可独立驱动的纯异步函数对（八步启动链）
    setup_logging_from_settings → load_all_config
    → init_database → pool_manager.start
    → app.state.gateway = LlmGateway(默认用量接线)
    → app.state.memory_store = build_memory_store(检索 / 梦境 / 调度器共用)
    → _assemble_memory_services(索引管道绑定 memory_service / episode_sink /
       memory_search_service / dream_service 长寿实例组——停机槽位复位)
    → _startup_channels(TELEGRAM / QQ 启用时装配；缺省 no-op)
    → _startup_scheduler(DUTY / DREAM_SCHEDULE 启用时装配；缺省 no-op)
shutdown_app_state(app)                          LIFO 六步停机链：scheduler → channels
                                                  → 后台任务 drain → pool stop
                                                  → close_database → 线程池幂等兜底
lifespan                                          上述函数对的 asynccontextmanager 薄封装
```

函数对不藏在 lifespan 闭包内——测试无需 ASGI lifespan runner 直调（本工程不引入 asgi-lifespan 依赖）。`app.state.gateway` 为后续 API 消费方预装配；`app.state.memory_store` 为检索 / 梦境端点与调度器共用存储；CLI 自建等价栈。persona / usage ORM 在 main.py 导入期注册（memory 域经 memories 路由导入链注册——fresh SQLite 首启 create_all 即全表；生产 schema 以 Alembic 为准）。

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
- `tests/unit/test_chat_world_book.py`——R2b 选择算法链纯函数面：constant 恒注入 / 关键词窗口 / selective 副键 / 递归 2 跳上限与环安全 / 互斥分组确定性（含平局索引裁定）/ 概率门三态（种子 rng 按渲染序消耗）/ 冷却阻塞-解禁 / 功能式状态传递（入参不变更）/ token 预算截止（截断不记冷却）/ 深度槽渲染序 / 缺省字段 = 既有行为（兼容证明）；
- `tests/unit/test_chat_service.py`——假网关记录组装消息：正典字段 / 世界书全语义（constant / scan_depth / selective / 大小写 / 预算截断 / 位置次序）/ PHI 末位 / 参数直通 / 流式聚合与提前终止 / 会话上限 / 查库路径（共享 SQLite）；R2b 增补：组装回合状态功能式推进 / 种子 rng 概率门 / 冷却会话循环闭环（第 1 轮注入 → 2/3 轮缺席 → 第 4 轮回归）；
- `tests/unit/test_output_protocol.py`——协议解析矩阵（心声 / 情绪 / 双标签
  两种次序 / 无标签 / 未闭合 / 未知维度 / 越界夹取 / 重复标签 / 纯标签空正文）
  + 流式前缀剥离（扣留 / 直通 / flush 交还 / 跨块标签）；
- `tests/unit/test_chat_emotion.py`——协议 + 情绪引擎集成（真实 SQLite：可见
  正文返回 / meta 落库 + 详情 DTO 读回 / M 态推进 / 心情块注入位置 / 无标签
  零状态 / 引擎停用 / 流式边界不落 meta）；
- `tests/unit/test_chat_swipe.py`——消息变体全语义（候选落库形态 / 脊柱-only 上下文 / 序号续编 / 空正文零残留 / 父回合校验 / 选定替换 + 单次 M 态推进 + 注记 / include_variants 读侧 / 引擎停用零推进）；
- `tests/unit/test_chat_swipe_api.py`——变体端点（ASGITransport）：重生成 / 详情嵌套 / 选定 / 错误面（父回合语义 / 数量边界 / 缺失资源）；
- `tests/unit/test_migration_chat_message_variants.py`——迁移 0010 SQLite 升 / 降 / 复升 + 复合唯一键语义；
- `tests/unit/test_chat_persistence.py`——持久化集成：落库 / 网关失败保留用户回合 / resume 全链路 / 持久化关闭兼容纯内存形态 / CLI `/new` `/sessions` `/resume` 命令（真实启动栈 + tmp SQLite + 假网关）；
- `tests/unit/test_chat_cli.py`——注入式 I/O 驱动全流程（真实启动栈 + tmp SQLite + 假网关，never real stdin）：命令面 / 无主人格指引（退出码 1）/ 业务异常不崩 REPL / main 入口；
- `tests/unit/test_app_main.py`——create_app 路由注册 / startup-shutdown 函数对（真实组件 + LIFO 追踪）/ health 与 personas 经 ASGITransport 走真实 app / lifespan 全周期 / 外部引擎治理；
- 门禁：`harness/chat` ≥80%（实测 99.4%）、`app/main.py` ≥80%（实测 100%）、`app/api` ≥80%（实测 100%）。
