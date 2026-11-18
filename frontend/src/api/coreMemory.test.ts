import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  listCoreMemoryBlocks,
  listCoreMemoryRevisions,
  updateCoreMemoryBlock,
} from '@/api/coreMemory'
import { ApiError } from '@/api/client'
import {
  BACKEND_ORIGIN,
  errorResult,
  mockCoreMemoryRevision,
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
  method: 'get' | 'put',
  respond: () => ReturnType<typeof HttpResponse.json>,
): { captured: () => CapturedRequest | null } {
  let slot: CapturedRequest | null = null
  server.use(
    http[method](pattern, async ({ request }) => {
      const body = request.method === 'GET' ? null : await request.json().catch(() => null)
      slot = { method: request.method, url: new URL(request.url), body }
      return respond()
    }),
  )
  return { captured: () => slot }
}

describe('core-memory 端点（人格作用域查询参数）', () => {
  it('listCoreMemoryBlocks：persona_id 进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/core-memory/blocks`, 'get', () =>
      HttpResponse.json(okResult([])),
    )
    await listCoreMemoryBlocks(4)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    await listCoreMemoryBlocks(undefined)
    expect(captured()?.url.searchParams.get('persona_id')).toBeNull()
  })

  it('updateCoreMemoryBlock：PUT 路径 + 正文 / 锁定载荷 + persona_id', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/core-memory/blocks/:block`, 'put', () =>
      HttpResponse.json(okResult({})),
    )
    await updateCoreMemoryBlock(4, 'self_identity', { content: '新的自我认知', locked: true })
    expect(captured()?.method).toBe('PUT')
    expect(captured()?.url.pathname.endsWith('/core-memory/blocks/self_identity')).toBe(true)
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(captured()?.body).toEqual({ content: '新的自我认知', locked: true })
  })

  it('listCoreMemoryRevisions：块路径 + 条数上限 + persona_id', async () => {
    const { captured } = capture(
      `${BACKEND_ORIGIN}/core-memory/blocks/:block/revisions`,
      'get',
      () => HttpResponse.json(okResult([mockCoreMemoryRevision()])),
    )
    const revisions = await listCoreMemoryRevisions(4, 'open_notes', 5)
    expect(
      captured()?.url.pathname.endsWith('/core-memory/blocks/open_notes/revisions'),
    ).toBe(true)
    expect(captured()?.url.searchParams.get('limit')).toBe('5')
    expect(captured()?.url.searchParams.get('persona_id')).toBe('4')
    expect(revisions[0]?.version).toBe(3)
  })

  it('失败面：错误 ResultDTO 转 ApiError（中文消息 / 错误码）', async () => {
    server.use(
      http.get(`${BACKEND_ORIGIN}/core-memory/blocks`, () =>
        HttpResponse.json(errorResult('核心记忆块不存在', 'NOT_FOUND'), { status: 404 }),
      ),
    )
    await expect(listCoreMemoryBlocks(4)).rejects.toBeInstanceOf(ApiError)
    await expect(listCoreMemoryBlocks(4)).rejects.toMatchObject({
      code: 'NOT_FOUND',
      status: 404,
    })
  })
})
