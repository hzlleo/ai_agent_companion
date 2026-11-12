# persona — 多人格注册表（P0-5 交付）

`backend/src/packages/harness/persona/` 承载多人格注册表：chara card v2 人格卡片（L 层正典）+ M/S 心理状态层 + 事务性单激活切换。表名 `ff_personas` / `ff_persona_states`（本工程 `ff_` 前缀约定）。首个 app 层交付面（`app/api/personas.py` 管理 API + `app/middleware/error_handler.py` 共享异常处理器）随本域落地。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（可用性状态 / L-M-S 状态层） | `PersonaStatus` / `PersonaStateLayer` |
| `chara_card.py` | chara card v2 规格模型（卡片 + 世界书，Unit 文件） | `CharaCardModel` / `CharaBookModel` / `CharaBookEntryModel` / `CharaBookPosition` |
| `orm.py` | 注册表 ORM（`ff_personas` + `ff_persona_states`） | `PersonaRow` / `PersonaStateRow` |
| `dtos.py` | 创建 / 更新 / 复制 / 记录 / 卡片 / 状态 / 查询 / 批量 DTO（Unit 文件） | `PersonaCreateDTO` / `PersonaUpdateDTO` / `PersonaCopyDTO` / `PersonaRecordDTO` / `PersonaCardDTO` / `PersonaStateUpsertDTO` / `PersonaStateDTO` / `PersonaQueryDTO` / `PersonaBatchDelete*` |
| `service.py` | 注册 CRUD / 事务性单激活切换 / 状态层读写 / 批量删除 + `bind_persona_context` 运行期绑定 | `PersonaService` / `bind_persona_context` |

依赖方向：persona → core（dtos 分页 / errors / context）+ database（Base Mixins / get_async_db）；app 层（api/personas + middleware/error_handler）→ persona + core + database。**两张表均非 PersonaMixin**（见下）。

## L / M / S 三层心理状态模型

| 层 | 含义 | 载体 | 变更语义 |
|---|---|---|---|
| **L**（long-term） | 长期身份正典：性格 / 场景 / 开场白 / 世界书 | `ff_personas.chara_card`（chara card v2 JSON 文档） | 会话期**不可变**；仅管理面经 PUT 端点整体替换或补丁合并 |
| **M**（mid-term） | 中期心情 / 关系评价（跨会话演化） | `ff_persona_states` 行（`layer="M"`，state JSON） | 会话运行期 upsert（整体替换式） |
| **S**（short-term） | 短期会话状态：话题焦点 / 情绪强度 / 最近意图 | `ff_persona_states` 行（`layer="S"`，state JSON） | 会话内高频 upsert；会话外可残留（下会话开场参考） |

L 层不入状态表的理由：身份正典与可变状态的生命周期不同（卡片随人格管理面演进；M/S 随对话演化），且卡片需要作为整体文档校验与传输（DTO 边界一次成型）。

## 关键设计决策

1. **注册表是全局管理面数据（非 PersonaMixin）**：admin 需在无人格上下文下跨人格列出 / 切换 / 复制 / 删除人格；PersonaMixin 的请求上下文自动过滤（缺上下文即 fail loud）与该语义直接冲突（人格行自身的 persona_id 无从定义）。P1 记忆表才是 PersonaMixin 消费面（per-persona 逻辑隔离）。
2. **status（可用性）与 is_active（单激活位）正交**：`status=active/disabled` 是管理面可用性开关；`is_active` 是「当前唯一激活人格」绑定位——全表至多一行 `True`。激活要求 `status=active`；status 切 disabled 时自动清激活位（停用即下线，杜绝矛盾态）。
3. **事务性单激活切换**：`activate` 在单事务内「全表清激活位 → 激活目标」——激活 B 必然去激活 A；任一步失败整体回滚。`deactivate` 允许归零（系统态），幂等。
4. **删除守卫（本工程锁定语义）**：删除**当前激活人格被禁止**（`CONFLICT`，调用方须先 `deactivate` 显式两步）；删除非激活人格时同事务清理其 M/S 状态行（SQLite 测试方言默认不启用 FK 强制，服务层显式删除保证跨方言一致；DB 级 `ON DELETE CASCADE` 仅作直删 SQL 兜底）。
5. **无乐观锁版本列**：本工程 ORM 无版本计数约定（database/base.py 仅 BigIntIdMixin / TimestampMixin / PersonaMixin，usage 域表亦无）——遵循既有约定不引入 `version` 列；注册表写路径低频（管理面操作），冲突面由 name 唯一约束 + 单事务切换兜底。
6. **运行期绑定 `bind_persona_context(persona_id)`**：注册表主键为 BigInt，而上下文轴 `persona_id_var` / PersonaMixin 均为字符串——绑定 helper 接受 `int | str` 并经 `str()` 归一（chat 会话等运行期入口消费，见 `core.context.bind_context`）。

