import { describe, expect, it, vi } from 'vitest'
import {
  MicrophoneCapture,
  REALTIME_AUDIO_CONSTRAINTS,
  RealtimeVoiceClient,
  type RealtimeCaptureContextLike,
  type RealtimeMediaStreamLike,
  type RealtimeScriptProcessorLike,
  type RealtimeWorkletNodeLike,
} from './realtimeVoiceClient'
import {
  CAPTURE_WORKLET_BLOCK_SAMPLES,
  CAPTURE_WORKLET_NAME,
  base64ToBytes,
  takeFixedBlocks,
} from './realtimeAudio'

/** 可控媒体流（记录停止次数；可回读实测约束）。 */
class FakeTrack {
  stopped = 0
  constructor(
    private readonly settings: {
      echoCancellation?: boolean
      noiseSuppression?: boolean
      autoGainControl?: boolean
    } = {},
  ) {}
  stop(): void {
    this.stopped += 1
  }
  getSettings(): {
    echoCancellation?: boolean
    noiseSuppression?: boolean
    autoGainControl?: boolean
  } {
    return this.settings
  }
}

class FakeStream implements RealtimeMediaStreamLike {
  constructor(readonly tracks: FakeTrack[]) {}
  getTracks(): FakeTrack[] {
    return this.tracks
  }
  getAudioTracks(): FakeTrack[] {
    return this.tracks
  }
}

/** 可控 ScriptProcessor 节点。 */
class FakeProcessor implements RealtimeScriptProcessorLike {
  onaudioprocess:
    | ((event: { inputBuffer: { getChannelData(channel: number): Float32Array } }) => void)
    | null = null
  connects = 0
  disconnects = 0
  connect(): void {
    this.connects += 1
  }
  disconnect(): void {
    this.disconnects += 1
  }
}

/** 可控 AudioWorklet 节点。 */
class FakeWorkletNode implements RealtimeWorkletNodeLike {
  port: { onmessage: ((event: { data: unknown }) => void) | null } = { onmessage: null }
  connects = 0
  disconnects = 0
  connect(): void {
    this.connects += 1
  }
  disconnect(): void {
    this.disconnects += 1
  }
  push(samples: Float32Array): void {
    this.port.onmessage?.({ data: samples })
  }
}

/** 可控采集上下文。 */
class FakeContext implements RealtimeCaptureContextLike {
  sampleRate = 16000
  destination: unknown = {}
  audioWorklet: { addModule(url: string): Promise<void> } | undefined
  modules: string[] = []
  processors: FakeProcessor[] = []
  resumeCalls = 0
  closeCalls = 0
  connectedTargets: unknown[] = []
  constructor(options: { worklet?: boolean; addModuleRejects?: boolean } = {}) {
    if (options.worklet === true) {
      this.audioWorklet = {
        addModule: (url: string) => {
          this.modules.push(url)
          return options.addModuleRejects === true
            ? Promise.reject(new Error('worklet 加载失败'))
            : Promise.resolve()
        },
      }
    }
  }
  createMediaStreamSource(): { connect(target: unknown): void; disconnect(): void } {
    return {
      connect: (target: unknown) => {
        this.connectedTargets.push(target)
      },
      disconnect: () => undefined,
    }
  }
  createScriptProcessor(): FakeProcessor {
    const processor = new FakeProcessor()
    this.processors.push(processor)
    return processor
  }
  createGain(): { gain: { value: number }; connect(target: unknown): void } {
    return { gain: { value: 1 }, connect: () => undefined }
  }
  resume(): Promise<void> {
    this.resumeCalls += 1
    return Promise.resolve()
  }
  close(): Promise<void> {
    this.closeCalls += 1
    return Promise.resolve()
  }
}

