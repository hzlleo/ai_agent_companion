# realtime-voice — 实时语音全双工会话（VAD + 流式转写 + 句级 TTS + 打断）

`backend/src/packages/harness/voice/realtime_*` 承载独立的全双工语音会话：WebSocket 单连接单会话、能量/静音窗口 VAD 切段、切段后经既有 ASR 面转写、人格回复流式生成按句切分后经既有 TTS 面合成并分片下行、用户新语音或手动按钮触发 barge-in（取消在途生成与合成、清空播放队列）。

服务端**只经 HTTP 契约消费既有语音服务**（CosyVoice TTS 包装端点 / Qwen3-ASR OpenAI 兼容 audio 端点，见 [voice.md](voice.md)），不读取模型文件、不新增依赖；上行音频只做 PCM16 字节运算（能量统计 / WAV 封装），下行音频只做 WAV 字节分片——前端用 Web Audio `decodeAudioData` 解码播放。

## 能力与取材边界（实际实现依据）

| 环节 | 实际取材 | 说明 |
|---|---|---|
| 转写 | 既有 `VoiceService.transcribe` → Qwen3-ASR **批量** audio 端点 | 该端点无流式通道；切段后整段上传转写。**中途 partial 转写**由节流的中途批量转写产生（同一语段的已积累音频在节流间隔到达时上传一次，结果作为 `partial_transcript` 下行；最终稿以切段后的完整音频转写为准） |
| 合成 | 既有 `VoiceService.synthesize` → CosyVoice **整句** WAV | 该端点返回完整 WAV 字节，无流式音频；服务端按句合成后按固定字节数分片下行（`audio_chunk`），客户端按 `turn_id + sentence_index` 聚合收齐后解码——半句音频永不下发（未闭合句不进合成队列） |
| 打断 | 服务端任务取消 + 下行 `interrupted` / `state(listening)` | 播放停止由客户端收到 `interrupted` 后立即执行（并回 `barge_in_ack`）；服务端同时取消生成任务与逐句合成任务、清空未合成句队列、丢弃未闭合句缓冲 |
| 回声消除（AEC） | **未做**（浏览器侧依赖系统/耳机） | 外放场景下麦克风可能拾取 TTS 输出触发误打断——部署建议耳机或浏览器 AEC 平台能力，见「边界」 |

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `realtime_models.py` | 协议 / 配置 / 指标 Model Classes（Unit 文件）：上下行判别联合、VAD 公开参数、会话上限、`REALTIME_VOICE_` 配置与不变式、指标快照、传输与回复生成面协议 | `REALTIME_INBOUND_ADAPTER` / `REALTIME_OUTBOUND_ADAPTER` / `RealtimeVoiceSettings` / `RealtimeTransportFace` / `RealtimeChatFace` / `RealtimeSessionMetricsModel` |
| `realtime_vad.py` | 能量/静音窗口 VAD（双阈值滞回 + 起说防抖 + 最短语音 + 单段上限），帧时长累加计时（不读系统时钟） | `EnergyVad` / `build_vad_config` / `VadEventModel` |
| `realtime_state.py` | 相位迁移表（idle → listening → thinking → speaking → listening，含打断 / 超时 / 错误 / 关闭） | `RealtimeVoiceStateMachine` / `RealtimeStateEvent` / `RealtimeTransitionModel` |
| `realtime_sentence.py` | 助手文本句切（标点 / 收尾符号 / 长度上限 / 流式累积） | `split_assistant_sentences` / `SentenceAccumulator` |
| `realtime_audio.py` | PCM16 能量 / WAV 封装 / 字节分片 / 时长换算 | `compute_rms_normalized` / `pcm16_to_wav` / `iter_byte_chunks` |
| `realtime_session.py` | 会话引擎：收发循环 / 背压队列 / VAD 事件处理 / 转写 / 生成 / 逐句 TTS / 打断 / 看门狗 / 优雅关闭 / 指标日志 | `RealtimeVoiceSession` / `ChatSessionStreamAdapter` |
| `app/api/realtime_voice.py` | WS 端点（只做 API 层）：准入（鉴权 / 总闸 / 人格解析）、ChatService 与语音服务装配、Starlette 传输适配 | `realtime_voice_websocket` / `StarletteRealtimeTransport` |

