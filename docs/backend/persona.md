# persona — 多人格注册表

`backend/src/packages/harness/persona/` 承载多人格注册表：chara card v2 人格卡片（L 层正典）+ M/S 心理状态层 + 事务性主人格切换（多激活可寻址模型——多人格同时可用，主人格唯一）。表名 `ff_personas` / `ff_persona_states`（本工程 `ff_` 前缀约定）。首个 app 层交付面（`app/api/personas.py` 管理 API + `app/middleware/error_handler.py` 共享异常处理器）随本域落地。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（可用性状态 / L-M-S 状态层） | `PersonaStatus` / `PersonaStateLayer` |
| `chara_card.py` | chara card v2 规格模型（卡片 + 世界书，Unit 文件） | `CharaCardModel` / `CharaBookModel` / `CharaBookEntryModel` / `CharaBookPosition` |
| `orm.py` | 注册表 ORM（`ff_personas` + `ff_persona_states`） | `PersonaRow` / `PersonaStateRow` |
| `dtos.py` | 创建 / 更新 / 复制 / 记录 / 卡片 / 状态 / 查询 / 批量 DTO（Unit 文件） | `PersonaCreateDTO` / `PersonaUpdateDTO` / `PersonaCopyDTO` / `PersonaRecordDTO` / `PersonaCardDTO` / `PersonaStateUpsertDTO` / `PersonaStateDTO` / `PersonaQueryDTO` / `PersonaBatchDelete*` |
| `service.py` | 注册 CRUD / 事务性主人格切换 / 可寻址集 / 状态层读写 / 批量删除 + `bind_persona_context` 运行期绑定 | `PersonaService` / `bind_persona_context` |

依赖方向：persona → core（dtos 分页 / errors / context）+ database（Base Mixins / get_async_db）；app 层（api/personas + middleware/error_handler）→ persona + core + database。**两张表均非 PersonaMixin**（见下）。

## L / M / S 三层心理状态模型

| 层 | 含义 | 载体 | 变更语义 |
|---|---|---|---|
| **L**（long-term） | 长期身份正典：性格 / 场景 / 开场白 / 世界书 | `ff_personas.chara_card`（chara card v2 JSON 文档） | 会话期**不可变**；仅管理面经 PUT 端点整体替换或补丁合并 |
| **M**（mid-term） | 中期心情 / 关系评价（跨会话演化） | `ff_persona_states` 行（`layer="M"`，state JSON） | 会话运行期 upsert（整体替换式） |
| **S**（short-term） | 短期会话状态：话题焦点 / 情绪强度 / 最近意图 | `ff_persona_states` 行（`layer="S"`，state JSON） | 会话内高频 upsert；会话外可残留（下会话开场参考） |

L 层不入状态表的理由：身份正典与可变状态的生命周期不同（卡片随人格管理面演进；M/S 随对话演化），且卡片需要作为整体文档校验与传输（DTO 边界一次成型）。

## 关键设计决策（多激活可寻址模型）

