import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  correctFact,
  createFact,
  deleteFact,
  listFacts,
  rejectFact,
} from '@/api/facts'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, mockFact, okResult, server } from '@/test/server'

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

describe('facts 端点（人格作用域查询参数）', () => {
  it('listFacts：persona_id / 过滤 / 游标进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/facts`, 'get', () =>
      HttpResponse.json(
        okResult({
          items: [mockFact()],
          total: -1,
          page: 1,
          page_size: 20,
          next_cursor: null,
          has_more: false,
        }),
      ),
    )
    const page = await listFacts(4, { category: 'preference', status: 'active', cursor: 'abc', limit: 5 })
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(captured()?.url.searchParams.get('category')).toBe('preference')
    expect(captured()?.url.searchParams.get('status')).toBe('active')
    expect(captured()?.url.searchParams.get('cursor')).toBe('abc')
    expect(captured()?.url.searchParams.get('limit')).toBe('5')
    expect(page.items[0]?.category).toBe('preference')

    await listFacts(undefined)
    expect(captured()?.url.searchParams.has('persona_id')).toBe(false)
    expect(captured()?.url.searchParams.has('category')).toBe(false)
  })

  it('createFact：POST 体透传（category / content / confidence）', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/facts`, 'post', () =>
      HttpResponse.json(okResult(mockFact({ id: 9 }), '人物事实已保存')),
    )
    await createFact(2, { category: 'biography', content: '主人在杭州工作', confidence: 0.7 })
    expect(captured()?.url.searchParams.get('persona_id')).toBe('2')
    expect(captured()?.body).toEqual({
      category: 'biography',
      content: '主人在杭州工作',
      confidence: 0.7,
    })
  })

  it('correctFact：PUT 更正路径 + 404 → ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/facts/:factId`, 'put', () =>
      HttpResponse.json(okResult(mockFact({ id: 101, version: 2 }), '人物事实已更正')),
    )
    const corrected = await correctFact(3, 1, { content: '主人其实更喜欢茶' })
    expect(captured()?.url.pathname.endsWith('/facts/1')).toBe(true)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('3')
    expect(captured()?.body).toEqual({ content: '主人其实更喜欢茶' })
    expect(corrected.version).toBe(2)

    server.use(
      http.put(`${BACKEND_ORIGIN}/facts/:factId`, () =>
        HttpResponse.json(
          { success: false, code: 'NOT_FOUND', message: '人物事实不存在', data: null },
          { status: 404 },
        ),
      ),
    )
    await expect(correctFact(3, 404, { content: '不存在' })).rejects.toBeInstanceOf(ApiError)
  })

  it('rejectFact：POST reject 路径', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/facts/:factId/reject`, 'post', () =>
      HttpResponse.json(okResult(mockFact({ id: 1, status: 'rejected' }), '人物事实已驳回')),
    )
    const rejected = await rejectFact(2, 1)
    expect(captured()?.url.pathname.endsWith('/facts/1/reject')).toBe(true)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('2')
    expect(rejected.status).toBe('rejected')
  })

  it('deleteFact：DELETE 路径 + 404 抛 ApiError', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/facts/:factId`, 'delete', () =>
      HttpResponse.json(okResult(null, '人物事实已删除')),
    )
    await deleteFact(2, 1)
    expect(captured()?.method).toBe('DELETE')
    expect(captured()?.url.pathname.endsWith('/facts/1')).toBe(true)

    server.use(
      http.delete(`${BACKEND_ORIGIN}/facts/:factId`, () =>
        HttpResponse.json(
          { success: false, code: 'NOT_FOUND', message: '人物事实不存在', data: null },
          { status: 404 },
        ),
      ),
    )
    await expect(deleteFact(2, 404)).rejects.toBeInstanceOf(ApiError)
  })
})
