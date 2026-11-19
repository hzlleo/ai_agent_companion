import { describe, expect, it, vi } from 'vitest'
import {
  FRAME_SAMPLES,
  PcmFrameChunker,
  RealtimePlaybackQueue,
  base64ToBytes,
  downsampleTo16k,
  floatToPcm16,
  pcm16ToBase64,
  type RealtimeAudioBufferLike,
  type RealtimeAudioContextLike,
  type RealtimeAudioSourceLike,
} from './realtimeAudio'
import type { RealtimeAudioChunkMessage } from '@/types/realtimeVoice'

/** 构造恒定幅度的 Float32 采样。 */
function constantSamples(count: number, value: number): Float32Array {
  return new Float32Array(count).fill(value)
}

/** 构造下行音频分片消息。 */
function audioChunk(
  overrides: Partial<RealtimeAudioChunkMessage> = {},
): RealtimeAudioChunkMessage {
  return {
    type: 'audio_chunk',
    ts_ms: 1,
    turn_id: 'turn-1',
    sentence_index: 0,
    seq: 0,
    total_chunks: 1,
    content_type: 'audio/wav',
    data: btoa('abc'),
    ...overrides,
  }
}

/** fake 音源（记录 start / stop）。 */
class FakeSource implements RealtimeAudioSourceLike {
  buffer: RealtimeAudioBufferLike | null = null
  onended: ((event: Event) => void) | null = null
  startTimes: number[] = []
  stopCalls = 0
  connect(): void {}
  start(when?: number): void {
    this.startTimes.push(when ?? 0)
  }
  stop(): void {
    this.stopCalls += 1
  }
}

/** fake 音频上下文（可控 currentTime / 解码结果）。 */
class FakeContext implements RealtimeAudioContextLike {
  currentTime = 5
  destination: unknown = {}
  sources: FakeSource[] = []
  decoded: Uint8Array[] = []
  decodeError: Error | null = null
  decodeGate: Promise<void> | null = null
  createBufferSource(): RealtimeAudioSourceLike {
    const source = new FakeSource()
    this.sources.push(source)
    return source
  }
  decodeAudioData(data: ArrayBuffer): Promise<RealtimeAudioBufferLike> {
    if (this.decodeError !== null) {
      return Promise.reject(this.decodeError)
    }
    this.decoded.push(new Uint8Array(data))
    if (this.decodeGate !== null) {
      return this.decodeGate.then(() => ({ duration: 1 }))
    }
    return Promise.resolve({ duration: 1 })
  }
}

describe('downsampleTo16k', () => {
  it('48kHz → 16kHz 按 3:1 块平均降采样', () => {
    const input = new Float32Array(9)
    input.set([0.3, 0.3, 0.3, 0.6, 0.6, 0.6, 0.9, 0.9, 0.9])
    const output = downsampleTo16k(input, 48000)
    expect(output.length).toBe(3)
    expect(output[0]).toBeCloseTo(0.3)
    expect(output[1]).toBeCloseTo(0.6)
    expect(output[2]).toBeCloseTo(0.9)
  })

  it('输入采样率不高于目标时返回副本；非法采样率返回空数组', () => {
    const input = constantSamples(4, 0.5)
    const output = downsampleTo16k(input, 8000)
    expect(Array.from(output)).toEqual([0.5, 0.5, 0.5, 0.5])
    expect(output).not.toBe(input)
    expect(downsampleTo16k(input, 0).length).toBe(0)
  })
})

describe('floatToPcm16 / pcm16ToBase64', () => {
  it('削波到 [-1, 1] 并四舍五入为 int16', () => {
    const pcm = floatToPcm16(new Float32Array([1.5, -1.5, 0, 0.5]))
    expect(Array.from(pcm)).toEqual([32767, -32767, 0, 16384])
  })

  it('base64 解码后为小端 int16 字节序列', () => {
    const pcm = new Int16Array([1, -2])
    const bytes = base64ToBytes(pcm16ToBase64(pcm))
    expect(Array.from(bytes)).toEqual([0x01, 0x00, 0xfe, 0xff])
  })
})

describe('PcmFrameChunker', () => {
  it('按 1600 采样成帧（100ms @16kHz），跨 push 拼接', () => {
    const frames: Int16Array[] = []
    const chunker = new PcmFrameChunker((pcm) => frames.push(pcm), FRAME_SAMPLES)
    chunker.push(constantSamples(1000, 0.25))
    expect(frames).toHaveLength(0)
    chunker.push(constantSamples(700, 0.25))
    expect(frames).toHaveLength(1)
    expect(frames[0]?.length).toBe(FRAME_SAMPLES)
    chunker.push(constantSamples(FRAME_SAMPLES * 2, -0.5))
    expect(frames).toHaveLength(3)
  })

  it('flush 冲出不完整尾帧；空 push 无输出', () => {
    const frames: Int16Array[] = []
    const chunker = new PcmFrameChunker((pcm) => frames.push(pcm), FRAME_SAMPLES)
    chunker.push(new Float32Array(0))
    expect(frames).toHaveLength(0)
    chunker.push(constantSamples(10, 1))
    chunker.flush()
    expect(frames).toHaveLength(1)
    expect(frames[0]?.length).toBe(10)
  })
})

