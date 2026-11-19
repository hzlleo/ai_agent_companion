/**
 * 实时语音前端音频工具——麦克风 PCM 编码与下行句级音频播放队列。
 *
 * 边界（与后端同口径，不新增依赖）：
 * - 上行：AudioContext 采集的 Float32 采样先降采样到 16kHz、再转 16 位小端
 *   PCM、按 100ms 成帧、base64 编码后经 WS 上行——纯 TypedArray 运算；
 * - 下行：TTS 音频是逐句 WAV 字节流分片（`audio_chunk`），本模块按
 *   「轮次 + 句序」聚合分片，收齐后 `decodeAudioData` 解码并**串行排期**
 *   播放（句间不重叠）；`interrupt` 立即停止在播与排队音频并作废在途解码
 *   ——打断语义的安全边界在前端这一侧闭合。
 *
 * 播放/解码依赖浏览器 Web Audio（`AudioContextLike` 为最小结构契约），
 * 测试注入 fake 上下文即可覆盖全部排期与打断分支。
 */

import type { RealtimeAudioChunkMessage } from '@/types/realtimeVoice'

/** 上行目标采样率（与后端 `REALTIME_VOICE_SAMPLE_RATE_HZ` 缺省一致）。 */
export const TARGET_SAMPLE_RATE = 16000

/** 上行帧时长（毫秒——每帧独立 base64 上行）。 */
export const FRAME_DURATION_MS = 100

/** 单帧采样点数（16kHz × 100ms）。 */
export const FRAME_SAMPLES = (TARGET_SAMPLE_RATE * FRAME_DURATION_MS) / 1000

/** 播放侧最小音频缓冲契约（Web Audio `AudioBuffer` 的结构子集）。 */
export interface RealtimeAudioBufferLike {
  /** 缓冲时长（秒——串行排期依据）。 */
  duration: number
}

/** 播放侧最小音源契约（Web Audio `AudioBufferSourceNode` 的结构子集）。 */
export interface RealtimeAudioSourceLike {
  /** 绑定的音频缓冲。 */
  buffer: RealtimeAudioBufferLike | null
  /** 播放结束回调。 */
  onended: ((event: Event) => void) | null
  /** 连接到目标节点。 */
  connect(target: unknown): void
  /** 在指定时刻开始播放。 */
  start(when?: number): void
  /** 立即停止。 */
  stop(): void
}

/** 播放侧最小音频上下文契约（Web Audio `AudioContext` 的结构子集）。 */
export interface RealtimeAudioContextLike {
  /** 当前播放时钟（秒）。 */
  currentTime: number
  /** 输出目标。 */
  destination: unknown
  /** 解码容器音频字节。 */
  decodeAudioData(data: ArrayBuffer): Promise<RealtimeAudioBufferLike>
  /** 创建音源节点。 */
  createBufferSource(): RealtimeAudioSourceLike
}

/**
 * 按整数倍降采样到目标采样率（块平均——朴素抗混叠，移动端开销可接受）。
 *
 * @param samples 原始采样（Float32，[-1, 1]）。
 * @param inputRate 输入采样率（Hz）。
 * @param targetRate 目标采样率（Hz，缺省 16kHz）。
 * @returns 降采样后的采样；输入采样率不高于目标时返回副本。
 */
export function downsampleTo16k(
  samples: Float32Array,
  inputRate: number,
  targetRate: number = TARGET_SAMPLE_RATE,
): Float32Array {
  if (inputRate <= 0 || targetRate <= 0) {
    return new Float32Array(0)
  }
  if (inputRate <= targetRate) {
    return new Float32Array(samples)
  }
  const ratio = inputRate / targetRate
  const outputLength = Math.floor(samples.length / ratio)
  const output = new Float32Array(outputLength)
  const block = Math.max(1, Math.round(ratio))
  for (let index = 0; index < outputLength; index += 1) {
    const start = Math.floor(index * ratio)
    const end = Math.min(samples.length, start + block)
    let sum = 0
    for (let cursor = start; cursor < end; cursor += 1) {
      sum += samples[cursor] ?? 0
    }
    output[index] = end > start ? sum / (end - start) : 0
  }
  return output
}

/**
 * Float32 采样转 16 位小端 PCM（超量程削波——不产生回绕噪声）。
 *
 * @param samples 归一化采样（[-1, 1]）。
 * @returns Int16 采样。
 */
export function floatToPcm16(samples: Float32Array): Int16Array {
  const pcm = new Int16Array(samples.length)
  for (let index = 0; index < samples.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[index] ?? 0))
    pcm[index] = Math.round(clamped * 32767)
  }
  return pcm
}

/**
 * Int16 采样 base64 编码（小端字节序——与后端 PCM16 解码同口径）。
 *
 * @param pcm Int16 采样。
 * @returns base64 字符串。
 */
export function pcm16ToBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.length * 2)
  for (let index = 0; index < pcm.length; index += 1) {
    const sample = (pcm[index] ?? 0) & 0xffff
    bytes[index * 2] = sample & 0xff
    bytes[index * 2 + 1] = (sample >> 8) & 0xff
  }
  let binary = ''
  const chunkSize = 0x8000
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}

/**
 * 帧缓冲切分器：把连续 16kHz 采样按固定帧长切出并回调。
 *
 * Args 见构造；`push` 接受任意长度采样（内部缓存），`flush` 冲出不完整尾帧
 * （仅在停止采集时使用——正常运行每帧都是满帧）。
 */
export class PcmFrameChunker {
  private readonly frameSamples: number
  private buffer: Float32Array = new Float32Array(0)

