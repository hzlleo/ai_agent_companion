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
| 覆盖率门 | `pixi run coverage-gates` | 按模块 ≥80% enforce |
