import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import {
  batchCancelReminders,
  cancelReminder,
  createReminder,
  getReminder,
  listReminders,
} from '@/api/reminders'
import { ApiError } from '@/api/client'
import { BACKEND_ORIGIN, mockReminder, okResult, server } from '@/test/server'

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

const EMPTY_PAGE = { items: [], total: -1, page: 1, page_size: 20, next_cursor: null, has_more: false }

describe('reminders 端点（人格作用域查询参数）', () => {
  it('listReminders：persona_id + status 进查询串；undefined 时不发送', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/reminders`, 'get', () =>
      HttpResponse.json(okResult(EMPTY_PAGE)),
    )
    await listReminders(3, { status: 'pending', cursor: 'c', limit: 50 })
    const first = captured()
    expect(first?.url.searchParams.get('persona_id')).toBe('3')
    expect(first?.url.searchParams.get('status')).toBe('pending')
    expect(first?.url.searchParams.get('cursor')).toBe('c')
    expect(first?.url.searchParams.get('limit')).toBe('50')

    await listReminders(undefined, {})
    const second = captured()
    expect(second?.url.searchParams.has('persona_id')).toBe(false)
  })

  it('createReminder：POST 体透传 + persona_id 查询参数', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/reminders`, 'post', () =>
      HttpResponse.json(okResult(mockReminder({ id: 9 }), '提醒已创建')),
    )
    const created = await createReminder(2, { title: '喝水', due_at_ms: 1_700_000_000_000 })
    const request = captured()
    expect(request?.method).toBe('POST')
    expect(request?.url.searchParams.get('persona_id')).toBe('2')
    expect(request?.body).toEqual({ title: '喝水', due_at_ms: 1_700_000_000_000 })
    expect(created.id).toBe(9)
  })

  it('getReminder：404 → ApiError（隔离语义）', async () => {
    const error = await getReminder(1, 404).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('NOT_FOUND')
  })

  it('cancelReminder：成功返回 cancelled 记录', async () => {
    const record = await cancelReminder(1, 3)
    expect(record.status).toBe('cancelled')
  })

  it('cancelReminder：已投递状态守卫 422 → ApiError', async () => {
    const error = await cancelReminder(1, 2).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(422)
    expect((error as ApiError).code).toBe('VALIDATION_ERROR')
  })

  it('batchCancelReminders：ids 透传 + 部分成功响应', async () => {
    const { captured } = capture(`${BACKEND_ORIGIN}/reminders/batch-cancel`, 'post', () =>
      HttpResponse.json(
        okResult({
          total: 2,
          succeeded: 1,
          failed: 1,
          items: [
            { id: 3, success: true, code: null, message: '已取消' },
            { id: 1, success: false, code: 'NOT_FOUND', message: '提醒不存在' },
          ],
        }),
      ),
    )
    const result = await batchCancelReminders(1, [3, 1])
    expect(captured()?.body).toEqual({ ids: [3, 1] })
    expect(result.failed).toBe(1)
    expect(result.items[1]?.code).toBe('NOT_FOUND')
  })
})
