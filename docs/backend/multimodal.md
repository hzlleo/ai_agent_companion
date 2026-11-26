# multimodal — 多模态域（look_at 图像理解 + 表情包库 / VLM 标注）

`backend/src/packages/harness/multimodal/` 承载**图像理解能力面**（look_at）：llm 域的**多模态内容分块**（OpenAI 兼容 vision 消息形态——`ChatMessageModel.content` 升级为 `str | list[ContentPartModel]`，纯文本全量向后兼容）、**LookAtService 编排门面**（请求校验 → data URL 构建 → 条目链解析 → 网关调用）+ **look_at 工具注册面**（duty 工具循环消费——图像经 base64 携带；app 装配在 `_startup_scheduler` duty 分支经装配网关注册）+ app 层 **look_at 管理 API**（multipart 上传）。

锁定设计决策：**视觉模型是 llm 注册表的一个条目**（`multimodal: true`，绑定 `look_at` 角色）——生产形态为本地 llama-server 服务 `models/Qwen3-VL-4B-Instruct-GGUF` 子模块权重（部署机关注；compose 接线由部署侧另行承接），后端只经 OpenAI 兼容 HTTP 契约消费，永不读取模型文件路径（与 llm / voice 域「只连端点」同约定）。look_at 角色绑定的条目**非多模态**时走 config 域的**多模态回退链**（`llm.multimodal_fallback` 段——本域消费既有裁决，不重新实现）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `mm_models.py` | 多模态域 Model Classes（Unit 文件）：look_at 请求 / 结果 / 工具参数契约、data URL 构建与解析助手、网关与服务注入面协议 | `LookAtRequestModel` / `LookAtResultModel` / `LookAtToolArgsModel` / `LookAtGatewayFace` / `LookAtFace` / `build_data_url` / `parse_data_url` / `LOOK_AT_IMAGE_MIME_ALLOWLIST` / `LOOK_AT_IMAGE_MAX_BYTES` |
| `mm_prompts.py` | prompt Unit 文件（视觉理解任务书 + 聚焦区块 + 缺省指令；版本常量 + sha256 byte-lock） | `LOOK_AT_CHARTER_TEMPLATE` / `render_look_at_charter` / `render_user_instruction` |
| `look_at.py` | LookAtService 编排门面（校验 / 消息组装 / 条目链 / 网关调用）+ look_at 工具注册面 | `LookAtService` / `register_look_at_tool` / `LOOK_AT_TOOL_NAME` |
| `sticker_models.py` | 表情包库 Model Classes（Unit 文件）：`STICKER_` 配置面、上传 / 查询 / 修改 / 删除 / 标注 / 批量结果契约、通道桥收录载荷、视觉标注网关注入面 | `StickerSettings` / `StickerBatchUploadDTO` / `StickerRecordDTO` / `InboundStickerPayloadModel` / `StickerAnnotationResultModel` |
| `sticker_orm.py` | 表情包 ORM 行（PersonaMixin 人格作用域 + 哈希去重） | `StickerRow` |
| `sticker_prompts.py` | 表情包 VLM 标注提示词（prompt Unit 文件——版本常量 + byte-lock） | `STICKER_ANNOTATION_CHARTER_TEMPLATE` / `render_sticker_annotation_charter` |
| `sticker_service.py` | 表情包服务（上传去重 / 标签 / 删除 / VLM 标注 / 通道收发桥） | `StickerService` |
| `sticker_bridge.py` | 通道表情桥（附件解码 / 入站收录 / 出站素材包装——app 装配薄服务，模型与逻辑从 `app/main.py` 下沉） | `StickerBridge` / `decode_inbound_stickers` / `StickerBridgeServiceFace` |

依赖方向：multimodal → llm（`ChatMessageModel` 内容分块 / `LlmGateway` 条目直连）+ config（`ModelResolver` 角色解析 / `resolve_multimodal_chain` 回退链裁决 / `LlmModelEntry`）+ tools（`ToolRegistry` 注册面）+ core（errors / time_utils）；llm / config / tools 不反向依赖 multimodal（单向，无环）。**multimodal 不硬依赖 usage 域**——look_at 用量行完全由网关 `chat_for_entry(purpose="look_at")` 自动发射（成功 / 失败均落账），服务层零双记。

