/**
 * 实时语音面板——开始 / 停止全双工语音会话、实时字幕、打断与状态提示。
 *
 * 数据面（全部经注入工厂装配；默认走浏览器真实实现）：
 * - `RealtimeVoiceClient`：WS 协议上行与下行分发（`@/lib/realtimeVoiceClient`）；
 * - `MicrophoneCapture`：麦克风采集 → 16kHz PCM 帧 base64 上行；
 * - `RealtimePlaybackQueue`：下行句级音频聚合解码与串行播放。
 *
 * 交互语义（与后端协议同口径）：
 * - 只有收到 `ready` 后才启动麦克风采集（采样率 / 声道以服务端公告为准）；
 * - 采集约束固定全开 AEC / 降噪 / 自动增益，并回读实际生效值——未生效时
 *   提示建议佩戴耳机（服务端 `ready.aec=false` 时同样展示降级）；
 * - 期间的上行音频由服务端 VAD 切段；`interrupted` 到达时立即清空本地播放
 *   并回 `barge_in_ack`；
 * - 移动端边界：页面进入后台暂停采集（会话不重连），回到前台 / 点击页面
 *   时恢复采集（浏览器要求用户手势时由点击触发）；
 * - 可恢复 `error` 保留会话（提示条），不可恢复 `error` / `closed` 停止采集
 *   并展示结束态。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Mic, MicOff, Square, Zap } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { EmptyState } from '@/components/ui/EmptyState'
import { buildRealtimeVoiceUrl } from '@/api/realtimeVoice'
import { RealtimePlaybackQueue } from '@/lib/realtimeAudio'
import {
  MicrophoneCapture,
  RealtimeVoiceClient,
  type MicrophoneCaptureLike,
  type RealtimeCaptureCapabilities,
  type RealtimeClientHandlers,
  type RealtimeVoiceClientLike,
} from '@/lib/realtimeVoiceClient'
import type {
  RealtimeDownlinkMessage,
  RealtimePanelStatus,
  RealtimeSessionPhase,
  RealtimeTranscriptEntry,
} from '@/types/realtimeVoice'

/** 页面注入工厂（测试替身 / 生产默认实现）。 */
export interface RealtimeVoicePageProps {
  /** 客户端工厂（缺省 = 真实 WS 客户端）。 */
  createClient?: (handlers: RealtimeClientHandlers) => RealtimeVoiceClientLike
  /** 采集工厂（缺省 = 真实麦克风采集）。 */
  createCapture?: (
    onFrameBase64: (payload: string) => void,
    onError: (message: string) => void,
  ) => MicrophoneCaptureLike
  /** 播放队列工厂（返回 null = 环境不支持 Web Audio，只显示字幕）。 */
  createPlayback?: (onError: (message: string) => void) => RealtimePlaybackQueue | null
  /** 绑定人格（缺省 = 服务端解析当前激活人格）。 */
  personaId?: number | null
}

/** 相位 → 面板状态（closed 由消息面单独处理）。 */
const PHASE_STATUS: Record<RealtimeSessionPhase, RealtimePanelStatus> = {
  idle: 'listening',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
  closed: 'closed',
}

/** 状态展示文案与徽章色。 */
const STATUS_META: Record<
  RealtimePanelStatus,
  { label: string; variant: 'success' | 'info' | 'warning' | 'secondary' | 'destructive' }
> = {
  idle: { label: '未开始', variant: 'secondary' },
  connecting: { label: '连接中', variant: 'info' },
  listening: { label: '聆听中', variant: 'success' },
  thinking: { label: '思考中', variant: 'info' },
  speaking: { label: '播报中', variant: 'warning' },
  closed: { label: '已结束', variant: 'secondary' },
  error: { label: '异常', variant: 'destructive' },
}

