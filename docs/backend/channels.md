# channels — 平台通道层（Telegram 首个适配器；QQ / NapCat OneBot v11）

`backend/src/packages/harness/channels/` 承载平台通道抽象：**通道中立路由器**（入站消息 → 人格会话循环 → 出站投递）+ **平台适配器**（Telegram——手写 Bot API 客户端 + 长轮询 runner；QQ——NapCat OneBot v11 客户端 + webhook 推送入站；企业微信——智能机器人长连接 WebSocket 客户端 + 回调 runner）。通道抽象保持类型可扩展（`ChannelType` StrEnum——B站等后续通道在此追加成员与同级子模型）。消息协议已升级 **v2**（附件 / 静默 / 合成事件——见「协议 v2」一节；v1 纯文本形态缺省保持，additive 向后兼容）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `channel_models.py` | 通道中立 Model Classes（Unit 文件）：通道类型值域 / 附件（v2）/ 入站 / 出站 / 上下文 / 投递结果（含逐附件结果 v2） | `ChannelType` / `ChannelAttachmentKind` / `ChannelMessageAttachment` / `InboundMessageModel` / `OutboundMessageModel` / `ChannelContextModel` / `AttachmentDeliveryResult` / `ChannelDeliveryResultModel` / `ATTACHMENT_BASE64_SIZE_GUIDE_BYTES` |
| `channel_service.py` | 通道路由器（每聊天会话 + 人格绑定 + 单消息错误隔离）+ 适配器发送面协议 | `ChannelRouter` / `ChannelAdapterProtocol` / `CHANNEL_FALLBACK_REPLY_TEXT` |
| `telegram/tg_models.py` | Bot API wire 模型子集（Unit 文件）：update / message（含 v2 媒体字段 + caption）/ 媒体对象 / 响应包裹（泛型） | `TgUpdateModel` / `TgMessageModel` / `TgPhotoSizeModel` / `TgVoiceModel` / `TgAudioModel` / `TgDocumentModel` / `TgStickerModel` / `TgResponseModel[T]` / `TgSendMessageResult` / `TgBotInfoModel` |
| `telegram/tg_client.py` | 手写 Bot API 客户端（共享 httpx client 注入；4096 分段；v2 免通知 + 媒体发送双形态） | `TelegramClient` / `split_tg_message_text` |
| `telegram/tg_runner.py` | 长轮询 runner（offset 持久化 + 去重 + 退避 + 优雅停机 + 发送面适配 + v2 入站映射 / 出站附件投递） | `TelegramChannelRunner` / `TG_FILE_PSEUDO_SCHEME` / `extract_tg_attachments` / `map_tg_message_to_inbound` |
| `qq/qq_models.py` | OneBot v11 wire 模型子集（Unit 文件）：事件（段数组 / CQ 码字符串双形态 message）/ 段 / 动作请求包裹 / 动作响应包裹（投递映射）/ v2 附件段发射 | `Ob11EventModel` / `Ob11MessageSegmentModel` / `Ob11ApiRequestModel` / `Ob11ApiResponseModel` / `Ob11LoginInfoModel` / `attachment_to_ob11_segment` |
| `qq/qq_client.py` | 手写 NapCat OneBot v11 HTTP API 客户端（共享 httpx client 注入；单一动作对象包裹；v2 段数组发送） | `NapCatOneBotClient` |
| `qq/qq_adapter.py` | QQ 适配器发送面（chat_id grammar 解析 → NapCat 动作 → 投递映射；无 runner；v2 附件段数组出站） | `NapCatQQAdapter` |
| `qq/qq_webhook.py` | webhook 事件处理器（容错解析 / 大类过滤 / message_id 去重 / 消息展平 / v2 附件提取；恒不抛） | `process_webhook_event` / `flatten_message_text` / `extract_message_attachments` / `QqMessageDedupWindow` / `reset_qq_webhook_state` |
| `wecom/wecom_models.py` | 智能机器人长连接 wire 模型（Unit 文件）：订阅 / 心跳 / 回调 / 应答 / 回复 / 推送 / 素材上传命令 + 入站映射 + markdown 分段 | `WeComCallbackFrameModel` / `WeComCommandModel` / `map_wecom_callback_to_inbound` / `parse_wecom_chat_id` / `split_wecom_markdown_text` |
| `wecom/wecom_client.py` | 长连接客户端（订阅 / req_id 应答关联 / 应用层心跳 / 指数退避重连 + 抖动 / 单连接不变式 / 被踢下线 / 优雅停机 / 素材分片上传） | `WeComLongConnectionClient` / `WeComConnectionState` / `WeComConnectionProtocol` |
| `wecom/wecom_runner.py` | 企微 runner（回调去重 / 回复上下文 / 有界入站队列 + worker / 出站文本分段与附件投递 / 逐附件失败承载） | `WeComChannelRunner` / `WeComMessageDedupWindow` |

依赖方向：channels → chat（ChatService）+ persona（bind_persona_context）+ config（Settings.channels）+ core（errors / time_utils）；HTTP 出站一律经 pools 共享 `httpx.AsyncClient`（硬规则：无私建连接）。锁定设计决策：**不引入 aiogram / python-telegram-bot / 任何 OneBot SDK**——按平台协议手写（与 llm 域「不引入 openai SDK」同约定）。

## 架构：适配器 / 路由器 / runner 链路顺序

```
Telegram 云端
    ↓ getUpdates 长轮询（offset 游标）
TelegramChannelRunner（telegram/tg_runner.py）
    ↓ TgUpdateModel → InboundMessageModel（ID 字符串化 / epoch 秒 ×1000 归一）
ChannelRouter.handle_inbound（channel_service.py）
    ① 适配器寻址（未注册 → CHANNEL_DISABLED，仅记录不抛）
    ①′ per-chat 串行化锁（asyncio.Lock——同聊天全链路串行 / 不同聊天互不阻塞）
    ② 心流门控（FLOW_ENABLED——verdict=reply 继续；defer/merge 经中央调度器
       一次性作业排程后返回空投递；skip 直接返回；见「心流门控」一节）
    ③ 每聊天会话懒建（身份键 channel:chat_id——见「会话语义」）
    ③′ 群聊多人格选择（GROUP_PERSONA_ENABLED——见「群聊多人格」一节；
       每个应答人格使用自己的会话 channel:chat_id:persona:<id>）
    ④ bind_persona_context(persona_id)
    ⑤ ChatService.reply(state, text)          组装 → LlmGateway（role=chat）
                                              → 用量记账自动发生（零通道侧
                                              额外用量行——通道层不重复记账）
    ⑥ 适配器发送 OutboundMessageModel（多人格逐条发送）
TelegramChannelRunner.send_message（发送面适配）
    ↓ 4096 分段（见「分段规则」）
TelegramClient（telegram/tg_client.py）
    ↓ 共享 httpx.AsyncClient → Bot API
Telegram 云端
```

装配（`app/main.py` lifespan，`_startup_channels` / `_shutdown_channels`）：

```
TELEGRAM_ENABLED=true 时：
    TelegramClient（token 解析——缺失即 CHANNEL_TOKEN_MISSING fail fast）
    → ChannelRouter（ChatService 工厂闭包 app.state.gateway +
       ChatSessionService；session_factory=session_scope 持久化装配）
    → TelegramChannelRunner → router.register_adapter(TELEGRAM, runner)
    → asyncio.create_task(runner.run()) + background_task_manager.register
    （app.state.channels_router / channels_runner / channels_task 三槽位）
QQ_ENABLED=true 时（同一 ChannelRouter——任一通道启用即构造）：
    NapCatOneBotClient（access token 间接——缺失不 fail fast）
    → NapCatQQAdapter → router.register_adapter(QQ, adapter)
    → get_login_info 启动校验（warn-and-continue——见「QQ 通道」一节）
    （无 runner / task——推送式入站零长驻任务）
WECOM_ENABLED=true 时（同一 ChannelRouter）：
    WeComLongConnectionClient（BotID + 密钥环境变量间接——缺失即跳过该通道）
    → WeComChannelRunner → router.register_adapter(WECOM, runner)
    → asyncio.create_task(runner.run()) + background_task_manager.register
    （app.state.channels_wecom_runner / channels_wecom_task 槽位）
TELEGRAM_ENABLED / QQ_ENABLED / WECOM_ENABLED 全为 false（缺省）：no-op——零通道对象构造
```

停机 LIFO（`shutdown_app_state` 六步）：`_shutdown_channels`（先于公共池——runner 消费共享 HTTP client）→ 后台任务 drain（`background_task_manager.shutdown`——fire-and-forget 在途任务先于池关闭排空）→ `pool_manager.stop()` → `close_database()` → `shutdown_shared_thread_pool()`。通道停机链：`request_stop()`（优雅——轮询态在途长轮询最多再等一个窗口；退避态的退避睡眠最长 `backoff_max_seconds`（默认 60s）不可被 request_stop 中断）→ bounded 等待（5s）→ 超时 `cancel` 兜底（cancel 语义覆盖两种形态——优雅等待不保证收口，5s 上限后强制终止；offset 已逐批持久化，重启安全）。

## 人格语义（锁定决策）