## llm 内容分块（wire 格式——OpenAI 兼容 vision 消息形态）

`ChatMessageModel.content` 升级为 `str | list[ContentPartModel]`（判别联合，按 `type` 分派）：

| 分块类型 | 域内形态 | wire 形态 |
|---|---|---|
| 文本 | `TextContentPart(text=...)` | `{"type": "text", "text": "..."}` |
| 图片 | `ImageContentPart(image_url=ImageContentUrlModel(url=..., detail=None))` | `{"type": "image_url", "image_url": {"url": "...", "detail": "high"}}`（`detail` 仅显式设置时进入 wire） |

`url` 承载两类形态：`https://` 远程图片 URL，或 `data:<mime>;base64,<payload>` 内联 data URL（**look_at 链路主形态**——图像字节不落盘、不外发，经 `build_data_url` 直传端点）。纯字符串 content 在全部既有调用面（chat / duty / dream / 流式）原样有效，wire 保持 `{"role", "content"}` 二键不变；仅分块列表序列化为 parts 数组。

**流式边界（文档化决策）**：流式传输面恒 content-only 纯文本——`build_chat_request_body` 对 `stream=True` 且携带内容分块的组合直接拒绝（`VALIDATION_ERROR`，与 tools+stream 守卫同形态）；**tools 与分块可组合**（视觉 + 工具调用形态——duty 轮工具循环消费）。

请求体示例（look_at 链路的实际 wire 形态）：

```json
{
  "model": "qwen3-vl-4b",
  "messages": [
    {"role": "system", "content": "【视觉理解任务书】\n...（五段结构化框架 + 聚焦区块 + 红线）"},
    {"role": "user", "content": [
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}},
      {"type": "text", "text": "图里有几只猫"}
    ]}
  ]
}
```

## 架构：look_at 调用链路顺序

```
调用方（look_at API / duty 工具循环）
    ↓ LookAtService.describe(image, mime_type, question)   ① 请求校验
      （LookAtRequestModel——MIME 允许清单 png/jpeg/webp/gif + 10MB 上限
       + 聚焦问题 ≤200 字符；ValidationError → VALIDATION_ERROR）
    ↓ ② build_data_url（图像字节内联——不落盘 / 不外发 / 不落日志）
    ↓ ③ 消息组装（system 视觉理解任务书 + user 内容分块
       [image, text(question 或缺省指令)]——mm_prompts Unit 文件）
    ↓ ④ 条目链解析（resolver.resolve("look_at") → 角色 条目；
       resolve_multimodal_chain——见下节回退链顺序）
    ↓ ⑤ 逐条目尝试 gateway.chat_for_entry(entry, messages,
       purpose="look_at")（失败 / 空 content → 下一级；
       全部失败 → LLM_CALL_FAILED；单条目链透传原始错误码）
    ↓ ⑥ 用量发射（网关自动——purpose="look_at"，成功 / 失败均落账）
LookAtResultModel（description + model_entry + latency_ms）
```

### 回退链顺序（config 域 `llm.multimodal_fallback` 段——消费既有裁决）

```
resolver.resolve("look_at")            角色未绑定 → MODEL_NOT_FOUND（404）
    ↓ role 条目 multimodal=true？
      ├─ 是 → [role 条目]（零回退，直连）
      └─ 否 → 回退链 = [local, remote] 中已配置且非 None 的级
               ├─ 链非空 → 逐级尝试（local 优先：低延迟 / 零外发成本；
               │           remote 兜底：本地不可用或能力不足时承接）
               └─ 链为空 → CONFIG_INVALID（422，fail loud——中文可操作
                           指引：配置 llm.multimodal_fallback 段或将
                           look_at 角色绑定到 multimodal: true 条目）
```

config.yaml 示例：

