/**
 * 实时语音浏览器客户端——WS 生命周期（上行消息 / 心跳）与麦克风采集。
 *
 * 职责边界：
 * - `RealtimeVoiceClient`：连接、start / audio_chunk / end / barge_in /
 *   barge_in_ack / ping 上行，下行 JSON 解析后交给页面回调；收到服务端
 *   `closed` 即关闭套接字并停止心跳。socketFactory 可注入（测试零网络）。
 * - `MicrophoneCapture`：getUserMedia（AEC / 降噪 / 自动增益全开并回读
 *   实际生效值）→ AudioWorklet 采集（加载失败回退 ScriptProcessor）→
 *   降采样 16kHz、100ms 成帧、base64 上行；采集上下文 / getUserMedia /
 *   worklet 模块 URL / 建节点工厂均可注入（测试零真实音频设备）。移动端
 *   后台切换经 `pause` / `resume` 暂停与恢复采集（WS 会话不重连）。
 *
 * 失败语义：环境不支持 Web Audio / 麦克风被拒 → `onError` 回调中文提示，
 * 页面保留会话并提示用户（不静默）。
 */

import {
  CAPTURE_WORKLET_NAME,
  PcmFrameChunker,
  TARGET_SAMPLE_RATE,
  downsampleTo16k,
  pcm16ToBase64,
} from '@/lib/realtimeAudio'
import captureWorkletModuleUrl from '@/lib/realtimeCaptureWorklet.ts?worker&url'
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
  audioWorklet?: { addModule(url: string): Promise<void> }
  createGain?(): { gain: { value: number }; connect(target: unknown): void }
  resume?(): Promise<void>
  close?(): Promise<void>
}

/** 音频轨道实测设置（getSettings 回读——能力是否真正生效）。 */
export interface RealtimeTrackSettingsLike {
  echoCancellation?: boolean
  noiseSuppression?: boolean
  autoGainControl?: boolean
}

/** 音频轨道最小契约（停止 + 可选实测设置回读）。 */
export interface RealtimeAudioTrackLike {
  stop(): void
  getSettings?(): RealtimeTrackSettingsLike
}

/** 麦克风媒体流最小契约。 */
export interface RealtimeMediaStreamLike {
  getTracks(): RealtimeAudioTrackLike[]
  getAudioTracks?(): RealtimeAudioTrackLike[]
}

/** ScriptProcessor 节点最小契约。 */
export interface RealtimeScriptProcessorLike {
  onaudioprocess:
    | ((event: { inputBuffer: { getChannelData(channel: number): Float32Array } }) => void)
    | null
  connect(target: unknown): void
  disconnect(): void
}

/** AudioWorklet 节点最小契约（port 回传定长采样块）。 */
export interface RealtimeWorkletNodeLike {
  port: { onmessage: ((event: { data: unknown }) => void) | null }
  connect(target: unknown): void
  disconnect(): void
}

/** 采集能力实测值（浏览器实际生效的约束——未回读字段按未生效处理）。 */
export interface RealtimeCaptureCapabilities {
  echoCancellation: boolean
  noiseSuppression: boolean
  autoGainControl: boolean
}

/** 采集约束（锁定决策：AEC / 降噪 / 自动增益全开；服务端公告 aec=false 时仍按此采集）。 */
export const REALTIME_AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
}

/** 采集注入项（测试以 fake 上下文 / fake getUserMedia 驱动）。 */
export interface MicrophoneCaptureOptions {
  onFrameBase64: (payload: string) => void
  onError?: (message: string) => void
  audioContextFactory?: () => RealtimeCaptureContextLike
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<RealtimeMediaStreamLike>
  frameSamples?: number
  workletModuleUrl?: string
  workletNodeFactory?: (context: RealtimeCaptureContextLike) => RealtimeWorkletNodeLike
}

/** 页面依赖的采集能力（fake 可满足同一契约）。 */
export interface MicrophoneCaptureLike {
  start(): Promise<void>
  stop(): Promise<void>
  pause?(): Promise<void>
  resume?(): Promise<void>
  getCapabilities?(): RealtimeCaptureCapabilities | null
  isPaused?(): boolean
}

/** 采集资源束（启动提交 / 停止释放的原子单元——迟到启动只清理自己的局部资源）。 */
interface CaptureResources {
  context: RealtimeCaptureContextLike | null
  stream: RealtimeMediaStreamLike | null
  processor: RealtimeScriptProcessorLike | null
  workletNode: RealtimeWorkletNodeLike | null
  sourceNode: {
    connect(target: unknown): void
    disconnect(): void
  } | null
  chunker: PcmFrameChunker | null
}

