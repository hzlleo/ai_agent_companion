import { describe, expect, it, vi } from 'vitest'
import {
  MicrophoneCapture,
  RealtimeVoiceClient,
  SOCKET_OPEN,
  type RealtimeCaptureContextLike,
  type RealtimeClientHandlers,
  type RealtimeMediaStreamLike,
  type RealtimeScriptProcessorLike,
  type RealtimeSocketLike,
} from './realtimeVoiceClient'
import {
  base64ToBytes,
  type RealtimeAudioSourceLike,
} from './realtimeAudio'
import type { RealtimeReadyMessage } from '@/types/realtimeVoice'

/** fake WS（记录上行帧，可手动触发事件）。 */
class FakeSocket implements RealtimeSocketLike {
  readyState: number = SOCKET_OPEN
  sent: string[] = []
  closed = 0
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  onclose: ((event: unknown) => void) | null = null

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closed += 1
    this.readyState = 3
    this.onclose?.({})
  }

  open(): void {
    this.onopen?.({})
  }

  receive(payload: unknown): void {
    this.onmessage?.({ data: payload })
  }

  fail(): void {
    this.onerror?.({})
  }

  uplink(): Array<Record<string, unknown>> {
    return this.sent.map((payload) => JSON.parse(payload) as Record<string, unknown>)
  }
}

/** 组合测试替身：client + socket + handlers。 */
function buildClient(personaId?: number) {
  const handlers: Required<RealtimeClientHandlers> = {
    onMessage: vi.fn(),
    onSocketState: vi.fn(),
    onError: vi.fn(),
  }
  const sockets: FakeSocket[] = []
  const intervals: Array<{ handler: () => void; timeout: number; cleared: () => boolean }> = []
  const client = new RealtimeVoiceClient({
    url: 'ws://test/api/v1/realtime/voice/ws',
    handlers,
    personaId,
    now: () => 1000,
    socketFactory: () => {
      const socket = new FakeSocket()
      sockets.push(socket)
      return socket
    },
    setIntervalFn: (handler, timeout) => {
      let cleared = false
      intervals.push({ handler, timeout, cleared: () => cleared })
      return { clear: () => (cleared = true) }
    },
    clearIntervalFn: (handle) => (handle as { clear: () => void }).clear(),
  })
  return { client, handlers, sockets, intervals }
}

/** 构造 ready 下行消息。 */
function readyMessage(overrides: Partial<RealtimeReadyMessage> = {}): RealtimeReadyMessage {
  return {
    type: 'ready',
    ts_ms: 1,
    protocol_version: '1',
    session_id: 's-1',
    sample_rate_hz: 16000,
    channels: 1,
    audio_format: 'wav',
    asr_enabled: true,
    tts_enabled: true,
    barge_in_enabled: true,
    aec: true,
    streaming_asr: false,
    streaming_tts: false,
    vad: {
      speech_rms_threshold: 0.06,
      silence_rms_threshold: 0.03,
      min_speech_ms: 300,
      silence_end_ms: 700,
      max_segment_ms: 15000,
    },
    limits: {
      max_session_ms: 600000,
      idle_timeout_ms: 60000,
      heartbeat_interval_ms: 15000,
      audio_queue_max_chunks: 64,
      sentence_max_chars: 80,
    },
    ...overrides,
  }
}

