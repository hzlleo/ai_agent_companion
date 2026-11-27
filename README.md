# ai_agent_companion

自建「大肥鱼」式 AI 陪伴智能体——**模型无关**（主 LLM 为远端 vLLM 0.32+ 或任意 OpenAI 兼容端点）、**编排自持**（不依赖任何厂商 harness）、**多人格**（chara card v2 + 按人格隔离的记忆）、能自主上网活动的私人 AI 伴侣。

## 核心特性

- **多人格注册表**：chara card v2 人设卡 + L/M/S 三层心理状态 + 人格切换/复制，记忆与平台账号按人格隔离
- **三层记忆**：情景（append-only 事件日志 + 每日日记）/ 策展（MEMORY.md + USER.md + 关系状态）/ 夜间梦境整理（规则门控 + provenance 分级）
- **统一检索**：SeekDB（向量 + BM25 + 标量过滤单一引擎，MySQL 协议原生 async）+ SeekDB 权威关系库（与检索同实例、不同库名）；双时间线先以关系建模落地
- **值班轮 + 调度**：任务书（charter）模式 + 写入类平台工具经 safety 审批门接入（规划中）+ 全量审计
- **模型路由**：config.yaml 角色绑定 + 条件路由策略（亲密度 / 时段 / 对话类型条件命中时改由目标角色条目承接）；亲密回复独立通道经内置策略等价承接（`LLM_INTIMATE_*`，默认关闭）
- **工具层**：OpenAI 兼容原生工具调用 + docker 沙盒隔离执行 + `look_at` 视觉理解（本地 Qwen3-VL，主模型无需多模态）+ 管理员声明式外部 HTTP 工具扩展（`extensions_config.json`，默认关闭；见 [docs/backend/tools.md](docs/backend/tools.md)）
- **管理后台**：人格/记忆/配置/工具全管理 + Token 用量多维分析（时间 × 模型 × 用途 × 输入/输出/总量）
- **语音**：CosyVoice3-0.5B TTS + Qwen3-ASR-0.6B 流式识别，独立本地服务进程
- **实时语音全双工**：WebSocket 会话 + 能量/静音 VAD 切段 + 切段转写（节流中途字幕）+ 人格回复逐句 TTS 分片播报 + 打断（取消在途生成/合成）；管理台实时字幕面板；默认关闭
- **声纹（PIPL 合规）**：同意先于处理、人格 × 发送者隔离、撤回 / 删除即清样本与向量；默认关闭
- **表情包库**：哈希去重 + 来源与授权备注必填 + VLM 描述 / 标签标注 + 通道附件 v2 收发；默认关闭
- **平台通道**：Telegram 长轮询 / QQ（NapCat OneBot v11）推送入站 / 企业微信智能机器人长连接（WebSocket——订阅鉴权、心跳保活、退避重连、素材分片上传）/ 抖音 web（浏览器 sidecar 底座上的会话页采集与发送）/ SmsForwarder 手机短信与通知转发（token 鉴权、有界入站队列、仅入站）/ WhatsApp Cloud API（webhook hub 校验 + HMAC 签名、有界入站队列、Graph API 文本出站、24 小时客户服务窗）；共用统一通道路由器与附件协议 v2
- **画布**：人格作用域的 HTML / SVG / Markdown 渲染产物——管理台沙箱预览、修订留号、单文件导出

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2.0 async |
| 前端 | React 19 + Vite |
| 关系库 | SeekDB 权威存储（MySQL 协议，aiomysql 驱动） |
| 检索 | SeekDB（AI-native 搜索数据库，MySQL 协议） |
| 主 LLM | 远端 vLLM 0.32+（多 provider 网关，OpenAI 兼容） |
| 辅助模型 | Qwen3-VL（look_at）/ Qwen3-Embedding-0.6B / bge-m3（备）/ CosyVoice3 / Qwen3-ASR —— 全部独立本地服务进程 |
| 环境管理 | Pixi（根薄编排 + backend/frontend 子项目独立） |
| 时间戳 | BigInteger epoch-millis（全 API 统一） |

