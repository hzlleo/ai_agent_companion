# persona — 多人格注册表

`backend/src/packages/harness/persona/` 承载多人格注册表：chara card v2 人格卡片（L 层正典）+ M/S 心理状态层 + 事务性主人格切换（多激活可寻址模型——多人格同时可用，主人格唯一）。表名 `ff_personas` / `ff_persona_states`（本工程 `ff_` 前缀约定）。首个 app 层交付面（`app/api/personas.py` 管理 API + `app/middleware/error_handler.py` 共享异常处理器）随本域落地。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（可用性状态 / L-M-S 状态层 / 导入冲突策略与原因） | `PersonaStatus` / `PersonaStateLayer` / `PersonaImportConflictStrategy` / `PersonaImportReason` |
| `chara_card.py` | chara card v2 规格模型（卡片 + 世界书，Unit 文件） | `CharaCardModel` / `CharaBookModel` / `CharaBookEntryModel` / `CharaBookPosition` |
| `trait_library.py` | 特质组件库（组件 / 预设模型 + 确定性卡片生成 + 官方库装载，Unit 文件） | `TraitComponentModel` / `TraitPresetModel` / `generate_card_from_preset` / `load_official_presets` / `load_official_components` |
| `presets/` | 官方库数据面（仓库数据非代码：`components.yaml` 33 组件 + `presets.yaml` 8 预设） | — |
| `orm.py` | 注册表 ORM（`ff_personas` + `ff_persona_states`） | `PersonaRow` / `PersonaStateRow` |
| `dtos.py` | 创建 / 更新 / 复制 / 记录 / 卡片 / 状态 / 查询 / 批量 / 导入 / 特质库 DTO（Unit 文件） | `PersonaCreateDTO` / `PersonaUpdateDTO` / `PersonaCopyDTO` / `PersonaRecordDTO` / `PersonaCardDTO` / `PersonaStateUpsertDTO` / `PersonaStateDTO` / `PersonaQueryDTO` / `PersonaBatchDelete*` / `PersonaImportRequestDTO` / `PersonaImportResultModel` / `PersonaImportBatch*` / `TraitLibraryDTO` |
| `service.py` | 注册 CRUD / 事务性主人格切换 / 可寻址集 / 状态层读写 / 批量删除 / 导入（幂等 + 冲突策略）+ `bind_persona_context` 运行期绑定 | `PersonaService` / `bind_persona_context` / `canonical_card_hash` |
| `directives.py` / `directives_models.py` | 长期指令（USER.md 式常备指令）服务与 DTO——策展记忆 `user_note` + `directive:` 前缀命名空间的收窄面（R2b D7-14） | `DirectivesService` / `DirectiveCreateDTO` / `DirectiveUpdateDTO` / `DirectiveRecordDTO` / `DIRECTIVE_TOPIC_PREFIX` |
| `facts.py` / `facts_models.py` / `facts_orm.py` | 人物事实（结构化事实 / 证据链 / 更正循环）服务、DTO 与 `ff_persona_facts` ORM（R2c） | `FactsService` / `FactCreateDTO` / `FactCorrectDTO` / `FactRecordDTO` / `FactQueryDTO` / `PersonaFactRow` / `PersonaFactCategory` |
| `skills.py` / `skills_models.py` / `skills_orm.py` | 程序性技能（触发短语 / 原地版本 / 启用状态 / compose 匹配 / 梦境合并）服务、DTO 与 `ff_persona_skills` ORM（R2c） | `SkillsService` / `SkillCreateModel` / `SkillUpdateModel` / `SkillRecordDTO` / `SkillQueryDTO` / `PersonaSkillRow` / `SkillStatus` |
| `core_memory.py` / `core_memory_models.py` / `core_memory_orm.py` | 自编辑核心记忆（四块自我模型 / 主人锁定 / append-only 修订 / 工具注册）服务、DTO 与 `ff_core_memory_blocks` / `ff_core_memory_revisions` ORM（R2c） | `CoreMemoryService` / `register_core_memory_tools` / `CoreMemoryBlockDTO` / `CoreMemoryRevDTO` / `OwnerEditModel` / `AgentEditModel` / `CoreMemoryBlockRow` / `CoreBlockKey` |

依赖方向：persona → core（dtos 分页 / errors / context）+ database（Base Mixins / get_async_db）+ voice（预设建议语音 `VoiceProfileModel`）+ memory（人物事实的证据校验复用 `MemoryService.get_episode` 与检索层取段规则；梦境编排反向消费 `FactsService` / `SkillsService`——见 memory.md 夜间梦境）+ tools（核心记忆工具注册复用 `ToolRegistry` / `ToolSpecModel`）；app 层（api/personas + api/directives + api/facts + api/skills + api/core_memory + middleware/error_handler）→ persona + core + database。**`ff_personas` / `ff_persona_states` 两张表非 PersonaMixin**（见下）；**`ff_persona_facts` / `ff_persona_skills` / `ff_core_memory_blocks` 是 PersonaMixin 人格作用域表**（画像 / 做事方式 / 自我模型都属某个人格对主人的认知；`ff_core_memory_revisions` 经 `block_id` 外键间接归属，见该表说明）。

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
| `state` | JSON | 非空 | 状态 JSON 文档（整体替换式更新；M 层可含 `emotion` 子文档——R2b 轻情绪引擎，见 [emotion.md](emotion.md)） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

### ff_persona_facts（人物事实——R2c，迁移 0013）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | VARCHAR(64) | 非空；索引（PersonaMixin 自动注入 / 过滤） | 所属人格 |
| `category` | VARCHAR(16) | 非空；索引 | 类别（preference / biography / relationship / context） |
| `content` | VARCHAR(1000) | 非空 | 事实陈述（服务层归一化后落库） |
| `normalized_hash` | VARCHAR(64) | 非空；索引 | 正文归一化 sha256（事实身份轴） |
| `confidence` | FLOAT | 非空 | 置信度（0-1） |
| `status` | VARCHAR(16) | 非空；索引 | active / superseded / rejected |
| `provenance` | VARCHAR(16) | 非空 | owner / agent（untrusted 服务层拒绝） |
| `source` | VARCHAR(16) | 非空 | dream / manual / correction |
| `evidence` | JSON | 非空 | 证据引用数组（`FactEvidenceRefModel` 序列化形态） |
| `superseded_by_id` | BIGINT | 可空；FK `ff_persona_facts.id`（ON DELETE SET NULL） | 取代者（更正链） |
| `version` | INT | 非空 | 更正链版本号（首版 1） |
| `active_marker` | VARCHAR(16) | 可空；**唯一键 `uq_ff_persona_facts_active (persona_id, category, normalized_hash, active_marker)`**（NULL 共存——两方言唯一键均忽略 NULL） | 单 active 守卫标记 |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

