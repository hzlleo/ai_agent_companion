# memory — 三层记忆域（P1 chunk 1 交付）

`backend/src/packages/harness/memory/` 承载陪伴智能体的三层记忆：情景层（append-only 事件日志）、策展层（supersede 语义蒸馏记忆）、工作层（chat 会话回合持久化）。本 chunk 交付存储 + 管理 API；检索 / 向量召回与梦境整理（写入策展层的蒸馏任务）属 P1 chunk 2/3。

## 三层模型

| 层 | 表 | 语义 | 写入方 |
|---|---|---|---|
| episodic（情景） | `ff_memory_episodes` | 发生过的每件事一行；**append-only**（只增不改，删除是管理面显式动作） | chat 回合 / duty 报告 / dream 产物 / platform 动作 / 管理面手工注入 |
| curated（策展） | `ff_curated_memory` | 蒸馏后的结构化记忆；同 (persona, kind, topic) **至多一条 active**（supersede 取代语义，历史链可回溯） | 管理面 upsert（本 chunk）/ 梦境整理任务（chunk 3） |
| working（工作） | `ff_chat_sessions` + `ff_chat_messages` | chat 会话与回合（会话内上下文组装原料） | `ChatService` 持久化绑定（见 chat.md） |

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（EpisodeKind / MemoryProvenance / CuratedKind / CuratedStatus）+ 事件载荷 Model Classes + 内容指纹 | `parse_episode_payload` / `compute_content_hash` |
| `orm.py` | 四张表（PersonaMixin 作用域切分见下） | `MemoryEpisodeRow` / `CuratedMemoryRow` / `ChatSessionRow` / `ChatMessageRow` |
| `dtos.py` | 事件 / 策展 / 会话的创建 / 记录 / 查询 / 批量删除 DTO | `EpisodeCreateDTO` / `CuratedUpsertDTO` / `ChatSessionDetailDTO` 等 |
| `service.py` | `MemoryService`（情景 + 策展）+ `ChatSessionService`（会话） | `MemoryService` / `ChatSessionService` |

依赖方向：memory → core（dtos / errors / time_utils）+ database（Base / Mixin）+ llm（`ChatMessageRole`——消息角色值域复用）；不依赖 persona / chat 域（人格上下文经 `bind_persona_context` 由调用方绑定）。

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
| `status` | String(16)，索引 | active / superseded |
| `superseded_by_id` | BIGINT，自引用 FK（SET NULL） | 取代者行主键（active 行为 NULL） |
| `version` | Integer | 链内版本号（首版 1，supersede 递增） |

### ff_chat_sessions / ff_chat_messages（工作层）

`ff_chat_sessions`：`persona_id`（PersonaMixin）/ `title` / `message_count`（下一消息 seq）/ `last_active_at`（索引）。
`ff_chat_messages`：`session_id`（FK ff_chat_sessions.id，级联删除，索引）/ `seq`（会话内从 1 递增）/ `role`（user/assistant/system）/ `content` / `ts_ms`。

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

upsert_curated(dto)                       supersede 语义 upsert（单事务原子切换）
query_curated(query) / get_curated(id)    查询 / 读取（kind / topic / status 过滤）
get_curated_history(id)                   历史链（锚点行同 (kind, topic) 全链，版本升序）
delete_curated(id)                        管理面删除（active 行删除后该 topic 重新起链，version 从 1 计数）

create_session(title) / list_sessions / get_session / delete_session / append_message
```

### supersede 链（单一真相）

```
upsert(topic=T, content=C1)  →  row#1 (v1, active)
upsert(topic=T, content=C2)  →  row#2 (v2, active)   row#1 → status=superseded, superseded_by_id=row#2.id
upsert(topic=T, content=C3)  →  row#3 (v3, active)   row#2 → status=superseded, superseded_by_id=row#3.id
```

- 同 (persona, kind, topic) 至多一条 `status=active`——由服务层单事务保证（新行落库 → 旧行标记 + 链接 → commit）；
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
GET    /api/v1/memories/curated                     游标分页（kind / topic / status 过滤）
POST   /api/v1/memories/curated/upsert              upsert（supersede 语义）
GET    /api/v1/memories/curated/{id}/history        历史链（版本升序）
DELETE /api/v1/memories/curated/{id}                删除
GET    /api/v1/memories/sessions                    会话游标分页
GET    /api/v1/memories/sessions/{id}               详情（含全量消息 seq 升序）
DELETE /api/v1/memories/sessions/{id}               删除（消息同事务清理）
```

全部响应经 `ResultDTO` 封装；分页经 `PageResponse`；Session 经 `get_async_db` 注入（事务边界归服务层）。

## 调用链顺序（写入面）

```
chat 回合（reply / stream_reply）
    ↓ ChatService._persist_turn（session_scope 短事务 + bind_persona_context）
    ↓ ChatSessionService.append_message（seq 递增 + 会话行计数 / last_active_at 同事务更新）
    → ff_chat_messages

（chunk 2/3 落地后）回合蒸馏 / 梦境整理
    → ff_memory_episodes（append-only 事件日志）
    → ff_curated_memory（supersede 语义蒸馏产物）

管理面手工注入（POST /api/v1/memories/episodes）
    → MemoryService.append_episode（content_hash 计算 + 可选去重）
```

## 测试与门禁

- `tests/unit/test_memory_service.py`——服务面全语义（追加 / 去重双通道 / 分页过滤 / 批量部分成功 / 日记取回 / **人格隔离** / supersede 链 / untrusted 拒绝 / 会话 seq 递增 / 载荷判别与指纹）；
- `tests/unit/test_chat_persistence.py`——ChatService 持久化集成（落库 / 恢复全链路 / 持久化关闭兼容 P0）+ CLI `/new` `/sessions` `/resume` 命令；
- `tests/unit/test_memories_api.py`——ASGITransport 全端点 + 处理器直调契约面；
- 共享 SQLite fixture（StaticPool 单连接）+ PersonaMixin contextvars 隔离驱动；零真实网络 / 零真实配置；
- 门禁：`harness/memory` ≥80%、`app/api` ≥80%（`pixi run coverage-gates`）；运行：`cd backend && pixi run tests-unit`。