describe('RealtimeVoiceClient', () => {
  it('open 后发送 start（含人格 / 采样参数），连接态回调按序触发', () => {
    const { client, handlers, sockets } = buildClient(5)
    client.connect()
    expect(handlers.onSocketState).toHaveBeenCalledWith('connecting')
    const socket = sockets[0]
    expect(socket).toBeDefined()
    socket?.open()
    expect(handlers.onSocketState).toHaveBeenCalledWith('open')
    expect(socket?.uplink()[0]).toMatchObject({
      type: 'start',
      ts_ms: 1000,
      persona_id: 5,
      sample_rate_hz: 16000,
      channels: 1,
    })
  })

  it('上行音频帧序号递增；end / barge_in / barge_in_ack 载荷正确', () => {
    const { client, sockets } = buildClient()
    client.connect()
    sockets[0]?.open()
    client.sendAudioFrame('AAAA')
    client.sendAudioFrame('BBBB')
    client.end()
    client.bargeIn()
    client.bargeInAck('intr-1')
    const messages = sockets[0]?.uplink() ?? []
    expect(messages.slice(1).map((message) => message.type)).toEqual([
      'audio_chunk',
      'audio_chunk',
      'end',
      'barge_in',
      'barge_in_ack',
    ])
    expect(messages[1]).toMatchObject({ seq: 0, data: 'AAAA', ts_ms: 1000 })
    expect(messages[2]).toMatchObject({ seq: 1, data: 'BBBB' })
    expect(messages[4]).toMatchObject({ type: 'barge_in', reason: 'manual' })
    expect(messages[5]).toMatchObject({ interruption_id: 'intr-1' })
  })

  it('ready 后按建议间隔发送心跳；closed 关闭连接并停止心跳', () => {
    const { client, handlers, sockets, intervals } = buildClient()
    client.connect()
    const socket = sockets[0]
    socket?.open()
    socket?.receive(JSON.stringify(readyMessage()))
    expect(handlers.onMessage).toHaveBeenCalledTimes(1)
    expect(intervals).toHaveLength(1)
    expect(intervals[0]?.timeout).toBe(15000)
    intervals[0]?.handler()
    expect(socket?.uplink().some((message) => message.type === 'ping')).toBe(true)
    socket?.receive(
      JSON.stringify({ type: 'closed', ts_ms: 2, reason: 'client_end', detail: null }),
    )
    expect(socket?.closed).toBe(1)
    expect(intervals[0]?.cleared()).toBe(true)
  })

  it('非法 JSON / 非对象 / 非字符串载荷静默忽略', () => {
    const { client, handlers, sockets } = buildClient()
    client.connect()
    const socket = sockets[0]
    socket?.open()
    socket?.receive('{not json')
    socket?.receive('[]')
    socket?.receive(JSON.stringify({ noType: true }))
    socket?.receive(12345)
    expect(handlers.onMessage).not.toHaveBeenCalled()
  })

  it('socket 错误触发 error 回调；关闭后音频帧不再上行', () => {
    const { client, handlers, sockets } = buildClient()
    client.connect()
    const socket = sockets[0]
    socket?.open()
    socket?.fail()
    expect(handlers.onSocketState).toHaveBeenCalledWith('error')
    expect(handlers.onError).toHaveBeenCalledWith('实时语音连接异常，请检查服务端状态')
    const before = socket?.sent.length ?? 0
    client.close()
    client.sendAudioFrame('AAAA')
    expect(socket?.sent.length).toBe(before)
    expect(socket?.closed).toBe(1)
    // 再次 close：幂等（套接字引用已释放）。
    client.close()
    expect(socket?.closed).toBe(1)
  })

  it('未 open 前 send 静默丢弃', () => {
    const { client, sockets } = buildClient()
    client.connect()
    const socket = sockets[0]
    socket!.readyState = 0
    client.end()
    expect(socket?.sent).toHaveLength(0)
  })
})

/** fake 采集处理器。 */
class FakeProcessor implements RealtimeScriptProcessorLike {
  onaudioprocess:
    | ((event: { inputBuffer: { getChannelData(channel: number): Float32Array } }) => void)
    | null = null
  connected = 0
  disconnected = 0
  connect(): void {
    this.connected += 1
  }
  disconnect(): void {
    this.disconnected += 1
  }
}

