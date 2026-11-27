# 后端模块文档索引

按 harness 领域包逐模块建档；每份文档必须包含：完整 JSON 示例、字段说明表、链路顺序。

| 模块 | 文档 | 状态 |
|---|---|---|
| core（errors/dtos/time_utils/logging/thread_pool/background_tasks/context/i18n/cache/enums） | [core.md](core.md) | 已交付 |
| config（loader/models/settings + section_admin 配置节管理基座 + registry_admin LLM 注册表管理 + search_registry 搜索 provider 注册表管理 + wizard / 三件套生成 + SeekDB env 面） | [config.md](config.md) | 已交付（含 llm.route_policies 条件路由配置面；配置节管理基座——写路径共享机械，搜索 provider 注册表为第二个消费者） |
| database（engine/base Mixins/session/alembic） | [database.md](database.md) | 已交付 |
| pools（manager/http/llm/seekdb 共享池 + 熔断） | [pools.md](pools.md) | 已交付（含 SeekDB 检索池） |
| llm（模型网关：多 provider / roles 路由 / 用量埋点；含注册表管理 API：models 路由 + 原子持久化 / 热重建 / 回滚；条件路由策略 `llm.route_policies`（亲密度 / 时段 / 用途条件改道）；亲密回复独立通道 `LLM_INTIMATE_*` 作为内置策略（默认关闭、缺任一必填即不启用）） | [llm.md](llm.md) | 已交付（条件路由求值纯函数 + 内置亲密策略等价转译；公共池复用 / 失败不静默换道） |
| usage（token 用量记录与多维聚合；含用量观测 API：records / aggregates / summary 路由——跨人格口径） | [usage.md](usage.md) | 已交付（含管理 API） |
| persona（chara card / 注册表 / 主人格切换与可寻址集 / 切换复制 / L-M-S 状态；长期指令（策展 user_note 收窄面 + 对话捕获）；含人格语音绑定 voice_profile；特质组件库（组件 / 预设模型 + 确定性生成 + 官方库装载）+ 导入端点（预设生成 / 卡片文件——内容哈希幂等 + 名称冲突三策略 + 批量部分成功）；亲密补强：120+ 组件 × 十二类目 + 成熟度分层（safe/suggestive/explicit）+ 预设亲密等级 0-3 与四态场景脚本 + 生成期覆盖过滤；人物事实（证据链 / 更正循环）+ 程序性技能（触发短语 / 原地版本 / 启用状态 / compose 匹配）+ 自编辑核心记忆（固定四块 / 主人锁定 / append-only 修订 / 工具注册）；关系连续性：关系进展摘要（关系类事实派生阶段 / 时间衰减得分 / 边界识别——无独立存储与 API）） | [persona.md](persona.md) | 已交付（多激活可寻址模型——多人格同时可用、主人格唯一（DB 唯一键强制）；含首个 app 层：personas 管理 API + 共享异常处理器；语音绑定增列见 [voice.md](voice.md)；官方预设库以纯特质组合集发行（版权洁净），用户完整卡片经导入端点私有导入；核心记忆写入闸为结构性工具列表裁剪——群聊 peer 回合不可见自编辑工具） |
| chat（会话循环 / prompt 组装 / 世界书 / 控制台 CLI / 会话持久化 / 可选工具循环（构造注入 tools 后 reply 走有界循环，见 [search.md](search.md)）/ 核心记忆 compose 块与发送者来源分级工具闸（见 [persona.md](persona.md) 核心记忆一节）/ 消息变体 swipes（单回合多候选重生成 + 选定提升，见 [memory.md](memory.md) 变体列）/ 关系进展块（关系类事实派生、恒注入、1200 字符帽）/ 亲密通道路由输入（关系摘要等级交给网关裁决）/ 心流门控（`FLOW_` env 面——判定矩阵 / 连发合并 / 延迟批次经中央调度器 / 会话节拍状态）/ 群聊多人格选择（`GROUP_PERSONA_` env 面——轮转 / 冷却 / 重复抑制 / 人格会话归属）） | [chat.md](chat.md) | 已交付（含持久化与恢复；心流门控默认关闭、群聊多人格默认关闭——既有单人格即时回复语义零变化；工具循环经 web_search / 浏览器 / 核心记忆工具面装配消费——非 owner 回合剔除 edit_core_memory，CLI 零变更；变体上下文只回放正典脊柱、变体不推进 M 态（探索面）；reply / stream / 工具循环 / 合规重试携带同一亲密通道路由输入；心流门控与群聊多人格在通道路由器接线——延迟 / 合并批次经调度器一次性作业，多人格逐人格独立会话归属） |
| emotion（轻情绪引擎：8 维情绪枚举 + 输出协议 `<情绪>` 增量 → M 态推进 / 半衰期衰减 / 摘要行 + compose 心情块；心声 / 情绪快照经会话消息 `meta` 持久化（9 号迁移）与会话详情 API 读侧） | [emotion.md](emotion.md) | 已交付（EMOTION_ env 面**默认开启**——纯本地零成本；TTS 风格 / 前端表情消费面待心流层与管理台接入） |
| app 装配（create_app + lifespan + health 存活面 + CORS（ServerConfig.cors_origins 驱动，Vite dev 默认来源）+ 管理面 owner-token 鉴权（见下方「认证」一节）） | [chat.md](chat.md#app-装配appmainpy--appapihealthpy) | 已交付（随 chat 域文档建档；含 memories / usage / models 路由 + CORS + owner-token 鉴权） |
| memory（三层记忆：情景 / 策展 / 工作层存储 + 管理 API + 检索层 + 夜间梦境 + 记忆生命周期（置顶 / 冻结 / 时间衰减 / 使用强化 / 归档，迁移 0012）+ 会话消息变体列（迁移 0010）） | [memory.md](memory.md) | 已交付（存储 / 持久化 / API；检索层：MemoryStore + SeekDB / SQLite 回退 + 索引管道（生产接线 + 删除 / 归档索引移除）+ search API + 写读强化；夜间梦境：fact / skill 双提取阶段 + 巩固合并 + 日记 + 生命周期归档 + 运行日志 + 手工触发 API（迁移 0013 / 0014 经 SeekDB 真引擎 round-trip 验证）；记忆管道闭合：对话回合 → 情景层 episode sink） |
| defense（提示注入防线：不可信内容 nonce 定界包裹 / 检测规则表与保守阈值 / 宪章五条 / 角色内反击台词 / peer 免疫矩阵 / 38 条红队回归） | [defense.md](defense.md) | 已交付（结构隔离覆盖当轮入站与工具结果（chat + duty）；检测只提示不拦截；`INJECTION_*` env 面待主 Agent 纳入示例模板生成） |
| compliance（人格输出防线：角色契约五条 compose 常驻块 / 助手腔检测规则表与保守阈值（AI 自我声明 / 道歉开场 / 短回复总结腔 / 客服话术）/ 命中后单次强化重试（角色提醒块 + 温度提升，工具循环分支保留工具证据但不带工具面）/ 流式不重试边界） | [chat.md](chat.md#人格输出契约与合规守卫) | 已交付（`COMPLIANCE_` env 面缺省开启；`COMPLIANCE_*` 四键待主 Agent 纳入示例模板生成） |
| duty / scheduler（值班轮与中央调度：LLM 原生工具调用 + 工具注册表 + 章程驱动值班工具循环 + 手滚调度器 + 梦境定时触发 + 提醒扫描作业） | [duty.md](duty.md) | 已交付（含 harness/tools 工具注册表与内置工具——沙盒执行器见 [tools-sandbox.md](tools-sandbox.md)；提醒扫描作业见 [reminders.md](reminders.md)） |
| reminders（持久化提醒：`ff_reminders` 表（迁移 0011）+ CRUD / 批量取消部分成功 + 到期扫描（FOR UPDATE SKIP LOCKED）+ 属主通道投递作业（60s 间隔）+ 投递失败计数与上限） | [reminders.md](reminders.md) | 已交付（跨重启存活；无通道时保持 pending 的补投语义；零新增 env 键） |
| tools / sandbox（工具沙盒：docker/tool-sandbox-agent 独立隔离运行时 + wire 契约双树镜像 + 后端沙盒客户端 / registry 代理注册 + http_fetch 示例工具 + compose 加固部署） | [tools-sandbox.md](tools-sandbox.md) | 已交付（沙盒为可选运行时——enabled=false 默认，平台工具按 http_fetch 范式接入） |
| tools / browser（浏览器 sidecar：docker/browser-sidecar 独立隔离运行时（playwright chromium）+ wire 契约双树镜像（REST 同构沙盒）+ 后端 sidecar 客户端 / registry 代理注册（read_text 不可信标注 + screenshot look_at 配对提示）+ 导航策略链（协议→私网拒绝→DNS→允许清单→路由拦截）+ compose 加固部署） | [tools-browser.md](tools-browser.md) | 已交付（sidecar 为可选运行时——enabled=false 默认，JS 渲染回退面；duty / chat 双注册，warn-and-continue 降级） |
| tools / extension（外部 HTTP 工具扩展：extensions_config.json 声明式注册 + 参数 JSON Schema 执行面（type 严格 / required / enum / pattern / 数值与长度范围 / 数组 items 与长度 / additionalProperties；不支持嵌套 object 内部约束 / format / oneOf·anyOf）+ `{env:}` 密钥间接（URL 不支持占位、拒绝 userinfo 与长字面查询密钥）+ 共享池流式发送 / 超时与响应字节帽 / 禁重定向 + registry 装配报告） | [tools.md](tools.md) | 已交付（默认关闭；端点由管理员本地文件定义——信任边界是该文件，不做私网拦截；同名默认不覆盖已注册工具） |
| voice（语音域：CosyVoice3 TTS 客户端 + Qwen3-ASR OpenAI 兼容转写客户端 + VoiceService 编排 / 用量埋点 + persona 语音绑定 + voice 管理 API + CLI --voice；声纹档案（`VOICEPRINT_` env 面——PIPL 告知 / 同意记录 / 批量注册 / 余弦校验 / 撤回删除 / 保存期清理，迁移 0016）） | [voice.md](voice.md) | 已交付（外部本地模型服务经 HTTP 契约消费——enabled=false 默认，compose 接线由部署侧另行承接；声纹默认关闭，未配置说话人向量端点时整功能关闭；B站通道规划中） |
| realtime-voice（实时语音全双工会话：WS 协议与相位状态机 / 能量静音 VAD 切段（双阈值滞回 + 防抖 + 单段上限）/ 切段转写与节流中途转写 / 句切与逐句 TTS 分片下行 / barge-in 取消无泄漏 / 背压与空闲时长上限 / `REALTIME_VOICE_` env 面 / 管理台实时字幕面板） | [realtime-voice.md](realtime-voice.md) | 已交付（默认关闭；ASR / TTS 复用既有 VoiceService 批量端点——真实供应商连通性留部署侧一次性验证；AEC / 移动端优化未做） |
| multimodal（多模态域：llm 内容分块（OpenAI 兼容 vision 消息）+ LookAtService 编排（config 回退链消费）/ purpose="look_at" 用量埋点 + look_at 工具注册 + look_at 管理 API；表情包库（`STICKER_` env 面——哈希去重 / 来源与授权备注 / VLM 标注状态机 / 通道附件 v2 收发桥 / 管理 API + 前端面板，迁移 0016）） | [multimodal.md](multimodal.md) | 已交付（视觉模型 = llm 注册表 multimodal 条目——生产形态本地 llama-server + Qwen3-VL GGUF 子模型；表情包默认关闭，关闭时通道语义零变化；B站通道规划中） |
| search（联网搜索域：Bocha / Zhipu Web Search 双 provider 客户端（failover + 降级冷却）+ SearchService 预算门禁（per-day / per-persona 按调用行计数）+ purpose="search" 用量埋点 + web_search 工具（duty / chat 工具循环共用——app 装配在 _startup_search 注册）+ chat 工具循环（ChatService 可选工具面）+ 搜索注册表管理 API（/api/v1/search）） | [search.md](search.md) | 已交付（config.yaml `search` 段注册表——ConfigSectionAdmin 基座第二个消费者；SEARCH_* env 面运营开关 / 预算 / 冷却） |
| rhythm（拟人节律层：睡眠窗口判定（跨午夜 / naive 本地墙钟）+ 拟人回复延迟采样（bounded log-uniform + 快答尾）+ 打字模拟（base + 每字符速度封顶）+ 生效值解析（人格覆写 + 温度缩放）+ RhythmService 门面（心流门控 / GUI 消费面）） | [rhythm.md](rhythm.md) | 已交付（纯函数核心 + RHYTHM_ env 面；与意图快径共享单一随机源；`FLOW_ENABLED=true` 时门结果进入心流判定） |
| intent（意图快径：TIME_QUERY / GREETING / IDENTITY / HELP / DIRECTIVE 五类高频问法本地模式表匹配 → 罐头回复完全绕过主 LLM + ChatService.reply 前钩（通道专属注入——duty / CLI 零变化）+ 拟真延迟 800-1500ms + 快径回合照常落库（记忆 / 梦境可见）） | [intent.md](intent.md) | 已交付（INTENT_ env 面**默认开启**（纯本地零成本）；未命中无损兜底走 LLM） |
| 工具层与辅助模型客户端（工具注册表 / docker 沙盒 / docker 浏览器 sidecar / 语音 / 多模态） | 聚合说明（无独立文档）：工具注册表见 [duty.md](duty.md)，docker 沙盒见 [tools-sandbox.md](tools-sandbox.md)，docker 浏览器 sidecar 见 [tools-browser.md](tools-browser.md)，语音见 [voice.md](voice.md)，多模态见 [multimodal.md](multimodal.md) | 已交付（分域建档） |
| channels（平台通道：通道中立路由器 + Telegram 适配器 / 长轮询 runner / offset 持久化 / 4096 分段 + QQ 适配器 / NapCat OneBot v11 webhook 推送入站 / 有界入站 runner / 消息展平 / message_id 去重 + 企业微信智能机器人长连接（WebSocket 订阅 / 心跳 / 退避重连 / 素材分片上传 / markdown 分段）+ 抖音 web 通道（浏览器 sidecar 底座上的会话页轮询采集 / 发送、去重、系统消息 synthetic、图片附件、指数退避）+ SmsForwarder 短信 / 通知转发（手机端 webhook 仅入站、token 鉴权、有界队列、msgid / 内容去重、属主分级）+ WhatsApp Cloud API（webhook hub 校验 / HMAC 签名、wamid 去重、有界入站队列、Graph API 文本出站、24 小时客户服务窗）+ 心流门控接线（延迟 / 合并批次经中央调度器）+ 群聊多人格应答（逐人格会话归属）） | [channels.md](channels.md) | 已交付（Telegram；QQ / NapCat——推送式入站 + 有界 runner，warn-and-continue 启动校验；企微长连接——`WECOM_ENABLED` 默认关闭，装配期凭据失败仅跳过该通道；抖音 web——`DOUYIN_ENABLED` 默认关闭，依赖浏览器 sidecar 底座（底座未就绪只跳过该通道），出站仅文本；SmsForwarder——`SMS_FORWARDER_ENABLED` 默认关闭，仅入站（出站恒丢弃），通知默认忽略，生产强制非空 token；WhatsApp——`WHATSAPP_ENABLED` 默认关闭，webhook 签名 / hub 校验 + 文本出站，生产强制四项凭据非空，24 小时窗外的自由格式出站以受控业务错误拒绝（模板消息未实现）；duty 报告出站经既有路由器消费；心流门控 / 群聊多人格默认关闭，单聊语义零变化；B站通道 / safety 审批红线规划中） |
| canvas（渲染产物：人格作用域的 HTML / SVG / Markdown 自包含产物持久化（迁移 0017）+ 管理 API（列表 / 创建 / 修订 / 删除 / 导出）+ 沙箱预览面板 + 单文件导出） | [canvas.md](canvas.md) | 已交付（类型创建后固定、修订号递增、预览不带 allow-scripts；导出 MIME / 扩展名按类型固定，CORS 暴露 Content-Disposition；未接 chat 工具——对话内产出属后续交付面） |
| performance（性能基线：纯本地微基准 `bench_core.py`（门控 / 关系摘要 / 句切 / VAD / 关系块渲染）+ ASGI 进程内只读端点冒烟 `api_smoke.py`；基线文件与 10× 环境容忍策略） | [performance.md](performance.md) | 已交付（无新依赖 / 新 env 键 / 迁移；`bench-core --self-check/--gate` 与 `api-smoke --self-check` 入门禁链，`api-smoke --gate` 部署机手动执行） |

## 认证（管理面 owner-token 鉴权）

设计审计放行条件：管理 API（含成本驱动与账号驱动能力）不得以无鉴权面扩张。
实现为 **单一 owner token 的 Bearer 校验**（`app/middleware/admin_auth.py` 的
`require_admin_token` 依赖，经 `app/main.py` 的 `APIRouter(dependencies=[...])`
包装路由对全部管理路由**单点挂接**）。保护面清单以 `app/main.py`
`_include_routers` 为唯一权威（当前 15 个路由集），下表为可读镜像——增删路由
须同步本表（`tests/unit/test_auth_surface_docs.py` 逐项比对，漂移即红）。

### 配置（env 间接——token 本值永不落 yaml / 配置树）

| 字段 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `admin_token_env` | `ServerConfig`（`packages/harness/config/models.py`） | `ADMIN_AUTH_TOKEN` | owner token 的**环境变量名**（间接引用）；实际 token 从该变量按请求读取 |
| `ADMIN_AUTH_TOKEN` | 进程环境变量 / `.env` | （空） | owner token 本值——**运维设置项**；非空 = 鉴权启用，空 / 未设置 = 开发 / 测试形态鉴权关闭（`ENV=prod` 强制非空，见下） |

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
| personas / memories / usage / voice / voiceprints / look_at / stickers / models / search / reminders / directives / facts / skills / core-memory / canvas | **受保护**（统一挂 `require_admin_token`） | 管理面全操作集（含模型注册表 / 搜索 provider 注册表与沙盒消费的成本 / 账号驱动能力；声纹 / 表情包 / 提醒 / 长期指令 / 人物事实 / 程序性技能 / 核心记忆 / 渲染产物为人格作用域数据面） |
| `GET /api/v1/health` | **豁免** | 零依赖存活面（health.py docstring 契约：liveness 探针不触 DB / 池 / LLM，同理不得因鉴权配置拒答） |
| `GET /api/v1/realtime/voice/ws` | **豁免**（自鉴权） | 浏览器 WebSocket 无法设置自定义头：token 经查询参数 `token` 传递，端点内以 `verify_admin_token` 恒时比较（见 [realtime-voice.md](realtime-voice.md)） |
| `POST /api/v1/channels/qq/webhook` | **豁免** | 自鉴权面：NapCat 事件上报经自身 `webhook_token_env` Bearer 校验（见 [channels.md](channels.md)）；再叠 admin token 会要求部署机持有管理凭据——双重鉴权破坏上报链路 |
| `POST /api/v1/channels/sms-forwarder` | **豁免** | 自鉴权面：手机端 SmsForwarder 经 `SMS_FORWARDER_TOKEN` 请求头 / 查询参数校验（见 [channels.md](channels.md)）；再叠 admin token 会要求手机端持有管理凭据——双重鉴权破坏转发链路 |
| `GET/POST /api/v1/channels/whatsapp` | **豁免** | 自鉴权面：GET hub 握手经 `WHATSAPP_VERIFY_TOKEN` 恒时比较，POST 经 `X-Hub-Signature-256` HMAC 校验（见 [channels.md](channels.md)）；再叠 admin token 会要求 Meta 侧配置管理凭据——双重鉴权破坏回调链路 |

### 关闭形态与生产强制（`ENV=prod` fail-closed）

开发 / 测试环境（`ENV=local` / `dev` / `test`，默认 `local`）保持既有形态：
`ADMIN_AUTH_TOKEN` 为空 / 未设置 = 鉴权关闭（全部管理路由放行），启动期落
**一条** info 日志通告关闭态（不逐请求刷屏）。

生产环境（`ENV=prod`）**R3 起强制**：

- `ADMIN_AUTH_TOKEN` 解析为空 → `startup_app_state` 抛
  `BusinessException(CONFIG_INVALID)`，文案「生产环境必须设置 ADMIN_AUTH_TOKEN」，
  app 拒绝启动；
- QQ 通道启用（`QQ_ENABLED=true`）且 `QQ_WEBHOOK_TOKEN` 解析为空 → 同样拒绝
  启动（文案「生产环境启用 QQ 通道时必须设置 QQ_WEBHOOK_TOKEN」）；
- WhatsApp 通道启用（`WHATSAPP_ENABLED=true`）且 verify token / app secret /
  access token / phone number id 任一项为空 → 同样拒绝启动（文案列出缺失键）；
- 策略实现为
  `packages/harness/config/auth_policy.py::ensure_auth_policy`（纯函数，
  启动链在组件装配前调用；仅在 `ENV=prod` 生效）。

公网暴露**必须**配置非空 token；开发 / 测试环境的关闭态不会带入生产。