describe('RealtimePlaybackQueue', () => {
  it('分片收齐后按 seq 拼接解码并串行排期播放', async () => {
    const context = new FakeContext()
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ seq: 1, total_chunks: 2, data: btoa('def') }))
    expect(queue.pendingSentences).toBe(1)
    queue.push(audioChunk({ seq: 0, total_chunks: 2, data: btoa('abc') }))
    expect(queue.pendingSentences).toBe(0)
    await vi.waitFor(() => expect(context.sources).toHaveLength(1))
    expect(String.fromCharCode(...(context.decoded[0] ?? new Uint8Array()))).toBe('abcdef')
    expect(context.sources[0]?.startTimes[0]).toBe(5)

    // 第二句在首句时长之后排期（串行不重叠）。
    queue.push(audioChunk({ sentence_index: 1, data: btoa('ghi') }))
    await vi.waitFor(() => expect(context.sources).toHaveLength(2))
    expect(context.sources[1]?.startTimes[0]).toBe(6)
  })

  it('重复分片忽略；缺片不触发解码', async () => {
    const context = new FakeContext()
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ seq: 0, total_chunks: 2, data: btoa('a') }))
    queue.push(audioChunk({ seq: 0, total_chunks: 2, data: btoa('a') }))
    expect(queue.pendingSentences).toBe(1)
    await Promise.resolve()
    expect(context.decoded).toHaveLength(0)
  })

  it('interrupt 停止在播音源、清空聚合并作废在途解码', async () => {
    const context = new FakeContext()
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ data: btoa('done') }))
    await vi.waitFor(() => expect(context.sources).toHaveLength(1))
    queue.push(audioChunk({ sentence_index: 1, seq: 0, total_chunks: 2, data: btoa('half') }))
    expect(queue.pendingSentences).toBe(1)
    queue.interrupt()
    expect(queue.pendingSentences).toBe(0)
    expect(context.sources[0]?.stopCalls).toBe(1)
    // 打断后新句从 currentTime 重新排期（不继承旧链）。
    queue.push(audioChunk({ sentence_index: 2, data: btoa('next') }))
    await vi.waitFor(() => expect(context.sources).toHaveLength(2))
    expect(context.sources[1]?.startTimes[0]).toBe(5)
  })

  it('收齐但分片序号有缺口时不触发解码（防御性分支）', async () => {
    const context = new FakeContext()
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ seq: 0, total_chunks: 2, data: btoa('a') }))
    queue.push(audioChunk({ seq: 2, total_chunks: 2, data: btoa('c') }))
    expect(queue.pendingSentences).toBe(0)
    await Promise.resolve()
    expect(context.decoded).toHaveLength(0)
  })

  it('非 Error 拒绝对象归一为 Error 文案', async () => {
    const context = new FakeContext()
    context.decodeError = 'plain-failure' as unknown as Error
    const errors: string[] = []
    const queue = new RealtimePlaybackQueue(context, (error) => errors.push(error.message))
    queue.push(audioChunk({ data: btoa('bad') }))
    await vi.waitFor(() => expect(errors).toEqual(['plain-failure']))
  })

  it('打断发生在解码在途时：解码完成后不再排期播放', async () => {
    const context = new FakeContext()
    let release: () => void = () => undefined
    context.decodeGate = new Promise<void>((resolve) => {
      release = resolve
    })
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ data: btoa('late') }))
    await vi.waitFor(() => expect(context.decoded).toHaveLength(1))
    queue.interrupt()
    release()
    await Promise.resolve()
    await Promise.resolve()
    expect(context.sources).toHaveLength(0)
  })

  it('已停止音源重复 stop 抛错时打断仍幂等', async () => {
    const context = new FakeContext()
    const queue = new RealtimePlaybackQueue(context)
    queue.push(audioChunk({ data: btoa('done') }))
    await vi.waitFor(() => expect(context.sources).toHaveLength(1))
    const source = context.sources[0]
    if (source !== undefined) {
      source.stop = () => {
        throw new Error('already stopped')
      }
    }
    expect(() => queue.interrupt()).not.toThrow()
  })

  it('解码失败经 onError 回调（不抛）', async () => {
    const context = new FakeContext()
    context.decodeError = new Error('decode failed')
    const errors: string[] = []
    const queue = new RealtimePlaybackQueue(context, (error) => errors.push(error.message))
    queue.push(audioChunk({ data: btoa('bad') }))
    await vi.waitFor(() => expect(errors).toEqual(['decode failed']))
    expect(context.sources).toHaveLength(0)
  })
})
