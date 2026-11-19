/**
 * 实时语音浏览器客户端——WS 生命周期（上行消息 / 心跳）与麦克风采集。
 *
 * 职责边界：
 * - `RealtimeVoiceClient`：连接、start / audio_chunk / end / barge_in /
 *   barge_in_ack / ping 上行，下行 JSON 解析后交给页面回调；收到服务端
 *   `closed` 即关闭套接字并停止心跳。socketFactory 可注入（测试零网络）。
 * - `MicrophoneCapture`：getUserMedia → AudioContext → ScriptProcessor 采集，
 *   降采样 16kHz、100ms 成帧、base64 上行；采集上下文 / getUserMedia 均可
 *   注入（jsdom 无 Web Audio 时页面注入 fake 或降级报错）。
 *
 * 失败语义：环境不支持 Web Audio / 麦克风被拒 → `onError` 回调中文提示，
 * 页面保留会话并提示用户（不静默）。
 */

import { PcmFrameChunker, TARGET_SAMPLE_RATE, downsampleTo16k, pcm16ToBase64 } from '@/lib/realtimeAudio'
import type { RealtimeDownlinkMessage, RealtimeUplinkMessage } from '@/types/realtimeVoice'

/** 套接字就绪态常量（与浏览器 WebSocket.OPEN 同值）。 */
export const SOCKET_OPEN = 1

/** 套接字连接态。 */
export type RealtimeSocketState = 'connecting' | 'open' | 'closed' | 'error'

/** 上行 WS 套接字最小契约（浏览器 WebSocket 结构子集）。 */
export interface RealtimeSocketLike {
  readyState: number
  send(data: string): void
  close(): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onerror: ((event: unknown) => void) | null
  onclose: ((event: unknown) => void) | null
}

/** 页面消费的下行 / 连接态回调。 */
export interface RealtimeClientHandlers {
  onMessage: (message: RealtimeDownlinkMessage) => void
  onSocketState: (state: RealtimeSocketState) => void
  onError?: (message: string) => void
}

/** 客户端注入项（测试 / 页面默认构造）。 */
export interface RealtimeVoiceClientOptions {
  url: string
  handlers: RealtimeClientHandlers
  personaId?: number | null
  socketFactory?: (url: string) => RealtimeSocketLike
  now?: () => number
  setIntervalFn?: (handler: () => void, timeout: number) => unknown
  clearIntervalFn?: (handle: unknown) => void
}

/** 页面依赖的客户端能力（fake 可满足同一契约）。 */
export interface RealtimeVoiceClientLike {
  connect(): void
  sendAudioFrame(base64: string): void
  end(): void
  bargeIn(): void
  bargeInAck(interruptionId: string): void
  close(): void
}

/** 心跳定时器句柄包装（可注入定时面——测试无需真实计时）。 */
interface HeartbeatHandle {
  cancel: () => void
}

/**
 * 实时语音 WS 客户端（协议上行 + 下行解析 + 心跳）。
 *
 * Args 见 `RealtimeVoiceClientOptions`；`connect` 后按浏览器事件顺序推进，
 * 全部上行消息带 epoch 毫秒时间戳（`now` 可注入）。
 */
export class RealtimeVoiceClient implements RealtimeVoiceClientLike {
  private readonly options: RealtimeVoiceClientOptions
  private readonly now: () => number
  private socket: RealtimeSocketLike | null = null
  private heartbeat: HeartbeatHandle | null = null
  private sequence = 0
  private closed = false

  constructor(options: RealtimeVoiceClientOptions) {
    this.options = options
    this.now = options.now ?? (() => Date.now())
  }

  /** 建立连接（open 后发送 start）。 */
  connect(): void {
    const factory =
      this.options.socketFactory ?? ((url: string) => new WebSocket(url) as RealtimeSocketLike)
    this.options.handlers.onSocketState('connecting')
    const socket = factory(this.options.url)
    this.socket = socket
    socket.onopen = () => {
      this.options.handlers.onSocketState('open')
      const message: RealtimeUplinkMessage = {
        type: 'start',
        ts_ms: this.now(),
        ...(typeof this.options.personaId === 'number'
          ? { persona_id: this.options.personaId }
          : {}),
        sample_rate_hz: TARGET_SAMPLE_RATE,
        channels: 1,
      }
      this.send(message)
    }
    socket.onmessage = (event) => {
      this.handleIncoming(event.data)
    }
    socket.onerror = () => {
      this.options.handlers.onSocketState('error')
      this.options.handlers.onError?.('实时语音连接异常，请检查服务端状态')
    }
    socket.onclose = () => {
      this.stopHeartbeat()
      this.options.handlers.onSocketState('closed')
    }
  }

