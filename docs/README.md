# ai_agent_companion 文档索引

文档铁律：**贴合代码**——每个模块文档包含完整 JSON 示例、字段说明表、链路顺序；代码变更未同步文档视为未完成。

## 目录

| 路径 | 内容 |
|---|---|
| [backend/](backend/README.md) | 后端模块文档（core / config / database / pools / llm / persona / memory / chat / usage / duty / channels / voice / multimodal / tools-sandbox …按模块逐份；safety 审批面规划中暂无文档） |
| [test-strategy/](test-strategy/README.md) | 测试策略（全异步 / 无真实配置 / 共享资源 / 覆盖率门 / 棘轮门） |
| [models.md](models.md) | 模型子模组（ModelScope 源清单 / UA 覆写 / lfs 拉取 / 升级流程） |
| [TASKBOOK_TEMPLATE.md](TASKBOOK_TEMPLATE.md) | 任务书模板（文件白名单 / 机器化 DoD / 六路接线矩阵 / 既有测试改动申报 / 反模式黑名单） |
| [ACCEPTANCE_SOP.md](ACCEPTANCE_SOP.md) | 验收 SOP（证据包门槛 / 随机红探针 / 越权与弱化审计 / 证伪型审查者提示词 / 复审模板） |

## 工具与门禁

工程门禁分两层：`pixi run gates`（根级链式门禁，CI 与本地同一口径）与
`pixi run verify-slice`（全链逐步执行 + 证据包落盘）。所有门禁脚本只用
Python 标准库，经 backend pixi 环境的 python 执行；基线与允许清单只允许
在评审后下调 / 精简，不允许静默上调。

| 入口 | 作用 | 命令 |
|---|---|---|
| `scripts/red_probe.py` + `backend/red_manifest.toml` | 红探针：临时副本 AST 源码体替换后跑登记的 node id，要求至少一红；门禁模式要求新源文件被 probe 覆盖或在冻结基线；清单外符号可用 `--tests` 走一次性探针（不写清单，`errors>0 且 failed==0` 会标注「疑似导入期假红」） | `cd backend && pixi run red-probe --symbol <module:attr>`（追加 `--tests <nodeid>`）/ `pixi run red-probe-gate` |
| `scripts/verify_slice.py` | 切片验收证据包：tests-all / coverage-gates / lint / typecheck / gates 逐步校验退出码与尾部摘要，日志与 JSON 落 `docs/evidence/`；哈希剔除 `.egg-info` / 缓存 / 依赖目录，干净副本可复算同一 sha8 | `cd backend && pixi run verify-slice -- --slice <slice>` |
| `scripts/generate_examples_env.py` | `.env.example` 生成 + `docs/backend/config.md` env 分节表同步 / 漂移校验 | `--sync-docs` / `--check-docs`（经 `pixi run docs-truth`） |
| `scripts/todo_freeze_check.py` | docs 未完成标记（TODO / 待办 / 待定 / 暂未）只降不升棘轮 | `cd backend && pixi run docs-truth` |
| `scripts/assertion_freeze.py` | 既有单测 assert / pytest.raises 计数只降不升（AST 摘要变化报告） | `cd backend && pixi run assertion-freeze` |
| `scripts/surface_check.py` + `scripts/.surface_allowlist.json` | app/api wire 模型导出面完整性（未登记须允许清单留 reason）+ `.env.example` 键面复核；允许清单受 `max_entries` 规模棘轮约束（超限即红，新增豁免须 `--update` 同步抬高） | `cd backend && pixi run surface-check` / `pixi run python ../scripts/surface_check.py --update` |
| `scripts/prose_pin_check.py` | prose-pin 启发式报告：命中文案分 field_or_code / prose / other 三桶，「疑似 prose pin」只计 prose 桶、Top-N prose 优先，另报纯结构断言占比，不阻断 | `cd backend && pixi run prose-pin` |
| `scripts/count_sync_tests.py` | 同步测试只降不升棘轮 | `cd backend && pixi run sync-test-ratchet` |
| `scripts/noqa_freeze_check.py` | noqa / type:ignore 增量冻结 | `cd backend && pixi run noqa-freeze` |
| `scripts/check_sandbox_dtos_drift.py` | 沙盒 / 浏览器 sidecar wire 契约文本镜像漂移 | `cd backend && pixi run sandbox-drift` |
| `scripts/check_chinese_comments.py` | 注释门（docstring 存在性 + 中文 + Google 段落提示） | `cd backend && pixi run comment-gates` |
| `scripts/check_coverage_per_module.py` | 按模块覆盖率门禁（≥80% enforce） | `cd backend && pixi run coverage-gates` |
| `scripts/export_dto_schemas.py` | 后端 DTO JSON Schema fixture 导出（前端契约漂移门数据源） | `cd backend && pixi run export-dto-schemas` |

## 路线图（历史规划——P0 至 P4 已全部落地，保留作交付历程记录）

| 阶段 | 内容 |
|---|---|
| P0 | 工程骨架 + core/config/database/pools + LLM 网关 + 多人格注册表 + 控制台聊天 + usage 域 |
| P1 | 三层记忆 + 日记 + 夜间梦境 + SeekDB 统一检索接入 |
| P2 | 前端管理后台（React 19 + Vite）+ 全量管理 API |
| P3 | 平台通道（Telegram 先行）+ 值班轮 + 审批门 + docker 工具沙盒树 |
| P4 | 语音（CosyVoice3 + Qwen3-ASR 独立本地服务）+ look_at 视觉工具 + B站/QQ 通道 |

## 已知事项

1. `.env` / `config.yaml` / `extensions_config.json` 严禁入库（.gitignore 已拦截）；example 三件套由 `pixi run gen-examples` 从 Pydantic 模型自动生成。
