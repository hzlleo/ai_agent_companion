import { describe, expect, it } from 'vitest'
import { act, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import RealtimeVoicePage from './RealtimeVoicePage'
import { renderWithProviders } from '@/test/utils'
import type {
  MicrophoneCaptureLike,
  RealtimeClientHandlers,
  RealtimeSocketState,
  RealtimeVoiceClientLike,
} from '@/lib/realtimeVoiceClient'
import type { RealtimePlaybackQueue } from '@/lib/realtimeAudio'
import type { RealtimeAudioChunkMessage, RealtimeDownlinkMessage } from '@/types/realtimeVoice'

/** 可控假客户端（页面注入面）。 */
class FakeClient implements RealtimeVoiceClientLike {
  handlers: RealtimeClientHandlers | null = null
  connects = 0
  ends = 0
  bargeIns = 0
  acks: string[] = []
  frames: string[] = []
  closes = 0
  connect(): void {
    this.connects += 1
  }
  sendAudioFrame(payload: string): void {
    this.frames.push(payload)
  }
  end(): void {
    this.ends += 1
  }
  bargeIn(): void {
    this.bargeIns += 1
  }
  bargeInAck(interruptionId: string): void {
    this.acks.push(interruptionId)
  }
  close(): void {
    this.closes += 1
  }
  emit(message: RealtimeDownlinkMessage): void {
    this.handlers?.onMessage(message)
  }
  socket(state: RealtimeSocketState): void {
    this.handlers?.onSocketState(state)
  }
}

/** 可控假采集。 */
class FakeCapture implements MicrophoneCaptureLike {
  starts = 0
  stops = 0
  onFrame: ((payload: string) => void) | null = null
  onError: ((message: string) => void) | null = null
  start(): Promise<void> {
    this.starts += 1
    return Promise.resolve()
  }
  stop(): Promise<void> {
    this.stops += 1
    return Promise.resolve()
  }
}

/** 可控假播放队列。 */
class FakePlayback {
  pushed: RealtimeAudioChunkMessage[] = []
  interrupts = 0
  push(chunk: RealtimeAudioChunkMessage): void {
    this.pushed.push(chunk)
  }
  interrupt(): void {
    this.interrupts += 1
  }
}

const ready: RealtimeDownlinkMessage = {
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
  streaming_asr: true,
  streaming_tts: true,
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
}

/** 构造页面 + 三件替身。 */
function renderPage(options: { playback?: boolean; bargeIn?: boolean; tts?: boolean } = {}) {
  const client = new FakeClient()
  const capture = new FakeCapture()
  const playback = new FakePlayback()
  const view = renderWithProviders(
    <RealtimeVoicePage
      personaId={3}
      createClient={(handlers) => {
        client.handlers = handlers
        return client
      }}
      createCapture={(onFrameBase64, onError) => {
        capture.onFrame = onFrameBase64
        capture.onError = onError
        return capture
      }}
      createPlayback={() => (options.playback === false ? null : (playback as unknown as RealtimePlaybackQueue))}
    />,
  )
  return { view, client, capture, playback }
}

describe('RealtimeVoicePage', () => {
  it('初始空态：未开始徽章、开始可用、停止/打断禁用', () => {
    renderPage()
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('未开始')
    expect(screen.getByRole('button', { name: /开始对话/ })).toBeEnabled()
    expect(screen.getByRole('button', { name: /停止/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /打断/ })).toBeDisabled()
    expect(screen.getByText('尚未开始对话')).toBeInTheDocument()
  })

  it('开始 → ready：连接中转到聆听中、启动采集并展示会话参数', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    expect(client.connects).toBe(1)
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('连接中')
    act(() => {
      client.emit(ready)
    })
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('聆听中')
    expect(capture.starts).toBe(1)
    expect(screen.getByText(/16000 Hz 单声道/)).toBeInTheDocument()
    expect(screen.getByText(/空闲超时 60 秒/)).toBeInTheDocument()
    expect(screen.getByText('会话已就绪，请开始说话')).toBeInTheDocument()
  })

  it('字幕：partial → final 覆盖、助手增量聚合与定稿、音频分片入播放队列', async () => {
    const user = userEvent.setup()
    const { client, playback } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
      client.emit({ type: 'partial_transcript', ts_ms: 2, segment_id: 'seg-1', text: '你好' })
    })
    expect(screen.getByText('你好')).toBeInTheDocument()
    expect(screen.getByText('识别中…')).toBeInTheDocument()
    act(() => {
      client.emit({
        type: 'final_transcript',
        ts_ms: 3,
        segment_id: 'seg-1',
        text: '你好呀',
        duration_ms: 300,
        language: 'zh',
      })
      client.emit({
        type: 'assistant_text',
        ts_ms: 4,
        turn_id: 'turn-1',
        text: '在的，',
        final: false,
      })
    })
    expect(screen.getByText('你好呀')).toBeInTheDocument()
    expect(screen.getByText('在的，')).toBeInTheDocument()
    act(() => {
      client.emit({
        type: 'assistant_text',
        ts_ms: 5,
        turn_id: 'turn-1',
        text: '在的，怎么了？',
        final: true,
      })
      client.emit({
        type: 'audio_chunk',
        ts_ms: 6,
        turn_id: 'turn-1',
        sentence_index: 0,
        seq: 0,
        total_chunks: 1,
        content_type: 'audio/wav',
        data: 'AAAA',
      })
      client.emit({ type: 'state', ts_ms: 7, state: 'speaking', detail: null })
    })
    expect(screen.getByText('在的，怎么了？')).toBeInTheDocument()
    expect(screen.queryByText('识别中…')).not.toBeInTheDocument()
    expect(playback.pushed).toHaveLength(1)
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('播报中')
  })

  it('打断：清空播放、回执并回到聆听；打断按钮调用 bargeIn', async () => {
    const user = userEvent.setup()
    const { client, playback } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
      client.emit({ type: 'state', ts_ms: 2, state: 'speaking', detail: null })
    })
    await user.click(screen.getByRole('button', { name: /打断/ }))
    expect(client.bargeIns).toBe(1)
    expect(playback.interrupts).toBe(1)
    act(() => {
      client.emit({
        type: 'interrupted',
        ts_ms: 3,
        interruption_id: 'intr-1',
        turn_id: 'turn-1',
        reason: 'manual',
      })
    })
    expect(client.acks).toEqual(['intr-1'])
    expect(screen.getByText('已打断当前回复')).toBeInTheDocument()
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('聆听中')
    expect(playback.interrupts).toBeGreaterThanOrEqual(2)
  })

  it('可恢复错误显示提示条但保留会话；不可恢复错误停止采集并置异常态', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
      client.emit({
        type: 'error',
        ts_ms: 2,
        code: 'LLM_CALL_FAILED',
        message: '语音转写失败',
        recoverable: true,
      })
    })
    expect(screen.getByText('语音转写失败')).toBeInTheDocument()
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('聆听中')
    act(() => {
      client.emit({
        type: 'error',
        ts_ms: 3,
        code: 'INTERNAL_ERROR',
        message: '会话内部错误',
        recoverable: false,
      })
    })
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('异常')
    expect(capture.stops).toBe(1)
  })

  it('closed 与停止按钮：结束态、停采集、结束请求', async () => {
    const user = userEvent.setup()
    const { client, capture, playback } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    act(() => {
      client.emit({ type: 'closed', ts_ms: 2, reason: 'client_end', detail: null })
    })
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('已结束')
    expect(capture.stops).toBe(1)
    expect(playback.interrupts).toBeGreaterThanOrEqual(1)

    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    await user.click(screen.getByRole('button', { name: /停止/ }))
    expect(client.ends).toBe(1)
    expect(capture.stops).toBe(2)
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('已结束')
    expect(screen.getByText('会话已结束')).toBeInTheDocument()
  })

  it('socket 错误 / 关闭提示；采集与播放错误走提示条', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.socket('error')
    })
    expect(
      screen.getByText('实时语音连接失败（请确认 REALTIME_VOICE_ENABLED 与鉴权配置）'),
    ).toBeInTheDocument()
    act(() => {
      capture.onError?.('麦克风权限被拒绝')
    })
    expect(screen.getByText('麦克风权限被拒绝')).toBeInTheDocument()
    act(() => {
      client.socket('closed')
    })
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('已结束')
  })

  it('barge-in 关闭时打断按钮禁用；TTS 关闭时提示仅字幕', async () => {
    const user = userEvent.setup()
    const { client } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit({ ...ready, barge_in_enabled: false, tts_enabled: false })
    })
    expect(screen.getByRole('button', { name: /打断/ })).toBeDisabled()
    expect(screen.getByText('会话已就绪（TTS 未启用，仅显示字幕）')).toBeInTheDocument()
    expect(screen.getByText(/仅字幕/)).toBeInTheDocument()
  })

  it('无播放能力（createPlayback 返回 null）时字幕照常、音频分片不报错', async () => {
    const user = userEvent.setup()
    const { client } = renderPage({ playback: false })
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
      client.emit({
        type: 'audio_chunk',
        ts_ms: 2,
        turn_id: 'turn-1',
        sentence_index: 0,
        seq: 0,
        total_chunks: 1,
        content_type: 'audio/wav',
        data: 'AAAA',
      })
      client.emit({
        type: 'final_transcript',
        ts_ms: 3,
        segment_id: 'seg-1',
        text: '测试字幕',
        duration_ms: 120,
        language: 'zh',
      })
    })
    expect(screen.getByText('测试字幕')).toBeInTheDocument()
  })

  it('卸载时关闭客户端、停止采集并清空播放', async () => {
    const user = userEvent.setup()
    const { view, client, capture, playback } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    view.unmount()
    expect(client.closes).toBe(1)
    expect(capture.stops).toBeGreaterThanOrEqual(1)
    expect(playback.interrupts).toBeGreaterThanOrEqual(1)
  })

  it('默认工厂：jsdom 无 WebAudio / 无麦克风时仍可开始并给出提示', async () => {
    const user = userEvent.setup()
    renderWithProviders(<RealtimeVoicePage />)
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('连接中')
  })

  it('播放错误回调展示提示条', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <RealtimeVoicePage
        createClient={(handlers) => {
          const client = new FakeClient()
          client.handlers = handlers
          return client
        }}
        createCapture={() => new FakeCapture()}
        createPlayback={(onError) => {
          expect(typeof onError).toBe('function')
          onError('音频解码失败')
          return null
        }}
      />,
    )
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    expect(screen.getByText('音频解码失败')).toBeInTheDocument()
  })

  it('麦克风帧回调：采集帧经客户端上行', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
      capture.onFrame?.('FRAME-A')
    })
    expect(client.frames).toEqual(['FRAME-A'])
  })
})