```yaml
llm:
  models:
    - name: "local-vision"
      base_url: "http://127.0.0.1:8001/v1"    # llama-server（Qwen3-VL GGUF）
      model: "qwen3-vl-4b"
      roles: ["look_at"]
      multimodal: true
  multimodal_fallback:                        # look_at 条目非多模态时的回退链
    local:
      name: "local-vision"
      base_url: "http://127.0.0.1:8001/v1"
      model: "qwen3-vl-4b"
      multimodal: true
    remote:
      name: "remote-vision"
      base_url: "https://api.example.com/v1"
      api_key_env: "REMOTE_VISION_API_KEY"
      model: "glm-4.6v"
      multimodal: true
```

## prompt 契约（mm_prompts.py——byte-lock）

视觉理解任务书（system 首块）要求模型输出**结构化中文描述**，依次五段：主体 / 场景 / 文字内容（OCR 逐字转录）/ 图表数据（类型 + 表头 + 关键数值）/ 值得注意的细节；聚焦区块按问题有无填充（问题给定时紧扣提问作答，缺省时整体总结收束）；**红线：只描述图中真实可见的内容，不猜测图中不存在的文字或数据，辨识不清须注明「不确定」**。全部 prompt 文本只在 `mm_prompts.py` 定义（版本常量 `LOOK_AT_PROMPTS_VERSION` / `LOOK_AT_CHARTER_VERSION` + 逐模板 sha256 byte-lock 测试 `test_mm_prompts.py`——变更是知情的、可审计的）。

## 工具面（LLM 工具循环消费）

`register_look_at_tool(registry, service)` 注册 `look_at` 工具（注册即出现在 `registry.list_specs()`——duty 工具循环自动获得；app 装配在 `_startup_scheduler` duty 分支经装配网关注册；未注册 = 工具面不出现）：

| 参数 | 类型 | 语义 |
|---|---|---|
| `image_base64` | str（必填） | 图片 base64 载荷（LLM 无法传二进制——执行器解码后委托服务） |
| `mime_type` | str（默认 `image/png`） | 图片 MIME（png / jpeg / webp / gif；执行器不做字节嗅探） |
| `question` | str（可选，≤200） | 聚焦问题（缺省整体结构化描述） |

失败面（registry 异常隔离契约——**恒不抛异常**）：base64 载荷非法 / 参数校验失败 / 服务侧业务异常（回退链空 `CONFIG_INVALID`、链路失败 `LLM_CALL_FAILED` 等）均归一 `ok=False` 工具错误（错误文案反馈给模型自行调整）。

## 管理 API（app/api/look_at.py）

### POST /api/v1/look_at —— 图片上传 → 结构化中文描述

```
POST /api/v1/look_at
Content-Type: multipart/form-data; boundary=...

------...
Content-Disposition: form-data; name="image"; filename="cat.png"
Content-Type: image/png

<PNG 图片字节>
------...
Content-Disposition: form-data; name="question"

图里有几只猫
------...--

200 ← {"success": true, "code": "200", "message": "图像理解完成",
       "data": {"description": "1. 主体：...\n2. 场景：...\n...",
                "model_entry": "local-vision", "latency_ms": 830}}
```

契约要点：

- **multipart 零新增依赖**（与 voice ASR 同锁定决策）：stdlib `email` 解析器解析 multipart/form-data（RFC 2046 边界 / RFC 2231 文件名由 stdlib 承担）；形态不符 / 缺 `image` 字段 → `BAD_REQUEST`（400）；
- **MIME 解析序**：`image` 部件声明 Content-Type 在允许清单内 → 直接采用；否则按文件扩展名映射（`.png` / `.jpg` / `.jpeg` / `.webp` / `.gif`）；仍未命中 → 请求校验拒绝（`VALIDATION_ERROR` → 422）——兼容 curl 无 Content-Type 上传与浏览器显式声明两种形态；
- **校验面**：MIME 清单 / 10MB 上限 / 聚焦问题 ≤200 字符 → `VALIDATION_ERROR`（422）；链路失败 → `LLM_CALL_FAILED`（503）等；
- **人格绑定**：缺省当前激活人格（用量 `persona_id` 归属）；无激活人格 → 未绑定形态（不因无人格而拒绝）；
- **用量**：网关 `purpose="look_at"` 自动落账（`ff_usage_records`——token 按端点回传，时延真实）；图像字节永不落日志。

