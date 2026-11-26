# database — 数据库域

`backend/src/packages/harness/database/` 承载 SQLAlchemy 2.0 async 引擎工厂（SeekDB 权威存储（MySQL 协议，aiomysql 驱动） / SQLite 测试）、声明式基类与 Mixin（含 PersonaMixin 人格作用域自动隔离）、会话门面与引擎持有器；`backend/alembic/` 承载异步迁移环境。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `engine.py` | 引擎工厂（方言分派）+ 生命周期（init / close / rebuild） | `create_database_engine` / `init_database` / `close_database` |
| `base.py` | 声明式 Base + Mixin + PersonaMixin 自动隔离 event listener | `Base` / `PersonaMixin` / `TimestampMixin` |
| `session.py` | 会话工厂获取 / session_scope / 依赖注入形态 | `session_scope` / `get_async_db` |
| `units/engine_holder.py` | 引擎/会话工厂槽位持有器（所有权治理） | `EngineHolder` / `db_engine_holder` |
| `backend/alembic/` | 异步迁移环境 + 初始基线迁移（基线无域表） | `pixi run alembic upgrade head` |

依赖方向：database → config（DatabaseConfig）+ core（context / errors / time_utils），合法。

## 方言分派（锁定决策）

| 方言 | 触发 | URL 形态 | 池语义 |
|---|---|---|---|
| SeekDB（权威，MySQL 协议 aiomysql） | `DATABASE_TYPE=mysql` | `mysql+aiomysql://user:pwd@host:port/db?charset=utf8mb4` | `pool_size` / `max_overflow` / `pool_recycle`（防 wait_timeout 断连）/ `pool_pre_ping` 全量生效；默认端口 2881（SeekDB MySQL 线协议） |
| SQLite（测试，aiosqlite） | `DATABASE_TYPE=sqlite` | `sqlite+aiosqlite:///{name}` | `name=":memory:"` → `StaticPool`（单连接共享——SQLite 内存库 per-connection 语义的必然选择）；文件库默认 QueuePool |

`BigIntIdMixin` 主键在 SQLite 方言渲染为 INTEGER（`with_variant`）——SQLite 仅对 `INTEGER PRIMARY KEY` 启用 rowid 自增；MySQL 协议方言侧保持 BIGINT AUTOINCREMENT。

**单栈 SeekDB（锁定）**：权威库与检索库同实例、不同库名——`DatabaseConfig` 即 SeekDB 连接（默认 `127.0.0.1:2881` / root / 空密码 / `ai_agent_companion`），`SeekDBSettings` 的 host / port / user / password 留空即继承权威库，显式设置时必须与权威库一致（启动期 `BusinessException(CONFIG_INVALID)` fail loud；拆分实例需另立决策）。不变式按 host / port 字面比较，`DATABASE_TYPE=sqlite` 时同样执行（dev 同口径，无方言豁免；仅在 `seekdb.enabled=true` 时校验）。行锁语义沿用 MySQL 协议 / SeekDB 术语——`innodb_lock_wait_timeout` 仅为 SeekDB 兼容占位变量，生效旋钮是 `ob_trx_lock_timeout`。

## Mixin 字段表

| Mixin | 注入列 | 说明 |
|---|---|---|
| `BigIntIdMixin` | `id: BigInteger PK autoincrement` | 全工程统一主键形态（SQLite 方言 INTEGER 变体） |
| `TimestampMixin` | `created_at` / `updated_at: BigInteger` | epoch 毫秒（`default=now_ms` / `onupdate=now_ms` callable）；DB 侧 server_default 由 Alembic 迁移层负责 |
| `PersonaMixin` | `persona_id: String(64) index` | 人格作用域键——写路径自动注入 + 读路径自动过滤（见下） |

## PersonaMixin 人格隔离链（顺序）

```
写路径（INSERT）  before_flush（Session 事件）
    新 PersonaMixin 实例缺 persona_id？
        ├─ 是 → 从 persona_id_var 注入当前人格（业务代码零显式赋值）
        │        persona_id_var 未设置 → BusinessException(PERSONA_CONTEXT_REQUIRED)
        └─ 否（显式指定）→ 原样保留（跨人格系统写入合法通道）

读路径（SELECT）  do_orm_execute（Session 事件）
    语句触及 PersonaMixin 实体？（column_descriptions entity +
    FROM 子句表名反查 ORM 注册表双通道——覆盖实体 select 与
    func.count()+select_from 聚合形态）
        ├─ 是 → persona_id_var 有值 → with_loader_criteria 附加
        │        WHERE persona_id = <当前人格>（include_aliases）
        │        persona_id_var 未设置 → BusinessException(PERSONA_CONTEXT_REQUIRED)
        └─ 否（非人格作用域表）→ 零影响

绕过通道（管理面 / 跨人格聚合）
    session.execute(stmt, execution_options={"skip_persona_filter": True})
```

