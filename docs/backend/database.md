# database — 数据库域（P0-3 交付）

`backend/src/packages/harness/database/` 承载 SQLAlchemy 2.0 async 引擎工厂（MySQL 8 权威 / SQLite 测试）、声明式基类与 Mixin（含 PersonaMixin 人格作用域自动隔离）、会话门面与引擎持有器；`backend/alembic/` 承载异步迁移环境。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `engine.py` | 引擎工厂（方言分派）+ 生命周期（init / close / rebuild） | `create_database_engine` / `init_database` / `close_database` |
| `base.py` | 声明式 Base + Mixin + PersonaMixin 自动隔离 event listener | `Base` / `PersonaMixin` / `TimestampMixin` |
| `session.py` | 会话工厂获取 / session_scope / 依赖注入形态 | `session_scope` / `get_async_db` |
| `units/engine_holder.py` | 引擎/会话工厂槽位持有器（所有权治理） | `EngineHolder` / `db_engine_holder` |
| `backend/alembic/` | 异步迁移环境 + 初始基线迁移（本 chunk 无域表） | `pixi run alembic upgrade head` |

依赖方向：database → config（DatabaseConfig）+ core（context / errors / time_utils），合法。

## 方言分派（锁定决策）

| 方言 | 触发 | URL 形态 | 池语义 |
|---|---|---|---|
| MySQL 8（权威，aiomysql） | `DATABASE_TYPE=mysql` | `mysql+aiomysql://user:pwd@host:port/db?charset=utf8mb4` | `pool_size` / `max_overflow` / `pool_recycle`（防 wait_timeout 断连）/ `pool_pre_ping` 全量生效 |
| SQLite（测试，aiosqlite） | `DATABASE_TYPE=sqlite` | `sqlite+aiosqlite:///{name}` | `name=":memory:"` → `StaticPool`（单连接共享——SQLite 内存库 per-connection 语义的必然选择）；文件库默认 QueuePool |

`BigIntIdMixin` 主键在 SQLite 方言渲染为 INTEGER（`with_variant`）——SQLite 仅对 `INTEGER PRIMARY KEY` 启用 rowid 自增；MySQL 侧保持 BIGINT AUTOINCREMENT。

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
| `get_async_db()` | FastAPI `Depends` 形态 async generator（P0-6 装配；本模块不 import FastAPI） |

Session 关闭防御：请求任务取消与清理竞争时 `close()` 可能抛 `IllegalStateChangeError` → 降级 `invalidate()` 丢弃底层连接（连接池 pre_ping / recycle 兜底），不向调用方注入新异常。

## alembic 迁移

```
cd backend
pixi run alembic revision --autogenerate -m "..."   # 生成新迁移（域 chunk 落地模型后在 env.py 补 import）
pixi run alembic upgrade head                        # 应用迁移
```

- `alembic/env.py`：异步引擎（`run_sync` 桥接）+ `sqlalchemy.url` 由 `settings.database.async_sqlalchemy_url` 动态注入（alembic.ini 留空）+ `sys.path` 注入 backend/src；
- `versions/000000000001_initial.py`：空基线（本 chunk 无域表——版本链锚点；persona / chat / memory 等域表由后续 chunk 迁移创建）；
- `target_metadata = Base.metadata`（autogenerate 对比基准）。

## 测试与门禁

- 测试位置：`src/tests/unit/test_database_base.py` / `test_database_engine.py` / `test_database_session.py`（3 份）；ORM 测试模型在 `src/tests/db_models.py` 唯一定义；
- 共享 DB fixture（铁律 3 落地点，`src/tests/conftest.py`）：**会话级** SQLite in-memory + StaticPool 引擎（一次性 `create_all`）+ **autouse 每测试自动清表**（逆依赖序 DELETE，保留 schema）——aiosqlite future 按调用在当前事件循环创建并由工作线程回调，引擎跨 function 级测试循环共享安全（已实证）；
- PersonaMixin 测试证明：自动注入 / 显式保留 / 无上下文 fail loud（读写两路径）/ contextvars 并发任务隔离 / skip 绕过通道 / 非人格表零影响 / 聚合查询同样过滤；
- 覆盖率（`pixi run tests-unit` 实测）：harness/database 组 **93.5%**；
- 运行：`cd backend && pixi run tests-unit`。