## 目录结构

```
ai_agent_companion/
├── pixi.toml            # 根薄编排层（零依赖，仅委托任务）
├── scripts/             # 工程级门禁脚本（注释/覆盖率/棘轮/noqa 冻结/conftest env 硬覆盖/红探针/证据包/文档真相/断言棘轮/导出面/prose-pin）
├── backend/             # 后端独立 pixi 工程
│   └── src/
│       ├── app/         # 薄 FastAPI 交付层（只做 API）
│       └── packages/harness/   # 全部业务逻辑（PEP 420 命名空间，领域分包）
├── frontend/            # 前端独立 pixi 工程（React 19 + Vite 管理台：人格/记忆/用量/模型/表情包/画布/实时语音）
├── models/              # 本地模型子模组（ModelScope 源，权重经 git-lfs 按需拉取——见 docs/models.md）
├── docker/              # 工具沙盒隔离树（每树完全隔离）+ compose
└── docs/                # 文档（贴合代码：JSON 示例 + 字段说明 + 链路顺序）
```

## 快速开始

```bash
pixi install                # 安装根环境
cd backend && pixi install  # 安装后端环境
cd .. && pixi run test-all  # 后端全量测试
cd backend && pixi run chat # 控制台与当前人格对话
cd ../frontend && pixi install && pixi run install  # 前端环境（必须经 pixi 提供 Node 22；勿直接 npm）
pixi run dev                # 前端开发服务器（5173，连本机后端 8000）
```

手机短信 / 通知转发（SmsForwarder）：后端设 `SMS_FORWARDER_ENABLED=true` 与
`SMS_FORWARDER_TOKEN`，手机端把模板 POST 到 `/api/v1/channels/sms-forwarder`
（请求头或查询参数携带同一 token；详见 `docs/backend/channels.md` SmsForwarder 一节）。

WhatsApp Cloud API：后端设 `WHATSAPP_ENABLED=true` 与四项凭据
（`WHATSAPP_VERIFY_TOKEN` / `WHATSAPP_APP_SECRET` / `WHATSAPP_ACCESS_TOKEN` /
`WHATSAPP_PHONE_NUMBER_ID`），在 Meta 应用侧把 webhook 指向
`/api/v1/channels/whatsapp` 并订阅 `messages` 字段（详见 `docs/backend/channels.md`
WhatsApp 一节；出站仅自由格式文本，受 24 小时客户服务窗约束）。

配置 example 三件套（`.env.example` / `config.example.yaml` / `extensions_config.example.json`）由
`pixi run gen-examples` 从 Pydantic 配置类自动生成；真实配置经 `pixi run setup` 交互式向导生成，全部配置项均受管理。

性能基线：`docs/backend/performance.md`（`cd backend && pixi run bench-core` / `pixi run api-smoke`；`--gate` / `--update` / `--self-check` 模式同页）；前端构建体积门禁见同页「前端构建产物体积基线」（`cd frontend && pixi run build && pixi run bundle-check`）。

管理后台的 owner-token 鉴权：运维在部署侧设置 `ADMIN_AUTH_TOKEN` 环境变量（非空 =
启用鉴权，全部管理路由要求 `Authorization: Bearer <token>`；留空 = 鉴权关闭，仅限
本机 / 可信内网——公网暴露必须配置）。变量名可经 `SERVER_ADMIN_TOKEN_ENV` 调整；
前端在首次收到 401 时弹出令牌输入页（详见 `frontend/README.md` 认证一节与
`docs/backend/README.md` 认证契约）。

## 工程约束

全异步（CPU/IO 阻塞仅经 `run_blocking_ops`）、公共池复用、Model Class 优先（Unit 文件分离）、
提示词独立 Unit 文件、epoch-millis、完整错误处理、批量支持、日志三 sink 每日轮换保留 30 天、
Google Style 中文注释、测试全原生异步且不依赖真实配置、按模块覆盖率 ≥80%。
详见 [docs/README.md](docs/README.md)。