两个守卫均 fail loud：触及人格作用域实体而无人格上下文是编程错误（入口层必须先经 `core.context.bind_context(persona_id=...)` 绑定），静默放行会造成全表可见的数据泄漏。contextvars 传播与入口层一致（asyncio 任务天然隔离——并发任务各持各的人格上下文）。

## 引擎生命周期（顺序）

```
lifespan startup:
    init_database(config=None)      # None = settings.database
        1. 按所有权令牌 dispose 旧引擎（owner_token=None 的外部安装引擎不动，仅告警）
        2. create_database_engine(config) → 写入 db_engine_holder 槽位
        3. session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        4. owner_token 刷新（uuid4.hex——处置凭证）
        5. create_all fallback（checkfirst 幂等——开发环境零迁移可用；
           生产 schema 演进以 Alembic 为准）

lifespan shutdown:
    close_database()
        1. 生产所有权引擎 dispose（外部安装引擎不动，仅告警）
        2. 持有器恒 None 哨兵复位（从不回放「关闭前捕获的值」）

配置变更热重建:
    rebuild_database_engine(config)  # 槽位整体交换 + 令牌刷新
```

`EngineHolder`（`db_engine_holder` 单例）三槽位：`async_engine` / `session_factory` / `owner_token`。全写经 `__setattr__` 记 debug 日志（可观测）；`__slots__` 保证未知槽位名即刻 AttributeError。测试 monkeypatch 直接写槽位（owner_token 留 None 即外部安装语义）。

## 会话门面（session.py）

| API | 语义 |
|---|---|
| `get_session_factory()` | 持有器槽位调用时读；未初始化 → `INTERNAL_ERROR`（含初始化指引） |
| `session_scope()` | `async with` 作用域：正常退出 commit / 异常 rollback / 恒关闭（服务层主入口） |
| `get_async_db()` | FastAPI `Depends` 形态 async generator（app 装配消费；本模块不 import FastAPI） |

Session 关闭防御：请求任务取消与清理竞争时 `close()` 可能抛 `IllegalStateChangeError` → 降级 `invalidate()` 丢弃底层连接（连接池 pre_ping / recycle 兜底），不向调用方注入新异常。

## alembic 迁移

```
cd backend
pixi run alembic revision --autogenerate -m "..."   # 生成新迁移（新域模型落地后在 env.py 补 import）
pixi run alembic upgrade head                        # 应用迁移
```

- `alembic/env.py`：异步引擎（`run_sync` 桥接）+ `sqlalchemy.url` 由 `settings.database.async_sqlalchemy_url` 动态注入（alembic.ini 留空）+ `sys.path` 注入 backend/src；
- `versions/000000000001_initial.py`：空基线（版本链锚点）；域表迁移链：`0002_add_usage_records`（用量）→ `0003_add_personas`（人格注册表 + 状态）→ `0004_add_memory_tables`（记忆四表）→ `0005_add_dream_runs`（梦境运行日志）→ `0006_add_persona_voice_profile`（人格音色绑定）→ `0007_memory_concurrency_constraints`（策展单 active 唯一键 + 会话消息 seq 唯一键——memory 域并发不变式）→ `0008_persona_primary_marker`（persona 域多激活可寻址模型：`ff_personas` 增 `is_primary_marker` 可空列 + 唯一键 `uq_ff_personas_primary`，旧 `is_active` 单激活位退役——旧激活行回填 `'primary'` 后删列，降级逆向重建；与 0007 同「NULL 共存唯一键」模式）→ `0009_add_chat_message_meta`（会话消息 meta 列）→ `0010_chat_message_variants`（消息变体列）→ `0011_add_reminders`（提醒表）→ `0012_curated_lifecycle`（策展生命周期列）→ `0013_persona_facts`（人物事实）→ `0014_persona_skills`（程序性技能）→ `0015_core_memory`（核心记忆块 + append-only 修订）→ `0016_voiceprint_sticker`（声纹样本 / 同意记录 / 表情包库三表）→ `0017_canvas_documents`（画布产物表 + 人格 / 更新时间复合索引）→ `0018_persona_facts_confidence_index`（人物事实 `(persona_id, status, confidence)` 复合索引）→ `0019_dream_runs_single_flight`（梦境运行日志 `running_marker` 可空列 + `(persona_id, running_marker)` 唯一键——单飞守卫）；
- `target_metadata = Base.metadata`（autogenerate 对比基准）。