/** 读实际生效的音频约束（无 getSettings 的轨道按未生效处理）。 */
function readCaptureCapabilities(stream: RealtimeMediaStreamLike): RealtimeCaptureCapabilities {
  const tracks = stream.getAudioTracks?.() ?? stream.getTracks()
  const settings = tracks[0]?.getSettings?.() ?? {}
  return {
    echoCancellation: settings.echoCancellation === true,
    noiseSuppression: settings.noiseSuppression === true,
    autoGainControl: settings.autoGainControl === true,
  }
}

/**
 * 麦克风采集器（16kHz 单声道 100ms 帧 base64 回调）。
 *
 * 采集链路（锁定顺序）：`getUserMedia`（AEC / 降噪 / 自动增益全开并回读
 * 实际生效值）→ 优先 `AudioWorklet`（模块加载失败 / 建节点失败回退
 * `ScriptProcessor`）→ 降采样 16kHz → 100ms 成帧 → base64 回调。两条
 * 采集路径的输出语义一致（帧长 / 采样率 / 心跳不受影响）。
 *
 * 移动端边界：`pause` 断开节点并停止轨道（保留 WS 会话，不丢会话状态），
 * `resume` 重新申请媒体流恢复采集——后台切换后由页面在可见 / 触摸时调用。
 *
 * Args 见 `MicrophoneCaptureOptions`；`start` 建流并挂采集回调，`stop`
 * 断开节点、停轨、关上下文（幂等——未启动时 stop 为空操作）。
 */
export class MicrophoneCapture implements MicrophoneCaptureLike {
  private readonly options: MicrophoneCaptureOptions
  private context: RealtimeCaptureContextLike | null = null
  private stream: RealtimeMediaStreamLike | null = null
  private processor: RealtimeScriptProcessorLike | null = null
  private workletNode: RealtimeWorkletNodeLike | null = null
  private sourceNode: {
    connect(target: unknown): void
    disconnect(): void
  } | null = null
  private chunker: PcmFrameChunker | null = null
  private capabilities: RealtimeCaptureCapabilities | null = null
  private paused = false
  private generation = 0
  private pendingStop = false
  private starting: { generation: number; task: Promise<void> } | null = null

  constructor(options: MicrophoneCaptureOptions) {
    this.options = options
  }

  /** 启动采集（权限被拒 / 环境不支持 → onError + 不抛；同世代在途调用复用同一链路）。 */
  async start(): Promise<void> {
    if (this.context !== null) {
      return
    }
    this.pendingStop = false
    const inflight = this.starting
    if (inflight !== null && inflight.generation === this.generation) {
      await inflight.task
      return
    }
    const generation = this.generation
    const task = this.startInternal(generation)
    const record = { generation, task }
    this.starting = record
    try {
      await task
    } finally {
      if (this.starting === record) {
        this.starting = null
      }
    }
  }