依赖方向：`voice.realtime_* → voice（服务门面 / 客户端契约）、chat（ChatService 流式）、config、core、persona、pools`；app 层仅装配与适配，协议语义全部在 harness（`app/` 只做 API 层的硬约束）。

## 链路顺序（麦克风 → VAD → ASR → compose → 句切 → TTS → 下行 → 打断）

```
浏览器麦克风（AudioContext，16kHz 单声道 PCM16）
    ↓ WS audio_chunk（base64，seq 递增；单块解码后 ≤ 64 KiB）
realtime_session 收帧循环
    ↓ 背压队列（REALTIME_VOICE_AUDIO_QUEUE_MAX_CHUNKS；溢出丢最旧块并计数）
    ↓ 批量出队（REALTIME_VOICE_AUDIO_BATCH_MAX_CHUNKS）
EnergyVad（起说防抖 / 语音态 / 静音窗 / 单段上限）
    ↓ speech_start（播放中 → barge-in，见下文时序）
    ↓ segment_ready / segment_forced（PCM16 + 有效语音时长）
转写任务（音频工人只投递；转写期间 VAD / 打断判定继续）
    ↓ VoiceService.transcribe → Qwen3-ASR（批量端点）
    ↓ final_transcript（下行；中途 partial 由节流批量转写产生）
ChatService.stream_reply（人格正典 / 宪章 / 注入防御 / 关系进展 / 情绪块 / 合规守卫配置）
    ↓ assistant_text 增量（下行）
SentenceAccumulator（标点 / 收尾符号 / 长度上限）
    ↓ 已闭合句
VoiceService.synthesize → CosyVoice（整句 WAV）
    ↓ audio_chunk 分片（base64 WAV，turn_id + sentence_index + seq）
    ↓ state（speaking）→ 客户端聚合收齐后 decodeAudioData 串行播放
    ↓ 全部句播完 → state（listening）
```

多段转写可并行；转写完成的文本按**完成序**进入生成请求通道（单会话同一时刻只有一个活跃轮次，其余排队依次消费）。客户端以 `segment_id` 对齐 final_transcript 与对应轮次，因此近似同时完成的多段可能先收到后段语段的结果。

## WS 端点与准入

| 项 | 值 |
|---|---|
| 路径 | `GET /api/v1/realtime/voice/ws`（WS 升级） |
| 鉴权 | 查询参数 `token`（浏览器 WS 无法设置自定义头）或 `Authorization: Bearer` 头；与 HTTP 管理面同一 `verify_admin_token` 恒时比较（`ADMIN_AUTH_TOKEN` 为空 = 鉴权关闭）。失败：下行 `error(UNAUTHORIZED)` 后 1008 关闭 |
| 总闸 | `REALTIME_VOICE_ENABLED=false` → 下行 `error(CONFLICT)` 后 1013 关闭（可重试） |
| 人格 | 查询参数 `persona_id`（缺省当前激活人格；显式缺失 / 无主人格 → `NOT_FOUND` + 关闭）。`start.persona_id` 提供时必须与连接绑定人格一致 |
| 会话落库 | 连接建立时创建持久化聊天会话（`ff_chat_sessions` / `ff_chat_messages`，与文字通道同表）——语音回合参与记忆 / 梦境可见面 |
| 单连接单会话 | `start` 只接受一次；重复 `start` 返回可恢复 `error` |

握手后的下行 `ready` 公告能力与上限（客户端据此决定是否启动采集与是否显示「仅字幕」形态）：

```json
{
  "type": "ready",
  "ts_ms": 1760000000000,
  "protocol_version": "1",
  "session_id": "6f1c2f9d4b0e4a0f9c1d2e3f4a5b6c7d",
  "sample_rate_hz": 16000,
  "channels": 1,
  "audio_format": "wav",
  "asr_enabled": true,
  "tts_enabled": true,
  "barge_in_enabled": true,
  "vad": {
    "speech_rms_threshold": 0.06,
    "silence_rms_threshold": 0.03,
    "min_speech_ms": 300,
    "silence_end_ms": 700,
    "max_segment_ms": 15000
  },
  "limits": {
    "max_session_ms": 600000,
    "idle_timeout_ms": 60000,
    "heartbeat_interval_ms": 15000,
    "audio_queue_max_chunks": 64,
    "sentence_max_chars": 80
  }
}
```