/** 默认客户端工厂（真实 WS + 当前地址）。 */
function defaultCreateClient(
  handlers: RealtimeClientHandlers,
  personaId: number | null | undefined,
): RealtimeVoiceClientLike {
  return new RealtimeVoiceClient({
    url: buildRealtimeVoiceUrl(personaId),
    handlers,
    personaId,
  })
}

/** 默认采集工厂（真实麦克风 + Web Audio）。 */
function defaultCreateCapture(
  onFrameBase64: (payload: string) => void,
  onError: (message: string) => void,
): MicrophoneCaptureLike {
  return new MicrophoneCapture({ onFrameBase64, onError })
}

/** 默认播放队列工厂（无 Web Audio 时返回 null——降级为纯字幕）。 */
function defaultCreatePlayback(
  onError: (message: string) => void,
): RealtimePlaybackQueue | null {
  const Context = typeof window === 'undefined' ? undefined : window.AudioContext
  if (Context === undefined) {
    return null
  }
  try {
    return new RealtimePlaybackQueue(new Context(), (error) => onError(error.message))
  } catch (error) {
    onError(error instanceof Error ? error.message : '音频播放不可用（降级为仅字幕）')
    return null
  }
}

/** 向上插入字幕条目（同键替换；最终稿覆盖增量稿）。 */
function upsertTranscript(
  entries: RealtimeTranscriptEntry[],
  entry: RealtimeTranscriptEntry,
): RealtimeTranscriptEntry[] {
  const index = entries.findIndex((item) => item.role === entry.role && item.key === entry.key)
  if (index < 0) {
    return [...entries, entry]
  }
  const next = [...entries]
  next[index] = entry
  return next
}

/**
 * 实时语音面板。
 *
 * @param props 注入工厂与人格绑定（见 `RealtimeVoicePageProps`）。
 */
