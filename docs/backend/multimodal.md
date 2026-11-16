# multimodal — 多模态 look_at 域（P4 chunk 2 交付：LLM 内容分块 + 图像理解）

`backend/src/packages/harness/multimodal/` 承载**图像理解能力面**（look_at）：llm 域的**多模态内容分块**（OpenAI 兼容 vision 消息形态——`ChatMessageModel.content` 升级为 `str | list[ContentPartModel]`，纯文本全量向后兼容）、**LookAtService 编排门面**（请求校验 → data URL 构建 → 条目链解析 → 网关调用）+ **look_at 工具注册面**（duty / persona 工具循环消费——图像经 base64 携带）+ app 层 **look_at 管理 API**（multipart 上传）。

锁定设计决策：**视觉模型是 llm 注册表的一个条目**（`multimodal: true`，绑定 `look_at` 角色）——生产形态为本地 llama-server 服务 `models/Qwen3-VL-4B-Instruct-GGUF` 子模块权重（部署机关注；compose 接线属后续部署 chunk），后端只经 OpenAI 兼容 HTTP 契约消费，永不读取模型文件路径（与 llm / voice 域「只连端点」同约定）。look_at 角色绑定的条目**非多模态**时走 config 域的**多模态回退链**（`llm.multimodal_fallback` 段——本 chunk 消费既有裁决，不重新实现）。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `mm_models.py` | 多模态域 Model Classes（Unit 文件）：look_at 请求 / 结果 / 工具参数契约、data URL 构建与解析助手、网关与服务注入面协议 | `LookAtRequestModel` / `LookAtResultModel` / `LookAtToolArgsModel` / `LookAtGatewayFace` / `LookAtFace` / `build_data_url` / `parse_data_url` / `LOOK_AT_IMAGE_MIME_ALLOWLIST` / `LOOK_AT_IMAGE_MAX_BYTES` |
| `mm_prompts.py` | prompt Unit 文件（视觉理解任务书 + 聚焦区块 + 缺省指令；版本常量 + sha256 byte-lock） | `LOOK_AT_CHARTER_TEMPLATE` / `render_look_at_charter` / `render_user_instruction` |
| `look_at.py` | LookAtService 编排门面（校验 / 消息组装 / 条目链 / 网关调用）+ look_at 工具注册面 | `LookAtService` / `register_look_at_tool` / `LOOK_AT_TOOL_NAME` |

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
调用方（look_at API / duty / persona 工具循环）
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

`register_look_at_tool(registry, service)` 注册 `look_at` 工具（注册即出现在 `registry.list_specs()`——duty / persona 轮自动获得；未注册 = 工具面不出现）：

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

## 部署注记（生产形态）

- 视觉端点：本地 llama-server 服务 `models/Qwen3-VL-4B-Instruct-GGUF` 子模块权重（OpenAI 兼容 `/v1/chat/completions`，支持 image_url data URL 分块）；注册为 llm 条目（`multimodal: true`，绑定 `look_at` 角色）；
- 远程兜底：公网 OpenAI 兼容多模态端点条目（`multimodal_fallback.remote`，密钥经 `api_key_env` 环境变量——密钥永不落 yaml）；
- 图像体量边界：单次 10MB 内存字节（超过 422——调用方先行压缩 / 裁剪；TTS 大输出边界同形态的镜像决策）。