### ff_persona_skills（程序性技能——R2c，迁移 0014）

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | VARCHAR(64) | 非空；索引（PersonaMixin 自动注入 / 过滤） | 所属人格 |
| `name` | VARCHAR(120) | 非空 | 技能名（slug 由服务层按名称归一化派生） |
| `slug` | VARCHAR(80) | 非空；**唯一键 `uq_ff_persona_skills_slug (persona_id, slug)`** | 归一化 kebab 标识（同人格唯一——原地编辑身份轴） |
| `description` | VARCHAR(500) | 非空 | 何时使用的触发描述 |
| `triggers` | JSON | 非空 | 触发短语数组（1-8 条、每条 1-40 字符——字面包含匹配） |
| `content` | TEXT | 非空 | markdown 步骤正文（1-8000 字符） |
| `status` | VARCHAR(16) | 非空；索引 | active / disabled |
| `source` | VARCHAR(16) | 非空 | manual / mined |
| `confidence` | FLOAT | 非空 | 置信度（0-1） |
| `use_count` | INT | 非空 | compose 命中次数（排序强化轴） |
| `last_used_ms` | BIGINT | 可空 | 最近一次命中时刻（epoch 毫秒） |
| `version` | INT | 非空 | 原地编辑版本号（首版 1，编辑递增——无历史链） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

迁移链：`backend/alembic/versions/000000000003_add_personas.py`（Revision 000000000003，Revises 000000000002——两表 + 索引 + 唯一约束）→ `000000000006_add_persona_voice_profile.py`（Revision 000000000006，Revises 000000000005——`ff_personas` 增 `voice_profile` JSON 可空列，纯增列、既有行落 NULL）→ `000000000008_persona_primary_marker.py`（Revision 000000000008，Revises 000000000007——多激活可寻址模型重构：增 `is_primary_marker` 可空列 + 唯一键 `uq_ff_personas_primary`，旧 `is_active=1` 行回填 `'primary'`（旧单激活不变式保证至多一行），删 `is_active` 列；降级逆向重建；升级 / 降级 / 再升级均经 SQLite 冒烟验证）→ `000000000013_persona_facts.py`（Revision 000000000013，Revises 000000000012——新建 `ff_persona_facts` + 单 active 唯一键 + 索引；SQLite 冒烟与 SeekDB 真引擎 round-trip 双双覆盖）→ `000000000014_persona_skills.py`（Revision 000000000014，Revises 000000000013——新建 `ff_persona_skills` + slug 唯一键 + 状态索引；SQLite 冒烟与 SeekDB 真引擎 round-trip 双双覆盖）→ `000000000015_core_memory.py`（Revision 000000000015，Revises 000000000014——新建 `ff_core_memory_blocks`（四块唯一键 + persona 索引）与 `ff_core_memory_revisions`（block_id 级联外键 + block_id / ts_ms 索引）；降级先删修订表再删块表；SQLite 冒烟与 SeekDB 真引擎 round-trip 双双覆盖）。

### ff_core_memory_blocks / ff_core_memory_revisions（自编辑核心记忆——R2c，迁移 0015）

块表（`ff_core_memory_blocks`，PersonaMixin 人格作用域——固定四块 × 每块一行）：

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键 |
| `persona_id` | VARCHAR(64) | 非空；索引（PersonaMixin 自动注入 / 过滤） | 所属人格 |
| `block_key` | VARCHAR(32) | 非空；**唯一键 `uq_ff_core_memory_blocks_key (persona_id, block_key)`** | 块键（self_identity / owner_profile / relationship_story / open_notes——封闭四元组） |
| `content` | TEXT | 非空 | 块正文（0-2000 字符；主人可清空、人格写入须非空） |
| `locked` | BOOLEAN | 非空（缺省 false） | 主人专属写闸（true = 人格工具路径被拒绝） |
| `version` | INT | 非空（首版 1） | 编辑版本号（内容 / 锁定状态变更递增） |
| `updated_by` | VARCHAR(16) | 非空 | owner / agent（最近一次写入者） |
| `last_edited_session_id` | BIGINT | 可空 | 最近一次对话内编辑的会话主键（管理面编辑为 NULL） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

修订表（`ff_core_memory_revisions`，**append-only——服务层只插入、从不更新 / 删除**）：

| 列 | 类型 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增（SQLite 渲染 INTEGER） | 主键（越大越新） |
| `block_id` | BIGINT | 非空；索引；FK `ff_core_memory_blocks.id`（ON DELETE CASCADE） | 归属块（块删除级联清理修订） |
| `version` | INT | 非空 | 修订产生时的块版本号（与块行当时版本一致） |
| `content` | TEXT | 非空 | 修订后的块全文（逐字留存——历史不可变） |
| `provenance` | VARCHAR(16) | 非空 | owner / agent（写入者） |
| `source` | VARCHAR(16) | 非空 | admin / chat（写入来源） |
| `session_id` | BIGINT | 可空 | 对话内编辑的会话主键（管理面修订为 NULL） |
| `ts_ms` | BIGINT | 非空；索引 | 修订时刻（epoch 毫秒） |
| `created_at` / `updated_at` | BIGINT | 非空 | epoch 毫秒（TimestampMixin） |

修订表不挂 PersonaMixin：归属经 `block_id` 指向已作用域化的块行（读取先按块键取块、再按 `block_id` 展开），避免两处人格轴需要同步。

## 值域

- `PersonaStatus`：`active`（可用——进入可寻址集，允许被设为主人格）/ `disabled`（停用——退出可寻址集，设主守卫拒绝；停用当前主人格被守卫拒绝）；
- `PersonaStateLayer`：`M`（中期心情 / 关系）/ `S`（短期会话状态）——`L` 不入状态表（见三层模型）；
- `CharaBookPosition`：`before_char` / `after_char`（v2 字符串形态）；
- `CoreBlockKey`：`self_identity`（自我认知）/ `owner_profile`（关于对方）/ `relationship_story`（我们的故事）/ `open_notes`（备忘）——封闭四元组，不存在第五块；
- `CoreMemoryActor`：`owner`（主人）/ `agent`（人格自身）——块的 `updated_by` 与修订的 `provenance` 同轴；
- `CoreMemorySource`：`admin`（管理面）/ `chat`（对话内工具调用）——与 actor 强制配对（admin↔owner、chat↔agent）；
- `AgentEditMode`：`replace`（整块替换）/ `append`（末尾追加）。

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