相位实际迁移时服务端会即时下行 `state`（客户端据此更新状态 / 字幕提示）。五条关闭路径（`client_end` / `idle_timeout` / `max_duration` / `internal_error` / `session_rejected`）的 wire 顺序均为（可选 `error`）→ `state(closed)` → `closed`；传输断开（`transport_closed`）时对端已不可达，`closed` 尽力发送，不保证 `state(closed)`。心跳 `ping` 的应答是携带 `detail="心跳应答"` 的 `state` 消息。

`ready` 字段说明：

| 字段 | 类型 | 语义 |
|---|---|---|
| `protocol_version` | str | 协议版本（当前 `1`） |
| `session_id` | str | 服务端会话标识（日志 / 指标关联） |
| `sample_rate_hz` / `channels` | int | 上行 PCM 格式（与 `REALTIME_VOICE_` 配置一致；客户端不一致的 `start` 会被拒绝） |
| `audio_format` | str | 下行音频格式（恒 `wav`——TTS 端点契约） |
| `asr_enabled` / `tts_enabled` | bool | 能力可用性（ASR 关闭时整会话拒绝；TTS 关闭 = 仅文本字幕） |
| `barge_in_enabled` | bool | 打断开关（关闭时按钮禁用、新语音不打断播放） |
| `vad.*` | — | 公开的 VAD 参数（与配置同源，客户端可本地预判） |
| `limits.*` | — | 会话资源上限（时长 / 空闲 / 心跳建议 / 队列 / 单句字符上限） |

## 上行协议（客户端 → 服务端）

全部消息为 JSON 文本帧；时间字段 epoch 毫秒；未知 `type` / 字段越界 / 非法 base64 → 可恢复 `error(VALIDATION_ERROR)`（会话保留）。

| type | 字段 | 语义 |
|---|---|---|
| `start` | `ts_ms` / `persona_id`(可选) / `sample_rate_hz`(可选) / `channels`(可选) | 建立会话；采样率 / 声道提供时必须与服务端配置一致 |
| `audio_chunk` | `ts_ms` / `seq` / `data`(base64 PCM16) | 麦克风 PCM16 分块（解码后 2 字节至 64 KiB；超过单块上限即拒绝该块：可恢复 `error(VALIDATION_ERROR)` + `audio_chunks_rejected` 计数，会话保留） |
| `end` | `ts_ms` | 客户端结束：服务端冲刷尾段、排空在途转写与轮次播报后 `closed`（排空期间服务端超时 / 内部关闭先到即取消在途工作并收尾） |
| `barge_in` | `ts_ms` / `reason`(`manual` / `vad`) | 手动打断（播放面板按钮） |
| `barge_in_ack` | `ts_ms` / `interruption_id` | 客户端已停止本地播放的确认（回传下行 interrupted 的标识） |
| `ping` | `ts_ms` | 心跳（服务端以 `state` 应答并刷新空闲计时） |

示例：

```json
{"type": "start", "ts_ms": 1760000000000, "persona_id": 3, "sample_rate_hz": 16000, "channels": 1}
{"type": "audio_chunk", "ts_ms": 1760000000050, "seq": 0, "data": "AADgAOAA4ADgAOAA4ADgAA=="}
{"type": "barge_in", "ts_ms": 1760000001200, "reason": "manual"}
{"type": "barge_in_ack", "ts_ms": 1760000001210, "interruption_id": "intr-1"}
{"type": "end", "ts_ms": 1760000009000}
```

## 下行协议（服务端 → 客户端）

| type | 字段 | 语义 |
|---|---|---|
| `partial_transcript` | `ts_ms` / `segment_id` / `text` | 进行中语段的节流转写（非最终；与 final 共用 `segment_id`） |
| `final_transcript` | `ts_ms` / `segment_id` / `text` / `duration_ms` / `language` | 切段后的最终转写（空结果不下行） |
| `assistant_text` | `ts_ms` / `turn_id` / `text` / `final` | 人格回复文本（`final=false` 为增量；`final=true` 为整轮全文） |
| `audio_chunk` | `ts_ms` / `turn_id` / `sentence_index` / `seq` / `total_chunks` / `content_type` / `data`(base64) | 句级 TTS 音频分片（收齐 `total_chunks` 后整句解码播放） |
| `state` | `ts_ms` / `state` / `detail` | 相位迁移（同时作为 ping 应答） |
| `interrupted` | `ts_ms` / `interruption_id` / `turn_id` / `reason` | 打断已生效（在途生成与合成已取消，播放队列应清空） |
| `error` | `ts_ms` / `code` / `message` / `recoverable` | 可恢复（当前轮次失败，会话保留）或不可恢复（随后 `closed`） |
| `closed` | `ts_ms` / `reason` / `detail` | 会话关闭（在此之前已下行 `state(closed)`；随后关闭传输） |