function buildCapture(options: {
  context: FakeContext
  stream: FakeStream
  onFrame?: (payload: string) => void
  onError?: (message: string) => void
  workletNode?: FakeWorkletNode | null
  workletNodeFactory?: () => RealtimeWorkletNodeLike
  useDefaultWorkletNode?: boolean
  frameSamples?: number
}) {
  const frames: string[] = []
  const errors: string[] = []
  const constraintsSeen: MediaStreamConstraints[] = []
  const getUserMedia = vi.fn((constraints: MediaStreamConstraints) => {
    constraintsSeen.push(constraints)
    return Promise.resolve(options.stream as RealtimeMediaStreamLike)
  })
  const usesDefaultWorkletNode = options.useDefaultWorkletNode === true
  const capture = new MicrophoneCapture({
    onFrameBase64: (payload) => {
      frames.push(payload)
      options.onFrame?.(payload)
    },
    onError: (message) => {
      errors.push(message)
      options.onError?.(message)
    },
    getUserMedia,
    audioContextFactory: () => options.context,
    workletModuleUrl: 'blob:worklet',
    ...(usesDefaultWorkletNode
      ? {}
      : {
          workletNodeFactory:
            options.workletNodeFactory ??
            (() => options.workletNode ?? new FakeWorkletNode()),
        }),
    frameSamples: options.frameSamples,
  })
  return { capture, frames, errors, getUserMedia, constraintsSeen }
}

describe('MicrophoneCapture 约束与能力回读', () => {
  it('getUserMedia 固定全开 AEC / 降噪 / 自动增益', async () => {
    const context = new FakeContext()
    const { capture, getUserMedia, constraintsSeen } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
    })
    await capture.start()
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(constraintsSeen[0]).toEqual(REALTIME_AUDIO_CONSTRAINTS)
    expect(REALTIME_AUDIO_CONSTRAINTS.audio).toMatchObject({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    })
  })

  it('回读实际生效值（未生效字段为 false）', async () => {
    const context = new FakeContext()
    const { capture } = buildCapture({
      context,
      stream: new FakeStream([
        new FakeTrack({ echoCancellation: false, noiseSuppression: true, autoGainControl: true }),
      ]),
    })
    expect(capture.getCapabilities()).toBeNull()
    await capture.start()
    expect(capture.getCapabilities()).toEqual({
      echoCancellation: false,
      noiseSuppression: true,
      autoGainControl: true,
    })
  })
})

describe('MicrophoneCapture 采集路径（worklet 优先 / ScriptProcessor 回退）', () => {
  it('AudioWorklet 加载成功：模块 URL 传入 addModule，块回传经 16k 成帧上行', async () => {
    const context = new FakeContext({ worklet: true })
    const node = new FakeWorkletNode()
    const { capture, frames } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
      workletNode: node,
      frameSamples: 4,
    })
    await capture.start()
    expect(context.modules).toEqual(['blob:worklet'])
    expect(context.processors).toEqual([])
    expect(node.connects).toBeGreaterThan(0)
    node.push(new Float32Array([0.5, 0.25, -0.5, -0.25]))
    expect(frames).toHaveLength(1)
    expect(base64ToBytes(frames[0] ?? '').length).toBe(8)
  })

  it('worklet 模块加载失败：回退 ScriptProcessor 且语义一致', async () => {
    const context = new FakeContext({ worklet: true, addModuleRejects: true })
    const { capture, frames } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
      frameSamples: 4,
    })
    await capture.start()
    expect(context.modules).toEqual(['blob:worklet'])
    expect(context.processors).toHaveLength(1)
    const processor = context.processors[0]
    processor?.onaudioprocess?.({
      inputBuffer: { getChannelData: () => new Float32Array([0.5, 0.25, -0.5, -0.25]) },
    })
    expect(frames).toHaveLength(1)
    expect(base64ToBytes(frames[0] ?? '').length).toBe(8)
  })

  it('Worklet 建节点失败（无 AudioWorkletNode）：回退 ScriptProcessor', async () => {
    const context = new FakeContext({ worklet: true })
    const { capture } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
      workletNodeFactory: () => {
        throw new Error('AudioWorkletNode 不可用')
      },
    })
    await capture.start()
    expect(context.processors).toHaveLength(1)
  })

  it('无 AudioWorklet 能力：直接走 ScriptProcessor（零模块加载）', async () => {
    const context = new FakeContext()
    const { capture } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
    })
    await capture.start()
    expect(context.modules).toEqual([])
    expect(context.processors).toHaveLength(1)
  })

  it('worklet 块常量与处理器名与模块契约一致', () => {
    expect(CAPTURE_WORKLET_NAME).toBe('realtime-capture')
    expect(CAPTURE_WORKLET_BLOCK_SAMPLES).toBeGreaterThan(0)
  })
})