**世界书（CharaBookModel）**：`name` / `description`（str）、`scan_depth`（int ≥ 1，最近 N 条消息内检索——**书级**扫描窗口，条目级不设窗口）、`token_budget`（int ≥ 1，超预算按渲染序截断）、`recursive_scanning`（bool——递归扫描**总开关**：条目级 `recursive=True` 须本开关放行，错配构造期拒绝）、`entries`（条目列表）。

**世界书条目（CharaBookEntryModel）**：

| 字段 | 类型 | 校验 / 缺省 | 说明 |
|---|---|---|---|
| `keys` | list[str] | 缺省 `[]` | 主触发关键词（任一命中即插入） |
| `secondary_keys` | list[str] | 缺省 `[]`；**仅 `selective=True` 可携带且须有非空键**（选择性条目空副键永不触发——构造期拒绝） | 副触发关键词（选择性触发收敛） |
| `content` | str | 缺省 `""` | 条目正文 |
| `insertion_order` | int | **界 0-9999**，缺省 100 | 插入顺序（值越大越靠后） |
| `constant` | bool | 缺省 False | 恒插入（免触发；无视分组 / 概率 / 冷却门，不占 token 预算） |
| `case_sensitive` | bool | 缺省 False | 关键词匹配区分大小写 |
| `selective` | bool | 缺省 False | 选择性触发（主键 + 副键双命中） |
| `position` | CharaBookPosition | 缺省 `after_char` | 插入位置（决定渲染段位） |
| `depth` | int | **界 0-20**，缺省 4 | 深度槽位：段内值越大越靠段首（远离对话末尾），0 为段尾槽（最贴近末条消息一侧）；同槽按 `insertion_order` |
| `recursive` | bool | 缺省 False；**须书级 `recursive_scanning=True`**（错配构造期拒绝） | 条目正文参与递归扫描面（其内容可再触发其他条目） |
| `include_group` | str \| None | 缺省 None；空白归一 None；≤64 字符 | 互斥分组名（同组非 constant 命中条目一次组装只注入优先级最高者） |
| `trigger_probability` | float | **界 0.0-1.0**，缺省 1.0 | 触发概率门（命中后按概率注入；1.0 恒过门且零随机数消耗） |
| `cooldown_turns` | int | **界 0-50**，缺省 0 | 注入后冷却回合数（回合 = 组装调用；会话作用域内存追踪） |

### 世界书高级触发语义（R2b）

选择算法链（chat 域 `world_book.select_world_book_entries` 纯函数——顺序锁定，逐候选按链序过门）：

```
① constant 恒注入      constant 条目直接进候选集（免触发；无视分组 /
                        概率 / 冷却门；不占 token 预算）
② 关键词匹配            最近 scan_depth 条回合文本拼接扫描面：keys 任一
                        命中；selective 再要求 secondary_keys 任一命中；
                        大小写按条目声明
③ 递归扩展（2 跳上限）   书级 recursive_scanning 总开关 × 条目级 recursive
                        收窄：已入选条目正文追加进扫描面再触发新条目；
                        跳 1 → 跳 2 → 停（跳 2 入选者正文不再入面）；
                        visited 集合保证环不可能（条目至多入选一次）
④ 互斥分组剪枝          非 constant 候选同 include_group 只留优先级最高
                        者（insertion_order 升序、同值按条目索引升序
                        ——确定性）；constant 无视分组（不参与不排斥）
⑤ 概率门（rng）          非 constant 候选 trigger_probability < 1.0 时
                        消耗一次 rng.random() < p 判定；消耗序 = 渲染序
⑥ 冷却门（回合状态）     上次注入第 F 回合、当前第 C 回合时
                        1 <= C - F <= cooldown_turns 即拒（第 N+1 回合解禁）
⑦ token 预算装箱        渲染序累计 estimate_text_tokens；首条超预算即
                        截止（其后非 constant 候选不再评估）
```

渲染序（精确裁定）：条目恒渲染在 `position` 决定的段内（`before_char` 段在人格正典前 / `after_char` 段在后——段位语义不变，prompt 模板字节零改动）；段内按 **`(depth 降序, insertion_order 升序, 条目索引升序)`**。`depth` 语义是「距对话末尾的近因深度」（0 = 最贴近末条消息）：世界书条目渲染于角色定义段，故 depth 落位为段内深度槽——0 为段尾槽（紧邻其后渲染的对话历史一侧），值越大越靠段首。**兼容保证**：既有卡片全部条目缺省 `depth=4`（同槽）→ 段内退化为纯 `insertion_order` 升序 = 既有行为逐字节一致。