## 表情包库与 VLM 标注（`STICKER_` env 面——默认关闭）

`harness/multimodal/sticker_*` 承载按人格隔离的表情包素材库：上传时强制填写**来源与授权备注**（素材法律边界提示义务）、按文件哈希去重、可用 VLM 生成描述与标签、并可通过通道附件 v2 收发（入站收录 / 出站随文本附发）。库默认关闭（`STICKER_ENABLED=false`），关闭时管理 API 拒绝、通道桥不装配——既有通道语义零变化。

### 素材法律边界（锁定设计）

- 上传必填 `source_note`（1-512 字符）：记录素材来源与授权状态（自绘 / 朋友授权使用 / 公开表情站 CC0 等）；空白串按拒收处理；
- 库**不做版权推断**，只保证来源备注随记录可读；管理面板在上传区与每张卡片显著展示该字段；
- VLM 标注只补描述与标签，**不修改来源备注**；删除记录时同批删除文件。

### 数据模型（迁移 `000000000016`）

`ff_stickers`（每人格一份素材库）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `persona_id` | varchar(64) | 人格作用域（PersonaMixin 自动注入 / 过滤） |
| `sha256` | varchar(64) | 文件哈希（同人格去重键） |
| `content_type` | varchar(64) | 图片 MIME（png / jpeg / webp / gif） |
| `storage_path` | varchar(512) | 文件落盘路径（删除时同批清理） |
| `size_bytes` | int | 文件字节数 |
| `tags` | text | 标签 JSON 字符串数组 |
| `description` | text | 描述（0-500 字符；VLM 标注可补充） |
| `source_note` | varchar(512) | 来源与授权备注（上传必填） |
| `annotation_status` | varchar(16) | `pending` / `done` / `failed` |
| `annotation_error` / `annotation_model_entry` / `annotated_at_ms` | text / varchar / bigint | 失败原因 / 命中视觉条目 / 标注完成时间 |

唯一键 `uq_ff_stickers_persona_sha (persona_id, sha256)`；索引 `ix_ff_stickers_annotation_status`。文件写入采用「临时文件 + fsync + `os.replace`」原子换名，目录按人格哈希与哈希前缀分段（不同人格同文件互不覆盖）。

**并发冲突语义（锁定，根修）**：同人格同哈希并发写入按唯一键收敛——插入撞 1062（重复键）时以共享锁定读（`SELECT ... LOCK IN SHARE MODE`）解析已提交胜者行（不请求排他锁，消除唯一键检查共享锁 → 胜者查询排他锁的 S→X 升级死锁）；胜者路径与本次写入路径相同时保留文件，否则清理落败文件。SeekDB 可重复读旧快照下的新增键冲突实测以瞬态事务冲突（驱动错误 6235；死锁 1213）抛出——调用方（`upload_batch` / `ingest_bytes`）回滚后按已提交胜者单次重试：预读命中即收敛为既有记录 / 重复拒收（零 500），二次冲突上抛。SeekDB 的旧快照不因锁定读而跨越新增键，重试是收敛前提而非缓解。另：SeekDB 实测共享锁与共享锁互斥（第二把共享读以 1205 退出），故 1205 不在重试集合（仅 6235 / 1213）；当前两入口均先预读，落败路径不触达第二把共享读，故 1205 不可达——未来新增不预读的调用方需补 1205 处理。

### 链路顺序（上传 / 标注）

```
POST /api/v1/stickers（批量）
    ↓ ① 人格解析 + bind_persona_context（隔离轴）
    ↓ ② 功能门控（STICKER_ENABLED=false → 409）
    ↓ ③ 逐条目校验：MIME 白名单 / base64 / 字节上限 / 来源备注非空 /
    │            标签数量与长度 / 描述长度
    ↓ ④ 哈希去重（同人格已存在 → duplicate 拒收）
    ↓ ⑤ 原子落盘 + 行插入（status=pending）→ 逐条目部分成功结果

POST /api/v1/stickers/{id}/annotate
    ↓ ① 记录读取 + 文件读取（缺失 → failed 状态）
    ↓ ② 视觉条目解析（look_at 角色 + 多模态回退链——复用 look_at 裁决）
    ↓ ③ 逐条目 gateway.chat_for_entry(purpose="look_at")（公共池 / 用量自动落账）
    ↓ ④ 严格 JSON 解析（description / tags；容忍代码围栏与前后解释文本）
    ↓ ⑤ 成功：写描述 / 合并标签 / status=done / 模型条目 / 完成时间
        失败：status=failed + annotation_error（可重试）
```

