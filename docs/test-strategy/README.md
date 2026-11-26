# 测试策略

## 铁律

1. **全量原生异步**：所有测试 `async def test_*`（`pytest asyncio_mode=auto` 零样板）；同步测试零容忍——`scripts/count_sync_tests.py --enforce` 棘轮把关（基线 0）。
2. **不依赖真实配置，也不用默认配置**：根 `src/conftest.py` 硬覆盖环境变量 + CWD scratch 隔离；每个测试经显式 fixture 工厂构造配置（`isolated_settings` 模式，随 config 模块交付）。
3. **多测试依赖资源共享**：SQLite StaticPool 会话级引擎共享 + 自动清表；HTTP MockTransport 共享；任何测试不得私建池。
4. **覆盖率按模块 ≥80%**：`scripts/check_coverage_per_module.py --enforce`（harness 子包包级 + app 层分组，聚合口径为语句数求和而非百分比均值）。
5. **禁止为过测试改实现**；测试与实现冲突时先论证哪边是 bug。
6. **实现类子 Agent 只跑自己改动涉及的测试**；全量测试（`pixi run tests-all`）只能由主 Agent 执行。
7. 所有测试命令经 pixi：`cd backend && pixi run tests-unit / tests-integration / tests-all`。

## 门禁清单（CI 全跑）

| 门禁 | 命令 | 语义 |
|---|---|---|
| 中文注释门 | `pixi run comment-gates` | 公开模块/类/函数 docstring 必须存在且含中文（Google 风格段落缺失仅 hint） |
| 同步测试棘轮 | `pixi run sync-test-ratchet` | 同步 `def test_` 只降不升，基线 0 = 零容忍 |
| noqa 冻结 | `pixi run noqa-freeze` | `# noqa` / `# type: ignore` 只减不增，基线 0 = 零容忍 |
| 沙盒 DTO 漂移门 | `pixi run sandbox-drift` | 沙盒 wire 契约 16 项文本镜像比对（backend `sandbox_models.py` ↔ `docker/tool-sandbox-agent` 树定义块——8 模型块 + 8 常量行，任一侧漂移即红） |
| DTO schema fixture 门 | 后端 `test_dto_schema_fixture.py` + 前端 `dtoContract.test.ts` | 49 组共享 wire 契约双侧比对（fixture 经 `pixi run export-dto-schemas` 再生，后端改动未重导出 / 前端类型漂移均即红） |
| 覆盖率门 | `pixi run coverage-gates` | 按模块 ≥80% enforce |
| SeekDB 真引擎套件 | CI 专用步骤 `pixi run pytest src/tests/integration_seekdb -v --asyncio-mode=auto`（本地 `pixi run tests-seekdb`） | 迁移链 round-trip + 并发不变式 + 混合检索往返——真引擎强制（缺 env 即 `pytest.fail`，不跳过） |

## SeekDB 真引擎强制套件（src/tests/integration_seekdb/）

**不跳过策略**：套件目标经 runner 显式注入的 `SEEKDB_TEST_*` env 解析（`SEEKDB_TEST_URL=host:port`、`SEEKDB_TEST_USER` 默认 `root`、`SEEKDB_TEST_PASSWORD` 默认空、`SEEKDB_TEST_DATABASE` 默认 `aac_ci_verify`）；缺失 `SEEKDB_TEST_URL` 即 `pytest.fail`（失败消息携带本地容器启动命令）——无静默跳过，替代原 env 门控默认跳过的 `test_seekdb_smoke.py`（已删除，混合检索往返契约整体迁入）。套件**不读** `src/conftest.py` 的 S3 硬覆盖 env 面——docker 化测试依赖 + 显式测试 env 是本工程认可的测试服务模式（同 MySQL service container），不属于「真实项目配置」。

**覆盖面**（四模块；scratch 库每模块 DROP+CREATE 重建——hermetic per run，单进程顺序运行）：

- 迁移链 round-trip：alembic 0001→0008 升级 head（`SHOW TABLES` 收敛 9 表集含 `alembic_version`，版本戳 0008）→ 降级 base → 复升 head；
- 并发不变式：`uq_ff_personas_primary` 单主人格（NULL 标记多行共存 + 第二 `'primary'` 被 1062 点名拒绝）、`uq_ff_curated_active` 复合单 active（superseded NULL 多代共存）、`uq_ff_chat_messages_seq` 会话内序号唯一、跨连接 `SELECT ... FOR UPDATE` 持锁 / 等锁超时（错误 1205）/ 释放正控、表情双形态收敛（旧快照表预读 → 6235 → 调用方重试收敛 / 无旧快照 → 1062 → 共享锁定读解析胜者）——全部 raw SQL / 服务层共用插入函数经 aiomysql 直击真引擎；
- 混合检索往返：`SeekDBStore` 生产路径 `ensure_schema` → 批量 upsert → keyword / hybrid（Form B 融合排序）检索回读 → 同键覆盖幂等 → 人格隔离 → delete 清理；
- 单栈权威引擎：`DatabaseConfig(type=mysql, host/port=真引擎, name=专用 scratch 库)` 走 `init_database` + Alembic 升级 head + 人格创建 / 读取 / 删除 round-trip（权威存储实际跑在 SeekDB，不触碰套件库）。

**方言 bug 起源（制度化理由）**：迁移降级序在 MySQL 协议系引擎（含 SeekDB）受错误 5320（外键列须保有索引——须先删外键约束再删其索引）约束，SQLite 方言永不暴露该序错误，真引擎 round-trip 验证即为此制度化；同理行锁等待的生效旋钮是 `ob_trx_lock_timeout`（微秒）——`innodb_lock_wait_timeout` 在 SeekDB 为 mock 变量（live 实测读回恒 50、`SET SESSION` 无效）。

**本地运行**：

```bash
# 1. 启动本地 SeekDB（server 模式——MySQL 线协议 2881；root 空密码）
docker run -d --name seekdb-smoke -p 2881:2881 \
  oceanbase/seekdb:1.4.0-100000172026082615

# 2. 运行套件（测试 env 由 pixi 任务注入）
cd backend && pixi run tests-seekdb
```

**CI 接线**：backend job 挂 SeekDB service container（同镜像，2881:2881）+ job 级 `SEEKDB_TEST_*` env + 就绪等待步骤（容器初始化需 ~60-90s，探针重试连接最长 180s——service 容器无 healthcheck 语义）+ 专用步骤显式定向运行套件。`tests-all` **不含**本套件（pyproject `norecursedirs` 排除出 `src/tests/` 扫集）——分立即「CI 全跑、本地无容器快速回路保持可跑」；`coverage-gates` 的逐模块分组本就跳过 tests 路径，套件不进覆盖率门。
