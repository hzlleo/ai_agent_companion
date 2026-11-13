import { describe, expect, it } from 'vitest'
import {
  createEmptyEpisodeForm,
  formToEpisodeRequest,
  parseEpisodePayload,
  payloadTemplateForKind,
  validateEpisodeForm,
} from './episodeFormModel'

describe('parseEpisodePayload', () => {
  it('chat：合法对象收敛为类型化载荷（未知键忽略）', () => {
    const result = parseEpisodePayload(
      'chat',
      '{"user_content": "累了", "assistant_content": "抱抱", "unknown_key": 1}',
    )
    expect(result).toEqual({
      payload: { user_content: '累了', assistant_content: '抱抱', session_id: null },
    })
  })

  it('chat：session_id 数值透传', () => {
    const result = parseEpisodePayload('chat', '{"session_id": 3}')
    expect(result).toEqual({
      payload: { user_content: '', assistant_content: '', session_id: 3 },
    })
  })

  it('duty：details 须为对象', () => {
    const ok = parseEpisodePayload('duty', '{"title": "t", "details": {"k": 1}}')
    expect(ok).toEqual({ payload: { title: 't', summary: '', details: { k: 1 } } })
    const bad = parseEpisodePayload('duty', '{"details": [1, 2]}')
    expect(bad).toEqual({ error: 'duty 载荷的 details 须为 JSON 对象' })
  })

  it('platform：action/summary 类型校验', () => {
    const bad = parseEpisodePayload('platform', '{"action": 1}')
    expect(bad).toEqual({ error: 'platform 载荷的 action 须为字符串' })
    const ok = parseEpisodePayload('platform', '{"action": "msg", "summary": "s"}')
    expect(ok).toEqual({ payload: { action: 'msg', summary: 's', metadata: {} } })
  })

  it('dream / system：text 类型校验 + 缺省收敛', () => {
    const bad = parseEpisodePayload('dream', '{"text": 1}')
    expect(bad).toEqual({ error: '注记载荷的 text 须为字符串' })
    const ok = parseEpisodePayload('system', '{}')
    expect(ok).toEqual({ payload: { text: '', metadata: {} } })
  })

  it('非法 JSON / 非对象 → 中文错误', () => {
    expect(parseEpisodePayload('chat', 'not-json')).toEqual({ error: 'JSON 解析失败，请检查格式' })
    expect(parseEpisodePayload('chat', '[]')).toEqual({ error: '事件载荷必须为 JSON 对象' })
    expect(parseEpisodePayload('chat', '"text"')).toEqual({ error: '事件载荷必须为 JSON 对象' })
  })

  it('chat 字段类型校验', () => {
    expect(parseEpisodePayload('chat', '{"user_content": 1}')).toEqual({
      error: 'chat 载荷的 user_content 须为字符串',
    })
    expect(parseEpisodePayload('chat', '{"session_id": "x"}')).toEqual({
      error: 'chat 载荷的 session_id 须为数值或 null',
    })
  })
})

describe('validateEpisodeForm', () => {
  it('默认模板 + 已填 source 校验通过（仅缺 source 时报必填）', () => {
    const empty = validateEpisodeForm(createEmptyEpisodeForm())
    expect(Object.keys(empty)).toEqual(['source'])
    const filled = validateEpisodeForm({ ...createEmptyEpisodeForm(), source: 'admin-console' })
    expect(Object.keys(filled)).toHaveLength(0)
  })

  it('source 空 / 超长 → 错误', () => {
    const form = { ...createEmptyEpisodeForm(), source: '' }
    expect(validateEpisodeForm(form)['source']).toContain('必填')
    const long = { ...createEmptyEpisodeForm(), source: 'x'.repeat(129) }
    expect(validateEpisodeForm(long)['source']).toBeDefined()
  })

  it('importance 非法 → 错误', () => {
    const form = { ...createEmptyEpisodeForm(), importance: '11' }
    expect(validateEpisodeForm(form)['importance']).toContain('1-10')
    const nan = { ...createEmptyEpisodeForm(), importance: 'abc' }
    expect(validateEpisodeForm(nan)['importance']).toBeDefined()
  })

  it('载荷 JSON 非法 → payloadText 错误', () => {
    const form = { ...createEmptyEpisodeForm(), payloadText: '{bad' }
    expect(validateEpisodeForm(form)['payloadText']).toBe('JSON 解析失败，请检查格式')
  })
})

describe('formToEpisodeRequest', () => {
  it('合法表单 → 请求体（ts_ms 恒 null）', () => {
    const form = {
      ...createEmptyEpisodeForm(),
      source: 'admin-console',
      importance: '7',
      payloadText: '{"text": "手动注记"}',
      kind: 'system' as const,
      provenance: 'owner' as const,
    }
    const request = formToEpisodeRequest(form)
    expect(request).toEqual({
      kind: 'system',
      source: 'admin-console',
      content: { text: '手动注记', metadata: {} },
      importance: 7,
      provenance: 'owner',
      ts_ms: null,
    })
  })

  it('载荷非法 → null（调用方前置校验兜底）', () => {
    const form = { ...createEmptyEpisodeForm(), payloadText: '[' }
    expect(formToEpisodeRequest(form)).toBeNull()
  })
})

describe('payloadTemplateForKind', () => {
  it('kind 切换提供对应骨架（可解析）', () => {
    expect(JSON.parse(payloadTemplateForKind('chat'))).toHaveProperty('user_content')
    expect(JSON.parse(payloadTemplateForKind('duty'))).toHaveProperty('title')
    expect(JSON.parse(payloadTemplateForKind('platform'))).toHaveProperty('action')
    expect(JSON.parse(payloadTemplateForKind('dream'))).toHaveProperty('text')
    expect(JSON.parse(payloadTemplateForKind('system'))).toHaveProperty('text')
  })
})