### API（app/api/stickers.py）

```
POST /api/v1/stickers
{"items": [
  {"image_base64": "iVBORw0KGgo...", "content_type": "image/png",
   "source_note": "自绘素材", "tags": ["开心", "猫猫"], "description": ""}
]}
200 ← {"success": true, "code": "200", "message": "表情包上传完成",
       "data": {"created": [{"id": 12, "sha256": "3f9a...",
                             "content_type": "image/png", "size_bytes": 8421,
                             "tags": ["开心", "猫猫"], "description": "",
                             "source_note": "自绘素材",
                             "annotation_status": "pending",
                             "annotation_error": null,
                             "annotation_model_entry": null,
                             "annotated_at_ms": null,
                             "created_at": 1760000000000,
                             "updated_at": 1760000000000,
                             "data_base64": null}],
                "rejected": [{"index": 1, "reason": "duplicate",
                              "message": "同一人格下已存在相同文件（按哈希去重）"}]}}

GET /api/v1/stickers?tag=开心&annotation_status=pending&limit=100&include_data=true
200 ← {"success": true, "code": "200", "message": "表情包列表已获取",
       "data": [{"id": 12, "...": "...", "data_base64": "iVBORw0KGgo..."}]}

PATCH /api/v1/stickers/12
{"tags": ["开心"], "description": "笑着挥手", "source_note": "朋友授权使用"}
200 ← {"success": true, "code": "200", "message": "表情包已更新", "data": {"id": 12, "...": "..."}}

POST /api/v1/stickers/12/annotate
200 ← {"success": true, "code": "200", "message": "标注完成（模型条目 local-vision）",
       "data": {"ok": true,
                "record": {"id": 12, "annotation_status": "done",
                           "description": "笑着挥手", "tags": ["开心", "挥手"],
                           "annotation_model_entry": "local-vision",
                           "annotated_at_ms": 1760000100000, "...": "..."},
                "message": "标注完成（模型条目 local-vision）"}}
# 失败同样 HTTP 200（可读状态，管理面可重试）：
200 ← {"success": true, "code": "200", "message": "视觉端点不可用",
       "data": {"ok": false,
                "record": {"id": 12, "annotation_status": "failed",
                           "annotation_error": "local-vision: ...", "...": "..."},
                "message": "local-vision: ..."}}

DELETE /api/v1/stickers/12
200 ← {"success": true, "code": "200", "message": "表情包已删除",
       "data": {"deleted_ids": [12], "missing_ids": []}}

POST /api/v1/stickers/batch-delete
{"ids": [12, 13, 999]}
200 ← {"success": true, "code": "200", "message": "表情包删除完成",
       "data": {"deleted_ids": [12, 13], "missing_ids": [999]}}

GET /api/v1/stickers/12/file
200 ← Content-Type: image/png
      Content-Disposition: inline; filename="sticker-12.png"
      <图片字节>
```

### prompt 契约（sticker_prompts.py——byte-lock）

标注任务书要求严格 JSON：`{"description": "一句中文描述", "tags": ["标签", "..."]}`；描述 ≤60 汉字（含图中文字可点明），标签 2-6 个中文短词（情绪 / 场景 / 动作 / 角色类型），红线为「只描述画面真实可见内容、不猜测画面外剧情、辨识不清写不确定」。全部文本只在 `sticker_prompts.py` 定义（`STICKER_PROMPTS_VERSION` + 逐模板 sha256 byte-lock：`test_sticker_prompts.py`）。

### 通道附件 v2 收发桥（默认关闭）

`ChannelRouter` 新增两个可选钩子（`sticker_ingest_hook` / `sticker_reply_hook`，缺省 None = 零影响）：

