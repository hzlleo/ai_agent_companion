# 后端模块文档索引

按 harness 领域包逐模块建档；每份文档必须包含：完整 JSON 示例、字段说明表、链路顺序。

| 模块 | 文档 | 状态 |
|---|---|---|
| core（errors/dtos/time_utils/logging/thread_pool/background_tasks/context/i18n/cache/enums） | [core.md](core.md) | 已交付 |
| config（loader/models/settings + section_admin 配置节管理基座 + registry_admin LLM 注册表管理 + search_registry 搜索 provider 注册表管理 + wizard / 三件套生成 + SeekDB env 面） | [config.md](config.md) | 已交付（含 SeekDBSettings 检索 env 面；配置节管理基座——写路径共享机械，搜索 provider 注册表为第二个消费者） |
| database（engine/base Mixins/session/alembic） | [database.md](database.md) | 已交付 |
| pools（manager/http/llm/seekdb 共享池 + 熔断） | [pools.md](pools.md) | 已交付（含 SeekDB 检索池） |
| llm（模型网关：多 provider / roles 路由 / 用量埋点；含注册表管理 API：models 路由 + 原子持久化 / 热重建 / 回滚） | [llm.md](llm.md) | 已交付（含注册表管理面） |
| usage（token 用量记录与多维聚合；含用量观测 API：records / aggregates / summary 路由——跨人格口径） | [usage.md](usage.md) | 已交付（含管理 API） |
| persona（chara card / 注册表 / 主人格切换与可寻址集 / 切换复制 / L-M-S 状态；长期指令（D7-14——策展 user_note 收窄面 + 对话捕获）；含人格语音绑定 voice_profile；R2b-2：特质组件库（组件 / 预设模型 + 确定性生成 + 官方库装载）+ 导入端点（预设生成 / 卡片文件——内容哈希幂等 + 名称冲突三策略 + 批量部分成功）；R2c：人物事实（证据链 / 更正循环）+ 程序性技能（触发短语 / 原地版本 / 启用状态 / compose 匹配）） | [persona.md](persona.md) | 已交付（多激活可寻址模型——多人格同时可用、主人格唯一（DB 唯一键强制）；含首个 app 层：personas 管理 API + 共享异常处理器；语音绑定增列见 [voice.md](voice.md)；官方预设库以纯特质组合集发行（版权洁净），用户完整卡片经导入端点私有导入） |
| chat（会话循环 / prompt 组装 / 世界书 / 控制台 CLI / 会话持久化 / 可选工具循环（R2a-4——构造注入 tools 后 reply 走有界循环，见 [search.md](search.md)）/ R2b D7-5 消息变体 swipes（单回合多候选重生成 + 选定提升，见 [memory.md](memory.md) 变体列）） | [chat.md](chat.md) | 已交付（含持久化与恢复；工具循环经 web_search 装配消费——CLI 零变更；变体上下文只回放正典脊柱、变体不推进 M 态（探索面）） |
| emotion（轻情绪引擎：8 维情绪枚举 + 输出协议 `<情绪>` 增量 → M 态推进 / 半衰期衰减 / 摘要行 + compose 心情块；心声 / 情绪快照经会话消息 `meta` 持久化（9 号迁移）与会话详情 API 读侧） | [emotion.md](emotion.md) | 已交付（R2b D7-7：EMOTION_ env 面**默认开启**——纯本地零成本；TTS 风格 / 前端表情消费面 R2d / R2e 接入） |
| app 装配（create_app + lifespan + health 存活面 + CORS（ServerConfig.cors_origins 驱动，Vite dev 默认来源）+ 管理面 owner-token 鉴权（见下方「认证」一节）） | [chat.md](chat.md#app-装配appmainpy--appapihealthpy) | 已交付（随 chat 域文档建档；含 memories / usage / models 路由 + CORS + owner-token 鉴权） |
| memory（三层记忆：情景 / 策展 / 工作层存储 + 管理 API + 检索层 + 夜间梦境 + 记忆生命周期（R2c：置顶 / 冻结 / 时间衰减 / 使用强化 / 归档，迁移 0012）+ 会话消息变体列（迁移 0010）） | [memory.md](memory.md) | 已交付（存储 / 持久化 / API；检索层：MemoryStore + SeekDB / SQLite 回退 + 索引管道（R2c 生产接线 + 删除 / 归档索引移除）+ search API + 写读强化；夜间梦境：fact / skill 双提取阶段 + 巩固合并 + 日记 + 生命周期归档 + 运行日志 + 手工触发 API（迁移 0013 / 0014 经 SeekDB 真引擎 round-trip 验证）；R2c 记忆管道闭合：对话回合 → 情景层 episode sink） |
| duty / scheduler（值班轮与中央调度：LLM 原生工具调用 + 工具注册表 + 章程驱动值班工具循环 + 手滚调度器 + 梦境定时触发 + 提醒扫描作业） | [duty.md](duty.md) | 已交付（含 harness/tools 工具注册表与内置工具——沙盒执行器见 [tools-sandbox.md](tools-sandbox.md)；提醒扫描作业见 [reminders.md](reminders.md)） |
| reminders（持久化提醒：`ff_reminders` 表（迁移 0011）+ CRUD / 批量取消部分成功 + 到期扫描（FOR UPDATE SKIP LOCKED）+ 属主通道投递作业（60s 间隔）+ 投递失败计数与上限） | [reminders.md](reminders.md) | 已交付（P10=B——跨重启存活；无通道时保持 pending 的补投语义；零新增 env 键） |
| tools / sandbox（工具沙盒：docker/tool-sandbox-agent 独立隔离运行时 + wire 契约双树镜像 + 后端沙盒客户端 / registry 代理注册 + http_fetch 示例工具 + compose 加固部署） | [tools-sandbox.md](tools-sandbox.md) | 已交付（沙盒为可选运行时——enabled=false 默认，平台工具按 http_fetch 范式接入） |
| tools / browser（浏览器 sidecar：docker/browser-sidecar 独立隔离运行时（playwright chromium）+ wire 契约双树镜像（REST 同构沙盒）+ 后端 sidecar 客户端 / registry 代理注册（read_text 不可信标注 + screenshot look_at 配对提示）+ 导航策略链（协议→私网拒绝→DNS→允许清单→路由拦截）+ compose 加固部署） | [tools-browser.md](tools-browser.md) | 已交付（sidecar 为可选运行时——enabled=false 默认，JS 渲染回退面；duty / chat 双注册，warn-and-continue 降级） |
| voice（语音域：CosyVoice3 TTS 客户端 + Qwen3-ASR OpenAI 兼容转写客户端 + VoiceService 编排 / 用量埋点 + persona 语音绑定 + voice 管理 API + CLI --voice） | [voice.md](voice.md) | 已交付（外部本地模型服务经 HTTP 契约消费——enabled=false 默认，compose 接线由部署侧另行承接；B站通道规划中） |
| multimodal（多模态 look_at 域：llm 内容分块（OpenAI 兼容 vision 消息）+ LookAtService 编排（config 回退链消费）/ purpose="look_at" 用量埋点 + look_at 工具注册（duty 工具循环——app 装配在 _startup_scheduler duty 分支注册）+ look_at 管理 API（multipart 上传）） | [multimodal.md](multimodal.md) | 已交付（视觉模型 = llm 注册表 multimodal 条目——生产形态本地 llama-server + Qwen3-VL GGUF 子模型；B站通道规划中） |
| search（联网搜索域：Bocha / Zhipu Web Search 双 provider 客户端（failover + 降级冷却）+ SearchService 预算门禁（per-day / per-persona 按调用行计数）+ purpose="search" 用量埋点 + web_search 工具（duty / chat 工具循环共用——app 装配在 _startup_search 注册）+ chat 工具循环（ChatService 可选工具面）+ 搜索注册表管理 API（/api/v1/search）） | [search.md](search.md) | 已交付（R2a-4：config.yaml `search` 段注册表——ConfigSectionAdmin 基座第二个消费者；SEARCH_* env 面运营开关 / 预算 / 冷却） |
| rhythm（拟人节律层：睡眠窗口判定（跨午夜 / naive 本地墙钟）+ 拟人回复延迟采样（bounded log-uniform + 快答尾）+ 打字模拟（base + 每字符速度封顶）+ 生效值解析（人格覆写 + 温度缩放）+ RhythmService 门面（R2d 心流 / R2e GUI 消费面——R2a 回复链路不消费，行为零变化）） | [rhythm.md](rhythm.md) | 已交付（R2a-6：纯函数核心 + RHYTHM_ env 面；与意图快径共享单一随机源） |
| intent（意图快径：TIME_QUERY / GREETING / IDENTITY / HELP / DIRECTIVE 五类高频问法本地模式表匹配 → 罐头回复完全绕过主 LLM + ChatService.reply 前钩（通道专属注入——duty / CLI 零变化）+ 拟真延迟 800-1500ms + 快径回合照常落库（记忆 / 梦境可见）） | [intent.md](intent.md) | 已交付（R2a-6：INTENT_ env 面**默认开启**（纯本地零成本）；未命中无损兜底走 LLM） |
| 工具层与辅助模型客户端（工具注册表 / docker 沙盒 / docker 浏览器 sidecar / 语音 / 多模态） | 聚合说明（无独立文档）：工具注册表见 [duty.md](duty.md)，docker 沙盒见 [tools-sandbox.md](tools-sandbox.md)，docker 浏览器 sidecar 见 [tools-browser.md](tools-browser.md)，语音见 [voice.md](voice.md)，多模态见 [multimodal.md](multimodal.md) | 已交付（分域建档） |
| channels（平台通道：通道中立路由器 + Telegram 适配器 / 长轮询 runner / offset 持久化 / 4096 分段 + QQ 适配器 / NapCat OneBot v11 webhook 推送入站 / 消息展平 / message_id 去重） | [channels.md](channels.md) | 已交付（Telegram；duty 报告出站经既有路由器消费；QQ / NapCat——推送式入站无 runner，warn-and-continue 启动校验；B站通道 / safety 审批红线规划中） |

## 认证（管理面 owner-token 鉴权）

R2 设计审计放行条件：管理 API（含成本驱动与账号驱动能力）不得以无鉴权面扩张。
实现为 **单一 owner token 的 Bearer 校验**（`app/middleware/admin_auth.py` 的
`require_admin_token` 依赖，经 `app/main.py` 的 `APIRouter(dependencies=[...])`
包装路由对六个管理路由**单点挂接**）。

### 配置（env 间接——token 本值永不落 yaml / 配置树）

| 字段 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `admin_token_env` | `ServerConfig`（`packages/harness/config/models.py`） | `ADMIN_AUTH_TOKEN` | owner token 的**环境变量名**（间接引用）；实际 token 从该变量按请求读取 |
| `ADMIN_AUTH_TOKEN` | 进程环境变量 / `.env` | （空） | owner token 本值——**运维设置项**；非空 = 鉴权启用，空 / 未设置 = 鉴权关闭 |

链路顺序（每请求）::

    请求 → require_admin_token 依赖
        ↓ Settings 解析（app.state.settings → 全局单例回落）
        ↓ os.environ[admin_token_env] 读取（空 → 直接放行 = 关闭形态）
        ↓ Authorization: Bearer <token> 前缀解析
        ↓ secrets.compare_digest 恒时比较（utf-8 字节面）
        ↓ 通过 → 路由处理器 / 失败 → BusinessException(UNAUTHORIZED) → 共享异常处理器

鉴权失败响应（HTTP 401，`ResultDTO` error 形态；`details` 只携带变量名，**不含 token 本值**）::

    {
        "success": false,
        "code": "UNAUTHORIZED",
        "message": "管理面鉴权失败：Authorization Bearer token 缺失或不匹配",
        "data": {"admin_token_env": "ADMIN_AUTH_TOKEN"}
    }

### 保护面与豁免契约（显式清单——增删须同步 `app/main.py` `_include_routers`）

| 路由集 | 挂载 | 豁免理由 |
|---|---|---|
| personas / memories / usage / voice / look_at / models / search / reminders / directives / facts / skills | **受保护**（统一挂 `require_admin_token`） | 管理面全操作集（含模型注册表 / 搜索 provider 注册表与沙盒消费的成本 / 账号驱动能力；提醒 / 长期指令 / 人物事实 / 程序性技能为人格作用域数据面） |
| `GET /api/v1/health` | **豁免** | 零依赖存活面（health.py docstring 契约：liveness 探针不触 DB / 池 / LLM，同理不得因鉴权配置拒答） |
| `POST /api/v1/channels/qq/webhook` | **豁免** | 自鉴权面：NapCat 事件上报经自身 `webhook_token_env` Bearer 校验（见 [channels.md](channels.md)）；再叠 admin token 会要求部署机持有管理凭据——双重鉴权破坏上报链路 |

### 关闭形态与 R3 加固指引

`ADMIN_AUTH_TOKEN` 为空 / 未设置 = 鉴权关闭（全部管理路由放行）——仅适合本机开发 /
可信内网形态；启动期落**一条** info 日志通告关闭态（含 R3 指针，不逐请求刷屏）。
公网暴露**必须**配置非空 token。R3 计划补齐强制启用（生产 env 下空 token 拒绝启动 /
启动向导强制写入），本节随 R3 落地同步更新。