  /**
   * @param onFrame 满帧回调（Int16 PCM）。
   * @param frameSamples 单帧采样点数（缺省 1600 = 100ms @16kHz）。
   */
  constructor(
    private readonly onFrame: (pcm: Int16Array) => void,
    frameSamples: number = FRAME_SAMPLES,
  ) {
    this.frameSamples = frameSamples
  }

  /**
   * 追加采样（长度任意，内部成帧后逐帧回调）。
   *
   * @param samples 16kHz 采样。
   */
  push(samples: Float32Array): void {
    if (samples.length === 0) {
      return
    }
    const merged = new Float32Array(this.buffer.length + samples.length)
    merged.set(this.buffer, 0)
    merged.set(samples, this.buffer.length)
    let offset = 0
    while (merged.length - offset >= this.frameSamples) {
      const frame = merged.subarray(offset, offset + this.frameSamples)
      this.onFrame(floatToPcm16(frame))
      offset += this.frameSamples
    }
    this.buffer = merged.slice(offset)
  }

  /** 冲出不完整尾帧（不足一帧时丢弃——静音尾部无需上行）。 */
  flush(): void {
    if (this.buffer.length > 0) {
      this.onFrame(floatToPcm16(this.buffer))
      this.buffer = new Float32Array(0)
    }
  }
}

/** 句级音频分片聚合键（轮次 + 句序）。 */
function sentenceKey(turnId: string, sentenceIndex: number): string {
  return `${turnId}:${sentenceIndex}`
}

interface PendingSentence {
  total: number
  parts: Map<number, string>
  received: number
}

/**
 * 下行句级音频播放队列（收齐分片 → 解码 → 串行排期播放）。
 *
 * 语义（锁定）：
 * - 同一句的分片按 `seq` 聚合，收齐 `total_chunks` 才解码（半句不播）；
 * - 句间播放时刻串行累加（`currentTime + 前面缓冲总时长`），不重叠；
 * - `interrupt` 停止全部在播音源、清空聚合与排队，并使在途解码作废。
 */
export class RealtimePlaybackQueue {
  private readonly context: RealtimeAudioContextLike
  private readonly onError?: (error: Error) => void
  private pending = new Map<string, PendingSentence>()
  private sources = new Set<RealtimeAudioSourceLike>()
  private decodeChain: Promise<void> = Promise.resolve()
  private generation = 0
  private chainTime = 0

  /**
   * @param context Web Audio 上下文（结构契约）。
   * @param onError 解码 / 播放失败回调（缺省静默——音频缺失不阻断会话）。
   */
  constructor(context: RealtimeAudioContextLike, onError?: (error: Error) => void) {
    this.context = context
    this.onError = onError
  }

  /** 待收齐 / 待解码的句数（诊断与测试断言面）。 */
  get pendingSentences(): number {
    return this.pending.size
  }

  /**
   * 追加一个音频分片（同句分片收齐后自动解码排期）。
   *
   * @param chunk 下行 audio_chunk 消息。
   */
  push(chunk: RealtimeAudioChunkMessage): void {
    const key = sentenceKey(chunk.turn_id, chunk.sentence_index)
    const existing = this.pending.get(key) ?? {
      total: chunk.total_chunks,
      parts: new Map<number, string>(),
      received: 0,
    }
    if (existing.parts.has(chunk.seq)) {
      return
    }
    existing.parts.set(chunk.seq, chunk.data)
    existing.received += 1
    existing.total = chunk.total_chunks
    this.pending.set(key, existing)
    if (existing.received < existing.total) {
      return
    }
    this.pending.delete(key)
    const ordered: string[] = []
    for (let sequence = 0; sequence < existing.total; sequence += 1) {
      const part = existing.parts.get(sequence)
      if (part === undefined) {
        return
      }
      ordered.push(part)
    }
    this.enqueueDecode(ordered)
  }

  /** 打断：停止全部音源、清空在途聚合与排期时钟（在途解码作废）。 */
  interrupt(): void {
    this.generation += 1
    this.pending.clear()
    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        // 已停止的音源重复 stop 会抛错——打断路径按幂等处理。
      }
    }
    this.sources.clear()
    this.decodeChain = Promise.resolve()
    this.chainTime = 0
  }

  private enqueueDecode(parts: string[]): void {
    const generation = this.generation
    this.decodeChain = this.decodeChain
      .then(() => this.decodeAndSchedule(parts, generation))
      .catch((error: unknown) => {
        this.onError?.(error instanceof Error ? error : new Error(String(error)))
      })
  }

  private async decodeAndSchedule(parts: string[], generation: number): Promise<void> {
    const bytes = base64PartsToBytes(parts)
    const buffer = await this.context.decodeAudioData(bytes.buffer as ArrayBuffer)
    if (generation !== this.generation) {
      return
    }
    const source = this.context.createBufferSource()
    source.buffer = buffer
    source.connect(this.context.destination)
    const startAt = Math.max(this.context.currentTime, this.chainTime)
    this.chainTime = startAt + buffer.duration
    this.sources.add(source)
    source.onended = () => {
      this.sources.delete(source)
    }
    source.start(startAt)
  }
}

/**
 * base64 分片按序拼接为原始字节（WAV 容器字节）。
 *
 * @param parts 按 `seq` 排序的 base64 分片。
 * @returns 拼接后的字节。
 */
export function base64PartsToBytes(parts: string[]): Uint8Array {
  const chunks = parts.map((part) => base64ToBytes(part))
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const merged = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    merged.set(chunk, offset)
    offset += chunk.length
  }
  return merged
}

/**
 * base64 → 字节（atob 结果逐字符拆字节）。
 *
 * @param payload base64 字符串。
 * @returns 原始字节。
 */
export function base64ToBytes(payload: string): Uint8Array {
  const binary = atob(payload)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}
