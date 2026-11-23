# voice — 语音域（TTS 合成 + ASR 转写 + 人格语音绑定 + 声纹档案）

`backend/src/packages/harness/voice/` 承载语音能力面：**TTS 客户端**（CosyVoice3 包装服务——文本 → WAV 音频）、**ASR 客户端**（Qwen3-ASR OpenAI 兼容 audio 端点——音频 → 文本）、**VoiceService 编排门面**（enabled 门控 / 人格绑定参数解析 / 用量埋点）+ persona 域的**人格语音绑定**（`ff_personas.voice_profile`）+ app 层 **voice 管理 API** + CLI **`--voice` 旗标**。

锁定设计决策：**语音服务是部署机上的外部本地进程**——CosyVoice3 与 Qwen3-ASR 权重经 `models/` 子模块承载（部署机关注；compose 接线由部署侧另行承接），后端**只经 HTTP 契约消费，永不读取模型文件路径**（与 llm 域「只连端点」同约定）。缺省双关闭（`VOICE_TTS_ENABLED=false` / `VOICE_ASR_ENABLED=false`）——零配置开发可用、测试 hermetic。

全双工实时会话（WS 协议 / VAD 切段 / 逐句播报 / 打断）建立在本域服务之上，独立建档见 [realtime-voice.md](realtime-voice.md)。

**双轨取材（R4-3）**：除上列批量客户端外，本域另有两个**流式通道客户端**（真流式 ASR / TTS，默认关闭、开关开启且握手成功才启用，失败自动回退批量主链）：

| 通道 | 客户端 | 传输 | 默认 |
|---|---|---|---|
| 批量 ASR | `asr_client.py`（OpenAI 兼容 multipart） | 共享 httpx 池 | 关（`VOICE_ASR_ENABLED`） |
| 流式 ASR | `asr_stream_client.py`（实时转写事件族 WS） | 注入传输（生产 = `stream_transport.py` 的 websockets 适配） | 关（`VOICE_ASR_STREAMING_ENABLED`） |
| 批量 TTS | `tts_client.py`（POST /tts → WAV） | 共享 httpx 池 | 关（`VOICE_TTS_ENABLED`） |
| 流式 TTS | `tts_stream_client.py`（逐分片 PCM16 WS） | 注入传输（同上） | 关（`VOICE_TTS_STREAMING_ENABLED`） |

流式与批量**同源分面**：段 / 句的计次口径、预算闸与用量行语义与 R4-2 完全一致（见下「流式连接计次口径」）；流式通道的任何失败都不消费批量预算、也不阻断批量主链。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `voice_models.py` | 语音域 Model Classes（Unit 文件）：TTS / ASR 请求与结果契约、人格语音绑定、用量事件、客户端注入面协议 | `TtsRequestModel` / `TtsResultModel` / `AsrResultModel` / `VoiceProfileModel` / `VoiceTtsRequestDTO` / `VoiceUsageEvent` / `TtsClientFace` / `AsrClientFace` |
| `tts_client.py` | CosyVoice3 合成客户端（共享 httpx client 注入；部署契约持有方） | `CosyVoiceTtsClient` |
| `asr_client.py` | Qwen3-ASR 转写客户端（OpenAI 兼容 multipart；共享 httpx client 注入） | `Qwen3AsrClient` |
| `stream_models.py` | 流式通道传输面契约（帧模型 / 协议 / 关闭码语义 / 连接工厂类型——公开面零裸 dict） | `VoiceStreamFrameModel` / `VoiceStreamTransportFace` / `VoiceStreamConnector` |
| `stream_transport.py` | 流式通道真实 WebSocket 传输（websockets 运行时依赖，仅装配层 / 探测脚本消费） | `WebSocketStreamTransport` / `make_websocket_connector` |
| `asr_stream_client.py` | Qwen3-ASR 实时识别客户端（握手 / 喂帧 / partial+final / finish / cancel / 保活与超时 / 错误映射） | `Qwen3AsrStreamClient` / `AsrStreamHandle` / `AsrStreamClientFace` |
| `tts_stream_client.py` | CosyVoice 流式合成客户端（文本 → PCM16 分片流；barge-in 取消关闭连接） | `CosyVoiceStreamClient` / `TtsStreamClientFace` |
| `voice_budget.py` | 分面日预算闸 / 计数 / 用量接线（ASR / TTS / voiceprint 共用；purpose 过滤 + 成功计量） | `enforce_daily_budget` / `wire_default_voice_usage_recorder` / `wire_default_voice_usage_counter` |
| `voice_service.py` | 编排门面（enabled 门控 + 人格绑定解析 + 分面预算闸 + 惰性客户端构建 + 成功用量发射 + 双轨流式通道（`open_asr_stream` / `synthesize_stream`）） | `VoiceService` / `VoiceUsageRecorder` / `VoiceBudgetCounter` |
| `voiceprint_models.py` | 声纹域 Model Classes（Unit 文件）：`VOICEPRINT_` 配置面、注册 / 校验 / 同意 / 档案契约、PIPL 告知文本与版本常量 | `VoiceprintSettings` / `VoiceprintEnrollRequestDTO` / `VoiceprintVerifyResultModel` / `VoiceprintPrivacyNoticeDTO` / `render_privacy_notice` |
| `voiceprint_orm.py` | 声纹样本 / 同意记录 ORM 行（PersonaMixin 人格作用域） | `VoiceprintSampleRow` / `VoiceprintConsentRow` |
| `voiceprint_embedding.py` | 说话人向量 HTTP 客户端（可配置端点；共享池 client 注入） | `VoiceprintEmbeddingClient` |
| `voiceprint_service.py` | 声纹服务（同意闸 / 批量注册 / 余弦校验 / 档案删除 / 撤回 / 保存期清理 / 文件落盘） | `VoiceprintService` |