示例（一次完整轮次的下行序）：

```json
{"type": "final_transcript", "ts_ms": 1760000000780, "segment_id": "seg-1", "text": "今天好累啊", "duration_ms": 1200, "language": "zh"}
{"type": "assistant_text", "ts_ms": 1760000000950, "turn_id": "turn-1", "text": "那就先坐下，", "final": false}
{"type": "assistant_text", "ts_ms": 1760000001100, "turn_id": "turn-1", "text": "说说今天怎么了？", "final": false}
{"type": "assistant_text", "ts_ms": 1760000001150, "turn_id": "turn-1", "text": "那就先坐下，说说今天怎么了？", "final": true}
{"type": "state", "ts_ms": 1760000001300, "state": "speaking", "detail": null}
{"type": "audio_chunk", "ts_ms": 1760000001310, "turn_id": "turn-1", "sentence_index": 0, "seq": 0, "total_chunks": 3, "content_type": "audio/wav", "data": "UklGR..."}
{"type": "audio_chunk", "ts_ms": 1760000001320, "turn_id": "turn-1", "sentence_index": 0, "seq": 1, "total_chunks": 3, "content_type": "audio/wav", "data": "iVBOR..."}
{"type": "state", "ts_ms": 1760000002600, "state": "listening", "detail": null}
```

## 状态机（相位迁移表）

相位：`idle → listening → thinking → speaking → listening`；`closed` 为吸收态。迁移由 `realtime_state._RULES` 显式枚举（无隐式兜底），非法事件返回 `allowed=false` 且相位不变（协议层迟到 / 重复消息不致命）。

| 相位 | 事件 | 结果 |
|---|---|---|
| idle | start | listening |
| listening | speech_start / asr_done / turn_done | listening（空转） |
| listening | segment_ready | thinking |
| listening | audio_playing | speaking |
| thinking | segment_ready / speech_start / asr_done / tts_failure | thinking |
| thinking | audio_playing | speaking |
| thinking | turn_done / barge_in / asr_error / generation_error | listening |
| speaking | speech_start / audio_playing / tts_failure / generation_error | speaking |
| speaking | segment_ready | thinking（新语段串行进入转写） |
| speaking | asr_done | speaking（转写完成，等待当前轮播完后生成） |
| speaking | turn_done / barge_in / asr_error | listening |
| 任意非关闭态 | client_end / idle_timeout / max_duration / internal_error | closed |
| closed | 任意关闭事件 | closed（吸收态） |

## VAD 参数与切段矩阵

机制：归一化能量（平均绝对值 / 满量程）逐帧计算；**起说需连续 `vad_start_consecutive_frames` 帧达到起说阈值**（单帧尖峰不触发）；进入语音态后，帧能量持续低于断句阈值累计 `vad_silence_end_ms` 即切段；任一帧回到阈值以上清零静音计时（滞回，句中换气不切分）；有效语音低于 `vad_min_speech_ms` 直接丢弃（不调 ASR）；语段总时长达到 `vad_max_segment_ms` 强制切段。计时由帧时长累加，与网络接收节奏无关。

| 输入形态 | 结果 |
|---|---|
| 单帧高声 | 不起说（防抖） |
| 连续达标帧 | 起说（候选帧并入语段缓冲，不丢前导音） |
| 短暂停顿（< 静音窗） | 不切段；恢复高声后继续同一语段 |
| 静音 ≥ 静音窗 | `segment_ready`（送 ASR） |
| 有效语音 < 最短门槛 | `segment_discarded`（不调 ASR，指标计数） |
| 持续语音 ≥ 单段上限 | `segment_forced`（强制切段送 ASR） |
| 客户端 `end` | `flush` 冲刷在途语段后转写并回复 |

## 打断（barge-in）时序

