# ai_agent_companion 文档索引

文档铁律：**贴合代码**——每个模块文档包含完整 JSON 示例、字段说明表、链路顺序；代码变更未同步文档视为未完成。

## 目录

| 路径 | 内容 |
|---|---|
| [backend/](backend/README.md) | 后端模块文档（core / config / database / pools / llm / persona / memory / chat / usage / duty / tools / safety …按模块逐份） |
| [test-strategy/](test-strategy/README.md) | 测试策略（全异步 / 无真实配置 / 共享资源 / 覆盖率门 / 棘轮门） |

## 路线图（P0 骨架 → 完整形态）

| 阶段 | 内容 |
|---|---|
| P0 | 工程骨架 + core/config/database/pools + LLM 网关 + 多人格注册表 + 控制台聊天 + usage 域 |
| P1 | 三层记忆 + 日记 + 夜间梦境 + SeekDB 统一检索接入 |
| P2 | 前端管理后台（React 19 + Vite）+ 全量管理 API |
| P3 | 平台通道（Telegram 先行）+ 值班轮 + 审批门 + docker 工具沙盒树 |
| P4 | 语音（CosyVoice3 + Qwen3-ASR 独立本地服务）+ look_at 视觉工具 + B站/QQ 通道 |

## 已知事项

1. `.env` / `config.yaml` / `extensions_config.json` 严禁入库（.gitignore 已拦截）；example 三件套由 `pixi run setup` 从 Pydantic 模型自动生成（config 模块落地后生效）。