**主人格应答缺省通道会话（多激活可寻址模型）**：会话懒建经 `ChatService.create_session(db_session=...)` 内部解析当前主人格（persona 域注册表的事务性切换不变式消费面）；已建会话按 `persona_id` 保持在位（会话级寻址——通道消息不因主人格切换而换人格）。**群聊多人格**（`GROUP_PERSONA_ENABLED`，默认关闭）在同一群会话内按轮转 / 冷却 / 重复抑制选择应答者；每个应答人格拥有自己的持久化会话，会话行人格主键即消息应答归属（`replied_by`）。单聊语义不受影响。`reply` 全程经 `bind_persona_context(state.persona_id)` 绑定人格上下文（PersonaMixin 表作用域——与 chat 域会话持久化同语义）。

## 心流门控（`FLOW_ENABLED`——被延迟 / 合并的消息走中央调度器）

门控是聊天生成前的判定层：入站先经 `evaluate_flow_gate` 得到
`reply | defer | merge | skip` 与延迟毫秒 / 理由枚举（矩阵与配置见
[chat.md](chat.md#心流门控heartflow-gate)）。本层只负责把裁决映射到运行面：

```
FLOW_ENABLED=true
    ↓ 入站记录（FlowChatStateModel：到达时刻 / 积压 / 待回复缓冲）
    ↓ 节律门快照（RhythmService.gate_reply——睡眠 / 拟人延迟）
    ↓ 关系摘要（关系类现行事实派生——仅门控启用时读取）
    ↓ evaluate_flow_gate → verdict / delay_ms / reason
    ├─ reply  → 继续会话路由与即时投递
    ├─ skip   → 返回空投递结果（机器事件不落回复）
    └─ defer / merge
          ↓ scheduler_provider() → Scheduler.schedule_once(job, delay_ms)
          ↓ 返回空投递结果（本消息已被排程接管）
```

延迟批次执行（`_run_flow_batch`）在**同一 per-chat 锁**内运行：睡眠窗口
未结束或合并静默窗口未满时按剩余时长重排；否则按
`FLOW_MERGE_MAX_MESSAGES` 保留最近条目、换行连接后进入会话路由，投递
成功后清零积压与缓冲。调度器未就绪 / 排程失败时延迟裁决降级立即回复
（fail-safe，记 warning）；排程标记超过 `FLOW_PENDING_GRACE_MS` 未执行
视为任务丢失，后续入站重新排程。单条消息的所有失败仍走既有兜底隔离。

门控裁决 JSON 示例：

```json
{
  "verdict": "defer",
  "delay_ms": 27000000,
  "reason": "sleep_window"
}
```

延迟批次排程观测（日志面字段）：`key`（会话身份键）、`job`（一次性作业
名）、`delay_ms`、`used`（合并实际条数）、`dropped`（超上限丢弃条数）。

## 群聊多人格（`GROUP_PERSONA_ENABLED`——选择 / 轮转 / 冷却 / 归属）

启用后，群聊消息在启用人格集合（`PersonaService.list_addressable`）内
按确定性规则选择应答者；选择逻辑在 `chat/persona_rotation.py`（纯函数，
时间注入），本层负责会话 / 人格上下文 / 投递：

```
群聊入站
    ↓ list_addressable（主人格在前、其余主键升序）
    ↓ select_group_responders（冷却排除 → 上一轮未发言者优先 → 上限截断）
    ├─ 空选择 → 回退主人格单人格路径
    └─ 应答者列表
          ↓ 逐人格：会话键 channel:chat_id:persona:<id> 懒建（落库标题同值）
          ↓ bind_persona_context(persona_id) → ChatService.reply(peer)
          ↓ 适配器逐条发送（仅首条引用原消息；其余不引用）
          ↓ 应答记忆更新（最近应答时刻 + 上一轮应答者）
```

选择规则（锁定）：

| 条件 | 结论 | 理由枚举 |
|---|---|---|
| 功能关闭 / 单聊 | 空选择（既有单人格路径） | `disabled` / `not_group` |
| 无启用人格 | 空选择（回退主人格路径） | `no_candidates` |
| 冷却外且上一轮未发言者存在 | 优先该人格 | `rotation` |
| 可用者都刚发过言 | 允许重复（轮转退化） | `repeat_fallback` |
| 全部处于冷却 | 选最近应答时刻最早者 | `all_cooling_fallback` |

归属语义（`replied_by`）：群聊多人格模式下每个应答人格的会话标题带
`:persona:<id>` 后缀，`ff_chat_sessions.persona_id` 即该会话全部消息的
应答归属；消息行经会话外键归属，无需消息级冗余列，无迁移。单人格会话
标题保持 `channel:chat_id`（管理台会话页对多人格会话显示「群聊多人格 ·
人格#N」徽标）。

配置（`GROUP_PERSONA_` env 面）：

| 字段 | env 键 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `GROUP_PERSONA_ENABLED` | `false` | 群聊多人格开关 |
| `max_responders` | `GROUP_PERSONA_MAX_RESPONDERS` | `1` | 单条群消息应答人数上限（1-4） |
| `cooldown_ms` | `GROUP_PERSONA_COOLDOWN_MS` | `60000` | 同一人格两次应答的最小间隔毫秒 |

选择结论 JSON 示例：

```json
{
  "responder_persona_ids": [1, 3],
  "reason": "rotation",
  "cooling_persona_ids": [2]
}
```

失败语义：逐人格隔离——单人格回复 / 投递失败记日志并继续其余人格；
全部失败时抛首个异常，由 `handle_inbound` 既有兜底回发短中文提示
（不因一个模型故障吞掉整轮群聊应答）。

## 会话语义（每聊天懒建 + 持久化）

- **身份键**：`channel:chat_id`（如 `channel:telegram:10086`）——路由器内存字典维度；持久化会话标题同值（`ff_chat_sessions.title`，便于管理台溯源）；
- **懒建**：每聊天首条消息建会话（`create_session(db_session=...)` → 落库携 `session_id`）；其后消息复用同一 `ChatSessionStateModel`（内存回合累积 + `ChatService.reply` 逐回合持久化——`ff_chat_messages`，经 ChatService 自持 `session_scope`，通道层零额外持久化路径）；
- **重启行为（文档化边界）**：进程重启后路由器内存字典为空——同聊天新消息起新会话（新 `session_id`；历史行保留在库）。按身份键恢复既有会话属后续多通道扩展面（需会话身份列 / 查询 API，当前 memory 域未提供）；
- **纯内存降级形态**（`session_factory=None`——测试 / 诊断用）：会话创建不落库；真实 `ChatService` 将抛 `INTERNAL_ERROR` → 走兜底回发。

## 错误隔离（铁律）

**单条消息的任何失败不得击穿 runner**——`ChannelRouter.handle_inbound` 恒不抛异常：

| 失败类别 | 用户面行为 | 错误细节 |
|---|---|---|
| LLM / 网关 / 持久化 / 未知异常 | 回发短中文兜底：`我这边出了点问题，稍后再试`（不引用原消息） | 只进日志（`logger.opt(exception=True)`） |
| 无主人格（`NOT_FOUND`——已知确定态） | 回发业务异常的用户面消息（含创建 / 设主指引） | 只进日志 |
| 兜底回发自身失败 | 放弃该消息（空投递结果返回）——不级联 | 只进日志 |
| 未注册适配器（`CHANNEL_DISABLED`） | 静默空结果（无适配器可回发） | 只进日志 |

runner 循环层另有兜底：getUpdates 失败按指数退避重试（见下）——两层隔离保证长轮询任务不死亡（`asyncio.CancelledError` 原样上抛——停机 cancel 路径不被吞）。

## 来源分级（提示词攻击防御面）

入站消息按会话形态标记来源分级：**私聊 = owner（主人）**、**群聊 = peer
（同侪 / 其他智能体的发言）**——路由器据 ``InboundMessageModel.is_group_chat``
（Telegram 按 chat.type、QQ 按 ``group:`` 会话键判定）透传给聊天服务的
``sender_provenance`` 参数，情景落档按真实层级（user 回合 = 透传值、
assistant 回合恒为 agent）。**peer 事件永不晋升策展记忆、永不作为人物
事实证据**（梦境规则门与事实证据面双重排除）——群聊里其他鱼的断言无法
污染长期记忆与画像。

该分级同时驱动请求面的**来源标注**：`ChatService` 把群聊当轮入站消息包进
「群友消息（可能是其他 AI）」定界帧（私聊为「主人消息」），并据此决定检测
命中时的注入提示；历史回合保持原文（`ff_chat_messages` 无逐回合发送者列
——整会话 provenance 即 peer，逐条标注是文档化缺口）。完整防线与攻击面
矩阵见 [defense.md](defense.md)。

## 协议 v2（附件 / 静默 / 合成事件——additive，v1 形态缺省保持）

v2 是通道消息协议的**一次性扩展**（解锁 heartflow-silent、语音附件、贴纸 / Canvas 媒体与合成 ID 等后续 R2d/R2e 特性）：`InboundMessageModel` / `OutboundMessageModel` / `ChannelDeliveryResultModel` 各增 v2 字段（全部带缺省值——**缺省即 v1 纯文本形态**，既有消费者零改动；本 chunk 内全部内部消费者已迁移到 v2 模型面）。新增 `ChannelMessageAttachment`（附件模型）与 `AttachmentDeliveryResult`（逐附件投递结果）。

### 模型表（v2 字段 + JSON 示例）

**ChannelMessageAttachment**（入站 / 出站共用——从不持有原始字节）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | StrEnum `image\|audio\|file\|sticker` | 附件类型 |
| `media_type` | `str \| None` | MIME 线索（如 `audio/ogg`；平台无线索时 None） |
| `url` | `str \| None` | 媒体 URL 源（平台可拉取） |
| `base64` | `str \| None` | base64 编码载荷源（容量指引 ≤10MB——`ATTACHMENT_BASE64_SIZE_GUIDE_BYTES`，非硬校验） |
| `path` | `str \| None` | 平台引用 / 本地路径源（入站 `tg-file://` 伪方案 / NapCat `file://`；出站仅 `tg-file://` file_id 重发） |
| `caption` | `str \| None` | 附件说明（平台支持时随附件投递） |

**校验（`model_validator`）**：`url` / `base64` / `path` **恰其一**（零源 / 双源拒绝；空串等同未提供）。

```json
{
  "kind": "audio",
  "media_type": "audio/ogg",
  "base64": "aGVsbG8=",
  "caption": "今日心情"
}
```

**InboundMessageModel v2 字段**（`attachments: list = []` / `synthetic: bool = False` / `silent: bool = False`）：

```json
{
  "channel": "telegram",
  "chat_id": "10086",
  "message_id": "42",
  "sender_id": "7",
  "text": "看看这只猫",
  "ts_ms": 1731000000000,
  "attachments": [
    {"kind": "image", "media_type": null, "path": "tg-file://AgACAgIAAx0", "caption": "看看这只猫"}
  ],
  "synthetic": false,
  "silent": false
}
```

`synthetic=True` = 机器生成事件上浮（heartflow / mood——R2e 消费面），非人类消息；入站 `silent` 为平台上报的无通知上下文标记位（预留）。

**OutboundMessageModel v2 字段**（`attachments: list = []` / `silent: bool = False`）：

```json
{
  "chat_id": "10086",
  "text": "语音来了",
  "reply_to_message_id": "42",
  "attachments": [
    {"kind": "audio", "media_type": "audio/ogg", "base64": "aGVsbG8=", "caption": null}
  ],
  "silent": true
}
```

`silent=True` 请求免通知投递（Telegram `disable_notification`；QQ 无通知概念——优雅忽略 no-op）；引用回复只挂文本消息（附件投递不携带引用）。

**ChannelDeliveryResultModel v2 字段**（`attachment_results: list[AttachmentDeliveryResult] = []`——`{index, ok, error, provider_message_id}`）：

```json
{
  "channel": "telegram",
  "chat_id": "10086",
  "message_ids": ["900", "901"],
  "attachment_results": [
    {"index": 0, "ok": true, "error": null, "provider_message_id": "901"},
    {"index": 1, "ok": false, "error": "平台拒绝该图片", "provider_message_id": null}
  ]
}
```

`message_ids` v2 语义：文本分段 + 送达成功附件的平台消息一并计入。**部分成功语义**：单附件失败不上抛（`ok=False` + `error` 承载，文本与其余附件照常投递）；**文本段失败仍按 v1 上抛**（路由器兜底隔离）。

### 每适配器映射表（出站附件 kind → 平台发送面）

| kind | Telegram | QQ（OneBot v11） | 企业微信（长连接） |
|---|---|---|---|
| `image` | `sendPhoto`（photo） | `image` 段 | 上传 `type=image` → `aibot_send_msg`（`msgtype=image`） |
| `audio`（media_type 含 ogg/opus） | `sendVoice`（voice） | `record` 段 | 上传 `type=voice` → `msgtype=voice`（平台限 amr ≤2MB） |
| `audio`（其余 / 无线索） | `sendAudio`（audio） | `record` 段 | 同上（格式不符按逐附件失败承载） |
| `file` | `sendDocument`（document） | **不支持**（OneBot v11 无文件消息段——`ok=False` 逐附件结果承载，文本与其余附件照常投递） | 上传 `type=file` → `msgtype=file`（≤20MB） |
| `sticker` | `sendSticker`（sticker；**无 caption 参数——caption 丢弃**） | `image` 段（QQ 无独立贴纸段——贴纸即图片形态） | 上传 `type=image` → `msgtype=image`（企微无贴纸消息类型） |

出站媒体源形态：`url` → Telegram JSON 引用 / QQ 段 `file` 直传；`base64` → Telegram multipart 上传（httpx `files=`）/ QQ 段 `file="base64://<载荷>"` 包装；`path` → **仅接受 `tg-file://` 伪方案**（file_id 重发——适配器剥前缀透传），其余 path 形态 `VALIDATION_ERROR` 逐附件结果承载。QQ 侧 caption 无段级承载概念——丢弃（出站文本即伴随文本）。

投递顺序（两通道一致）：**先文本后附件**。Telegram 文本与每个附件各自独立平台消息（逐附件独立成败）；QQ 段数组**同消息**投递（`params.message = [text 段, 媒体段...]`——段内附件无独立失败面，动作失败整条上抛走 v1 语义）。

### `tg-file://` 伪方案契约（入站方向——下载免取 v2）

Telegram 真实媒体拉取需 **getFile + 文件 URL** 二段 Bot API 流程；v2 入站**不做下载**——附件以 `path="tg-file://{file_id}"` 伪方案承载平台文件引用，真实媒体 fetch deferred 到 R2d 贴纸 / 语音工作。photo 取最大尺寸（宽 × 高）的 `file_id`；voice / audio / document 的 `mime_type` 进 `media_type`（photo / sticker 无 MIME 线索 → None）；caption 挂首附件（`message.caption` 同时兜底为入站 `text`——媒体消息用户的伴随话语）；提取顺序 photo → voice → audio → document → sticker（真实 Bot API 消息至多一种媒体——固定顺序为确定性兜底）。

Telegram 入站路由门槛（v2）：文本与媒体互斥（Bot API 语义）——媒体消息以 **caption 兜底为文本**（有 caption 即路由）；**纯媒体无 caption 维持 v1 跳过语义**（不进路由器，offset 照常推进）；纯文本消息 v1 形态零变更。

### QQ 入站附件提取（段数组形态）

NapCat 段数组消息的 image / record 段提取为附件（引用形态优先序）：`data.url`（可下载 URL）→ `url` 源；`data.file` 为 `base64://` 前缀 → `base64` 源（载荷剥前缀）；其余 `data.file`（`file://` URL / 平台引用）→ `path` 源原样承载。**边界**：仅段数组形态提取（NapCat HTTP POST 上报缺省 `message_format=array`）；CQ 码字符串形态维持占位符降级（参数解析转义复杂度不抵增益——文档化决策）；无可用引用（url / file 皆缺）的媒体段跳过。

### 流式降级（锁定契约）

**v2 附件不参与流式**——流式回复恒为纯文本（网关工具面已强制文本流；通道面从未流式附件，v2 将其显式化为契约）：附件到达适配器后整投递，由适配器聚合逐附件结果。

### 消费者边界（诚实边界）

v2 只交付 **wire**（模型 + 适配器 + 测试）。**表情包收发桥**已接入（`STICKER_ENABLED=true` 时装配——见下节）；语音 / 合成事件消费者仍待后续切片。当前 `ChatService.reply` 回复循环仍为纯文本（回合内容不落附件、`ff_chat_messages` 零 schema 变更——附件不持久化）；duty 属主投递维持纯文本（无附件）。

### 表情包收发桥（`STICKER_ENABLED`——默认关闭）

`ChannelRouter` 的可选钩子 `sticker_ingest_hook` / `sticker_reply_hook`（缺省 None = 既有语义零变化）在 app 装配时由 `StickerService` 接线：

```
入站（handle_inbound）
    ↓ 门控判定前：入站 attachments 含 kind=sticker？→ 以应答人格上下文
      调用收录桥（闭包开短事务 → StickerService.ingest_bytes）
    ↓ 仅 base64 已持有字节可收录；tg-file:// / url 引用跳过（二次下载
      属后续交付面）
    ↓ 收录失败仅告警——回复链路不受影响

出站（_send_single_reply）
    ↓ 本轮入站带 sticker 且 STICKER_CHANNEL_REPLY_ENABLED=true？
       ├─ 是 → 取当前人格最新可读素材 → ChannelMessageAttachment(kind=sticker,
       │        media_type=记录 MIME, base64=文件字节) 随文本投递
       └─ 否 → 出站零附件（v1 形态）
    ↓ 取表情 / 平台附件投递失败：文本照常投递（逐附件结果由适配器聚合）
```

延迟 / 合并批次只承接文本，不携带入站附件；收录已在门控前完成，回表情仅即时回复路径生效（文档化边界）。群聊多人格模式下每位应答人格使用自己的素材库（人格隔离轴与文本会话一致）。


## offset 持久化（重启不重消费）

- **游标语义**：批末推进 `offset = max(update_id) + 1`；下一轮 `getUpdates(offset=...)` 携带（服务端据此丢弃更早更新——确认语义）；非消息更新与无文本消息**跳过路由但 offset 照常推进**（否则服务端无限重投）；
- **状态文件**：`state_dir/telegram_offset.json`（`{"offset": N}` 单键）；
- **原子写**：临时文件（`.tmp-<epoch-ms>`）+ flush + fsync + `os.replace` 原子换名（config 域 registry_admin 同模式）；**不做 .bak 备份轮换**——offset 是单键可重建状态，不值得备份成本（文档化决策，与 registry_admin 的差异点）；
- **崩溃重放窗口（批末持久化的取舍）**：offset 在**整批消息处理完后**才推进落盘——批内消息已回复、但批末 offset 未持久化时进程崩溃，重启后该批整体重放，**已回复过的消息会再次回复**（用户可见的重复回复；原子写只保证「旧值或新值、无半写态」，不缩短批内窗口）。取舍依据：逐 update 持久化把每次回复后置一次 fsync（ms 级放大到消息级），对单用户陪伴机器人的低流量形态，批末持久化 + 罕见崩溃窗口的重复回复是更优交换；窗口大小 = 单批处理时长（LLM 回复耗时 × 批内消息数）；
- **降级**：文件损坏 → 告警 + 视为首次拉取；写失败 → 告警 + 内存态继续——两种降级均不让 runner 死；
- **阻塞 I/O 下沉**：读写均经 `run_blocking_ops`（core 共享线程池 / 事件循环默认执行器——不占事件循环）。

## 去重与退避

- **去重**：在途 `update_id` 已见集合（FIFO 窗口 1024 条）兜底「确认前重投」（服务端 offset 确认是主去重面——集合只覆盖确认间隙的双投）；
- **退避**：连续失败按 `backoff_base_seconds × 2^(n-1)` 指数增长、`backoff_max_seconds` 封顶；成功即复位。仅覆盖 getUpdates 链路（发送失败在路由器层兜底隔离，不进退避）。

## 启动校验与 token 间接

- **token 永不落配置树**：`TelegramSettings.bot_token_env` 只持有变量名（默认 `TELEGRAM_BOT_TOKEN`）——与 llm `api_key_env` / SeekDB `password_env` 同间接模式；实际 token 由 `TelegramClient` 构造期从环境变量读取；
- **缺失 fail fast**：`TELEGRAM_ENABLED=true` 且变量缺失 / 纯空白 → lifespan 装配期抛 `CHANNEL_TOKEN_MISSING`（中文错误信息含变量名）——app 拒绝启动；
- **无效 fail fast**：runner `run()` 首步 getMe 校验——401/403（token 无效 / 撤销）抛 `CHANNEL_TOKEN_MISSING` 终止任务（清晰错误日志 + lifespan done-callback 落 ERROR）；暂态失败（网络 / 5xx）仅告警并进入退避循环（不因启动时网络抖动杀通道）。

## sendMessage 4096 分段规则

| 规则 | 行为 |
|---|---|
| 文本 ≤ 4096 字符 | 单条发送 |
| 超限 | 每段「前 4096 字符窗口」内找**最后一个换行符**切分（段落不跨语义行；切点换行符被消费——下段不以空行开头） |
| 窗口内无换行（或仅在首位） | 硬切 4096 |
| `reply_to_message_id` | 只挂首段（续文逐段引用会渲染成多条引用噪声） |
| 空文本 / 非数字引用 ID | `VALIDATION_ERROR`（调用方 bug 面——不进重试） |

## 错误码映射（channels 域段——core ErrorCode 注册表既有成员）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| token 环境变量缺失 / 为空（构造期） | `CHANNEL_TOKEN_MISSING` | 500 | 否（fail fast） |
| HTTP 401 / 403 或 ok=False + error_code 401/403（token 无效） | `CHANNEL_TOKEN_MISSING` | 500 | 否 |
| 其余 HTTP 非 200 / ok=False / 传输异常 / JSON 非法 | `CHANNEL_API_ERROR` | 503 | runner 退避 |
| 路由器收到未注册适配器通道的入站消息 | `CHANNEL_DISABLED`（**仅预留不 raise**——运行时为静默空结果，见「错误隔离」表；409 映射未接线） | （预留） | 否（装配面） |

## QQ 通道（NapCat / OneBot v11）

### 架构：推送式入站 vs Telegram 拉取式

```
QQ（NapCat 部署机服务，OneBot v11 实现）
    ↓ HTTP POST 事件上报（NapCat 侧配置上报 URL 指向本后端）
POST /api/v1/channels/qq/webhook（app/api/channels_qq.py）
    ↓ token 校验（webhook_token_env 配置时恒时比较 Bearer）
qq_webhook.process_webhook_event（qq/qq_webhook.py）
    ① Ob11EventModel 容错解析（非法载荷 = 确认并忽略）
    ② 大类过滤（仅 post_type=message 进路由；notice / request /
       meta_event 确认并忽略）
    ③ message_id 去重（进程内有界 FIFO 窗口 1024）
    ④ 消息展平（段数组 / CQ 码字符串 → 纯文本；非文本段占位符化）
    ⑤ InboundMessageModel（chat_id grammar + epoch 秒 ×1000 归一）
ChannelRouter.handle_inbound（channel_service.py——与 telegram 共用）
    ↓ ①-⑤ 同 telegram（会话懒建 / 人格绑定 / ChatService.reply）
NapCatQQAdapter.send_message（发送面适配）
    ↓ chat_id grammar 解析 → NapCatOneBotClient（单一动作对象包裹 POST）
NapCat HTTP API（POST {http_api_base_url} {"action": ..., "params": {...}}）
    ↓ {"status": "ok"|"async", "retcode": 0, "data": {"message_id": N}}
QQ
```

与 Telegram 的根本差异：**入站方向相反**。Telegram 是后端长轮询**拉取**（getUpdates + offset 游标 + runner 后台任务）；QQ 是 NapCat 主动**推送**到本后端 webhook（后端零长驻任务、零游标状态文件）。停机语义随之简化：QQ 无 runner 可停（适配器注册态随 `channels_router` 槽位复位；共享池 client 生命周期归 pools）。

### chat_id grammar（锁定决策）

| OutboundMessageModel.chat_id | NapCat 动作 | 路由器会话身份键 |
|---|---|---|
| `private:<user_id>` | `send_private_msg` | `channel:qq:private:<uid>` |
| `group:<group_id>` | `send_group_msg` | `channel:qq:group:<gid>` |

- grammar 非法（未知前缀 / 非数字 / 非正整数）→ `VALIDATION_ERROR`（调用方 bug 面——webhook 入站只产出合法 grammar，出站兜底路径亦复用入站 chat_id）；
- 身份键含通道名 + 目标类型前缀——QQ 私聊 / 群聊 / Telegram 会话三者天然隔离（无跨通道撞键可能）；
- **引用回复不透传**（文档化偏差）：`reply_to_message_id` 在出站时丢弃——OneBot v11 引用需构造 `reply` 消息段（渲染为引用块），与通道中立纯文本出站语义不匹配；兜底消息本就不引用，正常回复的引用增益不抵段组装复杂度（后续通道增强面再评估）。

### 出站动作包裹（锁定决策——单一对象形态）

`POST {http_api_base_url}` 请求体 `{"action": "<动作名>", "params": {...}}`（NapCat HTTP API 兼容「动作名进 URL 路径」形态——本工程不使用，单一形态锁定防双形态漂移）。成功判定：`retcode == 0` **且** `status ∈ {ok, async}`（`async` = 已受理异步执行——亦为成功）；`data.message_id` 归一为投递结果 `message_ids`（wire 未回传时为空列表——送达成功但无引用凭据）。出站 `params.message` 直接传纯文本字符串（OneBot v11 message 字段合法形态之一）；**不做超长分段**——QQ 客户端对长文本自行折行，平台未文档化单消息硬上限（tg 4096 分段规则不适用）。

### 消息展平（入站文本归一）

OneBot v11 `message` 字段双形态：段数组（`[{"type": "text", "data": {...}}]`）或 CQ 码字符串（`"你好[CQ:image,file=x.jpg]"`）——双形态均解析：

- `text` 段取 `data.text` **无缝拼接**（多 text 段是同一消息的连续切片）；CQ 码字符串的码外字面文本保留；
- 非文本段按占位符表降级（人格可见的降级形态）：

| 段类型 | 占位符 | 段类型 | 占位符 |
|---|---|---|---|
| `image` | `[图片]` | `at` | `[提及]` |
| `face` | `[表情]` | `reply` | `[引用回复]` |
| `record` | `[语音]` | `share` / `music` | `[分享]` / `[音乐]` |
| `video` | `[视频]` | `red_packet` / `poke` / `dice` | `[红包]` / `[戳一戳]` / `[骰子]` |
| `forward` | `[合并转发]` | `json` / `xml` | `[卡片消息]` |
| 未知类型 | `[其它消息]` | （CQ 字符串内 `text` 罕见形态） | 空串 |

- 展平后空文本（无段 / 空串）跳过路由；纯图片消息展平为 `[图片]` **进路由**（人格可感知非文本内容——与 tg「非文本消息过滤不进路由」的差异点，QQ 占位符化后即文本）。

### 去重与顺序（文档化边界）

- **去重**：`message_id` 进程内有界 FIFO 窗口（1024——tg_runner 同语义同容量）；兜底「NapCat 重启 / 网络重试」窗口内的重投。进程重启清零（重启窗口内的重投可能重消费——QQ 无 offset 持久化等价物，OneBot v11 HTTP POST 上报无确认游标；NapCat 侧重试窗口短，影响面小）；
- **顺序**：HTTP POST 上报不保证事件顺序（群消息并发上报可能乱序到达）——路由器以 per-chat `asyncio.Lock` 串行消费**到达序**（同聊天全链路串行：会话懒建 + 回复 + 兜底回发；不同聊天互不阻塞；并发首消息不重复建会话行）；到达序非平台发送序；
- **message_id 负值**：NapCat 实现下常见——合法形态（去重键与入站 `message_id` 字符串化承载）。

### webhook 确认语义（铁律）

**webhook 处理器恒不抛异常**（逐事件隔离：载荷非法 / 字段缺失 / 路由器内部异常只进日志）；端点对接受 / 忽略 / 隔离失败**恒 200 `{"status": "ok"}`**——OneBot 上报方对非 200 会**重试**，确认即防重投风暴（与 tg runner「非消息更新 offset 照常推进」同哲学）。仅两类请求形态错误走 4xx：鉴权失败（401）/ 请求体非 JSON 对象（400）。

### 启动校验：warn-and-continue（与 telegram 的差异）

| | Telegram | QQ |
|---|---|---|
| token 缺失（构造期） | fail loud（app 拒绝启动） | 不 fail（NapCat 未开启鉴权是合法形态） |
| 启动校验失败（getMe / get_login_info） | token 无效 → fail fast 终止 runner 任务；暂态失败 → 告警进退避循环 | **一律 warn-and-continue**——适配器仍注册，出站失败逐消息经路由器兜底隔离 |
| 理由 | 长驻轮询任务——token 无效时任务无意义 | 推送式入站 + 无长驻任务；NapCat 常晚于后端就绪（部署时序），fail fast 只会因部署顺序杀掉整个 app |

### NapCat 部署要点（部署机独立服务）

1. **NapCat 服务**：部署机安装并登录 QQ 账号（NapCat 为独立进程，暴露 OneBot v11 HTTP API——默认 `http://127.0.0.1:3000`，即 `QQ_HTTP_API_BASE_URL` 指向目标）；
2. **HTTP POST 上报配置**：NapCat 侧配置事件上报 URL 指向本后端 `POST /api/v1/channels/qq/webhook`（公网部署经反代暴露该端点；上报携带 access token 时走 `Authorization: Bearer` 头）；
3. **token 双侧配置**：NapCat 侧开启 access token 鉴权时，本后端设 `QQ_ACCESS_TOKEN`（出站 Bearer）；上报校验 token 设 `QQ_WEBHOOK_TOKEN`（入站恒时比较——**空 = 鉴权关闭，仅限同机 / 可信内网部署**，公网暴露必须配置否则任何人可注入伪造事件）；
4. **群 vs 私聊语义**：私聊会话身份 `private:<user_id>`（一人一会话）；群聊 `group:<group_id>`（一群一会话，群内所有成员共享同一人格会话——发送者 ID 经 `sender_id` 承载）。

### 错误码映射（QQ 面——复用 channels 域既有码表）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| access token 无效（NapCat 401/403） | `CHANNEL_TOKEN_MISSING` | 500 | 否（配置面） |
| 其余 HTTP 非 200 / retcode 非 0 / status=failed / 传输异常 / JSON 非法 | `CHANNEL_API_ERROR` | 503 | 逐消息隔离 |
| chat_id grammar 非法 / 空文本 | `VALIDATION_ERROR` | 422 | 否（调用方 bug） |
| webhook 鉴权失败（入站） | `UNAUTHORIZED` | 401 | 否 |
| webhook 请求体非 JSON 对象 | `BAD_REQUEST` | 400 | 否 |
| 路由器收到未注册适配器通道的入站消息 | `CHANNEL_DISABLED`（**仅预留不 raise**——同 telegram 段；运行时静默空结果） | （预留） | 否（装配面） |

### Settings（QQ 分节）

env 面（`env_prefix=QQ_`；嵌套覆写 `CHANNELS__QQ__*` 亦可用）：

| 字段 | env 变量 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `QQ_ENABLED` | `false` | 是否装配 QQ 通道适配器（false = 零通道对象构造） |
| `http_api_base_url` | `QQ_HTTP_API_BASE_URL` | `http://127.0.0.1:3000` | NapCat HTTP API 基础 URL（单一动作对象包裹 POST 目标） |
| `access_token_env` | `QQ_ACCESS_TOKEN_ENV` | `QQ_ACCESS_TOKEN` | **出站** access token 环境变量名（间接——本值以 `Authorization: Bearer` 头携带；空 = NapCat 未开启鉴权形态） |
| `webhook_token_env` | `QQ_WEBHOOK_TOKEN_ENV` | `QQ_WEBHOOK_TOKEN` | **入站**校验 token 环境变量名（间接——恒时比较；**空 = 鉴权关闭**，见部署要点 3） |
| `request_timeout_seconds` | `QQ_REQUEST_TIMEOUT_SECONDS` | `15` | 单次动作请求超时秒数（> 0） |

```bash
# .env 形态（分节 env 前缀）
QQ_ENABLED=true
QQ_HTTP_API_BASE_URL=http://127.0.0.1:3000
QQ_ACCESS_TOKEN=your-napcat-access-token      # access_token_env 指向的本值（永不落 yaml）
QQ_WEBHOOK_TOKEN=your-webhook-verify-token    # webhook_token_env 指向的本值
QQ_REQUEST_TIMEOUT_SECONDS=15
```

```yaml
# config.yaml 不承载通道配置（env 面专属——与 telegram 同约定）；
# 嵌套覆写形态（进程环境变量，优先级高于 .env）：
#   CHANNELS__QQ__ENABLED=true
#   CHANNELS__QQ__REQUEST_TIMEOUT_SECONDS=9
```

隔离构造（测试 / 工具）：

```python
from packages.harness.config.models import ChannelsSettings, QqChannelSettings
from packages.harness.config.settings import build_isolated_settings

settings = build_isolated_settings(
    channels=ChannelsSettings(
        qq=QqChannelSettings(enabled=True, access_token_env="QQ_TEST_ACCESS_TOKEN")
    )
)
```

> 注：`backend/.env.example` 键面以 `.env.example`（`pixi run gen-examples` 生成）为真源，本表为语义说明（`QQ_*` / 双 token 本值变量）。

## 企业微信智能机器人长连接（WECOM）

### 协议依据与装配形态

协议来源：企业微信开发者中心《智能机器人长连接》
（`https://developer.work.weixin.qq.com/document/path/101463`）与《接收消息》
（`https://developer.work.weixin.qq.com/document/path/100719`）。长连接（WebSocket）
模式面向**无固定公网 IP / 高实时性**部署：开发者服务器主动连到
`wss://openws.work.weixin.qq.com`，订阅鉴权后由平台经同一连接下发消息回调，
回复与主动推送也走该连接，**不需要回调 URL、不需要消息加解密**。

凭据面（与经典自建应用的区别）：长连接的订阅鉴权只要 **BotID + Secret**
（管理后台「智能机器人 → API 模式 → 长连接」页的「获取凭证」），Secret 是长连接
专用密钥，与回调模式的 Token / EncodingAESKey 不是同一凭据；素材上传同样在长连接
命令面完成，不走向 `/cgi-bin/gettoken` 的 HTTP 面——因此本工程 `WECOM_` env 面
**不引入** `corp_id` / `agent_id` / access_token 三项（无消费点，出现即死配置）。

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `wecom/wecom_models.py` | wire Model Classes（Unit 文件）：订阅 / 心跳 / 回调 / 应答 / 回复 / 主动推送 / 素材上传命令；入站映射（会话键 / 文本归一 / 附件 / 合成来源标记）；markdown 字节分段；chat_id grammar 解析 | `WeComCallbackFrameModel` / `WeComAckModel` / `WeComCommandModel` / `map_wecom_callback_to_inbound` / `parse_wecom_chat_id` / `split_wecom_markdown_text` / `build_*_command` |
| `wecom/wecom_client.py` | 长连接客户端：订阅 / 请求应答关联（req_id）/ 应用层心跳 / 指数退避重连 + 抖动 / 单连接不变式 / 被踢下线处理 / 优雅停机 / 素材分片上传；连接传输面经 `connector` 注入（生产 = websockets，测试 = 进程内 fake） | `WeComLongConnectionClient` / `WeComConnectionState` / `WeComConnectionProtocol` / `open_websocket_connection` |
| `wecom/wecom_runner.py` | runner：回调去重与回复上下文登记 / 有界入站队列 + 固定 worker / 出站文本分段与附件上传投递 / 逐附件失败承载 / 生命周期收尾 | `WeComChannelRunner` / `WeComMessageDedupWindow` |

依赖方向：wecom → channels（通道中立模型 / `ChannelRouter`）+ core（errors /
time_utils / thread_pool）；WebSocket 传输用 `websockets`（由声明的
`uvicorn[standard]` 运行依赖提供，未新增第三方依赖）；附件 `url` 源下载经
pools 共享 `httpx.AsyncClient`（硬规则：无私建连接）；禁止自建线程池（worker 是
原生 async 任务）。

装配（`app/main.py` 的 `_startup_wecom_channel`）：

```
WECOM_ENABLED=true
    ↓ WeComLongConnectionClient（BotID + 密钥环境变量名 → 构造期解析密钥；
      缺失即 CHANNEL_TOKEN_MISSING）
    ↓ WeComChannelRunner（入站队列 + 去重 + 回复上下文 + 出站投递）
    ↓ router.register_adapter(ChannelType.WECOM, runner)
    ↓ asyncio.create_task(runner.run()) + background_task_manager.register
    （app.state.channels_wecom_runner / channels_wecom_task 槽位）
WECOM_ENABLED=false（缺省）：no-op——零连接 / 零后台任务
```

失败语义（与 telegram 的差异，锁定）：telegram token 缺失 fail loud（app 拒绝
启动）；企微装配期的配置 / 凭据失败记 ERROR 后**跳过该通道**——长连接是并行的
新增通道，其配置瑕疵不连带杀掉已在工作的 telegram / QQ。运行期故障（握手失败 /
断连 / 订阅被拒 / 命令失败）由客户端退避重连与 runner 逐消息 / 逐附件隔离消化，
同样不波及其余通道。

### 入站链路（回调 → 路由）

```
企业微信（长连接 aibot_msg_callback）
    ↓ 客户端收帧循环（应答按 req_id 归位；回调帧交处理器）
WeComChannelRunner._handle_callback
    ① 事件帧分流（aibot_event_callback 不产出人类回合：enter_chat /
       template_card_event / feedback_event 仅记日志；disconnected_event 由
       客户端状态机消费并停止重连）
    ② msgid 去重（官方指定排重键；进程内有界 FIFO 窗口 1024，超容淘汰最旧）
    ③ 回复上下文登记（req_id + 到达时刻——出站 aibot_respond_msg 的寻址原料）
    ④ map_wecom_callback_to_inbound（会话键 / 文本归一 / 附件 / 群聊标记 /
       compat_info → synthetic）
    ⑤ 有界入站队列（容量 64；满则丢弃并记 ERROR——平台重投由 msgid 去重兜底）
    ⑥ 固定 worker（默认 2）→ ChannelRouter.handle_inbound
       （per-chat 锁 / 心流门控 / 群聊多人格 / 人格绑定 / ChatService.reply /
        兜底回发——与 telegram、QQ 完全同一套路由语义）
```

收帧循环与回复执行**解耦**（回调入队后立即返回）：一次 LLM 回复耗时数秒，若在
收帧循环内同步等待，心跳应答与后续回调都会被阻塞，连接会被平台判定失活。

入站字段（官方文档子集——`extra="ignore"` 容忍平台加字段）：

| wire 字段 | 类型 | 说明 | 本工程消费 |
|---|---|---|---|
| `cmd` | string | `aibot_msg_callback` / `aibot_event_callback` | 分流（仅消息回调进路由） |
| `headers.req_id` | string | 回调请求标识 | 回复上下文的寻址键（`aibot_respond_msg` 必须透传） |
| `body.msgid` | string | 回调唯一标志（官方：用于事件排重） | 去重键 + 入站 `message_id` |
| `body.chattype` | string | `single` / `group` | 会话键前缀与 `is_group_chat` |
| `body.chatid` | string | 群聊会话 ID（仅群聊） | 群聊会话键 `group:<chatid>` |
| `body.from.userid` | string | 发送者 userid（超管创建时为明文，否则为加密 userid） | 单聊会话键 `single:<userid>` + 入站 `sender_id` |
| `body.msgtype` | string | `text` / `image` / `mixed` / `voice` / `file` / `video` / `event` | 文本归一与附件提取分派 |
| `body.text.content` | string | 文本内容 | 入站文本 |
| `body.voice.content` | string | 平台已转写的语音文本 | 入站文本（无附件——音频字节不在回调面） |
| `body.mixed.msg_item[]` | array | 图文混排子项（`text` / `image`） | 逐项拼接为文本 + 图片附件 |
| `body.image.url` / `body.file.url` / `body.video.url` | string | 资源下载地址（5 分钟有效，**密文**） | 附件 `url` 源 |
| `body.*.aeskey` | string | 逐链接解密密钥（AES-256-CBC，IV 取前 16 字节） | 保留在 wire 模型（消费者解密面） |
| `body.quote` | object | 用户引用的原消息（`msgtype` + 对应结构体） | 不消费（历史轮次已承载上下文） |
| `body.compat_info.source` | string | 非普通用户消息来源（`ai_assistant` / `channel`） | `synthetic=True`（机器来源面） |

入站归一表：

| 输入形态 | 归一文本 | 附件 |
|---|---|---|
| 文本 | `text.content` 原文 | 无 |
| 语音 | `voice.content`（平台转写） | 无 |
| 图文混排 | 文本项原文 + 图片项占位符 `[图片]` 逐项拼接 | 各图片项 → `image`（`url` 源） |
| 图片 | `[图片]` | `image`（`url` 源） |
| 文件 | `[文件]` | `file`（`url` 源） |
| 视频 | `[视频]` | `file` + `media_type=video/mp4` |

不可路由（返回 None 并跳过，不投递路由器）：缺 `msgid`、未知 `chattype`、群聊缺
`chatid`、单聊缺发送者、未知 `msgtype`、归一文本为空。

入站回调 JSON 示例（群聊文本，字段名与官方文档一致）：

```json
{
  "cmd": "aibot_msg_callback",
  "headers": {"req_id": "cb-req-7f3a"},
  "body": {
    "msgid": "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI=",
    "aibotid": "bot-1a2b3c",
    "chatid": "chat-8899",
    "chattype": "group",
    "from": {"userid": "zhangsan"},
    "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?...",
    "msgtype": "text",
    "text": {"content": "@小鱼 今天好累"},
    "compat_info": {"source": "ai_assistant"}
  }
}
```

映射结果（通道中立模型）：

```json
{
  "channel": "wecom",
  "chat_id": "group:chat-8899",
  "message_id": "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI=",
  "sender_id": "zhangsan",
  "text": "@小鱼 今天好累",
  "ts_ms": 1731000000000,
  "attachments": [],
  "synthetic": true,
  "silent": false,
  "is_group_chat": true
}
```

`silent` 在企微无免通知投递语义——入站恒 False，出站字段被适配器优雅忽略
（与 QQ 同约定）。媒体资源的 `aeskey` 属 wire 层：通道中立附件只承载 `url`，
需要字节的消费者（如贴纸库）按 wire 模型取 `aeskey` 解密，本层不下载不落盘。

### 出站链路（文本 / 附件）

```
OutboundMessageModel
    ① chat_id grammar 解析（single:<userid> → chat_type=1 / group:<chatid> → 2）
    ② 文本分段（markdown content ≤20480 字节 UTF-8；换行边界优先、硬切兜底）
    ③ 首段：回复上下文新鲜 → aibot_respond_msg（msgtype=markdown，透传回调
       req_id）；上下文缺失 / 超过回复窗口 → aibot_send_msg
       续段：恒 aibot_send_msg（同一 req_id 多条回复的语义官方未文档化）
    ④ 附件：解析字节（base64 直接 / url 经共享 HTTP 池下载 / path 本地读）
       → aibot_upload_media_init / _chunk / _finish 取 media_id
       → aibot_send_msg（image / voice / file）
    ⑤ ChannelDeliveryResultModel（message_ids 恒空——平台应答只有
       errcode / errmsg；attachment_results 逐附件结局）
```

回复与推送的寻址差异（官方文档）：`aibot_respond_msg` 必须回填触发该会话的
消息回调 `req_id`，且在收到回调后 24 小时内有效；`aibot_send_msg` 以
`chatid` + `chat_type` 寻址，要求用户先在会话里给机器人发过消息（回复场景天然
满足）。通道中立出站模型不携带 `req_id`，因此回复上下文由 runner 按会话登记并
用 `WECOM_REPLY_WINDOW_SECONDS` 判定时效——过期即降级主动推送，避免把陈旧
`req_id` 发给平台。

出站命令 JSON 示例：

```json
{"cmd": "aibot_respond_msg", "headers": {"req_id": "cb-req-7f3a"},
 "body": {"msgtype": "markdown", "markdown": {"content": "辛苦了，先喝口水"}}}
```

```json
{"cmd": "aibot_send_msg", "headers": {"req_id": "req-9b1c"},
 "body": {"chatid": "8899", "chat_type": 2, "msgtype": "markdown",
          "markdown": {"content": "**提醒**：今晚十点开会"}}}
```

```json
{"cmd": "aibot_send_msg", "headers": {"req_id": "req-9b1d"},
 "body": {"chatid": "zhangsan", "chat_type": 1, "msgtype": "image",
          "image": {"media_id": "1G6nrLmr5EC3MMb_-zK1dDdzmd0p7cNliYu9V5w7o8K0"}}}
```

素材上传（官方文档「上传临时素材」）：初始化拿 `upload_id` → 逐片上传（单片明文
≤512KB、≤100 片、分片可乱序且幂等）→ 完成返回 `media_id`（素材 3 天有效，
上传会话 30 分钟有效）。

```json
{"cmd": "aibot_upload_media_init", "headers": {"req_id": "req-up1"},
 "body": {"type": "image", "filename": "image.png", "total_size": 9,
          "total_chunks": 1, "md5": "5d41402abc4b2a76b9719d911017c592"}}
```

```json
{"cmd": "aibot_upload_media_chunk", "headers": {"req_id": "req-up2"},
 "body": {"upload_id": "up-123", "chunk_index": 0, "base64_data": "aGVsbG8="}}
```

```json
{"cmd": "aibot_upload_media_finish", "headers": {"req_id": "req-up3"},
 "body": {"upload_id": "up-123"}}
```

```json
{"headers": {"req_id": "req-up3"}, "errcode": 0, "errmsg": "ok",
 "body": {"type": "image", "media_id": "1G6nrLmr5EC3MMb_-zK1dDdzmd0p7cNliYu9V5w7o8K0",
          "created_at": "1380000000"}}
```

附件映射表（v2 kind → 上传素材类型 / 推送消息类型）：

| kind | 上传素材 `type` | 推送 `msgtype` | 平台限制 |
|---|---|---|---|
| `image` | `image` | `image` | ≤10MB；png / jpg / jpeg / gif |
| `sticker` | `image` | `image` | 企微无独立贴纸消息类型——贴纸即图片 |
| `audio` | `voice` | `voice` | ≤2MB；amr |
| `file` | `file` | `file` | ≤20MB |
| （视频载荷以 `file` 类承载） | `file` | `file` | 附件投递不产出 `video` 消息 |

出站边界（文档化）：`reply_to_message_id` 不透传（企微消息体无引用字段——引用是
入站侧 `quote` 结构）；`caption` 丢弃（image / file 消息无说明字段，出站文本即
伴随文本）；`message_ids` 恒空（应答不含消息 ID）；单附件失败不上抛
（`AttachmentDeliveryResult.ok=False` + 原因，文本与其余附件照常投递），文本段
失败按 v1 语义上抛由路由器兜底。平台频率限制（单会话 30 条/分钟、1000 条/小时）
由平台侧强制；超限表现为命令应答非零 errcode，走同一错误映射。

### 连接生命周期（订阅 / 心跳 / 重连 / 停机）

```
IDLE → CONNECTING（wss 握手）→ 订阅（aibot_subscribe：BotID + Secret）
    ├─ 订阅被拒（errcode != 0）→ CHANNEL_TOKEN_MISSING（fail fast：凭据 /
    │   机器人身份错误重试无意义，任务带清晰错误终止）
    └─ READY（心跳任务 + 收帧接管）
          ├─ 心跳应答超时 / 收帧异常 / 握手失败 → BACKOFF（指数退避 + 抖动）→ 重连
          ├─ disconnected_event（新连接踢掉本连接）→ KICKED（停止重连）
          └─ request_stop → 关闭连接 → STOPPED（run 正常返回）
```

| 语义 | 实现 |
|---|---|
| 单连接不重复 | `run()` 以在途任务句柄幂等——重复调用直接返回；官方约束「每个机器人同一时间只能保持一个有效长连接」，多实例并行会互相踢 |
| 心跳 | 应用层 `{"cmd": "ping"}` 按 `WECOM_HEARTBEAT_INTERVAL_SECONDS`（默认 30s，官方建议值）发送；单次应答超时 `WECOM_HEARTBEAT_TIMEOUT_SECONDS`（默认 10s）即关闭连接交 supervisor 重连——半死连接不会继续吞消息 |
| 重连 | `WECOM_BACKOFF_BASE_SECONDS × 2^(n-1)` 指数增长、`WECOM_BACKOFF_MAX_SECONDS` 封顶、叠加 `WECOM_BACKOFF_JITTER_RATIO` 比例抖动（默认 0.3）；成功订阅即复位计数。退避等待可被停机请求即时打断 |
| 被踢下线 | 收到 `disconnected_event` 记 WARNING、置 KICKED 并停止重连（继续重连会与新连接反复互踢——官方建议主备切换而非多活） |
| 优雅停机 | `request_stop()` 置位 → 收帧循环与退避等待立即收口 → 关闭 WebSocket → worker 收尾；app 停机路径 bounded 等待 5s，超时 `cancel` 兜底 |
| 观测 | 连接建立 / 订阅成功 / 就绪 / 断开退避 / 被踢 / 停机均落日志（含 endpoint、BotID、连续失败次数、退避秒数）；`state` / `reconnect_attempt` 暴露状态机读数 |

命令应答关联：每条出站命令生成独立 `req_id` 并在在途表登记，应答按 `req_id`
归位；超时、平台拒绝、连接中断都会拒绝在途请求（`CHANNEL_API_ERROR`），不悬挂。

### 错误码映射（企微面——复用 channels 域既有码表）

| 失败类别 | 错误码 | HTTP | 可重试 |
|---|---|---|---|
| 密钥环境变量缺失 / 空白（构造期） | `CHANNEL_TOKEN_MISSING` | 500 | 否（跳过该通道） |
| 订阅被拒（errcode != 0） | `CHANNEL_TOKEN_MISSING` | 500 | 否（凭据 / 身份面） |
| 应答超时 / 平台非零 errcode / 连接未就绪 / 收帧断连 | `CHANNEL_API_ERROR` | 503 | 断开类由 supervisor 退避重连；命令类逐消息隔离 |
| chat_id grammar 非法 / 文本为空 / 附件过小或超类型上限 | `VALIDATION_ERROR` | 422 | 否（调用方 / 载荷面） |
| 单附件失败（下载失败 / 上传被拒 / 格式不符） | 不抛出 | — | 逐附件结果 `ok=False` 承载 |

### Settings（企微分节）

env 面（`env_prefix=WECOM_`；嵌套覆写 `CHANNELS__WECOM__*` 亦可用）：

| 字段 | env 变量 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `WECOM_ENABLED` | `false` | 是否装配长连接 runner（false = 零连接 / 零后台任务） |
| `bot_id` | `WECOM_BOT_ID` | 空 | 智能机器人 BotID（enabled=true 时必填——校验拒绝空值） |
| `secret_env` | `WECOM_SECRET_ENV` | `WECOM_SECRET` | 长连接专用密钥的环境变量名（**间接**——密钥本值永不落配置树） |
| `ws_endpoint` | `WECOM_WS_ENDPOINT` | `wss://openws.work.weixin.qq.com` | 长连接端点（可指向代理） |
| `request_timeout_seconds` | `WECOM_REQUEST_TIMEOUT_SECONDS` | `15` | 单条命令应答超时秒数 |
| `heartbeat_interval_seconds` | `WECOM_HEARTBEAT_INTERVAL_SECONDS` | `30` | 应用层心跳间隔秒数（官方建议 30） |
| `heartbeat_timeout_seconds` | `WECOM_HEARTBEAT_TIMEOUT_SECONDS` | `10` | 心跳应答超时秒数（必须小于间隔） |
| `open_timeout_seconds` | `WECOM_OPEN_TIMEOUT_SECONDS` | `10` | WebSocket 握手超时秒数 |
| `backoff_base_seconds` | `WECOM_BACKOFF_BASE_SECONDS` | `1.0` | 重连退避基数秒 |
| `backoff_max_seconds` | `WECOM_BACKOFF_MAX_SECONDS` | `60.0` | 退避封顶秒数（校验 ≥ 基数） |
| `backoff_jitter_ratio` | `WECOM_BACKOFF_JITTER_RATIO` | `0.3` | 退避抖动比例（0-1，0 = 无抖动） |
| `reply_window_seconds` | `WECOM_REPLY_WINDOW_SECONDS` | `86400` | 消息回调回复有效期（官方 24 小时——超期降级主动推送） |

```bash
# .env 形态（分节 env 前缀）
WECOM_ENABLED=true
WECOM_BOT_ID=bot-1a2b3c
WECOM_SECRET=your-long-connection-secret     # secret_env 指向的本值（永不落 yaml）
WECOM_HEARTBEAT_INTERVAL_SECONDS=30
WECOM_BACKOFF_JITTER_RATIO=0.3
WECOM_REPLY_WINDOW_SECONDS=86400
```

```yaml
# config.yaml 不承载通道配置（env 面专属——与 telegram / QQ 同约定）；
# 嵌套覆写形态（进程环境变量，优先级高于 .env）：
#   CHANNELS__WECOM__ENABLED=true
#   CHANNELS__WECOM__BOT_ID=bot-1a2b3c
```

隔离构造（测试 / 工具）：

```python
from packages.harness.config.models import ChannelsSettings, WecomChannelSettings
from packages.harness.config.settings import build_isolated_settings

settings = build_isolated_settings(
    channels=ChannelsSettings(
        wecom=WecomChannelSettings(enabled=True, bot_id="bot-isolated")
    )
)
```

> 注：`backend/.env.example` 键面以 `.env.example`（`pixi run gen-examples` 生成）
> 为真源，本表为语义说明（`WECOM_*` 与密钥本值变量 `WECOM_SECRET`）。

### 部署要点（企业微信侧）

1. **开启 API 模式（长连接）**：企业微信管理后台 →「智能机器人」→ 选中机器人 →
   API 模式 → 选择「长连接」；API 模式只能二选一，切到「设置接收消息回调地址」
   会让现有长连接失效（反之亦然）；
2. **取凭证**：同页「获取凭证」取 BotID 与 Secret——Secret 是长连接专用密钥，
   不要与回调模式的 Token / EncodingAESKey 混用；
3. **本工程配置**：`WECOM_ENABLED=true`、`WECOM_BOT_ID=<BotID>`、
   `WECOM_SECRET=<Secret>`（或改 `WECOM_SECRET_ENV` 指向别的变量名）；
4. **网络面**：只需出站可达 `wss://openws.work.weixin.qq.com`——无需公网 IP、
   无需反向代理、无需回调 URL 与加解密配置；
5. **单实例约束**：同一机器人只跑一个进程的长连接（多实例会互踢，被踢实例按
   KICKED 停止重连并打 WARNING——高可用用主备切换而不是多活）。

## Settings（`Settings.channels`）——telegram 分节

env 面（`env_prefix=TELEGRAM_`；嵌套覆写 `CHANNELS__TELEGRAM__*` 亦可用）：

| 字段 | env 变量 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | `TELEGRAM_ENABLED` | `false` | 是否装配通道 runner（false = 零通道对象构造） |
| `bot_token_env` | `TELEGRAM_BOT_TOKEN_ENV` | `TELEGRAM_BOT_TOKEN` | bot token 环境变量名（**间接**——token 本值永不落配置树） |
| `api_base` | `TELEGRAM_API_BASE` | `https://api.telegram.org` | Bot API 基础 URL（可指向自建代理） |
| `poll_timeout_seconds` | `TELEGRAM_POLL_TIMEOUT_SECONDS` | `25` | 长轮询超时秒数（1–50；请求超时 = 该值 + 5s 传输裕量） |
| `owner_chat_id` | `TELEGRAM_OWNER_CHAT_ID` | `null` | 属主会话 ID（空字符串归一 null；duty 值班报告投递目标——`app/main.py` 装配传入 `DutyService(owner_chat_id=...)`，见 duty.md「报告路由」） |
| `backoff_base_seconds` | `TELEGRAM_BACKOFF_BASE_SECONDS` | `1.0` | 轮询失败退避基数秒 |
| `backoff_max_seconds` | `TELEGRAM_BACKOFF_MAX_SECONDS` | `60.0` | 退避封顶秒数（校验 ≥ 基数） |
| `state_dir` | `TELEGRAM_STATE_DIR` | `data/channels` | offset 状态目录（相对路径按进程 CWD 解析） |

JSON 示例（pydantic-settings 复杂类型 env 形态 + .env 形态）：

```bash
# .env 形态（分节 env 前缀）
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:AAF-your-real-bot-token   # bot_token_env 指向的本值（永不落 yaml）
TELEGRAM_POLL_TIMEOUT_SECONDS=25
TELEGRAM_BACKOFF_BASE_SECONDS=1.0
TELEGRAM_BACKOFF_MAX_SECONDS=60.0
TELEGRAM_STATE_DIR=data/channels
```

```yaml
# config.yaml 不承载通道配置（env 面专属——与 server/database 同约定）；
# 嵌套覆写形态（进程环境变量，优先级高于 .env）：
#   CHANNELS__TELEGRAM__ENABLED=true
#   CHANNELS__TELEGRAM__POLL_TIMEOUT_SECONDS=10
```

隔离构造（测试 / 工具）：

```python
from packages.harness.config.models import ChannelsSettings, TelegramSettings
from packages.harness.config.settings import build_isolated_settings

settings = build_isolated_settings(
    channels=ChannelsSettings(
        telegram=TelegramSettings(enabled=True, state_dir="/tmp/channels-state")
    )
)
```

> 注：`backend/.env.example` 键面以 `.env.example`（`pixi run gen-examples` 生成）为真源，本表为语义说明（`TELEGRAM_*`）。

## 测试面

| 测试文件 | 覆盖 |
|---|---|
| `tests/unit/test_channels_models.py` | 值域（telegram + qq）/ 校验 / 默认值 / 身份键；v2：附件恰一源矩阵 / kind 值域 / 容量指引常量 / 入站出站投递 v2 字段缺省与可设 / 逐附件结果形态 |
| `tests/unit/test_channels_settings.py` | telegram env 面对齐 / 默认值 / 边界校验 / 隔离构造 / 嵌套覆写 |
| `tests/unit/test_qq_settings.py` | QQ env 面对齐 / 默认值 / 边界校验 / 隔离构造 / `CHANNELS__QQ__*` 嵌套覆写 |
| `tests/unit/test_tg_client.py` | MockTransport：请求形状（timeout / offset / 超时裕量）/ 4096 分段 / 错误映射表 / token fail fast；v2：silent → disable_notification / 媒体 JSON 引用与 multipart 上传形状 / 空媒体面校验 / 媒体错误映射 |
| `tests/unit/test_qq_client.py` | MockTransport：动作包裹形状 / Bearer 头有无 / 超时注入 / status=async 成功 / 错误映射表 / 空文本拒绝；v2：段数组动作包裹形状 / 空段列表拒绝 |
| `tests/unit/test_tg_runner.py` | fake client + fake router + 注入休眠：offset 往返 / 去重 / 退避 / 优雅停机 / 启动校验 / 发送面；v2：媒体消息 caption 兜底 + 伪方案附件提取 / 纯媒体跳过 / silent 透传 / 附件 kind 分流（base64 上传 / url JSON / file_id 重发 / sticker caption 丢弃）/ 部分失败聚合 / 文本失败上抛（v1 语义） |
| `tests/unit/test_qq_adapter.py` | fake 客户端：chat_id grammar 双形态 / 投递映射（ok / 无 message_id）/ grammar 校验 / 失败上抛；v2：段数组发射（文本先 + 媒体后）/ base64 包装 / sticker→image 映射 / file 类逐附件失败承载 / silent no-op / 段发送失败上抛 |
| `tests/unit/test_qq_webhook.py` | fake 路由器：私聊 / 群聊路由形状 / 展平双形态 + 占位符表 / 去重 + FIFO 淘汰 / 忽略矩阵 / 隔离铁律；v2：image / record 段附件提取（url / base64:// 剥前缀 / file:// path 源）/ 无引用跳过 / CQ 字符串形态提取不生效（边界） |
| `tests/unit/test_channel_router.py` | fake ChatService：会话复用 / 持久化调用形状 / 人格绑定 / 错误隔离矩阵 / per-chat 并发串行化；v2：入站附件 + synthetic 透传（回复循环纯文本——零路由器改动证明） |
| `tests/unit/test_channels_qq_api.py` | ASGITransport：恒 200 确认形状 / 鉴权矩阵（401 / 关闭）/ 体校验 400 / 路由器未装配确认丢弃 |
| `tests/unit/test_app_channels_lifespan.py` | telegram enabled / disabled 装配 / 停机 LIFO 顺序 / token 缺失 fail fast / 任务终态收割 / cancel 兜底 |
| `tests/unit/test_app_qq_channels_lifespan.py` | QQ enabled / disabled 装配 / warn-and-continue / 双通道共用路由器 / 共享池注入 / 装配顺序契约 |
| `tests/unit/test_wecom_models.py` | 会话键 grammar / 入站映射矩阵（文本 / 语音 / 混排 / 图片 / 文件 / 视频 / 合成来源）/ 不可路由形态 / 附件提取与占位符 / 出站命令 wire 形状（含素材上传三步）/ markdown 字节分段（换行优先 + UTF-8 硬切） |
| `tests/unit/test_wecom_client.py` | fake 连接 + 记录式伪时钟：构造校验 / 订阅（形状 + 被拒 fail fast + 单连接）/ 心跳（间隔 + 超时关闭）/ 退避（指数 + 抖动区间 + 复位）/ 被踢下线 / 命令面（超时 / 拒绝 / 未就绪 / 断连拒结）/ 素材上传（单片 / 多片 / 限制 / 缺字段）/ 帧容错 / 处理器隔离 / 优雅停机 |
| `tests/unit/test_wecom_runner.py` | fake 客户端 + fake 路由器：入站路由形状 / 去重与窗口淘汰 / 事件帧不路由 / 队列过载丢弃 / 路由器异常隔离 / 回复上下文（登记 / 过期 / 缺失）/ 文本分段投递 / 附件三源上传与推送 / 逐附件失败承载 / 停机传导 |
| `tests/unit/test_wecom_settings.py` | WECOM_ env 面对齐 / 默认值 / 不变式（BotID 必填 / 退避边界 / 心跳预算 / 抖动值域）/ 隔离构造 / 嵌套覆写 |
| `tests/unit/test_app_wecom_channels_lifespan.py` | 企微 enabled / disabled 装配 / 凭据失败仅跳过该通道 / 双通道共用路由器 / 共享 HTTP 池注入 / 停机传导 |
| `tests/wecom_fakes.py` | 共享 fake 基建：wire 级连接替身（自动应答 / 错误注入 / 断连注入）/ 连接工厂（失败窗口）/ 逐次放行伪休眠 / fake 客户端 / fake 路由器 |

零真实网络（MockTransport / fake）；零真实配置（隔离 Settings + conftest 硬覆盖；测试 token 为专用假值）；全部 `async def test_*`。