```
用户新语音（VAD speech_start，相位 thinking/speaking）
    │ 或客户端 barge_in（手动按钮）
    ↓
服务端：取消在途生成任务（ChatService.stream_reply 生成器关闭——半截回合不入历史）
    ↓ 取消逐句 TTS 任务（在途 synthesize 请求取消）
    ↓ 清空未合成句队列（丢弃句数落指标日志）
    ↓ 丢弃未闭合句缓冲（未闭合句从不合成 → 无半句音频）
    ↓ 下行 interrupted（interruption_id / turn_id / reason）
    ↓ 下行 state（listening）
    ↓
客户端：清空播放队列（停止在播音源 + 作废在途解码）→ 上行 barge_in_ack
```

安全边界（锁定）：打断后生成 / 合成 / 中途转写任务全部 cancel + gather，任务集合只剩仍在转写已说完语段的任务（其结果照常产出 final_transcript 与回复）；会话关闭后任务集合为空。被取消的生成轮次不写入助手回合（与既有 `stream_reply` 半截不入历史的语义一致）。

## 并发、背压与资源上限

| 机制 | 行为 |
|---|---|
| 音频入队 | 有界队列 `REALTIME_VOICE_AUDIO_QUEUE_MAX_CHUNKS`；满时**丢最旧块**并计数（实时音频宁丢旧帧不阻塞收帧循环）；丢弃数每 32 块落一条 warning，关闭时落指标日志 |
| 音频块上限 | 单块 base64 解码后 **≤ 64 KiB**（`REALTIME_AUDIO_CHUNK_MAX_BYTES`；对应编码长度上限同步校验）；超限块在解析期拒绝——可恢复 `error(VALIDATION_ERROR)`，计入 `audio_chunks_rejected`，不进入队列、不影响后续帧 |
| 批量出队 | 单次最多 `REALTIME_VOICE_AUDIO_BATCH_MAX_CHUNKS` 块喂 VAD（降低逐帧调度开销） |
| 转写并发 | 切段后转写以独立任务执行并登记进统一任务集合——音频工人只投递，VAD / 起说判定 / 打断在转写窗口内持续工作；多段转写可并行，结果按完成序进入生成请求通道 |
| 轮次串行 | 单会话同一时刻只有一个活跃轮次；打断开关关闭时新语段排队（播完当前轮再生成） |
| 会话上限 | `REALTIME_VOICE_MAX_SESSION_MS`（到时 `closed(max_duration)`） |
| 空闲超时 | `REALTIME_VOICE_IDLE_TIMEOUT_MS`（无任何上行消息即 `closed(idle_timeout)`；ping / 音频 / 控制消息均刷新） |
| 心跳 | `REALTIME_VOICE_HEARTBEAT_INTERVAL_MS` 为 `ready` 建议值；服务端只按空闲超时判定 |
| 优雅关闭 | `end` → 冲刷尾段 → 排空在途转写与轮次播报 → `closed(client_end)`；排空期间服务端超时 / 内部关闭先到即取消在途生成与 TTS 并立即收尾（`closed` 原因取先到者）。服务端主动关闭（超时 / 错误 / 拒绝）与客户端 `end` 均在 `closed` 前下行 `state(closed)` |

## 错误码（复用 core 码表，不新增错误码）

| 场景 | code | recoverable |
|---|---|---|
| 非法 JSON / 未知 type / 字段越界 / 采样率或声道不一致 / start.persona_id 不一致 / 重复 start / 未 start 的音频 | `VALIDATION_ERROR` | true（采样率与 persona 不一致时 false，随后 `closed(session_rejected)`） |
| 音频块超过单块上限（解码后 > 64 KiB） | `VALIDATION_ERROR` | true（该块丢弃并计入 `audio_chunks_rejected`，会话保留） |
| ASR 未启用（start 时） | `CONFLICT` | false（随后 `closed(session_rejected)`） |
| 总闸关闭（WS 准入） | `CONFLICT` | false（1013 关闭） |
| 鉴权失败（WS 准入） | `UNAUTHORIZED` | false（1008 关闭） |
| 无主人格 / 人格不存在 | `NOT_FOUND` | false（1008 关闭） |
| ASR 调用失败 | `LLM_CALL_FAILED` | true（回聆听，会话保留） |
| 回复生成失败 | `LLM_CALL_FAILED` | true（回聆听，会话保留） |
| TTS 单句失败 | `LLM_CALL_FAILED` | true（跳过该句，后续句继续） |
| 引擎意外异常 | `INTERNAL_ERROR` | false（随后 `closed(internal_error)`） |

## 配置（`REALTIME_VOICE_` env 面，默认关闭）