依赖方向：voice → persona（`PersonaRecordDTO` 绑定面）/ config（Settings.voice）/ core（errors / context / time_utils / thread_pool）；persona → voice 仅经 `voice_models.VoiceProfileModel`（绑定列序列化——单向，无环）。HTTP 出站一律经 pools 共享 `httpx.AsyncClient`（硬规则：无私建连接——与 llm / channels / tools 沙盒同约定）；multipart 出站经 httpx 原生 `files=` / `data=`（零手工拼装、零新增依赖）。

## 架构：客户端 / 服务 / 用量链路顺序

```
调用方（voice API / CLI --voice / 后续通道语音面）
    ↓ VoiceService.synthesize(text, persona)          ① enabled 门控
                                                      ② 参数解析（见下节优先级）
    ② persona.voice_profile（存在时覆盖 settings 缺省）
    ③ TTS_DAILY_BUDGET_* 分面日预算闸（persona → global；
       0/0 = 不限且零计数查询；超限 RATE_LIMITED 零出站）
    ④ VoiceService._resolve_tts_client（注入优先；
       缺省经公共池共享 client 瞬时构建——零连接资源）
CosyVoiceTtsClient（tts_client.py）
    ↓ POST {base_url}/tts  JSON {text, speaker, speed[, instruct_text]}
    ↓ ← 200 audio/wav 字节（Content-Type 校验）
TtsResultModel（audio: bytes + content_type + latency_ms——内存字节形态）
    ↓ ⑤ 仅成功路径用量发射（失败 / 被拒不落行）
usage 域（wire_default_voice_usage_recorder → UsageCreateDTO）
    → ff_usage_records（purpose="tts"，token 0/0/0，真实时延，persona 绑定）
```

ASR 同构（`VoiceService.transcribe(audio, filename, persona)` → `ASR_DAILY_BUDGET_*` 分面闸 → `Qwen3AsrClient` → `POST {base_url}/v1/audio/transcriptions` multipart → `AsrResultModel` → 成功 usage 行 `purpose="asr"`）。

流式通道同构（双轨，见 [realtime-voice.md](realtime-voice.md)）：