冷却的会话作用域（诚实文档化）：`cooldown_turns` 的回合计数与注入追踪存于 `ChatSessionStateModel.world_book_state`（组装调用计数 + `{条目索引: 注入回合号}`），**纯内存态、无持久化 schema**——进程重启即复位（冷却全部解除）；条目索引在会话内稳定（卡片快照会话期不可变）。

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
    "recursive_scanning": true,
    "entries": [
      {
        "keys": ["鱼缸", "书房"],
        "secondary_keys": [],
        "content": "大肥鱼住在主人书房的圆形鱼缸里。",
        "insertion_order": 10,
        "constant": false,
        "case_sensitive": false,
        "selective": false,
        "position": "after_char",
        "depth": 4,
        "recursive": false,
        "include_group": null,
        "trigger_probability": 1.0,
        "cooldown_turns": 0
      },
      {
        "keys": ["晚饭"],
        "secondary_keys": ["计划"],
        "content": "大肥鱼很关心主人有没有好好吃饭。",
        "insertion_order": 20,
        "constant": false,
        "case_sensitive": true,
        "selective": true,
        "position": "before_char",
        "depth": 4,
        "recursive": false,
        "include_group": null,
        "trigger_probability": 1.0,
        "cooldown_turns": 0
      },
      {
        "keys": ["晚安", "睡觉"],
        "secondary_keys": [],
        "content": "睡前语态：大肥鱼会压低声音说晚安。",
        "insertion_order": 30,
        "constant": false,
        "case_sensitive": false,
        "selective": false,
        "position": "after_char",
        "depth": 0,
        "recursive": false,
        "include_group": "night-greeting",
        "trigger_probability": 0.8,
        "cooldown_turns": 2
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
    "last_summary": "主人这周工作很忙，聊了三次加班",
    "emotion": {
      "values": {"joy": 0.62, "affection": 0.58},
      "updated_at_ms": 1760000000000
    }
  }
}
```

（`emotion` 子文档由轻情绪引擎经同一 upsert 面合并写入——其他 M 层键原样
保留；结构 / 衰减 / 摘要规则见 [emotion.md](emotion.md)。）

```json
{
  "state": {
    "topic": "周末计划",
    "energy": 0.6,
    "recent_intent": "想约主人出门散步"
  }
}
```

## 特质组件库与导入（R2b-2）

### 特质组件库（P1——纯特质组合集发行）

官方预设库以**纯特质组合集**形态发行：零角色名 / 零背景故事 / 零既有形象引用（版权洁净由「只发行可组合特质」保证）；用户经导入端点私有导入自己的完整 chara card v2 卡片文件。两层结构：

- **组件**（`TraitComponentModel`，`presets/components.yaml`，33 个）：最小可复用人格碎片——`description_zh`（落入卡片 `personality` 的短句）+ 六类分类（温性 / 言语风格 / 习惯癖性 / 价值取向 / 社交风格 / 幽默感）+ `tone_directives`（进入 system 提示的短指令行）+ `sample_speech`（示例台词——拼入 `mes_example`）+ `conflicts`（不宜共存的组件 id——组合时软警告不拒绝）。
- **预设**（`TraitPresetModel`，`presets/presets.yaml`，8 个原型：大家闺秀 / 小家碧玉 / 绿茶 / 傲娇 / 温柔治愈 / 高冷御姐 / 元气少女 / 毒舌损友）：组件 id 组合集（声明序即组合序）+ 可选覆写片段（`name_suffix` 名字后缀 / `personality` / `mes_example` / `system_prompt_fragment`）+ `suggested_voice`（导入时随人格落库）+ `suggested_book`（生成时并入卡片世界书）。

**生成面是纯函数**（`generate_card_from_preset`——确定性组合，逐字段锁定）：

- `name` = 基础名 + `name_suffix`（存在时；基础名 = 期望名或预设中文名）；
- `personality` = 预设描述 → 预设级补充 → 组件碎片，`；` 拼接；
- `system_prompt` = 组件语气指令行（声明序）→ 预设级补充行，换行拼接；
- `mes_example` = 预设级示例 → 组件示例台词（各成 `<START>\n{name}: 台词` 块）；
- `character_book` = 建议条目非空时建书（名 = `{预设id}-book`），否则 None；
- 组合集命中冲突声明 → `GenerationWarningModel` 列表**返回不抛错**（调用方并入导入结果 `warnings` 呈现）。

**装载面**（`load_official_components` / `load_official_presets`）：路径相对资源加载（`presets/` 数据文件）+ 模型校验 + 模块级缓存——**模块导入期零副作用**，首次调用（端点）即校验，非法数据文件 fail loud（`ValueError` → 端点映射 500）。官方库不变式（装载期校验拒绝违约数据）：预设引用组件恒存在、id 唯一、组合集内无重复、**官方预设自身零冲突**（命中冲突声明 = 发行数据作者错误）、冲突引用指向已知组件。显式传入数据目录（测试合成数据）绕过缓存。

### 导入端点（P2——内容哈希幂等 + 名称冲突三策略）

`POST /api/v1/personas/import` 双载荷形态（JSON 或 multipart——stdlib `email` 解析器解析，与 voice.asr 同模式零新增依赖；请求体上限为模块级常量 2MB，不引入新 settings 键）：

- `{preset_id}`——服务端按官方预设 + 组件库确定性生成卡片（期望名缺省 = 预设中文名；生成软警告并入结果 `warnings`；预设建议语音随创建落库；未知预设 404）；
- `{card}`（JSON）或 `file`（multipart 卡片 JSON 文件）——用户自有卡片文档直载；
- `conflict_strategy`（skip 缺省 / rename / overwrite）+ `desired_name` 对两种载荷通用。

**冲突判定序（锁定语义，`PersonaService.import_persona`）**：

1. **内容同卡幂等**：导入卡片与既有人格卡片的内容哈希（`canonical_card_hash`——`model_dump` + `sort_keys` 紧凑序列化的 sha256；等价 JSON 恒同哈希）一致 → `{imported: false, reason: identical_content, persona_id: 既有行}`——**与冲突策略无关**（同卡重导恒幂等）；
2. 目标注册名（`desired_name` 或卡片名）空闲 → 创建新行（恒 disabled + 非主人格；展示名回落卡片名）；
3. 名称冲突 → 策略生效：`skip`（缺省）返回 `name_conflict` 未导入；`rename` 自动后缀 `名字-2` 起寻名落库（期望名已被占用时）；`overwrite` 整体替换占用行卡片——**保留** M/S 状态 / status / 主人格标记 / 语音绑定（仅卡片文档替换）。

`POST /api/v1/personas/import/batch`（multipart `files` 多文件）：逐文件独立事务部分成功——解析失败记 `VALIDATION_ERROR` 失败项；**幂等跳过与名称冲突（skip 策略）是合法结果**（`success=true, imported=false`，`reason` 呈现原因）；计数面 `{total, succeeded, failed, imported_count}`。

前端消费面（`frontend/src/pages/personas/`）：预设画廊（`/personas/presets`——卡片网格 + 组件芯片 + 生成流）与列表页导入按钮（`PersonaImportButton`——单文件冲突三选一重问（跳过 / 改名 / 覆盖）+ 批量结果对话框）；共享三选一对话框 `PersonaImportConflictDialog`。multipart 请求体由前端**手工 RFC 2046 序列化**（卡片文档恒 UTF-8 文本——确定性 wire 形态，规避 axios JSON 实例头对 FormData 的改写与拦截器链对跨实现 File 对象的文件名丢失）。

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
result = await service.import_persona(session, card, strategy=..., desired_name=..., warnings=..., voice_profile=...)  # 导入（内容哈希幂等 + 冲突策略；预设建议语音仅创建路径落库）
result = await service.import_personas_batch(session, files, strategy=...)  # 批量导入（部分成功：解析失败失败项 / 幂等与冲突跳过为合法成功项）
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


## 长期指令（R2b D7-14——USER.md 式常备指令）

长期指令是主人对人格的**持久行为约定**（如「以后都用简体中文」「别再熬夜了」）——
与一次性记忆的区别是它在每轮对话组装的系统提示里恒常注入（chat compose 链的
【长期指令】块，见 [chat.md](chat.md) 组装链）。

**存储决策（锁定）**：**不新建表**——复用策展记忆域 `ff_curated_memory` 的
`kind=user_note` + `topic='directive:<slug>'` 行；supersede 语义（同 topic 单
active + 历史链 + 唯一键强制）与删除语义直接继承策展层，本域只做命名空间收窄与
边界校验（不复制存储实现）。

| 项 | 取值 | 说明 |
|---|---|---|
| `kind` | `user_note` | 固定（策展类型值域既有成员） |
| `topic` | `directive:<slug>` | 前缀强制（`DIRECTIVE_TOPIC_PREFIX`）；缺省 slug = 正文 sha256 前 12 hex（同文重复创建收敛同链——天然去重）；显式 topic 用于编辑（同 topic 覆盖 supersede，`version` 递增） |
| `content` | 1-500 字符 | 首尾空白裁剪；超长 / 空白拒绝（`VALIDATION_ERROR`） |
| `provenance` | `owner` | 固定（长期指令恒源自主人——对话捕获与面板创建同轴） |
| 注入帽 | 20 条 / 2000 字符 | chat compose 渲染帽（超帽追加截断说明行） |

`DirectivesService` API（人格作用域——调用方先 `bind_persona_context`）：

```python
service = DirectivesService()                                     # 无状态；Session 逐调用注入
record = await service.create(session, DirectiveCreateDTO(content=..., topic=None))   # 创建 / 同 topic 覆盖
record = await service.update(session, directive_id, DirectiveUpdateDTO(content=...)) # 按既有 topic 重写
records = await service.list_active(session, limit=200)           # 现行指令（id 降序）
texts = await service.list_active_texts(session, limit=20)        # compose 注入面（正文列表）
await service.delete(session, directive_id)                       # 删除（策展行删除）
```

同域非指令行（普通 `user_note` 备忘）在列表 / 更新 / 删除面均不可见（topic 前缀
收窄——非本域行 `NOT_FOUND`）。他人格指令同样 `NOT_FOUND`（PersonaMixin 作用域）。

### 管理 API 面（app/api/directives.py）

前缀 `/api/v1/directives`；全部响应经 `ResultDTO` 封装，DB Session 经
`get_async_db` 注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/directives` | 现行指令列表（`persona_id` 可选——缺省主人格；他人格不可见） |
| POST | `/api/v1/directives` | 创建（body `{content, topic?}`；同 topic 覆盖为 supersede） |
| PUT | `/api/v1/directives/{id}` | 按既有 topic 重写（body `{content}`；supersede 链 +1） |
| DELETE | `/api/v1/directives/{id}` | 删除（策展行删除；不可恢复） |