- 解码 / 筛选 / 出站包装逻辑在 `harness/multimodal/sticker_bridge.py`（`StickerBridge`，失败隔离与「只收录内联 base64」边界同源）；`app/main.py` 只注入 `StickerService` 与 `session_scope` 并接线钩子；

- **入站收录**：入站消息含 `kind=sticker` 附件时，以应答人格上下文调用收录桥（桥开短事务 → `StickerService.ingest_bytes`）；仅收录**已持有字节**的 base64 载荷（QQ 段内联形态）；`tg-file://` / `url` 引用需二次下载，桥跳过并文档化；收录失败仅告警，不阻断回复；
- **出站附发**：本轮入站带 sticker 附件且 `STICKER_CHANNEL_REPLY_ENABLED=true` 时，取当前人格最新可读素材作为 `OutboundMessageModel.attachments` 的一张 `sticker` 附件（base64 形态）随文本投递；取表情失败仅告警，文本照常投递；
- 延迟 / 合并批次不携带附件（批次只承接文本）——入站收录已在门控判定前完成，回表情仅即时回复路径生效；
- 通道适配器按既有 v2 映射表投递（Telegram `sendSticker`；QQ `image` 段），不新建第二套发送面。

### 错误码映射（sticker 段——复用既有码表）

| 失败类别 | 错误码 | HTTP |
|---|---|---|
| 功能关闭 | `CONFLICT` | 409 |
| 批量条数超过配置上限 / 标签或描述非法（修改面） | `VALIDATION_ERROR` | 422 |
| 记录或文件不存在 | `NOT_FOUND` | 404 |
| 上传逐条目校验失败（类型 / 字节 / 来源备注 / 标签 / 描述 / 重复） | 逐条目 `rejected`（HTTP 200 部分成功） | — |
| 标注依赖失败 / JSON 非法 / 条目未配置 | 逐记录 `failed` 状态（HTTP 200，可重试） | — |

### Settings（`STICKER_` env 面——默认关闭）

| 键 | 默认 | 说明 |
|---|---|---|
| `STICKER_ENABLED` | `false` | 表情包库总闸（关闭时 API 409、通道桥不装配） |
| `STICKER_STORAGE_DIR` | `data/stickers` | 文件落盘根目录 |
| `STICKER_MAX_UPLOAD_BYTES` | `5242880` | 单文件字节上限（5MB 量级） |
| `STICKER_MAX_TAGS` | `8` | 单条标签数上限 |
| `STICKER_TAG_MAX_LENGTH` | `24` | 单个标签最长字符数 |
| `STICKER_DESCRIPTION_MAX_LENGTH` | `500` | 描述最长字符数 |
| `STICKER_SOURCE_NOTE_MAX_LENGTH` | `512` | 来源 / 授权备注最长字符数 |
| `STICKER_MAX_BATCH_ITEMS` | `20` | 单次批量上传条数上限 |
| `STICKER_CHANNEL_REPLY_ENABLED` | `true` | 通道桥回表情开关（仅 `STICKER_ENABLED=true` 时生效） |

### 前端面板

`frontend/src/pages/stickers/StickersPage.tsx`（管理台 `/stickers`）：人格选择 / 标注状态过滤 / 多文件上传（来源备注必填、初始标签）/ 缩略图与来源备注展示 / 标签编辑 / 自动标注与失败重试 / 删除确认；类型、API、hooks、msw 与测试同批交付。

## 部署注记（生产形态）

- 视觉端点：本地 llama-server 服务 `models/Qwen3-VL-4B-Instruct-GGUF` 子模块权重（OpenAI 兼容 `/v1/chat/completions`，支持 image_url data URL 分块）；注册为 llm 条目（`multimodal: true`，绑定 `look_at` 角色）；
- 远程兜底：公网 OpenAI 兼容多模态端点条目（`multimodal_fallback.remote`，密钥经 `api_key_env` 环境变量——密钥永不落 yaml）；
- 图像体量边界：单次 10MB 内存字节（超过 422——调用方先行压缩 / 裁剪；TTS 大输出边界同形态的镜像决策）。