1. **注册表是全局管理面数据（非 PersonaMixin）**：admin 需在无人格上下文下跨人格列出 / 切换 / 复制 / 删除人格；PersonaMixin 的请求上下文自动过滤（缺上下文即 fail loud）与该语义直接冲突（人格行自身的 persona_id 无从定义）。记忆表才是 PersonaMixin 消费面（per-persona 逻辑隔离）。
2. **status（可用性）与主人格标记正交**：`status=active/disabled` 是管理面可用性开关——**enabled（status=active）人格构成可寻址集**，可同时被多个上下文寻址绑定（通道会话 / 心流 / 多会话）；`is_primary_marker` 是「主人格」标记位（主人格行恒 `'primary'`、其余行 NULL）——**全表至多一个主人格由 DB 唯一键 `uq_ff_personas_primary (is_primary_marker)` 强制**（MySQL / SQLite 唯一键均忽略 NULL——与策展记忆单 active 守卫同模式）。设为主人格要求 `status=active`。
3. **事务性主人格切换 + 并发收敛**：`set_primary` 在单事务内「全表清主人格标记 → 目标取标记」——设 B 为主人格必然解除 A；任一步失败整体回滚。并发 set_primary 撞唯一键时回滚后重试一次（重读当前主人格走解除-再占位分支），再冲突映射 `INTERNAL_ERROR`。`disable` 是纯生命周期操作（status 切 disabled；幂等）。
4. **主人格须恒在位（两步守卫——本工程锁定语义）**：删除 / 停用（`disable` 与 `update(status=disabled)`）**当前主人格均被禁止**（`CONFLICT`，调用方须先 `set_primary` 另设主人格——显式两步，杜绝「删除即静默掉线为零主人格」的隐式状态跳变）。删除非主人格时同事务清理其 M/S 状态行（SQLite 测试方言默认不启用 FK 强制，服务层显式删除保证跨方言一致；DB 级 `ON DELETE CASCADE` 仅作直删 SQL 兜底）。
5. **主人格读取 fail loud**：`get_primary_persona` 无主人格（全新库 / 从未设置）抛 `NOT_FOUND`（清晰中文错误 + fix 指引）——消费面（chat 会话 / 值班 / 梦境 / 记忆面缺省目标）原将「无激活人格」翻译为各自错误，统一由服务层承担；无人格可正常工作的消费面（语音 / 图像理解未绑定形态）自行捕获归零。可寻址集经 `list_addressable()` 读取（enabled 人格，id 升序）。
6. **无乐观锁版本列**：本工程 ORM 无版本计数约定（database/base.py 仅 BigIntIdMixin / TimestampMixin / PersonaMixin，usage 域表亦无）——遵循既有约定不引入 `version` 列；注册表写路径低频（管理面操作），冲突面由 name 唯一约束 + 主人格标记唯一键 + 单事务切换兜底。
7. **运行期绑定 `bind_persona_context(persona_id)`**：注册表主键为 BigInt，而上下文轴 `persona_id_var` / PersonaMixin 均为字符串——绑定 helper 接受 `int | str` 并经 `str()` 归一（chat 会话等运行期入口消费，见 `core.context.bind_context`）。

> **前向注（R2d）**：当前「按上下文寻址」的消费面已具备数据模型基础（可寻址集 + 会话级 persona 绑定——通道会话按 `persona_id` 保持在位、resumed 会话保留其记录人格）；R2d 将在此之上交付 per-context 绑定（心流 / 群聊 / 多会话显式选择人格，主人格仅作缺省解析目标）。

## 链路顺序

**创建 → 设主 → 会话绑定**：

```
管理面 POST /api/v1/personas（PersonaCreateDTO：name + card）
    ↓ 唯一名校验（CONFLICT fail loud）→ PersonaRow（status 缺省 disabled，非主人格）
    ↓ 管理面 PUT /{id}（status=active 启用——进入可寻址集）
    ↓ 管理面 POST /{id}/set-primary（单事务：全表清标记 → 目标占位；DB 唯一键兜底）
    ↓ 运行期（chat 会话）async with bind_persona_context(persona.id):
    ↓     PersonaMixin 表自动按 str(persona.id) 隔离；llm 网关用量记账携带 persona_id
```

**复制链**：

