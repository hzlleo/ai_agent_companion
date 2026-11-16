import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  activatePersona,
  batchDeletePersonas,
  copyPersona,
  createPersona,
  deactivatePersona,
  deletePersona,
  getPersona,
  getPersonaCard,
  getPersonaStates,
  listPersonas,
  replacePersonaCard,
  updatePersona,
  upsertPersonaState,
} from '@/api/personas'
import { ApiError } from '@/api/client'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockCard,
  mockPersona,
  okResult,
  server,
} from '@/test/server'

/** 捕获到的请求（method / url / body）。 */
interface CapturedRequest {
  method: string
  url: URL
  body: unknown
}

/** 注册捕获 handler 并返回捕获槽。 */
function capture(pattern: string, method: 'get' | 'post' | 'put' | 'delete', respond: (captured: CapturedRequest) => ReturnType<typeof HttpResponse.json>): {
  captured: () => CapturedRequest | null
} {
  let slot: CapturedRequest | null = null
  server.use(
    http[method](pattern, async ({ request }) => {
      const body = request.method === 'GET' || request.method === 'DELETE' ? null : await request.json().catch(() => null)
      slot = { method: request.method, url: new URL(request.url), body }
      return respond(slot)
    }),
  )
  return { captured: () => slot }
}

/** 断言 rejects 为 ApiError 且 message 匹配。 */
async function expectApiError(promise: Promise<unknown>, message: string, code: string): Promise<void> {
  const error = await promise.catch((e: unknown) => e)
  expect(error).toBeInstanceOf(ApiError)
  const apiError = error as ApiError
  expect(apiError.message).toBe(message)
  expect(apiError.code).toBe(code)
}