| 键 | 默认 | 说明 |
|---|---|---|
| `REALTIME_VOICE_ENABLED` | `false` | WS 端点总闸 |
| `REALTIME_VOICE_SAMPLE_RATE_HZ` | `16000` | 上行采样率（8000-48000） |
| `REALTIME_VOICE_CHANNELS` | `1` | 上行声道数（1-2） |
| `REALTIME_VOICE_VAD_SPEECH_RMS_THRESHOLD` | `0.06` | 起说能量阈值（归一化 0-1） |
| `REALTIME_VOICE_VAD_SILENCE_RMS_THRESHOLD` | `0.03` | 断句能量阈值（必须低于起说阈值） |
| `REALTIME_VOICE_VAD_START_CONSECUTIVE_FRAMES` | `3` | 起说防抖帧数 |
| `REALTIME_VOICE_VAD_MIN_SPEECH_MS` | `300` | 最短有效语音（低于丢弃） |
| `REALTIME_VOICE_VAD_SILENCE_END_MS` | `700` | 静音结束窗口 |
| `REALTIME_VOICE_VAD_MAX_SEGMENT_MS` | `15000` | 单段上限（强制切段） |
| `REALTIME_VOICE_PARTIAL_TRANSCRIPT_INTERVAL_MS` | `800` | 中途转写节流（按已积累语音时长；0 = 关闭） |
| `REALTIME_VOICE_PARTIAL_MIN_SPEECH_MS` | `600` | 中途转写最短已积累语音 |
| `REALTIME_VOICE_SENTENCE_MAX_CHARS` | `80` | 助手单句字符上限（TTS 单次请求上限 500 以内） |
| `REALTIME_VOICE_TTS_CHUNK_BYTES` | `16384` | 下行音频分片字节数 |
| `REALTIME_VOICE_AUDIO_QUEUE_MAX_CHUNKS` | `64` | 音频入队上限（溢出丢最旧） |
| `REALTIME_VOICE_AUDIO_BATCH_MAX_CHUNKS` | `8` | 单次批量出队上限（须 ≤ 队列上限） |
| `REALTIME_VOICE_MAX_SESSION_MS` | `600000` | 单会话最长时长 |
| `REALTIME_VOICE_IDLE_TIMEOUT_MS` | `60000` | 空闲超时（须短于最大时长） |
| `REALTIME_VOICE_HEARTBEAT_INTERVAL_MS` | `15000` | 心跳建议间隔 |
| `REALTIME_VOICE_BARGE_IN_ENABLED` | `true` | 打断开关 |

TTS 音色 / 模型沿用既有 `VOICE_TTS_*`（人格 `voice_profile` 绑定生效），ASR 语言沿用 `VOICE_ASR_LANGUAGE`，本分节不重复。

`.env` 片段（模板由 `pixi run gen-examples` 从配置模型生成，键面以 `backend/.env.example` 为真源）：

```dotenv
REALTIME_VOICE_ENABLED=true
REALTIME_VOICE_SAMPLE_RATE_HZ=16000
REALTIME_VOICE_CHANNELS=1
REALTIME_VOICE_VAD_SPEECH_RMS_THRESHOLD=0.06
REALTIME_VOICE_VAD_SILENCE_RMS_THRESHOLD=0.03
REALTIME_VOICE_VAD_SILENCE_END_MS=700
REALTIME_VOICE_BARGE_IN_ENABLED=true
VOICE_ASR_ENABLED=true
VOICE_TTS_ENABLED=true
```

加载期不变式（半生效配置直接拒绝启动）：断句阈值必须低于起说阈值；批量出队不得超过队列上限；空闲超时必须短于最大时长；单段上限必须大于最短语音；中途转写起点不得晚于单段上限。

## 前端面板（`frontend/src/pages/realtime/RealtimeVoicePage.tsx`）

路由 `/realtime`（侧栏「实时语音」）。功能：开始 / 停止、麦克风采集（16kHz PCM 分块上行）、流式播放下行（句级聚合解码串行播放）、实时字幕（partial / final / 助手增量与定稿）、打断按钮、状态徽章与错误提示、会话参数展示。注入面（`createClient` / `createCapture` / `createPlayback`）供测试替身装配；无 Web Audio 环境降级为仅字幕。

协议地址经 `buildRealtimeVoiceUrl` 构造（http→ws / https→wss，携带已保存 owner token 与可选 `persona_id`）。