## 链路顺序

**创建 → 激活 → 会话绑定**：

```
管理面 POST /api/v1/personas（PersonaCreateDTO：name + card）
    ↓ 唯一名校验（CONFLICT fail loud）→ PersonaRow（status 缺省 disabled，is_active=False）
    ↓ 管理面 PUT /{id}（status=active 启用）
    ↓ 管理面 POST /{id}/activate（单事务：全表清位 → 激活目标）
    ↓ 运行期（P0-6 chat 会话）async with bind_persona_context(persona.id):
    ↓     PersonaMixin 表自动按 str(persona.id) 隔离；llm 网关用量记账携带 persona_id
```

**复制链**：

```
POST /{id}/copy（PersonaCopyDTO：new_name + include_states）
    ↓ 源行读取（NOT_FOUND fail loud）→ 新名唯一校验（CONFLICT）
    ↓ 新行：卡片深拷贝 + status=disabled + is_active=False（启用与激活均为显式后续动作）
    ↓ include_states=True 时：逐层深拷贝 M/S 状态行（源状态保留——拷贝非移动）
    ↓ 单事务提交（新行 + 状态行原子落库）
```

**状态层 upsert**：

```
PUT /{id}/states/{layer}（layer ∈ {M, S}；PersonaStateUpsertDTO：state JSON 文档）
    ↓ 人格存在守卫（NOT_FOUND）→ (persona_id, layer) 行存在则整体替换 state，否则插入
    ↓ 单事务提交；GET /{id}/states 返回已写层（layer 升序——M 在前 S 在后）
```

## 表结构

### ff_personas（注册表）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `name` | VARCHAR(128) | 非空；**全局唯一**（`uq_ff_personas_name`） | 注册名（标识与引用键） |
| `display_name` | VARCHAR(128) | 非空 | 展示名（缺省回落 card.name） |
| `status` | VARCHAR(16) | 非空 | 可用性状态（active / disabled） |
| `is_active` | BOOLEAN | 非空 | 单激活绑定位（全表至多一行 True） |
| `chara_card` | JSON | 非空 | 人格卡片（chara card v2 文档，L 层正典） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

### ff_persona_states（M/S 状态层）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | BIGINT | 非空；FK `ff_personas.id`（ON DELETE CASCADE）；索引 | 所属人格 |
| `layer` | VARCHAR(8) | 非空；`(persona_id, layer)` 唯一 | 状态层（M=中期；S=短期） |
| `state` | JSON | 非空 | 状态 JSON 文档（整体替换式更新） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

迁移：`backend/alembic/versions/000000000003_add_personas.py`（Revision 000000000003，Revises 000000000002——两表 + 索引 + 唯一约束，升级 / 降级均经 SQLite 冒烟验证）。

## 值域

- `PersonaStatus`：`active`（可用——允许激活）/ `disabled`（停用——激活守卫拒绝，停用即下线）；
- `PersonaStateLayer`：`M`（中期心情 / 关系）/ `S`（短期会话状态）——`L` 不入状态表（见三层模型）；
- `CharaBookPosition`：`before_char` / `after_char`（v2 字符串形态）。

## chara card v2 字段表（CharaCardModel）