```
POST /{id}/copy（PersonaCopyDTO：new_name + include_states）
    ↓ 源行读取（NOT_FOUND fail loud）→ 新名唯一校验（CONFLICT）
    ↓ 新行：卡片深拷贝 + status=disabled + 非主人格（启用与设主均为显式后续动作）
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
| `status` | VARCHAR(16) | 非空 | 可用性状态（active / disabled；enabled 人格构成可寻址集） |
| `is_primary_marker` | VARCHAR(16) | 可空；**唯一键 `uq_ff_personas_primary`**（NULL 共存——两方言唯一键均忽略 NULL） | 主人格标记（主人格行恒 `'primary'`、其余行 NULL——DB 强制全表至多一个主人格） |
| `chara_card` | JSON | 非空 | 人格卡片（chara card v2 文档，L 层正典） |
| `voice_profile` | JSON | 可空 | 人格语音绑定（`VoiceProfileModel` 序列化形态——speaker / instruct_text / speed；NULL = 未绑定，合成回落 `Settings.voice.tts` 缺省） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

### ff_persona_states（M/S 状态层）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | BIGINT | 非空；FK `ff_personas.id`（ON DELETE CASCADE）；索引 | 所属人格 |
| `layer` | VARCHAR(8) | 非空；`(persona_id, layer)` 唯一 | 状态层（M=中期；S=短期） |
| `state` | JSON | 非空 | 状态 JSON 文档（整体替换式更新） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

迁移链：`backend/alembic/versions/000000000003_add_personas.py`（Revision 000000000003，Revises 000000000002——两表 + 索引 + 唯一约束）→ `000000000006_add_persona_voice_profile.py`（Revision 000000000006，Revises 000000000005——`ff_personas` 增 `voice_profile` JSON 可空列，纯增列、既有行落 NULL）→ `000000000008_persona_primary_marker.py`（Revision 000000000008，Revises 000000000007——多激活可寻址模型重构：增 `is_primary_marker` 可空列 + 唯一键 `uq_ff_personas_primary`，旧 `is_active=1` 行回填 `'primary'`（旧单激活不变式保证至多一行），删 `is_active` 列；降级逆向重建；升级 / 降级 / 再升级均经 SQLite 冒烟验证）。

## 值域

- `PersonaStatus`：`active`（可用——进入可寻址集，允许被设为主人格）/ `disabled`（停用——退出可寻址集，设主守卫拒绝；停用当前主人格被守卫拒绝）；
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
| `secondary_keys` | list[str] | 缺省 `[]`；**仅 `selective=True` 可携带且须有非空键**（选择性条目空副键永不触发——构造期拒绝） | 副触发关键词（选择性触发收敛） |
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
record = await service.create(session, dto)                       # 创建（唯一名校验；非主人格；voice_profile 可选绑定）
record = await service.get(session, persona_id)                   # 读取（NOT_FOUND fail loud）
page = await service.query(session, PersonaQueryDTO(...))         # 游标分页（id 降序 + status 过滤）
record = await service.update(session, persona_id, dto)           # 部分更新（重命名 / 状态 / 卡片补丁 / 语音绑定整体替换）
record = await service.copy(session, persona_id, dto)             # 复制（可选深拷贝 M/S；恒 disabled + 非主人格；语音绑定随行复制）
await service.delete(session, persona_id)                         # 删除（主人格守卫：CONFLICT——先另设主人格）
result = await service.batch_delete(session, ids)                 # 批量删除（部分成功形态）
record = await service.set_primary(session, persona_id)           # 设为主人格（事务性切换；DB 唯一键 + 冲突重试）
record = await service.disable(session, persona_id)               # 停用（生命周期；主人格守卫拒绝；幂等）
primary = await service.get_primary_persona(session)              # 当前主人格（无主人格 NOT_FOUND fail loud）
addressable = await service.list_addressable(session)             # 可寻址集（enabled 人格，id 升序）
card = await service.get_card(session, persona_id)                # 卡片读取
card = await service.replace_card(session, persona_id, card)      # 卡片整体替换（PUT 语义）
state = await service.upsert_state(session, persona_id, layer, dto)  # M/S 状态 upsert
state = await service.get_state(session, persona_id, layer)       # 指定层状态（未写返回 None）
states = await service.get_states(session, persona_id)            # 全部已写层（layer 升序）

async with bind_persona_context(persona.id):                      # 运行期任务绑定（int | str 归一）
    ...
```

`PersonaRecordDTO` 携带 `voice_profile: VoiceProfileModel | None`（None = 未绑定）——创建 / 更新 / 复制 / 读取全链路透出人格语音绑定，语音合成面（`voice` 域）据此以人格级 speaker / speed 覆盖 settings 缺省（优先级：请求参数 > 人格绑定 > settings 缺省）。