describe('personas 端点', () => {
  it('listPersonas：GET /personas + status/cursor/limit 查询串', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas`, 'get', () =>
      HttpResponse.json(okResult({ items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false })),
    )
    await listPersonas({ status: 'active', cursor: 'abc', limit: 50 })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('GET')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas')
    expect(capturedRequest.url.searchParams.get('status')).toBe('active')
    expect(capturedRequest.url.searchParams.get('cursor')).toBe('abc')
    expect(capturedRequest.url.searchParams.get('limit')).toBe('50')
  })

  it('listPersonas：可选参数缺省时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas`, 'get', () =>
      HttpResponse.json(okResult({ items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false })),
    )
    await listPersonas({})
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.url.searchParams.get('status')).toBe(null)
    expect(capturedRequest.url.searchParams.get('cursor')).toBe(null)
  })

  it('createPersona：POST /personas + 完整请求体', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas`, 'post', (c) => {
      const body = c.body as { name: string }
      return HttpResponse.json(okResult(mockPersona({ id: 9, name: body.name })))
    })
    const record = await createPersona({
      name: 'new-one',
      display_name: null,
      status: 'disabled',
      card: { ...mockCard },
    })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('POST')
    expect(record.name).toBe('new-one')
    expect(capturedRequest.body).toEqual({
      name: 'new-one',
      display_name: null,
      status: 'disabled',
      card: mockCard,
    })
  })

  it('createPersona：注册名冲突 → 409 ApiError', async () => {
    server.use(
      http.post(`${BACKEND_ORIGIN}/personas`, () =>
        HttpResponse.json(errorResult('注册名已被占用', 'CONFLICT'), { status: 409 }),
      ),
    )
    await expectApiError(
      createPersona({ name: 'dup', display_name: null, status: 'disabled', card: { ...mockCard } }),
      '注册名已被占用',
      'CONFLICT',
    )
  })

  it('batchDeletePersonas：POST /personas/batch-delete + 部分成功形态', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/batch-delete`, 'post', () =>
      HttpResponse.json(
        okResult({
          total: 3,
          succeeded: 1,
          failed: 2,
          items: [
            { id: 1, success: false, code: 'CONFLICT', message: '当前激活人格禁止删除，请先停用' },
            { id: 2, success: true, code: null, message: '已删除' },
            { id: 3, success: false, code: 'NOT_FOUND', message: '人格不存在' },
          ],
        }),
      ),
    )
    const result = await batchDeletePersonas([1, 2, 3])
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('POST')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/batch-delete')
    expect(capturedRequest.body).toEqual({ ids: [1, 2, 3] })
    expect(result.total).toBe(3)
    expect(result.succeeded).toBe(1)
    expect(result.failed).toBe(2)
    expect(result.items[0]).toEqual({
      id: 1,
      success: false,
      code: 'CONFLICT',
      message: '当前激活人格禁止删除，请先停用',
    })
  })

  it('getPersona：GET /personas/{id}', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId`, 'get', ({ url }) => {
      const id = Number(url.pathname.split('/').pop())
      return HttpResponse.json(okResult(mockPersona({ id })))
    })
    const record = await getPersona(7)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('GET')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/7')
    expect(record.id).toBe(7)
  })

  it('getPersona：404 → 人格不存在', async () => {
    await expectApiError(getPersona(404), '人格不存在', 'NOT_FOUND')
  })

  it('updatePersona：PUT /personas/{id} + 部分更新请求体', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId`, 'put', () =>
      HttpResponse.json(okResult(mockPersona({ name: 'renamed' }))),
    )
    await updatePersona(5, { name: 'renamed', status: 'active' })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('PUT')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/5')
    expect(capturedRequest.body).toEqual({ name: 'renamed', status: 'active' })
  })

  it('updatePersona：卡片补丁合并校验失败 → 400', async () => {
    server.use(
      http.put(`${BACKEND_ORIGIN}/personas/:personaId`, () =>
        HttpResponse.json(errorResult('卡片合并校验失败', 'VALIDATION_ERROR'), { status: 400 }),
      ),
    )
    await expectApiError(
      updatePersona(5, { card: { name: '' } }),
      '卡片合并校验失败',
      'VALIDATION_ERROR',
    )
  })

  it('deletePersona：DELETE /personas/{id} 成功（data 为空）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId`, 'delete', () =>
      HttpResponse.json(okResult(null, '人格已删除')),
    )
    await expect(deletePersona(7)).resolves.toBeUndefined()
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('DELETE')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/7')
  })

  it('deletePersona：激活守卫 → 409 + 后端提示', async () => {
    await expectApiError(deletePersona(1), '当前激活人格禁止删除，请先停用', 'CONFLICT')
  })

  it('copyPersona：POST /personas/{id}/copy + new_name/include_states', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/copy`, 'post', (c) => {
      const body = c.body as { new_name: string }
      return HttpResponse.json(okResult(mockPersona({ id: 10, name: body.new_name, status: 'disabled' })))
    })
    const record = await copyPersona(1, { new_name: 'copy-1', display_name: null, include_states: true })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('POST')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/1/copy')
    expect(capturedRequest.body).toEqual({ new_name: 'copy-1', display_name: null, include_states: true })
    expect(record.name).toBe('copy-1')
  })

  it('copyPersona：新注册名冲突 → 409', async () => {
    await expectApiError(
      copyPersona(1, { new_name: 'companion-1', display_name: null, include_states: false }),
      '注册名已被占用',
      'CONFLICT',
    )
  })

  it('activatePersona：POST /personas/{id}/activate', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/activate`, 'post', ({ url }) => {
      const id = Number(url.pathname.split('/')[4])
      return HttpResponse.json(okResult(mockPersona({ id, is_active: true })))
    })
    const record = await activatePersona(3)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('POST')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/3/activate')
    expect(record.is_active).toBe(true)
  })

  it('activatePersona：停用状态 → 409', async () => {
    await expectApiError(activatePersona(3), '停用状态的人格不可激活', 'CONFLICT')
  })

  it('deactivatePersona：POST /personas/{id}/deactivate', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/deactivate`, 'post', ({ url }) => {
      const id = Number(url.pathname.split('/')[4])
      return HttpResponse.json(okResult(mockPersona({ id, is_active: false })))
    })
    const record = await deactivatePersona(1)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('POST')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/1/deactivate')
    expect(record.is_active).toBe(false)
  })

  it('getPersonaCard：GET /personas/{id}/card', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/card`, 'get', ({ url }) => {
      const id = Number(url.pathname.split('/')[4])
      return HttpResponse.json(okResult({ persona_id: id, card: { ...mockCard }, updated_at: 1731000000000 }))
    })
    const response = await getPersonaCard(6)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('GET')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/6/card')
    expect(response.persona_id).toBe(6)
    expect(response.card.name).toBe('小助手')
  })

  it('replacePersonaCard：PUT /personas/{id}/card + 完整卡片', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/card`, 'put', (c) => {
      const body = c.body as { name: string }
      return HttpResponse.json(okResult({ persona_id: 6, card: { ...mockCard, name: body.name }, updated_at: 1 }))
    })
    await replacePersonaCard(6, { ...mockCard, name: '新称呼' })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('PUT')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/6/card')
    expect(capturedRequest.body).toEqual({ ...mockCard, name: '新称呼' })
  })

  it('getPersonaStates：GET /personas/{id}/states', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/states`, 'get', ({ url }) => {
      const id = Number(url.pathname.split('/')[4])
      return HttpResponse.json(
        okResult([
          { persona_id: id, layer: 'M', state: { mood: 'calm' }, created_at: 1, updated_at: 1 },
          { persona_id: id, layer: 'S', state: { topic: 'night' }, created_at: 1, updated_at: 1 },
        ]),
      )
    })
    const states = await getPersonaStates(8)
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('GET')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/8/states')
    expect(states).toHaveLength(2)
    expect(states[0]?.layer).toBe('M')
  })

  it('upsertPersonaState：PUT /personas/{id}/states/{layer} + state 整体替换', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, 'put', (c) => {
      const body = c.body as { state: Record<string, unknown> }
      return HttpResponse.json(
        okResult({ persona_id: 8, layer: 'S', state: body.state, created_at: 1, updated_at: 2 }),
      )
    })
    const saved = await upsertPersonaState(8, 'S', { state: { topic: 'work' } })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.method).toBe('PUT')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/8/states/S')
    expect(capturedRequest.body).toEqual({ state: { topic: 'work' } })
    expect(saved.state).toEqual({ topic: 'work' })
  })

  it('upsertPersonaState：M 层路径参数', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/personas/:personaId/states/:layer`, 'put', () =>
      HttpResponse.json(okResult({ persona_id: 8, layer: 'M', state: {}, created_at: 1, updated_at: 2 })),
    )
    await upsertPersonaState(8, 'M', { state: { mood: 'happy' } })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.url.pathname).toBe('/api/v1/personas/8/states/M')
  })
})