  /**
   * 上行一帧 PCM16 base64（未连接时静默丢弃——采集先于连接就绪的边界）。
   *
   * @param base64 PCM16 base64 载荷。
   */
  sendAudioFrame(base64: string): void {
    if (this.closed) {
      return
    }
    const message: RealtimeUplinkMessage = {
      type: 'audio_chunk',
      ts_ms: this.now(),
      seq: this.sequence,
      data: base64,
    }
    this.sequence += 1
    this.send(message)
  }

  /** 请求服务端结束会话（服务端回 closed 后连接随之下线）。 */
  end(): void {
    this.send({ type: 'end', ts_ms: this.now() })
  }

  /** 手动打断（服务端回 interrupted + state）。 */
  bargeIn(): void {
    this.send({ type: 'barge_in', ts_ms: this.now(), reason: 'manual' })
  }

  /**
   * 回执「本地播放已清空」。
   *
   * @param interruptionId 对应 interrupted 消息的标识。
   */
  bargeInAck(interruptionId: string): void {
    this.send({ type: 'barge_in_ack', ts_ms: this.now(), interruption_id: interruptionId })
  }

  /** 主动关闭套接字（页面卸载 / 停止）。 */
  close(): void {
    this.closed = true
    this.stopHeartbeat()
    const socket = this.socket
    this.socket = null
    if (socket !== null) {
      socket.onclose = null
      socket.onerror = null
      socket.onmessage = null
      try {
        socket.close()
      } catch {
        // 已关闭的套接字重复 close 会抛错——按幂等处理。
      }
    }
  }

  private send(message: RealtimeUplinkMessage): void {
    const socket = this.socket
    if (socket === null || socket.readyState !== SOCKET_OPEN) {
      return
    }
    socket.send(JSON.stringify(message))
  }

  private handleIncoming(data: unknown): void {
    if (typeof data !== 'string') {
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(data)
    } catch {
      return
    }
    if (typeof parsed !== 'object' || parsed === null) {
      return
    }
    const message = parsed as RealtimeDownlinkMessage
    if (typeof message.type !== 'string') {
      return
    }
    if (message.type === 'ready') {
      this.startHeartbeat(message.limits.heartbeat_interval_ms)
    }
    this.options.handlers.onMessage(message)
    if (message.type === 'closed') {
      this.close()
    }
  }

  private startHeartbeat(intervalMs: number): void {
    this.stopHeartbeat()
    if (intervalMs <= 0) {
      return
    }
    const setIntervalFn =
      this.options.setIntervalFn ??
      ((handler: () => void, timeout: number) => globalThis.setInterval(handler, timeout))
    const clearIntervalFn =
      this.options.clearIntervalFn ??
      ((handle: unknown) => globalThis.clearInterval(handle as ReturnType<typeof setInterval>))
    const handle = setIntervalFn(() => {
      this.send({ type: 'ping', ts_ms: this.now() })
    }, intervalMs)
    this.heartbeat = { cancel: () => clearIntervalFn(handle) }
  }

  private stopHeartbeat(): void {
    this.heartbeat?.cancel()
    this.heartbeat = null
  }
}

/** 音频上下文采集面最小契约（Web Audio + getUserMedia 的结构子集）。 */
export interface RealtimeCaptureContextLike {
  sampleRate: number
  destination: unknown
  createMediaStreamSource(stream: RealtimeMediaStreamLike): {
    connect(target: unknown): void
    disconnect(): void
  }
  createScriptProcessor(
    bufferSize: number,
    inputChannels: number,
    outputChannels: number,
  ): RealtimeScriptProcessorLike
  createGain?(): { gain: { value: number }; connect(target: unknown): void }
  resume?(): Promise<void>
  close?(): Promise<void>
}