卡片补丁合并语义（`PersonaUpdateDTO.card`）：仅出现字段覆写既有卡片（`exclude_unset` 合并后整体重校验——合并结果违约 `VALIDATION_ERROR` fail loud）；`character_book` 提供时整体替换（不做条目级合并），显式 `null` 清除世界书。

## 管理 API 面（app/api/personas.py）

前缀 `/api/v1/personas`；全部响应经 `ResultDTO` 封装，分页经 `PageResponse`，DB Session 经 `get_async_db` 注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/personas` | 游标分页列表（`status` 过滤 + `cursor` / `limit`） |
| POST | `/api/v1/personas` | 创建（name + card 必填；`voice_profile` 可选绑定） |
| POST | `/api/v1/personas/batch-delete` | 批量删除（部分成功：`{total, succeeded, failed, items}`） |
| GET | `/api/v1/personas/{id}` | 读取（含 `voice_profile` 绑定态） |
| PUT | `/api/v1/personas/{id}` | 部分更新（重命名 / 展示名 / 状态 / 卡片补丁 / 语音绑定——`voice_profile` **出现即整体替换**，显式 `null` 清除绑定，无字段级合并） |
| DELETE | `/api/v1/personas/{id}` | 删除（主人格守卫 409） |
| POST | `/api/v1/personas/{id}/copy` | 复制（`new_name` + `include_states`；语音绑定随行复制） |
| POST | `/api/v1/personas/{id}/set-primary` | 设为主人格（事务性切换；停用人格 409） |
| POST | `/api/v1/personas/{id}/disable` | 停用（生命周期；主人格守卫 409） |
| GET | `/api/v1/personas/primary` | 当前主人格（无主人格 404——清晰中文错误） |
| GET | `/api/v1/personas/{id}/card` | 卡片读取 |
| PUT | `/api/v1/personas/{id}/card` | 卡片整体替换（body 为完整卡片） |
| GET | `/api/v1/personas/{id}/states` | 已写状态层列表 |
| PUT | `/api/v1/personas/{id}/states/{layer}` | 状态层 upsert（layer ∈ {M, S}；body 为 `{"state": {...}}`） |

错误映射（`app/middleware/error_handler.py` 共享处理器，app 装配点经 `register_exception_handlers(app)` 挂接）：`BusinessException` → HTTP 状态取 ErrorCode 映射（404 / 409 / 422 / 500 …）+ `ResultDTO` error 形态（`data` 携带 details；`headers` 透传）；请求校验失败（参数 / 请求体 / 路径枚举）→ **HTTP 400** + `code=VALIDATION_ERROR`（`data.errors` 为 `[{loc, msg, type}]`）。

## 测试与门禁

- 测试位置：`src/tests/unit/test_persona_chara_card.py`（卡片规格纯单元）/ `test_persona_service.py`（服务面：主人格事务性切换 / 并发收敛（唯一键 + 冲突重试 + gather 双切恰一主人格）/ 停用守卫 / 复制含状态 / 唯一名冲突 / 删除守卫 / 可寻址集 / 状态 upsert / 批量部分成功 / 提交失败回滚）/ `test_persona_api.py`（HTTP 面 ASGITransport + 错误映射 + 批量部分成功；处理器直调契约面）/ `test_app_error_handler.py`（异常处理器映射：状态码 / ResultDTO 形态 / headers 透传 / 400 校验形态 / 幂等注册）；
- 全部 `async def test_`；引擎经共享会话级 SQLite fixture（StaticPool 单连接 + 每测试自动清表）；HTTP 面零真实网络（ASGITransport 本地装配 app）；
- 覆盖率（scoped 实测）：harness/persona **100%**、app/api **100%**、app/middleware **100%**；
- 运行：`cd backend && pixi run tests-unit`。