```
VoiceService.open_asr_stream(sample_rate_hz, on_partial, persona)
    ↓ ASR_DAILY_BUDGET_* 分面闸（被拒 → 零建连、零出站）
    ↓ Qwen3AsrStreamClient.open（注入传输；握手超时 / 被拒 → LLM_CALL_FAILED）
    ↓ 连接已开 → 计 1 次；finish / cancel 收尾时落 1 行 usage（purpose="asr"）
Qwen3AsrStreamHandle（feed / finish / cancel——partial 由 on_partial 回调下行）

VoiceService.synthesize_stream(text, persona)
    ↓ TTS_DAILY_BUDGET_* 分面闸（被拒 → 零建连）
    ↓ CosyVoiceStreamClient.synthesize_stream（PCM16 分片流）
    ↓ 正常收尾 → 落 1 行 usage（purpose="tts"）；取消（barge-in）→ 不落行
```用量语义（锁定决策）：**语音调用无 token 语义**——input / output / total 恒 0 落账（调用计数 + 真实时延仍有观测价值；`UsagePurpose` 枚举 tts / asr 成员本域消费）。**仅成功后落账**：出站失败不记、被预算拒绝不记；用量发射恒不阻断主调用（记录器异常仅告警——与 llm 网关同契约）。分面预算单位 / 软竞态 / 错误 details 见 [usage.md](usage.md#分面日预算asr--tts--voiceprint)。

### 大输出边界（文档化决策）

TTS 结果为**内存字节**（`audio: bytes`）——单次合成受请求文本 500 字符约束，音频体量在 MB 量级内；若后续需要长文本 / 流式音频，在服务层演进流式传输面（结果模型保持归一形态不变）。

### ASR 上传上限（资源面守卫——与 look_at 图像 10MB 上限同性质）

ASR 端点对 multipart 请求体设大小上限（`VOICE_ASR_MAX_UPLOAD_BYTES`，默认 25MB）：超限在解析前拒绝（422 `VALIDATION_ERROR`，details 携带上限与实际字节数）。与 look_at 图像 10MB 上限的不对称是**文档化决策**：两者均为资源面守卫（防无限请求体内存缓冲），量级差异源于音频载荷体积天然大于截图——一段 30s WAV 即可超过 10MB。

## 人格语音绑定（persona voice_profile）

`ff_personas.voice_profile` JSON 可空列（迁移 `000000000006`；纯增列——既有行落 NULL）存储 `VoiceProfileModel` 序列化形态：

| 字段 | 类型 | 语义 |
|---|---|---|
| `speaker` | str（1-128） | 发音人（CosyVoice speaker id——预置名或部署侧克隆音色名） |
| `instruct_text` | str \| null（≤2000 字符） | instruct2 模式风格指令（自然语言控制语气 / 情绪；null = 普通合成） |
| `speed` | float（0.5-2.0，默认 1.0） | 语速 |

**参数解析优先级（锁定决策）：请求参数 > 人格绑定 > settings 缺省。** 无人格绑定（列 NULL / 未传人格）→ settings 缺省（`VOICE_TTS_DEFAULT_SPEAKER` / `VOICE_TTS_DEFAULT_SPEED`）。绑定管理经 personas 既有端点（`POST /api/v1/personas` 携带 / `PUT /api/v1/personas/{id}` 替换或显式 null 清除——出现即整体替换语义，无字段级合并；复制人格随卡片一并继承绑定）。

## Settings（env 面）

| 键 | 默认 | 说明 |
|---|---|---|
| `VOICE_TTS_ENABLED` | `false` | TTS 合成开关（voice API / CLI `--voice` 的门控） |
| `VOICE_TTS_BASE_URL` | `http://127.0.0.1:9880` | CosyVoice 包装服务基础 URL |
| `VOICE_TTS_MODEL_ENTRY` | `cosyvoice3` | 用量记账模型条目名 |
| `VOICE_TTS_REQUEST_TIMEOUT_SECONDS` | `30` | 单次合成请求超时（音频合成耗时显著长于普通 API） |
| `VOICE_TTS_DEFAULT_SPEAKER` | `中文女` | 缺省发音人（无人格绑定时回落） |
| `VOICE_TTS_DEFAULT_SPEED` | `1.0` | 缺省语速（0.5-2.0） |
| `TTS_DAILY_BUDGET_GLOBAL` | `0` | 全局每日 TTS 合成调用数上限（0 = 不限；有效上限 10,000） |
| `TTS_DAILY_BUDGET_PERSONA` | `0` | 单人格每日 TTS 合成调用数上限（0 = 不限；有效上限 10,000） |
| `VOICE_ASR_ENABLED` | `false` | ASR 转写开关 |
| `VOICE_ASR_BASE_URL` | `http://127.0.0.1:8082` | Qwen3-ASR 服务基础 URL |
| `VOICE_ASR_MODEL_ENTRY` | `qwen3-asr-0.6b` | 用量记账条目名（兼 multipart `model` 字段值） |
| `VOICE_ASR_REQUEST_TIMEOUT_SECONDS` | `60` | 单次转写请求超时（音频上传 + 转写） |
| `VOICE_ASR_LANGUAGE` | （空） | 缺省语言提示（空 = 端点自动检测；请求级参数覆盖） |
| `VOICE_ASR_MAX_UPLOAD_BYTES` | `26214400` | ASR 上传请求体上限（字节，25MB——超限 422；量级依据见上节） |
| `ASR_DAILY_BUDGET_GLOBAL` | `0` | 全局每日 ASR 转写调用数上限（0 = 不限；有效上限 10,000） |
| `ASR_DAILY_BUDGET_PERSONA` | `0` | 单人格每日 ASR 转写调用数上限（0 = 不限；有效上限 10,000） |
| `VOICE_ASR_STREAMING_ENABLED` | `false` | 流式识别通道开关（False = 整段批量转写；失败自动回退批量） |
| `VOICE_ASR_STREAMING_BASE_URL` | `ws://127.0.0.1:8082/v1/realtime` | 流式识别 WebSocket 端点（沿用语部分节凭据，不重复鉴权键） |
| `VOICE_ASR_STREAMING_MODEL` | `qwen3-asr-flash-realtime` | 流式识别握手 model 字段值 |
| `VOICE_ASR_STREAMING_TIMEOUT_SECONDS` | `5` | 流式识别握手 / 事件等待超时（秒） |
| `VOICE_TTS_STREAMING_ENABLED` | `false` | 流式合成通道开关（False = 整句批量合成；失败自动回退批量） |
| `VOICE_TTS_STREAMING_BASE_URL` | `ws://127.0.0.1:9880/realtime` | 流式合成 WebSocket 端点（同上凭据口径） |
| `VOICE_TTS_STREAMING_MODEL` | `cosyvoice3` | 流式合成握手 model 字段值 |
| `VOICE_TTS_STREAMING_TIMEOUT_SECONDS` | `10` | 流式合成握手 / 分片等待超时（秒） |
| `VOICE_TTS_STREAMING_SAMPLE_RATE_HZ` | `24000` | 流式合成输出 PCM16 采样率（Hz——下行 WAV 封装依据） |

键面以 `.env.example`（`pixi run gen-examples` 生成）为真源，本表为语义说明。

## 部署探测脚本（流式通道一次性连通性验证，不入门禁）

`scripts/probe_voice_streaming.py` 在部署机按生效配置对 ASR / TTS 流式端点做**最小握手与一帧往返**（ASR：握手 → 追加 100ms 探测音频 → 收尾；TTS：握手 → 取首分片 → 取消），逐子项输出结果：

```
cd backend && pixi run python ../scripts/probe_voice_streaming.py
# 可选：--only asr|tts / --timeout 30
```

| 输出 | 含义 | 处置 |
|---|---|---|
| `PASS` | 握手 + 一帧往返成功 | 可开启对应 `*_STREAMING_ENABLED` |
| `SKIP` | 未启用或端点未配置（无凭据） | 保持关闭即可——批量主链照常 |
| `FAIL` | 连接 / 握手 / 往返失败（输出端点与原因） | **该子项降级**：关闭对应流式开关（或保持运行期自动回退批量）；不影响批量主链与会话 |

退出码：0 = 无 FAIL；1 = 存在 FAIL；2 = 参数错误。脚本零真实模型推理依赖（只验协议连通性），但不入门禁链——本机 CI 全部单测走 fake 传输面。

## 管理 API（app/api/voice.py）

### POST /api/v1/voice/tts —— 文本 → WAV 音频

```
POST /api/v1/voice/tts
Content-Type: application/json

{"text": "今天也要元气满满哦", "persona_id": null, "speaker": null, "speed": null}

200 ← Content-Type: audio/wav
      X-Latency-Ms: 412            （合成往返时延毫秒）
      <WAV 音频字节>
```

人格解析：`persona_id` 显式给定（缺失 → 404 `NOT_FOUND`）；缺省取当前激活人格；两者皆无 → 未绑定形态合成（settings 缺省参数 + 用量 persona_id 为空——语音是可选叠加面，不因无人格而拒绝）。`speaker` / `speed` 为请求级显式覆盖（优先级见上节）。

错误（统一异常处理器 → `ResultDTO` error 形态）：TTS 未启用 → 409 `CONFLICT`；人格缺失 → 404；请求校验（文本 1-500 / 语速 0.5-2.0）→ 400 `VALIDATION_ERROR`；合成服务失败 → 503 `LLM_CALL_FAILED`。

### POST /api/v1/voice/asr —— 音频上传 → 转写文本

```
POST /api/v1/voice/asr
Content-Type: multipart/form-data; boundary=...

------...
Content-Disposition: form-data; name="file"; filename="memo.wav"
Content-Type: audio/wav

<WAV 音频字节>
------...
Content-Disposition: form-data; name="language"

zh
------...--

200 ← {"success": true, "code": "200", "message": "语音识别完成",
       "data": {"text": "今天也要元气满满哦", "duration_ms": 3400, "language": "zh"}}
```

`language` 字段可选（缺省 settings / 端点自动检测）；用量绑定当前激活人格。**inbound multipart 零新增依赖**（锁定决策）：FastAPI `File` / `Form` 参数面需要 `python-multipart` 运行时——本工程不引入；端点经原始 `Request` 读取请求体，用 stdlib `email` 解析器解析 multipart/form-data（RFC 2046 边界 / 引号与 RFC 2231 文件名由 stdlib 承担；字节载荷 byte 级原样透传——经 httpx 原生编码体实证）。形态不符 / 缺 `file` 字段 → 400 `BAD_REQUEST`；请求体超过 `VOICE_ASR_MAX_UPLOAD_BYTES` → 422 `VALIDATION_ERROR`；ASR 未启用 → 409。

curl 示例：

```bash
# TTS（音频落文件 + 时延头）
curl -fsS -X POST http://127.0.0.1:8000/api/v1/voice/tts \
  -H 'Content-Type: application/json' \
  -d '{"text": "今天也要元气满满哦"}' -o reply.wav -D headers.txt

# ASR（multipart 上传）
curl -fsS -X POST http://127.0.0.1:8000/api/v1/voice/asr \
  -F 'file=@memo.wav' -F 'language=zh'
```

## CLI --voice（opt-in 语音回复）

```
pixi run chat -- --voice      # 须 VOICE_TTS_ENABLED=true + TTS 服务在线
```

- 回复文本流写完后**聚合全文** → `VoiceService.synthesize(text, persona=当前激活人格)`（人格绑定发音生效）→ 播放；
- **best-effort 播放**：播放器运行时发现（`ffplay` 优先——`-autoexit -nodisp` 静默形态；`aplay` 兜底），阻塞播放经 `run_blocking_ops` 下沉线程池（不占事件循环）；**无头机器无播放器 = 告警并跳过**（正常形态——文本回复不受影响）；播放器僵死有 600s 上限兜底；
- 合成失败 → `[语音合成失败] ...` 提示上屏，**恒不中断 REPL**；`--voice` 但 TTS 未启用 → 启动期一行提示后纯文本对话；
- 旗标缺省 = 零语音对象构造、零合成调用（opt-in 契约由测试锁定）。

## 部署契约（本客户端持有文档——部署侧据此包装）

### CosyVoice3（TTS）

`models/` 子模块承载权重（部署机关注；后端零文件路径依赖）；部署为**薄包装 HTTP 服务**暴露唯一端点：

```
POST {VOICE_TTS_BASE_URL}/tts
{"text": str, "speaker": str | null, "speed": float, "instruct_text": str?}
200 ← Content-Type: audio/wav；响应体 = WAV 容器音频字节
```

缺省请求体恰为前三键（`instruct_text` 仅人格绑定携带时出现——instruct2 模式扩展，≤2000 字符）。权重编排（compose 接线 / 端口 9880 分配）由部署侧另行承接。

### Qwen3-ASR（ASR）

经其 vLLM 工具链服务以 **OpenAI 兼容 audio 端点**部署（标准 OpenAI SDK 亦可直连；本工程按「不引入 SDK」约定经共享 httpx client 手写 multipart）：

```
POST {VOICE_ASR_BASE_URL}/v1/audio/transcriptions   (multipart/form-data)
file: (filename, audio_bytes)    model: <model_id>    language?: str
200 ← {"text": str, "duration_ms"?: int, "language"?: str}
```

### 流式 ASR / TTS（R4-3——默认关闭，部署侧可选启用）

流式端点是**可选部署件**：不启用（`*_STREAMING_ENABLED=false`）时后端零 WebSocket 连接，批量主链不受影响。启用时按下列事件族契约部署（与 DashScope 实时协议同形的薄包装即可）：

```
# 流式 ASR（ws {VOICE_ASR_STREAMING_BASE_URL}）
→ {"type":"session.update","session":{"model":str,"input_audio_format":"pcm16","sample_rate_hz":int,"language":str|null}}
← {"type":"session.created"} / {"type":"session.updated"}
→ {"type":"input_audio_buffer.append","audio":base64(pcm16)}
→ {"type":"input_audio_buffer.commit"}
→ {"type":"session.finish"}
← {"type":"conversation.item.input_audio_transcription.text","text":str}        # partial
← {"type":"conversation.item.input_audio_transcription.completed","transcript":str}
← {"type":"session.finished"} / {"type":"error","error":{"code":str,"message":str}}
→ {"type":"session.cancel"}                                                     # 取消（best-effort）

# 流式 TTS（ws {VOICE_TTS_STREAMING_BASE_URL}）
→ {"type":"tts.start","text":str,"speaker":str|null,"speed":float[,"instruct_text":str],"sample_rate_hz":int,"model":str}
← {"type":"tts.started"}                                                        # 可选
← {"type":"tts.delta","audio":base64(pcm16)}                                    # 顺序即播放序
← {"type":"tts.completed"} / {"type":"error",...}
→ {"type":"tts.cancel"}                                                         # barge-in（best-effort）
```

关闭码处置（锁定）：关闭码仅入错误 `details`；收尾事件（final / completed）前的任何对端关闭一律按失败处理并回退批量；`1008` / `1011` 属服务端语义，客户端不主动发送。客户端主动关闭只用 `1000`（正常收尾 / 取消）/ `1001`（收尾超时）/ `1002`（握手失败）。

### 流式连接计次口径（锁定——与 R4-2 分面语义一致）

| 事件 | 预算闸 | usage 行 |
|---|---|---|
| ASR 流式连接**开启成功** | 开启前判定（被拒 → 零建连、零出站） | 0 行（尚未收尾） |
| ASR 流式连接收尾（finish 成功 / cancel 干净关闭） | — | **1 行**（`purpose="asr"`，provider `qwen3-asr`，token 0） |
| ASR 连接 / 握手失败（未开启） | 开启前已判定 | 0 行（回退批量后按批量口径记账） |
| TTS 每句流式合成成功收尾 | 每句判定（被拒 → 零建连） | **1 行**（`purpose="tts"`，且产出过非空分片；空音频 0 行、回退批量后按批量口径记账） |
| TTS 流式中途取消（barge-in） | 已判定 | 0 行（与批量句级取消等价） |

被拒调用不建连、不出站、不落行；流式失败回退批量时，批量路径按自身口径独立判定与记账（不重复计数）。


503「依赖服务不可用」类与 LLM 端点同属远程模型服务依赖（usage 域亦将 tts / asr 与 chat / embed 并列为模型调用用途），故复用 `LLM_CALL_FAILED`；禁用态复用通用 `CONFLICT`（409）：

| 失败类别 | 错误码 | HTTP |
|---|---|---|
| TTS / ASR 未启用 | `CONFLICT` | 409 |
| 请求参数越界（文本 / 语速 / 空音频 / 空文件名 / ASR 上传超限） | `VALIDATION_ERROR` | 422 |
| 上传形态不符（非 multipart / 缺 file 字段） | `BAD_REQUEST` | 400 |
| 传输异常 / 非 200 / 响应契约破坏（内容类型 / JSON / 空体） | `LLM_CALL_FAILED` | 503 |
| 公共池未启动（惰性构建前置破坏） | `INTERNAL_ERROR` | 500 |

## 声纹档案（PIPL 合规面）

`harness/voice/voiceprint_*` 承载声纹识别能力：**在用户主动授予同意后**注册语音样本、提取说话人向量、在后续音频中校验「是否同一说话人」。声纹按**人格 × 发送者**隔离；样本音频落盘、向量落库，撤回同意或删除档案时样本文件与向量立即清除，同意记录只保留版本与授予 / 撤回时间作为处理合法性凭证。

### PIPL 告知与同意（目的 / 范围 / 保存期 / 撤回权）

管理 API `GET /api/v1/voiceprints/notice` 返回当前告知文本（版本 `v1`，保存期取 `VOICEPRINT_RETENTION_DAYS`）；告知文本明确：

| 义务 | 文本口径 |
|---|---|
| 目的 | 在多人对话与语音消息中区分说话人身份（称呼 / 关系记忆 / 说话人确认） |
| 范围 | 仅同意后采集样本，提取说话人向量，保存时长 / 采样率 / 哈希等必要元数据；全文不因声纹额外留存，向量不用于第三方训练 |
| 保存期 | 样本与向量最多 `VOICEPRINT_RETENTION_DAYS` 天（到期自动清理）；撤回 / 删除立即清除 |
| 撤回权 | 随时可撤回同意或删除档案；撤回后样本音频与向量立即清除，仅保留同意文本版本 / 授予时间 / 撤回时间 |
| 隔离 | 人格 × 发送者隔离，他人格不可见 |

授予同意是注册与校验的硬前置（`POST .../consent`）；同版本有效同意幂等返回。撤回（`POST .../consent/withdraw`）写入 `revoked_at_ms` 并同批清除样本行与文件。删除档案（`DELETE .../{sender_id}`）只清样本，不动同意记录（主人可保留授权凭证继续使用该校验能力）。

### 数据模型（迁移 `000000000016`）

`ff_voiceprint_samples`（每人格 × 每发送者每样本一行）：

| 字段 | 类型 | 语义 |
|---|---|---|
| `persona_id` | varchar(64) | 人格作用域（PersonaMixin 自动注入 / 过滤） |
| `sender_id` | varchar(128) | 发送者 ID（平台侧用户标识） |
| `sample_path` | varchar(512) | 样本文件落盘路径（撤回 / 删除 / 过期时随行清理） |
| `sample_sha256` | varchar(64) | 样本哈希（同发送者去重键） |
| `duration_ms` / `sample_rate_hz` | int | 样本时长 / 采样率（注册门槛校验面） |
| `embedding` / `embedding_dim` | text / int | 说话人向量（JSON 浮点数组）/ 维度（同档案一致性校验） |

唯一键 `uq_ff_voiceprint_samples_persona_sender_sha (persona_id, sender_id, sample_sha256)`；索引 `ix_ff_voiceprint_samples_persona_sender`。`ff_voiceprint_consents` 记录 `consent_text_version` / `granted_at_ms` / `revoked_at_ms`（可空 = 有效），同一发送者可多次授予 / 撤回，读取面取最新有效行。

### 链路顺序（注册 / 校验）

```
POST /api/v1/voiceprints/{sender_id}/enroll
    ↓ ① 人格解析 + bind_persona_context（隔离轴）
    ↓ ② 功能门控：VOICEPRINT_ENABLED=true 且 VOICEPRINT_EMBEDDING_BASE_URL 非空
    ↓ ③ 同意闸：当前有效同意（版本 = v1）不存在 → 409
    ↓ ④ 批量门槛：样本数 ≥ VOICEPRINT_MIN_SAMPLES、≤ MAX_BATCH_SAMPLES、
    │            档案累计 ≤ MAX_SAMPLES
    ↓ ⑤ 逐样本校验（MIME 白名单 / base64 / 字节上限 / 时长 / 采样率 / 哈希去重）
    ↓ ⑥ 每样本 VOICEPRINT_DAILY_BUDGET_* 闸 → 向量提取（共享 HTTP 池）→ 维度一致性
    │   （中途超限：整体回滚本批文件与样本行；已成功出站调用各留一行用量）
    ↓ ⑦ 样本原子落盘（临时文件 + fsync + os.replace）+ 行插入（逐样本部分成功）
    ↓ ⑧ 返回注册结果与档案聚合

POST /api/v1/voiceprints/{sender_id}/verify
    ↓ ①-③ 同上（校验同样要求同意）
    ↓ ④ 短音频拒识：duration_ms < VOICEPRINT_MIN_VERIFY_DURATION_MS
    ↓ ⑤ 保存期清理 → 无样本 → matched=false（no_samples）
    ↓ ⑥ VOICEPRINT_DAILY_BUDGET_* 闸 → 查询向量 → 档案向量均值 → 余弦相似度
    ↓ ⑦ matched = similarity ≥ VOICEPRINT_SIMILARITY_THRESHOLD
```

时长门槛是两条独立路径：注册按 `VOICEPRINT_MIN_SAMPLE_DURATION_MS` 校验
样本，校验按 `VOICEPRINT_MIN_VERIFY_DURATION_MS` 判定。低于校验门槛的
音频返回 `matched=false` / `audio_too_short`（不抛 422）；位于
`[VOICEPRINT_MIN_VERIFY_DURATION_MS, VOICEPRINT_MIN_SAMPLE_DURATION_MS)`
区间的音频正常进入相似度比对，不套用注册门槛。

### API（app/api/voiceprints.py）

```
POST /api/v1/voiceprints/user-1/consent
{"consent_text_version": "v1"}
200 ← {"success": true, "code": "200", "message": "声纹同意已授予",
       "data": {"id": 3, "sender_id": "user-1", "consent_text_version": "v1",
                "granted_at_ms": 1760000000000, "revoked_at_ms": null}}

POST /api/v1/voiceprints/user-1/enroll
{"samples": [
  {"audio_base64": "UklGRi...", "content_type": "audio/wav",
   "duration_ms": 2400, "sample_rate_hz": 16000},
  {"audio_base64": "UklGRi...", "content_type": "audio/wav",
   "duration_ms": 2100, "sample_rate_hz": 16000}
]}
200 ← {"success": true, "code": "200", "message": "声纹注册完成",
       "data": {"accepted": 2,
                "rejected": [],
                "profile": {"sender_id": "user-1", "sample_count": 2,
                            "total_duration_ms": 4500,
                            "samples": [{"id": 7, "sender_id": "user-1",
                                         "sample_sha256": "9f2c...",
                                         "duration_ms": 2400, "sample_rate_hz": 16000,
                                         "embedding_dim": 192,
                                         "created_at": 1760000000000}],
                            "consent": {"id": 3, "sender_id": "user-1",
                                        "consent_text_version": "v1",
                                        "granted_at_ms": 1760000000000,
                                        "revoked_at_ms": null},
                            "privacy_notice_version": "v1",
                            "privacy_notice": "声纹识别用于…（完整告知文本）"}}}

POST /api/v1/voiceprints/user-1/verify
{"sample": {"audio_base64": "UklGRi...", "content_type": "audio/wav",
            "duration_ms": 1800, "sample_rate_hz": 16000}}
200 ← {"success": true, "code": "200", "message": "声纹校验完成",
       "data": {"sender_id": "user-1", "matched": true, "similarity": 0.8731,
                "threshold": 0.75, "reason": "matched"}}

POST /api/v1/voiceprints/user-1/consent/withdraw
200 ← {"success": true, "code": "200", "message": "声纹同意已撤回，样本已清除",
       "data": {"consent": {"id": 3, "sender_id": "user-1",
                            "consent_text_version": "v1",
                            "granted_at_ms": 1760000000000,
                            "revoked_at_ms": 1760000500000},
                "deleted_samples": 2}}

DELETE /api/v1/voiceprints/user-1
200 ← {"success": true, "code": "200", "message": "声纹档案已删除",
       "data": {"sender_id": "user-1", "deleted_samples": 0}}
```

`GET /api/v1/voiceprints` 返回当前人格全部档案（样本主体 ∪ 同意主体，便于展示「已同意未注册」态）；`GET /api/v1/voiceprints/{sender_id}` 返回单档案（无样本时零条目空档案）。全部端点支持 `persona_id` 查询参数（缺省当前主人格；无主人格 404）。

### 向量提供方契约（部署侧包装）

```
POST {VOICEPRINT_EMBEDDING_BASE_URL}{VOICEPRINT_EMBEDDING_PATH}
Content-Type: multipart/form-data
    file:  (filename, audio_bytes, content_type)   # 音频文件（必填）
    model: str                                      # VOICEPRINT_EMBEDDING_MODEL

200 → application/json
{"embedding": [float, ...]}          # 非空有限浮点数组
# 或 OpenAI 兼容形态：{"data": [{"embedding": [float, ...]}]}
```

未配置提供方（`VOICEPRINT_EMBEDDING_BASE_URL` 为空）时**注册 / 校验**关闭——不产生「已同意但无法比对」的半配置态；同意授予 / 撤回、档案读取与删除不受总闸限制（关停功能后仍可履行撤回权并清理数据）。

### 错误码映射（voiceprint 段——复用既有码表）

| 失败类别 | 错误码 | HTTP |
|---|---|---|
| 注册 / 校验时功能关闭或未配置向量提供方 / 未授予同意 | `CONFLICT` | 409 |
| 同意文本版本不一致 / 样本条数越界 / 校验音频非法 | `VALIDATION_ERROR` | 422 |
| 无有效同意时撤回 | `NOT_FOUND` | 404 |
| 向量提供方传输 / 非 200 / 响应契约破坏 | `LLM_CALL_FAILED` | 503 |
| 日预算超限（注册 / 校验出站前拒绝；注册已写文件回滚） | `RATE_LIMITED` | 429 |

### Settings（`VOICEPRINT_` env 面——默认关闭）

| 键 | 默认 | 说明 |
|---|---|---|
| `VOICEPRINT_ENABLED` | `false` | 声纹功能总闸 |
| `VOICEPRINT_EMBEDDING_BASE_URL` | （空） | 说话人向量服务基础 URL（空 = 整功能关闭） |
| `VOICEPRINT_EMBEDDING_PATH` | `/embedding` | 向量端点路径 |
| `VOICEPRINT_EMBEDDING_MODEL` | `voiceprint-embedding` | 向量模型名（multipart `model` 字段） |
| `VOICEPRINT_REQUEST_TIMEOUT_SECONDS` | `30.0` | 向量提取请求超时 |
| `VOICEPRINT_SIMILARITY_THRESHOLD` | `0.75` | 校验余弦相似度阈值（0-1） |
| `VOICEPRINT_MIN_SAMPLES` | `3` | 一次注册至少样本数 |
| `VOICEPRINT_MAX_SAMPLES` | `20` | 单发送者档案样本上限 |
| `VOICEPRINT_MIN_SAMPLE_DURATION_MS` | `1500` | 注册样本最短时长 |
| `VOICEPRINT_MIN_VERIFY_DURATION_MS` | `1000` | 校验音频最短时长（短音频拒识） |
| `VOICEPRINT_MIN_SAMPLE_RATE_HZ` | `8000` | 注册样本最低采样率 |
| `VOICEPRINT_MAX_UPLOAD_BYTES` | `10485760` | 单样本字节上限（10MB 量级） |
| `VOICEPRINT_MAX_BATCH_SAMPLES` | `20` | 单次注册请求样本条数上限 |
| `VOICEPRINT_RETENTION_DAYS` | `365` | 样本与向量最长保存天数 |
| `VOICEPRINT_STORAGE_DIR` | `data/voiceprints` | 样本文件落盘根目录 |
| `VOICEPRINT_DAILY_BUDGET_GLOBAL` | `0` | 全局每日声纹向量提取调用数上限（0 = 不限；有效上限 10,000） |
| `VOICEPRINT_DAILY_BUDGET_PERSONA` | `0` | 单人格每日声纹向量提取调用数上限（0 = 不限；有效上限 10,000） |

## 测试

`src/tests/unit/test_voice_{settings,clients,service,api}.py` + `test_persona_voice_profile.py` + `test_chat_cli_voice.py`（82 例）：客户端 MockTransport（请求形状 / multipart 体 / 内容类型校验 / 超时 / 错误映射）、服务（优先级 / 仅成功用量行 / 分面预算闸 / disabled / 惰性构建 / 默认接线真实落库）、人格绑定全链路 round-trip（服务 / DTO / API）、API（ASGI + 处理器直调双面：字节响应 + 头 / multipart 解析 / 校验 400 / 上传上限字节精确边界）、CLI（旗标开关 / 人格绑定 / 失败存活 / 旗标透传）。迁移 `000000000006` 经 scratch SQLite `alembic upgrade head` → `downgrade -1` → 再 upgrade 实证（升列 / 降列 / 复升三态）。

声纹测试：`src/tests/unit/test_voiceprint_service.py`（同意闸 / 注册门槛与逐样本拒收 / 重复哈希 / 向量维度 / 时长与采样率 / 短音频拒识 / 命中与阈值下 / 撤回清样本留凭证 / 删除幂等 / 保存期清理 / 人格 × 发送者隔离）、`test_voiceprint_embedding.py`（multipart 请求形状 / 两种响应形态 / 传输与非 200 / 向量非法）、`test_voiceprint_api.py`（告知 / 同意 / 注册 / 档案 / 校验 / 撤回 / 删除 / 人格解析 404 / 请求体校验）、`test_voice_budget.py`（三组分面预算矩阵 / purpose 隔离 / 记账口径 / 声纹中途超限回滚）；迁移 `000000000016` 经 scratch SQLite round-trip（`test_migration_voiceprint_sticker.py`）与 SeekDB 真引擎迁移链（`integration_seekdb/test_migration_chain.py`）双面验证。
