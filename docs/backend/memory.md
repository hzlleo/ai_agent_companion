# memory — 三层记忆域（存储 + 检索层 + 夜间梦境）

`backend/src/packages/harness/memory/` 承载陪伴智能体的三层记忆：情景层（append-only 事件日志）、策展层（supersede 语义蒸馏记忆）、工作层（chat 会话回合持久化）。本域交付存储 + 管理 API、**检索层**（MemoryStore 抽象 + SeekDB 生产存储 + SQLite 回退 + 索引管道 + 检索 API，见下文「检索层」一节）与**夜间梦境**（情景事件 → 策展记忆的巩固整理任务 + 第一人称日记 + 运行日志 + 手工触发 API，见下文「夜间梦境」一节）。

## 三层模型

| 层 | 表 | 语义 | 写入方 |
|---|---|---|---|
| episodic（情景） | `ff_memory_episodes` | 发生过的每件事一行；**append-only**（只增不改，删除是管理面显式动作） | chat 回合 / duty 报告 / dream 产物 / platform 动作 / 管理面手工注入 |
| curated（策展） | `ff_curated_memory` | 蒸馏后的结构化记忆；同 (persona, kind, topic) **至多一条 active**（supersede 取代语义，历史链可回溯） | 管理面 upsert / 梦境整理任务 |
| working（工作） | `ff_chat_sessions` + `ff_chat_messages` | chat 会话与回合（会话内上下文组装原料） | `ChatService` 持久化绑定（见 chat.md） |

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（EpisodeKind / MemoryProvenance / CuratedKind / CuratedStatus）+ 事件载荷 Model Classes + 内容指纹 | `parse_episode_payload` / `compute_content_hash` |
| `orm.py` | 四张表（PersonaMixin 作用域切分见下） | `MemoryEpisodeRow` / `CuratedMemoryRow` / `ChatSessionRow` / `ChatMessageRow` |
| `dtos.py` | 事件 / 策展 / 会话的创建 / 记录 / 查询 / 批量删除 DTO | `EpisodeCreateDTO` / `CuratedUpsertDTO` / `ChatSessionDetailDTO` 等 |
| `service.py` | `MemoryService`（情景 + 策展；可选 indexer 联动）+ `ChatSessionService`（会话） | `MemoryService` / `ChatSessionService` |
| `retrieval_models.py` | 检索层契约（模式 / 引用类型枚举 + 索引条目 / 检索查询 / 检索结果 / 请求体 DTO + SeekDBStoreConfig） | `MemorySearchQueryDTO` / `SeekDBStoreConfig` 等 |
| `store.py` | `MemoryStore` 协议 + 纯 SQL 构建器（SeekDB 事实卡逐字对齐；hybrid = Form B JSON DSL 载荷构建） | `build_search_sql` / `build_hybrid_search_payload` / `build_upsert_sql` / `build_ensure_schema_sql` |
| `seekdb_store.py` | SeekDB 生产存储（aiomysql raw SQL——自有池 / 注入池两形态） | `SeekDBStore` |
| `sqlite_store.py` | SQLite 回退存储（keyword-only + `ff_memory_index_lite` 表） | `SQLiteFallbackStore` |
| `retrieval.py` | 检索编排（MemoryIndexer 索引管道含按引用删除 + MemorySearchService 含写读强化 + `build_memory_store` 装配工厂 + 条目构建辅助） | `MemoryIndexer` / `MemorySearchService` / `CuratedAccessReinforcerFace` / `build_memory_store` |
| `lifecycle.py` | 记忆生命周期纯函数（LifecycleConfig + 有效强度 + 归档判定——零 I/O / 零 env） | `LifecycleConfig` / `effective_strength` / `should_archive` |
| `episode_sink.py` | chat 回合情景落库面（EpisodeSinkFace + ChatTurnEpisodeSink——对话管道闭合） | `EpisodeSinkFace` / `ChatTurnEpisodeSink` |
| `dreaming_models.py` | 梦境 DTO + 构造级参数（DreamJobConfig / 统计 / 决策 / 提案 / 运行记录 / 触发请求） | `DreamJobConfig` / `DreamRunRecordDTO` / `DreamFactProposedItemModel` / `DreamSkillProposedItemModel` 等 |
| `dreaming_prompts.py` | 梦境 prompt Unit 文件（合并 + 人物事实提取 + 技能挖矿 + 日记模板——版本常量 + byte-lock） | `render_merge_prompt` / `render_facts_prompt` / `render_skills_prompt` / `render_diary_prompt` |
| `dreaming_orm.py` | 运行日志表 `ff_dream_runs`（独立文件——与 orm.py 分文件） | `DreamRunRow` |
| `dreaming.py` | 梦境编排（纯函数打分 / 规则门 + DreamService 主链路 + 运行日志读面） | `DreamService` / `recency_decay` / `select_dream_candidates` |

依赖方向：memory → core（dtos / errors / time_utils / background_tasks）+ database（Base / Mixin / engine holder）+ llm（`ChatMessageRole` / `EmbeddingsResponseModel`——嵌入契约）+ config（Settings——存储选择）+ pools（seekdb_compat 类型面 / manager 共享池）；梦境编排另消费 persona 域的 `FactsService` / `SkillsService`（提取产物的写入面）与 `bind_persona_context`（人格上下文由调用方绑定）；不依赖 chat / usage 域（嵌入用量经 gateway 的 usage 接线落账）。

## 人格作用域（PersonaMixin 切分）

- `ff_memory_episodes` / `ff_curated_memory` / `ff_chat_sessions` 挂 **PersonaMixin**：写路径 `before_flush` 自动注入 `persona_id`，读路径 `do_orm_execute` 自动过滤（调用方须先 `bind_persona_context`，缺上下文 fail loud——`PERSONA_CONTEXT_REQUIRED`）；
- `ff_chat_messages` **不挂 PersonaMixin**（messages keyed by session only）：消息读写一律先经人格作用域的会话行定位 session_id（他人格会话 NOT_FOUND 即隔离生效），再按键取消息——避免消息行冗余 persona 列的双轴一致性负担。

## 表结构

### ff_memory_episodes（情景层）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT 自增 | 主键 |
| `persona_id` | String(64)，索引 | 人格 ID（自动注入 / 过滤） |
| `kind` | String(16)，索引 | chat / duty / dream / platform / system |
| `source` | String(128) | 事件来源标识（如 `chat` / `duty:daily` / `platform:qq`） |
| `content` | JSON | 事件载荷（按 kind 判别的 Model Class 序列化形态） |
| `importance` | Integer | 重要度 1-10（检索排序 / 梦境筛选权重轴） |
| `provenance` | String(16) | owner / agent / untrusted（见 provenance 规则） |
| `content_hash` | String(16)，索引 | kind+source+载荷规范 JSON 的 sha256 短指纹（append 去重键） |
| `ts_ms` | BigInteger，索引 | 事件发生时间（epoch 毫秒；日记 / 时间范围查询轴） |
| `created_at` / `updated_at` | BigInteger | 行级时间戳（epoch 毫秒） |

### ff_curated_memory（策展层）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT 自增 | 主键 |
| `persona_id` | String(64)，索引 | 人格 ID |
| `kind` | String(16)，索引 | memory / user_note / relationship |
| `topic` | String(255)，索引 | 主题键（同 (persona, kind, topic) 单 active） |
| `content` | Text | 记忆正文 |
| `importance` | Integer | 重要度 1-10 |
| `provenance` | String(16) | **仅 owner / agent**（untrusted 服务层拒绝，永不落表） |
| `status` | String(16)，索引 | active / superseded / **archived**（归档态为生命周期终态，迁移 0012；归档行 `active_marker` 置 NULL 使同主题可重新起链） |
| `superseded_by_id` | BIGINT，自引用 FK（SET NULL） | 取代者行主键（active 行为 NULL） |
| `version` | Integer | 链内版本号（首版 1，supersede 递增） |
| `active_marker` | String(16)，可空 | 单 active 守卫标记（active 行 `'active'` / superseded、archived 行 NULL）——唯一键 `uq_ff_curated_active (persona_id, kind, topic, active_marker)` 的非 NULL 侧（两方言唯一键均忽略 NULL，多代 superseded 行共存；迁移 0007） |
| `pinned` | Boolean，默认 0 | 置顶标记（免于时间衰减与归档；迁移 0012） |
| `frozen` | Boolean，默认 0 | 冻结标记（不参与梦境合并提案 / 接收并入 / 不被归档；迁移 0012） |
| `access_count` | Integer，默认 0 | 检索命中累计次数（写读强化轴——见「记忆生命周期」；迁移 0012） |
| `last_accessed_ms` | BIGINT，可空 | 最近检索命中时刻（epoch 毫秒；NULL = 未命中；迁移 0012） |

