# 后端模块文档索引

按 harness 领域包逐模块建档；每份文档必须包含：完整 JSON 示例、字段说明表、链路顺序。

| 模块 | 文档 | 状态 |
|---|---|---|
| core（errors/dtos/time_utils/logging/thread_pool/background_tasks/context/i18n/cache/enums） | [core.md](core.md) | 已交付 |
| config（loader/models/settings/wizard/三件套生成 + SeekDB env 面） | [config.md](config.md) | 已交付（含 SeekDBSettings 检索 env 面） |
| database（engine/base Mixins/session/alembic） | [database.md](database.md) | 已交付 |
| pools（manager/http/llm/seekdb 共享池 + 熔断） | [pools.md](pools.md) | 已交付（含 SeekDB 检索池） |
| llm（模型网关：多 provider / roles 路由 / 用量埋点；含注册表管理 API：models 路由 + 原子持久化 / 热重建 / 回滚） | [llm.md](llm.md) | 已交付（含注册表管理面） |
| usage（token 用量记录与多维聚合；含用量观测 API：records / aggregates / summary 路由——跨人格口径） | [usage.md](usage.md) | 已交付（含管理 API） |
| persona（chara card / 注册表 / 切换复制 / L-M-S 状态；含人格语音绑定 voice_profile） | [persona.md](persona.md) | 已交付（含首个 app 层：personas 管理 API + 共享异常处理器；语音绑定增列见 [voice.md](voice.md)） |
| chat（会话循环 / prompt 组装 / 世界书 / 控制台 CLI / 会话持久化） | [chat.md](chat.md) | 已交付（含持久化与恢复） |
| app 装配（create_app + lifespan + health 存活面 + CORS（ServerConfig.cors_origins 驱动，Vite dev 默认来源）） | [chat.md](chat.md#app-装配appmainpy--appapihealthpy) | 已交付（随 chat 域文档建档；含 memories / usage / models 路由 + CORS） |
| memory（三层记忆：情景 / 策展 / 工作层存储 + 管理 API + 检索层 + 夜间梦境） | [memory.md](memory.md) | 已交付（存储 / 持久化 / API；检索层：MemoryStore + SeekDB / SQLite 回退 + 索引管道 + search API；夜间梦境：巩固合并 + 日记 + 运行日志 + 手工触发 API） |
| duty / scheduler（值班轮与中央调度：LLM 原生工具调用 + 工具注册表 + 章程驱动值班工具循环 + 手滚调度器 + 梦境定时触发） | [duty.md](duty.md) | 已交付（含 harness/tools 工具注册表与内置工具——沙盒执行器见 [tools-sandbox.md](tools-sandbox.md)） |
| tools / sandbox（工具沙盒：docker/tool-sandbox-agent 独立隔离运行时 + wire 契约双树镜像 + 后端沙盒客户端 / registry 代理注册 + http_fetch 示例工具 + compose 加固部署） | [tools-sandbox.md](tools-sandbox.md) | 已交付（沙盒为可选运行时——enabled=false 默认，平台工具按 http_fetch 范式接入） |
| voice（语音域：CosyVoice3 TTS 客户端 + Qwen3-ASR OpenAI 兼容转写客户端 + VoiceService 编排 / 用量埋点 + persona 语音绑定 + voice 管理 API + CLI --voice） | [voice.md](voice.md) | 已交付（外部本地模型服务经 HTTP 契约消费——enabled=false 默认，compose 接线由部署侧另行承接；B站通道规划中） |
| multimodal（多模态 look_at 域：llm 内容分块（OpenAI 兼容 vision 消息）+ LookAtService 编排（config 回退链消费）/ purpose="look_at" 用量埋点 + look_at 工具注册（duty 工具循环——app 装配在 _startup_scheduler duty 分支注册）+ look_at 管理 API（multipart 上传）） | [multimodal.md](multimodal.md) | 已交付（视觉模型 = llm 注册表 multimodal 条目——生产形态本地 llama-server + Qwen3-VL GGUF 子模型；B站通道规划中） |
| 工具层与辅助模型客户端（工具注册表 / docker 沙盒 / 语音 / 多模态） | 聚合说明（无独立文档）：工具注册表见 [duty.md](duty.md)，docker 沙盒见 [tools-sandbox.md](tools-sandbox.md)，语音见 [voice.md](voice.md)，多模态见 [multimodal.md](multimodal.md) | 已交付（分域建档） |
| channels（平台通道：通道中立路由器 + Telegram 适配器 / 长轮询 runner / offset 持久化 / 4096 分段 + QQ 适配器 / NapCat OneBot v11 webhook 推送入站 / 消息展平 / message_id 去重） | [channels.md](channels.md) | 已交付（Telegram；duty 报告出站经既有路由器消费；QQ / NapCat——推送式入站无 runner，warn-and-continue 启动校验；B站通道 / safety 审批红线规划中） |