### 对话捕获（意图快径 DIRECTIVE——R2b）

通道对话里以「记住 / 记得 / 以后都 / 以后 / 别再 / 不要」开头的消息经意图快径
`DIRECTIVE` 类型捕获（见 [chat.md](chat.md) 意图快径一节）：正文经装配注入的
`DirectiveSinkFace` 落库为长期指令（`provenance=owner`），**落库成功才**回复
「已记住：正文」；落库失败 / 未接线时退化为普通附和（不承诺记住——避免下一轮
compose 读不到指令时自相矛盾）。副作用恒不阻断回复链路。

前端消费面（`frontend/src/pages/personas/DirectivesPanel.tsx`）：人格编辑页
「常备指令」卡片——列表 / 新建 / 编辑（supersede）/ 删除（确认）。

## 人物画像（R2c——人物事实 / 证据链 / 更正循环）

人物画像是人格对**主人（对方）**的结构化认知——与长期指令（对人格的行为约定）
分界：指令约束「怎么回应」，事实描述对方「是什么样的人」。每轮对话组装的系统
提示在长期指令块之后注入【关于对方】块（见 [chat.md](chat.md) 组装链）。

**存储决策（锁定）**：**独立表** `ff_persona_facts`（PersonaMixin 人格作用域 +
证据 JSON + supersede 语义）——事实是结构化数据（类别 / 置信度 / 证据回链 /
更正链），策展记忆的自由文本 `topic` 面无法承载类别轴与证据链。

| 项 | 取值 | 说明 |
|---|---|---|
| `category` | preference 偏好 / biography 背景 / relationship 关系 / context 近况 | 封闭值域（DTO 边界拒绝越界） |
| `content` | 1-1000 字符（提取面提案 1-200） | NFKC + 空白折叠 + 裁剪后落库；`normalized_hash` = sha256（事实身份轴） |
| `confidence` | 0-1 | 梦境提取 < 0.3 丢弃（`FACT_MIN_CONFIDENCE`）；手工缺省 0.8 |
| `provenance` | owner / agent | **服务层强制配对**：dream → agent；manual / correction → owner；untrusted 一律拒绝 |
| `source` | dream / manual / correction | 写入来源（面板展示） |
| `evidence` | `[{episode_id, quote ≤200, ts_ms, provenance}]` | 手工事实可无事件背书（episode_id=None）；**摘录由系统从被引用事件正文逐字截取**（梦境面），证据追加合并语义 |

supersede / 更正语义（与策展层同一已验证模式）：事实身份 =
`(persona, category, normalized_hash)`；现行行 `active_marker='active'` 由唯一键
强制（历史 / 驳回行 NULL，两方言唯一键忽略 NULL）。**同身份重复写入 = 元数据
刷新**（置信度取最大值 + 证据合并，不推进版本）；**更正 = 新版本现行行**
（`version` 旧行 + 1、来源 correction、旧行 superseded + `superseded_by_id`
链接，旧证据保留并追加「主人纠正：…」证据）；**驳回 = rejected + 标记清空**
（幂等；同内容可重新提取起链）；**删除 = 硬删除**（清空指向本行的链链接）。

`FactsService` API（人格作用域——调用方先 `bind_persona_context`）：

```python
service = FactsService()                                            # 无状态；Session 逐调用注入
result = await service.upsert_fact(session, FactCreateDTO(...))      # 创建 / 同身份元数据刷新（result.created 区分）
record = await service.correct_fact(session, fact_id, FactCorrectDTO(...))  # 主人更正（supersede 链）
record = await service.reject_fact(session, fact_id)                # 驳回（幂等）
await service.delete_fact(session, fact_id)                         # 硬删除（管理面）
page = await service.list_facts(session, FactQueryDTO(...))         # 游标分页（类别 / 状态 + 置信度降序）
lines = await service.top_facts_for_compose(session)                # compose 注入行（12 条 / 1500 字符帽）
```

证据校验口径：带 `episode_id` 的证据须指向当前人格作用域内真实存在的情景事件；
校验失败 / untrusted 证据按「丢弃该条证据 + 告警」处理，**不**阻断事实写入
（证据是增强面）。他人格事实在读取 / 更正 / 驳回 / 删除面均 `NOT_FOUND`。

### 管理 API 面（app/api/facts.py）