## 部署侧一次性真机验证（开发机未闭环项）

单元测试全部经 fake ASR / TTS / 网关与伪时钟（零真实网络、零真实等待）。真实供应商连通性需在部署机执行一次：

1. 起 CosyVoice 包装服务与 Qwen3-ASR 服务（见 [voice.md](voice.md) 部署契约）；
2. 配置 `VOICE_TTS_ENABLED=true` / `VOICE_ASR_ENABLED=true` / `REALTIME_VOICE_ENABLED=true` 与端点；
3. 浏览器打开 `/realtime`，说一句短句：确认 `final_transcript` 文本合理、回复音频可播放；
4. 播放中再说一句：确认 `interrupted` 到达、播放立即停止、新语段被转写；
5. 记录 ASR 首字延迟 / TTS 首音延迟，按需调 `REALTIME_VOICE_VAD_*` 与 `REALTIME_VOICE_PARTIAL_TRANSCRIPT_INTERVAL_MS`。

## 测试面

`src/tests/unit/test_realtime_{models,vad,state,sentence,audio,session}.py` + `test_app_realtime_voice.py`（共享 fake 基建 `src/tests/realtime_fakes.py`）：

- 协议：全部上行类型解析 / 非法载荷拒绝 / 单块 64KiB 上限边界（恰好通过 / 超限拒绝 / 超限后会话继续）/ 下行联合序列化往返 / 配置不变式与 env 前缀；
- VAD：单帧不触发 / 防抖起说 / 静音切段 / 短语音丢弃 / 句中停顿不切分 / 单段强制 / 冲刷 / 多段序列；
- 状态机：合法边矩阵穷举 / 关闭事件全相位 / 非法边 / 吸收态 / 规则唯一性；
- 句切与音频：标点保留 / 收尾符号 / 长度上限与停顿优先 / 硬切 / 流式累积；能量 / WAV 容器 / 分片拼接 / 时长；
- 会话端到端：ready 字段、完整轮次（转写 → 文本 → 音频 → 回聆听）、中途转写节流、强制切段、手动 / VAD 打断（任务集合清空、无泄漏）、打断关闭态、转写任务挂起时 VAD 打断仍生效、关闭取消挂起转写、背压丢最旧块、超限音频块拒绝后会话继续、空闲 / 最大时长、end 尾段冲刷、end 排空被关闭事件抢占、关闭 wire 顺序（state(closed) → closed）、ASR / 生成 / TTS 失败可恢复、TTS 关闭降级、传输断开、心跳应答、协议对称性、真实 `ChatService` 适配；
- 端点：路由挂载、总闸 / 鉴权 / 人格解析拒绝面、真实人格与聊天会话落库、传输适配（文本 / 二进制 / 断开 / 幂等关闭）。

前端：`api/realtimeVoice.test.ts`、`lib/realtimeAudio.test.ts`、`lib/realtimeVoiceClient.test.ts`、`pages/realtime/RealtimeVoicePage.test.tsx`。

## 边界（未做项，见 LEDGER §6）

心流门控仅作用于通道入站；实时语音直连 `ChatService.stream_reply`，不经门控裁决。

转写任务化：切段后转写经独立任务执行，音频工人只投递——转写窗口内 VAD、起说与打断判定持续工作，不会因 ASR 在途而停摆。多段转写可并行，转写结果按完成序进入生成队列（不按语段序号重排）；因此同一说话人两段近似同时完成时，先完成者先回复，客户端应据 `segment_id` 对齐字幕与轮次。

- **AEC / 回声消除**：未实现；外放场景可能自触发打断，建议耳机或浏览器平台 AEC。
- **移动端优化**：未做（ScriptProcessor 采集、下行 WAV 解码）；移动浏览器后台切走时连接可能被系统挂起。
- **ASR / TTS 真流式**：既有服务端点无流式通道；partial 转写来自节流中途批量调用，TTS 音频为整句 WAV 分片（客户端整句解码后播放，句内不可边收边播）。
- **真实供应商验证**：部署侧一次性执行（见上节）。
- **工具调用**：实时语音轮次不注入工具面（低延迟优先）；需要工具（搜索 / 沙盒）的场景走文字通道。
- **合规强化重试**：流式路径沿用既有「流式不重试」边界（助手腔检测与重试只发生在非流式 `reply`）。
