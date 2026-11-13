import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  createModelEntry,
  deleteModelEntry,
  getRegistryState,
  listRoleBindings,
  updateModelEntry,
} from '@/api/models'
import { ApiError } from '@/api/client'
import type { ModelEntryUpdateRequest } from '@/types/model-registry'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockModelEntry,
  mockRegistryState,
  mockRoleBindings,
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
function capture(
  pattern: string,
  method: 'get' | 'post' | 'put' | 'delete',
  respond: () => ReturnType<typeof HttpResponse.json>,
): { captured: () => CapturedRequest | null } {
  let slot: CapturedRequest | null = null
  server.use(
    http[method](pattern, async ({ request }) => {
      const body =
        request.method === 'GET' || request.method === 'DELETE'
          ? null
          : await request.json().catch(() => null)
      slot = { method: request.method, url: new URL(request.url), body }
      return respond()
    }),
  )
  return { captured: () => slot }
}

/** 断言 rejects 为 ApiError。 */
async function expectApiError(promise: Promise<unknown>, message: string, code: string): Promise<ApiError> {
  const error = await promise.catch((e: unknown) => e)
  expect(error).toBeInstanceOf(ApiError)
  const apiError = error as ApiError
  expect(apiError.message).toBe(message)
  expect(apiError.code).toBe(code)
  return apiError
}

describe('models 端点', () => {
  it('getRegistryState：脱敏条目 + 显式绑定解包', async () => {
    const state = await getRegistryState()
    expect(state.entries).toHaveLength(2)
    expect(state.entries[0]).toMatchObject({
      name: 'main',
      api_key_env: 'MAIN_API_KEY',
      roles: ['chat'],
      resolved_roles: ['chat'],
    })
    expect(state.entries[0]).not.toHaveProperty('api_key')
  })

  it('listRoleBindings：五角色绑定面解包', async () => {
    const bindings = await listRoleBindings()
    expect(bindings).toHaveLength(5)
    expect(bindings.find((binding) => binding.role === 'chat')).toEqual({
      role: 'chat',
      bound: true,
      entry_name: 'main',
    })
    expect(bindings.find((binding) => binding.role === 'duty')).toEqual({
      role: 'duty',
      bound: false,
      entry_name: null,
    })
  })

  it('createModelEntry：请求体透传（含 roles / concurrency）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/models/entries`, 'post', () =>
      HttpResponse.json(okResult(mockRegistryState(), '模型条目已创建')),
    )
    await createModelEntry({
      name: 'new-entry',
      provider: 'openai_compatible',
      base_url: 'http://127.0.0.1:9000/v1',
      api_key_env: 'NEW_KEY',
      model: 'm1',
      roles: ['duty'],
      concurrency: 4,
      multimodal: true,
    })
    expect(captured()?.body).toEqual({
      name: 'new-entry',
      provider: 'openai_compatible',
      base_url: 'http://127.0.0.1:9000/v1',
      api_key_env: 'NEW_KEY',
      model: 'm1',
      roles: ['duty'],
      concurrency: 4,
      multimodal: true,
    })
  })

  it('updateModelEntry：路径按 name；请求体不含 name（不可变）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/models/entries/:name`, 'put', () =>
      HttpResponse.json(okResult(mockRegistryState(), '模型条目已更新')),
    )
    await updateModelEntry('main', { roles: [], concurrency: null })
    const capturedRequest = captured()
    if (capturedRequest === null) throw new Error('请求未被捕获')
    expect(capturedRequest.url.pathname).toBe('/api/v1/models/entries/main')
    expect(capturedRequest.body).toEqual({ roles: [], concurrency: null })
  })

  it('deleteModelEntry：仍绑定角色 → 409（bound_roles + 两步指引 details）', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(
          errorResult('模型条目仍绑定角色，禁止删除（先解绑再删除）: main', 'CONFLICT', {
            name: 'main',
            bound_roles: ['chat'],
            fix: '两步操作：PUT /api/v1/models/entries/{name} 将 roles 置空（条目声明角色）；显式 role_bindings 指向的条目需先在 config.yaml 移除该绑定',
          }),
          { status: 409 },
        ),
      ),
    )
    const error = await expectApiError(
      deleteModelEntry('main'),
      '模型条目仍绑定角色，禁止删除（先解绑再删除）: main',
      'CONFLICT',
    )
    expect(error.status).toBe(409)
    expect(error.details).toMatchObject({ name: 'main', bound_roles: ['chat'] })
  })

  it('创建重名 → 409 CONFLICT', async () => {
    const error = await expectApiError(
      createModelEntry({
        name: 'main',
        provider: 'openai_compatible',
        base_url: 'http://127.0.0.1:9000/v1',
        api_key_env: '',
        model: 'm',
        roles: [],
        concurrency: null,
        multimodal: false,
      }),
      '模型条目名已存在: main',
      'CONFLICT',
    )
    expect(error.status).toBe(409)
  })

  it('请求体携带 name（extra=forbid）→ 400 + 逐字段错误列表', async () => {
    server.use(
      http.put(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(
          errorResult('请求参数错误', 'VALIDATION_ERROR', {
            errors: [{ loc: ['body', 'name'], msg: 'Extra inputs are not permitted', type: 'extra_forbidden' }],
          }),
          { status: 400 },
        ),
      ),
    )
    const malformed = { name: 'main', model: 'x' } as unknown as ModelEntryUpdateRequest
    const error = await expectApiError(
      updateModelEntry('main', malformed),
      '请求参数错误',
      'VALIDATION_ERROR',
    )
    expect(error.status).toBe(400)
  })

  it('删除成功：写后注册表状态解包', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(
          okResult(mockRegistryState([mockModelEntry()]), '模型条目已删除'),
        ),
      ),
    )
    const state = await deleteModelEntry('embed-model')
    expect(state.entries.map((entry) => entry.name)).toEqual(['main'])
  })

  it('未知条目 → 404 NOT_FOUND', async () => {
    server.use(
      http.delete(`${BACKEND_ORIGIN}/models/entries/:name`, () =>
        HttpResponse.json(errorResult('模型条目不存在: ghost', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    const error = await expectApiError(deleteModelEntry('ghost'), '模型条目不存在: ghost', 'NOT_FOUND')
    expect(error.status).toBe(404)
    expect(mockRoleBindings()).toHaveLength(5)
  })
})