前缀 `/api/v1/facts`；全部响应经 `ResultDTO` 封装，DB Session 经 `get_async_db`
注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/facts` | 事实列表（`category` / `status` 过滤 + `cursor` / `limit`；缺省 active；`persona_id` 可选——缺省主人格） |
| POST | `/api/v1/facts` | 手工录入（body `{category, content, confidence?, evidence?}`；owner 来源） |
| PUT | `/api/v1/facts/{id}` | 主人更正（body `{content, confidence?}`；supersede 链 +1） |
| POST | `/api/v1/facts/{id}/reject` | 驳回（active → rejected；幂等） |
| DELETE | `/api/v1/facts/{id}` | 硬删除（不可恢复） |

前端消费面（`frontend/src/pages/personas/FactsPanel.tsx`）：人格编辑页「了解 TA」
卡片——按类别分组的事实列表（置信度 / 来源徽章、证据展开器含逐字摘录与情景
回链）、新建 / 更正 / 驳回 / 删除。

## 程序性技能（R2c——触发短语 / 原地版本 / 启用状态）

技能是人格对**主人做事方式**的程序性记忆——反复出现的偏好动作（「先给结论再
解释」）、固定工作流（「写周报先列三件事」）、风格约定（「回消息短句」）。
与人物事实的分界：事实描述对方「是什么样的人」，技能描述「事情该怎么做」；
前者是画像，后者是操作指令。启用的技能在每轮对话组装时按**当前用户输入**
匹配命中后注入【相关技能】块（见 [chat.md](chat.md) 组装链）。

**存储决策（锁定）**：**独立表** `ff_persona_skills`（PersonaMixin 人格作用域
+ 触发短语 JSON + slug 唯一键）。**原地版本语义（与事实的 supersede 链刻意
不同）**：技能是操作指令而非信念断言——同一人格同一时刻只按一套指令办事，
旧版全文没有回溯语义价值；因此编辑走原地更新（`version` 递增），不产生历史
行、不链式指向；需要留档时由调用方在编辑前自行导出。

| 项 | 取值 | 说明 |
|---|---|---|
| `name` | 1-120 字符 | 服务层据此派生 `slug`（NFKC 小写 + 非单词字符折叠为 `-`；CJK 保留） |
| `slug` | ≤80 字符 | `(persona_id, slug)` 唯一——手工创建 / 改名撞既有 slug 即 409 `CONFLICT`（不静默覆盖） |
| `description` | 1-500 字符 | 面板展示 + compose 的弱匹配轴（按非单词字符切分的 2-40 字符片段，最多 12 个） |
| `triggers` | JSON 数组 1-8 条、每条 1-40 字符 | 主人可能说出的**字面短语**；compose 匹配只做 NFKC + casefold 后的包含判定，不做模糊推理 |
| `content` | 1-8000 字符（梦境提案 ≤4000） | markdown 步骤正文；compose 注入时最多两条、总字符 2000 帽 |
| `status` | active / disabled | disabled 不参与匹配，仍可查看 / 编辑 / 重新启用 |
| `source` | manual / mined | mined 只标记梦境来源——与手工行同等可编辑 / 停用 / 删除（主人始终有最终控制权） |
| `confidence` | 0-1 | 手工缺省 0.6；梦境提案 < 0.4 丢弃（`SKILL_MIN_CONFIDENCE`） |
| `use_count` / `last_used_ms` | INT / BIGINT | compose 命中后的写读强化：命中次数决定同场景注入优先级；写入是 fire-and-forget（计数失败只告警，不影响回复） |

`SkillsService` API（人格作用域——调用方先 `bind_persona_context`）：

```python
service = SkillsService()                                            # 无状态；Session 逐调用注入
record = await service.create(session, SkillCreateModel(...))        # 手工创建（slug 冲突 → CONFLICT）
record = await service.update(session, skill_id, SkillUpdateModel(...))  # 原地编辑（version +1）
record = await service.set_status(session, skill_id, SkillStatus.DISABLED)  # 启用 / 停用（幂等）
await service.delete(session, skill_id)                              # 硬删除（管理面）
page = await service.list_skills(session, SkillQueryDTO(...))        # 游标分页（状态过滤 + use_count 降序）
hits = await service.match_for_compose(session, user_text)           # 命中技能（≤2 条；触发写读强化）
refs = await service.active_skill_refs(session)                      # 梦境提案的去重参考（名称 / slug）
result = await service.upsert_mined(session, SkillCreateModel(...))  # 梦境写入（result.created / merged）
```

compose 匹配语义：候选集为启用态技能；命中 = 任一触发短语（归一化后）出现在
本轮用户文本中，或描述关键词任一片段出现；排序 = `use_count` 降序、同次数按
名称升序；最多取 2 条（`SKILL_COMPOSE_MAX_SKILLS`）；命中后调度后台任务把
`use_count + 1` / `last_used_ms` 落当前时刻。梦境写入（`upsert_mined`）对同
slug 既有行只做**元数据合并**——置信度取最大值 + 触发短语并集（上限 8 条），
正文 / 名称 / 状态 / 版本均不变（重复挖到同一技能不膨胀版本，主人停用的技能
不会被梦境悄悄重新启用）。证据编号在提案阶段只作有效性校验（技能表不存证据
链——操作指令不是可溯源断言）。

### 管理 API 面（app/api/skills.py）

前缀 `/api/v1/skills`；全部响应经 `ResultDTO` 封装，DB Session 经 `get_async_db`
注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/skills` | 技能列表（`status` 过滤 + `cursor` / `limit`；`persona_id` 可选——缺省主人格） |
| POST | `/api/v1/skills` | 手工创建（body `{name, description, triggers, content, confidence?}`；同名 slug 409） |
| PUT | `/api/v1/skills/{id}` | 原地编辑（body 字段全部可选；version +1） |
| POST | `/api/v1/skills/{id}/enable` | 启用（幂等） |
| POST | `/api/v1/skills/{id}/disable` | 停用（幂等） |
| DELETE | `/api/v1/skills/{id}` | 硬删除（不可恢复） |

前端消费面（`frontend/src/pages/personas/SkillsPanel.tsx`）：人格编辑页「做事
方式」卡片——技能列表（名称 / 描述 / 触发短语徽章 / 状态 / 来源 / 命中次数）、
新建 / 编辑（每行一条触发短语 + markdown 正文）、启用 / 停用、删除。

## 核心记忆（R2c——自编辑自我模型 / 修订链 / 写入闸）

核心记忆是人格的**可自编辑长期自我模型**：固定四个短文本块（自我认知 /
关于对方 / 我们的故事 / 备忘），人格在对话中经 `edit_core_memory` 工具主动
维护，主人可在管理面直接改写或整块锁定。与人物事实 / 程序性技能的分界：