| 字段 | 类型 | 校验 | 说明 |
|---|---|---|---|
| `name` | str | **非空**（1-128） | 人格称呼名 |
| `description` | str | 缺省 `""` | 人格简介（外观 / 背景 / 身份） |
| `personality` | str | 缺省 `""` | 性格描述 |
| `scenario` | str | 缺省 `""` | 场景设定 |
| `first_mes` | str | 缺省 `""` | 开场白 |
| `mes_example` | str | 缺省 `""` | 对话示例（few-shot 语态锚定） |
| `system_prompt` | str | 缺省 `""` | 系统提示覆写 |
| `post_history_instructions` | str | 缺省 `""` | 历史后指令 |
| `character_book` | CharaBookModel \| None | 缺省 `null` | 世界书（检索增强条目集） |

**世界书（CharaBookModel）**：`name` / `description`（str）、`scan_depth`（int ≥ 1，最近 N 条消息内检索）、`token_budget`（int ≥ 1，超预算按 insertion_order 截断）、`recursive_scanning`（bool）、`entries`（条目列表）。

**世界书条目（CharaBookEntryModel）**：

| 字段 | 类型 | 校验 / 缺省 | 说明 |
|---|---|---|---|
| `keys` | list[str] | 缺省 `[]` | 主触发关键词（任一命中即插入） |
| `secondary_keys` | list[str] | 缺省 `[]`；**仅 `selective=True` 可携带** | 副触发关键词（选择性触发收敛） |
| `content` | str | 缺省 `""` | 条目正文 |
| `insertion_order` | int | **界 0-9999**，缺省 100 | 插入顺序（值越大越靠后） |
| `constant` | bool | 缺省 False | 恒插入（免触发） |
| `case_sensitive` | bool | 缺省 False | 关键词匹配区分大小写 |
| `selective` | bool | 缺省 False | 选择性触发（主键 + 副键双命中） |
| `position` | CharaBookPosition | 缺省 `after_char` | 插入位置 |

**卡片 JSON 示例**：

```json
{
  "name": "大肥鱼",
  "description": "一条会说话的胖鱼，住在主人书房的鱼缸里",
  "personality": "温柔、爱操心、有点啰嗦，但很可靠",
  "scenario": "主人每天下班后会和它聊聊今天的事",
  "first_mes": "你回来啦！今天过得怎么样？",
  "mes_example": "<START>\n{{user}}: 我累了\n{{char}}: 那就歇会儿，我陪你。",
  "system_prompt": "",
  "post_history_instructions": "保持回复简短，两三句以内。",
  "character_book": {
    "name": "feiyu-world",
    "description": "大肥鱼的世界观设定",
    "scan_depth": 4,
    "token_budget": 800,
    "recursive_scanning": false,
    "entries": [
      {
        "keys": ["鱼缸", "书房"],
        "secondary_keys": [],
        "content": "大肥鱼住在主人书房的圆形鱼缸里。",
        "insertion_order": 10,
        "constant": false,
        "case_sensitive": false,
        "selective": false,
        "position": "after_char"
      },
      {
        "keys": ["晚饭"],
        "secondary_keys": ["计划"],
        "content": "大肥鱼很关心主人有没有好好吃饭。",
        "insertion_order": 20,
        "constant": false,
        "case_sensitive": true,
        "selective": true,
        "position": "before_char"
      }
    ]
  }
}
```

**M / S 状态文档示例**（`PUT /api/v1/personas/{id}/states/M`）：

```json
{
  "state": {
    "mood": "期待",
    "relationship": 80,
    "last_summary": "主人这周工作很忙，聊了三次加班"
  }
}
```

```json
{
  "state": {
    "topic": "周末计划",
    "energy": 0.6,
    "recent_intent": "想约主人出门散步"
  }
}
```

## PersonaService API