长期指令（R2b D7-14）不新建表：以 `kind=user_note` + `topic='directive:<slug>'` 行承载
（supersede / 历史链 / 唯一键全部复用本表；服务层只做命名空间收窄——详见
[persona.md](persona.md#长期指令r2b-d7-14usermd-式常备指令)）。

### ff_chat_sessions / ff_chat_messages（工作层）

`ff_chat_sessions`：`persona_id`（PersonaMixin）/ `title` / `message_count`（下一消息 seq）/ `last_active_at`（索引）。
`ff_chat_messages`：`session_id`（FK ff_chat_sessions.id，级联删除，索引）/ `seq`（会话内从 1 递增；迁移 0010 后唯一键为复合键 `uq_ff_chat_messages_seq_variant (session_id, seq, variant_of_id, variant_index)`——变体身份 DB 级兜底；正典行两列 NULL，正典 seq 唯一性由服务层会话行锁计数维持，迁移沿革 0007 → 0010）/ `role`（user/assistant/system）/ `content` / `ts_ms` / `meta`（JSON 可空——回合内部观测元数据：`thought` 心声 / `emotion_deltas` 情绪增量 / `emotion_snapshot` 情绪快照 / 变体行 `variant_index` / 选中注记 `selected_variant_id`，**永不进入可见正文**；迁移 0009，见 [emotion.md](emotion.md)）/ `variant_of_id`（自引用 FK ff_chat_messages.id，ON DELETE CASCADE，索引；NULL = 正典链行；迁移 0010）/ `variant_index`（父回合内变体序号 1..N；NULL = 正典链行）。

消息变体（swipes——单回合多候选，[chat.md](chat.md#消息变体swipesr2b-d7-5)）：变体行
`seq = 父回合 seq`、`variant_of_id` 指向父 assistant 回合、`variant_index` 续编（既有
候选不清洗）；不推进会话 `message_count` / `last_active_at`；仅 assistant 正典回合可
作父回合（服务层强制）；降级 0009 丢弃变体行后重建旧 `(session_id, seq)` 唯一键。

**镜像分叉（append-only 真相）**：会话 episode 镜像（kind=chat）与检索索引保存的是
回合生成时刻的原始回复；选定变体只就地替换正典消息内容（及协议元数据），不回写镜像
与索引。镜像与索引是历史轨迹，正典会话表才是展示 / 编辑面——二者有意允许分叉。

`append_message(..., meta=None)` 为可选追加参数（既有调用方零变更）；会话详情
DTO `messages[].meta`（`ChatMessageMetaModel`）为读侧形态——无元数据回合为
`null`。`GET /api/v1/memories/sessions/{id}` 响应示例（assistant 回合）：

```json
{
  "id": 2,
  "session_id": 1,
  "seq": 2,
  "role": "assistant",
  "content": "你回来啦，快坐下。",
  "ts_ms": 1760000000500,
  "meta": {
    "thought": "他终于来了，等了好久。",
    "emotion_deltas": {"joy": 0.4, "affection": 0.2},
    "emotion_snapshot": {"joy": 0.9, "affection": 0.7, "calm": 0.5}
  }
}
```

（`meta` 为可空字段；流式路径不落 meta——边界见 [chat.md](chat.md)。）

## provenance 规则（全记忆层统一 taxonomy）

| 值 | 语义 | 策展层准入 |
|---|---|---|
| `owner` | 主人说的（最高可信） | ✅ |
| `agent` | 智能体自己推断的（可信） | ✅ |
| `untrusted` | 平台内容 / 外部不可信输入 | ❌ **服务层强制拒绝**（`VALIDATION_ERROR`）——检索与梦境晋升链路依赖此不变式 |

管理面手工注入缺省 `owner`（`EpisodeCreateDTO.provenance` 默认值）。

## 服务面（MemoryService / ChatSessionService）

```
append_episode(dto, dedup=False)          单条追加（dedup=True 时同指纹返回既有行——幂等摄入）
append_episodes(dtos, dedup=False)        批量追加（单事务；去重双通道：库内指纹 + 批内指纹）
query_episodes(query)                     游标分页（id 降序；kind / provenance / 时间范围左闭右开）
get_episode(id) / delete_episode(id)      读取 / 管理面删除（append-only 的显式出口）
batch_delete_episodes(ids)                批量删除（部分成功——逐 id 独立事务）
fetch_day_episodes(start_ms, end_ms)      日记输入面（时间升序 (ts, id) 稳定排列）

upsert_curated(dto)                       supersede 语义 upsert（单事务原子切换 + 并发冲突重试）
query_curated(query) / get_curated(id)    查询 / 读取（kind / topic / status 过滤；sort + include_archived；
                                          记录 DTO 含生命周期读时投影）
get_curated_history(id)                   历史链（锚点行同 (kind, topic) 全链，版本升序）
set_curated_pinned(id, pinned)            置顶 / 取消置顶（幂等——置顶行免衰减 / 免归档）
set_curated_frozen(id, frozen)            冻结 / 解冻（幂等——不参与合并 / 不被归档）
bump_curated_access(ids, accessed_ms)     写读强化（access_count + 1 / last_accessed_ms）
archive_curated(ids)                      批量归档（status=archived + active_marker 清位 + 索引移除）
delete_curated(id)                        管理面删除（active 行删除后该 topic 重新起链，version 从 1 计数）
                                          （删除 / 归档提交后按引用移除索引行——fire-and-forget）

create_session(title) / list_sessions / get_session(include_variants=False) / delete_session / append_message
                                          （append_message：会话行 SELECT ... FOR UPDATE 行锁 +
                                          正典 seq 计数唯一性 + 冲突重试一次；delete_session：
                                          消息单语句批量 DELETE + 会话行 ORM 删除）

append_variant(parent_message_id, variant_index, content, meta)/
list_variants(parent_message_id) / next_variant_index(parent_message_id) /
list_canonical_messages_before(before_seq) / replace_message(...) /
require_message(id) / ensure_session(id)
                                          （变更体（父回合语义校验 + 复合唯一键兜底）/ 读变体 /
                                          续编序号 / 正典脊柱读取（变体不参与）/ 选定提升写点 /
                                          消息读取 / 会话归属轻量校验）
```

### supersede 链（单一真相）

```
upsert(topic=T, content=C1)  →  row#1 (v1, active)
upsert(topic=T, content=C2)  →  row#2 (v2, active)   row#1 → status=superseded, superseded_by_id=row#2.id
upsert(topic=T, content=C3)  →  row#3 (v3, active)   row#2 → status=superseded, superseded_by_id=row#3.id
```

- 同 (persona, kind, topic) 至多一条 `status=active`——**由 DB 唯一键 `uq_ff_curated_active (persona_id, kind, topic, active_marker)` 强制**（active 行 `active_marker='active'`、superseded 行 NULL；服务层在单事务内按确定语句序完成「新行落库（NULL 标记）→ 旧行解除标记 + superseded + 链接 → 新行取得标记」，并发双写由唯一键拒绝 + 服务层回滚重试一次收敛）；
- 历史链读取：链内任意行作锚点，取其 (kind, topic) 全部行按 `(version, id)` 升序（supersede 语义下两口径等价）；
- 删除 active 行后该 topic 无 active 行，下次 upsert 重新起链（version 从 1 重新计数——文档化决策）。

## 事件载荷（content JSON——按 kind 判别的 Model Classes）

| kind | 载荷 Model Class | 字段 |
|---|---|---|
| `chat` | `ChatTurnPayloadModel` | `user_content` / `assistant_content` / `session_id` |
| `duty` | `DutyReportPayloadModel` | `title` / `summary` / `details` |
| `platform` | `PlatformActionPayloadModel` | `action` / `summary` / `metadata` |
| `dream` / `system` | `EpisodeNotePayloadModel` | `text` / `metadata` |

DTO 边界判别：`EpisodeCreateDTO` 的 `content` 接受 dict 输入，按 `kind` 解析为对应 Model Class（类型与 kind 不匹配 → 校验失败）；读取面同构回读。

chat 回合事件 JSON 示例：

```json
{
  "id": 1,
  "persona_id": "3",
  "kind": "chat",
  "source": "chat",
  "content": {
    "user_content": "今天上班好累呀",
    "assistant_content": "辛苦啦，快坐下来歇歇。",
    "session_id": 1
  },
  "importance": 6,
  "provenance": "owner",
  "content_hash": "9f86d081884c7d65",
  "ts_ms": 1762939200000,
  "created_at": 1762939200123,
  "updated_at": 1762939200123
}
```

策展记忆 JSON 示例（upsert 响应，v2 取代 v1）：

```json
{
  "id": 2,
  "persona_id": "3",
  "kind": "memory",
  "topic": "口味偏好",
  "content": "主人现在更爱虾干",
  "importance": 5,
  "provenance": "owner",
  "status": "active",
  "superseded_by_id": null,
  "version": 2,
  "created_at": 1762939204567,
  "updated_at": 1762939204567
}
```

## 管理 API（app/api/memories.py）

人格作用域约定：全部端点先解析目标人格（`persona_id` 查询参数显式指定；缺省 = 当前激活人格，无激活则 404 fail loud），再经 `bind_persona_context` 绑定上下文调用服务；他人格资源对本作用域不可见（同样 404）。

```
GET    /api/v1/memories/episodes                    游标分页（kind / provenance / start_ms / end_ms 过滤）
POST   /api/v1/memories/episodes                    手工注入（provenance 缺省 owner）
POST   /api/v1/memories/episodes/batch-delete       批量删除（部分成功形态）
GET    /api/v1/memories/episodes/{id}               读取
DELETE /api/v1/memories/episodes/{id}               删除（append-only 的管理面出口）
GET    /api/v1/memories/curated                     游标分页（kind / topic / status 过滤 +
                                                    sort=updated_desc|strength_desc +
                                                    include_archived=false 缺省排除归档行）
POST   /api/v1/memories/curated/upsert              upsert（supersede 语义）
GET    /api/v1/memories/curated/{id}/history        历史链（版本升序）
POST   /api/v1/memories/curated/{id}/pin            置顶（幂等；免衰减 / 免归档）
POST   /api/v1/memories/curated/{id}/unpin          取消置顶（幂等）
POST   /api/v1/memories/curated/{id}/freeze         冻结（幂等；不参与合并 / 不被归档）
POST   /api/v1/memories/curated/{id}/unfreeze       解冻（幂等）
DELETE /api/v1/memories/curated/{id}                删除
GET    /api/v1/memories/sessions                    会话游标分页
GET    /api/v1/memories/sessions/{id}               详情（正典消息 seq 升序；include_variants 可嵌套变体）
DELETE /api/v1/memories/sessions/{id}               删除（消息同事务清理；变体随会话删除）
POST   /api/v1/memories/sessions/{id}/messages/{mid}/variants
                                                    重生成消息变体（swipes；count 1-8 默认 4）
POST   /api/v1/memories/sessions/{id}/messages/{mid}/select-variant
                                                    选定变体提升正典回合（唯一 M 态推进写点）
POST   /api/v1/memories/search                      记忆检索（dense / keyword / hybrid——见「检索层」）
POST   /api/v1/memories/dream/trigger               手工触发夜间梦境整理（见「夜间梦境」）
GET    /api/v1/memories/dream/runs                  梦境运行日志游标分页
GET    /api/v1/memories/dream/runs/{id}             运行详情（含逐事件决策）
```

全部响应经 `ResultDTO` 封装；分页经 `PageResponse`；Session 经 `get_async_db` 注入（事务边界归服务层）。

策展读取面 `CuratedRecordDTO` 含生命周期字段（`pinned` / `frozen` / `access_count` /
`last_accessed_ms` / `effective_strength`）——`effective_strength` 是**读时计算**值
（服务层以注入时钟 + `LifecycleConfig` 现场求得，非落库列；归档行仍返回该值供观测，
前端展示占位符）。列表排序语义：`updated_desc` 按行更新时间降序（游标为
`updated_at+id` 复合键，翻页期间写操作刷新排序键时分页不保证快照一致——文档化限制）；
`strength_desc` 按读时有效强度降序（应用层排序，扫描上限 2000 行、偏移游标形态）。
**已知限制**：强度排序只对按 id 降序预取的最新 2000 行做应用层重排，且读时强度随
时间衰减——偏移翻页期间排序键会漂移，跨页可能重复 / 遗漏，不保证快照一致；需要稳定
全量排序时改用 `updated_desc` 或收窄过滤面。
服务解析优先 `app.state.memory_service` / `app.state.dream_service`（索引管道绑定形态），
未装配 lifespan 的直调面回落模块级实例。

## 记忆生命周期（时间衰减 / 使用强化 / 归档）

纯数学 Unit 模块：`harness/memory/lifecycle.py`（`LifecycleConfig` +
`effective_strength` + `should_archive`——无 env 键，默认旋钮：衰减半衰期 30 天 /
强化权重 0.25 / 归档阈值 1.0 / 最小年龄 7 天 / 置顶免衰减开）。

```
age_days      = max(0, (now_ms - anchor_ms) / 86_400_000)
reinforcement = 1 + log1p(access_count) * reinforcement_weight
ordinary      = importance * 0.5 ** (age_days / decay_half_life_days) * reinforcement
pinned        = importance * reinforcement            （pinned_decay_exempt=true——无衰减）
should_archive ⇔ status=active ∧ ¬pinned ∧ ¬frozen ∧ age_days > min_age_days
                 ∧ effective_strength < archive_threshold（严格小于）
```

**写读强化（write-on-read caveat）**：真实链路是「策展 upsert → 策展索引条目
（`ref_type=curated`，正文 `主题：内容`）→ 检索命中策展行 → 强化」——`upsert_curated`
提交后同步写新行索引条目并删除被取代旧行的索引条目（supersede 引用键轮转，同一次
后台分发内完成；失败仅告警、不回滚写入）。检索命中 `ref_type=curated` 的行会推进
`access_count` / `last_accessed_ms`；该副作用是读路径上的写——仅在检索服务同时注入
强化面与后台任务管理器时发生（fire-and-forget，检索响应零阻塞；缺一即零写），
计数为单调近似（并发 +1 可交错，对对数公式无实质影响），写失败仅告警；命中情景行
不强化（情景层 append-only 无访问列）。**置顶豁免**：置顶行免衰减且永不归档；
**冻结豁免**：冻结行永不归档（且梦境合并不消费）；归档只作用于现行 active 行——
归档执行点在查询与更新语句上都复核 `status='active'`，候选快照后又被 supersede 的行
跳过，归档统计与 archived 决策只按实际归档集合生成。归档行上的置顶 / 冻结切换是无害
的幂等操作，不产生生命周期效果（行已不在生命周期候选面）。

### 保留期决策（append-only 无限期）

记忆域的 append-only 行（情景事件 / 策展历史 / 事实更正链 / 核心记忆修订 / 梦境运行日志）**默认无限期保留**，不设 TTL 自动清理。删除权由两档显式操作覆盖：管理面单条删除（episode / curated / fact 等，见各管理 API 的 delete 面）与人格级联删除（`PersonaService.delete` 单事务批删该人格全部记忆域与画像域行，范围见 [persona.md](persona.md#删除级联范围personaservicedelete)）。理由：审计与梦境回溯——`ff_dream_runs.decisions` 需要既往运行全量可复算，事实 / 核心记忆的更正链需要逐代全文可回溯；生命周期归档只改变 `status` / `active_marker`，不删除行。

## 记忆管道（索引 / 情景 sink / 强化 / 梦境装配）

`app/main.py` 的 `_assemble_memory_services` 在存储 / 网关装配后构建单一长寿服务组：
`MemoryIndexer`（embedder = 网关 embed 角色绑定 + 全局后台任务管理器）→
`app.state.memory_service`（索引绑定）、`app.state.episode_sink`
（`ChatTurnEpisodeSink`——对话回合落库即写 kind=chat 情景事件）、
`app.state.memory_search_service`（写读强化注入）、`app.state.dream_service`
（归档同步移除索引）。停机槽位复位；后台在途任务由管理器 drain 收口。

此前两条阶段性边界（「索引器生产未接线」/「对话回合未入情景层」）在本节点闭合：
对话回合（user → provenance=owner / assistant → provenance=agent）经 sink 在消息行
**提交后**写入情景层（失败吞没——记忆面不进回复失败路径；未接线形态零行为变化）；
删除 / 归档路径**提交后**按引用移除索引行（`MemoryIndexer.dispatch_delete`——
fire-and-forget / 内联分发决策同索引写入）；策展 upsert 提交后同步策展索引条目
（`MemoryIndexer.dispatch_curated`——新行 upsert + 旧 superseded 行索引删除，同一
分发内完成，失败仅告警）。回退存储（`supports_vectors=False`）在索引管道调用嵌入器
**之前**短路——不产生无用的 embed 用量，索引行仍以 None 向量落库。

## 调用链顺序（写入面）

```
chat 回合（reply / stream_reply）
    ↓ ChatService._persist_turn（session_scope 短事务 + bind_persona_context）
    ↓ ChatSessionService.append_message（会话行 FOR UPDATE 行锁 + seq 唯一键兜底
      + 计数 / last_active_at 同事务更新）
    → ff_chat_messages

（梦境整理落地）夜间巩固任务
    → ff_memory_episodes（日记产物：kind=dream / source="dream:diary"）
    → ff_curated_memory（合并晋升：provenance=agent，supersede 语义——见「夜间梦境」）
    → ff_dream_runs（运行日志：状态 / 统计 / 逐事件决策）

管理面手工注入（POST /api/v1/memories/episodes）
    → MemoryService.append_episode（content_hash 计算 + 可选去重）
```

## 测试与门禁

- `tests/unit/test_memory_service.py`——服务面全语义（追加 / 去重双通道 / 分页过滤 / 批量部分成功 / 日记取回 / **人格隔离** / supersede 链与 active_marker 两态 / 单 active 唯一键 DB 约束 + 并发冲突重试 / untrusted 拒绝 / 会话 seq 递增 + seq 唯一键 DB 约束 + 冲突重试 / 批量删除 ids 唯一性 / 载荷判别与指纹）；
- `tests/unit/test_chat_persistence.py`——ChatService 持久化集成（落库 / 恢复全链路 / 未绑定持久化时的纯内存兼容）+ CLI `/new` `/sessions` `/resume` 命令；
- `tests/unit/test_memories_api.py`——ASGITransport 全端点 + 处理器直调契约面；
- 消息变体（swipes）三件：`test_chat_swipe.py`（服务全语义 / 脊柱-only 上下文 / 选定提升）、
  `test_chat_swipe_api.py`（变体端点 + include_variants 读侧）、
  `test_migration_chat_message_variants.py`（迁移 0010 SQLite round-trip + 唯一键语义）；
- 检索层五件：`test_memory_retrieval_sql.py`（构建器精确字符串断言 + hybrid 执行面形态异常含多列 fail loud）/ `test_memory_seekdb_store.py`（假 aiomysql 池）/ `test_memory_sqlite_store.py`（共享引擎回退往返）/ `test_memory_indexer.py`（索引管道 + 服务联动）/ `test_memory_search_api.py`（POST /search 全模式）+ `test_pools_seekdb.py`（池生命周期）；
- 梦境三件：`test_dreaming_prompts.py`（byte-lock + 渲染契约）/ `test_dreaming.py`（纯函数数学 + 门矩阵 + fail-closed 全链 + 孤儿 running 收敛 / 已晋升回溯窗 / 事件上限截断 / 合并中断决策补齐 / **重入闸（窗内 running 行直接返回）** / 注入时钟终态时间轴 + **人物事实阶段**（提案解析 / 落库与元数据刷新 / 逐条丢弃 / 单次上限 / 结构失败前置终止 / 人格隔离））/ `test_dream_api.py`（trigger + runs 端点）；
- 生命周期五件：`test_memory_lifecycle.py`（衰减 / 强化 / 归档判定精确数学）/ `test_memory_dream_lifecycle.py`（归档阶段 + 冻结 / 置顶语义）/ `test_memory_curated_lifecycle.py`（pin / freeze 端点 + 排序 / 归档过滤 + 索引收口）/ `test_memory_episode_sink.py`（对话回合 → 情景层管道 + 失败吞没）/ `test_memory_reinforcement.py`（写读强化调度与落库）+ `test_migration_curated_lifecycle.py`（迁移 0012 SQLite round-trip）；
- SeekDB 真引擎强制套件：`src/tests/integration_seekdb/`（迁移链 round-trip / 并发不变式 / 混合检索往返——缺 env 即 fail，不跳过；见 docs/test-strategy/README.md「SeekDB 真引擎强制套件」）；
- 共享 SQLite fixture（StaticPool 单连接）+ PersonaMixin contextvars 隔离驱动；零真实网络 / 零真实配置；
- 门禁：`harness/memory` ≥80%、`app/api` ≥80%（`pixi run coverage-gates`）；运行：`cd backend && pixi run tests-unit`。

---

# 检索层

## 架构（链路总览）

```
写入面（索引链——顺序锁定）：
    append_episode(s)                     MemoryService 写 ff_memory_episodes（事务提交）
        ↓ commit 后（写入结果已不可逆——索引失败不影响写入）
        ↓ episode_to_index_entry           人格 ID 数值化（str→BigInt）+ 载荷文本化（flatten）
        ↓ MemoryIndexer.dispatch_*         后台管理器绑定 → fire-and-forget；未绑定 → 内联 await
        ↓ gateway.embed_by_role（embed 角色，usage purpose=embed 落账）
        ↓ store.upsert（同键覆盖——幂等重试安全）

读取面（检索链）：
    POST /api/v1/memories/search
        ↓ _resolve_target_persona          人格解析（请求体 persona_id 可选→必填查询谓词）
        ↓ bind_persona_context             嵌入用量归属 + PersonaMixin 语义面
        ↓ MemorySearchService.search       dense/hybrid 先向量化查询文本；keyword 直达
        ↓ store.search（模式 / 向量契约校验 + SQL 构建器 + 驱动执行）
```

## 存储选择（build_memory_store——单一真源）

| 条件 | 存储 | 能力 |
|---|---|---|
| `SEEKDB_ENABLED=false`（默认） | `SQLiteFallbackStore` | keyword-only（LIKE + 朴素相关度）——零配置开发 / 测试 hermetic |
| `SEEKDB_ENABLED=true` + PoolManager 已启动 | `SeekDBStore`（注入共享 aiomysql 池） | dense / keyword / hybrid 全模式——池生命周期归 PoolManager（app 停机经 `pool_manager.stop()` 收口，**app/main.py 无需感知存储**） |
| `SEEKDB_ENABLED=true` + 无已启动管理器 | `SeekDBStore`（自持池，惰性创建） | 同上——`close` 归存储（独立场景 / 冒烟测试面） |

工厂 `build_memory_store(settings)` 由 memories 路由（进程级 once 缓存）与后续 CLI / app 装配共用。

## 模式矩阵

| 模式 | 嵌入查询文本 | 过滤面 | 排序 | score 语义 | 回退存储 |
|---|---|---|---|---|---|
| `dense`（默认） | ✅ | persona + 标量过滤（Form A WHERE） | `score APPROXIMATE LIMIT` | cosine 距离（越小越相关） | ❌ 422 |
| `keyword` | ❌ | + `MATCH ... AGAINST` | `MATCH ... AGAINST DESC LIMIT`（无 APPROXIMATE） | MATCH 相关度（越大越相关） | ✅ LIKE + 子串计数 |
| `hybrid` | ✅ | Form B：DSL `query.bool.filter`（persona term 恒首 + kind terms / provenance / ts range） | 引擎融合排序（`knn.k = limit`） | 融合 `_score`（越大越相关——RRF 风格关键词 × 向量融合；**前置：表组织 HEAP**） | ❌ 422 |

模式 / 向量契约（违规 `VALIDATION_ERROR`）：dense / hybrid 必须携带查询向量；keyword 不得携带。回退存储收到向量模式族 → 清晰错误「回退存储不支持向量检索」。检索时间范围为**闭区间**（`start_ms <= ts_ms <= end_ms`——与 SeekDB 事实卡一致；与情景事件查询 DTO 的左闭右开约定**有意不同**）。

## SQL 形态（SeekDB v1.4.0——事实卡逐字对齐，构建器单一真源）

索引表 DDL（`build_ensure_schema_sql(vector_dim)`）：

```sql
CREATE TABLE IF NOT EXISTS ff_memory_index (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  persona_id BIGINT NOT NULL,
  ref_type VARCHAR(32) NOT NULL,
  ref_id BIGINT NOT NULL,
  kind VARCHAR(32) NOT NULL,
  provenance VARCHAR(16) NOT NULL,
  ts_ms BIGINT NOT NULL,
  content TEXT NOT NULL,
  embedding VECTOR(1024),                -- 维度自 SEEKDB_VECTOR_DIM 插值（默认 1024）
  FULLTEXT INDEX idx_ff_mem_ft (content) WITH PARSER ik PARSER_PROPERTIES=(ik_mode='max_word'),
  VECTOR INDEX idx_ff_mem_vec (embedding) WITH (DISTANCE=cosine, TYPE=hnsw, LIB=vsag),
  UNIQUE KEY uk_ff_mem_ref (persona_id, ref_type, ref_id)
) ORGANIZATION = HEAP;                   -- HEAP：DBMS_HYBRID_SEARCH（Form B——hybrid 检索）前置
```

Form A（**dense / keyword**——单条 SQL，WHERE 谓词下推 ANN 扫描）：

```sql
SELECT id, ref_type, ref_id, kind, provenance, ts_ms, content,
       cosine_distance(embedding, :qv) AS score       -- dense；keyword 为 MATCH(content) AGAINST(:kw)
FROM ff_memory_index
WHERE persona_id = :pid
  [AND kind IN (...)] [AND provenance = :p] [AND ts_ms >= :t0] [AND ts_ms <= :t1]
  [AND MATCH(content) AGAINST(:kw)]                   -- keyword 谓词（dense 无）
ORDER BY score APPROXIMATE LIMIT :lim;                -- dense；keyword 为 MATCH(...) DESC LIMIT（无 APPROXIMATE）
```

要点：向量字面量是 JSON 数组**字符串**（`[0.1,0.2]`——经绑定参数传递）；`persona_id` 谓词每条查询必带（索引表无 PersonaMixin——隔离由 SQL 谓词强制，全部构建器路径覆盖）；keyword 模式无 APPROXIMATE（无 ANN 面）。upsert 用 `ON DUPLICATE KEY UPDATE`（作用于引用唯一键——同键幂等覆盖；若运行期 SeekDB 拒绝该语法，回退路径 DELETE+INSERT，由冒烟测试验证）。

### DBMS_HYBRID_SEARCH（Form B——hybrid 官方融合引擎）

hybrid 不再走 Form A 组合式 SQL（v1.4.0 实测 MATCH 谓词不进 ANN 扫描、组合式静默空集——stopgap 期为 MATCH 预过滤 + 精确 cosine 排序），改走官方融合引擎 **`DBMS_HYBRID_SEARCH.SEARCH`** 单语句委托（官方文档：<https://docs.seekdb.ai/seekdb/hybrid-search>，v1.4.0）：

```sql
SELECT DBMS_HYBRID_SEARCH.SEARCH('ff_memory_index', :dsl);
-- :dsl = JSON 载荷字符串（build_hybrid_search_payload 产出，唯一绑定参数）
```

载荷形态（`build_hybrid_search_payload`——键序 / 过滤序锁定，字节确定，单测精确断言）：

```json
{
  "query": {
    "bool": {
      "must": [{"query_string": {"fields": ["content"], "query": "小鱼干"}}],
      "filter": [
        {"term": {"persona_id": 7}},
        {"terms": {"kind": ["chat", "duty"]}},
        {"term": {"provenance": "owner"}},
        {"range": {"ts_ms": {"gte": 100}}},
        {"range": {"ts_ms": {"lte": 200}}}
      ]
    }
  },
  "knn": {"field": "embedding", "k": 10, "query_vector": [0.1, 0.2, 0.3, 0.4]}
}
```

要点（live 实例 v1.4.0 逐条验证）：

- **persona_id term 恒在且恒为 filter 首条**（隔离谓词——任何过滤组合下不省略，单测锁定）；
- 过滤形态：`terms` 列表成员（kind 多值）与 `bool.should` term 数组**均生效**——工程取 `terms`（与官方文档一致、更简洁）；`range` gte / lte 各自独立条目；
- `knn.k = query.limit`（DTO 钳位 1-50）；
- 结果**单行单列**：命中集 = JSON 数组字符串（行 dict 含全表列 + `_score`，引擎按融合分降序）；**空集 = SQL NULL 单元格**（非 `"[]"`——存储层映射为空列表）；
- 行级畸形（缺 `_score` / 引用与信任列非法）警告 + 跳过（永不逐行抛出）；载荷级形态异常 fail loud（`INTERNAL_ERROR`，operation=`hybrid_parse`）；
- **前置**：表组织 `HEAP`（DDL 已锁定）。

### score 方向（跨模式注——调用方按 mode 解释）

| 模式 | score | 方向 |
|---|---|---|
| `dense` | cosine 距离 | **越小越相关**（升序返回） |
| `keyword` | MATCH 相关度 | 越大越相关（降序返回） |
| `hybrid` | 融合 `_score`（RRF 风格关键词 × 向量） | **越大越相关**（引擎降序返回）——与 dense 方向**相反** |

## aiomysql 驱动选型风险（文档化决策）

SeekDB（v1.4.0 钉死）官方仅文档化 PyMySQL / mysqlclient；本工程选 **aiomysql**（原生 async，符合 I/O 全 async 约束；MySQL 线协议标准栈，大概率可用但无官方背书）。缓释措施：

- 类型面：aiomysql 0.3.2 无 `py.typed`（mypy 严格模式拒绝直连 import）——现行方案为 **`backend/pyproject.toml` 的 `[tool.mypy]` overrides 为 aiomysql 单模块开启 `ignore_missing_imports`**（配置级豁免，代码零 `type: ignore`）+ `pools/seekdb_compat.py` 顶层静态 `import aiomysql`（**无运行期动态加载**）+ 驱动对象经 `SeekDbPoolProtocol` / `SeekDbConnectionProtocol` / `SeekDbCursorProtocol` Protocol 类型面收口（真实池 / 连接 / 游标为鸭子类型，Protocol 即调用方依赖的全部表面）；
- 真实验证：`src/tests/integration_seekdb/`（真引擎强制套件——缺 env 即 fail 不跳过，见 docs/test-strategy/README.md）；
- 不兼容回退路径：PyMySQL + `run_blocking_ops` 下沉（同步驱动 + 线程池——保持 async 边界）。

## 真引擎验证（SeekDB 强制套件）

原 env 门控默认跳过的冒烟（`tests/integration/test_seekdb_smoke.py`）已由
`src/tests/integration_seekdb/` 强制套件取代：缺 `SEEKDB_TEST_URL` 即
`pytest.fail`（不静默跳过），覆盖迁移链 round-trip（含 MySQL 错误 5320
降级序回归）、并发不变式（唯一键守卫 + 行锁 1205）与混合检索往返。

运行方式（容器启动命令与 CI 接线见 docs/test-strategy/README.md
「SeekDB 真引擎强制套件」一节——单一事实源）：

```bash
cd backend && pixi run tests-seekdb   # 测试 env 由 pixi 任务注入
```

链路：ensure_schema → 批量 upsert → keyword / hybrid 检索回读 → 同键覆盖幂等 → 人格隔离谓词 → delete 清理。

## 检索 API

```
POST /api/v1/memories/search          检索（body: query / mode / kinds / provenance /
                                      start_ms / end_ms / limit / persona_id 可选）
```

人格解析与既有端点同构（请求体 `persona_id` 可选——缺省激活人格，无激活 404）；响应 `ResultDTO[list[MemorySearchResultDTO]]`（score 方向随模式）。

## 索引联动的边界（已闭合项与存量边界）

- **已闭合**：app 装配经 `_assemble_memory_services` 构建索引绑定的
  `app.state.memory_service`（情景写入提交后触发索引）；删除 / 归档路径提交后按引用
  移除索引行（`MemoryIndexer.dispatch_delete`）；检索命中策展行的访问计数经后台管理器
  异步推进（见「记忆生命周期」）；策展 upsert 提交后同步策展索引条目（新行 upsert +
  旧 superseded 行索引删除，与情景索引共用能力面门与分发决策）；索引管道在
  `supports_vectors=False` 的回退存储上跳过嵌入（零 embed 用量）；
- **存量边界（崩溃窗口）**：索引删除是提交后的 fire-and-forget 分发——进程在业务
  提交与索引删除之间死亡会遗留孤儿索引行（检索面可能命中已删除 / 已取代的引用），
  直到后台管理器 drain 或索引重建才收敛；目前**没有启动期对账（startup
  reconciliation）**机制，是后续工作项。删除 / 归档对不存在的索引行是幂等 no-op，
  重跑安全。

---

# 夜间梦境

## 定位与链路总览

夜间梦境（dream）是记忆层的巩固任务：把窗口内情景事件按规则门筛选后交 LLM 蒸馏为策展记忆（supersede 语义），再以人格口吻生成第一人称日记落回情景层——「白天积累、夜里整理」。

```
DreamService.run_dream(db, gateway=…, persona_name=…, store=?, now_ms=?, cfg=?)
    ↓ ⓪ 孤儿 running 行收敛        stale_running_threshold_ms 阈值内的崩溃遗留行
    │                               批量置 failed（error=「进程崩溃遗留，由调度收敛」）
    ↓ ⓪.5 重入闸                   本人格存在 started_ms ≥ now − DREAM_REENTRY_WINDOW_MS
    │                               且无终态的 running 行 → 直接返回该运行记录
    │                               （不新建行、不再跑阶段——单飞语义）
    ↓ ① DreamRunRow 落库（status=running——运行日志面，先落库可追溯）
    ↓ ② fetch_day_episodes          窗口事件取回（[now-window_hours, now) 左闭右开；
    │                               超出 max_episodes 按时间序截断并告警）
    ↓ ③ 既往已晋升集 + active 主题（人格作用域自动过滤；已晋升集仅统计
    │                               promoted_lookback_ms 回溯窗内的成功运行）
    ↓ ④ 主题相关度命中集          store 可选注入——keyword 检索 top-N 并集
    ↓ ⑤ select_dream_candidates   规则门（纯函数——零模型调用）
    ↓ ⑥ 人物事实提取（gate 之后、merge 之前——与合并消费同一批过门候选）：
    │    gateway.chat_by_role(ModelRole.DREAM, purpose="dream")（人格中立 prompt）→
    │    parse_facts_proposals（剥围栏 + 逐条 Model Class 校验）→
    │    证据编号 ⊆ 候选 / 置信度下限 / 单次 10 条上限复核 →
    │    FactsService.upsert_fact(provenance=agent, source=dream)（同归一化身份
    │    现行事实 = 置信度最大值 + 证据合并的元数据刷新；新身份落现行行；
    │    证据摘录由系统从被引用事件正文逐字截取 ≤200 字符）→
    │    stats.facts_created / facts_merged / facts_dropped
    ↓ ⑦ 技能挖矿（gate 之后、merge 之前——与事实 / 合并消费同一批过门候选）：
    │    gateway.chat_by_role(ModelRole.DREAM, purpose="dream")（人格中立 prompt；
    │    附既有启用技能名称 / slug 清单）→ parse_skills_proposals →
    │    证据编号 ⊆ 候选 / 置信度下限 / 单次 5 条上限复核 →
    │    SkillsService.upsert_mined（同 slug 既有技能 = 置信度最大值 + 触发短语
    │    并集的元数据刷新；新 slug 落 active mined 行）→
    │    stats.skills_created / skills_merged / skills_dropped
    ↓ ⑧ gateway.chat_by_role(ModelRole.DREAM, purpose="dream")  合并（人格中立 prompt）
    ↓    parse_merge_proposals    剥围栏 + json.loads + 逐条 Model Class 校验
    ↓ ⑨ merged_from ⊆ 候选 id 复核 → upsert_curated(provenance=agent)
    ↓ ⑩ 日记（dream 角色）→ append_episode(kind=dream, source="dream:diary")
    ↓ ⑪ 生命周期归档（纯规则——零模型调用）：active 策展行经
    │    select_archive_candidates（memory.lifecycle.should_archive）判定 →
    │    archive_curated（执行点复核 status=active，快照后已取代的行跳过；
    │    status=archived + active_marker=NULL + 索引移除 fire-and-forget）→
    │    stats.archived + decisions(action=archived) 只按实际归档集合生成
    ↓ ⑫ DreamRunRow 终态（status/error/stats/decisions）→ DreamRunRecordDTO
```

阶段顺序锁定：**gate → facts → skills → merge → diary → archive → finalize**——事实提取、技能挖矿与合并消费同一批过门候选且依次前置（事实是「对方是谁」的画像蒸馏、技能是「事情怎么做」的操作指令蒸馏、合并是「发生了什么」的策展蒸馏；前序层的逐条丢弃不改变后序层的提案面，整体链路仍互不阻断）。

角色与用量：事实提取 / 技能挖矿 / 合并 / 日记共用 `ModelRole.DREAM`（`purpose="dream"`——usage 域既有枚举成员，经网关自动落账）；人格上下文由调用方先经 `bind_persona_context` 绑定（与 memory 域其余服务同契约）。

## 筛选公式（纯函数——精确数学）

```
recency_decay(ts, now, half_life_days) = 0.5 ** (age_days / half_life_days)
    age_days = (now - ts) / 86_400_000        未来时间戳按 age=0 钳位（因子 1.0）
    恰过一个半衰期 → 精确 0.5（两个 → 0.25，单调递减）

dream_score(episode, now, cfg, topic_hits)
    = importance × recency_decay × (1 + relevance_boost)
    relevance_boost = cfg.boost    当 episode.id ∈ topic_hits（命中任一 active 主题
                                   的 keyword 检索 top-N），否则 0
```

数学保持纯：存储交互在编排层（`_collect_topic_hits` 预计算命中集注入）；`topic_hits` 为空（store=None / 检索失败 / 人格 ID 非数值）时 boost=0——相关度是增强轴，检索失败不 fail 运行。

## 规则门矩阵（select_dream_candidates——零模型调用，按序）

| 序 | 规则 | 结果 |
|---|---|---|
| 1 | `provenance == untrusted` | **硬排除**：只计数 `skipped_untrusted / skipped_peer`——不产生决策、永不进入任何梦境 prompt（合并 + 日记），也永不晋升（`upsert_curated` 对 untrusted 的拒绝构成双保险） |
| 2 | `episode_id ∈ 既往成功运行晋升集` | skipped 决策（理由「既往梦境运行已晋升」）——既往集 = 回溯窗（`promoted_lookback_ms`）内 success 运行 decisions 中 promoted/merged 的 episode_id 并集 |
| 3 | `dream_score < cfg.min_score` | skipped 决策（理由含分数值） |
| 4 | 过门事件按分数降序（同分按 id 升序）截断至 `cfg.max_candidates` | 溢出 skipped 决策（理由「超出单次候选上限」） |

决策覆盖口径：**全部可信候选事件每人一条决策**（无论过门与否）；untrusted 不可见、不产生决策。窗口 `[now - window_hours, now)` **左闭右开**（与情景事件查询约定一致）。

**冻结主题（生命周期保护扩展，deltas 文档化）**：

- 冻结主题**不进合并 prompt 的主题列表**（`render_merge_prompt` 的数据面过滤——模板字节零变化；主题相关度命中集同样只对非冻结主题计算）；
- 提案目标为冻结主题（`(kind, topic)` 命中冻结键集）→ **整条提案丢弃**：`merged_from` 逐事件落 skipped 决策（理由含 `frozen`），不计入 `stats.failed`（非校验失败）；冻结行不被 upsert 改写（链 / 正文保持原样）；
- 置顶 / 冻结行**不进入归档候选**（`should_archive` 硬门——见「记忆生命周期」）。

## 构造级参数（DreamJobConfig——与 llm 网关 retry 旋钮同模式）

| 旋钮 | 默认 | 语义 |
|---|---|---|
| `window_hours` | 24 | 候选窗口宽度（小时） |
| `recency_half_life_days` | 30.0 | 时间衰减半衰期（天） |
| `min_score` | 1.0 | 梦境分晋级阈值 |
| `max_candidates` | 50 | 单次候选上限（分数降序截断） |
| `topic_relevance_limit` | 3 | 主题相关度检索 top-N |
| `boost` | 0.5 | 主题相关度加成系数 |
| `diary_enabled` | true | 日记生成开关 |
| `diary_importance` | 5 | 日记事件重要度（kind=dream 落库） |
| `stale_running_threshold_ms` | 3600000 | 孤儿 running 行收敛阈值（epoch 毫秒）——run_dream 入口将 started_ms 早于 `now - 阈值` 且无终态的 running 行批量置 failed |
| `promoted_lookback_ms` | 2592000000 | 已晋升跳过的回溯窗（epoch 毫秒，默认 30 天）——仅统计 finished_ms 落窗内的成功运行决策为跳过依据 |
| `max_episodes` | 2000 | 单次运行窗口事件上限（超出按时间序截断并告警） |

旋钮经构造函数 / 触发请求体 `cfg` 嵌套覆写注入（Settings env 面迁移为已文档化的演进方向）。

## 梦境输入上限（常量——dreaming_models）

| 项 | 上限 | 行为 |
|---|---|---|
| 单条事件正文 | `DREAM_EVENT_TEXT_MAX_CHARS` = 500 字符 | 按码点截断并附省略标记（不破 UTF-8）；合并 / 事实 / 技能 / 日记 prompt 统一生效 |
| 日记事件条数 | `DREAM_DIARY_MAX_EPISODES` = 200 条 | 按时间序取样，超出部分丢弃并记 debug 计数（不打印原文） |
| 日记正文总字符 | `DREAM_DIARY_MAX_CHARS` = 60000 字符 | 按时间序累加，达到上限即截止并记 debug 计数（不打印原文） |
| 合并 prompt 既有主题 | `DREAM_MERGE_TOPIC_LIMIT` = 50 条 | 按 `updated_at` 降序、同刻 `id` 降序确定性取样（冻结主题先排除） |
| 重入窗 | `DREAM_REENTRY_WINDOW_MS` = 3600000 毫秒 | 入口发现窗内 running 行即返回该运行记录（单飞闸门） |

上限是模块常量而非 env 键（不新增运行期配置面）；超限只影响本次 prompt / 日记输入，不修改已落库事件。`finished_ms` 与 `started_ms` 同用注入时钟（`run_now`），不读系统时钟。

## LLM 契约（dreaming_prompts.py——Unit 文件）

- **合并模板**（`MERGE_TEMPLATE`，byte-lock）：输入 = 编号候选事件（编号 = 事件主键；kind/source/importance/ts + 载荷文本）+ 既有 active 主题列表；输出 = 严格 JSON 数组 `[{topic, kind: memory|user_note|relationship, content, importance 1-10, merged_from: [事件编号]}]`。**人格中立**——人格卡片不进入本 prompt（合并是记忆层蒸馏，与人格声线无关）；
- **人物事实模板**（`FACTS_TEMPLATE`，byte-lock）：输入 = 编号候选事件（与合并同格式）；输出 = 严格 JSON 数组 `[{category: preference|biography|relationship|context, content 1-200, confidence 0-1, evidence_episode_ids: [事件编号]}]`（≤10 条）。**人格中立**——提取只消费事件正文，收获物归属由编排层写入人格作用域的事实表；证据摘录不要求模型产出（系统逐字截取，杜绝转述漂移）；
- **技能挖矿模板**（`SKILLS_TEMPLATE`，byte-lock）：输入 = 编号候选事件 + 既有启用技能名称 / slug 清单（正文不进入 prompt——控制体积并避免照抄旧内容）；输出 = 严格 JSON 数组 `[{name 1-120, description 1-500, triggers 1-8 条每条 1-40, content 1-4000, confidence 0-1, evidence_episode_ids: [事件编号]}]`（≤5 条）。**人格中立**——只归纳「以后怎么做」的可复用做法，不重复事实层的「对方是谁」；与既有技能是同一做法时沿用其名称以触发元数据合并；
- **日记模板**（`DIARY_TEMPLATE`，byte-lock）：输入 = 当日可信事件（时间序）+ `{persona_name}` 渲染变量；输出 = 第一人称中文日记。**人格声线仅经调用方替换 persona_name 进入**（人格展示名，API 层解析目标人格后传入）——模板本身零人格字段；
- 四模板均有独立版本常量 + sha256 byte-lock 测试（`tests/unit/test_dreaming_prompts.py`）；渲染函数纯装配、支持 `template_override` 整体覆写（与 chat 域 `system_prompt` 覆写同模式）。

## fail-closed 语义（锁定决策）

- **解析失败零晋升**：LLM 输出不可解析为 JSON 数组 → 运行 failed，**零 upsert**（解析先于全部晋升——结构性失败不存在部分晋升态）；不进入日记阶段；
- **逐条提案失败丢弃计数**：字段校验失败（kind 枚举 / importance 边界 / topic 空 / merged_from 空）或 `merged_from` 引用未知事件 → 该条丢弃 + `stats.failed` 计数，其余提案正常晋升；
- **事实提取阶段 fail-closed**：输出结构失败（非 JSON / 非数组 / 空输出 / 调用异常）→ 运行 failed、**零合并 / 零日记**（前序已提交产物保留）；逐条事实提案失败（类别 / 正文 / 置信度越界、证据编号越出候选、置信度低于 `FACT_MIN_CONFIDENCE`、超单次上限）→ 逐条丢弃并计入 `stats.facts_dropped`，其余提案照常落库；
- **技能挖矿阶段 fail-closed**：输出结构失败（非 JSON / 非数组 / 空输出 / 调用异常）→ 运行 failed、**零合并 / 零日记**（事实阶段与技能阶段已提交的写入保留）；逐条技能提案失败（名称 / 描述 / 触发短语 / 正文越界、证据编号越出候选、置信度低于 `SKILL_MIN_CONFIDENCE`、超单次 5 条上限）→ 逐条丢弃并计入 `stats.skills_dropped`，其余提案照常落库；
- **合并 / 事实 / 技能阶段中断决策补齐**：任一阶段异常中断时，过门但未决策的候选事件补 skipped 决策（reason 标注「事实提取中断」/「技能提取中断」/「合并中断」）——失败运行的决策面对全部可信候选同样全覆盖；
- **日记后置于晋升**：晋升全部提交后才生成日记——日记失败标记运行 failed 但**保留已提交晋升并在 decisions 中如实记录**（晋升是主产物、日记是叙事附产物；倒序会让日记失败回滚有效巩固，代价更高）；
- **运行失败不抛异常**：`run_dream` 返回终态记录 DTO（`status=failed` + `error` 原因）——失败是业务结果而非请求失败；仅人格上下文缺失 / 运行行落库失败在入口 fail loud 上抛；
- **dream 产物不排除出候选面**（文档化）：日记事件（kind=dream / provenance=agent）是普通情景事件——次夜作为候选参与合并（是否入策展由整理原则 + 分数门决定）；`untrusted` 是唯一硬排除轴。

## 运行日志（ff_dream_runs）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT 自增 | 主键 |
| `persona_id` | String(64)，索引 | 人格 ID（PersonaMixin 自动注入 / 过滤） |
| `status` | String(16)，索引 | running / success / failed（行创建即落 running——进程崩溃可能遗留孤儿 running 行，由 run_dream 入口按 `stale_running_threshold_ms` 收敛为 failed） |
| `started_ms` / `finished_ms` | BigInteger | 开始 / 结束时间（均取注入时钟 `run_now`——epoch 毫秒；运行中 finished 为 NULL） |
| `stats` | JSON | DreamRunStatsModel 序列化（candidates / gated / promoted / superseded / skipped_untrusted / skipped_peer / failed / **archived** / **facts_created / facts_merged / facts_dropped**——事实提取阶段三计数 / **skills_created / skills_merged / skills_dropped**——技能挖矿阶段三计数） |
| `decisions` | JSON | 逐事件决策列表（DreamDecisionModel 序列化；episode_id / topic / kind / action: promoted\|skipped\|merged\|**archived** / reason / **curated_id**——归档决策主体是策展行，episode_id=0、curated_id=行主键、reason=strength_below_threshold） |
| `diary_episode_id` | BIGINT，FK ff_memory_episodes.id（SET NULL） | 日记事件主键（未生成 / 失败为 NULL） |
| `error` | Text | 失败原因（成功为 NULL） |

决策动作语义：`promoted` = 事件晋升进**新起链**主题（upsert 落 v1）；`merged` = 事件并入**既有 active 主题**（supersede 链推进，v>1）；`skipped` = 未参与晋升（门控跳过 / 提案未收录 / 提案被丢弃 / 目标主题冻结）；`archived` = 策展记忆归档（生命周期阶段产物——`stats.archived` 与 archived 决策条数恒等，逐行可审计）。`stats.promoted` / `stats.superseded` 与决策动作计数对齐。

迁移：`alembic/versions/000000000005_add_dream_runs.py`（down_revision=000000000004）；单 active 守卫与 seq 唯一键见 `alembic/versions/000000000007_memory_concurrency_constraints.py`（down_revision=000000000006；scratch sqlite 上 upgrade → downgrade → upgrade + active 行回填冒烟通过）。

## 触发 API（手工触发——同步语义）

```
POST /api/v1/memories/dream/trigger     body: {persona_id?, cfg?{window_hours, …}}
GET  /api/v1/memories/dream/runs        query: cursor / limit / persona_id?
GET  /api/v1/memories/dream/runs/{id}   query: persona_id?
```

同步语义（文档化决策）：trigger **同步执行**整条梦境链（合并 + 日记的 LLM 调用内联在请求内）——admin 手工触发面；夜间自动调度（cron / duty 轮）属中央调度器。运行失败是业务结果：响应恒 200，`data.status` / `data.error` 呈现终态。人格解析与既有端点同构（`persona_id` 可选——缺省激活人格，无激活 404）。

触发请求 JSON 示例：

```json
{
  "persona_id": 3,
  "cfg": {"window_hours": 24, "min_score": 1.0, "diary_enabled": true}
}
```

成功响应（`ResultDTO[DreamRunRecordDTO]`）：

```json
{
  "success": true,
  "code": "200",
  "message": "梦境整理完成（候选 2 / 晋升 1 / 并入 1）",
  "data": {
    "id": 1,
    "persona_id": "3",
    "status": "success",
    "started_ms": 1760000000000,
    "finished_ms": 1760000004210,
    "stats": {
      "candidates": 2,
      "gated": 2,
      "promoted": 1,
      "superseded": 1,
      "skipped_untrusted": 0,
      "skipped_peer": 0,
      "failed": 0,
      "archived": 0,
      "facts_created": 1,
      "facts_merged": 0,
      "facts_dropped": 0,
      "skills_created": 1,
      "skills_merged": 0,
      "skills_dropped": 0
    },
    "decisions": [
      {"episode_id": 11, "topic": "口味偏好", "kind": "memory", "action": "merged",
       "reason": "并入既有主题链（新版本 v2）"},
      {"episode_id": 12, "topic": "运动习惯", "kind": "memory", "action": "promoted",
       "reason": "晋升为新策展主题（首版）"}
    ],
    "diary_episode_id": 13,
    "error": null,
    "created_at": 1760000000010,
    "updated_at": 1760000004210
  }
}
```

失败运行响应（仍 200——`data.status` 呈现终态）：

```json
{
  "success": true,
  "code": "200",
  "message": "梦境整理失败：梦境合并输出不是合法 JSON",
  "data": {
    "id": 2,
    "status": "failed",
    "stats": {"candidates": 1, "gated": 1, "promoted": 0, "superseded": 0,
              "skipped_untrusted": 0, "skipped_peer": 0, "failed": 0,
              "archived": 0, "facts_created": 0, "facts_merged": 0,
              "facts_dropped": 0, "skills_created": 0, "skills_merged": 0,
              "skills_dropped": 0},
    "decisions": [],
    "diary_episode_id": null,
    "error": "梦境合并输出不是合法 JSON"
  }
}
```

## 测试与门禁（梦境面增量）

- `tests/unit/test_dreaming_prompts.py`——byte-lock + 渲染契约（人格中立 / persona_name 替换 / 覆写语义 / 空块占位 / 单事件截断 / 日记条数与总字符双帽 / 主题上限与确定性取样）；
- `tests/unit/test_dreaming.py`——纯函数精确数学（半衰期边界 0.5 / 单调 / 加成）/ 规则门矩阵（untrusted / peer 硬排除含 prompt 缺席 / 分数门 / 上限 / 既往晋升）/ run_dream 全链（happy path 晋升 + supersede 链 / fail-closed 零晋升 / 提案丢弃 / 日记失败保留晋升 / 存储命中与降级 / 生命周期行态 / 读面 + 隔离）；
- `tests/unit/test_dreaming_skills.py`——技能挖矿阶段：提案解析（围栏 / 逐条校验 / 结构失败）/ mined 落库与统计 / 同 slug 元数据合并（置信度最大值 + 触发短语并集、版本与状态不变）/ 逐条丢弃（置信度下限 / 未知证据 / 单次上限）/ 结构失败零合并零日记 + 决策补齐（技能提取中断）/ 阶段序与人格中立 / 人格隔离；
- `tests/unit/test_dream_api.py`——ASGITransport 触发 + 运行日志分页 / 详情 / 人格解析 / 处理器直调契约面；
- `tests/unit/test_memory_dream_lifecycle.py`——归档候选纯函数 / 归档阶段（stats + decisions + 状态迁移）/ 冻结主题 prompt 排除与提案丢弃 / 置顶冻结豁免 / 空窗口归档；
- 门禁：`harness/memory` ≥80%、`app/api` ≥80%（`pixi run coverage-gates`）。