| 域 | 形态 | 写入者 | 历史 |
|---|---|---|---|
| 人物事实 | 多条结构化断言（类别 / 置信度 / 证据链） | 系统提取 + 主人更正 | supersede 链 |
| 程序性技能 | 每技能一条操作指令（触发短语 + 步骤） | 手工 + 梦境挖矿 | 原地版本（无历史行） |
| 核心记忆 | 每块一篇连续自我叙述（固定四块） | 主人管理面 + 人格对话自编辑 | **append-only 修订全文** |

块集合与种子：四个块键为封闭四元组；服务层首次读取（任何读 / 写入口）先
补齐四个空行（`content=''` / `locked=false` / `version=1` /
`updated_by=owner`），并发懒建撞唯一键时静默收敛（另一请求已完成种子）。
空块不进入 compose 注入。

| 项 | 取值 | 说明 |
|---|---|---|
| `block_key` | 四元组（见「值域」） | 「每人格每块至多一行」由唯一键强制 |
| `content` | 0-2000 字符 | 主人可清空；人格写入须非空；服务构造可收窄上限（构造旋钮） |
| `locked` | bool | 主人专属写闸：true 时人格工具调用被拒绝（中文可读反馈），解锁只能经管理面 |
| `version` | 首版 1 | 内容或锁定状态实际变更才递增（零变更保存幂等） |
| `updated_by` / `last_edited_session_id` | owner/agent / BIGINT | 最近写入者与对话内编辑的会话归属（管理面编辑清空会话列） |

**修订语义（append-only，锁定）**：内容变更时同步插入一条修订（写入后全文 +
当次版本号 + provenance + source + session_id + ts_ms）；服务层没有 update /
delete 修订的语句，块删除时由 `ON DELETE CASCADE` 随块清理。仅切换锁定不写
修订（修订记录「文本历史」，锁定是状态位）。配对强制：管理面恒
`owner`/`admin`、工具恒 `agent`/`chat`。

**自编辑工具契约（`edit_core_memory`）**：

```json
{
  "name": "edit_core_memory",
  "arguments": {
    "block": "self_identity",
    "content": "我是一条会写字的鱼，喜欢在深夜陪主人聊天。",
    "mode": "append"
  }
}
```

参数面：`block`（四元组 enum）、`content`（1-2000 字符）、`mode`
（`replace` 整块替换 / `append` 末尾追加——既有正文非空时以换行分隔）。
执行结果（registry 统一终态；工具执行器异常由 registry 归一 `ok=false`）：

```json
{
  "tool": "edit_core_memory",
  "ok": true,
  "result": {
    "ok": true,
    "message": "核心记忆块「自我认知」已更新（当前 28 字）",
    "length": 28
  },
  "error": null,
  "latency_ms": 18
}
```

锁定 / 超限属**业务可读拒绝**（registry 层 `ok=true`、载荷 `ok=false`——模型
可换块或精简后重试）：

```json
{
  "tool": "edit_core_memory",
  "ok": true,
  "result": {
    "ok": false,
    "message": "核心记忆块「自我认知」已锁定，仅主人可修改",
    "length": 28
  },
  "error": null,
  "latency_ms": 6
}
```

超限口径：`append` / `replace` 均按**目标全文**在落库前计长（`append` 为既有
正文 + 换行 + 新段），超限即拒绝且不改任何行，`length` 回传当前正文长度。

**结构性写入闸（不在工具内部）**：聊天服务按本轮发送者来源分级
（`sender_provenance`）从单轮工具列表里移除 `edit_core_memory`——只有
`owner` 回合的模型看得到它；群聊 `peer` 回合的工具面根本没有该工具（模型
无从「忘记不调用」），平台 `untrusted` 内容同理不能改写自我模型。工具执行期
的会话主键经 `core_memory_session_id_var` 绑定（修订 `session_id` 归属），
工具循环结束即复位。

**compose 注入面**：非空块按固定块序渲染【自我认知】块（`### 块名称` + 正文；
锁定块标题追加「（锁定）」），总字符 2400 帽（`CORE_MEMORY_COMPOSE_MAX_CHARS`，
超出即截断停止）；链位置在【关于对方】之后、【相关技能】之前（见
[chat.md](chat.md) 组装链）。注入面仅在持久化会话已落库时读取，读取失败不
阻断回复（日志 + 空块）。

**装配边界（锁定）**：工具只注册在**聊天注册表**（app 通道路径 ChatService
工厂消费；无搜索 / 浏览器工具面时也建表携带）——duty 是无头计划任务，不自
编辑自我模型；CLI 不注入工具面，行为零变化。

### 管理 API 面（app/api/core_memory.py）

