/**
 * 实时语音协议 wire 类型（与后端 `harness/voice/realtime_models` 字段一一对应）。
 *
 * 上行：start / audio_chunk（PCM16 base64）/ end / barge_in / barge_in_ack / ping。
 * 下行：ready / partial_transcript / final_transcript / assistant_text /
 * audio_chunk（WAV base64 分片）/ state / interrupted / error / closed。
 *
 * 约定：时间字段统一 epoch 毫秒；下行按 `type` 判别；页面只消费下行
 * 联合类型，上行消息经 `RealtimeVoiceClient` 构造（本文件不承载发送逻辑）。
 */

/** 会话相位（与后端状态机同值域）。 */
export type RealtimeSessionPhase = 'idle' | 'listening' | 'thinking' | 'speaking' | 'closed'

/** 会话关闭原因（与后端值域一致）。 */
export type RealtimeCloseReason =
  | 'client_end'
  | 'idle_timeout'
  | 'max_duration'
  | 'session_rejected'
  | 'internal_error'
  | 'transport_closed'

/** 打断来源（VAD 检测 / 客户端手动按钮）。 */
export type RealtimeBargeInReason = 'vad' | 'manual'

/** ready 消息内公开的 VAD 参数。 */
export interface RealtimeVadSettings {
  speech_rms_threshold: number
  silence_rms_threshold: number
  min_speech_ms: number
  silence_end_ms: number
  max_segment_ms: number
}

/** ready 消息内公开的会话资源上限。 */
export interface RealtimeSessionLimits {
  max_session_ms: number
  idle_timeout_ms: number
  heartbeat_interval_ms: number
  audio_queue_max_chunks: number
  sentence_max_chars: number
}

/** 下行 ready——会话建立成功。 */
export interface RealtimeReadyMessage {
  type: 'ready'
  ts_ms: number
  protocol_version: string
  session_id: string
  sample_rate_hz: number
  channels: number
  audio_format: 'wav'
  asr_enabled: boolean
  tts_enabled: boolean
  barge_in_enabled: boolean
  /** 服务端 AEC 期望（客户端采集须开启回声消除；false = 调试降级公告）。 */
  aec: boolean
  /** 流式识别通道可用性（true = partial 为真增量）。 */
  streaming_asr: boolean
  /** 流式合成通道可用性（true = 逐句流式合成）。 */
  streaming_tts: boolean
  vad: RealtimeVadSettings
  limits: RealtimeSessionLimits
}

/** 下行 partial_transcript——进行中语段的节流转写。 */
export interface RealtimePartialTranscriptMessage {
  type: 'partial_transcript'
  ts_ms: number
  segment_id: string
  text: string
}

/** 下行 final_transcript——语段最终转写。 */
export interface RealtimeFinalTranscriptMessage {
  type: 'final_transcript'
  ts_ms: number
  segment_id: string
  text: string
  duration_ms: number
  language: string | null
}

/** 下行 assistant_text——人格回复文本增量 / 整轮全文。 */
export interface RealtimeAssistantTextMessage {
  type: 'assistant_text'
  ts_ms: number
  turn_id: string
  text: string
  final: boolean
}

/** 下行 audio_chunk——句级 TTS 音频分片。 */
export interface RealtimeAudioChunkMessage {
  type: 'audio_chunk'
  ts_ms: number
  turn_id: string
  sentence_index: number
  seq: number
  total_chunks: number
  content_type: 'audio/wav'
  data: string
}

/** 下行 state——会话相位（同时作为心跳应答）。 */
export interface RealtimeStateMessage {
  type: 'state'
  ts_ms: number
  state: RealtimeSessionPhase
  detail: string | null
}

/** 下行 interrupted——打断已生效。 */
export interface RealtimeInterruptedMessage {
  type: 'interrupted'
  ts_ms: number
  interruption_id: string
  turn_id: string | null
  reason: RealtimeBargeInReason
}

/** 下行 error——可恢复 / 不可恢复错误。 */
export interface RealtimeErrorMessage {
  type: 'error'
  ts_ms: number
  code: string
  message: string
  recoverable: boolean
}

/** 下行 closed——会话关闭。 */
export interface RealtimeClosedMessage {
  type: 'closed'
  ts_ms: number
  reason: RealtimeCloseReason
  detail: string | null
}

/** 下行消息联合（按 type 判别）。 */
export type RealtimeDownlinkMessage =
  | RealtimeReadyMessage
  | RealtimePartialTranscriptMessage
  | RealtimeFinalTranscriptMessage
  | RealtimeAssistantTextMessage
  | RealtimeAudioChunkMessage
  | RealtimeStateMessage
  | RealtimeInterruptedMessage
  | RealtimeErrorMessage
  | RealtimeClosedMessage

/** 上行消息联合（客户端 → 服务端；由 `RealtimeVoiceClient` 构造）。 */
export type RealtimeUplinkMessage =
  | {
      type: 'start'
      ts_ms: number
      persona_id?: number
      sample_rate_hz?: number
      channels?: number
    }
  | { type: 'audio_chunk'; ts_ms: number; seq: number; data: string }
  | { type: 'end'; ts_ms: number }
  | { type: 'barge_in'; ts_ms: number; reason: RealtimeBargeInReason }
  | { type: 'barge_in_ack'; ts_ms: number; interruption_id: string }
  | { type: 'ping'; ts_ms: number }

/** 页面字幕条目（用户转写 / 助手回复；final 标记是否已定稿）。 */
export interface RealtimeTranscriptEntry {
  /** 条目归属（用户语音 / 助手文本）。 */
  role: 'user' | 'assistant'
  /** 语段 / 轮次标识（增量按该键聚合）。 */
  key: string
  /** 文本内容。 */
  text: string
  /** 是否已定稿。 */
  final: boolean
}

/** 面板状态（连接生命周期 + 服务端相位合流后的展示态）。 */
export type RealtimePanelStatus =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'closed'
  | 'error'