```python
service = PersonaService()                                        # 无状态；Session 逐调用注入
record = await service.create(session, dto)                       # 创建（唯一名校验；不激活）
record = await service.get(session, persona_id)                   # 读取（NOT_FOUND fail loud）
page = await service.query(session, PersonaQueryDTO(...))         # 游标分页（id 降序 + status 过滤）
record = await service.update(session, persona_id, dto)           # 部分更新（重命名 / 状态 / 卡片补丁）
record = await service.copy(session, persona_id, dto)             # 复制（可选深拷贝 M/S；恒 disabled）
await service.delete(session, persona_id)                         # 删除（激活守卫：CONFLICT）
result = await service.batch_delete(session, ids)                 # 批量删除（部分成功形态）
record = await service.activate(session, persona_id)              # 激活（事务性单激活切换）
record = await service.deactivate(session, persona_id)            # 停用激活（允许零激活；幂等）
active = await service.get_active_persona(session)                # 当前唯一激活人格（None = 系统态）
card = await service.get_card(session, persona_id)                # 卡片读取
card = await service.replace_card(session, persona_id, card)      # 卡片整体替换（PUT 语义）
state = await service.upsert_state(session, persona_id, layer, dto)  # M/S 状态 upsert
state = await service.get_state(session, persona_id, layer)       # 指定层状态（未写返回 None）
states = await service.get_states(session, persona_id)            # 全部已写层（layer 升序）

async with bind_persona_context(persona.id):                      # 运行期任务绑定（int | str 归一）
    ...
```

卡片补丁合并语义（`PersonaUpdateDTO.card`）：仅出现字段覆写既有卡片（`exclude_unset` 合并后整体重校验——合并结果违约 `VALIDATION_ERROR` fail loud）；`character_book` 提供时整体替换（不做条目级合并），显式 `null` 清除世界书。

## 管理 API 面（app/api/personas.py）

前缀 `/api/v1/personas`；全部响应经 `ResultDTO` 封装，分页经 `PageResponse`，DB Session 经 `get_async_db` 注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/personas` | 游标分页列表（`status` 过滤 + `cursor` / `limit`） |
| POST | `/api/v1/personas` | 创建（name + card 必填） |
| POST | `/api/v1/personas/batch-delete` | 批量删除（部分成功：`{total, succeeded, failed, items}`） |
| GET | `/api/v1/personas/{id}` | 读取 |
| PUT | `/api/v1/personas/{id}` | 部分更新（重命名 / 展示名 / 状态 / 卡片补丁） |
| DELETE | `/api/v1/personas/{id}` | 删除（激活守卫 409） |
| POST | `/api/v1/personas/{id}/copy` | 复制（`new_name` + `include_states`） |
| POST | `/api/v1/personas/{id}/activate` | 激活（事务性单激活切换） |
| POST | `/api/v1/personas/{id}/deactivate` | 停用激活（允许零激活） |
| GET | `/api/v1/personas/{id}/card` | 卡片读取 |
| PUT | `/api/v1/personas/{id}/card` | 卡片整体替换（body 为完整卡片） |
| GET | `/api/v1/personas/{id}/states` | 已写状态层列表 |
| PUT | `/api/v1/personas/{id}/states/{layer}` | 状态层 upsert（layer ∈ {M, S}；body 为 `{"state": {...}}`） |

错误映射（`app/middleware/error_handler.py` 共享处理器，app 装配点经 `register_exception_handlers(app)` 挂接）：`BusinessException` → HTTP 状态取 ErrorCode 映射（404 / 409 / 422 / 500 …）+ `ResultDTO` error 形态（`data` 携带 details；`headers` 透传）；请求校验失败（参数 / 请求体 / 路径枚举）→ **HTTP 400** + `code=VALIDATION_ERROR`（`data.errors` 为 `[{loc, msg, type}]`）。

## 测试与门禁

- 测试位置：`src/tests/unit/test_persona_chara_card.py`（卡片规格纯单元）/ `test_persona_service.py`（服务面：激活事务性 / 复制含状态 / 唯一名冲突 / 删除守卫 / 状态 upsert / 批量部分成功 / 提交失败回滚）/ `test_persona_api.py`（HTTP 面 ASGITransport + 错误映射 + 批量部分成功；处理器直调契约面）/ `test_app_error_handler.py`（异常处理器映射：状态码 / ResultDTO 形态 / headers 透传 / 400 校验形态 / 幂等注册）；
- 全部 `async def test_`；引擎经共享会话级 SQLite fixture（StaticPool 单连接 + 每测试自动清表）；HTTP 面零真实网络（ASGITransport 本地装配 app）；
- 覆盖率（scoped 实测）：harness/persona **100%**、app/api **100%**、app/middleware **100%**；
- 运行：`cd backend && pixi run tests-unit`。
