import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  createDirective,
  deleteDirective,
  listDirectives,
  updateDirective,
} from '@/api/directives'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, mockDirective, okResult, server } from '@/test/server'

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

describe('directives 端点（人格作用域查询参数）', () => {
  it('listDirectives：persona_id 进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/directives`, 'get', () =>
      HttpResponse.json(okResult([mockDirective()])),
    )
    const records = await listDirectives(4)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(records[0]?.topic).toBe('directive:7f1a2b3c4d5e')

    await listDirectives(undefined)
    expect(captured()?.url.searchParams.has('persona_id')).toBe(false)
  })

  it('createDirective：POST 体透传（content / 可选 topic）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/directives`, 'post', () =>
      HttpResponse.json(okResult(mockDirective({ id: 9 }), '长期指令已保存')),
    )
    await createDirective(2, { content: '以后都用简体中文' })
    expect(captured()?.url.searchParams.get('persona_id')).toBe('2')
    expect(captured()?.body).toEqual({ content: '以后都用简体中文' })
  })

  it('updateDirective：PUT 路径 + 体透传；404 → ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/directives/1`, 'put', () =>
      HttpResponse.json(okResult(mockDirective({ id: 101, version: 2 }), '长期指令已更新')),
    )
    const updated = await updateDirective(1, 1, { content: '不要超过十二点睡' })
    expect(captured()?.body).toEqual({ content: '不要超过十二点睡' })
    expect(updated.version).toBe(2)

    const error = await updateDirective(1, 404, { content: 'x' }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
  })

  it('deleteDirective：成功完成；404 → ApiError', async () => {
    await expect(deleteDirective(1, 2)).resolves.toBeUndefined()
    const error = await deleteDirective(1, 404).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('NOT_FOUND')
  })
})
