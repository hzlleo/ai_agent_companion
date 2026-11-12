# ai_agent_companion

自建「大肥鱼」式 AI 陪伴智能体——**模型无关**（主 LLM 为远端 vLLM 0.32+ 或任意 OpenAI 兼容端点）、**编排自持**（不依赖任何厂商 harness）、**多人格**（chara card v2 + 按人格隔离的记忆）、能自主上网活动的私人 AI 伴侣。

## 核心特性

- **多人格注册表**：chara card v2 人设卡 + L/M/S 三层心理状态 + 人格切换/复制，记忆与平台账号按人格隔离
- **三层记忆**：情景（append-only 事件日志 + 每日日记）/ 策展（MEMORY.md + USER.md + 关系状态）/ 夜间梦境整理（规则门控 + provenance 分级）
- **统一检索**：SeekDB（向量 + BM25 + 标量过滤单一引擎，MySQL 协议原生 async）+ MySQL 权威关系库；双时间线先以关系建模落地
- **值班轮 + 调度**：任务书（charter）模式 + 写操作审批门 + 全量审计
- **工具层**：MCP 工具协议 + `look_at` 视觉理解（本地 Qwen3-VL，主模型无需多模态）
- **管理后台**：人格/记忆/配置/工具全管理 + Token 用量多维分析（时间 × 模型 × 用途 × 输入/输出/总量）
- **语音**（P4）：CosyVoice3-0.5B TTS + Qwen3-ASR-0.6B 流式识别，独立本地服务进程

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2.0 async |
| 前端 | React 19 + Vite（P2 落地） |
| 关系库 | MySQL 8.x（aiomysql 异步驱动） |
| 检索 | SeekDB（AI-native 搜索数据库，MySQL 协议） |
| 主 LLM | 远端 vLLM 0.32+（多 provider 网关，OpenAI 兼容） |
| 辅助模型 | Qwen3-VL（look_at）/ Qwen3-Embedding-0.6B / bge-m3（备）/ CosyVoice3 / Qwen3-ASR —— 全部独立本地服务进程 |
| 环境管理 | Pixi（根薄编排 + backend/frontend 子项目独立） |
| 时间戳 | BigInteger epoch-millis（全 API 统一） |

## 目录结构

```
ai_agent_companion/
├── pixi.toml            # 根薄编排层（零依赖，仅委托任务）
├── scripts/             # 工程级门禁脚本（注释门/覆盖率门/棘轮/noqa 冻结）
├── backend/             # 后端独立 pixi 工程
│   └── src/
│       ├── app/         # 薄 FastAPI 交付层（只做 API）
│       └── packages/harness/   # 全部业务逻辑（PEP 420 命名空间，领域分包）
├── frontend/            # 前端独立 pixi 工程（React 19 + Vite，P2 落地）
├── docker/              # 沙盒镜像树（每树完全隔离，P3 落地）
└── docs/                # 文档（贴合代码：JSON 示例 + 字段说明 + 链路顺序）
```

## 快速开始

```bash
pixi install                # 安装根环境
cd backend && pixi install  # 安装后端环境
cd .. && pixi run test-all  # 后端全量测试
cd backend && pixi run chat # 控制台与当前人格对话（P0-6 后可用）
```

配置三件套（`.env.example` / `config.example.yaml` / `extensions_config.json.example`）由
`pixi run setup` 交互式向导生成（config 模块落地后可用），全部配置项均受管理。

## 工程约束

全异步（CPU/IO 阻塞仅经 `run_blocking_ops`）、公共池复用、Model Class 优先（Unit 文件分离）、
提示词独立 Unit 文件、epoch-millis、完整错误处理、批量支持、日志三 sink 每日轮换保留 30 天、
Google Style 中文注释、测试全原生异步且不依赖真实配置、按模块覆盖率 ≥80%。
详见 [docs/README.md](docs/README.md)。
