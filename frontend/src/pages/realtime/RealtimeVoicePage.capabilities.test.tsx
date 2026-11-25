import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import RealtimeVoicePage from './RealtimeVoicePage'
import { renderWithProviders } from '@/test/utils'
import type {
  MicrophoneCaptureLike,
  RealtimeCaptureCapabilities,
  RealtimeClientHandlers,
  RealtimeVoiceClientLike,
} from '@/lib/realtimeVoiceClient'
import type { RealtimePlaybackQueue } from '@/lib/realtimeAudio'
import type { RealtimeDownlinkMessage } from '@/types/realtimeVoice'

/** 页面注入面假客户端。 */
class FakeClient implements RealtimeVoiceClientLike {
  handlers: RealtimeClientHandlers | null = null
  connect(): void {
    return undefined
  }
  sendAudioFrame(): void {
    return undefined
  }
  end(): void {
    return undefined
  }
  bargeIn(): void {
    return undefined
  }
  bargeInAck(): void {
    return undefined
  }
  close(): void {
    return undefined
  }
  emit(message: RealtimeDownlinkMessage): void {
    this.handlers?.onMessage(message)
  }
}

/** 页面注入面假采集（含暂停 / 恢复 / 能力回读）。 */
class FakeCapture implements MicrophoneCaptureLike {
  starts = 0
  pauses = 0
  resumes = 0
  paused = false
  capabilities: RealtimeCaptureCapabilities | null = {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  }
  start(): Promise<void> {
    this.starts += 1
    this.paused = false
    return Promise.resolve()
  }
  stop(): Promise<void> {
    this.paused = false
    return Promise.resolve()
  }
  pause(): Promise<void> {
    this.pauses += 1
    this.paused = true
    return Promise.resolve()
  }
  resume(): Promise<void> {
    this.resumes += 1
    this.paused = false
    return Promise.resolve()
  }
  getCapabilities(): RealtimeCaptureCapabilities | null {
    return this.capabilities
  }
  isPaused(): boolean {
    return this.paused
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

/** 渲染页面并返回假客户端 / 假采集。 */
function renderPage() {
  const client = new FakeClient()
  const capture = new FakeCapture()
  const view = renderWithProviders(
    <RealtimeVoicePage
      createClient={(handlers) => {
        client.handlers = handlers
        return client
      }}
      createCapture={() => capture}
      createPlayback={(): RealtimePlaybackQueue | null => null}
    />,
  )
  return { view, client, capture }
}

/** 覆盖 document.visibilityState（jsdom 只读属性）。 */
function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  })
}

describe('RealtimeVoicePage 能力公告与采集边界', () => {
  it('展示流式识别 / 流式合成能力与回声消除态', async () => {
    const user = userEvent.setup()
    const { client } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    await act(async () => {
      client.emit(ready)
      await Promise.resolve()
    })
    expect(screen.getByText(/流式识别 开/)).toBeInTheDocument()
    expect(screen.getByText(/流式合成 开/)).toBeInTheDocument()
    expect(screen.getByText(/回声消除 开/)).toBeInTheDocument()
  })

  it('采集能力回读：浏览器未生效 AEC 时摘要展示降级（服务端公告为开）', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    capture.capabilities = {
      echoCancellation: false,
      noiseSuppression: true,
      autoGainControl: true,
    }
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    await act(async () => {
      client.emit(ready)
      await Promise.resolve()
    })
    expect(screen.getByText(/回声消除 降级/)).toBeInTheDocument()
    expect(
      screen.getByText('浏览器未生效回声消除（AEC），外放场景建议佩戴耳机'),
    ).toBeInTheDocument()
  })

  it('服务端公告 aec=false 时展示降级态', async () => {
    const user = userEvent.setup()
    const { client } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    await act(async () => {
      client.emit({ ...ready, aec: false })
      await Promise.resolve()
    })
    expect(screen.getByText(/回声消除 降级/)).toBeInTheDocument()
  })

  it('浏览器未生效 AEC / 降噪时给出提示（不阻断会话）', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    capture.capabilities = {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: true,
    }
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    await waitFor(() =>
      expect(
        screen.getByText('浏览器未生效回声消除（AEC），外放场景建议佩戴耳机'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText('浏览器未生效降噪，嘈杂环境下识别准确率可能下降。')).toBeInTheDocument()
    expect(screen.getByLabelText('会话状态')).toHaveTextContent('聆听中')
  })

  it('后台切换暂停采集，回到前台自动恢复（会话不重连）', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    await waitFor(() => expect(capture.starts).toBe(1))
    setVisibility('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(capture.pauses).toBe(1)
    expect(
      screen.getByText(/音频采集已暂停（页面在后台或被系统挂起）/),
    ).toBeInTheDocument()
    setVisibility('visible')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(capture.resumes).toBe(1))
    expect(capture.starts).toBe(1)
    expect(screen.queryByText(/音频采集已暂停（页面在后台或被系统挂起）/)).toBeNull()
  })

  it('暂停后点击页面恢复采集（移动端手势恢复）', async () => {
    const user = userEvent.setup()
    const { client, capture } = renderPage()
    await user.click(screen.getByRole('button', { name: /开始对话/ }))
    act(() => {
      client.emit(ready)
    })
    await waitFor(() => expect(capture.starts).toBe(1))
    setVisibility('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(capture.pauses).toBe(1)
    fireEvent.pointerDown(screen.getByText('实时语音会话'))
    await waitFor(() => expect(capture.resumes).toBe(1))
    expect(capture.isPaused()).toBe(false)
  })

  it('未开始会话时后台切换不影响采集（无采集实例）', () => {
    const { capture } = renderPage()
    setVisibility('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(capture.pauses).toBe(0)
    expect(capture.resumes).toBe(0)
    vi.restoreAllMocks()
  })
})