/** 麦克风媒体流最小契约。 */
export interface RealtimeMediaStreamLike {
  getTracks(): Array<{ stop(): void }>
}

/** ScriptProcessor 节点最小契约。 */
export interface RealtimeScriptProcessorLike {
  onaudioprocess:
    | ((event: { inputBuffer: { getChannelData(channel: number): Float32Array } }) => void)
    | null
  connect(target: unknown): void
  disconnect(): void
}

/** 采集注入项（测试以 fake 上下文 / fake getUserMedia 驱动）。 */
export interface MicrophoneCaptureOptions {
  onFrameBase64: (payload: string) => void
  onError?: (message: string) => void
  audioContextFactory?: () => RealtimeCaptureContextLike
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<RealtimeMediaStreamLike>
  frameSamples?: number
}

/** 页面依赖的采集能力（fake 可满足同一契约）。 */
export interface MicrophoneCaptureLike {
  start(): Promise<void>
  stop(): Promise<void>
}

/**
 * 麦克风采集器（16kHz 单声道 100ms 帧 base64 回调）。
 *
 * Args 见 `MicrophoneCaptureOptions`；`start` 建流并挂采集回调，`stop`
 * 断开节点、停轨、关上下文（幂等——未启动时 stop 为空操作）。
 */
export class MicrophoneCapture implements MicrophoneCaptureLike {
  private readonly options: MicrophoneCaptureOptions
  private context: RealtimeCaptureContextLike | null = null
  private stream: RealtimeMediaStreamLike | null = null
  private processor: RealtimeScriptProcessorLike | null = null
  private sourceNode: {
    connect(target: unknown): void
    disconnect(): void
  } | null = null
  private chunker: PcmFrameChunker | null = null

  constructor(options: MicrophoneCaptureOptions) {
    this.options = options
  }

  /** 启动采集（权限被拒 / 环境不支持 → onError + 不抛）。 */
  async start(): Promise<void> {
    if (this.context !== null) {
      return
    }
    try {
      const getUserMedia =
        this.options.getUserMedia ??
        ((constraints: MediaStreamConstraints) => {
          const mediaDevices = (navigator as unknown as { mediaDevices?: MediaDevices })
            .mediaDevices
          if (mediaDevices === undefined) {
            throw new Error('当前环境不支持麦克风采集（navigator.mediaDevices 不可用）')
          }
          return mediaDevices.getUserMedia(constraints)
        })
      const audioContextFactory =
        this.options.audioContextFactory ??
        (() => {
          const Context = (window as Window & { AudioContext?: typeof AudioContext })
            .AudioContext
          if (Context === undefined) {
            throw new Error('当前环境不支持 Web Audio（AudioContext 不可用）')
          }
          return new Context() as unknown as RealtimeCaptureContextLike
        })
      const stream = await getUserMedia({ audio: true })
      const context = audioContextFactory()
      await context.resume?.()
      const sourceNode = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      const chunker = new PcmFrameChunker((pcm) => {
        this.options.onFrameBase64(pcm16ToBase64(pcm))
      }, this.options.frameSamples)
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0)
        const resampled = downsampleTo16k(input, context.sampleRate)
        chunker.push(resampled)
      }
      if (context.createGain !== undefined) {
        const silent = context.createGain()
        silent.gain.value = 0
        processor.connect(silent)
        silent.connect(context.destination)
      } else {
        processor.connect(context.destination)
      }
      sourceNode.connect(processor)
      this.context = context
      this.stream = stream
      this.processor = processor
      this.sourceNode = sourceNode
      this.chunker = chunker
    } catch (error) {
      this.options.onError?.(
        error instanceof Error ? error.message : '麦克风采集启动失败',
      )
    }
  }

  /** 停止采集（幂等）。 */
  async stop(): Promise<void> {
    this.chunker?.flush()
    this.processor?.disconnect()
    this.sourceNode?.disconnect()
    for (const track of this.stream?.getTracks() ?? []) {
      track.stop()
    }
    await this.context?.close?.()
    this.context = null
    this.stream = null
    this.processor = null
    this.sourceNode = null
    this.chunker = null
  }
}