/** fake 采集上下文。 */
class FakeCaptureContext implements RealtimeCaptureContextLike {
  sampleRate = 48000
  destination: RealtimeAudioSourceLike['buffer'] extends never ? unknown : unknown = {}
  processor = new FakeProcessor()
  resumed = false
  closed = false
  createMediaStreamSource(): { connect(target: unknown): void; disconnect(): void } {
    return { connect: () => undefined, disconnect: () => undefined }
  }
  createScriptProcessor(): RealtimeScriptProcessorLike {
    return this.processor
  }
  createGain: (() => { gain: { value: number }; connect(target: unknown): void }) | undefined = () => {
    return { gain: { value: 1 }, connect: () => undefined }
  }
  resume(): Promise<void> {
    this.resumed = true
    return Promise.resolve()
  }
  close(): Promise<void> {
    this.closed = true
    return Promise.resolve()
  }
}

/** fake 媒体流（记录停轨次数）。 */
function fakeStream(): { stream: RealtimeMediaStreamLike; stopped: () => number } {
  let stops = 0
  const stream: RealtimeMediaStreamLike = {
    getTracks: () => [
      {
        stop: () => {
          stops += 1
        },
      },
    ],
  }
  return { stream, stopped: () => stops }
}

describe('MicrophoneCapture', () => {
  it('采集链路：resume → 处理器接线 → 48k 输入按 100ms 成帧 base64 回调', async () => {
    const frames: string[] = []
    const context = new FakeCaptureContext()
    const { stream, stopped } = fakeStream()
    const capture = new MicrophoneCapture({
      onFrameBase64: (payload) => frames.push(payload),
      audioContextFactory: () => context,
      getUserMedia: () => Promise.resolve(stream),
    })
    await capture.start()
    expect(context.resumed).toBe(true)
    expect(context.processor.connected).toBe(1)
    const input = new Float32Array(4800).fill(0.5)
    context.processor.onaudioprocess?.({
      inputBuffer: { getChannelData: () => input },
    })
    expect(frames).toHaveLength(1)
    const bytes = base64ToBytes(frames[0] ?? '')
    expect(bytes).toHaveLength(3200)
    await capture.stop()
    expect(context.processor.disconnected).toBe(1)
    expect(stopped()).toBe(1)
    expect(context.closed).toBe(true)
  })

  it('重复 start 不重复建流；stop 幂等', async () => {
    const context = new FakeCaptureContext()
    const { stream } = fakeStream()
    let calls = 0
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      audioContextFactory: () => context,
      getUserMedia: () => {
        calls += 1
        return Promise.resolve(stream)
      },
    })
    await capture.start()
    await capture.start()
    expect(calls).toBe(1)
    await capture.stop()
    await capture.stop()
    expect(context.closed).toBe(true)
  })

  it('无 createGain 时直连 destination；无 track 时停止安全', async () => {
    const context = new FakeCaptureContext()
    context.createGain = undefined
    const emptyStream: RealtimeMediaStreamLike = { getTracks: () => [] }
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      audioContextFactory: () => context,
      getUserMedia: () => Promise.resolve(emptyStream),
    })
    await capture.start()
    expect(context.processor.connected).toBe(1)
    await capture.stop()
  })

  it('权限被拒 → onError 中文提示且不抛出', async () => {
    const errors: string[] = []
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      getUserMedia: () => Promise.reject(new Error('麦克风权限被拒绝')),
      audioContextFactory: () => new FakeCaptureContext(),
    })
    await capture.start()
    expect(errors).toEqual([])
    const withSink = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      onError: (message) => errors.push(message),
      getUserMedia: () => Promise.reject(new Error('麦克风权限被拒绝')),
    })
    await withSink.start()
    expect(errors).toEqual(['麦克风权限被拒绝'])
  })

  it('缺省工厂：jsdom 无 AudioContext 时走环境提示（不抛）', async () => {
    const errors: string[] = []
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      onError: (message) => errors.push(message),
      getUserMedia: () => Promise.resolve(fakeStream().stream),
    })
    await capture.start()
    expect(errors.join('')).toContain('Web Audio')
  })
})