### 0017_canvas_documents（画布产物）

`ff_canvas_documents`（PersonaMixin 人格作用域）：`title` / `kind`（html / svg / markdown）/ `content`（自包含文本）/ `revision`（创建 1、修改递增）/ `created_at` / `updated_at`（epoch 毫秒）。索引：`ix_ff_canvas_documents_persona_id`、`ix_ff_canvas_documents_persona_updated (persona_id, updated_at)`（「当前人格最近修改」列表查询）。无唯一键——同名产物合法（标题是展示面，主键与修订号才是标识）。降级删索引后删表（数据整体丢弃）。

### 0018_persona_facts_confidence_index（人物事实复合索引）

为 `ff_persona_facts` 增加 `ix_ff_persona_facts_persona_status_confidence (persona_id, status, confidence)`：服务层按人格作用域 + 状态过滤、再按置信度排序读取现行事实面（管理面板 / compose 注入候选 / 关系摘要派生）。表时间列为 epoch 毫秒（`created_at` / `updated_at` BigInteger），`confidence` 为 0-1 浮点。升级仅建索引、无数据迁移；降级仅删索引、事实行保留。

### 0019_dream_runs_single_flight（梦境运行单飞守卫）

为 `ff_dream_runs` 增加可空 `running_marker`（String(16)）与唯一键 `uq_ff_dream_runs_persona_running (persona_id, running_marker)`：运行中行写固定 `'1'`，终态 / 失败 / 进程崩溃收敛清 NULL；MySQL / SQLite 唯一键均忽略 NULL，历史运行行多代共存。服务层 `DreamService.run_dream` 以插入带标记行为单飞动作，并发第二行被 1062 拒绝后回滚并返回既有在途运行。升级顺序为增列 → 按人格回填最新运行中行 → 建唯一键；降级先删约束再删列（运行行保留，标记数据丢弃）。唯一键名与语义见 [memory.md](memory.md#夜间梦境记忆巩固)。

## 人格级联删除（服务层批删，无跨域 FK）

记忆域 / 画像域表与 `ff_personas` 之间**没有跨域外键**（PersonaMixin 是逻辑作用域列，不是 FK）——人格删除的级联由 `PersonaService.delete` 在单事务内按 `persona_id` 批删，**范围从 `Base.metadata` 派生**：所有含 `persona_id` 列的表自动纳入（当前含状态 / 情景 / 策展 / 梦境 / 事实 / 技能 / 核心记忆块 / 会话 / 表情包 / 声纹样本与同意 / 提醒 / 画布），无 persona_id 的依赖表按归属轴清理（聊天消息经 session_id、核心记忆修订经 block_id），回退索引 `ff_memory_index_lite` 同事务按 persona_id 删除，SeekDB 生产索引经注入 indexer 提交后 best-effort 清理；`ff_usage_records` 有意保留（跨人格聚合真源）。磁盘文件（表情 / 声纹样本）在提交后 best-effort 删除。这条服务层级联是方言一致性的唯一保证（SQLite 测试方言默认不启用 FK 强制；DB 级 CASCADE 仅作直删 SQL 兜底）；结构测试守护「派生集合 == 元数据全集」。范围与逐表处理见 [persona.md](persona.md#删除级联范围personaservicedelete)。

## 测试与门禁

- 测试位置：`src/tests/unit/test_database_base.py` / `test_database_engine.py` / `test_database_session.py`（3 份）；ORM 测试模型在 `src/tests/db_models.py` 唯一定义；
- 共享 DB fixture（铁律 3 落地点，`src/tests/conftest.py`）：**会话级** SQLite in-memory + StaticPool 引擎（一次性 `create_all`）+ **autouse 每测试自动清表**（逆依赖序 DELETE，保留 schema）——aiosqlite future 按调用在当前事件循环创建并由工作线程回调，引擎跨 function 级测试循环共享安全（已实证）；
- PersonaMixin 测试证明：自动注入 / 显式保留 / 无上下文 fail loud（读写两路径）/ contextvars 并发任务隔离 / skip 绕过通道 / 非人格表零影响 / 聚合查询同样过滤；
- 覆盖率（`pixi run tests-unit` 实测）：harness/database 组 **93.5%**；
- 运行：`cd backend && pixi run tests-unit`。