describe('MicrophoneCapture 移动端暂停 / 恢复', () => {
  it('pause 停轨关上下文并置暂停态；resume 重新申请媒体流（会话不重连）', async () => {
    const context = new FakeContext()
    const track = new FakeTrack()
    const { capture, getUserMedia } = buildCapture({
      context,
      stream: new FakeStream([track]),
    })
    await capture.start()
    expect(capture.isPaused()).toBe(false)
    await capture.pause()
    expect(capture.isPaused()).toBe(true)
    expect(track.stopped).toBe(1)
    expect(context.closeCalls).toBe(1)
    await capture.resume()
    expect(capture.isPaused()).toBe(false)
    expect(getUserMedia).toHaveBeenCalledTimes(2)
  })

  it('stop 幂等：重复停止不抛、重复 start 不重复建流', async () => {
    const context = new FakeContext()
    const { capture, getUserMedia } = buildCapture({
      context,
      stream: new FakeStream([new FakeTrack()]),
    })
    await capture.start()
    await capture.start()
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    await capture.stop()
    await capture.stop()
    expect(capture.isPaused()).toBe(false)
  })

  it('媒体设备不可用：onError 中文提示且不抛', async () => {
    const errors: string[] = []
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      onError: (message) => errors.push(message),
      audioContextFactory: () => new FakeContext(),
      getUserMedia: () => Promise.reject(new Error('麦克风权限被拒绝')),
    })
    await capture.start()
    expect(errors).toEqual(['麦克风权限被拒绝'])
  })
})

describe('takeFixedBlocks（worklet 块切分核心）', () => {
  it('满块按序切出、余量留存（跨调用累积）', () => {
    const first = takeFixedBlocks(new Float32Array(0), new Float32Array([1, 2, 3, 4, 5]), 4)
    expect(first.blocks).toHaveLength(1)
    expect(Array.from(first.blocks[0] ?? [])).toEqual([1, 2, 3, 4])
    expect(Array.from(first.rest)).toEqual([5])
    const second = takeFixedBlocks(first.rest, new Float32Array([6, 7, 8]), 4)
    expect(second.blocks).toHaveLength(1)
    expect(Array.from(second.blocks[0] ?? [])).toEqual([5, 6, 7, 8])
    expect(second.rest).toHaveLength(0)
  })

  it('块长非正：返回余量副本与零块', () => {
    const result = takeFixedBlocks(new Float32Array([1, 2]), new Float32Array([3]), 0)
    expect(result.blocks).toHaveLength(0)
    expect(Array.from(result.rest)).toEqual([1, 2])
  })
})