export default function RealtimeVoicePage(props: RealtimeVoicePageProps = {}) {
  const [status, setStatus] = useState<RealtimePanelStatus>('idle')
  const [transcripts, setTranscripts] = useState<RealtimeTranscriptEntry[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [sessionInfo, setSessionInfo] = useState<{
    sampleRateHz: number
    ttsEnabled: boolean
    bargeInEnabled: boolean
    idleTimeoutMs: number
    aecAnnounced: boolean
    streamingAsr: boolean
    streamingTts: boolean
  } | null>(null)
  const [captureCapabilities, setCaptureCapabilities] =
    useState<RealtimeCaptureCapabilities | null>(null)
  const [capturePaused, setCapturePaused] = useState(false)
  const clientRef = useRef<RealtimeVoiceClientLike | null>(null)
  const captureRef = useRef<MicrophoneCaptureLike | null>(null)
  const playbackRef = useRef<RealtimePlaybackQueue | null>(null)
  const statusRef = useRef<RealtimePanelStatus>('idle')

  const setStatusSafe = useCallback((next: RealtimePanelStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const stopCapture = useCallback(async () => {
    const capture = captureRef.current
    captureRef.current = null
    await capture?.stop()
  }, [])

  const handleMessage = useCallback(
    (message: RealtimeDownlinkMessage) => {
      switch (message.type) {
        case 'ready': {
          setSessionInfo({
            sampleRateHz: message.sample_rate_hz,
            ttsEnabled: message.tts_enabled,
            bargeInEnabled: message.barge_in_enabled,
            idleTimeoutMs: message.limits.idle_timeout_ms,
            aecAnnounced: message.aec,
            streamingAsr: message.streaming_asr,
            streamingTts: message.streaming_tts,
          })
          setNotice(
            message.tts_enabled
              ? '会话已就绪，请开始说话'
              : '会话已就绪（TTS 未启用，仅显示字幕）',
          )
          setStatusSafe('listening')
          const capture = captureRef.current
          void (async () => {
            await capture?.start()
            const capabilities = capture?.getCapabilities?.() ?? null
            setCaptureCapabilities(capabilities)
            if (capabilities !== null && !capabilities.echoCancellation) {
              setNotice('浏览器未生效回声消除（AEC），外放场景建议佩戴耳机')
            }
            if (capture?.isPaused?.() === true) {
              setCapturePaused(true)
            }
          })()
          return
        }
        case 'partial_transcript': {
          setTranscripts((entries) =>
            upsertTranscript(entries, {
              role: 'user',
              key: message.segment_id,
              text: message.text,
              final: false,
            }),
          )
          return
        }
        case 'final_transcript': {
          setTranscripts((entries) =>
            upsertTranscript(entries, {
              role: 'user',
              key: message.segment_id,
              text: message.text,
              final: true,
            }),
          )
          return
        }
        case 'assistant_text': {
          setTranscripts((entries) => {
            const existing = entries.find(
              (item) => item.role === 'assistant' && item.key === message.turn_id,
            )
            const text = message.final
              ? message.text
              : `${existing?.text ?? ''}${message.text}`
            return upsertTranscript(entries, {
              role: 'assistant',
              key: message.turn_id,
              text,
              final: message.final,
            })
          })
          return
        }
        case 'audio_chunk': {
          playbackRef.current?.push(message)
          return
        }
        case 'state': {
          setStatusSafe(PHASE_STATUS[message.state])
          return
        }
        case 'interrupted': {
          playbackRef.current?.interrupt()
          clientRef.current?.bargeInAck(message.interruption_id)
          setNotice('已打断当前回复')
          setStatusSafe('listening')
          return
        }
        case 'error': {
          setError(message.message)
          if (!message.recoverable) {
            setStatusSafe('error')
            void stopCapture()
          }
          return
        }
        case 'closed': {
          playbackRef.current?.interrupt()
          void stopCapture()
          setStatusSafe('closed')
          return
        }
        default:
          return
      }
    },
    [setStatusSafe, stopCapture],
  )

  const resumeCapture = useCallback(async () => {
    const capture = captureRef.current
    if (capture === null) {
      return
    }
    await capture.resume?.()
    setCapturePaused(capture.isPaused?.() ?? false)
    const capabilities = capture.getCapabilities?.() ?? null
    setCaptureCapabilities(capabilities)
  }, [])

  useEffect(() => {
    const onVisibilityChange = () => {
      const capture = captureRef.current
      if (capture === null) {
        return
      }
      if (document.visibilityState === 'hidden') {
        void capture.pause?.()
        setCapturePaused(true)
        return
      }
      void resumeCapture()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [resumeCapture])

  const handleStart = useCallback(() => {
    setError(null)
    setNotice(null)
    setTranscripts([])
    setCaptureCapabilities(null)
    setCapturePaused(false)
    setStatusSafe('connecting')
    const createPlayback = props.createPlayback ?? defaultCreatePlayback
    playbackRef.current = createPlayback((message) => setError(message))
    const handlers: RealtimeClientHandlers = {
      onMessage: handleMessage,
      onSocketState: (state) => {
        if (state === 'error') {
          setError('实时语音连接失败（请确认 REALTIME_VOICE_ENABLED 与鉴权配置）')
        }
        if (state === 'closed' && statusRef.current !== 'closed') {
          setStatusSafe('closed')
        }
      },
      onError: (message) => setError(message),
    }
    const createClient = props.createClient ?? ((next) => defaultCreateClient(next, props.personaId))
    const client = createClient(handlers)
    clientRef.current = client
    const createCapture = props.createCapture ?? defaultCreateCapture
    captureRef.current = createCapture(
      (payload) => clientRef.current?.sendAudioFrame(payload),
      (message) => setError(message),
    )
    client.connect()
  }, [handleMessage, props.createCapture, props.createClient, props.createPlayback, props.personaId, setStatusSafe])

  const handleStop = useCallback(() => {
    clientRef.current?.end()
    void stopCapture()
    setCapturePaused(false)
    playbackRef.current?.interrupt()
    setStatusSafe('closed')
    setNotice('会话已结束')
  }, [setStatusSafe, stopCapture])

  const handleInterrupt = useCallback(() => {
    clientRef.current?.bargeIn()
    playbackRef.current?.interrupt()
  }, [])

  useEffect(
    () => () => {
      const client = clientRef.current
      clientRef.current = null
      client?.close()
      void captureRef.current?.stop()
      captureRef.current = null
      playbackRef.current?.interrupt()
      playbackRef.current = null
    },
    [],
  )

  const active = status === 'connecting' || status === 'listening' || status === 'thinking' || status === 'speaking'
  const meta = STATUS_META[status]

  return (
    <div
      className="space-y-4 p-6"
      onPointerDown={() => {
        if (capturePaused) {
          void resumeCapture()
        }
      }}
    >
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle>实时语音会话</CardTitle>
          <Badge variant={meta.variant} aria-label="会话状态">
            {meta.label}
          </Badge>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" onClick={handleStart} disabled={active}>
              <Mic className="mr-1 h-4 w-4" aria-hidden="true" />
              开始对话
            </Button>
            <Button type="button" variant="outline" onClick={handleStop} disabled={!active}>
              <Square className="mr-1 h-4 w-4" aria-hidden="true" />
              停止
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={handleInterrupt}
              disabled={!active || sessionInfo?.bargeInEnabled === false}
            >
              <Zap className="mr-1 h-4 w-4" aria-hidden="true" />
              打断
            </Button>
          </div>
          {sessionInfo !== null ? (
            <p className="text-xs text-[var(--color-text-secondary)]">
              上行 {sessionInfo.sampleRateHz} Hz 单声道 ／ 下行{' '}
              {sessionInfo.ttsEnabled ? '语音播报' : '仅字幕'} ／ 空闲超时{' '}
              {Math.round(sessionInfo.idleTimeoutMs / 1000)} 秒 ／ 流式识别{' '}
              {sessionInfo.streamingAsr ? '开' : '关'} ／ 流式合成{' '}
              {sessionInfo.streamingTts ? '开' : '关'} ／ 回声消除{' '}
              {sessionInfo.aecAnnounced && (captureCapabilities?.echoCancellation ?? true)
                ? '开'
                : '降级'}
            </p>
          ) : null}
          {capturePaused ? (
            <Alert variant="info">
              <AlertDescription>
                音频采集已暂停（页面在后台或被系统挂起），点击页面或回到前台即可恢复（会话保持）。
              </AlertDescription>
            </Alert>
          ) : null}
          {captureCapabilities !== null && !captureCapabilities.noiseSuppression ? (
            <Alert variant="info">
              <AlertDescription>
                浏览器未生效降噪，嘈杂环境下识别准确率可能下降。
              </AlertDescription>
            </Alert>
          ) : null}
          {notice !== null ? (
            <Alert variant="info">
              <AlertDescription>{notice}</AlertDescription>
            </Alert>
          ) : null}
          {error !== null ? (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>实时字幕</CardTitle>
        </CardHeader>
        <CardContent>
          {transcripts.length === 0 ? (
            <EmptyState
              icon={<MicOff className="h-6 w-6 text-[var(--color-text-muted)]" />}
              title="尚未开始对话"
              description="点击「开始对话」授权麦克风后，转写与回复会实时显示在这里。"
            />
          ) : (
            <ul className="space-y-3" aria-label="实时字幕列表">
              {transcripts.map((entry) => (
                <li
                  key={`${entry.role}-${entry.key}`}
                  className="rounded-md border border-[var(--color-border)] p-3"
                >
                  <div className="mb-1 flex items-center gap-2">
                    <Badge variant={entry.role === 'user' ? 'info' : 'default'}>
                      {entry.role === 'user' ? '我' : '回复'}
                    </Badge>
                    {!entry.final ? (
                      <span className="text-xs text-[var(--color-text-muted)]">识别中…</span>
                    ) : null}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm">{entry.text}</p>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