  private async startInternal(generation: number): Promise<void> {
    let stream: RealtimeMediaStreamLike | null = null
    let context: RealtimeCaptureContextLike | null = null
    let processor: RealtimeScriptProcessorLike | null = null
    let workletNode: RealtimeWorkletNodeLike | null = null
    let sourceNode: {
      connect(target: unknown): void
      disconnect(): void
    } | null = null
    let chunker: PcmFrameChunker | null = null
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
      const mediaStream = await getUserMedia(REALTIME_AUDIO_CONSTRAINTS)
      stream = mediaStream
      if (this.isStale(generation)) {
        await this.releaseResources({
          context: null,
          stream,
          processor: null,
          workletNode: null,
          sourceNode: null,
          chunker: null,
        })
        return
      }
      const capabilities = readCaptureCapabilities(mediaStream)
      const audioContext = audioContextFactory()
      context = audioContext
      await audioContext.resume?.()
      if (this.isStale(generation)) {
        await this.releaseResources({
          context,
          stream,
          processor: null,
          workletNode: null,
          sourceNode: null,
          chunker: null,
        })
        return
      }
      const mediaSource = audioContext.createMediaStreamSource(mediaStream)
      sourceNode = mediaSource
      const frameChunker = new PcmFrameChunker((pcm) => {
        this.options.onFrameBase64(pcm16ToBase64(pcm))
      }, this.options.frameSamples)
      chunker = frameChunker
      const node = await this.tryStartWorklet(audioContext, frameChunker, generation)
      workletNode = node
      if (this.isStale(generation)) {
        await this.releaseResources({
          context,
          stream,
          processor: null,
          workletNode,
          sourceNode,
          chunker,
        })
        return
      }
      if (node === null) {
        const scriptProcessor = audioContext.createScriptProcessor(4096, 1, 1)
        scriptProcessor.onaudioprocess = (event) => {
          const input = event.inputBuffer.getChannelData(0)
          const resampled = downsampleTo16k(input, audioContext.sampleRate)
          frameChunker.push(resampled)
        }
        processor = scriptProcessor
      }
      this.connectOutput(audioContext, mediaSource, node, processor)
      this.context = audioContext
      this.stream = mediaStream
      this.processor = processor
      this.workletNode = node
      this.sourceNode = mediaSource
      this.chunker = frameChunker
      this.capabilities = capabilities
      this.paused = false
    } catch (error) {
      await this.releaseResources({ context, stream, processor, workletNode, sourceNode, chunker })
      if (generation === this.generation) {
        this.options.onError?.(
          error instanceof Error ? error.message : '麦克风采集启动失败',
        )
      }
    }
  }

  /** 停止采集（幂等；启动在途时等其迟到清理完成后再返回）。 */
  async stop(): Promise<void> {
    this.generation += 1
    this.pendingStop = true
    const inflight = this.starting
    const resources = this.takeResources()
    this.capabilities = null
    this.paused = false
    try {
      if (inflight !== null) {
        await inflight.task
      }
      await this.releaseResources(resources)
    } finally {
      this.pendingStop = false
    }
  }

  /** 暂停采集（移动端后台切换——保留 WS 会话与页面状态）。 */
  async pause(): Promise<void> {
    this.generation += 1
    const inflight = this.starting
    const resources = this.takeResources()
    this.paused = true
    if (inflight !== null) {
      await inflight.task
    }
    await this.releaseResources(resources)
  }

  /** 恢复采集（重新申请媒体流；会话不重连、不丢状态；失败保持暂停态）。 */
  async resume(): Promise<void> {
    if (this.context !== null) {
      this.paused = false
      return
    }
    await this.start()
  }

  /** 实际生效的采集能力（启动成功后可用；未回读字段为 false）。 */
  getCapabilities(): RealtimeCaptureCapabilities | null {
    return this.capabilities
  }

  /** 是否处于暂停态（页面展示恢复提示用）。 */
  isPaused(): boolean {
    return this.paused
  }

  private isStale(generation: number): boolean {
    return generation !== this.generation || this.pendingStop
  }

  private async tryStartWorklet(
    context: RealtimeCaptureContextLike,
    chunker: PcmFrameChunker,
    generation: number,
  ): Promise<RealtimeWorkletNodeLike | null> {
    const worklet = context.audioWorklet
    const moduleUrl = this.options.workletModuleUrl ?? captureWorkletModuleUrl
    if (worklet === undefined || moduleUrl === '') {
      return null
    }
    try {
      await worklet.addModule(moduleUrl)
      if (this.isStale(generation)) {
        return null
      }
      const node = this.createWorkletNode(context)
      if (this.isStale(generation)) {
        node.disconnect()
        return null
      }
      node.port.onmessage = (event) => {
        const data = event.data
        if (data instanceof Float32Array) {
          chunker.push(downsampleTo16k(data, context.sampleRate))
        }
      }
      return node
    } catch {
      return null
    }
  }

  private createWorkletNode(context: RealtimeCaptureContextLike): RealtimeWorkletNodeLike {
    const factory = this.options.workletNodeFactory
    if (factory !== undefined) {
      return factory(context)
    }
    const NodeCtor = (globalThis as { AudioWorkletNode?: typeof AudioWorkletNode })
      .AudioWorkletNode
    if (NodeCtor === undefined) {
      throw new Error('当前环境不支持 AudioWorklet（AudioWorkletNode 不可用）')
    }
    return new NodeCtor(context as unknown as BaseAudioContext, CAPTURE_WORKLET_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    }) as unknown as RealtimeWorkletNodeLike
  }

  private connectOutput(
    context: RealtimeCaptureContextLike,
    sourceNode: { connect(target: unknown): void },
    workletNode: RealtimeWorkletNodeLike | null,
    processor: RealtimeScriptProcessorLike | null,
  ): void {
    if (workletNode !== null) {
      sourceNode.connect(workletNode)
      if (context.createGain !== undefined) {
        const silent = context.createGain()
        silent.gain.value = 0
        workletNode.connect(silent)
        silent.connect(context.destination)
      } else {
        workletNode.connect(context.destination)
      }
      return
    }
    if (processor === null) {
      return
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
  }

  private takeResources(): CaptureResources {
    const resources: CaptureResources = {
      context: this.context,
      stream: this.stream,
      processor: this.processor,
      workletNode: this.workletNode,
      sourceNode: this.sourceNode,
      chunker: this.chunker,
    }
    this.context = null
    this.stream = null
    this.processor = null
    this.workletNode = null
    this.sourceNode = null
    this.chunker = null
    return resources
  }

  private async releaseResources(resources: CaptureResources): Promise<void> {
    resources.chunker?.flush()
    resources.processor?.disconnect()
    resources.workletNode?.disconnect()
    resources.sourceNode?.disconnect()
    for (const track of resources.stream?.getTracks() ?? []) {
      track.stop()
    }
    await resources.context?.close?.()
  }
}