前缀 `/api/v1/core-memory`；全部响应经 `ResultDTO` 封装，DB Session 经
`get_async_db` 注入（事务边界归服务层）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/core-memory/blocks` | 四块读取（首次读取懒建空行；`persona_id` 可选——缺省主人格） |
| PUT | `/api/v1/core-memory/blocks/{block_key}` | 主人编辑（body `{content, locked?}`——可清空 / 可锁定 / 可解锁） |
| GET | `/api/v1/core-memory/blocks/{block_key}/revisions` | 修订历史（最新在前；`limit` 1-100，缺省 20） |

PUT 请求与响应示例：

```json
{"content": "主人喜欢无糖乌龙茶，讨厌香菜。", "locked": true}
```

```json
{
  "success": true,
  "code": "200",
  "message": "核心记忆已保存",
  "data": {
    "id": 2,
    "block": "owner_profile",
    "content": "主人喜欢无糖乌龙茶，讨厌香菜。",
    "locked": true,
    "version": 4,
    "updated_by": "owner",
    "last_edited_session_id": null,
    "created_at": 1731000000000,
    "updated_at": 1731000000000
  }
}
```

修订列表示例（单条）：

```json
{
  "success": true,
  "code": "200",
  "message": "核心记忆修订已获取",
  "data": [
    {
      "id": 12,
      "block": "owner_profile",
      "version": 4,
      "content": "主人喜欢无糖乌龙茶，讨厌香菜。",
      "provenance": "owner",
      "source": "admin",
      "session_id": null,
      "ts_ms": 1731000000000,
      "created_at": 1731000000000
    }
  ]
}
```

前端消费面（`frontend/src/pages/personas/CoreMemoryPanel.tsx`）：人格编辑页
「自我认知」卡片——四块编辑卡（正文 + 锁定开关 + 保存 + 版本 / 写入者标识）
+ 修订历史抽屉（最新在前，展示版本 / 写入者 / 来源 / 全文；空块与已清空正文
有明确占位）。

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
| GET | `/api/v1/personas/presets` | 官方特质库读取（`{presets, components}` 画廊载荷；装载失败 500） |
| POST | `/api/v1/personas/import` | 导入（JSON：`{preset_id}` 官方预设生成 / `{card}` 用户卡片 + `conflict_strategy` + `desired_name`；multipart：`file` + 同名表单字段） |
| POST | `/api/v1/personas/import/batch` | 批量导入（multipart `files` 多文件 + `conflict_strategy`；部分成功形态） |
| GET | `/api/v1/personas/{id}/card` | 卡片读取 |
| PUT | `/api/v1/personas/{id}/card` | 卡片整体替换（body 为完整卡片） |
| GET | `/api/v1/personas/{id}/states` | 已写状态层列表 |
| PUT | `/api/v1/personas/{id}/states/{layer}` | 状态层 upsert（layer ∈ {M, S}；body 为 `{"state": {...}}`） |

错误映射（`app/middleware/error_handler.py` 共享处理器，app 装配点经 `register_exception_handlers(app)` 挂接）：`BusinessException` → HTTP 状态取 ErrorCode 映射（404 / 409 / 422 / 500 …）+ `ResultDTO` error 形态（`data` 携带 details；`headers` 透传）；请求校验失败（参数 / 请求体 / 路径枚举）→ **HTTP 400** + `code=VALIDATION_ERROR`（`data.errors` 为 `[{loc, msg, type}]`）。

## 测试与门禁

- 测试位置：`src/tests/unit/test_facts_service.py`（人物事实：归一化 / 身份指纹 / 创建与元数据刷新 / provenance 配对 / 证据校验丢弃 / 更正链与证据追加 / 驳回幂等 / 删除引用清理 / 列表过滤与游标 / compose 裁剪 / 人格隔离）/ `test_facts_api.py`（人物事实 HTTP 面：CRUD + 驳回 + 过滤 / 游标 + 隔离 404）/ `test_migration_persona_facts.py`（迁移 0013 SQLite round-trip）/ `test_skills_service.py`（技能：slug 归一 / 创建与同名冲突 / 触发短语归一 / 原地编辑与冲突 / 状态切换幂等 / 删除 / 列表过滤与游标 / compose 字面与描述命中 / 条数帽与排序 / 使用计数写入 / 梦境元数据合并与并集上限 / 人格隔离）/ `test_skills_api.py`（技能 HTTP 面：CRUD + 启停 + 同名 409 + 边界 / 游标 + 隔离 404）/ `test_migration_persona_skills.py`（迁移 0014 SQLite round-trip）/ `test_core_memory_service.py`（核心记忆：懒建四块 / 主人编辑与修订 / 锁定切换无修订 / 零变更幂等 / 构造上限 / 人格 replace 与 append / 锁定可读拒绝 / 目标全文超限不改行 / 修订倒序与截断 / append-only / 人格隔离）/ `test_core_memory_tool.py`（edit_core_memory：工具规格与 enum / 执行落会话归属 / 锁定可读拒绝 / 人格上下文缺失 / 非法块键）/ `test_core_memory_api.py`（核心记忆 HTTP 面：四块 / 编辑 / 修订 / 上限 400 / 非法块键 400 / 无主人格 404 / 跨人格隔离）/ `test_migration_core_memory.py`（迁移 0015 SQLite round-trip——两表 / 唯一键 / 级联外键 / 索引）/ `test_chat_core_memory.py`（见 chat.md）/ `test_chat_skills.py`（见 chat.md）/ `test_dreaming_skills.py`（见 memory.md）/ `src/tests/unit/test_directives_service.py`（长期指令：topic 派生 / 同文收敛 / 显式 topic 覆盖 / 边界 / 命名空间收窄 / 注入帽 / 链更新 / 删除 / 人格隔离）/ `test_directives_api.py`（长期指令 HTTP 面：CRUD + 隔离 404）/ `src/tests/unit/test_persona_chara_card.py`（卡片规格纯单元：v2 基础校验 + R2b 高级字段界校验 / include_group 归一 / recursive 总开关规格一致性 / 加性字段 JSON 往返）/ `test_persona_service.py`（服务面：主人格事务性切换 / 并发收敛（唯一键 + 冲突重试 + gather 双切恰一主人格）/ 停用守卫 / 复制含状态 / 唯一名冲突 / 删除守卫 / 可寻址集 / 状态 upsert / 批量部分成功 / 提交失败回滚）/ `test_persona_trait_library.py`（R2b-2 特质库：组件 / 预设模型规格（kebab / 分类值域 / 空组合拒绝）+ 纯函数生成（后缀拼接 / 分号拼接序 / 指令行拼接 / `<START>` 台词块 / 建议世界书并入 / 冲突软警告双向去重 / 未知组件 fail loud）+ 官方库装载（真实随包 YAML——8 预设名单序 / 全引用合法 / 发行零冲突 / 缓存命中）+ 显式目录违约形态 fail loud（缺失 / 结构 / 重复 id / 未知引用 / 发行冲突预设 / 空库））/ `test_persona_import_service.py`（R2b-2 导入服务：判定序（内容同卡三策略恒幂等）+ 名称冲突三策略矩阵（skip / rename 自动后缀链 / overwrite 保状态保标记保语音）+ 期望名 / 载荷恰一 + 批量部分成功（合法跳过成功项 / 解析失败失败项 / 计数面））/ `test_persona_import_api.py`（R2b-2 导入 HTTP 面：预设库读取 + JSON / multipart 双面矩阵（预设生成 / 期望名 + 后缀 / 同卡幂等 / 冲突三策略 / 未知预设 404 / 载荷恰一 422 / 策略非法 422 / 空体 400 / 非法 JSON 400/422）+ 批量部分成功（rename 策略表单字段）+ 处理器直调契约面）/ `test_persona_api.py`（HTTP 面 ASGITransport + 错误映射 + 批量部分成功；处理器直调契约面）/ `test_app_error_handler.py`（异常处理器映射：状态码 / ResultDTO 形态 / headers 透传 / 400 校验形态 / 幂等注册）；世界书选择算法链的运行语义测试在 chat 域 `test_chat_world_book.py`（见 chat.md）；
- 全部 `async def test_`；引擎经共享会话级 SQLite fixture（StaticPool 单连接 + 每测试自动清表）；HTTP 面零真实网络（ASGITransport 本地装配 app）；预设库装载测试用真实随包 YAML（仓库数据即 hermetic）；
- 覆盖率（scoped 实测）：harness/persona ≥95%（含人物事实 / 技能 / 核心记忆面）、app/api ≥85%、app/middleware **100%**；
- 运行：`cd backend && pixi run tests-unit`。