describe('MicrophoneCapture 默认环境分支（无注入依赖）', () => {
  it('默认 getUserMedia：媒体设备不可用 → onError 提示', async () => {
    const errors: string[] = []
    const capture = new MicrophoneCapture({
      onFrameBase64: () => undefined,
      onError: (message) => errors.push(message),
      audioContextFactory: () => new FakeContext(),
    })
    await capture.start()
    expect(errors[0]).toContain('navigator.mediaDevices 不可用')
  })

  it('默认 AudioContext：环境不支持 Web Audio → onError 提示', async () => {
    const errors: string[] = []
    const contextSpy = window as unknown as { AudioContext?: unknown }
    const original = contextSpy.AudioContext
    contextSpy.AudioContext = undefined
    try {
      const capture = new MicrophoneCapture({
        onFrameBase64: () => undefined,
        onError: (message) => errors.push(message),
        getUserMedia: () => Promise.resolve(new FakeStream([new FakeTrack()])),
      })
      await capture.start()
    } finally {
      contextSpy.AudioContext = original
    }
    expect(errors[0]).toContain('AudioContext 不可用')
  })

  it('默认 worklet 建节点：全局 AudioWorkletNode 缺失 → 回退 ScriptProcessor', async () => {
    const context = new FakeContext({ worklet: true })
    const globals = globalThis as unknown as { AudioWorkletNode?: unknown }
    const original = globals.AudioWorkletNode
    globals.AudioWorkletNode = undefined
    try {
      const { capture } = buildCapture({
        context,
        stream: new FakeStream([new FakeTrack()]),
        useDefaultWorkletNode: true,
      })
      await capture.start()
      expect(context.processors).toHaveLength(1)
    } finally {
      globals.AudioWorkletNode = original
    }
  })

  it('默认 worklet 建节点：全局构造器可用 → 走 worklet 路径', async () => {
    class StubWorkletNode {
      port: { onmessage: ((event: { data: unknown }) => void) | null } = { onmessage: null }
      connect(): void {
        return undefined
      }
      disconnect(): void {
        return undefined
      }
    }
    const globals = globalThis as unknown as { AudioWorkletNode?: unknown }
    const original = globals.AudioWorkletNode
    globals.AudioWorkletNode = StubWorkletNode
    const context = new FakeContext({ worklet: true })
    try {
      const { capture } = buildCapture({
        context,
        stream: new FakeStream([new FakeTrack()]),
        useDefaultWorkletNode: true,
      })
      await capture.start()
      expect(context.processors).toEqual([])
    } finally {
      globals.AudioWorkletNode = original
    }
  })

  it('无 createGain：worklet / ScriptProcessor 均直连 destination', async () => {
    const workletContext = new FakeContext({ worklet: true })
    ;(workletContext as { createGain?: unknown }).createGain = undefined
    const workletRun = buildCapture({
      context: workletContext,
      stream: new FakeStream([new FakeTrack()]),
    })
    await workletRun.capture.start()
    expect(workletContext.connectedTargets).toHaveLength(1)

    const processorContext = new FakeContext()
    ;(processorContext as { createGain?: unknown }).createGain = undefined
    const processorRun = buildCapture({
      context: processorContext,
      stream: new FakeStream([new FakeTrack()]),
    })
    await processorRun.capture.start()
    expect(processorContext.processors).toHaveLength(1)
  })
})

describe('RealtimeVoiceClient 默认时钟', () => {
  it('未注入 now 时使用系统时钟（start.ts_ms 为 epoch 毫秒）', () => {
    const socket = {
      readyState: 1,
      sent: [] as string[],
      send(data: string): void {
        socket.sent.push(data)
      },
      close(): void {
        return undefined
      },
      onopen: null as ((event: unknown) => void) | null,
      onmessage: null as ((event: { data: unknown }) => void) | null,
      onerror: null as ((event: unknown) => void) | null,
      onclose: null as ((event: unknown) => void) | null,
    }
    const client = new RealtimeVoiceClient({
      url: 'ws://test/ws',
      handlers: {
        onMessage: () => undefined,
        onSocketState: () => undefined,
      },
      socketFactory: () => socket,
    })
    client.connect()
    socket.onopen?.({})
    const start = JSON.parse(socket.sent[0] ?? '{}') as { ts_ms?: number }
    expect(start.ts_ms).toBeGreaterThan(0)
    client.close()
  })
})
