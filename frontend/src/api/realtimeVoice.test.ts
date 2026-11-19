import { beforeEach, describe, expect, it } from 'vitest'
import { buildRealtimeVoiceUrl, toWebSocketOrigin, REALTIME_VOICE_WS_PATH } from './realtimeVoice'
import { clearAdminToken, setAdminToken } from '@/api/auth'

describe('toWebSocketOrigin', () => {
  it('http / https 转 ws / wss（去尾斜杠）', () => {
    expect(toWebSocketOrigin('http://127.0.0.1:8000')).toBe('ws://127.0.0.1:8000')
    expect(toWebSocketOrigin('https://example.com/')).toBe('wss://example.com')
    expect(toWebSocketOrigin('wss://example.com')).toBe('wss://example.com')
    expect(toWebSocketOrigin('ws://example.com')).toBe('ws://example.com')
  })

  it('无协议前缀按 ws 处理', () => {
    expect(toWebSocketOrigin('127.0.0.1:9000')).toBe('ws://127.0.0.1:9000')
  })
})

describe('buildRealtimeVoiceUrl', () => {
  beforeEach(() => {
    clearAdminToken()
  })

  it('路径与协议正确；无 token / 无人格时只带路径', () => {
    const url = new URL(
      buildRealtimeVoiceUrl(undefined, { baseUrl: 'http://127.0.0.1:8000', token: null }),
    )
    expect(`${url.origin}${url.pathname}`).toBe(`ws://127.0.0.1:8000${REALTIME_VOICE_WS_PATH}`)
    expect(url.searchParams.has('token')).toBe(false)
    expect(url.searchParams.has('persona_id')).toBe(false)
  })

  it('携带已保存 token 与人格绑定查询参数', () => {
    setAdminToken('secret-token')
    const url = new URL(buildRealtimeVoiceUrl(7, { baseUrl: 'https://example.com' }))
    expect(url.protocol).toBe('wss:')
    expect(url.searchParams.get('token')).toBe('secret-token')
    expect(url.searchParams.get('persona_id')).toBe('7')
  })

  it('显式 token 覆盖已保存值（空串不带参数）', () => {
    setAdminToken('stored-token')
    expect(
      new URL(buildRealtimeVoiceUrl(null, { baseUrl: 'http://x:1', token: '' })).searchParams.has(
        'token',
      ),
    ).toBe(false)
    expect(
      new URL(
        buildRealtimeVoiceUrl(null, { baseUrl: 'http://x:1', token: 'explicit' }),
      ).searchParams.get('token'),
    ).toBe('explicit')
  })
})
